#!/usr/bin/env python3
"""Baja precios de mercado sin IA y los guarda como JSON en data/.

Fuentes (todas públicas, sin clave):
- data912.com: precios de BYMA en vivo (CEDEARs, acciones, bonos, letras, ONs) e historial diario por ticker.
- api.argentinadatos.com: dólar MEP (bolsa) y CCL, historial completo.
- Yahoo Finance (chart): acciones y ETF de EE.UU., incluidos los benchmarks.

Salida:
- data/last.json          último precio de TODOS los símbolos de BYMA {kind: {SYM: [precio, var%, volumen]}}
- data/hist/<SYM>.json    historial diario de los tickers de tickers.json {"YYYY-MM-DD": cierre}
- data/us/<SYM>.json      historial diario de EE.UU. {"YYYY-MM-DD": cierre}
- data/fx.json            {"mep": {...}, "ccl": {...}} valores de venta por fecha
- data/status.json        qué se bajó, qué falló y cuándo
"""
import json, os, sys, time, datetime, warnings, urllib.request, urllib.error, pathlib
warnings.filterwarnings('ignore', category=DeprecationWarning)

R = pathlib.Path(__file__).resolve().parent.parent
D = R / "data"
UA = {"User-Agent": "Mozilla/5.0 (cartera-datos; github actions)", "Accept": "application/json"}
status = {"startedAt": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z", "ok": [], "errors": [], "samples": {}}


T0 = time.time()
BUDGET = 9 * 60  # segundos para el historial de BYMA; lo que no entra queda para la próxima corrida


def get(url, tries=3, pause=1.0, timeout=30):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa
            last = e
            time.sleep(pause * (i + 1) * 2)
    raise RuntimeError(f"{url}: {last}")


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True))
    tmp.replace(path)


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def num(x):
    try:
        v = float(x)
        return v if v == v else None
    except Exception:
        return None


def pick(row, *keys):
    for k in keys:
        if isinstance(row, dict) and k in row and row[k] not in (None, ""):
            return row[k]
    return None


def norm_date(s):
    s = str(s)[:10]
    if len(s) == 10 and s[4] == "-":
        return s
    if len(s) == 10 and s[2] == "/":  # dd/mm/aaaa
        return f"{s[6:10]}-{s[3:5]}-{s[0:2]}"
    return None


cfg = json.loads((R / "tickers.json").read_text())
DESDE = cfg.get("desde", "2023-06-01")
today = datetime.datetime.utcnow() - datetime.timedelta(hours=3)  # hora de Buenos Aires
TODAY = today.strftime("%Y-%m-%d")

# ---------- 1. BYMA en vivo: todos los símbolos ----------
LIVE = {"cedears": "arg_cedears", "stocks": "arg_stocks", "bonds": "arg_bonds", "notes": "arg_notes", "corp": "arg_corp"}
last = {"asOf": TODAY, "updatedAt": None, "src": "data912 live", "byma": {}}
for kind, ep in LIVE.items():
    try:
        rows = get(f"https://data912.com/live/{ep}")
        status["samples"]["live_" + kind] = rows[:2] if isinstance(rows, list) else str(rows)[:300]
        out = {}
        for r in rows if isinstance(rows, list) else []:
            sym = pick(r, "symbol", "ticker", "simbolo")
            c = num(pick(r, "c", "close", "last", "px_last"))
            if not sym or c is None or c <= 0:
                continue
            out[str(sym).upper()] = [c, num(pick(r, "pct_change", "pct")), num(pick(r, "v", "volume"))]
        last["byma"][kind] = out
        status["ok"].append(f"live {kind}: {len(out)}")
    except Exception as e:
        status["errors"].append(f"live {kind}: {e}")
    time.sleep(0.6)
for ep in ("mep", "ccl"):
    try:
        rows = get(f"https://data912.com/live/{ep}")
        status["samples"]["live_" + ep] = rows[:2] if isinstance(rows, list) else rows
        last[ep + "_raw_n"] = len(rows) if isinstance(rows, list) else None
    except Exception as e:
        status["errors"].append(f"live {ep}: {e}")
last["updatedAt"] = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
if any(last["byma"].values()):
    save(D / "last.json", last)


def yahoo(sym, p1, p2, total=False):
    # total=True: cierre ajustado por dividendos (rendimiento total, para benchmarks y acciones de EE.UU.)
    j = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d&events=split", tries=2, timeout=25)
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError("sin datos: " + str((j.get("chart") or {}).get("error"))[:200])
    ts = res[0].get("timestamp") or []
    ind = res[0].get("indicators") or {}
    closes = (ind.get("quote") or [{}])[0].get("close") or []
    adj = (ind.get("adjclose") or [{}])[0].get("adjclose") if total else None
    tz = (res[0].get("meta") or {}).get("gmtoffset") or -18000
    out = {}
    for i, t in enumerate(ts):
        c = adj[i] if adj and i < len(adj) and adj[i] is not None else (closes[i] if i < len(closes) else None)
        if c is None:
            continue
        out[datetime.datetime.utcfromtimestamp(t + tz).strftime("%Y-%m-%d")] = round(float(c), 4)
    return out


P1 = int(datetime.datetime.strptime(DESDE, "%Y-%m-%d").timestamp())
P2 = int(time.time()) + 86400

