#!/usr/bin/env python3
"""
Cliente demo de la API de capital.com — SOLO ENTORNO DEMO por defecto.

Lee las credenciales desde variables de entorno (.env), nunca del código.
Comandos:
  python capital_client.py login        -> prueba el login y muestra la cuenta
  python capital_client.py account       -> saldo y cuentas
  python capital_client.py search PLATA  -> busca instrumentos (epics) por texto
  python capital_client.py price SILVER  -> precio actual de un epic
  python capital_client.py positions     -> posiciones abiertas
  python capital_client.py buy SILVER 1  -> abre compra de tamaño 1 (SOLO DEMO)
  python capital_client.py sell SILVER 1 -> abre venta de tamaño 1 (SOLO DEMO)
  python capital_client.py close DIID..   -> cierra una posición por dealId

Requiere: pip install requests python-dotenv
"""
import os
import sys
import json
import requests
from dotenv import load_dotenv

load_dotenv()

ENV = os.getenv("CAPITAL_ENV", "demo").lower()
ALLOW_LIVE = os.getenv("CAPITAL_ALLOW_LIVE", "0") == "1"

BASES = {
    "demo": "https://demo-api-capital.backend-capital.com",
    "live": "https://api-capital.backend-capital.com",
}

if ENV == "live" and not ALLOW_LIVE:
    sys.exit("⛔ CAPITAL_ENV=live está bloqueado. Este esqueleto es para practicar en demo.\n"
             "   Si de verdad quieres operar en real, es bajo tu responsabilidad: pon CAPITAL_ALLOW_LIVE=1.")

BASE = BASES.get(ENV)
if not BASE:
    sys.exit(f"CAPITAL_ENV inválido: {ENV!r} (usa 'demo' o 'live')")

API_KEY = os.getenv("CAPITAL_API_KEY")
IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER")
API_PASSWORD = os.getenv("CAPITAL_API_PASSWORD")
if not all([API_KEY, IDENTIFIER, API_PASSWORD]):
    sys.exit("Faltan credenciales en .env (CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_API_PASSWORD).")


def login():
    """Abre sesión y devuelve los headers de autenticación. Reintenta ante rate-limit (429)."""
    import time
    for intento in range(4):
        r = requests.post(
            f"{BASE}/api/v1/session",
            headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"},
            json={"identifier": IDENTIFIER, "password": API_PASSWORD, "encryptedPassword": False},
            timeout=30,
        )
        if r.status_code == 200:
            return {
                "X-CAP-API-KEY": API_KEY,
                "CST": r.headers["CST"],
                "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"],
                "Content-Type": "application/json",
            }
        if r.status_code == 429 and intento < 3:
            time.sleep(4 * (intento + 1))    # backoff 4/8/12s ante "too-many-requests"
            continue
        sys.exit(f"Login falló ({r.status_code}): {r.text}")


def debug_headers():
    """Login y muestra SOLO los nombres de los headers de respuesta (sin valores)."""
    r = requests.post(
        f"{BASE}/api/v1/session",
        headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"},
        json={"identifier": IDENTIFIER, "password": API_PASSWORD, "encryptedPassword": False},
        timeout=30,
    )
    print("Login status:", r.status_code)
    print("Nombres EXACTOS de headers de respuesta (como los envía capital.com):")
    for k in r.headers.keys():
        print("  -", k)


def get(h, path):
    return requests.get(f"{BASE}{path}", headers=h, timeout=30)


def post(h, path, body):
    return requests.post(f"{BASE}{path}", headers=h, json=body, timeout=30)


def cmd_account(h):
    r = get(h, "/api/v1/accounts")
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))


def cmd_search(h, term):
    r = get(h, f"/api/v1/markets?searchTerm={term}")
    for m in r.json().get("markets", [])[:20]:
        print(f"{m.get('epic'):<20} {m.get('instrumentName')}  ({m.get('instrumentType')})  bid={m.get('bid')} ask={m.get('offer')}")


def cmd_price(h, epic):
    r = get(h, f"/api/v1/markets/{epic}")
    print(json.dumps(r.json().get("snapshot", r.json()), indent=2, ensure_ascii=False))


def cmd_positions(h):
    r = get(h, "/api/v1/positions")
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))


def _parse_kv(extra):
    """Convierte ['sl=29.5','tp=31.0'] en {'sl':29.5,'tp':31.0}."""
    out = {}
    for a in extra:
        if "=" in a:
            k, v = a.split("=", 1)
            out[k.strip().lower()] = float(v)
    return out


