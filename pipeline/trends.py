# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/trends.py
#  z201 - TODAY'S TRENDS: the market's notable runs, breaks and extremes, measured from the data and stored every
#  session. Owner 11-20 1304 ET: "If the S&P closes red today this be the 6th day in a row of losses ... I need the
#  system to understand trends like this and point it out, this can be used for the bot as an indicator ... keep these
#  indicators stored somewhere each and every day. Use close and open prices in appropriate times ... consider there is
#  likely a sell off going on ... understand the weights of each stock in the S&P 500 to understand what is making the
#  percentage fall to what it is and how."
#
#  WHAT IT MEASURES (close-to-close unless it says otherwise - a "red day" is a close below the prior close, whatever
#  the open did; the open is used where it belongs: the gap at the open and the intraday move from it):
#    * streaks for the indices (S&P 500, Nasdaq, Dow, Russell 2000, NYSE), the 11 sector SPDRs, the VIX and cross-asset
#      markers (10-year yield, dollar, oil, gold, copper, bitcoin, high-yield and long Treasuries): length, from when,
#      how often a run that long happened in the data and when last, the record - and, while the market is open, what
#      a red or a green close today would make it
#    * the day: change, gap at the open, move from the open, the move in units of its 20-day volatility, the largest
#      move since when; distance from the 52-week and all-time highs; the 50- and 200-day averages and their crosses;
#      weekly runs; distribution days (an S&P decline of 0.2 %+ on higher volume, 25 sessions)
#    * the S&P 500 from the inside: every priced member's move times its index weight (State Street's SPY holdings,
#      drifted with each name's price since the holdings date) -> what moved the index and by how much, by name and by
#      sector, cap-weighted vs equal-weighted, how many rose and fell - for the day and summed over the current streak
#    * the sell-off read: seven criteria (streak, drawdown, below the 50-day, the VIX, distribution days, breadth,
#      credit) -> calm / under pressure / sell-off / heavy sell-off, with correction (-10 %) and bear-market (-20 %)
#      overlays. The bot reads it (TRAPP2-BOT: trendsAtEntry on every trade, new longs x0.9 in a sell-off)
#    * what happened next: after the earlier runs of the same length, the S&P 500's move 1, 5 and 20 sessions on vs
#      every day's - a base rate from the record, not a forecast
#
#  WHEN: every 30 minutes 0945-1645 ET + 1845 ET on trading days, queued on time by the health loop (pipeline/ontime.py;
#  .github/workflows/trends.yml's cron is the backstop). In the session the day is PROVISIONAL (the latest tape price);
#  after 1600 the official close from the tape makes it FINAL.
#  INPUTS (public, no keys): TRAPP2-1 data/history/<sym>.json (daily closes) and data/intraday/latest.json (the tape:
#  price, prior close, official open / close); the equity books' data/intraday/latest.json and <date>.json (member
#  prices); this repo's data/earnings/universe.json (S&P 500 weights) and data/web/nodes.json (sectors).
#  OUTPUTS: data/trends/latest.json (now), data/trends/daily/YYYY-MM-DD.json (one per FINAL session, members included -
#  kept; the last 12 sessions are back-filled from the books' day tapes when missing), data/trends/history.json (one
#  compact row per session), data/trends/weights_base.json (holdings-date prices). A file is rewritten only when
#  something other than its time stamp changed (no empty commits after the close).
#  Env: TRENDS_OFFLINE=1 (no fetches; tests) · TRENDS_NOW=ISO (tests)
# ============================================================
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BUILD = "z201"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "trends"
RAW = "https://raw.githubusercontent.com/TheMostLocal"
MACRO_BOOK = "TRAPP2-1"
BOOKS = ("TRAPP2", "TRAPP2-2", "TRAPP2-3", "TRAPP2-4")
UA = {"User-Agent": "Mozilla/5.0 (valuatio trends)"}

INDICES = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq Composite"), ("^DJI", "Dow Jones Industrial Average"),
           ("^RUT", "Russell 2000"), ("^NYA", "NYSE Composite")]
VOLS = [("^VIX", "VIX")]
SECTORS = [("XLK", "Technology"), ("XLC", "Communication Services"), ("XLY", "Consumer Discretionary"),
           ("XLF", "Financials"), ("XLV", "Health Care"), ("XLI", "Industrials"), ("XLP", "Consumer Staples"),
           ("XLE", "Energy"), ("XLU", "Utilities"), ("XLB", "Materials"), ("XLRE", "Real Estate")]
CROSS = [("^TNX", "10-year Treasury yield"), ("DX-Y.NYB", "US dollar index"), ("CL=F", "WTI crude oil"),
         ("GC=F", "Gold"), ("HG=F", "Copper"), ("BTC-USD", "Bitcoin"), ("HYG", "High-yield bonds (HYG)"),
         ("TLT", "Long Treasuries (TLT)")]
SPX = "^GSPC"
OPEN_MIN, CLOSE_MIN = 9 * 60 + 30, 16 * 60
SUSPECT = 0.40            # a member "moving" more than 40 % in a session is a data check, not a contribution
KEEP_HISTORY = 400        # rows in history.json
BACKFILL = 12             # recent sessions given a daily record when missing (tapes kept ~15 sessions)
FWD = (1, 5, 20)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now_utc():
    v = os.environ.get("TRENDS_NOW")
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else datetime.now(timezone.utc)


