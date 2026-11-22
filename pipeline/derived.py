# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/derived.py
#  z203 - MARKET STATISTICS BY THE FORMULA ("derive, don't fetch", owner 11-21 0951 ET: "We need all data to be 100%
#  accurate ... rely on the formulas instead of trying to pull more from a website that may hit a limit").
#  Every number here is computed from the books' own daily histories (data/history/<T>.json: closes split-adjusted,
#  NOT dividend-adjusted - price returns - plus volume), for every ticker in the five books' master.json, then checked
#  against what Yahoo reports for the same thing where it reports it. The master rows of TRAPP2 / -1 / -2 / -3 have no
#  52-week high / low at all (every row blank on 11-20), 92+ have no beta (the valuation's CAPM then assumes 1.00) and a
#  few have no average volume; this fills them from data we already hold.
#
#  PER TICKER (data/derived/market.json -> tickers.<T>; the legend is in the file's "keys"):
#    * 52-week CLOSING range (h52 / l52 with dates; the sessions dated after the same day a year earlier) and the
#      distance from each - Yahoo's 52-week range is intraday, so a closing range sits inside it; "since" when the
#      history is younger than 52 weeks (the range is since then)
#    * the record close in our history (ath, ~20 years), the 50- / 200-session averages and the price vs each
#    * average volume over 20 and 63 sessions (a63 = Yahoo's 3-month "Avg. Volume"), 20-session dollar volume (quote
#      currency), the last session's volume vs its prior 63-session average
#    * realized volatility, annualized: sample sd of daily log returns over 20 / 60 / 252 observations x sqrt(252)
#      (x sqrt(365) for a 7-day market - crypto)
#    * beta vs the S&P 500 (^GSPC, TRAPP2-1's history): b5 = 60 COMPLETED monthly price returns (Yahoo's "Beta (5Y
#      Monthly)" method - the current, unfinished month is left out; on 11-21 it matched Yahoo within 0.011 on average
#      over 16 large caps, 0.041 at most), b1 = 252 daily returns on common sessions; each with its n and R^2
#    * price returns 1 / 3 / 6 months, YTD, 1 year (calendar: the close on or before the same day k months earlier),
#      3 and 5 years annualized; the largest peak-to-trough fall inside the 52 weeks with its dates
#    * flags q: "stale" (no session for 10+ days while the S&P traded), "short" (< 52 weeks of history),
#      "jump:<date>:x<ratio>" (a one-session move of x2 or more / -50 % or more in the last 5 years - a split the
#      history missed, or a real event: check it; the numbers it spans are not used to fill)
#    * u52 / ub - what the app may FILL where the master row has nothing: u52 = 1 when the closing range is clean (not
#      stale, no jump inside it); ub = [beta, "m"|"d"] for US-listed USD equities and ETFs: b5 with 24+ months and no
#      jump in its span, else b1 with 200+ sessions and none in the year
#  CHECKS (doc.checks): our b5 vs Yahoo's beta, our a63 vs Yahoo's average volume, our closing range vs Yahoo's intraday
#  range (TRAPP2-4 has one) - counts by band and the widest gaps, so a stale or broken figure on either side shows.
#  INPUTS: books/<book>/data/{master.json,history/<T>.json} - the analytics workflow checks all five books out; a book
#  missing on disk is read from raw.githubusercontent (gzip). No keys, no vendor calls.
#  WHEN: .github/workflows/analytics.yml, after the research grades (03:00 UTC after a trading day + 14:30 UTC on
#  weekdays). The file is rewritten only when something other than its time stamp changed.
#  Env: DERIVED_NOW=ISO (tests) · DERIVED_OFFLINE=1 (no fetches) · DERIVED_BOOKS=<dir> (checkouts; default ./books)
# ============================================================
import calendar
import gzip
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BUILD = "z203"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "derived" / "market.json"
RAW = "https://raw.githubusercontent.com/TheMostLocal"
BOOKS = ("TRAPP2", "TRAPP2-2", "TRAPP2-3", "TRAPP2-4", "TRAPP2-1")   # the first book listing a ticker wins (as shares.py)
BENCH = ("^GSPC", "TRAPP2-1")
BENCH_FALLBACK = ("SPY", "TRAPP2-1")
ET = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (valuatio derived)", "Accept-Encoding": "gzip"}
STALE_DAYS = 10                 # no session for this long while the benchmark traded = stale
JUMP = math.log(2.0)            # a one-session move of x2 or /2
B5_MONTHS, B5_MIN = 60, 24      # Yahoo's 60 monthly returns; fewer than 24 is not a 5-year beta
B1_SESSIONS, B1_MIN = 252, 200
BETA_BANDS = (0.05, 0.15)       # our b5 vs Yahoo's: within 0.05 = agrees, 0.05-0.15 = near, beyond = check
PX_TOL = 0.02                   # our last close vs the book's close for the same session
R52_TOL, R52_WIDE = 0.005, 1.25
ADV_BAND = 0.25                 # our 63-session average volume vs Yahoo's: beyond 25 % = check
REVIEW = 40
KEYS = {
    "d": "last session in our history", "px": "its close", "n": "sessions in our history", "f": "first session",
    "h52": "[52-week closing high, date]", "l52": "[52-week closing low, date]",
    "since": "first session when the history is younger than 52 weeks (the range is since then)",
    "fh": "% from the 52-week closing high", "fl": "% from the 52-week closing low",
    "ath": "[highest close in our history, date]", "fa": "% from it",
    "m50": "50-session average close", "v50": "% vs it", "m200": "200-session average close", "v200": "% vs it",
    "a20": "20-session average volume", "a63": "63-session average volume (Yahoo's 3-month)",
    "dv20": "20-session average close x volume (quote currency)", "rv": "last session's volume / prior 63-session average",
    "s20": "annualized volatility %, 20 observations", "s60": "60", "s252": "252",
    "ppy": "observations per year when not 252 (365 = a 7-day market)",
    "b5": "[beta vs ^GSPC, 60 completed monthly returns (Yahoo's method), n, R^2]",
    "b1": "[beta vs ^GSPC, 252 daily returns, n, R^2]",
    "r": "price returns %: 1m 3m 6m ytd 1y; 3y 5y annualized",
    "dd": "[largest peak-to-trough fall % inside the 52 weeks, peak date, trough date]",
    "q": "flags: stale · short · jump:<date>:x<ratio> · px:x<ratio> (our last close vs the book's for that session)",
    "u52": "1 = the closing range may fill a blank 52-week high / low",
    "x52": "Yahoo's 52-week range is wrong - show ours: 1 = a close outside it, 2 = its extreme a split factor beyond every close",
    "w52": "1 = Yahoo's 52-week extreme is 25 %+ beyond every close: show both",
    "ub": "[beta, 'm' monthly | 'd' daily] that may fill a blank beta (US-listed USD equities / ETFs)",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now_utc():
    v = os.environ.get("DERIVED_NOW")
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else datetime.now(timezone.utc)


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


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
    if os.environ.get("DERIVED_OFFLINE") == "1":
        return None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                body = r.read()
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    body = gzip.decompress(body)
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
        time.sleep(1.5 * (k + 1))
    return None


def books_dir():
    return Path(os.environ.get("DERIVED_BOOKS") or (ROOT / "books"))


def read_book_file(book, rel):
    """books/<book>/<rel> from the checkout; the raw URL when the book is not checked out."""
    base = books_dir() / book
    if base.is_dir():
        return _read(base / rel)
    return fetch_json(f"{RAW}/{book}/main/{urllib.parse.quote(rel)}")


def history(book, sym):
    return read_book_file(book, f"data/history/{sym}.json")


# ------------------------------------------------------------------------------------------------- series & dates ---
def series(rows):
    """History rows -> (dates, closes, volumes): sorted, one row per date, positive closes only."""
    by = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        d, c = str(r.get("date") or "")[:10], _num(r.get("close"))
        if len(d) == 10 and c and c > 0:
            by[d] = (c, _num(r.get("volume")))
    ds = sorted(by)
    return ds, [by[d][0] for d in ds], [by[d][1] for d in ds]


def _d(s):
    return date.fromisoformat(s)


def years_back(d, k=1):
    try:
        return d.replace(year=d.year - k)
    except ValueError:                       # 29 Feb
        return d.replace(year=d.year - k, day=28)


def months_back(d, k):
    y, m = divmod(d.month - 1 - k, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def at_or_before(ds, target_iso):
    """Index of the last session on or before target, or -1."""
    return bisect_right(ds, target_iso) - 1


def rnd(x, sig=6):
    return None if x is None else float(f"{x:.{sig}g}")


def pct(x, dp=2):
    return None if x is None else round(100.0 * x, dp)


# ------------------------------------------------------------------------------------------------------- formulas ---
def ols(x, y):
    """Slope of y on x and R^2 (sample moments) -> (beta, r2) or None."""
    n = len(x)
    if n < 3 or n != len(y):
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    if sxx <= 0:
        return None
    return sxy / sxx, (sxy * sxy / (sxx * syy) if syy > 0 else 0.0)


def ann_vol(closes, n, ppy):
    """Annualized volatility over the last n daily log returns (sample sd), or None without n returns."""
    if len(closes) < n + 1:
        return None
    r = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    m = sum(r) / n
    var = sum((v - m) ** 2 for v in r) / (n - 1)
    return math.sqrt(var) * math.sqrt(ppy)


def avg_volume(vols, n):
    """Mean volume over the last n sessions (a 0 counts - no trades; a missing one doesn't); None when the window has
    no volume to speak of (fewer than half its sessions > 0: an index level, a rate)."""
    w = [v for v in vols[-n:] if v is not None]
    if len(vols) < n or len(w) < 0.8 * n or sum(1 for v in w if v > 0) < 0.5 * n:
        return None
    return sum(w) / len(w)


def common(ds, cs, bmap):
    """Sessions both the ticker and the benchmark have -> (dates, ticker closes, benchmark closes)."""
    d2, c2, b2 = [], [], []
    for d, c in zip(ds, cs):
        b = bmap.get(d)
        if b:
            d2.append(d)
            c2.append(c)
            b2.append(b)
    return d2, c2, b2


def beta_daily(cd, cc, cb, n=B1_SESSIONS):
    """The last n daily returns on common sessions -> (beta, n, r2, first session of the span) or None."""
    if len(cd) < 3:
        return None
    k = max(1, len(cd) - n)
    rs = [cc[i] / cc[i - 1] - 1 for i in range(k, len(cd))]
    rm = [cb[i] / cb[i - 1] - 1 for i in range(k, len(cd))]
    o = ols(rm, rs)
    return None if o is None else (o[0], len(rs), o[1], cd[k - 1])


def beta_monthly(cd, cc, cb, through, n=B5_MONTHS):
    """60 completed monthly returns: the last common session of each month up to `through` (YYYY-MM)."""
    last = {}
    for i, d in enumerate(cd):
        if d[:7] <= through:
            last[d[:7]] = i
    idx = [last[m] for m in sorted(last)][-(n + 1):]
    if len(idx) < 3:
        return None
    rs = [cc[idx[j]] / cc[idx[j - 1]] - 1 for j in range(1, len(idx))]
    rm = [cb[idx[j]] / cb[idx[j - 1]] - 1 for j in range(1, len(idx))]
    o = ols(rm, rs)
    return None if o is None else (o[0], len(rs), o[1], cd[idx[0]])


def last_jump(ds, cs, since_iso):
    """The most recent one-session move of x2+ or -50 %+ dated after since_iso -> (date, ratio) or None."""
    for i in range(len(cs) - 1, 0, -1):
        if ds[i] <= since_iso:
            break
        if abs(math.log(cs[i] / cs[i - 1])) >= JUMP - 1e-12:
            return ds[i], cs[i] / cs[i - 1]
    return None


def through_month(today):
    """The last COMPLETED month (YYYY-MM) as of today's date: the month before today's."""
    return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def stats(ds, cs, vs, bmap, today, bench_last, m=None):
    """Everything for one ticker. ds/cs/vs from series(); bmap {date: benchmark close}; today = ET date of the run;
    bench_last = the benchmark's last session (the stale test); m = its master row as masters() reads it (Yahoo's
    figures to check against; m["us"] = a US-listed USD equity / ETF, which a computed beta may fill)."""
    m = m or {}
    n = len(ds)
    if n < 2:
        return None
    D, px = ds[-1], cs[-1]
    Dd = _d(D)
    out = {"d": D, "px": rnd(px), "n": n, "f": ds[0]}
    q = []
    stale = bool(bench_last) and (_d(bench_last) - Dd).days >= STALE_DAYS
    if stale:
        q.append("stale")
    # our last close against the book's own close for the same session: a different share basis (a split the
    # history has not caught up with) shows here first - nothing is filled from a history that disagrees
    basis_ok = True
    if m.get("date") == D and m.get("price"):
        x = px / m["price"]
        if abs(x - 1) > PX_TOL:
            basis_ok = False
            q.append(f"px:x{x:.3f}")
    # 52 weeks: the sessions dated after the same day a year earlier
    cut = years_back(Dd).isoformat()
    i0 = bisect_right(ds, cut)
    win = cs[i0:]
    hi_i = max(range(i0, n), key=lambda i: (cs[i], i))
    lo_i = min(range(i0, n), key=lambda i: (cs[i], -i))
    out["h52"] = [rnd(cs[hi_i]), ds[hi_i]]
    out["l52"] = [rnd(cs[lo_i]), ds[lo_i]]
    if i0 == 0:
        out["since"] = ds[0]
        q.append("short")
    out["fh"] = pct(px / cs[hi_i] - 1)
    out["fl"] = pct(px / cs[lo_i] - 1)
    a_i = max(range(n), key=lambda i: (cs[i], i))
    out["ath"] = [rnd(cs[a_i]), ds[a_i]]
    out["fa"] = pct(px / cs[a_i] - 1)
    for k in (50, 200):
        if n >= k:
            ma = sum(cs[-k:]) / k
            out[f"m{k}"] = rnd(ma)
            out[f"v{k}"] = pct(px / ma - 1)
    # volume
    a20, a63 = avg_volume(vs, 20), avg_volume(vs, 63)
    if a20 is not None:
        out["a20"] = int(round(a20))
        dv = [c * v for c, v in zip(cs[-20:], vs[-20:]) if v is not None]
        if dv:
            out["dv20"] = int(rnd(sum(dv) / len(dv), 4))
    if a63 is not None:
        out["a63"] = int(round(a63))
    prior = avg_volume(vs[:-1], 63)
    if prior and vs[-1] is not None:
        out["rv"] = round(vs[-1] / prior, 2)
    # volatility, annualized by the observations a year actually holds (~252 a stock, ~260 a currency, 365 crypto)
    if i0 > 0:
        ppy = n - i0
    else:                                      # younger than a year: a 7-day market or not
        ppy = 365 if sum(1 for d in ds[-60:] if _d(d).weekday() >= 5) >= 0.2 * min(60, n) else 252
    ppy = max(50, min(366, ppy))
    if abs(ppy - 252) > 5:
        out["ppy"] = ppy
    for k in (20, 60, 252):
        v = ann_vol(cs, k, ppy)
        if v is not None:
            out[f"s{k}"] = pct(v, 1)
    # beta vs the S&P 500
    cd, cc, cb = common(ds, cs, bmap)
    b1 = beta_daily(cd, cc, cb) if len(cd) > B1_MIN // 2 else None
    b5 = beta_monthly(cd, cc, cb, through_month(today))
    if b1:
        out["b1"] = [round(b1[0], 3), b1[1], round(b1[2], 3)]
    if b5 and b5[1] >= B5_MIN:
        out["b5"] = [round(b5[0], 3), b5[1], round(b5[2], 3)]
    # returns (price): the close on or before the same day k months earlier
    r = {}
    for key, k in (("1m", 1), ("3m", 3), ("6m", 6), ("1y", 12), ("3y", 36), ("5y", 60)):
        j = at_or_before(ds, months_back(Dd, k).isoformat())
        if j >= 0 and (Dd - _d(ds[j])).days <= k * 31 + 31:
            tot = px / cs[j] - 1
            yrs = k // 12
            r[key] = pct((1 + tot) ** (1 / yrs) - 1 if yrs >= 3 else tot)
    j = at_or_before(ds, f"{Dd.year - 1}-12-31")
    if j >= 0 and ds[j][:4] == str(Dd.year - 1):
        r["ytd"] = pct(px / cs[j] - 1)
    if r:
        out["r"] = r
    # the largest fall inside the 52 weeks
    peak_i, dd, dd_p, dd_t = i0, 0.0, None, None
    for i in range(i0, n):
        if cs[i] > cs[peak_i]:
            peak_i = i
        f = cs[i] / cs[peak_i] - 1
        if f < dd:
            dd, dd_p, dd_t = f, ds[peak_i], ds[i]
    out["dd"] = [pct(dd), dd_p, dd_t]
    # a one-session x2 / -50 % inside the last 5 years
    jump = last_jump(ds, cs, years_back(Dd, 5).isoformat())
    if jump:
        q.append(f"jump:{jump[0]}:x{jump[1]:.2f}")
    if q:
        out["q"] = q
    # what may fill a blank field
    clean = not stale and basis_ok
    if clean and len(win) >= 20 and not (jump and jump[0] > cut):
        out["u52"] = 1
    if m.get("us") and clean:                    # a jump inside a beta's span (its first session on) rules that beta out
        if b5 and b5[1] >= B5_MIN and not (jump and jump[0] > b5[3]):
            out["ub"] = [round(b5[0], 3), "m"]
        elif b1 and b1[1] >= B1_MIN and not (jump and jump[0] > b1[3]):
            out["ub"] = [round(b1[0], 3), "d"]
    # Yahoo's 52-week range (intraday) against our closes. A close can't sit outside the same window's intraday
    # range, so ours outside Yahoo's (tested on the inner 50 weeks - no window-edge effects) means Yahoo's is wrong
    # (not re-based after a split, or stale): x52 = show ours instead. Yahoo's extreme 25 %+ beyond every close is
    # either a split Yahoo's range never re-based (the gap is a split factor: x2, x4, x1.5 ... -> x52 too) or a real
    # spike in a thin name (w52: shown side by side, not replaced).
    yh, yl = m.get("h52"), m.get("l52")
    if yh and yl and out.get("u52") and i0 > 0:
        j0 = bisect_right(ds, (Dd - timedelta(days=350)).isoformat())
        h50, l50 = max(cs[j0:]), min(cs[j0:])
        hx, lx = yh / cs[hi_i], cs[lo_i] / yl
        if h50 > yh * (1 + R52_TOL) or l50 < yl * (1 - R52_TOL):
            out["x52"] = 1
        elif (hx > R52_WIDE and split_factor(hx)) or (lx > R52_WIDE and split_factor(lx)):
            out["x52"] = 2
        elif hx > R52_WIDE or lx > R52_WIDE:
            out["w52"] = 1
    return out


SPLITS = (1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100, 200)


def split_factor(r):
    """Yahoo's intraday extreme / our closing one = k (a split factor) x a little (intraday beyond the close) ->
    the factor, or None. Band k x 0.97 .. k x 1.10."""
    for f in SPLITS:
        if 0.97 <= r / f <= 1.10:
            return f
    return None


# ------------------------------------------------------------------------------------------------- the universe ---
def masters(read=read_book_file):
    """{ticker: {book, cls, ccy, beta, adv, h52, l52}} - the first book listing a ticker wins."""
    out = {}
    for b in BOOKS:
        d = read(b, "data/master.json")
        rows = d if isinstance(d, list) else ((d or {}).get("rows") or []) if isinstance(d, dict) else []
        if isinstance(rows, dict):
            rows = list(rows.values())
        for r in rows:
            if not isinstance(r, dict):
                continue
            t = str(r.get("ticker") or "").upper().strip()
            if not t or t in out:
                continue
            out[t] = {"book": b, "cls": str(r.get("asset_class") or "").lower(), "ccy": str(r.get("currency") or "USD").upper(),
                      "beta": _num(r.get("beta")), "adv": _num(r.get("volumeavg") or r.get("avgVolume")),
                      "h52": _num(r.get("high52")), "l52": _num(r.get("low52")), "price": _num(r.get("price")),
                      "date": str(r.get("date") or "")[:10] or None, "country": str(r.get("country") or "").strip()}
            out[t]["us"] = us_equity(t, out[t])
    return out


def us_equity(t, m):
    """US-listed, quoted in USD, an equity or ETF row - the rows a computed beta may fill."""
    return (m.get("cls") == "equity" and m.get("ccy") in ("USD", "") and "." not in t and not t.startswith("^")
            and "=" not in t and not t.endswith("-USD"))


def load_bench(hist=history):
    for sym, book in (BENCH, BENCH_FALLBACK):
        ds, cs, _ = series(hist(book, sym))
        if len(ds) > 300:
            return sym, ds, cs
    return None, [], []


# --------------------------------------------------------------------------------------------------------- checks ---
def checks(tickers, mast):
    """Ours against Yahoo's where Yahoo reports the same thing. Beta: US issuers only - Yahoo's beta for a foreign
    issuer is not taken against the S&P 500 (on 11-21: US median gap 0.009, 94 % within 0.05; foreign 0.077)."""
    beta_n = {"agree": 0, "near": 0, "check": 0, "foreign": 0}
    adv_n = {"agree": 0, "check": 0}
    r52_n = {"inside": 0, "outside": 0, "split": 0, "wide": 0}
    px_n = {"agree": 0, "off": 0}
    bl, al, rl, pl = [], [], [], []
    for t, s in tickers.items():
        m = mast.get(t) or {}
        qs = " ".join(s.get("q") or [])
        if m.get("date") == s.get("d") and m.get("price"):
            if "px:" in qs:
                px_n["off"] += 1
                pl.append({"t": t, "ours": s["px"], "book": m["price"], "d": s["d"]})
            else:
                px_n["agree"] += 1
        b5, yb = s.get("b5"), m.get("beta")
        if b5 and yb is not None and b5[1] >= 36 and "jump" not in qs:
            if m.get("country") not in ("", "United States"):
                beta_n["foreign"] += 1
            else:
                g = b5[0] - yb
                band = "agree" if abs(g) <= BETA_BANDS[0] else "near" if abs(g) <= BETA_BANDS[1] else "check"
                beta_n[band] += 1
                if band == "check":
                    bl.append({"t": t, "ours": b5[0], "yahoo": round(yb, 3), "gap": round(g, 3), "r2": b5[2]})
        a63, ya = s.get("a63"), m.get("adv")
        if a63 and ya and ya > 0 and "stale" not in (s.get("q") or []):
            x = a63 / ya
            if abs(x - 1) <= ADV_BAND:
                adv_n["agree"] += 1
            else:
                adv_n["check"] += 1
                al.append({"t": t, "ours": a63, "yahoo": int(ya), "x": round(x, 2)})
        yh, yl = m.get("h52"), m.get("l52")
        if yh and yl and s.get("u52") and "short" not in qs:
            e = {"t": t, "ours": [s["l52"][0], s["h52"][0]], "yahoo": [yl, yh]}
            if s.get("x52") == 1:
                r52_n["outside"] += 1          # a CLOSE outside Yahoo's INTRADAY range: Yahoo's is wrong - ours is shown
                rl.append(dict(e, why="a close outside Yahoo's intraday range - ours shown"))
            elif s.get("x52") == 2:
                r52_n["split"] += 1            # Yahoo's extreme a split factor beyond every close: never re-based
                rl.append(dict(e, why="Yahoo's extreme a split factor beyond every close - ours shown"))
            elif s.get("w52"):
                r52_n["wide"] += 1
                rl.append(dict(e, why="Yahoo's extreme 25 %+ beyond every close"))
            else:
                r52_n["inside"] += 1
    bl.sort(key=lambda e: -abs(e["gap"]))
    al.sort(key=lambda e: -abs(math.log(e["x"])) if e["x"] > 0 else 0)
    rl.sort(key=lambda e: (e["why"][:3], e["t"]))
    pl.sort(key=lambda e: -abs(math.log(e["ours"] / e["book"])) if e["book"] else 0)
    return {"beta": {"bands": list(BETA_BANDS), **beta_n, "list": bl[:REVIEW]},
            "adv": {"band": ADV_BAND, **adv_n, "list": al[:REVIEW]},
            "r52": {"tol": R52_TOL, "wideX": R52_WIDE, **r52_n, "list": rl[:REVIEW]},
            "px": {"tol": PX_TOL, **px_n, "list": pl[:REVIEW]}}


def same(a, b):
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return {k: v for k, v in a.items() if k != "at"} == {k: v for k, v in b.items() if k != "at"}


def build(mast, bench, hist=history, today=None, workers=8):
    """mast from masters(); bench = (sym, dates, closes) -> the document."""
    bsym, bds, bcs = bench
    bmap = dict(zip(bds, bcs))
    bench_last = bds[-1] if bds else None
    today = today or now_utc().astimezone(ET).date()
    order = list(mast)

    def one(t):
        m = mast[t]
        books = [m["book"]] + [b for b in BOOKS if b != m["book"]]
        for b in books:
            ds, cs, vs = series(hist(b, t))
            if len(ds) >= 2:
                try:
                    s = stats(ds, cs, vs, bmap, today, bench_last, m)
                except Exception as e:      # noqa: BLE001 - one bad file must not stop the run
                    log(f"  ✗ {t}: {e}")
                    return t, None, b
                return t, s, b
        return t, None, None

    tickers, missing = {}, []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, s, b in ex.map(one, order):
            if s:
                tickers[t] = s
            else:
                missing.append(t)
    qn = lambda f: sum(1 for s in tickers.values() if any(str(x).startswith(f) for x in (s.get("q") or [])))
    counts = {"tickers": len(mast), "computed": len(tickers), "noHistory": len(missing),
              "stale": qn("stale"), "short": qn("short"), "jump": qn("jump"),
              "u52": sum(1 for s in tickers.values() if s.get("u52")),
              "ubMonthly": sum(1 for s in tickers.values() if (s.get("ub") or [None, None])[1] == "m"),
              "ubDaily": sum(1 for s in tickers.values() if (s.get("ub") or [None, None])[1] == "d"),
              "b5": sum(1 for s in tickers.values() if s.get("b5")), "b1": sum(1 for s in tickers.values() if s.get("b1")),
              "pxOff": qn("px:"), "x52": sum(1 for s in tickers.values() if s.get("x52")),
              "fills": {"high52": sum(1 for t, s in tickers.items() if s.get("u52") and not mast[t].get("h52")),
                        "beta": sum(1 for t, s in tickers.items() if s.get("ub") and mast[t].get("beta") is None),
                        "avgVolume": sum(1 for t, s in tickers.items() if s.get("a63") and not mast[t].get("adv"))}}
    return {
        "build": BUILD, "at": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "date": bench_last,
        "bench": bsym, "through": through_month(today),
        "method": ("closes split-adjusted, not dividend-adjusted (price returns); 52 weeks = sessions after the same day a "
                   "year earlier; volatility = sample sd of daily log returns x sqrt(252) (365 for a 7-day market); beta = "
                   "OLS slope vs ^GSPC: b5 60 completed monthly returns (Yahoo's method), b1 252 daily returns on common sessions"),
        "keys": KEYS, "counts": counts, "checks": checks(tickers, mast),
        "noHistory": sorted(missing)[:REVIEW], "tickers": tickers,
    }


def main():
    t0 = time.time()
    mast = masters()
    if not mast:
        log("derived: no master rows read - nothing written")
        return 0
    bench = load_bench()
    if not bench[0]:
        log("derived: no benchmark history (^GSPC / SPY) - nothing written")
        return 0
    doc = build(mast, bench)
    prior = _read(OUT)
    c = doc["counts"]
    ch = doc["checks"]
    log(f"derived {BUILD}: {c['computed']}/{c['tickers']} tickers from our histories (bench {doc['bench']} {doc['date']}, "
        f"months through {doc['through']}) · fills: 52-week {c['fills']['high52']}, beta {c['fills']['beta']}, avg volume "
        f"{c['fills']['avgVolume']}; Yahoo's 52-week range replaced (contradicts our closes) {c['x52']} · flags: stale "
        f"{c['stale']}, short {c['short']}, jump {c['jump']}, last close off the book's {c['pxOff']} · beta vs Yahoo (US "
        f"issuers): {ch['beta']['agree']} agree / {ch['beta']['near']} near / {ch['beta']['check']} check ({ch['beta']['foreign']} "
        f"foreign not compared) · avg volume: {ch['adv']['agree']} agree / {ch['adv']['check']} check · last close vs the "
        f"book's: {ch['px']['agree']} agree / {ch['px']['off']} off · 52-week vs Yahoo intraday: {ch['r52']['inside']} inside / "
        f"{ch['r52']['outside']} outside / {ch['r52']['split']} split / {ch['r52']['wide']} wide · {time.time() - t0:.1f}s")
    if same(prior, doc):
        log("derived: unchanged since the last run - not rewritten")
        return 0
    _write(OUT, doc)
    try:
        shown = OUT.relative_to(ROOT)
    except ValueError:
        shown = OUT
    log(f"derived: wrote {shown} ({OUT.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
