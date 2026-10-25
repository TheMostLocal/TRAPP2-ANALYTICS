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


def due(path, today, finnhub=False):
    doc = read(path)
    if not doc or FULL:
        return True
    if finnhub and doc.get("history") and len(doc["history"]) >= 6:
        # the calendar keeps this season current; Yahoo only tops up depth once a week
        try:
            return (datetime.now(timezone.utc) - datetime.fromisoformat(str(doc.get("yahooAt") or doc["updatedAt"]).replace("Z", "+00:00"))).days >= 7
        except Exception:
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


# ---------------------------------------------------------------- Finnhub calendar (z158)
PROXY = (os.environ.get("KEY_PROXY") or "").strip()


def finnhub_calendar(start, end):
    """Every company's earnings between two dates - date, before-open / after-close, EPS and REVENUE
    estimate and actual - one request per week, through the key proxy (the Finnhub key lives on Vercel).
    Returns {ticker: [rows]} or None when Finnhub can't be reached (the per-ticker Yahoo path then carries
    the season, as before)."""
    import json as _json
    import urllib.parse
    import urllib.request
    if not PROXY:
        return None
    out, d0, ok = {}, start, 0
    while d0 <= end:
        d1 = min(end, d0 + timedelta(days=6))
        url = f"https://finnhub.io/api/v1/calendar/earnings?from={d0.isoformat()}&to={d1.isoformat()}"
        try:
            req = urllib.request.Request(PROXY + "?u=" + urllib.parse.quote(url, safe=""), headers={"User-Agent": "valuatio-earnings"})
            with urllib.request.urlopen(req, timeout=60) as r:
                rows = (_json.loads(r.read().decode()) or {}).get("earningsCalendar") or []
            ok += 1
            for x in rows:
                s = str(x.get("symbol") or "").upper().replace(".", "-")
                if s:
                    out.setdefault(s, []).append(x)
        except Exception as e:
            log(f"Finnhub {d0}…{d1}: {e}")
        d0 = d1 + timedelta(days=1)
        time.sleep(1.1)                                          # 60 calls / minute on the free plan
    return out if ok else None


def _hour(h):
    return {"bmo": "BMO", "amc": "AMC"}.get(str(h or "").lower(), "")


def apply_finnhub(doc, rows, today):
    """Merge Finnhub's rows into a ticker's record: reported quarters into history (EPS and revenue,
    surprises), the next unreported date into next. Same report = dates within 3 days."""
    hist = list(doc.get("history") or [])
    nxt = doc.get("next")
    for x in sorted(rows, key=lambda r: r.get("date") or ""):
        d = str(x.get("date") or "")[:10]
        if not d:
            continue
        ea, ee, ra, re_ = num(x.get("epsActual")), num(x.get("epsEstimate")), num(x.get("revenueActual")), num(x.get("revenueEstimate"))
        if ea is not None and d <= today:
            row = {"date": d, "time": _hour(x.get("hour")), "epsEstimate": ee, "epsActual": ea,
                   "surprisePct": round((ea - ee) / abs(ee) * 100, 2) if ee not in (None, 0) else None,
                   "revenueEstimate": re_, "revenueActual": ra,
                   "revenueSurprisePct": round((ra - re_) / abs(re_) * 100, 2) if (ra is not None and re_ not in (None, 0)) else None,
                   "fiscal": f"{x.get('year')}Q{x.get('quarter')}" if x.get("year") and x.get("quarter") else None, "source": "finnhub"}
            same = next((h for h in hist if h.get("date") and abs((date.fromisoformat(h["date"]) - date.fromisoformat(d)).days) <= 3), None)
            if same:
                for k, v in row.items():
                    if v is not None and (k != "surprisePct" or same.get("surprisePct") is None or same.get("source") == "finnhub"):
                        same[k] = v
            else:
                hist.append(row)
        elif d >= today and ea is None:
            if not nxt or not nxt.get("date") or d <= nxt["date"] or nxt.get("date") < today:
                nxt = {"date": d, "time": _hour(x.get("hour")), "epsEstimate": ee, "revenueEstimate": re_, "source": "finnhub"}
    hist.sort(key=lambda h: h.get("date") or "")
    if nxt and nxt.get("date") and nxt["date"] < today:
        nxt = None
    doc["history"] = hist[-12:]
    doc["next"] = nxt
    return doc


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
    # z158: Finnhub's calendar first - the whole season (100 days back, 60 ahead) for every company in ~23
    # requests, with revenue. Yahoo, one call per ticker, then only deepens history (weekly) - or carries
    # everything, as before, if Finnhub is unreachable.
    cal = finnhub_calendar(today - timedelta(days=100), today + timedelta(days=60))
    if cal is not None:
        n = 0
        tset = set(tickers)
        for t, rows in cal.items():
            if t not in tset:
                continue
            doc = read(TICK / f"{t}.json") or {"ticker": t, "history": [], "next": None}
            doc = apply_finnhub(doc, rows, today.isoformat())
            doc["finnhubAt"] = now_iso()
            doc.setdefault("updatedAt", now_iso())
            write(TICK / f"{t}.json", doc)
            n += 1
        log(f"Finnhub calendar: {sum(len(v) for v in cal.values())} rows · {n} of our companies updated")
    limit = min(LIMIT, int(os.environ.get("EARNINGS_YAHOO_WITH_FINNHUB", "150"))) if cal is not None else LIMIT
    todo = [t for t in tickers if due(TICK / f"{t}.json", today, finnhub=cal is not None)][:limit]
    log(f"{len(tickers)} members · {len(todo)} to deepen from Yahoo{' (full)' if FULL else ''}")
    ok = fail = 0
    for t in todo:
        try:
            doc = fetch_one(t)
            old = read(TICK / f"{t}.json") or {}
            if old.get("finnhubAt"):
                # keep Finnhub's revenue and its fresher next date; Yahoo adds depth
                fh = {h["date"]: h for h in old.get("history") or [] if h.get("source") == "finnhub"}
                for h in doc["history"]:
                    m = next((v for k, v in fh.items() if abs((date.fromisoformat(k) - date.fromisoformat(h["date"])).days) <= 3), None)
                    if m:
                        h.update({k: v for k, v in m.items() if v is not None and k != "surprisePct"})
                have = {h["date"] for h in doc["history"]}
                doc["history"] = sorted(doc["history"] + [v for k, v in fh.items() if not any(abs((date.fromisoformat(k) - date.fromisoformat(x)).days) <= 3 for x in have)],
                                        key=lambda h: h["date"])[-12:]
                if old.get("next") and old["next"].get("source") == "finnhub":
                    doc["next"] = old["next"]
                doc["finnhubAt"] = old["finnhubAt"]
            doc["yahooAt"] = now_iso()
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
