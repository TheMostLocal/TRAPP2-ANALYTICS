# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/factors.py
#  z206 - POSITIONING FROM PRICES: the factor monitor. Owner 11-24 1100 ET: "It seems like the last few market days /
#  week have been a major sell off and yesterday was covering shorts for major hedges. Just an inference but make sure
#  things like this are being noticed."
#  A sell-off shows in the index; WHO was forced to trade shows in the cross-section. Desks read it from the daily
#  returns of a few long-short baskets (the prime brokers' factor reports); this computes the same from the books' own
#  histories. No positioning data is bought or fetched, so every label is a PRICE SIGNATURE of covering or de-grossing,
#  not a reported flow:
#    si    the most-shorted 50 (short % of float, Yahoo's latest report) minus the average stock - a squeeze when it jumps
#    mom   12-1 month momentum: the top fifth (last year's winners, the latest month skipped) minus the bottom fifth - a
#          sharp negative day is a momentum unwind: crowded longs sold and crowded shorts (the losers) bought back
#    rev   one-week reversal: the week's bottom fifth minus its top fifth - the week's losers bouncing
#    beta  the highest-beta fifth minus the lowest (252-session daily beta vs the S&P 500, re-formed every 21 sessions)
#          - risk on / risk off
#    ew    the average stock (equal-weighted) - the base the spreads are measured from
#  UNIVERSE, re-formed each session on what was known at the PRIOR close (no look-ahead in the baskets): US-listed
#  common stocks in the four equity books (no funds), close >= $5, 20-session dollar volume >= $5M, market cap >= $1B
#  (the master's, i.e. today's - the year carries a little survivorship), 260+ sessions of history. Returns are close to
#  close, price only; a one-session move beyond +100 % / -50 % is a split the history missed or a bad print and is left
#  out that day. Each factor's daily return over the last 252 sessions gives its mean and standard deviation, and a day's
#  z-score. The short basket uses TODAY's report for the whole year (FINRA reports twice a month; no history of it is
#  kept here): its series says how today's crowded shorts traded, not a point-in-time index.
#  THE DAY'S LABEL (character()): an up day (the S&P 500 1 sd+ of its 20-day moves) with the shorts squeezed (si +2 sd)
#  or momentum unwound (mom -2 sd) = "short-covering rally"; a down day with momentum unwound and the shorts up =
#  "de-grossing"; and so on - each with the numbers behind it. pipeline/trends.py adds the index tells it holds (gap
#  share, VIX, credit, cap- vs equal-weighted) and prices the same baskets from the live tapes every 30 minutes.
#  OUT: data/derived/factors.json - the series, the stats, the last session's read, the baskets as formed at the last
#  close (for the next session's live read), the crowded shorts (10 %+ of float, for the bot), every |z| >= 2 day.
#  WHEN: .github/workflows/analytics.yml, after market statistics (03:00 UTC after a trading day + 14:30 UTC weekdays).
#  Env: FACTORS_NOW=ISO (tests) · DERIVED_BOOKS / DERIVED_OFFLINE as pipeline/derived.py
#
#  z207 - THE OFFICIAL SHORT INTEREST. Yahoo's "short % of float" (z206) turned out unreliable against FINRA's own
#  numbers: WHR 0.1 % on Yahoo vs 32 % of its shares on FINRA's 11-13 report; CHTR 45 % vs 8.8 %; KMB 18 % vs 4.4 %;
#  FIG 17 % vs 5.6 %. The most-shorted 50 now rank on FINRA's consolidated short interest (data/short/index.json,
#  pipeline/fetch_short.py) as a share of the shares outstanding this system trusts (data/sec/shares.json - the SEC's
#  cover count where it agrees), with FINRA's days to cover beside it; Yahoo's figure only where FINRA has nothing (and
#  only if FINRA covers under 80 % of the universe does the basket rank on Yahoo). New blocks:
#    reported     what FINRA's latest settlement says shorts DID (lagged ~2 weeks): the basket's shares short vs the
#                 settlement before, the universe's (value-weighted at today's prices), names adding / covering 10 %+,
#                 the largest moves among 5 %+-shorted names
#    shortVolume  Reg SHO short sales as a share of volume on FINRA's latest day: the universe vs its 20-day average,
#                 the basket's average z, how many names are 2 sd+ above their own 20 days
#    crowded      {ticker: {si (% of shares), dtc (days to cover), chg (% change in shares short), settle, src}} for names
#                 at 8 %+ of shares, or 7+ days to cover with 5 %+ (FINRA), or 10 %+ of float where only Yahoo has one -
#                 the candidates; each reader applies its own bar (the quant book: 15 % / 10 days, 8 % / 7 while covering)
#  A settlement FINRA's pull left part-way (fetch_short.py before z207 stopped at 60,000 rows) is flagged "partial" and the
#  basket ranks on each name's newest settlement; a stock split between settlements leaves that name's change out.
# ============================================================
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import derived as dv  # noqa: E402