# ---------- 2. Historial diario BYMA de los tickers de la lista ----------
alllive = {}
for kind, m in last["byma"].items():
    for sym, v in m.items():
        alllive.setdefault(sym, v)
HIST_KINDS = {"cedears", "stocks", "bonds"}
for kind, syms in cfg.get("byma", {}).items():
    for sym in syms:
        path = D / "hist" / f"{sym}.json"
        cur = load(path, {})
        n0 = len(cur)
        if kind in HIST_KINDS:
            rows = None
            if time.time() - T0 < BUDGET:
                try:
                    rows = get(f"https://data912.com/historical/{kind}/{sym}", tries=2, timeout=25)
                except Exception as e:
                    rows = e
            else:
                rows = "sin tiempo en esta corrida"
            if isinstance(rows, list) and rows:
                for r in rows:
                    d = norm_date(pick(r, "date", "fecha", "d"))
                    c = num(pick(r, "c", "close", "cierre"))
                    if d and c and c > 0 and d >= DESDE:
                        cur[d] = c
                fechas = sorted(norm_date(pick(r, "date")) or "" for r in rows)
                status["ok"].append(f"hist {sym}: {len(rows)} ruedas en la fuente ({fechas[0]} a {fechas[-1]}), {len(cur)} guardadas (+{len(cur) - n0})")
            else:
                status["errors"].append(f"hist {sym}: data912 no tiene historial ({str(rows)[:80]})")
            # respaldo: Yahoo Finance tiene los CEDEARs y bonos de BYMA como <TICKER>.BA (completa lo que falte, no pisa data912)
            if not (isinstance(rows, list) and len(rows) > 300) and time.time() - T0 < BUDGET:
                try:
                    yb = yahoo(sym + ".BA", P1, P2)
                    n1 = len(cur)
                    for d, c in yb.items():
                        if d >= DESDE and d not in cur and c > 0:
                            cur[d] = c
                    status["ok"].append(f"hist {sym}: Yahoo {sym}.BA {len(yb)} ruedas (+{len(cur) - n1})")
                except Exception as e:
                    status["errors"].append(f"hist {sym}: Yahoo {sym}.BA tampoco ({str(e)[:120]})")
        # el último precio en vivo también suma al historial (así crece aunque no haya endpoint histórico)
        lv = alllive.get(sym)
        if lv and today.weekday() < 5:
            cur[TODAY] = lv[0]
        elif not lv:
            status["errors"].append(f"live {sym}: no figura en los paneles de BYMA de data912")
        if cur:
            save(path, cur)
        time.sleep(0.7)

# ---------- 2b. Alias: tickers anteriores del mismo activo ----------
for sym, olds in (cfg.get("alias") or {}).items():
    path = D / "hist" / f"{sym}.json"
    cur = load(path, {})
    n0 = len(cur)
    for old in olds:
        try:
            rows = get(f"https://data912.com/historical/cedears/{old}", tries=2, timeout=25)
            got = {norm_date(pick(r, "date")): num(pick(r, "c", "close")) for r in rows} if isinstance(rows, list) else {}
        except Exception:
            got = {}
        if not got:
            try:
                got = yahoo(old + ".BA", P1, P2)
            except Exception as e:
                status["errors"].append(f"alias {old}: {str(e)[:120]}")
        first_new = min(cur) if cur else "9999"
        for d, c in got.items():
            if d and c and c > 0 and d >= DESDE and d < first_new:
                cur[d] = c
    if cur:
        save(path, cur)
    status["ok"].append(f"alias {sym}: +{len(cur) - n0} ruedas de {', '.join(olds)}")

# ---------- 3. EE.UU. (Yahoo Finance) ----------
for sym in cfg.get("usa", []):
    path = D / "us" / f"{sym}.json"
    cur = load(path, {})
    try:
        n0 = len(cur)
        tot = sym in ("SPY", "ACWI", "AGG", "GLD")  # benchmarks: rendimiento total (dividendos reinvertidos); el resto, cierre real
        cur = yahoo(sym, P1, P2, total=tot) if tot else dict(cur, **yahoo(sym, P1, P2))
        save(path, cur)
        status["ok"].append(f"us {sym}: {len(cur)} (+{len(cur) - n0})")
    except Exception as e:
        status["errors"].append(f"us {sym}: {e}")
    time.sleep(0.5)

# ---------- 4. Dólar MEP y CCL (argentinadatos) ----------
fx = load(D / "fx.json", {"mep": {}, "ccl": {}})
for key, casa in (("mep", "bolsa"), ("ccl", "contadoconliqui")):
    try:
        rows = get(f"https://api.argentinadatos.com/v1/cotizaciones/dolares/{casa}")
        status["samples"]["fx_" + key] = rows[-2:] if isinstance(rows, list) else str(rows)[:300]
        n0 = len(fx[key])
        for r in rows:
            d = norm_date(pick(r, "fecha", "date"))
            v = num(pick(r, "venta", "sell"))
            if d and v and d >= DESDE:
                fx[key][d] = v
        status["ok"].append(f"fx {key}: {len(fx[key])} (+{len(fx[key]) - n0})")
    except Exception as e:
        status["errors"].append(f"fx {key}: {e}")
fx["src"] = "argentinadatos (venta)"
save(D / "fx.json", fx)

status["finishedAt"] = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
save(D / "status.json", status)
print(json.dumps({"ok": len(status["ok"]), "errors": status["errors"]}, ensure_ascii=False, indent=1))
