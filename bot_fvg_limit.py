#!/usr/bin/env python3
"""
=======================================================================
  Bot ORO FVG con ENTRADA POR ORDEN LIMITE  (DESPLEGADO 26-ago-2026)
=======================================================================
EN VIVO (demo): el workflow bot-fvg.yml corre ESTE archivo desde el 26-ago.
Reemplazo a bot_fvg.py (que entraba a mercado). Revertir = volver el yaml a
'python bot_fvg.py'. En MONITOREO: verificar en los primeros 5-10 fills que
las ordenes se coloquen/llenen en el borde y que el acierto suba hacia ~74%.

POR QUE EXISTE
--------------
El bot_fvg.py actual entra A MERCADO al cierre de la vela que rellena el
hueco, y mide el riesgo desde el precio VIVO hasta el borde. Un backtest de
ese mecanismo exacto ("mercado") da -25 (1h/2anos) y -36 (15m/60d), acierto
~60% -> coincide con lo que se ve en vivo (40%, P&L negativo).

En cambio, entrar EN EL BORDE del hueco (lo que asume el backtest validado)
da +430 ROB3 / 74% (1h) y +119 ROB3 / 75% (15m). Misma estrategia, distinto
FILL. La diferencia entre ganar y perder ES el mecanismo de entrada.

Este borrador coloca una ORDEN LIMITE PENDIENTE en el borde del hueco, con
SL y TP calculados desde el TAMANO del hueco (no desde el precio vivo), y
expiracion a FILL_WIN velas de formado el hueco. Asi el fill ocurre al precio
validado, replicando el backtest.

DISENO (stateless, cron cada 15m)
---------------------------------
- Cada corrida busca el FVG mas reciente que: (a) paso los filtros (EMA50,
  min-gap), (b) TODAVIA NO se rellena, (c) sigue fresco (< FILL_WIN velas).
- Si NO hay posicion abierta NI working order de este bot -> coloca la orden
  limite en el borde, con expiracion. Como maximo 1 orden pendiente a la vez
  (igual que la regla de 1 posicion). capital.com la rellena sola cuando el
  precio retrocede al borde, y cierra sola en SL/TP.
- Idempotencia: si ya hay working order (o posicion) de tamano SIZE en GOLD,
  no coloca otra. La expiracion (goodTillDate) limpia las que no se llenan.

PENDIENTE DE VERIFICAR EN VIVO (con el usuario):
  * Formato/soporte de goodTillDate en la cuenta demo.
  * Distancia minima permitida entre el nivel limite y el precio actual
    (capital.com puede rechazar niveles demasiado cercanos).
  * Que el fill respete el nivel (limite = sin slippage adverso teorico).
  * Registrar acierto real vs el 74% esperado tras >=20-30 fills.

Uso: python bot_fvg_limit.py [--status] [--dry-run]
"""
import sys
from datetime import datetime, timezone, timedelta
import capital_client as cc

EPIC     = "GOLD"
SIZE     = 1.0           # Subido de 0.3 a 1.0 el 26-ago (a pedido). Riesgo ~$12/trade
                         # (SL 1.5xgap, gap ~$8). Margen ~$230. OJO margen total de los 3
                         # bots ~$736 -> nivel de margen ~136% peor caso (mas justo). Aun
                         # SIN validar (1 trade) -> vigilar los primeros resultados.
SL_MULT  = 1.5           # SL = borde - 1.5 x tamano_hueco  (validado)
TP_R     = 1.0           # TP = borde + 1.0 x tamano_hueco  (validado, ROB3)
FILL_WIN = 20            # velas de vida del hueco antes de expirar la orden
BAR_MIN  = 15
ATR_LEN  = 14
MIN_GAP  = 0.4
MAX_GAP  = 3.0           # tope: salta huecos > 3xATR. Acota el riesgo por trade (SL=1.5xgap)
                         # evitando outliers gigantes (ej. hueco $54 -> perdida $82 ~7% cuenta).
                         # No toca los multiplicadores validados; solo descarta el caso extremo.