BUILD = "z207"
OUT = dv.DATA / "derived" / "factors.json"
EQUITY_BOOKS = ("TRAPP2", "TRAPP2-2", "TRAPP2-3", "TRAPP2-4")
BENCHES = (("^GSPC", "TRAPP2-1"), ("SPY", "TRAPP2-1"), ("SPY", "TRAPP2"))   # the macro book first; SPY in TRAPP2 if it's out
MIN_PX, MIN_DV, MIN_CAP, MIN_SESSIONS = 5.0, 5e6, 1e9, 260
WINDOW = 252                     # sessions of factor history
MOM_LONG, MOM_SKIP = 252, 21     # 12-1: from 252 sessions back to 21 sessions back
REV_DAYS = 5
BETA_DAYS, BETA_EVERY, BETA_MIN = 252, 21, 200
SI_TOP, SI_CROWDED = 50, 0.10
SI_LIST_PCT = 0.08                        # z207: the crowded list - 8 %+ of shares outstanding short (FINRA; the bot's and
                                          # the quant book's bar while shorts are being covered) ...
SI_LIST_DTC, SI_LIST_DTC_PCT = 7.0, 0.05  # ... or 7+ days to cover with 5 %+ short
SETTLE_WHOLE = 0.8                        # the newest settlement must cover 80 %+ of the names FINRA reports for a whole read
SI_MIN_COVER = 0.8                        # FINRA must cover 80 %+ of the universe for the basket to rank on it
QUINTILE = 0.2
MAX_UP, MAX_DOWN = 1.0, -0.5     # beyond: a missed split / bad print, not a return
DV_DAYS, DV_MIN_OBS = 20, 15
Z_NOTE, Z_EXTREME = 2.0, 3.0
UP_SIGMA = 1.0                   # an "up" / "down" day for the label: the S&P 1 sd+ of its 20-day moves
FACTORS = ("si", "mom", "rev", "beta")
LABEL = {"si": "the most-shorted 50 vs the average stock", "mom": "momentum (12-1 winners minus losers)",
         "rev": "the week's losers minus its winners", "beta": "high beta minus low beta"}
EVENTS_KEEP = 80
TAPE_PATCH_DAYS = 5              # a history row without its close in the last 5 sessions: the book's day tape has it


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now_utc():
    s = os.environ.get("FACTORS_NOW")
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else datetime.now(timezone.utc)


def _num(v):
    return dv._num(v)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ------------------------------------------------------------------------------------------------------ inputs ---
def universe_rows(read=dv.read_book_file):
    """{ticker: {book, si, cap}} - US-listed common stocks in the equity books (the first book listing one wins); funds
    (isetf, or an expense ratio - only funds carry one) are left out."""
    out = {}
    for b in EQUITY_BOOKS:
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
            m = {"cls": str(r.get("asset_class") or "").lower(), "ccy": str(r.get("currency") or "USD").upper()}
            if not dv.us_equity(t, m):
                continue
            if str(r.get("isetf") or "").strip().lower() == "true" or str(r.get("expenseRatio") or "").strip():
                continue
            si = _num(r.get("shortPctFloat"))
            out[t] = {"book": b, "si": si if si is not None and 0 < si <= 1.0 else None,
                      "siRaw": si, "cap": _num(r.get("marketcap") or r.get("marketCap"))}
    return out


