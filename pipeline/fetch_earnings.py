#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/fetch_earnings.py
#
#  z136 - every index member's earnings: past reports (date, EPS estimate, reported EPS,
#  surprise) and the next report date + estimate, from Yahoo (yfinance earnings dates), plus
#  price and market cap for weighting.
#    -> data/earnings/tickers/<T>.json { ticker, updatedAt, price, marketCap,
#         next: { date, time, epsEstimate }, history: [ { date, time, epsEstimate, epsActual, surprisePct } ] }
#  Incremental: a ticker is re-read when its file is older than 7 days, or a report is due or
#  just happened (within -1..+3 days), or it has no file. EARNINGS_FULL=1 re-reads all.
#  "time": "BMO" (before the open, before 0930 ET) / "AMC" (after the close, from 1600 ET) / "".
# ============================================================
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

from earnings_common import OUT, TICK, num, now_iso, read, write

FULL = os.environ.get("EARNINGS_FULL", "").strip() not in ("", "0", "false")
LIMIT = int(os.environ.get("EARNINGS_MAX", "700"))
PAUSE = float(os.environ.get("EARNINGS_PAUSE", "0.35"))


def log(*a):
    print("[earnings]", *a, flush=True)


def et(ts):
    from zoneinfo import ZoneInfo
    return ts.astimezone(ZoneInfo("America/New_York"))


def due(path, today):
    doc = read(path)
    if not doc or FULL:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(doc["updatedAt"].replace("Z", "+00:00"))).days
    except Exception:
        return True
    if age >= 7:
        return True
    dates = [h.get("date") for h in doc.get("history") or []] + [((doc.get("next") or {}).get("date"))]
    for d in dates:
        if not d:
            continue
        try:
            delta = (date.fromisoformat(d) - today).days
        except ValueError:
            continue
        if -1 <= delta <= 3:
            return True
    return False


def fetch_one(t):
    import yfinance as yf
    tk = yf.Ticker(t)
    df = tk.get_earnings_dates(limit=12)
    hist, nxt = [], None
    if df is not None and len(df):
        for ts, r in df.iterrows():
            e = et(ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts)
            m = e.hour * 60 + e.minute
            row = {"date": e.date().isoformat(), "time": "BMO" if m < 570 else ("AMC" if m >= 960 else ""),
                   "epsEstimate": num(r.get("EPS Estimate")), "epsActual": num(r.get("Reported EPS")),
                   "surprisePct": num(r.get("Surprise(%)"))}
            hist.append(row)
    hist.sort(key=lambda x: x["date"])
    today = date.today().isoformat()
    future = [h for h in hist if h["date"] >= today and h["epsActual"] is None]
    nxt = future[0] if future else None
    past = [h for h in hist if h["epsActual"] is not None]
    price = cap = None
    try:
        fi = tk.fast_info
        price, cap = num(fi.get("last_price")), num(fi.get("market_cap"))
    except Exception:
        pass
    return {"ticker": t, "updatedAt": now_iso(), "price": price, "marketCap": cap,
            "next": ({"date": nxt["date"], "time": nxt["time"], "epsEstimate": nxt["epsEstimate"]} if nxt else None),
            "history": past[-12:]}


def book_equities():
    """US-listed equities held in the four books - so every stock you value has its own earnings,
    not only index members. Read from GitHub (raw); a book that fails is skipped."""
    import json
    import re
    import urllib.request
    out = set()
    for b in ("TRAPP2", "TRAPP2-1", "TRAPP2-2", "TRAPP2-3"):
        try:
            with urllib.request.urlopen(f"https://raw.githubusercontent.com/TheMostLocal/{b}/main/data/master.json", timeout=60) as r:
                rows = json.loads(r.read().decode())
            rows = rows if isinstance(rows, list) else (rows.get("rows") or [])
            for x in rows:
                t = str(x.get("ticker") or "").upper().strip()
                ac = str(x.get("asset_class") or "Equity")
                if t and ac in ("Equity", "") and not re.search(r"\.[A-Z]{1,3}$|^\^|=X$|=F$|-USD$", t):
                    out.add(t)
        except Exception as e:
            log(f"{b}: book read failed ({e})")
    return out


def main():
    uni = read(OUT / "universe.json", {}) or {}
    tickers = sorted({m["ticker"] for ix in (uni.get("indices") or {}).values() for m in ix.get("members") or [] if m.get("ticker")} | book_equities())
    if not tickers:
        log("no universe yet (run fetch_index_members.py)")
        return 0
    today = date.today()
    todo = [t for t in tickers if due(TICK / f"{t}.json", today)][:LIMIT]
    log(f"{len(tickers)} members · {len(todo)} to refresh{' (full)' if FULL else ''}")
    ok = fail = 0
    for t in todo:
        try:
            doc = fetch_one(t)
            if doc["history"] or doc["next"]:
                write(TICK / f"{t}.json", doc)
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            log(f"{t}: {e}")
        time.sleep(PAUSE)
    log(f"refreshed {ok} · empty / failed {fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
