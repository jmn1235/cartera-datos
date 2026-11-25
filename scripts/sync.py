#!/usr/bin/env python3
"""Arma las escrituras para la base de la app Cartera JM a partir de los datos de este repositorio.

Lo corre el "Actualizador" (tarea de Claude) en su entorno: no busca nada en la web ni transcribe números,
solo lee los JSON de este repo (raw.githubusercontent.com) y lo que ya hay en la base, y deja listos los
documentos a escribir. El Actualizador después los escribe con ArtifactData batch.

Uso:
  python3 sync.py --db /tmp/act/db --versions /tmp/act/versions.json --job /tmp/act/db/jobs/<id>.json --out /tmp/act/out

  --db        carpeta con lo que bajó ArtifactData list (out_dir): assets/, ops/, prices/, fx/, bench/
  --versions  JSON {"prices/KO": 5, "fx/y2026": 12, ...} con la versión de cada documento existente
  --job       el pedido (opcional; si falta se procesan todos los activos con operaciones)
  --out       carpeta de salida: docs/<colección>__<id>.json, writes_<n>.json (lotes listos para batch),
              job_patch.json (lo que va en el pedido al cerrarlo) y resumen.txt
"""
import argparse, datetime, json, pathlib, statistics, sys, urllib.request

RAW = "https://raw.githubusercontent.com/jmn1235/cartera-datos/main/data/"
NOW = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
TODAY = (datetime.datetime.utcnow() - datetime.timedelta(hours=3)).strftime("%Y-%m-%d")
SRC = "data912"
BOND_TYPES = {"bono", "on", "letra"}
BYMA_TYPES = {"cedear", "cedear_etf", "accion_ar", "bono", "on", "letra", "fci", "otro"}
BATCH_BYTES = 800_000
BATCH_N = 40