def load_short(read=None):
    """z207: FINRA's latest short interest and Reg SHO short volume (data/short/index.json) with the share count this
    system trusts (data/sec/shares.json "s") -> ({t: {pct, dtc, chg, settle, shares, prev}}, {t: {r1, r20, z, date}})."""
    read = read or (lambda rel: dv._read(dv.DATA / rel))
    doc, sh = read("short/index.json") or {}, (read("sec/shares.json") or {}).get("tickers") or {}
    out, vol = {}, {}
    for t, e in ((doc.get("interest") or {}) if isinstance(doc, dict) else {}).items():
        n = _num((e or {}).get("shares"))
        so = _num((sh.get(t) or {}).get("s"))
        if n is None or n < 0 or not so:
            continue
        pct = n / so
        if pct > 1.0:                                     # more short than outstanding: a class / unit mix-up, not a reading
            continue
        out[t] = {"pct": pct, "dtc": _num(e.get("daysToCover")), "chg": None if e.get("split") else _num(e.get("changePct")),
                  "settle": str(e.get("settlementDate") or "")[:10], "shares": n, "prev": None if e.get("split") else _num(e.get("prevShares"))}
    for t, e in ((doc.get("volume") or {}) if isinstance(doc, dict) else {}).items():
        if isinstance(e, dict) and _num(e.get("ratio1d")) is not None:
            vol[t] = {"r1": _num(e.get("ratio1d")), "r20": _num(e.get("ratio20d")), "z": _num(e.get("z")), "date": e.get("date")}
    return out, vol


def reported_block(si_basket, official, prices, universe):
    """What FINRA's latest settlement says shorts did (vs the settlement before): the basket's shares short, the
    universe's value at today's prices, names adding / covering 10 %+, and the largest moves among 5 %+-shorted names."""
    latest = max((v["settle"] for t, v in official.items() if t in universe and v.get("settle")), default=None)
    if not latest:
        return None
    reported = sum(1 for t in official if t in universe)
    on_latest = sum(1 for t, v in official.items() if t in universe and v.get("settle") == latest)
    cur = {t: v for t, v in official.items() if t in universe and v.get("settle") == latest and v.get("prev")}

    def agg(names, weight=None):
        a = b = 0.0
        for t in names:
            v = cur.get(t)
            if not v:
                continue
            w = (weight or {}).get(t, 1.0)
            a += v["shares"] * w
            b += v["prev"] * w
        return round((a / b - 1) * 100, 2) if b > 0 else None
    dtcs = [cur[t]["dtc"] for t in si_basket if t in cur and cur[t].get("dtc") is not None]
    chgs = [cur[t]["chg"] for t in si_basket if t in cur and cur[t].get("chg") is not None]
    px = {t: prices.get(t) or 0 for t in cur}               # dollars short at today's price: a change in shares, not in price
    big = sorted(((v["chg"], t) for t, v in cur.items() if v.get("chg") is not None and v["pct"] >= 0.05), key=lambda x: x[0])
    return {"settle": latest, "names": len(cur), "partial": on_latest < SETTLE_WHOLE * reported,
            "cover": round(on_latest / reported, 3) if reported else None,
            "basket": {"chg": agg(si_basket, px), "median": round(statistics.median(chgs), 1) if chgs else None,
                       "dtc": round(statistics.median(dtcs), 2) if dtcs else None, "n": sum(1 for t in si_basket if t in cur)},
            "universe": {"chg": agg(cur, px),
                         "adding": sum(1 for v in cur.values() if (v.get("chg") or 0) >= 10),
                         "covering": sum(1 for v in cur.values() if (v.get("chg") or 0) <= -10)},
            "covered": [{"t": t, "chg": c, "pct": round(cur[t]["pct"] * 100, 1)} for c, t in big[:5] if c < 0],
            "added": [{"t": t, "chg": c, "pct": round(cur[t]["pct"] * 100, 1)} for c, t in big[::-1][:5] if c > 0]}


def short_volume_block(vol, si_basket, universe):
    """Reg SHO short sales as a share of volume on FINRA's latest day, for the universe and the basket."""
    days = [v.get("date") for t, v in vol.items() if t in universe and v.get("date")]
    if not days:
        return None
    d = max(days)
    cur = {t: v for t, v in vol.items() if t in universe and v.get("date") == d}
    r1 = [v["r1"] for v in cur.values() if v.get("r1") is not None]
    r20 = [v["r20"] for v in cur.values() if v.get("r20") is not None]
    bz = [cur[t]["z"] for t in si_basket if t in cur and cur[t].get("z") is not None]
    return {"date": d, "names": len(cur), "ratio": round(statistics.median(r1), 1) if r1 else None,
            "ratio20": round(statistics.median(r20), 1) if r20 else None,
            "basketZ": round(statistics.fmean(bz), 2) if bz else None,
            "high": sum(1 for v in cur.values() if (v.get("z") or 0) >= 2)}


def load_bench(hist=dv.history):
    for sym, book in BENCHES:
        ds, cs, _ = dv.series(hist(book, sym))
        if len(ds) > MOM_LONG + WINDOW:
            return sym, ds, cs
    return None, [], []