def _read(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _write(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    tmp.replace(p)


def fetch_json(url, tries=3, timeout=40):
    if os.environ.get("TRENDS_OFFLINE") == "1":
        return None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
        time.sleep(1.5 * (k + 1))
    return None


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def hist_url(sym, book=MACRO_BOOK):
    return f"{RAW}/{book}/main/data/history/{urllib.parse.quote(sym, safe='')}.json"


# ---------------------------------------------------------------------------------------------- series & today ---
def series(rows):
    """History rows -> (dates, closes, volumes), sorted, one row per date, valid closes only."""
    by = {}
    for r in rows or []:
        d, c = str((r or {}).get("date") or "")[:10], _num((r or {}).get("close"))
        if len(d) == 10 and c and c > 0:
            by[d] = (c, _num(r.get("volume")))
    ds = sorted(by)
    return ds, [by[d][0] for d in ds], [by[d][1] for d in ds]


def tape_point(sym, tape, last_date):
    """Today's point for sym from the tape -> {date, close, open, prevClose, final, asOf} or None.
    Final once the tape has the official close (a quote at or after 1600 ET); in the session, provisional (the
    latest price). Nothing before the open, on a weekend or holiday, or when the history already holds the day."""
    if not tape:
        return None
    day, sess = str(tape.get("date") or ""), tape.get("session")
    if len(day) != 10 or sess not in ("regular", "early-close") or (last_date and last_date >= day):
        return None
    close_min = int(tape.get("closeMinuteET") or CLOSE_MIN)
    et_min = int(tape.get("etMinute") or 0)
    px_close = _num((tape.get("close") or {}).get(sym))
    px = _num((tape.get("p") or {}).get(sym))
    if px_close:
        val, final = px_close, True
    elif px and et_min >= OPEN_MIN:
        val, final = px, et_min >= close_min + 30          # well after the bell the last price is the close
    else:
        return None
    return {"date": day, "close": val, "open": _num((tape.get("open") or {}).get(sym)),
            "prevClose": _num((tape.get("prevClose") or {}).get(sym)), "final": bool(final),
            "asOf": tape.get("etTime"), "marketOpen": bool(tape.get("marketOpen"))}


# ---------------------------------------------------------------------------------------------------- streaks ---
def runs(closes):
    """Runs of same-direction closes -> [(dir, first_day_index, last_day_index)]; dir +1 up / -1 down. A flat close
    ends a run and starts none."""
    out, cur = [], None
    for i in range(1, len(closes)):
        d = 1 if closes[i] > closes[i - 1] else -1 if closes[i] < closes[i - 1] else 0
        if cur and d == cur[0]:
            cur[2] = i
        else:
            if cur:
                out.append(tuple(cur))
            cur = [d, i, i] if d else None
    if cur:
        out.append(tuple(cur))
    return out


def current_run(dates, closes):
    """The run the last close belongs to -> {dir, n, from, through} or None (flat last day)."""
    rs = runs(closes)
    if not rs or rs[-1][2] != len(closes) - 1:
        return None
    d, a, b = rs[-1]
    return {"dir": "down" if d < 0 else "up", "n": b - a + 1, "from": dates[a], "through": dates[b]}


def run_context(dates, closes, direction, n, skip_last=True):
    """How rare a run of n+ is in this record: {atLeast (count), last {from, to, n}, record {from, to, n}, since}.
    skip_last: leave the run in progress out (it is the one being described)."""
    want = -1 if direction == "down" else 1
    rs = [r for r in runs(closes) if r[0] == want]
    if skip_last and rs and rs[-1][2] == len(closes) - 1:
        rs = rs[:-1]
    ge = [r for r in rs if r[2] - r[1] + 1 >= n]
    rec = max(rs, key=lambda r: (r[2] - r[1], r[2]), default=None)
    fmt = lambda r: {"from": dates[r[1]], "to": dates[r[2]], "n": r[2] - r[1] + 1} if r else None
    return {"n": n, "atLeast": len(ge), "last": fmt(ge[-1] if ge else None), "record": fmt(rec), "since": dates[0] if dates else None}


def forward_stats(dates, closes, direction, n, horizons=FWD):
    """After each earlier run that reached n (counted once, on the day it reached n): the move h sessions later, vs
    the move h sessions after any day. -> {n, events, h: {count, up, median, mean, base_up, base_median}}"""
    want = -1 if direction == "down" else 1
    events = [r[1] + n - 1 for r in runs(closes) if r[0] == want and r[2] - r[1] + 1 >= n]
    if events and events[-1] >= len(closes) - 1:
        events = events[:-1]                                  # the run in progress has no future yet
    out = {"n": n, "events": len(events), "h": {}}
    for h in horizons:
        f = [closes[e + h] / closes[e] - 1 for e in events if e + h < len(closes)]
        base = [closes[i + h] / closes[i] - 1 for i in range(len(closes) - h)]
        if not base:
            continue
        out["h"][str(h)] = {"count": len(f), "up": round(sum(x > 0 for x in f) / len(f), 3) if f else None,
                            "median": round(statistics.median(f) * 100, 2) if f else None,
                            "mean": round(statistics.fmean(f) * 100, 2) if f else None,
                            "baseUp": round(sum(x > 0 for x in base) / len(base), 3),
                            "baseMedian": round(statistics.median(base) * 100, 2)}
    return out


# ------------------------------------------------------------------------------------------------ one series ---
def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def roll_max(xs, n):
    """Trailing n-day max at each point (incl. that day)."""
    from collections import deque
    q, out = deque(), []
    for i, x in enumerate(xs):
        while q and xs[q[-1]] <= x:
            q.pop()
        q.append(i)
        if q[0] <= i - n:
            q.popleft()
        out.append(xs[q[0]])
    return out


def last_where(dates, flags, before):
    """The most recent date before index `before` where flags[i] is True."""
    for i in range(min(before, len(flags)) - 1, -1, -1):
        if flags[i]:
            return dates[i]
    return None


def card(sym, name, rows, tape=None, with_volume=False):
    """Everything this module says about one series, as of now. -> dict or None."""
    dates, closes, vols = series(rows)
    if len(closes) < 30:
        return None
    tp = tape_point(sym, tape, dates[-1])
    hist_dates, hist_closes = list(dates), list(closes)            # completed sessions only
    if tp and tp["final"]:
        hist_dates.append(tp["date"])
        hist_closes.append(tp["close"])
        vols = vols + [None]
    all_dates, all_closes = list(hist_dates), list(hist_closes)
    if tp and not tp["final"]:
        all_dates.append(tp["date"])
        all_closes.append(tp["close"])
    i = len(all_closes) - 1
    prev = all_closes[i - 1]
    last = all_closes[i]
    chg = last / prev - 1
    c = {"sym": sym, "name": name, "date": all_dates[i], "close": round(last, 4), "prevClose": round(prev, 4),
         "chg": round(chg * 100, 3), "dir": "red" if chg < 0 else "green" if chg > 0 else "flat",
         "final": not (tp and not tp["final"]), "asOf": (tp or {}).get("asOf") if tp else None}
    if tp and tp.get("prevClose") and abs(tp["prevClose"] / hist_closes[-1 if not tp["final"] else -2] - 1) > 0.005:
        c["dataCheck"] = f"the tape's prior close {tp['prevClose']} differs from the history's {round(hist_closes[-1 if not tp['final'] else -2], 2)}"
    if tp and tp.get("open"):
        c["open"] = round(tp["open"], 4)
        c["gap"] = round((tp["open"] / prev - 1) * 100, 3)               # the open against the prior close
        c["fromOpen"] = round((last / tp["open"] - 1) * 100, 3)          # the session's move from its open
    # streak through the last COMPLETED session, and what today would make it
    done = current_run(hist_dates, hist_closes)
    if done:
        done["context"] = run_context(hist_dates, hist_closes, done["dir"], done["n"])
        c["streak"] = done
    if tp and not tp["final"]:
        red = chg < 0
        c["provisional"] = {"now": "red" if red else "green" if chg > 0 else "flat"}
        for scen in ("red", "green"):
            d = "down" if scen == "red" else "up"
            n = (done["n"] + 1) if (done and done["dir"] == d) else 1
            ctx = run_context(hist_dates, hist_closes, d, n, skip_last=bool(done and done["dir"] == d))
            c["provisional"][scen] = {"dir": d, "n": n, "context": ctx}
    else:
        cur = current_run(all_dates, all_closes)
        if cur and not (done and done.get("n") == cur["n"] and done.get("through") == cur["through"]):
            cur["context"] = run_context(all_dates, all_closes, cur["dir"], cur["n"])
            c["streak"] = cur
    # what the NEXT close makes the run, from completed sessions only (= the provisional scenarios in the session; the
    # next session's after the close / before the open) - lets the app keep the headline live from the tape between runs
    c["next"] = {"after": hist_dates[-1]}
    for scen in ("red", "green"):
        d = "down" if scen == "red" else "up"
        n = (done["n"] + 1) if (done and done["dir"] == d) else 1
        c["next"][scen] = {"dir": d, "n": n, "context": run_context(hist_dates, hist_closes, d, n, skip_last=bool(done and done["dir"] == d))}
    # the day against its own history
    rets = [all_closes[k] / all_closes[k - 1] - 1 for k in range(1, len(all_closes))]
    sd = statistics.pstdev(rets[-21:-1]) if len(rets) >= 21 else None
    if sd:
        c["sigma"] = round(chg / sd, 2)
        c["rv20"] = round(sd * math.sqrt(252) * 100, 1)
    bigger = [k for k in range(len(rets) - 1) if (rets[k] <= chg if chg < 0 else rets[k] >= chg)]
    if chg and bigger:
        c["largestSince"] = all_dates[bigger[-1] + 1]
    elif chg:
        c["largestSince"] = None                                         # the largest in the record
    # highs, drawdowns
    hi252 = roll_max(all_closes, 252)
    ath = max(all_closes)
    c["high52"] = {"close": round(hi252[i], 4), "date": all_dates[max(range(max(0, i - 251), i + 1), key=lambda k: all_closes[k])],
                   "dd": round((last / hi252[i] - 1) * 100, 2)}
    c["high"] = {"close": round(ath, 4), "date": all_dates[all_closes.index(ath)], "dd": round((last / ath - 1) * 100, 2), "since": all_dates[0]}
    dd_series = [all_closes[k] / hi252[k] - 1 for k in range(len(all_closes))]
    for lvl in (0.05, 0.10, 0.20):
        if dd_series[i] <= -lvl:
            k = i
            while k > 0 and dd_series[k - 1] <= -lvl:
                k -= 1
            c.setdefault("ddEpisodes", {})[f"{int(lvl * 100)}"] = {"since": all_dates[k], "prev": last_where(all_dates, [x <= -lvl for x in dd_series], k)}
    # moving averages and crosses
    for n in (20, 50, 200):
        m = sma(all_closes, n)
        if m:
            above = last > m
            k = i
            while k >= n - 1 and ((all_closes[k] > sum(all_closes[k - n + 1:k + 1]) / n) == above):
                k -= 1
            c.setdefault("ma", {})[str(n)] = {"value": round(m, 4), "above": above, "pct": round((last / m - 1) * 100, 2),
                                              "sinceCross": all_dates[k + 1] if k >= n - 1 else None}
    if len(all_closes) >= 201:
        def cross_state(k):
            return sum(all_closes[k - 49:k + 1]) / 50 > sum(all_closes[k - 199:k + 1]) / 200
        st = cross_state(i)
        k = i
        while k >= 200 and cross_state(k) == st:
            k -= 1
        c["cross"] = {"state": "golden" if st else "death", "since": all_dates[k + 1] if k >= 200 else None}
    # weekly runs (week = Monday-dated ISO week; the current week counts once it has a close)
    wk = {}
    for d, x in zip(all_dates, all_closes):
        y, w, _ = date.fromisoformat(d).isocalendar()
        wk[(y, w)] = (d, x)
    wl = [wk[k] for k in sorted(wk)]
    if len(wl) >= 3:
        wd, wc = [x[0] for x in wl], [x[1] for x in wl]
        cw = current_run(wd, wc)
        if cw:
            cw["context"] = run_context(wd, wc, cw["dir"], cw["n"])
            cw["weekToDate"] = round((wc[-1] / wc[-2] - 1) * 100, 2)
            c["weeks"] = cw
        wr = [wc[k] / wc[k - 1] - 1 for k in range(1, len(wc))]
        if wr:
            r0 = wr[-1]
            prev = [k for k in range(len(wr) - 1) if (wr[k] <= r0 if r0 < 0 else wr[k] >= r0)]
            c["week"] = {"chg": round(r0 * 100, 2), "largestSince": wd[prev[-1] + 1] if prev else None, "weekOf": wd[-1]}
    # distribution days: a 0.2 %+ decline on higher volume than the prior session (completed sessions with volume)
    if with_volume:
        v = vols[:len(hist_closes)]
        dd_days = [hist_dates[k] for k in range(max(1, len(hist_closes) - 25), len(hist_closes))
                   if v[k] and v[k - 1] and hist_closes[k] / hist_closes[k - 1] - 1 <= -0.002 and v[k] > v[k - 1]]
        c["distribution"] = {"count": len(dd_days), "days": dd_days, "window": 25}
    c["_series"] = (all_dates, all_closes)                                # dropped before writing
    return c


# ------------------------------------------------------------------------------------------- S&P 500 internals ---
def spx_weights(universe):
    """State Street's SPY holdings -> ({ticker: weight}, holdings date)."""
    idx = ((universe or {}).get("indices") or {}).get("SPY") or {}
    w = {str(m.get("ticker") or "").upper(): float(m.get("weight") or 0) for m in idx.get("members") or [] if m.get("ticker")}
    return {t: x for t, x in w.items() if x > 0}, str(idx.get("updatedAt") or (universe or {}).get("updatedAt") or "")[:10]


def drifted(weights, base, prev, index_drift=1.0):
    """Weights at the prior close: holdings weight x (prior close / holdings-date price), renormalised over all
    members; a member without prices drifts with the index."""
    raw = {}
    for t, w in weights.items():
        b, p = base.get(t), prev.get(t)
        raw[t] = w * (p / b if b and p else index_drift)
    s = sum(raw.values())
    return {t: x / s for t, x in raw.items()} if s else dict(weights)


def internals(prices, weights, base, sectors, index_drift=1.0):
    """One session from the inside. prices {t: (price or close, prior close)} -> breadth, cap- vs equal-weighted, what
    moved the index (bp of index = weight x move), by name and by sector, members {t: [move %, bp]}."""
    prev = {t: pc for t, (px, pc) in prices.items() if pc}
    w = drifted(weights, base, prev, index_drift)
    mem, suspect = {}, []
    for t, wt in w.items():
        px, pc = prices.get(t, (None, None))
        if not px or not pc:
            continue
        r = px / pc - 1
        if abs(r) > SUSPECT:
            suspect.append({"t": t, "move": round(r * 100, 2), "why": f"a {r * 100:+.0f}% move is a data check (split, bad print) - left out"})
            continue
        mem[t] = (r, wt)
    if not mem:
        return None
    up = sum(1 for r, _ in mem.values() if r > 0)
    down = sum(1 for r, _ in mem.values() if r < 0)
    wcov = sum(wt for _, wt in mem.values())
    contrib = {t: r * wt for t, (r, wt) in mem.items()}
    explained = sum(contrib.values())
    by_sec = {}
    for t, cbp in contrib.items():
        s = sectors.get(t) or "Other"
        a = by_sec.setdefault(s, [0.0, 0.0, 0])
        a[0] += cbp
        a[1] += mem[t][1]
        a[2] += 1
    order = sorted(contrib, key=contrib.get)
    neg = [t for t in order if contrib[t] < 0]
    total_neg = sum(contrib[t] for t in neg) or None
    top_w = sorted(w, key=w.get, reverse=True)[:10]
    mega = sum(contrib.get(t, 0) for t in top_w)
    row = lambda t: {"t": t, "w": round(mem[t][1] * 100, 3), "move": round(mem[t][0] * 100, 2), "bp": round(contrib[t] * 1e4, 1),
                     "sector": sectors.get(t)}
    return {"members": len(w), "priced": len(mem), "weightPriced": round(wcov, 4), "up": up, "down": down, "flat": len(mem) - up - down,
            "pctUp": round(up / len(mem) * 100, 1), "ew": round(statistics.fmean(r for r, _ in mem.values()) * 100, 3),
            "cw": round(explained / wcov * 100, 3) if wcov else None, "explained": round(explained * 100, 3),
            "top": [row(t) for t in order[:10]], "bottom": [row(t) for t in order[::-1][:10] if contrib[t] > 0],
            "sectors": sorted(({"sector": s, "bp": round(a[0] * 1e4, 1), "move": round(a[0] / a[1] * 100, 2) if a[1] else None,
                                "w": round(a[1] * 100, 2), "n": a[2]} for s, a in by_sec.items()), key=lambda x: x["bp"]),
            "concentration": {"top5": round(sum(contrib[t] for t in neg[:5]) / total_neg, 3) if total_neg else None,
                              "top10": round(sum(contrib[t] for t in neg[:10]) / total_neg, 3) if total_neg else None,
                              "mega10": round(mega * 100, 3), "rest": round((explained - mega) * 100, 3)},
            "suspect": suspect, "_contrib": {t: [round(mem[t][0] * 100, 2), round(contrib[t] * 1e4, 2)] for t in mem}}


def streak_attribution(days):
    """Sum each session's contributions over a run -> what moved the index across it. days: [(date, members {t: [move, bp]}, sectors)]"""
    tot, n_days = {}, 0
    for _, mem, _ in days:
        if not mem:
            continue
        n_days += 1
        for t, (_, bp) in mem.items():
            tot[t] = tot.get(t, 0.0) + bp
    if not tot:
        return None
    sec = {}
    secmap = {}
    for _, _, s in days:
        secmap.update(s or {})
    for t, bp in tot.items():
        sec[secmap.get(t) or "Other"] = sec.get(secmap.get(t) or "Other", 0.0) + bp
    order = sorted(tot, key=tot.get)
    return {"days": n_days, "from": days[0][0], "to": days[-1][0], "bp": round(sum(tot.values()), 1),
            "top": [{"t": t, "bp": round(tot[t], 1), "sector": secmap.get(t)} for t in order[:10]],
            "bottom": [{"t": t, "bp": round(tot[t], 1), "sector": secmap.get(t)} for t in order[::-1][:5] if tot[t] > 0],
            "sectors": sorted(({"sector": s, "bp": round(v, 1)} for s, v in sec.items()), key=lambda x: x["bp"])}


# ------------------------------------------------------------------------------------------------- the read ---
REGIME_STATES = [(0, "calm"), (2, "under pressure"), (3, "sell-off"), (5, "heavy sell-off")]


def sell_off_read(spx, vix=None, ins=None, hyg=None):
    """Seven plain criteria -> state. Each criterion shows its value, so the call can be checked."""
    if not spx:
        return None
    crit = []

    def add(k, on, text, value=None):
        crit.append({"k": k, "on": bool(on) if on is not None else None, "text": text, "value": value})
    s = spx.get("streak") or {}
    pv = spx.get("provisional") or {}
    run_n = (pv.get(pv.get("now")) or {}).get("n") if pv.get("now") == "red" else (s.get("n") if s.get("dir") == "down" else 0)
    add("streak", (run_n or 0) >= 4, f"{run_n or 0} straight lower closes{' (counting today, provisional)' if pv.get('now') == 'red' else ''} - on at 4+", run_n)
    dd = (spx.get("high52") or {}).get("dd")
    add("drawdown", dd is not None and dd <= -5, f"{dd:+.1f}% from the 52-week high - on at -5%" if dd is not None else "no 52-week high", dd)
    m50 = (spx.get("ma") or {}).get("50") or {}
    add("trend", m50 and not m50.get("above"), f"{m50.get('pct', 0):+.1f}% vs the 50-day average - on below it" if m50 else "no 50-day average", m50.get("pct") if m50 else None)
    if vix:
        five = None
        ser = vix.get("_series")
        if ser and len(ser[1]) > 6:
            five = round((ser[1][-1] / ser[1][-6] - 1) * 100, 1)
        add("fear", vix["close"] >= 25 or (five is not None and five >= 30), f"VIX {vix['close']:.1f} ({five:+.0f}% over 5 sessions) - on at 25+ or +30% in a week" if five is not None else f"VIX {vix['close']:.1f}", vix["close"])
    else:
        add("fear", None, "no VIX read")
    dist = (spx.get("distribution") or {}).get("count")
    add("distribution", dist is not None and dist >= 5, f"{dist} distribution days in 25 sessions - on at 5+" if dist is not None else "no volume read", dist)
    if ins:
        add("breadth", ins["pctUp"] <= 40 or ins["ew"] <= -1.0, f"{ins['pctUp']:.0f}% of members up, equal-weighted {ins['ew']:+.2f}% - on at 40% or under, or -1%", ins["pctUp"])
    else:
        add("breadth", None, "no member prices")
    if hyg and hyg.get("_series") and len(hyg["_series"][1]) > 6:
        h5 = round((hyg["_series"][1][-1] / hyg["_series"][1][-6] - 1) * 100, 2)
        add("credit", h5 <= -1.0, f"high-yield bonds (HYG) {h5:+.2f}% over 5 sessions - on at -1%", h5)
    else:
        add("credit", None, "no HYG read")
    n_on = sum(1 for x in crit if x["on"])
    state = [name for lo, name in REGIME_STATES if n_on >= lo][-1]
    overlay = None
    if dd is not None and dd <= -20:
        overlay = "bear market"
    elif dd is not None and dd <= -10:
        overlay = "correction"
    known = sum(1 for x in crit if x["on"] is not None)
    return {"state": state, "overlay": overlay, "on": n_on, "of": known, "criteria": crit,
            "label": f"{state}{' · ' + overlay if overlay else ''}", "provisional": bool(pv.get("now"))}


def _when(d):
    try:
        x = date.fromisoformat(d)
        return f"{x.strftime('%b')} {x.day}, {x.year}"
    except Exception:
        return str(d)


def _range(a, b):
    """Apr 12-19, 2024 · Oct 25 - Nov 4, 2016 · Dec 30, 2024 - Jan 6, 2025"""
    try:
        x, y = date.fromisoformat(a), date.fromisoformat(b)
    except Exception:
        return f"{a} - {b}"
    if x == y:
        return _when(a)
    if x.year != y.year:
        return f"{_when(a)} - {_when(b)}"
    if x.month != y.month:
        return f"{x.strftime('%b')} {x.day} - {y.strftime('%b')} {y.day}, {y.year}"
    return f"{x.strftime('%b')} {x.day}-{y.day}, {y.year}"


def _rare(ctx):
    n = ctx.get("atLeast")
    return 3 if n is not None and n <= 20 else 2 if n is not None and n <= 60 else 1


def run_text(name, d, n, ctx, frm=None):
    word = "lower" if d == "down" else "higher"
    s = f"{name}: {n} straight {word} closes" + (f" (from {_when(frm)})" if frm else "")
    if ctx.get("last"):
        s += f" - the first {n}-session {'losing' if d == 'down' else 'winning'} run since {_range(ctx['last']['from'], ctx['last']['to'])}"
    elif ctx.get("since"):
        s += f" - none that long since the data starts ({_when(ctx['since'])})"
    s += f"; {ctx.get('atLeast', 0)} run{'s' if ctx.get('atLeast') != 1 else ''} of {n}+ since {ctx.get('since', '')[:4]}"
    if ctx.get("record"):
        s += f", the record {ctx['record']['n']} ({_when(ctx['record']['from'])})"
    return s + "."


def notables(cards, ins, read, attrib=None, nxt=None):
    """The day's notable things, most notable first. -> [{id, sev, kind, text}]"""
    out = []
    for c in cards:
        if not c:
            continue
        sym, name = c["sym"], c["name"]
        s, pv = c.get("streak"), c.get("provisional")
        if s and s["n"] >= (3 if sym in dict(INDICES) else 4):
            out.append({"id": f"{sym}:streak", "sym": sym, "kind": "streak", "sev": _rare(s["context"]) if s["n"] >= 4 else 1,
                        "text": run_text(name, s["dir"], s["n"], s["context"], s["from"])})
        if pv and s:
            cont = "red" if s["dir"] == "down" else "green"
            sc = pv.get(cont) or {}
            if sc.get("n", 0) >= 4 and sym in dict(INDICES):
                ctx = sc["context"]
                t = (f"{name} is {c['chg']:+.2f}% at {c.get('asOf') or 'the last tape'} ET{f', after opening {c['gap']:+.2f}%' if c.get('gap') is not None else ''}: "
                     f"a {cont} close makes it {sc['n']} straight {'lower' if cont == 'red' else 'higher'} closes"
                     + (f" - the first since {_range(ctx['last']['from'], ctx['last']['to'])}" if ctx.get("last") else " - the longest in the data")
                     + f" ({ctx.get('atLeast', 0)} since {str(ctx.get('since', ''))[:4]}). A {'green' if cont == 'red' else 'red'} close ends the run at {s['n']}.")
                out.append({"id": f"{sym}:if", "sym": sym, "kind": "provisional", "sev": _rare(ctx), "text": t})
        sig = c.get("sigma")
        big = (sig is not None and abs(sig) >= 2.5) if sym in dict(VOLS) else (abs(c["chg"]) >= 1.5 or (sig is not None and abs(sig) >= 2.5)) and (sym in dict(INDICES) or abs(c["chg"]) >= 3)
        if c.get("chg") is not None and big:
            since = c.get("largestSince")
            t = (f"{name} {c['chg']:+.2f}%{' so far' if not c['final'] else ''}"
                 + (f" ({sig:+.1f} sd of its 20-day moves)" if sig is not None else "")
                 + (f" - its largest {'drop' if c['chg'] < 0 else 'gain'} since {_when(since)}" if since else " - the largest in the data")
                 + ".")
            out.append({"id": f"{sym}:move", "sym": sym, "kind": "move", "sev": 3 if not since or since < (date.fromisoformat(c['date']) - timedelta(days=365)).isoformat() else 2, "text": t})
        ep = c.get("ddEpisodes") or {}
        if sym in dict(INDICES):
            for lvl, word in (("20", "a bear market"), ("10", "a correction"), ("5", "a pullback")):
                if lvl in ep:
                    h = c["high52"]
                    out.append({"id": f"{sym}:dd", "sym": sym, "kind": "drawdown", "sev": 3 if lvl != "5" else 2,
                                "text": f"{name} is {h['dd']:+.1f}% from its 52-week high ({_when(h['date'])}) - {word} (-{lvl}%+) since {_when(ep[lvl]['since'])}"
                                        + (f"; the previous one ended {_when(ep[lvl]['prev'])}." if ep[lvl].get("prev") else ".")})
                    break
            for n, label in (("200", "200-day"), ("50", "50-day")):
                m = (c.get("ma") or {}).get(n)
                if m and m.get("sinceCross") == c["date"]:
                    out.append({"id": f"{sym}:ma{n}", "sym": sym, "kind": "trend", "sev": 2 if n == "200" else 1,
                                "text": f"{name} {'crossed back above' if m['above'] else 'fell below'} its {label} average ({m['value']:,.2f}) {'today' if not c['final'] else 'on ' + _when(c['date'])}."})
            cr = c.get("cross")
            if cr and cr.get("since") == c["date"]:
                out.append({"id": f"{sym}:cross", "sym": sym, "kind": "trend", "sev": 2,
                            "text": f"{name}: a {cr['state']} cross today - the 50-day average moved {'above' if cr['state'] == 'golden' else 'below'} the 200-day."})
            w = c.get("weeks")
            if w and w["n"] >= 3:
                out.append({"id": f"{sym}:weeks", "sym": sym, "kind": "streak", "sev": _rare(w["context"]),
                            "text": f"{name}: {w['n']} straight {'lower' if w['dir'] == 'down' else 'higher'} weekly closes (this week {w['weekToDate']:+.2f}% so far)"
                                    + (f" - the first since the week of {_when(w['context']['last']['from'])}." if w["context"].get("last") else ".")})
        if sym == "^VIX":
            hi_since = None
            ser = c.get("_series")
            if ser:
                ds, xs = ser
                hi_since = last_where(ds, [x >= c["close"] for x in xs], len(xs) - 1)
            if c["close"] >= 25 or (hi_since and hi_since < (date.fromisoformat(c["date"]) - timedelta(days=60)).isoformat()):
                out.append({"id": "^VIX:level", "sym": sym, "kind": "fear", "sev": 3 if c["close"] >= 30 else 2,
                            "text": f"VIX {c['close']:.2f} ({c['chg']:+.1f}%) - the highest since {_when(hi_since)}." if hi_since else f"VIX {c['close']:.2f} - the highest in the data."})
        if sym in dict(INDICES) and c.get("gap") is not None and c.get("fromOpen") is not None \
                and c["gap"] * c["fromOpen"] < 0 and abs(c["fromOpen"]) >= 1.0 and c["dir"] != "flat" and (c["gap"] > 0) != (c["chg"] > 0):
            out.append({"id": f"{sym}:reversal", "sym": sym, "kind": "move", "sev": 2,
                        "text": f"{name} opened {'higher' if c['gap'] > 0 else 'lower'} ({c['gap']:+.2f}%) and {'is' if not c['final'] else 'closed'} "
                                f"{'lower' if c['chg'] < 0 else 'higher'} ({c['chg']:+.2f}%) - a {abs(c['fromOpen']):.2f}% {'slide' if c['fromOpen'] < 0 else 'climb'} from the open."})
        wk = c.get("week")
        if sym in dict(INDICES) and wk and abs(wk["chg"]) >= 3:
            out.append({"id": f"{sym}:week", "sym": sym, "kind": "move", "sev": 3 if abs(wk["chg"]) >= 5 else 2,
                        "text": f"{name} {wk['chg']:+.2f}% this week{' so far' if not c['final'] or date.fromisoformat(c['date']).weekday() < 4 else ''}"
                                + (f" - its largest weekly {'drop' if wk['chg'] < 0 else 'gain'} since the week of {_when(wk['largestSince'])}." if wk.get("largestSince") else " - the largest in the data.")})
        dist = c.get("distribution")
        if dist and dist["count"] >= 5:
            out.append({"id": f"{sym}:dist", "sym": sym, "kind": "distribution", "sev": 2,
                        "text": f"{name}: {dist['count']} distribution days in the last 25 sessions (a 0.2%+ decline on higher volume) - institutions selling into the tape."})
    if ins:
        gap = round(ins["cw"] - ins["ew"], 2) if ins.get("cw") is not None else None
        top = ins["top"][:5]
        names = ", ".join(f"{x['t']} {x['move']:+.1f}%" for x in top)
        bp = sum(x["bp"] for x in top)
        t = (f"Inside the S&P 500: {ins['up']} of {ins['priced']} priced members rose, {ins['down']} fell; cap-weighted {ins['cw']:+.2f}% vs "
             f"equal-weighted {ins['ew']:+.2f}%"
             + (f" - {'narrow: the biggest names did most of it' if gap is not None and abs(gap) >= 0.75 and abs(ins['cw']) > abs(ins['ew']) else 'broad: the typical stock moved as much as the index' if gap is not None and abs(gap) < 0.4 else 'mixed'}" if gap is not None else "")
             + f". Largest drags: {names} = {bp:+.0f} bp of the index.")
        out.append({"id": "spx:inside", "sym": SPX, "kind": "breadth", "sev": 2 if gap is not None and abs(gap) >= 0.75 else 1, "text": t})
        if ins["pctUp"] <= 15 or ins["pctUp"] >= 85:
            out.append({"id": "spx:lopsided", "sym": SPX, "kind": "breadth", "sev": 3 if ins["pctUp"] <= 10 or ins["pctUp"] >= 90 else 2,
                        "text": f"A lopsided {'down' if ins['pctUp'] <= 15 else 'up'} day: {ins['pctUp']:.0f}% of the priced S&P 500 members rose ({ins['up']} of {ins['priced']})."})
        sec = ins.get("sectors") or []
        if sec:
            worst, best = sec[0], sec[-1]
            out.append({"id": "spx:sectors", "sym": SPX, "kind": "sectors", "sev": 1,
                        "text": f"By sector (bp of the S&P 500): {worst['sector']} {worst['bp']:+.0f} bp ({worst['move']:+.2f}%), "
                                + ", ".join(f"{x['sector']} {x['bp']:+.0f}" for x in sec[1:4])
                                + f" … {best['sector']} {best['bp']:+.0f} bp ({best['move']:+.2f}%)."})
        if ins.get("suspect"):
            out.append({"id": "spx:check", "sym": SPX, "kind": "data", "sev": 1,
                        "text": "Data check: " + "; ".join(f"{x['t']} {x['move']:+.0f}%" for x in ins["suspect"][:4]) + " - left out of the attribution until confirmed."})
    if attrib and attrib.get("days", 0) >= 2:
        names = ", ".join(f"{x['t']} {x['bp']:+.0f}" for x in attrib["top"][:5])
        out.append({"id": "spx:run", "sym": SPX, "kind": "attribution", "sev": 2,
                    "text": f"Across the run ({_when(attrib['from'])} - {_when(attrib['to'])}, {attrib['days']} sessions priced): the members took {attrib['bp']:+.0f} bp off the S&P 500; "
                            f"the most: {names} bp; by sector {attrib['sectors'][0]['sector']} {attrib['sectors'][0]['bp']:+.0f} bp."})
    if read:
        out.append({"id": "read", "sym": SPX, "kind": "regime", "sev": 3 if read["state"] in ("sell-off", "heavy sell-off") or read.get("overlay") else 1,
                    "text": f"The sell-off read: {read['label']} - {read['on']} of {read['of']} criteria on ("
                            + "; ".join(x["text"] for x in read["criteria"] if x["on"]) + ")."})
    if nxt and nxt.get("events"):
        h20, h5 = (nxt["h"].get("20") or {}), (nxt["h"].get("5") or {})
        if h20.get("count"):
            out.append({"id": "spx:next", "sym": SPX, "kind": "history", "sev": 1,
                        "text": (f"If today closes {'red' if nxt['dir'] == 'down' else 'green'}: " if nxt.get("ifToday") else "")
                                + f"after the {nxt['events']} earlier S&P 500 runs that reached {nxt['n']} {'lower' if nxt['dir'] == 'down' else 'higher'} closes, "
                                f"5 sessions on it was higher {round((h5.get('up') or 0) * 100)}% of the time (median {h5.get('median', 0):+.2f}%), "
                                f"20 sessions on {round(h20['up'] * 100)}% (median {h20['median']:+.2f}%) - vs {round(h20['baseUp'] * 100)}% / {h20['baseMedian']:+.2f}% for any day. "
                                f"{'A small sample - a base rate, not a forecast.' if nxt['events'] < 15 else 'A base rate, not a forecast.'}"})
    for x in out:
        if x.get("sym") not in (SPX, None) and x["kind"] != "fear":
            x["sev"] = min(x["sev"], 2)
    out.sort(key=lambda x: (-x["sev"], ["provisional", "streak", "move", "regime", "drawdown", "breadth", "attribution", "fear", "distribution", "trend", "sectors", "history", "data"].index(x["kind"]) if x["kind"] in ("provisional", "streak", "move", "regime", "drawdown", "breadth", "attribution", "fear", "distribution", "trend", "sectors", "history", "data") else 99))
    return out


# ---------------------------------------------------------------------------------------------- data access ---
def load_member_prices(day, tapes):
    """{ticker: (price or close, prior close)} for a session from the books' tapes (latest.json for today in the
    session; the day file's official closes for a finished session)."""
    out = {}
    for tp in tapes:
        if not tp:
            continue
        pc = tp.get("prevClose") or {}
        cl = tp.get("close") or {}
        px = tp.get("p") or {}
        snaps = tp.get("snapshots")
        for t, p0 in pc.items():
            prev = _num(p0)
            if not prev:
                continue
            cur = _num(cl.get(t)) or _num(px.get(t))
            if cur is None and snaps:
                cur = next((_num((s.get("p") or {}).get(t)) for s in reversed(snaps) if (s.get("p") or {}).get(t) is not None), None)
            if cur and t not in out:
                out[str(t).upper()] = (cur, prev)
    return out


def book_tape(book, day=None):
    return fetch_json(f"{RAW}/{book}/main/data/intraday/{day or 'latest'}.json")


def weights_base(weights, wdate, cached):
    """Each member's close on the holdings date (cached until the holdings change). -> {t: close}"""
    if cached and cached.get("asOf") == wdate and cached.get("prices"):
        return cached["prices"]
    if os.environ.get("TRENDS_OFFLINE") == "1" or not wdate:
        return (cached or {}).get("prices") or {}

    def one(t):
        for b in BOOKS + (MACRO_BOOK,):
            rows = fetch_json(hist_url(t, b), tries=1, timeout=30)
            if rows:
                ds, cs, _ = series(rows)
                k = max((i for i, d in enumerate(ds) if d <= wdate), default=None)
                if k is not None:
                    return t, cs[k]
        return t, None
    with ThreadPoolExecutor(max_workers=8) as ex:
        got = dict(r for r in ex.map(one, sorted(weights)) if r[1])
    log(f"weights base: {len(got)} of {len(weights)} members priced on {wdate}")
    return got


def sector_map():
    nodes = (_read(DATA / "web" / "nodes.json", {}) or {}).get("nodes") or {}
    return {t: n.get("sector") for t, n in nodes.items() if n.get("sector")}


# ---------------------------------------------------------------------------------------------------- build ---
def strip(c, lean=False):
    """A card as written. lean (sectors, VIX, cross-asset): the next-close scenarios are dropped - the notables already
    say them; the app and the bot use the indices' (keeps latest.json ~10 KB smaller every run)."""
    if not c:
        return None
    out = {k: v for k, v in c.items() if not k.startswith("_")}
    if lean:
        out.pop("next", None)
        if out.get("provisional"):
            out["provisional"] = {"now": out["provisional"].get("now")}
    return out


def phase_of(tape):
    """premarket / session / after-close on a trading day; closed on a weekend or holiday."""
    if not tape or tape.get("session") not in ("regular", "early-close"):
        return "closed"
    m = int(tape.get("etMinute") or 0)
    if m < OPEN_MIN:
        return "premarket"
    return "session" if tape.get("marketOpen") else "after-close"


def macro_context():
    """The slower read next to the fast one: TRAPP2-ANALYTICS' early warning (fundamental, credit, labour data)."""
    w = _read(DATA / "warning" / "signals.json", {}) or {}
    if w.get("risk") is None:
        return None
    return {"risk": w.get("risk"), "level": w.get("level"), "regime": w.get("regime"), "date": w.get("date"),
            "headline": w.get("headline"), "provisional": bool(w.get("provisional"))}


def build(hist, tape, member_tapes, universe, sectors, base, prior_days=None, macro=None, day_tape=None):
    """hist {sym: rows}; tape: TRAPP2-1's latest; member_tapes: [the equity books' latest]; prior_days {date: members}
    for the streak attribution. -> the latest.json document (with a members map for the daily file)."""
    cards = {}
    for sym, name in INDICES + VOLS + SECTORS + CROSS:
        try:
            cards[sym] = card(sym, name, hist.get(sym), tape, with_volume=(sym == SPX))
        except Exception as e:
            log(f"{sym}: {type(e).__name__}: {e}")
            cards[sym] = None
    spx = cards.get(SPX)
    if not spx:
        return None
    if day_tape:                                         # a finished session read from its day file: its official opens
        for c in cards.values():
            o = _num((day_tape.get("open") or {}).get(c["sym"])) if c else None
            if c and o and "open" not in c and day_tape.get("date") == c["date"]:
                c["open"] = round(o, 4)
                c["gap"] = round((o / c["prevClose"] - 1) * 100, 3)
                c["fromOpen"] = round((c["close"] / o - 1) * 100, 3)
    weights, wdate = spx_weights(universe)
    ins = None
    if weights:
        prices = load_member_prices(spx["date"], member_tapes)
        ser_d, ser_c = spx["_series"]
        k0 = max((i for i, d in enumerate(ser_d) if d <= wdate), default=None) if wdate else None
        drift = (spx["prevClose"] / ser_c[k0]) if k0 is not None else 1.0
        ins = internals(prices, weights, base or {}, sectors or {}, drift)
    # the run, attributed: every session in the S&P's current down / up run that has members on file (+ today)
    attrib = None
    run = (spx.get("provisional") or {}).get(spx["provisional"]["now"]) if spx.get("provisional") and spx["provisional"]["now"] in ("red", "green") else spx.get("streak")
    s = spx.get("streak")
    if s and ins:
        days_in = [d for d in spx["_series"][0] if s["from"] <= d <= s["through"]]
        rows = [(d, (prior_days or {}).get(d), sectors) for d in days_in]
        cont = (spx.get("provisional") or {}).get("now") == ("red" if s["dir"] == "down" else "green")
        if spx["date"] not in days_in and (cont or spx["final"]):
            rows.append((spx["date"], ins["_contrib"], sectors))
        attrib = streak_attribution(rows)
        if attrib:
            attrib["sessions"] = len(rows)
    sd, sc = spx["_series"]
    hist_d, hist_c = (sd[:-1], sc[:-1]) if not spx["final"] else (sd, sc)
    nxt = None
    if run and run.get("n", 0) >= 2:
        nxt = forward_stats(hist_d, hist_c, run["dir"], run["n"])
        nxt["dir"] = run["dir"]
        nxt["ifToday"] = not spx["final"]
    read = sell_off_read(spx, cards.get("^VIX"), ins, cards.get("HYG"))
    notes = notables([cards.get(s) for s, _ in INDICES + VOLS + SECTORS + CROSS], ins, read, attrib, nxt)
    if macro and read:
        notes.append({"id": "macro", "sym": SPX, "kind": "history", "sev": 1,
                      "text": f"The slower read (early warning: credit, lending, labour and valuation data, {macro.get('date')}): {str(macro.get('level') or '').upper()} risk"
                              f" ({round(float(macro['risk']))}th percentile), {macro.get('regime')}. "
                              + ("The tape is selling while the fundamentals it watches are not flashing - a market-led sell-off so far."
                                 if read["state"] in ("sell-off", "heavy sell-off") and float(macro["risk"]) < 60 else
                                 "Both reads point the same way." if read["state"] in ("sell-off", "heavy sell-off") else "")})
    doc = {"build": BUILD, "asOf": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "date": spx["date"],
           "state": "final" if spx["final"] else "intraday", "phase": phase_of(tape), "etTime": (tape or {}).get("etTime"), "macro": macro,
           "tape": {"asOf": (tape or {}).get("asOf"), "session": (tape or {}).get("session"), "marketOpen": (tape or {}).get("marketOpen")},
           "read": read, "notable": notes, "next": nxt,
           "indices": {s: strip(cards.get(s)) for s, _ in INDICES},
           "vol": {s: strip(cards.get(s), lean=True) for s, _ in VOLS},
           "sectors": {s: strip(cards.get(s), lean=True) for s, _ in SECTORS},
           "cross": {s: strip(cards.get(s), lean=True) for s, _ in CROSS},
           "internals": {k: v for k, v in (ins or {}).items() if not k.startswith("_")} if ins else None,
           "runAttribution": attrib,
           "weights": {"source": "State Street SPY holdings (data/earnings/universe.json)", "asOf": wdate, "members": len(weights),
                       "drift": "each weight x (prior close / holdings-date close), renormalised", "basePriced": len(base or {})},
           "method": {"red": "a close below the prior close (close-to-close) - the open does not decide it",
                      "gap": "the official open against the prior close", "fromOpen": "the latest price or close against the official open",
                      "provisional": "in the session: the latest tape price; final once the tape has the official close",
                      "since": "runs and extremes are counted in this record (S&P 500 daily closes since its first row)"},
           "_members": (ins or {}).get("_contrib")}
    return doc


def history_rows(spx_rows, vix_rows, daily_dir, keep=KEEP_HISTORY):
    """One compact row per session from the index series (+ the internals of sessions with a daily record)."""
    ds, cs, _ = series(spx_rows)
    vd, vc, _ = series(vix_rows) if vix_rows else ([], [], None)
    vix = dict(zip(vd, vc))
    hi = roll_max(cs, 252)
    out = []
    start = max(1, len(cs) - keep)
    rs = runs(cs)
    run_at = {}
    for d0, a, b in rs:
        for k in range(a, b + 1):
            run_at[k] = (d0, k - a + 1)
    for k in range(start, len(cs)):
        rd, rn = run_at.get(k, (0, 0))
        row = {"date": ds[k], "spx": round(cs[k], 2), "chg": round((cs[k] / cs[k - 1] - 1) * 100, 3),
               "run": rn * (1 if rd > 0 else -1 if rd < 0 else 0), "dd52": round((cs[k] / hi[k] - 1) * 100, 2),
               "vix": vix.get(ds[k])}
        rec = _read(Path(daily_dir) / f"{ds[k]}.json") if daily_dir else None
        if rec and rec.get("internals"):
            i = rec["internals"]
            row.update(pctUp=i.get("pctUp"), ew=i.get("ew"), cw=i.get("cw"))
        g = ((rec or {}).get("indices") or {}).get(SPX) or {}
        if g.get("gap") is not None and g.get("date") == ds[k]:
            row.update(gap=g["gap"], fromOpen=g.get("fromOpen"))
        if rec and rec.get("read"):
            row.update(read=rec["read"].get("label"), on=rec["read"].get("on"))
        out.append(row)
    return out


def today_row(doc):
    """The history row for a FINAL session from its trends doc (None while the day is provisional)."""
    spx = ((doc or {}).get("indices") or {}).get(SPX) or {}
    if (doc or {}).get("state") != "final" or not spx.get("final") or spx.get("date") != doc.get("date"):
        return None
    st = spx.get("streak") or {}
    run = st.get("n", 0) * (-1 if st.get("dir") == "down" else 1) if st.get("through") == spx["date"] else 0
    row = {"date": spx["date"], "spx": round(spx["close"], 2), "chg": spx.get("chg"), "run": run,
           "dd52": (spx.get("high52") or {}).get("dd"), "vix": ((doc.get("vol") or {}).get("^VIX") or {}).get("close")}
    i = doc.get("internals") or {}
    if i:
        row.update(pctUp=i.get("pctUp"), ew=i.get("ew"), cw=i.get("cw"))
    if spx.get("gap") is not None:
        row.update(gap=spx["gap"], fromOpen=spx.get("fromOpen"))
    if doc.get("read"):
        row.update(read=doc["read"].get("label"), on=doc["read"].get("on"))
    return row


def main():
    t0 = time.time()
    tape = fetch_json(f"{RAW}/{MACRO_BOOK}/main/data/intraday/latest.json")
    syms = [s for s, _ in INDICES + VOLS + SECTORS + CROSS]
    with ThreadPoolExecutor(max_workers=8) as ex:
        hist = dict(zip(syms, ex.map(lambda s: fetch_json(hist_url(s)), syms)))
    if not hist.get(SPX):
        log("no S&P 500 history - nothing to read (kept the last files)")
        return 0
    member_tapes = [book_tape(b) for b in BOOKS]
    universe = _read(DATA / "earnings" / "universe.json", {})
    weights, wdate = spx_weights(universe)
    cached = _read(OUT / "weights_base.json", {})
    base = weights_base(weights, wdate, cached)
    if base and (cached or {}).get("asOf") != wdate:
        _write(OUT / "weights_base.json", {"asOf": wdate, "prices": base, "note": "each S&P 500 member's close on the holdings date - drifts the weights"})
    sectors = sector_map()
    daily = OUT / "daily"
    ds, cs, _ = series(hist[SPX])
    # backfill: recent sessions without a record, from the books' day tapes (members) and the history (index)
    missing = [d for d in ds[-BACKFILL:] if (_read(daily / f"{d}.json") or {}).get("state") != "final"]   # absent, or left provisional
    prior = {}
    for d in ds[-BACKFILL:]:
        rec = _read(daily / f"{d}.json")
        if rec and rec.get("members"):
            prior[d] = rec["members"]
    for d in missing:
        tapes = [book_tape(b, d) for b in BOOKS]
        if not any(tapes):
            continue
        k = ds.index(d)
        sub = {s: [r for r in (hist.get(s) or []) if str(r.get("date"))[:10] <= d] for s in syms}
        past = build(sub, None, tapes, universe, sectors, base, prior, None, book_tape(MACRO_BOOK, d))
        if past and past.get("_members"):
            prior[d] = past["_members"]
            rec = {k2: v for k2, v in past.items() if k2 != "_members"}
            rec.update(state="final", backfilled=True, members=past["_members"])
            _write(daily / f"{d}.json", rec)
            log(f"backfilled {d}: {len(past['_members'])} members")
    doc = build(hist, tape, member_tapes, universe, sectors, base, prior, macro_context())
    if not doc:
        log("no read")
        return 0
    members = doc.pop("_members", None)
    same = lambda x: json.dumps({k: v for k, v in (x or {}).items() if k != "asOf"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if doc.get("state") == "final":
        # one record per session, written once the day is final (an intraday run only updates latest.json - keeps the
        # repo small; a day whose after-close run is missed is rebuilt by the backfill above on the next run)
        rec = dict(doc, members=members)
        if same(_read(daily / f"{doc['date']}.json")) != same(rec):
            _write(daily / f"{doc['date']}.json", rec)
    if same(_read(OUT / "latest.json")) != same(doc):
        _write(OUT / "latest.json", doc)                    # unchanged since the last run (after the close, a re-run): no commit
    rows = history_rows(hist[SPX], hist.get("^VIX"), daily)
    today = today_row(doc)
    if today and (not rows or rows[-1]["date"] < today["date"]):
        rows = (rows + [today])[-KEEP_HISTORY:]            # the final close before the index history has the day
    old = _read(OUT / "history.json") or {}
    if old.get("rows") != rows or old.get("build") != BUILD:
        _write(OUT / "history.json", {"build": BUILD, "asOf": doc["asOf"], "rows": rows})
    spx = doc["indices"][SPX]
    st = spx.get("streak") or {}
    log(f"trends {doc['date']} {doc['state']} {doc.get('etTime') or ''}: S&P 500 {spx['chg']:+.2f}% · run {st.get('dir')} {st.get('n')} "
        f"(through {st.get('through')}) · read {(doc.get('read') or {}).get('label')} · {len(doc['notable'])} notable · "
        f"members {((doc.get('internals') or {}).get('priced'))} · {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
