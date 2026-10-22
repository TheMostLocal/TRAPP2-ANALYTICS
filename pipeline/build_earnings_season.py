#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/build_earnings_season.py
#
#  z136 - the reporting season, bottom-up from each member's own report (the table on
#  Data & Research -> Earnings), for every index in universe.json:
#    per sector, per group (Cyclicals / Near-Cyclicals / Defensives) and in total -
#    # reported / members, % reported, % beating (reported > estimate), % missing (< estimate;
#    equal = in line), aggregate surprise = sum((actual - estimate) x shares) / sum(|estimate| x shares)
#    (shares = index weight / price, or market cap / price), and the return since the quarter
#    end (sector SPDR minus SPY; the index row absolute).
#  Blended quarterly EPS for the index = index level x sum(weight x EPS / price), EPS = reported
#  when out, else the estimate - recorded daily in history.json (tracking starts the day this runs).
#    -> data/earnings/season.json, data/earnings/history.json
# ============================================================
import sys
from datetime import date

from earnings_common import GROUPS, OUT, SECTOR_ETF, TICK, now_iso, num, read, season_quarter, write

INDEX_LEVEL = {"SPY": "^GSPC", "QQQ": "^NDX", "DIA": "^DJI"}


def closes(symbols, start):
    """Daily closes since `start` (yfinance). {symbol: [(date, close), ...]}"""
    out = {}
    try:
        import yfinance as yf
        df = yf.download(symbols, start=start, interval="1d", auto_adjust=True, progress=False, group_by="ticker", threads=True)
        for s in symbols:
            try:
                f = df[s] if len(symbols) > 1 else df
                out[s] = [(str(i)[:10], float(c)) for i, c in f["Close"].dropna().items()]
            except Exception:
                pass
    except Exception as e:
        print("[season] price download failed:", e)
    return out


def ret_since(series, qend):
    if not series:
        return None
    base = [c for d, c in series if d <= qend]
    base = base[-1] if base else series[0][1]
    return series[-1][1] / base - 1 if base else None


def stats(rows):
    n = len(rows)
    rep = [r for r in rows if r["reported"]]
    beat = [r for r in rep if r["act"] > r["est"] + 1e-9]
    miss = [r for r in rep if r["act"] < r["est"] - 1e-9]
    num_ = sum((r["act"] - r["est"]) * r["shares"] for r in rep if r["shares"])
    den = sum(abs(r["est"]) * r["shares"] for r in rep if r["shares"])
    return {"members": n, "reported": len(rep), "pctReported": len(rep) / n if n else None,
            "pctBeat": len(beat) / len(rep) if rep else None, "pctMiss": len(miss) / len(rep) if rep else None,
            "surprise": num_ / den if den else None}


def book_prices():
    """z140: {TICKER: (price USD, market cap USD)} from the four books' master.json - Yahoo's fast_info
    returns nothing from GitHub's runners, which left every share count (and so the surprise and the
    blended EPS) empty."""
    import json
    import urllib.request
    out = {}
    for b in ("TRAPP2", "TRAPP2-1", "TRAPP2-2", "TRAPP2-3"):
        try:
            with urllib.request.urlopen(f"https://raw.githubusercontent.com/TheMostLocal/{b}/main/data/master.json", timeout=60) as r:
                rows = json.loads(r.read().decode())
            for x in rows if isinstance(rows, list) else (rows.get("rows") or []):
                t = str(x.get("ticker") or "").upper()
                p, c = num(x.get("price")), num(x.get("marketcap") or x.get("marketCap"))
                if t and p and p > 0 and t not in out and str(x.get("currency") or "USD").upper() == "USD":
                    out[t] = (p, c if c and c > 0 else None)
        except Exception as e:
            print(f"[season] {b} master unavailable ({e})")
    return out