def load_panel(rows, dates, hist=dv.history, workers=8):
    """{t: (closes, volumes)} aligned to dates (None where the name has no session); names with < MIN_SESSIONS rows
    in their whole history are left out."""
    pos = {d: i for i, d in enumerate(dates)}

    def one(t):
        try:
            ds, cs, vs = dv.series(hist(rows[t]["book"], t))
        except Exception as e:                       # noqa: BLE001 - one bad file must not stop the run
            log(f"  ✗ {t}: {e}")
            return t, None
        if len(ds) < MIN_SESSIONS:
            return t, None
        c, v = [None] * len(dates), [None] * len(dates)
        for d, x, y in zip(ds, cs, vs):
            k = pos.get(d)
            if k is not None:
                c[k], v[k] = x, y
        return t, (c, v)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return {t: p for t, p in ex.map(one, sorted(rows)) if p}


def patch_from_tapes(data, rows, dates, read=dv.read_book_file, days=TAPE_PATCH_DAYS):
    """A recent session with no close in the history (on 11-24 TRAPP2-4's 11-23 rows had a volume and no close for 980
    names): the official close from the book's own day tape (data/intraday/<date>.json "close" - Yahoo's daily bar,
    fetched at or after 1600 ET). -> {date: names patched}."""
    cache, out = {}, {}
    for k in range(max(0, len(dates) - days), len(dates)):
        d = dates[k]
        for t, (c, _v) in data.items():
            if c[k] is not None:
                continue
            b = rows[t]["book"]
            if (b, d) not in cache:
                tp = read(b, f"data/intraday/{d}.json")
                cache[(b, d)] = (tp or {}).get("close") or {} if isinstance(tp, dict) else {}
            x = _num(cache[(b, d)].get(t))
            if x and x > 0:
                c[k] = x
                out[d] = out.get(d, 0) + 1
    return out


# --------------------------------------------------------------------------------------------------- the panel ---
class Panel:
    """Closes / returns / dollar volume on a common session axis."""

    def __init__(self, dates, bench_closes, data, rows):
        self.dates = dates
        self.n = len(dates)
        self.names = sorted(data)
        self.c = {t: data[t][0] for t in self.names}
        self.rows = rows
        self.rb = [None] + [(bench_closes[k] / bench_closes[k - 1] - 1) if bench_closes[k - 1] else None for k in range(1, self.n)]
        self.r = {}
        self.dv = {}
        for t in self.names:
            c, v = data[t]
            rr = [None] * self.n
            for k in range(1, self.n):
                if c[k] and c[k - 1]:
                    x = c[k] / c[k - 1] - 1
                    rr[k] = x if MAX_DOWN < x < MAX_UP else None
            self.r[t] = rr
            dd = [None] * self.n
            win = []
            for k in range(self.n):
                win.append(c[k] * v[k] if c[k] and v[k] else None)
                if len(win) > DV_DAYS:
                    win.pop(0)
                obs = [x for x in win if x is not None]
                dd[k] = sum(obs) / len(obs) if len(obs) >= DV_MIN_OBS else None
            self.dv[t] = dd
        self._beta = {}

    def eligible(self, j):
        """Names tradable as of close j: close >= $5, 20-session dollar volume >= $5M, cap >= $1B."""
        out = []
        for t in self.names:
            c, d, cap = self.c[t][j], self.dv[t][j], self.rows[t].get("cap")
            if c and c >= MIN_PX and d and d >= MIN_DV and cap and cap >= MIN_CAP:
                out.append(t)
        return out

    def betas(self, f):
        """{t: beta} on the BETA_DAYS returns through close f (>= BETA_MIN pairs), memoized per formation close."""
        if f in self._beta:
            return self._beta[f]
        lo = max(1, f - BETA_DAYS + 1)
        out = {}
        for t in self.names:
            xs, ys = [], []
            rr = self.r[t]
            for k in range(lo, f + 1):
                a, b = rr[k], self.rb[k]
                if a is not None and b is not None:
                    xs.append(b)
                    ys.append(a)
            if len(xs) >= BETA_MIN:
                mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
                vx = sum((x - mx) ** 2 for x in xs)
                if vx > 0:
                    out[t] = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
        self._beta[f] = out
        return out

    def form(self, j, beta_at):
        """The baskets as known at close j -> {universe, mom: (bottom, top), rev: (bottom, top), beta: (bottom, top)}."""
        elig = self.eligible(j)

        def quint(scored):
            scored = sorted(scored)
            q = int(len(scored) * QUINTILE)
            if q < 5:
                return [], []
            return [t for _, t in scored[:q]], [t for _, t in scored[-q:]]
        mom, rev = [], []
        for t in elig:
            c = self.c[t]
            if j - MOM_LONG >= 0 and c[j - MOM_SKIP] and c[j - MOM_LONG]:
                mom.append((c[j - MOM_SKIP] / c[j - MOM_LONG] - 1, t))
            if j - REV_DAYS >= 0 and c[j] and c[j - REV_DAYS]:
                rev.append((c[j] / c[j - REV_DAYS] - 1, t))
        b = self.betas(beta_at)
        beta = [(b[t], t) for t in elig if t in b]
        return {"universe": elig, "mom": quint(mom), "rev": quint(rev), "beta": quint(beta)}