EMA_TREND = 50           # continuacion con la tendencia (EMA50, validado en 2.4 anos)


def _rma(s, k):
    out = [None] * len(s)
    if len(s) < k:
        return out
    p = sum(s[:k]) / k; out[k-1] = p
    for i in range(k, len(s)):
        p = (p * (k-1) + s[i]) / k; out[i] = p
    return out


def atr_series(h, l, c, k):
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return _rma(tr, k)


def ema_series(s, k):
    out = [s[0]]; a = 2 / (k + 1)
    for i in range(1, len(s)):
        out.append(s[i] * a + out[-1] * (1 - a))
    return out


def _mid(x):
    return (x["bid"] + x["ask"]) / 2 if isinstance(x, dict) else x


def current_bar_start():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(minute=(now.minute // BAR_MIN) * BAR_MIN, second=0, microsecond=0)


def fetch_closed(h):
    r = cc.get(h, f"/api/v1/prices/{EPIC}?resolution=MINUTE_15&max=200")
    if r.status_code != 200:
        sys.exit(f"No se pudo bajar precios ({r.status_code}): {r.text}")
    bar0 = current_bar_start()
    O, H, L, C = [], [], [], []
    for p in r.json().get("prices", []):
        t = (p.get("snapshotTimeUTC") or p.get("snapshotTime") or "").replace("Z", "")
        try:
            bt = datetime.fromisoformat(t)
        except ValueError:
            continue
        if bt >= bar0:
            continue
        O.append(_mid(p["openPrice"])); H.append(_mid(p["highPrice"]))
        L.append(_mid(p["lowPrice"]));  C.append(_mid(p["closePrice"]))
    return O, H, L, C


def find_pending_fvg(h):
    """Devuelve el FVG mas reciente FORMADO y AUN NO rellenado que pasa los
    filtros, listo para colocar una orden limite en su borde. None si no hay."""
    O, H, L, C = fetch_closed(h)
    if len(C) < FILL_WIN + 4:
        sys.exit("Pocas velas para calcular.")
    i = len(C) - 1
    atr = atr_series(H, L, C, ATR_LEN)
    ema = ema_series(C, EMA_TREND)
    # buscar de la vela mas reciente hacia atras el primer FVG valido no relleno
    for j in range(i, max(i - FILL_WIN, 2) - 1, -1):
        if j < 2:
            break
        bull = L[j] > H[j-2] and C[j] > C[j-2]
        bear = H[j] < L[j-2] and C[j] < C[j-2]
        if not (bull or bear):
            continue
        # filtro de tendencia (continuacion con EMA50)
        if (bull and C[j] <= ema[j]) or (bear and C[j] >= ema[j]):
            continue
        if bull:
            gap_top, gap_bot, dr = L[j], H[j-2], 1
        else:
            gap_top, gap_bot, dr = L[j-2], H[j], -1
        g = gap_top - gap_bot
        a = atr[j] or 0
        if g < MIN_GAP * a:
            continue
        if a > 0 and g > MAX_GAP * a:       # salta huecos outlier gigantes (riesgo desproporcionado)
            continue
        # NO debe haberse rellenado desde que se formo (borde aun sin tocar)
        filled = False
        for k in range(j + 1, i + 1):
            if (dr == 1 and L[k] <= gap_top) or (dr == -1 and H[k] >= gap_bot):
                filled = True; break
        if filled:
            continue
        # borde de entrada y niveles desde el TAMANO del hueco (como el backtest)
        if dr == 1:
            level = gap_top
            sl = level - SL_MULT * g
            tp = level + TP_R * g
            side = "BUY"
        else:
            level = gap_bot
            sl = level + SL_MULT * g
            tp = level - TP_R * g
            side = "SELL"
        age = i - j                     # velas desde que se formo
        remaining = FILL_WIN - age      # velas restantes de vida
        if remaining <= 0:
            continue
        return {"side": side, "level": round(level, 1), "sl": round(sl, 1),
                "tp": round(tp, 1), "gap": round(g, 2), "remaining_bars": remaining,
                "close": round(C[i], 1)}
    return {"close": round(C[i], 1)} if C else None


def _mysize(v):
    try:
        return abs(float(v) - SIZE) < 1e-9
    except (TypeError, ValueError):
        return False


def has_open_position(h):
    pos = cc.get(h, "/api/v1/positions").json().get("positions", [])
    return any(p["market"]["epic"] == EPIC and _mysize(p["position"]["size"]) for p in pos)


def has_working_order(h):
    r = cc.get(h, "/api/v1/workingorders")
    if r.status_code != 200:
        # ante duda, asumir que SI hay para no duplicar
        print(f"  (aviso: no pude leer working orders {r.status_code}) -> no coloco por seguridad")
        return True
    for w in r.json().get("workingOrders", []):
        mkt = w.get("marketData", {}); wod = w.get("workingOrderData", {})
        if mkt.get("epic") == EPIC and _mysize(wod.get("orderSize") or wod.get("size")):
            return True
    return False


def main():
    dry = "--dry-run" in sys.argv
    status = "--status" in sys.argv
    h = cc.login()
    setup = find_pending_fvg(h)
    close = setup.get("close") if setup else None
    print(f"[ORO FVG-LIMIT 15m GOLD] close={close}")
    if not setup or "side" not in setup:
        print("  >> sin FVG pendiente (formado y sin rellenar) ahora")
        return
    print(f"  >> FVG PENDIENTE {setup['side']}  borde(limite)={setup['level']} "
          f"SL={setup['sl']} TP={setup['tp']} gap={setup['gap']} vida={setup['remaining_bars']}v")
    if status:
        return
    if has_open_position(h):
        print("  Ya hay posicion FVG abierta en GOLD -> no coloco orden."); return
    if has_working_order(h):
        print("  Ya hay working order FVG en GOLD -> no duplico."); return
    if dry:
        print("  [DRY-RUN] No coloco la orden limite."); return
    # validar que el nivel esta del lado correcto del precio actual
    snap = cc.get(h, f"/api/v1/markets/{EPIC}").json().get("snapshot", {})
    bid, offer = snap.get("bid"), snap.get("offer")
    if setup["side"] == "BUY" and offer is not None and setup["level"] >= offer:
        print(f"  El borde {setup['level']} ya no esta bajo el precio ({offer}) -> ya se toco, no entro."); return
    if setup["side"] == "SELL" and bid is not None and setup["level"] <= bid:
        print(f"  El borde {setup['level']} ya no esta sobre el precio ({bid}) -> ya se toco, no entro."); return
    expiry = (datetime.now(timezone.utc).replace(tzinfo=None)
              + timedelta(minutes=setup["remaining_bars"] * BAR_MIN)).strftime("%Y-%m-%dT%H:%M:%S")
    body = {"epic": EPIC, "direction": setup["side"], "size": SIZE, "level": setup["level"],
            "type": "LIMIT", "stopLevel": setup["sl"], "profitLevel": setup["tp"],
            "goodTillDate": expiry}
    r = cc.post(h, "/api/v1/workingorders", body)
    if r.status_code not in (200, 201):
        print(f"  Orden limite NO colocada ({r.status_code}): {r.text}"); return
    ref = r.json().get("dealReference")
    conf = cc.get(h, f"/api/v1/confirms/{ref}").json()
    print(f"  ORDEN LIMITE COLOCADA: {setup['side']} {SIZE} {EPIC} @ {setup['level']} "
          f"SL={setup['sl']} TP={setup['tp']} exp={expiry} ref={ref} status={conf.get('dealStatus')}")


if __name__ == "__main__":
    main()