def main():
    uni = read(OUT / "universe.json", {}) or {}
    q = season_quarter()
    bp = book_prices()
    print(f"[season] book prices for {len(bp)} tickers")
    today = date.today().isoformat()
    etfs = sorted(set(SECTOR_ETF.values()) | set(uni.get("indices", {}).keys()) | set(INDEX_LEVEL.values()))
    px = closes(etfs, q["end"][:8] + "01")
    hist = read(OUT / "history.json", {"series": {}}) or {"series": {}}
    season = {"quarter": q, "asOf": now_iso(), "indices": {}}
    for etf, ix in (uni.get("indices") or {}).items():
        rows, upcoming, results = [], [], []
        for m in ix.get("members") or []:
            doc = read(TICK / f"{m['ticker']}.json") or {}
            bpx = bp.get(m["ticker"]) or (None, None)
            price = num(doc.get("price")) or bpx[0]
            cap = num(doc.get("marketCap")) or bpx[1]
            w = num(m.get("weight"))
            shares = (w / price) if (w and price) else ((cap / price) if (cap and price) else None)
            rep = next((h for h in reversed(doc.get("history") or []) if q["from"] <= (h.get("date") or "") <= q["to"]
                        and h.get("epsActual") is not None and h.get("epsEstimate") is not None), None)
            nxt = doc.get("next") or {}
            est_next = num(nxt.get("epsEstimate")) if q["from"] <= (nxt.get("date") or "") <= q["to"] else None
            r = {"ticker": m["ticker"], "name": m.get("name"), "sector": m.get("sector"), "weight": w, "price": price, "shares": shares,
                 "reported": rep is not None, "est": num(rep["epsEstimate"]) if rep else est_next, "act": num(rep["epsActual"]) if rep else None,
                 "date": rep["date"] if rep else nxt.get("date"), "time": (rep or nxt).get("time") if (rep or nxt) else ""}
            rows.append(r)
            if rep:
                results.append({k: r[k] for k in ("ticker", "name", "sector", "date", "time", "est", "act")} | {"surprisePct": num(rep.get("surprisePct"))})
            elif nxt.get("date") and today <= nxt["date"] <= q["to"]:
                upcoming.append({"ticker": m["ticker"], "name": m.get("name"), "sector": m.get("sector"), "date": nxt["date"], "time": nxt.get("time"), "est": est_next})
        sectors = {}
        for _, names in GROUPS:
            for s in names:
                st = stats([r for r in rows if r["sector"] == s])
                st["returnSinceQEnd"] = ret_since(px.get(SECTOR_ETF[s]), q["end"])
                sectors[s] = st
        groups = {g: stats([r for r in rows if r["sector"] in names]) for g, names in GROUPS}
        total = stats(rows)
        idx_ret = ret_since(px.get(etf), q["end"])
        total["returnSinceQEnd"] = idx_ret
        for s, st in sectors.items():
            st["relReturn"] = (st["returnSinceQEnd"] - idx_ret) if (st["returnSinceQEnd"] is not None and idx_ret is not None) else None
        for g, names in GROUPS:     # group relative return: cap-weighted mean of its sectors' relative returns
            ws = [(sum((r["weight"] or 0) for r in rows if r["sector"] == s), sectors[s]["relReturn"]) for s in names]
            ws = [(a, b) for a, b in ws if b is not None and a > 0]
            groups[g]["relReturn"] = sum(a * b for a, b in ws) / sum(a for a, _ in ws) if ws else None
        # blended quarterly EPS for the index (reported where out, else the estimate)
        lvl_s = px.get(INDEX_LEVEL.get(etf, ""))
        level = lvl_s[-1][1] if lvl_s else None
        terms = [((r["weight"] or 0), (r["act"] if r["act"] is not None else r["est"]), r["price"]) for r in rows]
        terms = [(a, e, p) for a, e, p in terms if a and e is not None and p]
        cover = sum(a for a, _, _ in terms)
        blended = level * sum(a * e / p for a, e, p in terms) / cover if (level and cover > 0.5) else None
        if blended is not None:
            ser = hist["series"].setdefault(f"{etf}:{q['label']}", [])
            if ser and ser[-1]["date"] == today:
                ser[-1] = {"date": today, "eps": round(blended, 4)}
            else:
                ser.append({"date": today, "eps": round(blended, 4)})
        results.sort(key=lambda x: x["date"] or "", reverse=True)
        upcoming.sort(key=lambda x: (x["date"], x["time"] or ""))
        season["indices"][etf] = {"name": ix.get("name"), "source": ix.get("source"), "total": total, "groups": groups, "sectors": sectors,
                                  "blendedEps": blended, "weightCoverage": cover, "results": results[:600], "upcoming": upcoming[:200]}
        print(f"[season] {etf} {q['label']}: {total['reported']}/{total['members']} reported · beat {total['pctBeat']} · surprise {total['surprise']} · blended EPS {blended}")
    write(OUT / "season.json", season)
    write(OUT / "history.json", hist)
    write_calendar()
    return 0


def write_calendar():
    """z138 - one small file the bot reads for catalysts: every ticker's next report and its record
    (beat rate and average surprise over its last 8 reports). -> data/earnings/calendar.json"""
    out = {}
    for p in sorted(TICK.glob("*.json")):
        doc = read(p) or {}
        hist = [h for h in (doc.get("history") or []) if h.get("epsActual") is not None and h.get("epsEstimate") is not None][-8:]
        nx = doc.get("next") or {}
        beats = [h for h in hist if h["epsActual"] > h["epsEstimate"] + 1e-9]
        sur = [num(h.get("surprisePct")) for h in hist if num(h.get("surprisePct")) is not None]
        out[p.stem] = {"next": {"date": nx.get("date"), "time": nx.get("time"), "est": num(nx.get("epsEstimate"))} if nx.get("date") else None,
                       "beatRate": round(len(beats) / len(hist), 3) if hist else None,
                       "avgSurprise": round(sum(sur) / len(sur), 2) if sur else None, "n": len(hist),
                       "last": hist[-1]["date"] if hist else None}
    write(OUT / "calendar.json", {"asOf": now_iso(), "tickers": out})
    print(f"[season] calendar: {len(out)} tickers")


if __name__ == "__main__":
    sys.exit(main())