def spreads(ret, form, si_basket):
    """One session's factor returns (fractions) from {t: return} and the baskets formed at the prior close."""
    uni = [ret[t] for t in form["universe"] if ret.get(t) is not None]
    ew = mean(uni)
    out = {"ew": ew, "n": len(uni)}
    if ew is None:
        return out

    def leg(names):
        return mean([ret.get(t) for t in names])
    mb, mt = (leg(x) for x in form["mom"]) if form["mom"][0] else (None, None)
    rb, rt = (leg(x) for x in form["rev"]) if form["rev"][0] else (None, None)
    bb, bt = (leg(x) for x in form["beta"]) if form["beta"][0] else (None, None)
    sl = leg(si_basket) if si_basket else None
    out.update(mom=(mt - mb) if mt is not None and mb is not None else None, momL=mt, momS=mb,
               rev=(rb - rt) if rb is not None and rt is not None else None, revL=rb, revW=rt,
               beta=(bt - bb) if bt is not None and bb is not None else None,
               si=(sl - ew) if sl is not None else None, siL=sl)
    return out


# ------------------------------------------------------------------------------------------------- the label ---
def zscore(x, st):
    if x is None or not st or not st.get("sd"):
        return None
    return (x - st["mean"]) / st["sd"]


def character(z, legs=None, idx=None):
    """The day's positioning label from the factor z-scores (+ the index tells pipeline/trends.py holds).
    z {si, mom, rev, beta}; legs {ew, siL, momL, momS} in %; idx {chg, sigma, gap, vixChg, credit, cwEw, pctUp, defCyc}.
    -> {label, signals: [{k, text, z?}], up/down} - a price signature, never a reported flow."""
    z = z or {}
    legs = legs or {}
    idx = idx or {}
    sig = idx.get("sigma")
    up = sig is not None and sig >= UP_SIGMA
    down = sig is not None and sig <= -UP_SIGMA
    zs, zm, zr, zb = (z.get(k) for k in FACTORS)
    squeeze = zs is not None and zs >= Z_NOTE
    pressed = zs is not None and zs <= -Z_NOTE
    unwind = zm is not None and zm <= -Z_NOTE
    surge = zm is not None and zm >= Z_NOTE
    bounce = zr is not None and zr >= Z_NOTE
    risk_on = zb is not None and zb >= Z_NOTE
    risk_off = zb is not None and zb <= -Z_NOTE
    sig_ = []

    def pp(x):
        return f"{x:+.2f}%" if x is not None else "?"
    if zs is not None and (squeeze or pressed):
        sig_.append({"k": "si", "z": round(zs, 1), "text": f"the most-shorted 50 {pp(legs.get('siL'))} vs the average stock {pp(legs.get('ew'))} "
                     f"({zs:+.1f} sd of its daily spreads over a year) - " + ("shorts covered / squeezed" if squeeze else "shorts pressed")})
    if zm is not None and (unwind or surge):
        sig_.append({"k": "mom", "z": round(zm, 1), "text": f"momentum: last year's winners {pp(legs.get('momL'))}, its losers {pp(legs.get('momS'))} "
                     f"({zm:+.1f} sd) - " + ("an unwind: crowded longs sold, crowded shorts bought back" if unwind else "winners extended")})
    if zr is not None and (bounce or zr <= -Z_NOTE):
        sig_.append({"k": "rev", "z": round(zr, 1), "text": (f"the week's losers beat its winners by {pp(legs.get('rev'))} ({zr:+.1f} sd) - the week's "
                     "shorts covered / a reversal") if bounce else (f"the week's losers lagged its winners by {pp(legs.get('rev'))} ({zr:+.1f} sd) - "
                     "the losers sold again")})
    if zb is not None and (risk_on or risk_off):
        sig_.append({"k": "beta", "z": round(zb, 1), "text": f"high beta minus low beta {pp(legs.get('beta'))} ({zb:+.1f} sd) - risk " + ("on" if risk_on else "off")})
    hedges = up and (idx.get("gapShare") or 0) >= 0.7 and (idx.get("vixChg") is not None and idx["vixChg"] <= -10)
    if hedges:
        sig_.append({"k": "hedges", "text": f"{idx['gapShare'] * 100:.0f}% of the S&P 500's move came at the open (gap {pp(idx.get('gap'))}) and the VIX fell "
                     f"{idx['vixChg']:.1f}% - index hedges unwound overnight"})
    credit_lag = up and idx.get("credit") is not None and idx.get("creditImplied") is not None and idx["creditImplied"] > 0 and idx["credit"] < idx["creditImplied"] / 3
    if credit_lag:
        sig_.append({"k": "credit", "text": f"high-yield bonds (HYG) {pp(idx.get('credit'))} vs {pp(idx.get('creditImplied'))} their beta to the S&P 500 implies - credit didn't follow"})
    cw_ew = idx.get("cwEw")
    if cw_ew is not None and abs(cw_ew) >= 1.0:
        sig_.append({"k": "narrow", "text": f"cap-weighted minus equal-weighted {pp(cw_ew)} - the biggest names did "
                     + ("better" if cw_ew > 0 else "worse") + " than the average stock" + (f" ({idx['pctUp']:.0f}% of members up)" if idx.get("pctUp") is not None else "")})
    if idx.get("defCyc") is not None and abs(idx["defCyc"]) >= 2.0:
        sig_.append({"k": "defensives", "text": f"defensives (staples, utilities, health care) minus cyclicals {pp(idx['defCyc'])} - "
                     + ("a defensive tape" if idx["defCyc"] > 0 else "cyclicals led")})
    liquidated = zr is not None and zr <= -Z_NOTE
    if up:
        if squeeze or unwind or bounce:
            label = "short-covering rally"
        elif risk_on:
            label = "risk-on rally"
        elif (idx.get("pctUp") or 0) >= 70 and not credit_lag:
            label = "broad rally"
        elif cw_ew is not None and cw_ew >= 1.0:
            label = "mega-cap rally"
        else:
            label = "rally"
    elif down:
        if unwind and zs is not None and zs >= 1.0:
            label = "de-grossing"
        elif unwind:
            label = "momentum unwind"
        elif pressed:
            label = "shorts pressing"
        elif liquidated:
            label = "losers liquidated"
        elif risk_off:
            label = "risk-off selling"
        elif cw_ew is not None and cw_ew <= -1.0:
            label = "mega-cap selling"
        else:
            label = "selling"
    elif squeeze or unwind or surge or pressed or bounce or liquidated:
        label = "rotation under a quiet index"
    elif risk_off or risk_on:
        label = "risk-off rotation" if risk_off else "risk-on rotation"
    elif cw_ew is not None and abs(cw_ew) >= 1.0 and idx.get("chg") is not None and (cw_ew > 0) == (idx["chg"] > 0):
        label = "mega-cap buying" if cw_ew > 0 else "mega-cap selling"
    else:
        label = None
    return {"label": label, "up": up, "down": down, "signals": sig_,
            "flags": {k: v for k, v in (("squeeze", squeeze), ("unwind", unwind), ("surge", surge), ("pressed", pressed),
                                        ("bounce", bounce), ("liquidated", liquidated), ("riskOn", risk_on), ("riskOff", risk_off), ("hedges", hedges),
                                        ("creditLag", credit_lag)) if v}}


