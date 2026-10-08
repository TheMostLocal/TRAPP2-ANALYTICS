#!/usr/bin/env python3
"""
scan_patterns.py - run every technical template / chart pattern over every ticker
in the four books, judge each identification by what happened next, and keep a
permanent record.

Reads  books/<BOOK>/data/master.json + books/<BOOK>/data/history/*.json
       (checked out by analytics.yml; override with PATTERNS_BOOKS=dir1,dir2,...)
Writes data/patterns/
  current.json       today's state per ticker: Minervini checklist (score + values)
                     and, per pattern template, the latest candidate with every
                     rule's pass/fail - for MANUAL review, whether or not it passes
  signals.json       identifications/confirmations in the last 10 bars, each with
                     its template's statistical track record
  stats.json         per template: backtest record (all history before the live
                     start) and live record (since), success rates + 95% CI,
                     average returns, edge vs the unconditional baseline
  records/<T>.json   every identification for one ticker (pivots, neckline, entry,
                     target, stop, outcome) + that ticker's per-template stats.
                     Only rewritten when the record changes.
  ledger_live.json   APPEND-ONLY live ledger: identifications made after the live
                     start are never re-derived or deleted; their status only moves
                     forward (identified -> confirmed -> success/failure/...), each
                     transition dated. This is the record that can't be retrofitted.

Backtest identifications are re-derived every run from the same rules (no
lookahead - see patterns_engine), so improving a rule re-scores history
honestly; the live ledger keeps what was actually called at the time.
"""
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import patterns_engine as pe  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "patterns"
REC = OUT / "records"
BOOKS = [p for p in (os.environ.get("PATTERNS_BOOKS") or "").split(",") if p] or \
        ["books/TRAPP2", "books/TRAPP2-1", "books/TRAPP2-2", "books/TRAPP2-3"]
MIN_BARS = 300
SIGNAL_WINDOW = 10          # bars: a detection this recent is an active signal
RECORD_CRITERIA_BARS = 504  # keep full rule detail on records from the last ~2 years
PATTERN_KEYS = pe.PATTERN_KEYS


def log(*a):
    print("[patterns]", *a, flush=True)


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def finite(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: finite(v) for k, v in o.items()}
    if isinstance(o, list):
        return [finite(v) for v in o]
    return o


def write_json(p, obj, compact=True):
    p.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(finite(obj), separators=(",", ":") if compact else None,
                     indent=None if compact else 1, allow_nan=False)
    p.write_text(txt)


def load_universe():
    uni = {}
    for b in BOOKS:
        rows = read_json(Path(b) / "data" / "master.json", []) or []
        book = Path(b).name
        for r in rows:
            t = (r.get("ticker") or "").upper()
            if t and t not in uni:
                uni[t] = {"book": book, "dir": b, "assetClass": r.get("asset_class"), "name": r.get("name")}
    return uni


def load_series(info, t):
    """-> (dates, closes, volumes|None). Volumes are None when the series has
    no real volume (FX, most indices) - volume rules then simply can't pass."""
    data = read_json(Path(info["dir"]) / "data" / "history" / f"{t}.json")
    if not isinstance(data, list):
        return None, None, None
    d, c, vol = [], [], []
    for bar in data:
        v = bar.get("close")
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0 and math.isfinite(v) and bar.get("date"):
            d.append(str(bar["date"])[:10])
            c.append(v)
            try:
                x = float(bar.get("volume") or 0)
            except (TypeError, ValueError):
                x = 0.0
            vol.append(x if math.isfinite(x) and x > 0 else 0.0)
    if len(c) < MIN_BARS:
        return None, None, None
    has_vol = sum(1 for x in vol[-260:] if x > 0) >= 200
    return d, c, (vol if has_vol else None)


def rs_percentiles(series, equities, calendar):
    """Cross-sectional RS percentile (0-99) on every 5th calendar date, forward-
    filled per ticker. Equities only (that's what RS ranks)."""
    sample = calendar[::5]
    pos = {}
    for t in equities:
        d, _ = series[t]
        pos[t] = {x: i for i, x in enumerate(d)}
    by_date = {}
    for dt in sample:
        vals = []
        for t in equities:
            i = pos[t].get(dt)
            if i is None:
                continue
            r = pe.rs_raw(series[t][1], i)
            if r is not None:
                vals.append((r, t))
        if len(vals) >= 20:
            vals.sort()
            m = len(vals) - 1
            by_date[dt] = {t: round(k / m * 99, 1) for k, (_, t) in enumerate(vals)}
    out = {}
    for t in equities:
        d, _ = series[t]
        arr, last = [None] * len(d), None
        for i, dt in enumerate(d):
            row = by_date.get(dt)
            if row is not None and t in row:
                last = row[t]
            arr[i] = last
        out[t] = arr
    return out