def cmd_order(h, epic, size, direction, extra):
    kv = _parse_kv(extra)
    body = {"epic": epic, "direction": direction, "size": float(size)}
    if "sl" in kv:
        body["stopLevel"] = kv["sl"]        # Stop Loss como nivel de precio
    if "tp" in kv:
        body["profitLevel"] = kv["tp"]      # Take Profit como nivel de precio
    tpsl = f"  SL={kv.get('sl','-')}  TP={kv.get('tp','-')}"
    print(f"[{ENV.upper()}] {direction} {size} de {epic}{tpsl} ...")
    r = post(h, "/api/v1/positions", body)
    if r.status_code not in (200, 201):
        sys.exit(f"Orden rechazada ({r.status_code}): {r.text}")
    ref = r.json().get("dealReference")
    conf = get(h, f"/api/v1/confirms/{ref}")
    print(json.dumps(conf.json(), indent=2, ensure_ascii=False))


def cmd_modify(h, deal_id, extra):
    """Ajusta SL/TP de una posición ya abierta: modify <dealId> sl=.. tp=.."""
    kv = _parse_kv(extra)
    body = {}
    if "sl" in kv:
        body["stopLevel"] = kv["sl"]
    if "tp" in kv:
        body["profitLevel"] = kv["tp"]
    if not body:
        sys.exit("Indica sl= y/o tp=  (ej: modify DIID... sl=29.5 tp=31.0)")
    r = requests.put(f"{BASE}/api/v1/positions/{deal_id}", headers=h, json=body, timeout=30)
    print(r.status_code, r.text)


def cmd_history(h, epic, resolution="HOUR", n="200"):
    """Descarga OHLC histórico para analizar el gráfico.
    resolution: MINUTE, MINUTE_5, MINUTE_15, HOUR, HOUR_4, DAY, WEEK"""
    r = get(h, f"/api/v1/prices/{epic}?resolution={resolution}&max={n}")
    if r.status_code != 200:
        sys.exit(f"No se pudo obtener histórico ({r.status_code}): {r.text}")
    prices = r.json().get("prices", [])
    rows = []
    for p in prices:
        o, hi, lo, c = p.get("openPrice"), p.get("highPrice"), p.get("lowPrice"), p.get("closePrice")
        mid = lambda x: (x.get("bid", 0) + x.get("ask", 0)) / 2 if isinstance(x, dict) else x
        rows.append({"t": p.get("snapshotTimeUTC") or p.get("snapshotTime"),
                     "o": round(mid(o), 5), "h": round(mid(hi), 5),
                     "l": round(mid(lo), 5), "c": round(mid(c), 5)})
    # imprime JSON compacto: pégamelo y yo analizo el gráfico
    print(json.dumps({"epic": epic, "resolution": resolution, "n": len(rows), "candles": rows},
                     ensure_ascii=False))


def cmd_close(h, deal_id):
    r = requests.delete(f"{BASE}/api/v1/positions/{deal_id}", headers=h, timeout=30)
    print(r.status_code, r.text)


def cmd_close_all(h):
    """Cierra TODAS las posiciones abiertas (usa el dealId correcto de cada una)."""
    pos = get(h, "/api/v1/positions").json().get("positions", [])
    if not pos:
        print("No hay posiciones abiertas."); return
    for p in pos:
        did = p["position"]["dealId"]
        epic = p["market"]["epic"]
        r = requests.delete(f"{BASE}/api/v1/positions/{did}", headers=h, timeout=30)
        print(f"cerrar {epic} {did} -> {r.status_code} {r.text}")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    h = login()
    if args[0] == "debug-headers":
        debug_headers(); return
    if args[0] == "login":
        print(f"✅ Login OK en entorno {ENV.upper()}.")
        cmd_account(h)
    elif args[0] == "account":
        cmd_account(h)
    elif args[0] == "search" and len(args) > 1:
        cmd_search(h, args[1])
    elif args[0] == "price" and len(args) > 1:
        cmd_price(h, args[1])
    elif args[0] == "positions":
        cmd_positions(h)
    elif args[0] == "history" and len(args) > 1:
        cmd_history(h, args[1], *(args[2:4]))
    elif args[0] == "buy" and len(args) > 2:
        cmd_order(h, args[1], args[2], "BUY", args[3:])
    elif args[0] == "sell" and len(args) > 2:
        cmd_order(h, args[1], args[2], "SELL", args[3:])
    elif args[0] == "modify" and len(args) > 1:
        cmd_modify(h, args[1], args[2:])
    elif args[0] == "close" and len(args) > 1:
        cmd_close(h, args[1])
    elif args[0] == "close-all":
        cmd_close_all(h)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