# ------------------------------------------------------------------------------------------------------ build ---
def build(rows, bench, data, today=None, official=None, vol=None):
    bsym, bds, bcs = bench
    keep = WINDOW + MOM_LONG + 2
    dates, bclose = bds[-keep:], bcs[-keep:]
    P = Panel(dates, bclose, {t: (p[0][-keep:], p[1][-keep:]) if len(p[0]) > keep else p for t, p in data.items()}, rows)
    n = P.n
    start = n - WINDOW
    if start < MOM_LONG + 1:
        return None
    last = n - 1
    # the crowded shorts: FINRA's latest report as a share of the shares outstanding (z207), among names tradable at
    # the last close - Yahoo's short % of float only if FINRA covers under 80 % of them
    official = official or {}
    elig_last = set(P.eligible(last))
    cover = sum(1 for t in elig_last if t in official) / len(elig_last) if elig_last else 0
    si_src = "finra" if cover >= SI_MIN_COVER else "yahoo"
    if si_src == "finra":
        si_ranked = sorted(((official[t]["pct"], t) for t in elig_last if t in official), reverse=True)
    else:
        si_ranked = sorted(((rows[t]["si"], t) for t in elig_last if rows[t].get("si") is not None), reverse=True)
    si_basket = [t for _, t in si_ranked[:SI_TOP]]
    beta_anchor = start - 1
    ser = {k: [] for k in ("dates", "ew", "si", "mom", "rev", "beta", "siL", "momL", "momS", "n")}
    for k in range(start, n):
        j = k - 1
        f = j - ((j - beta_anchor) % BETA_EVERY)
        form = P.form(j, f)
        ret = {t: P.r[t][k] for t in P.names}
        s = spreads(ret, form, si_basket)
        ser["dates"].append(dates[k])
        for key in ("ew", "si", "mom", "rev", "beta", "siL", "momL", "momS"):
            v = s.get(key)
            ser[key].append(round(v * 100, 4) if v is not None else None)
        ser["n"].append(s.get("n"))
    stats = {}
    for key in ("ew",) + FACTORS:
        xs = [x for x in ser[key] if x is not None]
        stats[key] = {"mean": round(statistics.fmean(xs), 5), "sd": round(statistics.stdev(xs), 5), "n": len(xs)} if len(xs) >= 60 else None
    # the last session against the 251 before it
    lastz, lastv = {}, {}
    for key in FACTORS:
        xs = [x for x in ser[key][:-1] if x is not None]
        st = {"mean": statistics.fmean(xs), "sd": statistics.stdev(xs)} if len(xs) >= 60 else None
        v = ser[key][-1]
        lastv[key] = v
        lastz[key] = round(zscore(v, st), 2) if zscore(v, st) is not None else None
    rbs = [x * 100 for x in P.rb[-21:-1] if x is not None]
    bchg = P.rb[-1] * 100 if P.rb[-1] is not None else None
    bsig = (bchg / statistics.stdev(rbs)) if bchg is not None and len(rbs) >= 15 and statistics.stdev(rbs) > 0 else None
    legs = {"ew": ser["ew"][-1], "siL": ser["siL"][-1], "momL": ser["momL"][-1], "momS": ser["momS"][-1],
            "rev": ser["rev"][-1], "beta": ser["beta"][-1]}
    ch = character(lastz, legs, {"chg": bchg, "sigma": bsig})
    # every |z| >= 2 day of the year (z against the whole year - descriptive)
    events = []
    for i, d in enumerate(ser["dates"]):
        for key in FACTORS:
            zz = zscore(ser[key][i], stats.get(key))
            if zz is not None and abs(zz) >= Z_NOTE:
                events.append({"date": d, "f": key, "ret": ser[key][i], "z": round(zz, 1)})
    events.sort(key=lambda e: (e["date"], -abs(e["z"])), reverse=True)
    # the baskets for the NEXT session: formed on the last close
    nf = last - ((last - beta_anchor) % BETA_EVERY)
    nxt = P.form(last, nf)
    crowded = {}
    for t in P.names:
        o = official.get(t)
        if o and (o["pct"] >= SI_LIST_PCT or ((o.get("dtc") or 0) >= SI_LIST_DTC and o["pct"] >= SI_LIST_DTC_PCT)):
            crowded[t] = {"si": round(o["pct"] * 100, 1), "dtc": o.get("dtc"), "chg": o.get("chg"), "settle": o.get("settle"), "src": "finra"}
        elif not o and rows[t].get("si") is not None and rows[t]["si"] >= SI_CROWDED:
            crowded[t] = {"si": round(rows[t]["si"] * 100, 1), "src": "yahoo"}
    last_px = {t: P.c[t][last] for t in P.names if P.c[t][last]}
    reported = reported_block(si_basket, official, last_px, set(elig_last)) if official else None
    short_vol = short_volume_block(vol or {}, si_basket, set(elig_last)) if vol else None
    odd = sorted(t for t in rows if rows[t].get("siRaw") is not None and rows[t]["siRaw"] > 1.0)
    doc = {
        "build": BUILD, "at": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "date": dates[-1], "bench": bsym,
        "method": ("daily close-to-close price returns, equal-weighted; baskets formed at the prior close: US-listed common "
                   "stocks in the four equity books, close >= $5, 20-session dollar volume >= $5M, market cap >= $1B (today's), "
                   "260+ sessions; mom = 12-1 month top minus bottom fifth; rev = the week's bottom fifth minus its top fifth; "
                   "beta = top minus bottom fifth of 252-session daily beta vs the benchmark, re-formed every 21 sessions; si = the "
                   "50 highest short interest as a share of shares outstanding (FINRA's latest settlement / the SEC count; Yahoo's "
                   "short % of float only without FINRA coverage; used for the whole year) minus the average stock; "
                   "z = (day - mean) / sd over 252 sessions; moves beyond +100 % / -50 % left out as data errors"),
        "units": "% a session",
        "labels": LABEL,
        "counts": {"universeRows": len(rows), "withHistory": len(P.names), "eligibleLast": len(elig_last),
                   "quintile": len(nxt["mom"][1]), "siBasket": len(si_basket), "crowded": len(crowded), "siOver100": odd[:20],
                   "siSource": si_src, "finraCover": round(cover, 3)},
        "stats": stats,
        "last": {"date": dates[-1], "ret": lastv, "z": lastz, "legs": legs, "spx": {"chg": round(bchg, 3) if bchg is not None else None,
                 "sigma": round(bsig, 2) if bsig is not None else None, "sym": bsym}, "character": ch},
        "baskets": {"formedOn": dates[-1], "universe": nxt["universe"],
                    "mom": {"bottom": nxt["mom"][0], "top": nxt["mom"][1]},
                    "rev": {"bottom": nxt["rev"][0], "top": nxt["rev"][1]},
                    "beta": {"bottom": nxt["beta"][0], "top": nxt["beta"][1], "formedOn": dates[nf]},
                    "si": si_basket, "siSource": si_src,
                    "siPct": {t: round((official[t]["pct"] if si_src == "finra" else rows[t]["si"]) * 100, 1) for t in si_basket},
                    "siDtc": {t: official[t].get("dtc") for t in si_basket if t in official}},
        "crowded": crowded,
        "reported": reported,
        "shortVolume": short_vol,
        "series": ser,
        "events": events[:EVENTS_KEEP],
    }
    return doc