def baseline_fwd20(series, tickers):
    """Unconditional mean 20-bar forward return (every 5th bar) - the bar a
    pattern's post-signal return has to beat to have an edge."""
    s = n = 0
    for t in tickers:
        c = series[t][1]
        for i in range(0, len(c) - pe.FWD_BARS, 5):
            s += c[i + pe.FWD_BARS] / c[i] - 1
            n += 1
    return round(s / n * 100, 3) if n else None


def _r(x, nd=4):
    return round(x, nd) if isinstance(x, (int, float)) and math.isfinite(x) else None


def dated(det, d):
    """One identification -> compact dated record. Every recorded pattern passed
    ALL of its rules (that's what made it an identification), so the per-rule
    detail isn't repeated here - `current.json` carries it for the live candidate.
    Keys: tp template · id identDate · st status · cd confirmDate · xd exitDate ·
    en entry · tg target · sp stop · rt returnPct (direction-adjusted, at exit) ·
    f20 20-bar forward % after the signal · mfe/mae best/worst excursion % ·
    pv pivots [[date, price, H|L]] · nk neckline [[date, price], [date, price]] ·
    sc/of Minervini score · ex Minervini 63-day excess vs SPY %."""
    di = lambda i: d[i] if i is not None and 0 <= i < len(d) else None
    out = {"tp": det["template"], "id": di(det.get("identIdx")), "st": det.get("status")}
    for src, dst in (("confirmIdx", "cd"), ("exitIdx", "xd")):
        if det.get(src) is not None:
            out[dst] = di(det[src])
    for src, dst, nd in (("entry", "en", 6), ("target", "tg", 6), ("stop", "sp", 6), ("returnPct", "rt", 2),
                         ("fwd20Pct", "f20", 2), ("mfePct", "mfe", 2), ("maePct", "mae", 2),
                         ("excessPct", "ex", 2), ("score", "sc", 0), ("of", "of", 0)):
        v = det.get(src)
        if v is not None:
            out[dst] = _r(v, nd) if nd else v
    if det.get("pivots"):
        out["pv"] = [[di(p["i"]), _r(p["price"], 6), p["type"]] for p in det["pivots"]]
    if det.get("neckline"):
        out["nk"] = [[di(int(x)), _r(y, 6)] for x, y in det["neckline"]]
    return out


def _bits(crit):
    return "".join("1" if x["pass"] else "0" for x in crit)


