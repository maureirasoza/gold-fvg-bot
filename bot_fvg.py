#!/usr/bin/env python3
"""
Bot ORO FVG (Fair Value Gap) intradia 15m — capital.com DEMO.

Estrategia de CONTINUACION (distinta a la de reversion del otro bot de oro):
  - Un FVG alcista es un hueco de desequilibrio: la vela j-2 y la j dejan un espacio
    (low[j] > high[j-2]) tras un impulso al alza.
  - Cuando el precio RETROCEDE y rellena ese hueco, se COMPRA esperando continuacion.
  - SL al otro lado del hueco; TP = 1.5 x riesgo.  Espejo para cortos.
Validado en backtest (72d, robusto split-half): mas rentable pero mas trades y menor
acierto (~38%) que la reversion. Corre en la MISMA cuenta con size 0.3 para no chocar
con el bot Bollinger (0.5).

NOTA de fidelidad: el backtest asume relleno con orden limite en el hueco; aca entra a
mercado al cierre de la vela que rellena. Los fills reales pueden diferir -> por eso se
prueba en demo y se registran costos.

Uso: python bot_fvg.py [--status] [--dry-run]
"""
import sys
from datetime import datetime, timezone, timedelta
import capital_client as cc

EPIC     = "GOLD"
SIZE     = 0.3           # distinto del bot Bollinger (0.5) para no chocar
TP_R     = 1.5           # Take Profit = 1.5 x riesgo
FILL_WIN = 20            # velas maximas de espera para que se rellene el hueco
BAR_MIN  = 15
ATR_LEN  = 14
MIN_GAP  = 0.4           # hueco minimo = 0.4 x ATR (evita stops bajo el ruido)


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


def _mid(x):
    return (x["bid"] + x["ask"]) / 2 if isinstance(x, dict) else x


def current_bar_start():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(minute=(now.minute // BAR_MIN) * BAR_MIN, second=0, microsecond=0)


def fetch_closed(h):
    r = cc.get(h, f"/api/v1/prices/{EPIC}?resolution=MINUTE_15&max=100")
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


def evaluate(h):
    O, H, L, C = fetch_closed(h)
    if len(C) < FILL_WIN + 4:
        sys.exit("Pocas velas para calcular.")
    i = len(C) - 1                       # ultima vela cerrada = vela de decision
    atr = atr_series(H, L, C, ATR_LEN)
    signal = None
    # buscar el FVG mas reciente (formado en [i-FILL_WIN, i-2]) que ESTA vela rellena por 1a vez
    for j in range(i - 1, max(i - FILL_WIN - 1, 2) - 1, -1):
        bull = L[j] > H[j-2] and C[j] > C[j-2]
        bear = H[j] < L[j-2] and C[j] < C[j-2]
        if not (bull or bear):
            continue
        if bull:
            gap_top, gap_bot, dr = L[j], H[j-2], 1
        else:
            gap_top, gap_bot, dr = L[j-2], H[j], -1
        # FILTRO: descarta huecos demasiado chicos (stop quedaria bajo el ruido)
        if (gap_top - gap_bot) < MIN_GAP * (atr[j] or 0):
            continue
        # el hueco no debe haberse rellenado antes de la vela i
        already = False
        for k in range(j + 1, i):
            if (dr == 1 and L[k] <= gap_top) or (dr == -1 and H[k] >= gap_bot):
                already = True; break
        if already:
            continue
        # la vela i lo rellena?
        if (dr == 1 and L[i] <= gap_top) or (dr == -1 and H[i] >= gap_bot):
            entry = C[i]
            if dr == 1:
                sl = gap_bot; risk = entry - sl
                tp = entry + TP_R * risk
            else:
                sl = gap_top; risk = sl - entry
                tp = entry - TP_R * risk
            if risk > 0:
                signal = {"side": "BUY" if dr == 1 else "SELL",
                          "entry": round(entry, 1), "sl": round(sl, 1), "tp": round(tp, 1)}
            break
    close = C[i]
    return {"close": round(close, 1), "signal": signal}


def _mysize(v):
    try:
        return abs(float(v) - SIZE) < 1e-9
    except (TypeError, ValueError):
        return False


def has_open_position(h):
    pos = cc.get(h, "/api/v1/positions").json().get("positions", [])
    return any(p["market"]["epic"] == EPIC and _mysize(p["position"]["size"]) for p in pos)


def acted_this_bar(h, bar0):
    frm = (bar0 - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    r = cc.get(h, f"/api/v1/history/activity?from={frm}")
    if r.status_code != 200:
        return False
    for a in r.json().get("activities", []):
        if a.get("epic") != EPIC or a.get("type") not in ("POSITION", "WORKING_ORDER"):
            continue
        if not _mysize(a.get("details", {}).get("size")):
            continue
        try:
            d = datetime.strptime(a["dateUTC"], "%Y-%m-%dT%H:%M:%S.%f")
        except (KeyError, ValueError):
            continue
        if d >= bar0:
            return True
    return False


def main():
    dry = "--dry-run" in sys.argv
    status = "--status" in sys.argv
    h = cc.login()
    ev = evaluate(h)
    sig = ev["signal"]
    print(f"[ORO FVG 15m GOLD] close={ev['close']}")
    if sig:
        print(f"  >> SENAL {sig['side']} (relleno de FVG)  entry={sig['entry']} SL={sig['sl']} TP={sig['tp']}")
    else:
        print("  >> sin FVG rellenado en la ultima vela")
    if status or not sig:
        return
    if has_open_position(h):
        print("  Ya hay posicion FVG abierta en GOLD -> no abro otra."); return
    bar0 = current_bar_start()
    if acted_this_bar(h, bar0):
        print(f"  Ya se opero FVG en esta vela 15m ({bar0}Z) -> candado."); return
    if dry:
        print("  [DRY-RUN] No coloco la orden."); return
    # Recalcular desde el precio ACTUAL: SL = borde del hueco (fijo); riesgo y TP desde el precio vivo.
    snap = cc.get(h, f"/api/v1/markets/{EPIC}").json().get("snapshot", {})
    sl = sig["sl"]
    if sig["side"] == "BUY":
        entry = snap.get("offer"); risk = entry - sl
    else:
        entry = snap.get("bid"); risk = sl - entry
    if risk is None or risk <= 0:
        print("  El precio ya paso el hueco (riesgo<=0) -> setup invalido, no entro."); return
    tp = round(entry + (TP_R * risk if sig["side"] == "BUY" else -TP_R * risk), 1)
    body = {"epic": EPIC, "direction": sig["side"], "size": SIZE, "stopLevel": sl, "profitLevel": tp}
    r = cc.post(h, "/api/v1/positions", body)
    if r.status_code not in (200, 201):
        print(f"  Orden NO colocada ({r.status_code}): {r.text} -> se reintenta en la proxima vela.")
        return
    ref = r.json().get("dealReference")
    conf = cc.get(h, f"/api/v1/confirms/{ref}").json()
    print(f"  ORDEN COLOCADA: {sig['side']} {SIZE} {EPIC} @ {entry} SL={sl} TP={tp} ref={ref} status={conf.get('dealStatus')}")


if __name__ == "__main__":
    main()