def same(a, b):
    strip = lambda x: {k: v for k, v in (x or {}).items() if k != "at"}
    return json.dumps(strip(a), sort_keys=True) == json.dumps(strip(b), sort_keys=True)


def main():
    t0 = time.time()
    rows = universe_rows()
    if not rows:
        log("factors: no master rows read - nothing written")
        return 0
    bench = load_bench()
    if not bench[0]:
        log("factors: no benchmark history (^GSPC / SPY) - nothing written")
        return 0
    keep = WINDOW + MOM_LONG + 2
    data = load_panel(rows, bench[1][-keep:])
    patched = patch_from_tapes(data, rows, bench[1][-keep:])
    if patched:
        log("factors: closes missing from the histories, taken from the books' day tapes: " + ", ".join(f"{d} {n}" for d, n in sorted(patched.items())))
    official, vol = load_short()
    doc = build(rows, bench, data, official=official, vol=vol)
    if not doc:
        log("factors: not enough sessions - nothing written")
        return 0
    doc["counts"]["patchedFromTapes"] = patched
    L, c = doc["last"], doc["counts"]
    zs = " · ".join(f"{k} {L['ret'][k]:+.2f}% ({L['z'][k]:+.1f} sd)" for k in FACTORS if L["ret"].get(k) is not None and L["z"].get(k) is not None)
    rp = doc.get("reported") or {}
    log(f"factors {BUILD}: {doc['date']} (bench {doc['bench']}) · {c['eligibleLast']} names, fifths of {c['quintile']}, the "
        f"{c['siBasket']} most-shorted ({c['siSource']}, FINRA covers {c['finraCover']:.0%}) · {zs} · the day: "
        f"{L['character']['label'] or 'no positioning signal'} · {len(doc['events'])} days at 2 sd+ in the year"
        + (f" · FINRA settled {rp['settle']}" + (f" (PARTIAL - {rp['cover']:.0%} of the names)" if rp.get("partial") else "")
           + f": the 50 {rp['basket']['chg']:+.1f}% dollars short, the universe {rp['universe']['chg']:+.1f}%"
           if rp.get("basket", {}).get("chg") is not None and rp.get("universe", {}).get("chg") is not None else "")
        + f" · {time.time() - t0:.1f}s")
    prior = dv._read(OUT)
    if same(prior, doc):
        log("factors: unchanged since the last run - not rewritten")
        return 0
    dv._write(OUT, doc)
    log(f"factors: wrote {OUT.name} ({OUT.stat().st_size / 1e3:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