def main():
    started = datetime.now(timezone.utc)
    uni = load_universe()
    log(f"universe {len(uni)} tickers across {len(BOOKS)} books")
    series, volumes = {}, {}
    for t, info in uni.items():
        d, c, vol = load_series(info, t)
        if c:
            series[t] = (d, c)
            volumes[t] = vol
    if "SPY" not in series:
        log("::error::no SPY history - cannot judge templates vs the market")
        return 1
    log(f"{len(series)} tickers with >= {MIN_BARS} bars")
    spy_d, spy_c = series["SPY"]
    spy_at = dict(zip(spy_d, spy_c))
    as_of = spy_d[-1]
    equities = [t for t in series if str(uni[t].get("assetClass") or "").lower() == "equity"]
    rs = rs_percentiles(series, equities, spy_d)
    base = baseline_fwd20(series, list(series))
    log(f"RS ranked {len(equities)} equities · baseline 20-bar forward return {base}%")

    ledger = read_json(OUT / "ledger_live.json", None) or {}
    meta = ledger.get("meta") or {}
    live_start = meta.get("liveStart") or as_of        # first run starts the live record
    entries = ledger.get("entries") or {}

    current, signals, all_dets = {}, [], {k: {"backtest": [], "live": []} for k in pe.TEMPLATES}
    for t, (d, c) in series.items():
        info = uni[t]
        piv = pe.zigzag(c)
        vol = volumes.get(t)
        ctx = pe._ctx(c, vol)
        dets = pe.detect_patterns(c, piv, v=vol, ctx=ctx)
        rs_t = rs.get(t)
        ev = pe.minervini_series(c, rs_t)
        spy_idx = lambda k, d=d: spy_at.get(d[k]) if 0 <= k < len(d) else None
        dets += pe.minervini_signals(c, ev, spy_idx)
        recs = [dated(x, d) for x in dets]
        for x, r in zip(dets, recs):
            bucket = "live" if (r["id"] or "") >= live_start else "backtest"
            all_dets[r["tp"]][bucket].append(x)          # stats run on the full-precision dict
            if bucket == "live":
                key = f"{t}|{r['tp']}|{r['id']}"
                old = entries.get(key)
                if old is None:
                    entries[key] = dict(r, ticker=t, firstSeen=as_of, history=[[as_of, r["st"]]])
                elif old.get("st") != r["st"] and old.get("st") in ("open", "confirmed"):
                    # status only moves forward; resolved entries are frozen
                    keep = {k: old[k] for k in ("firstSeen", "history") if k in old}
                    entries[key] = dict(r, ticker=t, **keep)
                    entries[key]["history"] = (old.get("history") or []) + [[as_of, r["st"]]]
            n_since = len(d) - 1 - x["identIdx"]
            conf_since = (len(d) - 1 - x["confirmIdx"]) if x.get("confirmIdx") is not None else 10 ** 9
            if n_since <= SIGNAL_WINDOW or conf_since <= SIGNAL_WINDOW:
                signals.append(dict(ticker=t, template=r["tp"], direction=x["direction"],
                                    identDate=r["id"], confirmDate=r.get("cd"), status=r["st"],
                                    entry=r.get("en"), target=r.get("tg"), stop=r.get("sp")))
        # --- today's manual-review state ---
        # Compact: rule NAMES live once in current.json["ruleNames"]; per ticker
        # only pass-bits ("1101...") and the measured values travel.
        cur = {"b": info["book"], "ac": info.get("assetClass"), "px": _r(c[-1], 6), "dt": d[-1]}
        if ev[-1]:
            e = ev[-1]
            cur["mv"] = {"s": e["score"], "of": e["of"], "p": _bits(e["criteria"]), "v": e["values"]}
        cands = {}
        for k in PATTERN_KEYS:
            cand = pe.latest_candidate(c, piv, k, v=vol, ctx=ctx)
            if cand:
                ii = cand.get("identIdx")
                cands[k] = {"pv": [[d[p["i"]], _r(p["price"], 6), p["type"]] for p in cand["pivots"]],
                            "nk": [[d[int(x)], _r(y, 6)] for x, y in cand["neckline"]],
                            "p": _bits(cand["criteria"]), "val": [x["value"] for x in cand["criteria"]],
                            "ok": cand["valid"], "id": d[ii] if ii is not None and ii < len(d) else None}
        cur["cand"] = cands
        current[t] = cur
        # --- per-ticker record (only rewritten when it changes) ---
        per = {k: pe.summarize([x for x in dets if x["template"] == k]) for k in pe.TEMPLATES
               if any(x["template"] == k for x in dets)}
        doc = {"ticker": t, "book": info["book"], "engine": pe.ENGINE_VERSION, "liveStart": live_start,
               "detections": recs, "stats": per}
        p = REC / f"{t.replace('/', '_')}.json"
        old = read_json(p)
        if old != json.loads(json.dumps(finite(doc))):
            write_json(p, doc)

    stats = {}
    for k, b in all_dets.items():
        dirn = -1 if pe.TEMPLATES[k]["direction"] == "bearish" else 1
        bl = (dirn * base) if base is not None else None
        stats[k] = {**pe.TEMPLATES[k],
                    "backtest": pe.summarize(b["backtest"], bl) if b["backtest"] else {"n": 0},
                    "live": pe.summarize(b["live"], bl) if b["live"] else {"n": 0}}
    # attach each template's record to its active signals
    for s in signals:
        bt = stats[s["template"]]["backtest"]
        s["track"] = {"successRate": bt.get("successRate"), "ci95": bt.get("successCI95"), "n": bt.get("judged"),
                      "edgeVsBaselinePct": bt.get("edgeVsBaselinePct")}
    signals.sort(key=lambda s: (s.get("confirmDate") or s["identDate"] or ""), reverse=True)

    meta.update({"liveStart": live_start, "engine": pe.ENGINE_VERSION, "updated": as_of})
    write_json(OUT / "ledger_live.json", {"meta": meta, "entries": entries}, compact=False)
    write_json(OUT / "current.json", {"asOf": as_of, "engine": pe.ENGINE_VERSION, "templates": pe.TEMPLATES,
                                      "rules": {"confirmBars": pe.CONFIRM_BARS, "outcomeBars": pe.OUTCOME_BARS,
                                                "minerviniFwd": pe.MINERVINI_FWD},
                                      "ruleNames": pe.rule_names(), "tickers": current})
    write_json(OUT / "stats.json", {"asOf": as_of, "engine": pe.ENGINE_VERSION, "liveStart": live_start,
                                    "baselineFwd20Pct": base, "templates": stats}, compact=False)
    write_json(OUT / "signals.json", {"asOf": as_of, "window": SIGNAL_WINDOW, "signals": signals}, compact=False)
    secs = (datetime.now(timezone.utc) - started).total_seconds()
    n_bt = sum(len(b["backtest"]) for b in all_dets.values())
    n_lv = sum(len(b["live"]) for b in all_dets.values())
    log(f"done in {secs:.0f}s · {n_bt} backtest + {n_lv} live identifications · {len(signals)} active signals · "
        f"live ledger {len(entries)} entries since {live_start}")
    for k, st in stats.items():
        b = st["backtest"]
        if b.get("n"):
            log(f"  {k:14s} n={b['n']:5d} judged={b['judged']:5d} success={b.get('successRate')}% "
                f"CI{b.get('successCI95')} avgRet={b.get('avgReturnPct')}% edge={b.get('edgeVsBaselinePct')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