def fetch(name, default=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(RAW + name, headers={"User-Agent": "cartera-jm-sync"}), timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return default


def load_dir(p):
    out = {}
    if p.exists():
        for f in sorted(p.glob("*.json")):
            try:
                out[f.stem] = json.loads(f.read_text())
            except Exception:
                pass
    return out


def days(a, b):
    return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days


def add_days(d, n):
    return (datetime.date.fromisoformat(d) + datetime.timedelta(days=n)).isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--versions", required=True)
    ap.add_argument("--job")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    db, out = pathlib.Path(a.db), pathlib.Path(a.out)
    (out / "docs").mkdir(parents=True, exist_ok=True)
    versions = json.loads(pathlib.Path(a.versions).read_text()) if pathlib.Path(a.versions).exists() else {}

    assets = load_dir(db / "assets")
    ops = list(load_dir(db / "ops").values())
    prices = load_dir(db / "prices")
    fxdocs = load_dir(db / "fx")
    bench = load_dir(db / "bench")

    last = fetch("last.json", {}) or {}
    status = fetch("status.json", {}) or {}
    fxsrc = fetch("fx.json", {"mep": {}, "ccl": {}}) or {"mep": {}, "ccl": {}}
    if not last.get("byma"):
        sys.exit("No se pudo leer data/last.json del repositorio cartera-datos: la tarea de GitHub todavía no corrió o falló.")
    live = {}
    for kind, m in last["byma"].items():
        for sym, v in m.items():
            live.setdefault(sym, (kind, v))

    # activos canónicos (los unificados se cuentan como su destino) y primera operación de cada uno
    def canon(i):
        seen = 0
        while assets.get(i, {}).get("mergeInto") and assets.get(assets[i]["mergeInto"]) and seen < 5:
            i = assets[i]["mergeInto"]; seen += 1
        return i
    first = {}
    for o in ops:
        i = o.get("assetId")
        if not i or i not in assets or o.get("type") not in ("buy", "sell", "opening"):
            continue
        c = canon(i)
        if c not in first or o["date"] < first[c]:
            first[c] = o["date"]
    first_all = min(first.values()) if first else TODAY

    writes, results, notes = [], [], []
    summary = {"ok": 0, "discrepancy": 0, "single": 0, "error": 0}

    def put(coll, doc_id, data):
        p = out / "docs" / f"{coll}__{doc_id}.json"
        p.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
        w = {"op": "set", "collection": coll, "doc_id": doc_id, "file_path": str(p)}
        v = versions.get(f"{coll}/{doc_id}")
        if v is not None:
            w["if_version"] = int(v)
        elif (coll == "prices" and doc_id in prices) or (coll == "fx" and doc_id in fxdocs) or (coll == "bench" and doc_id in bench):
            notes.append(f"Falta la versión de {coll}/{doc_id}: no se escribe.")
            return
        writes.append((w, p.stat().st_size))

    # ---------- precios de los activos ----------
    hist_cache = {}
    for aid in sorted(first):
        asset = assets[aid]
        typ, ccy = asset.get("type", "otro"), asset.get("priceCurrency", "ARS")
        us_sym = None
        if typ == "accion_usa" or aid.endswith(".E"):
            us_sym = asset.get("underlying") or aid[:-2] if aid.endswith(".E") else (asset.get("underlying") or aid)
        cands = []
        if us_sym:
            series_src = fetch(f"us/{us_sym}.json", None)
            src_label, sym = "Yahoo Finance", us_sym
            lv = None
        else:
            if ccy == "USD_MEP" and not aid.endswith("D"):
                cands = [aid + "D"]
            elif ccy == "USD_CABLE" and not aid.endswith("C"):
                cands = [aid + "C"]
            cands += [aid]
            sym = next((c for c in cands if c in live), cands[0])
            series_src = hist_cache.get(sym)
            if series_src is None:
                series_src = fetch(f"hist/{sym}.json", None)
                hist_cache[sym] = series_src
            lv = live.get(sym)
            src_label = SRC
        series_src = dict(series_src or {})
        if lv and lv[1][0] and datetime.date.fromisoformat(TODAY).weekday() < 5:
            series_src.setdefault(TODAY, lv[1][0])
        if not series_src:
            summary["error"] += 1
            results.append({"id": aid, "st": "error", "note": f"Sin datos en el repositorio para {sym}" + (" (agregalo a tickers.json)" if not us_sym and sym not in live else " (sin historial: agregalo a tickers.json)" if not us_sym else "")})
            continue
        cur_doc = prices.get(aid) or {}
        cur = dict(cur_doc.get("series") or {})
        # control de escala contra lo que ya había (bonos por 100 vs por 1, por ejemplo)
        overlap = [d for d in series_src if d in cur and isinstance(cur[d], dict) and cur[d].get("p") and cur[d].get("src") != "manual"]
        scale = 1.0
        if len(overlap) >= 3:
            r = statistics.median(series_src[d] / float(cur[d]["p"]) for d in overlap)
            if 0.8 <= r <= 1.25:
                pass
            elif 80 <= r <= 125:
                scale = 0.01; notes.append(f"{aid}: la fuente cotiza por 1 y la app por 100 (o al revés); se ajustó ×0,01.")
            elif 0.008 <= r <= 0.0125:
                scale = 100.0; notes.append(f"{aid}: se ajustó la escala ×100 para coincidir con la app.")
            else:
                notes.append(f"{aid}: los precios de {sym} no coinciden con los guardados (relación {r:.3g}); no se escribió historial, revisar el ticker o el ratio.")
                summary["discrepancy"] += 1
                results.append({"id": aid, "st": "discrepancy", "note": f"Escala distinta a la guardada ({r:.3g})"})
                continue
        start = min(first[aid], add_days(TODAY, -400))
        added = replaced = 0
        for d in sorted(series_src):
            if d < start or d > TODAY:
                continue
            p = round(float(series_src[d]) * scale, 6)
            old = cur.get(d)
            if old and old.get("src") == "manual":
                continue
            if old and old.get("src", "").startswith(("data912", "Yahoo")) and abs(float(old.get("p", 0)) - p) < 1e-9:
                continue
            if old:
                replaced += 1
            else:
                added += 1
            cur[d] = {"p": p, "src": src_label}
        ds = sorted(d for d in cur if isinstance(cur[d], dict) and cur[d].get("p"))
        lastd = ds[-1] if ds else None
        st = "ok"
        note = ""
        if not lastd or days(lastd, TODAY) > 5:
            st = "single"; note = f"Último cierre del {lastd}: sin operaciones recientes en {sym}"
        if len(ds) >= 2:
            p1, p0 = float(cur[ds[-1]]["p"]), float(cur[ds[-2]]["p"])
            if p0 and abs(p1 / p0 - 1) > 0.25:
                st = "discrepancy"; note = f"Salto de {100 * (p1 / p0 - 1):+.1f}% entre {ds[-2]} y {ds[-1]}: revisar cambio de ratio o dato"
        if lastd:
            e = dict(cur[lastd]); e["st"] = st; e["chk"] = f"{src_label} · {sym} · cierre {lastd}"; cur[lastd] = e
        summary[st] += 1
        results.append({"id": aid, "st": st, "last": {"date": lastd, "p": cur[lastd]["p"] if lastd else None}, "chk": f"{src_label} · {sym}", "note": note, "added": added, "replaced": replaced})
        if added or replaced or not cur_doc:
            meta = dict(cur_doc.get("meta") or {})
            meta.update({"lastRun": NOW, "lastStatus": st, "chk": f"{src_label} · {sym}", "src": src_label, "sym": sym})
            put("prices", aid, {"series": cur, "meta": meta})

    # ---------- benchmarks ----------
    bench_res = {}
    for b in ("SPY", "ACWI", "AGG", "GLD"):
        s = fetch(f"us/{b}.json", None)
        if not s:
            bench_res[b] = "sin datos en el repositorio"; continue
        cur = dict((bench.get(b) or {}).get("series") or {})
        n = 0
        for d, p in s.items():
            if d < add_days(first_all, -400) or d > TODAY:
                continue
            if d not in cur or abs(float(cur[d].get("p", 0)) - float(p)) > 1e-9:
                cur[d] = {"p": float(p), "src": "Yahoo Finance"}; n += 1
        bench_res[b] = "ok"
        if n:
            put("bench", b, {"series": cur})

    # ---------- dólar ----------
    mep, ccl = fxsrc.get("mep") or {}, fxsrc.get("ccl") or {}
    years = {}
    for d in sorted(set(mep) | set(ccl)):
        if d < add_days(first_all, -400) or d > TODAY:
            continue
        years.setdefault(d[:4], []).append(d)
    fx_added = 0
    for y, dl in years.items():
        doc = f"y{y}"
        cur = dict((fxdocs.get(doc) or {}).get("series") or {})
        n = 0
        for d in dl:
            old = cur.get(d) or {}
            if old.get("src") == "manual":
                continue
            rec = dict(old)
            if d in mep and (not old.get("mep") or old.get("src") != "argentinadatos"):
                rec["mep"] = float(mep[d])
            if d in ccl and (not old.get("ccl") or old.get("src") != "argentinadatos"):
                rec["ccl"] = float(ccl[d])
            if rec != old:
                rec["src"] = "argentinadatos"; cur[d] = rec; n += 1
        if n:
            fx_added += n
            put("fx", doc, {"series": cur})

    # ---------- lotes para ArtifactData batch ----------
    batches, curb, size = [], [], 0
    for w, sz in writes:
        if curb and (len(curb) >= BATCH_N or size + sz > BATCH_BYTES):
            batches.append(curb); curb, size = [], 0
        curb.append(w); size += sz
    if curb:
        batches.append(curb)
    for i, b in enumerate(batches, 1):
        (out / f"writes_{i}.json").write_text(json.dumps(b, ensure_ascii=False, indent=0))

    errs = status.get("errors") or []
    patch = {
        "status": "done", "finishedAt": None, "summary": summary, "results": results, "benchResults": bench_res,
        "fxNote": f"Dólar: {fx_added} fechas nuevas o completadas (argentinadatos).",
        "source": {"repo": "jmn1235/cartera-datos", "dataAt": last.get("updatedAt"), "errors": errs[:10]},
        "notes": " ".join(
            ([f"Precios de data912 del {last.get('updatedAt', '')[:16].replace('T', ' ')} UTC."] if last.get("updatedAt") else [])
            + ([f"{summary['error']} activos sin datos."] if summary["error"] else [])
            + notes[:6]
        )[:900],
    }
    (out / "job_patch.json").write_text(json.dumps(patch, ensure_ascii=False))
    txt = [f"Lotes: {len(batches)} ({sum(len(b) for b in batches)} documentos)", f"Resumen: {summary}", f"Benchmarks: {bench_res}", patch["fxNote"]]
    txt += [f"- {r['id']}: {r['st']} {r.get('note', '')}" for r in results if r["st"] != "ok"]
    txt += notes
    (out / "resumen.txt").write_text("\n".join(txt))
    print("\n".join(txt))
    print("ARCHIVOS:", " ".join(str(out / f"writes_{i}.json") for i in range(1, len(batches) + 1)))


if __name__ == "__main__":
    main()
