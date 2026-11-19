#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/warning_signals.py
#
#  z196 - THE DIGGING, part 2: read what is published and say, in plain English, whether stress is building where the
#  market isn't looking - or whether growth is organic (income, jobs, output, profits) rather than borrowed.
#
#  Owner (11-19): "I want this system to do the digging and find the warning signals for either organic upside growth
#  or signs of extreme risk." Steve Eisman's 2007 lessons, made mechanical:
#    * read the loss data, not the story  - bank delinquency / charge-off rates, claims, house prices (the public
#      remittance reports); a company saying "losses will stay under 5%" is not data
#    * models that assume history can't break - every series is scored against ITS OWN history since the 1980s-90s
#      (expanding window, publication lags respected: no hindsight)
#    * a seasonal bounce is not a recovery     - a short-term improvement against a worsening year-over-year trend is
#      flagged (tax-refund season improves consumer delinquencies every Feb-Apr; Eisman's team doubled up on it)
#    * watch the marginal buyer               - when the buyers of risk step back (commercial paper, asset-backed
#      paper, credit spreads, banks tightening) the refinancing machine stops ("Dusseldorf", summer 2007)
#    * the warning is public; the market may not be pricing it - "market blindness": fundamentals deteriorating for
#      months while spreads and volatility stay calm
#  And the other side: ORGANIC growth = real income ex transfers, payrolls, production, orders and profits rising
#  while credit grows no faster than income. Growth carried by debt outrunning income is late-cycle, not organic.
#
#  Four gauges (0-100, each an average of percentile scores against history), and a composite:
#    Fundamental stress  loss data 30 % · lenders 20 % · borrowers (jobs) 30 % · collateral 20 %
#    Market stress       spreads, VIX, financial-conditions indexes, commercial-paper funding
#    Fragility           leverage build-up and late-cycle signs (credit vs income, corporate debt / GDP, the curve)
#    Organic growth      income, jobs, output, orders, profits, credit not outrunning income
#    Composite = 0.40 fundamental + 0.40 market + 0.20 fragility; RISK = the composite's percentile vs its own history
#  Plus a bottom-up layer from data the system already collects: 8-K red flags (bankruptcy, debt acceleration,
#  non-reliance, impairments) vs their own base rate, insider buying breadth, short interest, headcount, the options
#  market's breadth (TRAPP2-OPTIONS market.json), credit-rating trends (TRAPP2-CREDIT), 52-week high / low breadth.
#  Sector exposures (which industries each warning hits) feed the bot. A BACKTEST against NBER recessions and equity
#  drawdowns since the 1990s says how much each reading has been worth - including how often it was wrong.
#
#  In:  data/warning/series/*.json (warning_series.py), data/sec, data/short, data/headcount (this repo);
#       TRAPP2-OPTIONS data/options/market.json, TRAPP2-CREDIT data/ratings.json, the five books' master.json (raw)
#  Out: data/warning/signals.json (the read), data/warning/daily.json (one row a day: the live gauges + raw breadth)
#  Env: WARNING_NOW (YYYY-MM-DD, tests) · WARNING_OFFLINE=1 (no raw fetches)
# ============================================================
import bisect
import json
import math
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from warning_series import SERIES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SER_DIR = DATA / "warning" / "series"
OUT_P = DATA / "warning" / "signals.json"
DAILY_P = DATA / "warning" / "daily.json"
OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER") or "TheMostLocal"
RAW = f"https://raw.githubusercontent.com/{OWNER}"
BOOKS = ("TRAPP2", "TRAPP2-1", "TRAPP2-2", "TRAPP2-3", "TRAPP2-4")
BUILD = "z196"
MIN_MONTHS = 60          # 5 years of a transformed series before it is scored
BACKTEST_FROM = "1990-01"
DAILY_KEEP = 800

# ------------------------------------------------------------------------------------------------------------------
#  What is scored. key, pillar, label, source (series id, or a derived key), transform, direction, weight, group
#  transform: level | yoy_diff (change in points over a year) | yoy_pct (% change over a year) | chg6 (6-month change)
#  direction: +1 = higher is MORE stress (for the growth pillar: +1 = higher is MORE growth)
#  group: which industries a warning in it hits (EXPOSURE below)
# ------------------------------------------------------------------------------------------------------------------
IND = [
    # loss data - what borrowers are failing to pay
    ("cc_dq", "loss", "Credit-card delinquencies", "DRCCLACBS", "yoy_diff", 1, 1.0, "consumer"),
    ("cc_dq_lvl", "loss", "Credit-card delinquency level", "DRCCLACBS", "level", 1, 0.4, "consumer"),
    ("cc_co", "loss", "Credit-card charge-offs", "CORCCACBS", "yoy_diff", 1, 0.8, "consumer"),
    ("cons_dq", "loss", "Consumer-loan delinquencies", "DRCLACBS", "yoy_diff", 1, 0.6, "consumer"),
    ("mtg_dq", "loss", "Mortgage delinquencies", "DRSFRMACBS", "yoy_diff", 1, 1.0, "housing"),
    ("mtg_dq_lvl", "loss", "Mortgage delinquency level", "DRSFRMACBS", "level", 1, 0.4, "housing"),
    ("mtg_co", "loss", "Mortgage charge-offs", "CORSFRMACBS", "yoy_diff", 1, 0.6, "housing"),
    ("cre_dq", "loss", "Commercial real-estate delinquencies", "DRCRELEXFACBS", "yoy_diff", 1, 0.9, "cre"),
    ("bl_dq", "loss", "Business-loan delinquencies", "DRBLACBS", "yoy_diff", 1, 0.8, "business"),
    ("all_dq", "loss", "All-loan delinquencies", "DRALACBS", "yoy_diff", 1, 0.5, "business"),
    # lenders - who is pulling back
    ("sloos_ci", "lenders", "Banks tightening business loans (large / mid)", "DRTSCILM", "level", 1, 1.0, "business"),
    ("sloos_cis", "lenders", "Banks tightening business loans (small firms)", "DRTSCIS", "level", 1, 0.6, "business"),
    ("sloos_cc", "lenders", "Banks tightening credit cards", "DRTSCLCC", "level", 1, 0.8, "consumer"),
    ("sloos_dem", "lenders", "Business loan demand", "DRSDCILM", "level", -1, 0.5, "business"),
    # borrowers - can they pay
    ("claims", "labor", "Initial jobless claims", "ICSA", "yoy_pct", 1, 1.0, "labor"),
    ("cclaims", "labor", "Continuing jobless claims", "CCSA", "yoy_pct", 1, 0.6, "labor"),
    ("unrate", "labor", "Unemployment rate", "UNRATE", "yoy_diff", 1, 0.8, "labor"),
    ("sahm", "labor", "Sahm rule", "SAHMREALTIME", "level", 1, 0.8, "labor"),
    ("temps", "labor", "Temporary-help jobs", "TEMPHELPS", "yoy_pct", -1, 0.7, "labor"),
    ("payrolls_s", "labor", "Nonfarm payrolls", "PAYEMS", "yoy_pct", -1, 0.5, "labor"),
    # collateral - what backs the loans
    ("hpi", "collateral", "Home prices", "CSUSHPINSA", "yoy_pct", -1, 1.0, "housing"),
    ("supply", "collateral", "Months' supply of new homes", "MSACSR", "level", 1, 0.6, "housing"),
    ("supply_d", "collateral", "New-home supply, change", "MSACSR", "yoy_diff", 1, 0.5, "housing"),
    ("permits", "collateral", "Building permits", "PERMIT", "yoy_pct", -1, 0.6, "housing"),
    ("mrate", "collateral", "30-year mortgage rate, change", "MORTGAGE30US", "yoy_diff", 1, 0.4, "housing"),
    # funding & markets - is the marginal buyer still buying
    ("baa", "funding", "Baa corporate spread", "BAA10Y", "level", 1, 1.0, "funding"),
    ("baa_d", "funding", "Baa spread, 6-month change", "BAA10Y", "chg6", 1, 0.6, "funding"),
    ("nfci", "funding", "Financial conditions (NFCI)", "NFCI", "level", 1, 1.0, "funding"),
    ("nfci_cr", "funding", "NFCI credit", "NFCICREDIT", "level", 1, 0.6, "funding"),
    ("nfci_risk", "funding", "NFCI risk", "NFCIRISK", "level", 1, 0.6, "funding"),
    ("stlfsi", "funding", "St. Louis Fed stress index", "STLFSI4", "level", 1, 0.8, "funding"),
    ("vix", "funding", "VIX", "VIXCLS", "level", 1, 0.8, "funding"),
    ("cp", "funding", "Commercial-paper funding spread", "CPSPREAD", "level", 1, 0.8, "funding"),
    ("abcp", "funding", "Asset-backed commercial paper outstanding", "ABCOMP", "yoy_pct", -1, 0.8, "funding"),
    ("hy", "funding", "High-yield spread", "BAMLH0A0HYM2", "level", 1, 0.8, "funding"),
    # leverage & late cycle - how much rides on it
    ("cred_gap", "fragility", "Consumer credit growth minus income growth", ("TOTALSL", "DSPI"), "yoy_gap", 1, 1.0, "consumer"),
    ("revol_gap", "fragility", "Revolving credit growth minus income growth", ("REVOLSL", "DSPI"), "yoy_gap", 1, 0.6, "consumer"),
    ("corp_debt", "fragility", "Corporate debt / GDP, change", "CORPDEBT_GDP", "yoy_diff", 1, 0.8, "business"),
    ("hh_debt", "fragility", "Household debt / GDP, change", "HDTGPDUSQ163N", "yoy_diff", 1, 0.6, "consumer"),
    ("bank_credit", "fragility", "Bank loan growth", "TOTLL", "yoy_pct", 1, 0.6, "business"),
    ("nfci_lev", "fragility", "NFCI leverage", "NFCILEVERAGE", "level", 1, 0.8, "funding"),
    ("curve", "fragility", "Yield curve (10y - 3m)", "T10Y3M", "level", -1, 0.8, "funding"),
    ("saving", "fragility", "Personal saving rate", "PSAVERT", "level", -1, 0.5, "consumer"),
    ("dsr", "fragility", "Household debt-service ratio", "TDSP", "level", 1, 0.5, "consumer"),
    # organic growth - income, jobs, output, orders, profits; credit not outrunning income
    ("income", "growth", "Real income ex transfers", "W875RX1", "yoy_pct", 1, 1.0, None),
    ("payrolls", "growth", "Payroll growth", "PAYEMS", "yoy_pct", 1, 0.8, None),
    ("indpro", "growth", "Industrial production", "INDPRO", "yoy_pct", 1, 0.6, None),
    ("orders", "growth", "Core capital-goods orders", "NEWORDER", "yoy_pct", 1, 0.6, None),
    ("retail", "growth", "Real retail sales", "REAL_RETAIL", "yoy_pct", 1, 0.5, None),
    ("cfnai", "growth", "Chicago Fed activity index", "CFNAIMA3", "level", 1, 0.8, None),
    ("openings", "growth", "Job openings", "JTSJOL", "yoy_pct", 1, 0.4, None),
    ("profits", "growth", "Corporate profits", "CP", "yoy_pct", 1, 0.5, None),
    ("productivity", "growth", "Productivity", "OPHNFB", "yoy_pct", 1, 0.4, None),
    ("starts", "growth", "Housing starts", "HOUST", "yoy_pct", 1, 0.3, None),
    ("income_led", "growth", "Income growth minus consumer-credit growth", ("DSPI", "TOTALSL"), "yoy_gap", 1, 0.6, None),
]
# derived series: (kind, a, b, scale)  ratio: a / b x scale · diff: a - b
DERIVED = {
    "CPSPREAD": ("diff", "DCPF3M", "DTB3", 1.0),
    "CORPDEBT_GDP": ("ratio", "BCNSDODNS", "GDP", 0.1),      # $m / $bn SAAR -> % of GDP
    "REAL_RETAIL": ("ratio", "RSAFS", "CPIAUCSL", 1.0),
}
DERIVED_UNIT = {"CPSPREAD": "pts", "CORPDEBT_GDP": "% of GDP", "REAL_RETAIL": "index"}
PILLARS = {
    "loss": ("Loss data", "what borrowers are failing to pay", "fundamental", 0.30),
    "lenders": ("Lenders", "who is pulling back", "fundamental", 0.20),
    "labor": ("Borrowers (jobs)", "can they pay", "fundamental", 0.30),
    "collateral": ("Collateral", "what backs the loans", "fundamental", 0.20),
    "funding": ("Funding & markets", "is the marginal buyer still buying", "market", 1.0),
    "fragility": ("Leverage & late cycle", "how much rides on it", "fragility", 1.0),
    "growth": ("Organic growth", "income, jobs, output, orders, profits", "growth", 1.0),
}
COMPOSITE_W = {"fundamental": 0.40, "market": 0.40, "fragility": 0.20}
LEVELS = ((40, "low"), (60, "moderate"), (75, "elevated"), (90, "high"), (101, "extreme"))
NBER_PEAKS = ("1990-07", "2001-03", "2007-12", "2020-02")      # fallback when USREC is missing
# which industries a warning in each group hits (Yahoo sector / industry names, as in the books' master.json)
EXPOSURE = {
    "consumer": [("industry", "Credit Services", 1.0), ("industry", "Auto & Truck Dealerships", 0.6), ("industry", "Specialty Retail", 0.4),
                 ("industry", "Banks - Regional", 0.4), ("sector", "Consumer Cyclical", 0.4)],
    "housing": [("industry", "Residential Construction", 1.0), ("industry", "Mortgage Finance", 1.0), ("industry", "REIT - Mortgage", 0.8),
                ("industry", "Real Estate Services", 0.7), ("industry", "Building Products & Equipment", 0.6),
                ("industry", "Home Improvement Retail", 0.5), ("industry", "Furnishings, Fixtures & Appliances", 0.5),
                ("industry", "Banks - Regional", 0.5), ("sector", "Real Estate", 0.4)],
    "cre": [("industry", "REIT - Office", 1.0), ("industry", "Banks - Regional", 0.9), ("industry", "REIT - Retail", 0.7),
            ("industry", "REIT - Diversified", 0.6), ("sector", "Real Estate", 0.6)],
    "business": [("industry", "Banks - Regional", 0.6), ("industry", "Asset Management", 0.4), ("sector", "Industrials", 0.4),
                 ("sector", "Basic Materials", 0.3)],
    "funding": [("industry", "REIT - Mortgage", 0.8), ("industry", "Asset Management", 0.7), ("industry", "Capital Markets", 0.6),
                ("industry", "Mortgage Finance", 0.6), ("industry", "Banks - Regional", 0.6), ("industry", "Banks - Diversified", 0.5)],
    "labor": [("industry", "Staffing & Employment Services", 1.0), ("industry", "Advertising Agencies", 0.5), ("industry", "Restaurants", 0.4),
              ("industry", "Travel Services", 0.4), ("industry", "Leisure", 0.4), ("sector", "Consumer Cyclical", 0.6)],
}
GROUP_TEXT = {"consumer": "consumer credit", "housing": "housing / mortgages", "cre": "commercial real estate",
              "business": "business credit", "funding": "funding markets", "labor": "the labour market"}


# ------------------------------------------------------------------------------------------------------------------
#  dates and series
# ------------------------------------------------------------------------------------------------------------------
def _d(s):
    return date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def month_end(y, m):
    return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))


def months_between(a, b):
    """Month-ends from month a (YYYY-MM) through the month before b (date) - complete months only."""
    y, m = int(a[:4]), int(a[5:7])
    out = []
    while True:
        e = month_end(y, m)
        if e >= b:
            break
        out.append(e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class Series:
    """One series: sorted dates / values, its frequency and publication lag."""
    def __init__(self, sid, obs, freq, lag):
        self.id, self.freq, self.lag = sid, freq, lag
        obs = sorted((_d(d), float(v)) for d, v in obs if d and v is not None)
        self.dates = [d for d, _ in obs]
        self.vals = [v for _, v in obs]
        self._cum = [0.0]
        for v in self.vals:
            self._cum.append(self._cum[-1] + v)

    def asof(self, t, lag=None):
        """The value known on date t. Daily / weekly: the mean of what was published in the 30 days to t (a month's
        reading, not one noisy print); monthly / quarterly: the latest observation published by t."""
        lag = self.lag if lag is None else lag
        cut = t - timedelta(days=lag)
        j = bisect.bisect_right(self.dates, cut)
        if j == 0:
            return None
        if self.freq in ("d", "w"):
            i = bisect.bisect_right(self.dates, cut - timedelta(days=30))
            if j - i <= 0:
                return None if (cut - self.dates[j - 1]).days > 45 else self.vals[j - 1]
            return (self._cum[j] - self._cum[i]) / (j - i)
        if (cut - self.dates[j - 1]).days > (130 if self.freq == "m" else 400):
            return None                      # discontinued / gap: don't carry a stale value forever
        return self.vals[j - 1]

    def last(self):
        return (self.dates[-1], self.vals[-1]) if self.dates else (None, None)


def load_series(ser_dir=None):
    ser_dir = Path(ser_dir or SER_DIR)
    out = {}
    for sid, meta in SERIES.items():
        p = ser_dir / f"{sid}.json"
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text())
            obs = d.get("obs") or [[o.get("date"), o.get("value")] for o in d.get("observations") or []]
            s = Series(sid, obs, meta[2], meta[3])
            if len(s.dates) >= 8:
                out[sid] = s
        except Exception as e:
            print(f"  series {sid} unreadable: {type(e).__name__}")
    return out


def value_at(src, S, t, lag0=False):
    """As-of value of a series id or a DERIVED key on date t (lag0: everything on file counts - the live read)."""
    if isinstance(src, str) and src in DERIVED:
        kind, a, b, k = DERIVED[src]
        va, vb = value_at(a, S, t, lag0), value_at(b, S, t, lag0)
        if va is None or vb is None:
            return None
        if kind == "diff":
            return va - vb
        return (va / vb * k) if vb else None
    s = S.get(src)
    if not s:
        return None
    return s.asof(t, 0 if lag0 else None)


def transformed(spec, S, t, lag0=False):
    """The scored number for one indicator on date t."""
    src, tf = spec[3], spec[4]
    year_ago = t - timedelta(days=365)
    if tf == "yoy_gap":
        a, b = src
        a1, a0, b1, b0 = (value_at(a, S, t, lag0), value_at(a, S, year_ago, lag0), value_at(b, S, t, lag0), value_at(b, S, year_ago, lag0))
        if None in (a1, a0, b1, b0) or a0 <= 0 or b0 <= 0:
            return None
        return (a1 / a0 - 1) * 100 - (b1 / b0 - 1) * 100
    v = value_at(src, S, t, lag0)
    if v is None:
        return None
    if tf == "level":
        return v
    if tf == "chg6":
        p = value_at(src, S, t - timedelta(days=182), lag0)
        return None if p is None else v - p
    p = value_at(src, S, year_ago, lag0)
    if p is None:
        return None
    if tf == "yoy_diff":
        return v - p
    if tf == "yoy_pct":
        return (v / p - 1) * 100 if p > 0 else None
    return None


def _raw_now(spec, S):
    """The latest published value of the indicator's (first) series and its date - for the plain-English line."""
    src = spec[3][0] if isinstance(spec[3], tuple) else spec[3]
    if src in DERIVED:
        kind, a, b, k = DERIVED[src]
        sa, sb = S.get(a), S.get(b)
        if not sa or not sb:
            return None, None
        t = min(sa.last()[0], sb.last()[0])
        return value_at(src, S, t, True), t
    s = S.get(src)
    return (s.last()[1], s.last()[0]) if s else (None, None)


# ------------------------------------------------------------------------------------------------------------------
#  scoring
# ------------------------------------------------------------------------------------------------------------------
class Expanding:
    """Percentile of each new value within everything seen so far (ties count half) - no look-ahead."""
    def __init__(self):
        self.xs = []

    def pct(self, x):
        if x is None or not self.xs:
            return None
        lo, hi = bisect.bisect_left(self.xs, x), bisect.bisect_right(self.xs, x)
        return (lo + 0.5 * (hi - lo)) / len(self.xs) * 100

    def add(self, x):
        if x is not None:
            bisect.insort(self.xs, x)


def _wmean(pairs):
    pairs = [(v, w) for v, w in pairs if v is not None and w > 0]
    sw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / sw if sw else None


def level_of(risk):
    if risk is None:
        return "unknown"
    for cut, name in LEVELS:
        if risk < cut:
            return name
    return "extreme"


def build_history(S, today, months=None):
    """Monthly panel since BACKTEST_FROM: each indicator's transformed value as known at each month-end (publication
    lags respected) and its score vs everything before it; pillars, the four gauges, composite, risk percentile."""
    months = months or months_between(BACKTEST_FROM, today)
    ind_hist = {spec[0]: Expanding() for spec in IND}
    counts = {spec[0]: 0 for spec in IND}
    first = {}
    gauges_hist = {k: Expanding() for k in ("fundamental", "market", "fragility", "growth", "composite")}
    rows = []
    for t in months:
        scores = {}
        for spec in IND:
            k, x = spec[0], transformed(spec, S, t)
            if x is None:
                continue
            first.setdefault(k, t)
            if counts[k] >= MIN_MONTHS:
                p = ind_hist[k].pct(x)
                scores[k] = p if spec[5] > 0 else 100 - p
            ind_hist[k].add(x)
            counts[k] += 1
        g = gauges(scores)
        row = {"t": t, "scores": scores, **g}
        for k in gauges_hist:
            row[k + "P"] = gauges_hist[k].pct(g.get(k)) if len(gauges_hist[k].xs) >= MIN_MONTHS else None
            gauges_hist[k].add(g.get(k))
        rows.append(row)
    ind_hist["_first"] = first
    return rows, ind_hist, gauges_hist


def pillar_scores(scores):
    out = {}
    for p in PILLARS:
        out[p] = _wmean([(scores.get(spec[0]), spec[6]) for spec in IND if spec[1] == p])
    return out


def gauges(scores):
    ps = pillar_scores(scores)
    fund = _wmean([(ps[p], PILLARS[p][3]) for p in PILLARS if PILLARS[p][2] == "fundamental"])
    out = {"pillars": ps, "fundamental": fund, "market": ps["funding"], "fragility": ps["fragility"], "growth": ps["growth"]}
    out["composite"] = _wmean([(out[k], w) for k, w in COMPOSITE_W.items()])
    return out


BLIND_F_PCT, BLIND_F_RISE, BLIND_M_PCT = 70, 8, 50


LEAD_PILLARS = ("loss", "lenders")    # the leading fundamentals - losses show up and lenders pull back first


def lead_pillar(pillars):
    """The most stressed LEADING pillar (loss data, lenders) -> (score, name) or (None, None)."""
    c = [(pillars.get(p), p) for p in LEAD_PILLARS if (pillars or {}).get(p) is not None]
    return max(c) if c else (None, None)


def _blind(fp, d_f6, mp, lead=None, d_lead6=None):
    """Market blindness - the warning is published and not priced. Either the fundamentals as a whole are in their top
    30 % of stress and 8+ points worse in six months, or a leading pillar (loss data / lenders) is in its top 15 % and
    10+ points worse - while funding markets sit in their calmer half. Eisman's 2006-07: delinquencies climbing for
    months while spreads were the tightest on record."""
    if mp is None or mp > BLIND_M_PCT:
        return False
    a = fp is not None and fp >= BLIND_F_PCT and d_f6 is not None and d_f6 >= BLIND_F_RISE
    b = lead is not None and lead >= 85 and d_lead6 is not None and d_lead6 >= 10
    return bool(a or b)


def classify(fp, mp, frp, gp, d_f3, d_g3, mp_max12, lead=None, lead_name=None, d_lead3=None):
    """Regime from the gauges' percentiles vs their own history (+ recent changes). First match wins."""
    if mp is not None and mp >= 90:
        return "acute stress", "funding markets at the top decile of their history"
    if gp is not None and fp is not None and gp <= 20 and fp >= 70:
        return "contraction", "growth in its bottom fifth while fundamentals deteriorate"
    if mp is not None and fp is not None and mp >= 75 and fp >= 70:
        return "stress spreading", "fundamentals deteriorating and funding markets now reacting"
    if fp is not None and fp >= 70 and (d_f3 or 0) > 0 and (mp is None or mp < 80):
        return "stress building", "fundamentals deteriorating (loss data / jobs / collateral) before markets react"
    if lead is not None and lead >= 85 and (d_lead3 or 0) >= 0 and (mp is None or mp < 80):
        return "stress building", f"{PILLARS[lead_name][0].lower()} at the {ordinal(lead)} percentile of stress and not improving - it leads"
    if mp_max12 is not None and mp_max12 >= 90 and mp is not None and mp < 70 and (d_g3 or 0) >= 0:
        return "repair", "markets calming after acute stress, growth stabilising"
    if frp is not None and frp >= 75 and (gp or 0) >= 40 and (fp is None or fp < 70):
        return "late cycle (debt-fuelled)", "growth leaning on leverage: credit outrunning income, flat / inverted curve"
    if (gp is not None and gp >= 55 and (fp is None or fp <= 50) and (mp is None or mp <= 60) and (frp is None or frp < 75)
            and (lead is None or lead < 80)):
        return "organic expansion", "income, jobs and output growing without a credit build-up"
    return "neutral", "no gauge at an extreme"


# ------------------------------------------------------------------------------------------------------------------
#  backtest
# ------------------------------------------------------------------------------------------------------------------
def recession_starts(S, months):
    s = S.get("USREC")
    if s and len(s.dates) > 100:
        out, prev = [], 0
        for d, v in zip(s.dates, s.vals):
            if v >= 0.5 and prev < 0.5:
                out.append(month_end(d.year, d.month))
            prev = v
        return out, "USREC"
    return [month_end(int(x[:4]), int(x[5:7])) for x in NBER_PEAKS], "NBER dates (built in)"


def equity_series(S, spy=None):
    """Daily closes for the drawdown test: FRED's Nasdaq Composite (1971+), else TRAPP2-1's SPY history (2006+)."""
    s = S.get("NASDAQCOM")
    if s and len(s.dates) > 2000:
        return s.dates, s.vals, "Nasdaq Composite (FRED)"
    if spy:
        return [b[0] for b in spy], [b[1] for b in spy], "SPY (TRAPP2-1 history)"
    return None, None, None


def forward_stats(dates, vals, t, horizon=365):
    """Return and worst drawdown from the close on/before t over the next `horizon` days (None if not yet known)."""
    i = bisect.bisect_right(dates, t) - 1
    if i < 0 or dates[-1] < t + timedelta(days=horizon):
        return None, None
    p0 = vals[i]
    j = bisect.bisect_right(dates, t + timedelta(days=horizon)) - 1
    window = vals[i + 1:j + 1]
    if not window or p0 <= 0:
        return None, None
    return vals[j] / p0 - 1, min(window) / p0 - 1


def backtest(rows, S, spy=None):
    rec, rec_src = recession_starts(S, [r["t"] for r in rows])
    ed, ev, eq_src = equity_series(S, spy)
    scored = [r for r in rows if r.get("compositeP") is not None]
    out = {"from": scored[0]["t"].isoformat()[:7] if scored else None, "months": len(scored), "recessionSource": rec_src,
           "equity": eq_src, "recessions": [], "buckets": [], "regimes": {}, "hit": None, "earned": {}}
    if not scored:
        return out
    by_t = {r["t"]: r for r in scored}
    ts = [r["t"] for r in scored]
    for s0 in rec:
        if s0 < ts[0] + timedelta(days=200):
            continue
        before = [r for r in scored if s0 - timedelta(days=731) <= r["t"] < s0]
        first = next((r for r in before if (r["compositeP"] or 0) >= 75), None)
        first_f = next((r for r in before if (r.get("fundamentalP") or 0) >= 70), None)
        blind = [r for r in before if r.get("blind")]

        def at(k):
            tt = month_end((s0 - timedelta(days=30 * k)).year, (s0 - timedelta(days=30 * k)).month)
            r = by_t.get(tt)
            return round(r["compositeP"], 1) if r and r.get("compositeP") is not None else None
        out["recessions"].append({"start": s0.isoformat()[:7], "riskAt12m": at(12), "riskAt6m": at(6), "riskAt3m": at(3),
                                  "leadMonths": round((s0 - first["t"]).days / 30.4) if first else None,
                                  "fundamentalLeadMonths": round((s0 - first_f["t"]).days / 30.4) if first_f else None,
                                  "blindnessMonths": len(blind)})
    if ed:
        fw = []
        for r in scored:
            ret, dd = forward_stats(ed, ev, r["t"])
            rec12 = any(r["t"] < s0 <= r["t"] + timedelta(days=366) for s0 in rec)
            if ret is not None:
                fw.append((r, ret, dd, rec12))
        if fw:
            base_dd = sum(1 for _, _, dd, _ in fw if dd <= -0.20) / len(fw)
            base_ret = sum(x[1] for x in fw) / len(fw)
            out["base"] = {"n": len(fw), "fwd12Ret": round(base_ret * 100, 1), "dd20Rate": round(base_dd * 100, 1),
                           "recessionWithin12": round(sum(1 for x in fw if x[3]) / len(fw) * 100, 1)}
            for lo, hi, name in ((0, 40, "low"), (40, 60, "moderate"), (60, 75, "elevated"), (75, 90, "high"), (90, 101, "extreme")):
                b = [x for x in fw if lo <= x[0]["compositeP"] < hi]
                if b:
                    out["buckets"].append({"level": name, "n": len(b), "fwd12Ret": round(sum(x[1] for x in b) / len(b) * 100, 1),
                                           "dd20Rate": round(sum(1 for x in b if x[2] <= -0.20) / len(b) * 100, 1),
                                           "avgWorstDD": round(sum(x[2] for x in b) / len(b) * 100, 1),
                                           "recessionWithin12": round(sum(1 for x in b if x[3]) / len(b) * 100, 1)})
            sig = [x for x in fw if x[0]["compositeP"] >= 75]
            if sig:
                hit = sum(1 for x in sig if x[2] <= -0.20 or x[3]) / len(sig)
                base_hit = sum(1 for x in fw if x[2] <= -0.20 or x[3]) / len(fw)
                out["hit"] = {"signalMonths": len(sig), "hitRate": round(hit * 100, 1), "baseRate": round(base_hit * 100, 1),
                              "falseAlarmRate": round((1 - hit) * 100, 1),
                              "rule": "risk >= 75th percentile; hit = a 20% drawdown or a recession start within 12 months"}
            for reg in sorted({x[0].get("regime") for x in fw if x[0].get("regime")}):
                b = [x for x in fw if x[0].get("regime") == reg]
                out["regimes"][reg] = {"n": len(b), "fwd12Ret": round(sum(x[1] for x in b) / len(b) * 100, 1),
                                       "dd20Rate": round(sum(1 for x in b if x[2] <= -0.20) / len(b) * 100, 1)}
            hi_b = [x for x in fw if x[0]["compositeP"] >= 75]
            org = out["regimes"].get("organic expansion") or {}
            out["earned"] = {
                # a size cut on high risk is earned when high-risk months really were followed by more 20% drawdowns
                "throttle": bool(len(hi_b) >= 24 and (sum(1 for x in hi_b if x[2] <= -0.20) / len(hi_b)) >= 1.5 * max(base_dd, 0.01)),
                # leaning in on organic expansion is earned only when those months beat the average AND fell less
                "organicUpside": bool(org.get("n", 0) >= 24 and org["fwd12Ret"] > out["base"]["fwd12Ret"] and org["dd20Rate"] < out["base"]["dd20Rate"]),
            }
    return out


# ------------------------------------------------------------------------------------------------------------------
#  bottom-up: what the companies, insiders, short sellers, options and credit markets are saying now
# ------------------------------------------------------------------------------------------------------------------
RED_W = {"1.03": 3.0, "2.04": 2.0, "4.02": 2.0, "2.06": 1.0, "4.01": 1.0, "3.01": 0.5}
RED_LABEL = {"1.03": "bankruptcy", "2.04": "debt acceleration", "4.02": "non-reliance on past financials", "2.06": "material impairment",
             "4.01": "auditor change", "3.01": "delisting notice"}
MERGER_ITEMS = {"2.01", "5.01"}      # a delisting notice filed with a completed acquisition / change in control is a merger
CAUTION_W = {"2.05": 0.5, "1.05": 0.3}
ANCHORS = {   # provisional raw -> 0-100 stress maps, used until 40 days of daily history exist (then: percentile)
    "secRatio": [(0.6, 25), (1.0, 50), (1.6, 80), (2.5, 95)],
    "shortRising": [(0.20, 25), (0.35, 50), (0.50, 80)],
    "headcountCutting": [(0.15, 25), (0.25, 50), (0.40, 80)],
    "optInverted": [(0.10, 20), (0.25, 50), (0.45, 80), (0.60, 95)],
    "optSkewHigh": [(0.10, 25), (0.25, 50), (0.45, 80)],
    "optIvRankHigh": [(0.05, 20), (0.15, 50), (0.35, 80), (0.60, 95)],
    "optPutLed": [(0.30, 25), (0.45, 50), (0.65, 80)],
    "creditNegTrend": [(0.15, 25), (0.30, 50), (0.50, 80)],
    "nearLows": [(0.05, 20), (0.15, 50), (0.35, 85)],
    "insiderBuy": [(0.10, 30), (0.20, 50), (0.35, 75), (0.50, 90)],      # growth side: more buying = more conviction
    "nearHighs": [(0.10, 30), (0.25, 50), (0.45, 80)],                    # growth side
}
BU_MARKET = ("optInverted", "optSkewHigh", "optIvRankHigh", "optPutLed", "creditNegTrend", "nearLows")
BU_FUND = ("secRatio", "shortRising", "headcountCutting")
BU_UP = ("insiderBuy", "nearHighs")


def _interp(x, pts):
    if x is None:
        return None
    if x <= pts[0][0]:
        return float(pts[0][1]) * max(0.0, x / pts[0][0]) if pts[0][0] > 0 else float(pts[0][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(min(99, pts[-1][1] + (x - pts[-1][0]) * 10))


def _get_json(url, timeout=40):
    if os.environ.get("WARNING_OFFLINE") == "1":
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TRAPP2-ANALYTICS warning"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  (unavailable: {url.split('/main/')[-1]} - {type(e).__name__})")
        return None


def load_books():
    """ticker -> {sector, industry, price, high52, low52} from the five books."""
    out = {}
    for b in BOOKS:
        for r in (_get_json(f"{RAW}/{b}/main/data/master.json", 60) or []):
            if not isinstance(r, dict):
                continue
            t = str(r.get("ticker") or "").upper()
            if t and t not in out and str(r.get("asset_class") or "").lower() in ("equity", "etf"):
                out[t] = {"sector": r.get("sector"), "industry": r.get("industry"), "price": r.get("price"),
                          "high52": r.get("high52"), "low52": r.get("low52"), "currency": r.get("currency"), "isetf": r.get("isetf")}
    return out


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def sec_breadth(sec_dir, books, today):
    """8-K red flags in the last 30 days vs their own base rate (days 31-180), per 1,000 companies covered; by sector;
    the latest red filings (the companies telling you). Form 4: share of companies with insiders net buying (90 days)."""
    sec_dir = Path(sec_dir)
    files = [p for p in sec_dir.glob("*.json") if p.name != "index.json"] if sec_dir.exists() else []
    if not files:
        return None
    n = 0
    w30 = wbase = 0.0
    by = {}
    recent = []
    for p in files:
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        n += 1
        t = str(d.get("ticker") or p.stem).upper()
        sec = (books.get(t) or {}).get("sector") or "Unknown"
        bs = by.setdefault(sec, {"n": 0, "w30": 0.0, "wbase": 0.0})
        bs["n"] += 1
        for e in d.get("events") or []:
            try:
                age = (today - _d(str(e.get("date"))[:10])).days
            except (TypeError, ValueError):
                continue
            if age < 0 or age > 180:
                continue
            items = set(e.get("items") or []) | set(e.get("red") or []) | set(e.get("caution") or [])
            red = [i for i in (e.get("red") or []) if not (i == "3.01" and items & MERGER_ITEMS)]
            w = sum(RED_W.get(i, 0) for i in red) + sum(CAUTION_W.get(i, 0) for i in e.get("caution") or [] if i not in MERGER_ITEMS)
            if w <= 0:
                continue
            if age <= 30:
                w30 += w
                bs["w30"] += w
                if red:
                    recent.append({"ticker": t, "date": str(e.get("date"))[:10], "items": red,
                                   "what": [RED_LABEL.get(i, i) for i in red], "sector": sec})
            else:
                wbase += w * 30.0 / 150.0
                bs["wbase"] += w * 30.0 / 150.0
    if not n:
        return None
    recent.sort(key=lambda r: (r["date"], sum(RED_W.get(i, 0) for i in r["items"])), reverse=True)
    # ratios are shrunk toward 1 (+2 weighted events each side) so a sector with two filings doesn't read 6x
    out = {"companies": n, "redRate30": round(w30 / n * 1000, 2), "redRateBase": round(wbase / n * 1000, 2),
           "ratio": round((w30 + 2) / (wbase + 2), 3) if (w30 + wbase) > 0 else None, "events30": round(w30, 1), "recent": recent[:10],
           "bySector": {k: {"n": v["n"], "ratio": round((v["w30"] + 2) / (v["wbase"] + 2), 2), "events30": round(v["w30"], 1),
                            "rate30": round(v["w30"] / v["n"] * 1000, 1)} for k, v in sorted(by.items()) if v["n"] >= 15},
           "weights": "1.03 bankruptcy 3 · 2.04 debt acceleration 2 · 4.02 non-reliance 2 · 2.06 impairment 1 · 4.01 auditor change 1 · "
                      "3.01 delisting notice 0.5 (often a voluntary transfer) · 2.05 restructuring 0.5 · 1.05 cyber incident 0.3"}
    idx = None
    try:
        idx = json.loads((sec_dir / "index.json").read_text())
    except Exception:
        pass
    if idx and isinstance(idx.get("tickers"), dict):
        act = buy = sell = cl = ceo = 0
        for t, v in idx["tickers"].items():
            ins = (v or {}).get("insider") or {}
            b, s = int(ins.get("buyers") or 0), int(ins.get("sellers") or 0)
            if b or s:
                act += 1
                buy += b > s
                sell += s > b
                cl += bool(ins.get("cluster"))
                ceo += bool(ins.get("ceoCfoBuy"))
        if act:
            out["insiders"] = {"companiesWithActivity": act, "netBuyers": buy, "netSellers": sell,
                               "buyShare": round(buy / act, 4), "clusterBuys": cl, "ceoCfoBuys": ceo}
    return out


def short_breadth(short, books):
    if not isinstance(short, dict):
        return None
    si = short.get("interest") or {}
    vol = short.get("volume") or {}
    ch = [(t, _num((v or {}).get("changePct"))) for t, v in si.items()]
    ch = [(t, c) for t, c in ch if c is not None]
    zs = [_num((v or {}).get("z")) for v in vol.values()]
    zs = [z for z in zs if z is not None]
    if not ch and not zs:
        return None
    by = {}
    for t, c in ch:
        sec = (books.get(t) or {}).get("sector") or "Unknown"
        b = by.setdefault(sec, [0, 0])
        b[0] += 1
        b[1] += c >= 10
    dtc = sorted(x for x in (_num((v or {}).get("daysToCover")) for v in si.values()) if x is not None)
    return {"names": len(ch), "risingShare": round(sum(1 for _, c in ch if c >= 10) / len(ch), 4) if ch else None,
            "medianDaysToCover": dtc[len(dtc) // 2] if dtc else None,
            "abnormalVolumeShare": round(sum(1 for z in zs if z >= 2) / len(zs), 4) if zs else None,
            "bySector": {k: {"n": v[0], "risingShare": round(v[1] / v[0], 3)} for k, v in sorted(by.items()) if v[0] >= 15},
            "rule": "short interest up 10%+ since the previous settlement; abnormal volume = short share of volume z >= 2"}


def headcount_breadth(hc, today):
    if not isinstance(hc, dict):
        return None
    ys = []
    for v in (hc.get("tickers") or {}).values():
        y = _num((v or {}).get("yoyPct"))
        try:
            fresh = (today - _d(str((v or {}).get("date"))[:10])).days <= 460
        except (TypeError, ValueError):
            fresh = False
        if y is not None and fresh:
            ys.append(y)
    if len(ys) < 30:
        return None
    ys.sort()
    return {"companies": len(ys), "cuttingShare": round(sum(1 for y in ys if y <= -2) / len(ys), 4),
            "medianYoyPct": ys[len(ys) // 2], "rule": "latest reported headcount vs a year earlier (10-K cadence); cutting = -2% or worse"}


def credit_breadth(rt, books):
    if not isinstance(rt, dict) or not isinstance(rt.get("ratings"), dict):
        return None
    cos = [(t, v) for t, v in rt["ratings"].items() if isinstance(v, dict) and v.get("k") == "co" and v.get("n")]
    if len(cos) < 30:
        return None
    neg = [t for t, v in cos if (_num(v.get("sig")) or 0) <= -0.10]
    dn = sum(1 for _, v in cos if (_num(v.get("d30")) or 0) > 0)       # d30: notches moved in 30 days, + = downgrade
    up = sum(1 for _, v in cos if (_num(v.get("d30")) or 0) < 0)
    by = {}
    for t, v in cos:
        sec = (books.get(t) or {}).get("sector") or "Unknown"
        b = by.setdefault(sec, [0, 0])
        b[0] += 1
        b[1] += (_num(v.get("sig")) or 0) <= -0.10
    return {"companies": len(cos), "negTrendShare": round(len(neg) / len(cos), 4), "downgrades30": dn, "upgrades30": up,
            "hyShare": round(sum(1 for _, v in cos if int(v["n"]) >= 11) / len(cos), 4),
            "distressedShare": round(sum(1 for _, v in cos if int(v["n"]) >= 17) / len(cos), 4), "asOf": rt.get("date"),
            "bySector": {k: {"n": v[0], "negTrendShare": round(v[1] / v[0], 3)} for k, v in sorted(by.items()) if v[0] >= 15}}


def price_breadth(books):
    rows = []
    for t, r in books.items():
        p, h, lo = _num(r.get("price")), _num(r.get("high52")), _num(r.get("low52"))
        if not p or not h or not lo or h <= lo or str(r.get("currency") or "USD").upper() != "USD" or r.get("isetf"):
            continue
        rows.append((r.get("sector") or "Unknown", p >= 0.95 * h, p <= 1.10 * lo))
    if len(rows) < 100:
        return None
    by = {}
    for sec, hi, lo in rows:
        b = by.setdefault(sec, [0, 0, 0])
        b[0] += 1
        b[1] += hi
        b[2] += lo
    return {"names": len(rows), "nearHighsShare": round(sum(1 for r in rows if r[1]) / len(rows), 4),
            "nearLowsShare": round(sum(1 for r in rows if r[2]) / len(rows), 4),
            "bySector": {k: {"n": v[0], "nearHighs": round(v[1] / v[0], 3), "nearLows": round(v[2] / v[0], 3)} for k, v in sorted(by.items()) if v[0] >= 15},
            "rule": "near highs = within 5% of the 52-week high; near lows = within 10% of the 52-week low (US stocks, funds excluded)"}


def bottom_up(today, books=None, sec_dir=None, short=None, hc=None, options=None, ratings=None):
    books = books or {}
    out = {"sec": sec_breadth(sec_dir or DATA / "sec", books, today), "short": short_breadth(short, books),
           "headcount": headcount_breadth(hc, today), "credit": credit_breadth(ratings, books), "prices": price_breadth(books),
           "options": None}
    if isinstance(options, dict) and (options.get("breadth") or options.get("appSignal")):
        out["options"] = {"asOf": options.get("asOf"), "fresh": options.get("fresh"), "breadth": options.get("breadth"),
                          "appSignal": {k: (options.get("appSignal") or {}).get(k) for k in ("indexIV", "volRegime", "marketTrend", "count")},
                          "sectors": options.get("sectors") or {}}
    raw = {
        "secRatio": (out["sec"] or {}).get("ratio"),
        "insiderBuy": ((out["sec"] or {}).get("insiders") or {}).get("buyShare"),
        "shortRising": (out["short"] or {}).get("risingShare"),
        "headcountCutting": (out["headcount"] or {}).get("cuttingShare"),
        "optInverted": ((out["options"] or {}).get("breadth") or {}).get("invertedShare"),
        "optSkewHigh": ((out["options"] or {}).get("breadth") or {}).get("skewHighShare"),
        "optIvRankHigh": ((out["options"] or {}).get("breadth") or {}).get("ivRankHighShare"),
        "optPutLed": ((out["options"] or {}).get("breadth") or {}).get("unusualPutLedShare"),
        "creditNegTrend": (out["credit"] or {}).get("negTrendShare"),
        "nearLows": (out["prices"] or {}).get("nearLowsShare"),
        "nearHighs": (out["prices"] or {}).get("nearHighsShare"),
    }
    out["raw"] = {k: v for k, v in raw.items() if v is not None}
    return out


def score_bottom_up(bu, daily):
    """Each raw breadth reading -> 0-100: its percentile in our own daily history once 40+ days exist, else the
    provisional anchors (labelled). -> {metric: (score, basis)}, and the three sub-gauges."""
    hist = {}
    for row in daily or []:
        for k, v in ((row.get("bu") or {}).items()):
            if _num(v) is not None:
                hist.setdefault(k, []).append(float(v))
    sc = {}
    for k, v in (bu.get("raw") or {}).items():
        h = hist.get(k) or []
        if len(h) >= 40:
            e = Expanding()
            for x in h:
                e.add(x)
            sc[k] = (round(e.pct(v), 1), f"percentile of {len(h)} days")
        elif k in ANCHORS:
            sc[k] = (round(_interp(v, ANCHORS[k]), 1), "provisional anchors (building history)")

    def mean_of(keys):
        xs = [sc[k][0] for k in keys if k in sc]
        return round(sum(xs) / len(xs), 1) if xs else None
    return sc, {"market": mean_of(BU_MARKET), "fundamental": mean_of(BU_FUND), "upside": mean_of(BU_UP)}


# ------------------------------------------------------------------------------------------------------------------
#  plain English
# ------------------------------------------------------------------------------------------------------------------
def _fmt(v, unit):
    if v is None:
        return "—"
    if unit in ("k", "k SAAR"):
        return f"{v:,.0f}k"
    if unit in ("$bn", "$bn SAAR", "$bn 2017"):
        return f"${v:,.0f}bn"
    if unit == "$m":
        return f"${v / 1000:,.0f}bn"
    if unit == "%":
        return f"{v:.2f}%"
    if unit == "% of GDP":
        return f"{v:.1f}% of GDP"
    if unit == "net %":
        return f"{v:+.1f}% net"
    return f"{v:.2f}"


def _tf_text(tf, x):
    if x is None:
        return ""
    if tf == "yoy_diff":
        return f"{x:+.2f} pt in a year"
    if tf == "yoy_pct":
        return f"{x:+.1f}% y/y"
    if tf == "chg6":
        return f"{x:+.2f} in 6 months"
    if tf == "yoy_gap":
        return f"{x:+.1f} pts (growth gap, y/y)"
    return ""


def ordinal(n):
    n = int(round(n))
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def indicator_rows(S, now_scores, now_x, first=None):
    first = first or {}
    out = []
    for spec in IND:
        k, pillar, label, src, tf, dr, w, grp = spec
        sid = src[0] if isinstance(src, tuple) else src
        meta = SERIES.get(sid) or SERIES.get(DERIVED.get(sid, (None, sid))[1]) or ("", "", "", 0, "")
        v, d = _raw_now(spec, S)
        out.append({"key": k, "pillar": pillar, "label": label, "series": src if isinstance(src, str) else "/".join(src),
                    "transform": tf, "dir": dr, "w": w, "group": grp, "value": None if v is None else round(v, 4),
                    "unit": meta[1] if sid not in DERIVED else DERIVED_UNIT.get(sid, ""),
                    "date": d.isoformat() if d else None, "x": None if now_x.get(k) is None else round(now_x[k], 4),
                    "score": None if now_scores.get(k) is None else round(now_scores[k], 1),
                    "since": first[k].year if first.get(k) else None})
    return out


def drivers_text(rows, pillar, top=2):
    rs = [r for r in rows if r["pillar"] == pillar and r["score"] is not None]
    rs.sort(key=lambda r: -(r["score"] * r["w"]))
    parts = []
    for r in rs[:top]:
        # a level is worth showing when it reads on its own (a rate, claims in thousands); an index or a dollar stock
        # isn't - its change is the story
        show = r["transform"] in ("level", "yoy_diff", "chg6") or r["unit"] in ("k", "%", "pts", "net %", "months", "% of GDP")
        val = _fmt(r["value"], r["unit"]) if (show and r["transform"] != "yoy_gap") else ""
        chg = _tf_text(r["transform"], r["x"]) if r["transform"] != "level" else ""
        what = ", ".join(p for p in (val, chg) if p)
        kind = "growth" if pillar == "growth" else "stress"
        since = f" since {r['since']}" if r.get("since") else ""
        parts.append(f"{r['label']} {what} ({ordinal(r['score'])} percentile of {kind}{since})".replace("  ", " "))
    return "; ".join(parts)


def seasonal_traps(S, now, rows):
    """A short-term improvement against a worsening year-over-year trend - the tax-refund trap. Only for series that
    describe stress (loss data, labour, collateral)."""
    out = []
    for spec in IND:
        k, pillar, label, src, tf, dr, w, grp = spec
        if pillar not in ("loss", "labor", "collateral") or tf not in ("yoy_diff", "yoy_pct") or not isinstance(src, str):
            continue
        s = S.get(src)
        if not s or len(s.vals) < 16:
            continue
        step = {"q": 1, "m": 1, "w": 4, "d": 21}.get(s.freq, 1)
        v_now, v_prev = s.vals[-1], s.vals[-1 - step]
        x = transformed(spec, S, now, lag0=True)
        if x is None:
            continue
        short = (v_now - v_prev) * dr            # < 0 = improving
        worse_yoy = x * dr > 0                    # > 0 = worse than a year ago
        if short < 0 and worse_yoy:
            m = s.dates[-1].month
            season = ("tax-refund season (Feb-Apr) - consumer delinquencies improve every spring" if (grp == "consumer" and m in (1, 2, 3, 4))
                      else "holiday hiring season" if (pillar == "labor" and m in (11, 12)) else None)
            out.append({"key": k, "label": label, "date": s.dates[-1].isoformat(), "season": season,
                        "text": f"{label}: the latest print improved, but it is still worse than a year ago ({_tf_text(tf, x)})"
                                + (f" - {season}; the year-over-year change is what counts." if season else
                                   " - check seasonality before calling a turn; the year-over-year change is what counts.")})
    return out


def marginal_buyer(scores, rows):
    keys = {"cp": "commercial-paper funding spread", "abcp": "asset-backed paper outstanding shrinking", "sloos_ci": "banks tightening business credit",
            "sloos_cc": "banks tightening credit cards", "nfci_cr": "credit conditions (NFCI)", "baa_d": "Baa spread widening", "hy": "high-yield spread"}
    hot = [(keys[k], scores[k]) for k in keys if (scores.get(k) or 0) >= 80]
    if len(hot) >= 2:
        return {"kind": "marginal-buyer", "severity": "high" if len(hot) >= 3 else "medium",
                "text": "The marginal buyer is stepping back: " + "; ".join(f"{t} ({ordinal(s)} pct)" for t, s in hot)
                        + ". When the buyers of risk stop (summer 2007: the German regional banks), refinancing stops and losses surface fast."}
    return None


# ------------------------------------------------------------------------------------------------------------------
#  sector exposure (the bot reads this)
# ------------------------------------------------------------------------------------------------------------------
def exposures(now_scores, bu):
    """Which industries the current warnings hit. Top-down: each group's mean indicator stress above 55 -> intensity
    (stress - 55) / 45 x the industry's link strength. Bottom-up: a sector whose options / filings / short interest /
    credit / 52-week-low breadth runs hot. -> {"industry": {name: {e, why}}, "sector": {name: {e, why}}}"""
    grp_scores = {}
    for spec in IND:
        if spec[7] and spec[1] != "growth" and now_scores.get(spec[0]) is not None:
            grp_scores.setdefault(spec[7], []).append((now_scores[spec[0]], spec[6], spec[2]))
    out = {"industry": {}, "sector": {}}

    def put(kind, name, e, why):
        cur = out[kind].get(name)
        if e > 0.02 and (not cur or e > cur["e"]):
            out[kind][name] = {"e": round(min(1.0, e), 3), "why": why}
    for g, xs in grp_scores.items():
        s = sum(v * w for v, w, _ in xs) / sum(w for _, w, _ in xs)
        if s <= 55:
            continue
        top = max(xs, key=lambda x: x[0] * x[1])
        for kind, name, strength in EXPOSURE.get(g, []):
            put(kind, name, (s - 55) / 45 * strength, f"{GROUP_TEXT[g]} stress {s:.0f}/100 (lead: {top[2]})")
    # bottom-up by sector
    secs = {}

    def add(sec, metric, val, anchor):
        if sec and sec != "Unknown" and val is not None:
            secs.setdefault(sec, []).append((metric, _interp(val, ANCHORS[anchor])))
    for sec, v in (((bu.get("options") or {}).get("sectors")) or {}).items():
        if (v.get("n") or 0) < 15:
            continue
        add(sec, "options term structure inverted", v.get("invertedShare"), "optInverted")
        add(sec, "options downside skew", v.get("skewHighShare"), "optSkewHigh")
    for sec, v in (((bu.get("sec") or {}).get("bySector")) or {}).items():
        add(sec, "8-K red flags vs base rate", v.get("ratio"), "secRatio")
    for sec, v in (((bu.get("short") or {}).get("bySector")) or {}).items():
        add(sec, "short interest rising", v.get("risingShare"), "shortRising")
    for sec, v in (((bu.get("credit") or {}).get("bySector")) or {}).items():
        add(sec, "credit trend negative", v.get("negTrendShare"), "creditNegTrend")
    for sec, v in (((bu.get("prices") or {}).get("bySector")) or {}).items():
        add(sec, "names near 52-week lows", v.get("nearLows"), "nearLows")
    for sec, xs in secs.items():
        if len(xs) < 2:
            continue
        s = sum(x[1] for x in xs) / len(xs)
        if s > 60:
            hot = sorted(xs, key=lambda x: -x[1])[:2]
            put("sector", sec, (s - 60) / 40, "bottom-up " + ", ".join(f"{m} {v:.0f}/100" for m, v in hot))
    return out


# ------------------------------------------------------------------------------------------------------------------
#  main
# ------------------------------------------------------------------------------------------------------------------
def _round(v, k=1):
    return None if v is None else round(v, k)


def run(today=None, S=None, bu_inputs=None, daily=None, spy=None):
    today = today or date.today()
    S = load_series() if S is None else S
    rows, ind_hist, g_hist = build_history(S, today)
    # flags per month (for the backtest): blindness = fundamentals in their top third and rising over 6 months while
    # market stress sits in its bottom half
    for i, r in enumerate(rows):
        f6 = rows[i - 6].get("fundamental") if i >= 6 else None
        f3 = rows[i - 3].get("fundamental") if i >= 3 else None
        g3 = rows[i - 3].get("growth") if i >= 3 else None
        mmax = max((x.get("marketP") or 0) for x in rows[max(0, i - 12):i + 1]) if i else None
        lead, lname = lead_pillar(r["pillars"])
        l6 = rows[i - 6]["pillars"].get(lname) if (i >= 6 and lname) else None
        l3 = rows[i - 3]["pillars"].get(lname) if (i >= 3 and lname) else None
        r["blindRaw"] = _blind(r.get("fundamentalP"), None if f6 is None or r.get("fundamental") is None else r["fundamental"] - f6,
                               r.get("marketP"), lead, None if l6 is None or lead is None else lead - l6)
        r["blind"] = bool(r["blindRaw"] and i and rows[i - 1].get("blindRaw"))        # two months running, not one noisy print
        r["regime"], _ = classify(r.get("fundamentalP"), r.get("marketP"), r.get("fragilityP"), r.get("growthP"),
                                  None if f3 is None or r.get("fundamental") is None else r["fundamental"] - f3,
                                  None if g3 is None or r.get("growth") is None else r["growth"] - g3, mmax,
                                  lead, lname, None if l3 is None or lead is None else lead - l3)
    # the live read: everything on file counts (lag 0), scored against the monthly history
    now_x, now_scores = {}, {}
    for spec in IND:
        x = transformed(spec, S, today, lag0=True)
        if x is None:
            continue
        now_x[spec[0]] = x
        h = ind_hist[spec[0]]
        if len(h.xs) >= MIN_MONTHS:
            p = h.pct(x)
            now_scores[spec[0]] = p if spec[5] > 0 else 100 - p
    g = gauges(now_scores)
    gp = {k: (g_hist[k].pct(g.get(k)) if len(g_hist[k].xs) >= MIN_MONTHS and g.get(k) is not None else None)
          for k in ("fundamental", "market", "fragility", "growth", "composite")}
    last = rows[-1] if rows else {}
    r3 = rows[-3] if len(rows) >= 3 else {}
    r6 = rows[-6] if len(rows) >= 6 else {}
    d_f3 = None if g.get("fundamental") is None or r3.get("fundamental") is None else g["fundamental"] - r3["fundamental"]
    d_f6 = None if g.get("fundamental") is None or r6.get("fundamental") is None else g["fundamental"] - r6["fundamental"]
    d_g3 = None if g.get("growth") is None or r3.get("growth") is None else g["growth"] - r3["growth"]
    mmax = max([(x.get("marketP") or 0) for x in rows[-12:]] + [gp["market"] or 0]) if rows else None
    lead, lname = lead_pillar(g["pillars"])
    l3 = (r3.get("pillars") or {}).get(lname) if lname else None
    l6 = (r6.get("pillars") or {}).get(lname) if lname else None
    d_l3 = None if lead is None or l3 is None else lead - l3
    d_l6 = None if lead is None or l6 is None else lead - l6
    regime, why = classify(gp["fundamental"], gp["market"], gp["fragility"], gp["growth"], d_f3, d_g3, mmax, lead, lname, d_l3)

    # bottom-up
    bu_inputs = bu_inputs if bu_inputs is not None else {}
    bu = bottom_up(today, books=bu_inputs.get("books"), sec_dir=bu_inputs.get("sec_dir"), short=bu_inputs.get("short"),
                   hc=bu_inputs.get("headcount"), options=bu_inputs.get("options"), ratings=bu_inputs.get("ratings"))
    bu_sc, bu_g = score_bottom_up(bu, daily)
    bu["scores"] = {k: {"score": v[0], "basis": v[1]} for k, v in bu_sc.items()}
    bu["gauges"] = bu_g

    risk = gp["composite"]
    degraded = risk is None
    if degraded:
        # no FRED history on file (first run / FRED down): a provisional read from the bottom-up layer alone
        xs = [v for v in (bu_g.get("market"), bu_g.get("fundamental")) if v is not None]
        risk = round(sum(xs) / len(xs), 1) if xs else None

    ind_rows = indicator_rows(S, now_scores, now_x, ind_hist.get("_first"))
    since = next((r["t"].year for r in rows if r.get("compositeP") is not None), None)
    flags = []
    blind = bool(_blind(gp["fundamental"], d_f6, gp["market"], lead, d_l6) and (last.get("blindRaw") or last.get("blind")))
    bu_calm = bu_g.get("market") is not None and bu_g["market"] < 45
    if blind:
        what = (f"fundamentals are in the {ordinal(gp['fundamental'])} percentile of stress and worsened {d_f6:+.0f} points in six months"
                if (gp["fundamental"] or 0) >= BLIND_F_PCT and (d_f6 or 0) >= BLIND_F_RISE else
                f"{PILLARS[lname][0].lower()} is at the {ordinal(lead)} percentile of stress, {d_l6:+.0f} points in six months "
                f"({drivers_text(ind_rows, lname, 1)})")
        flags.append({"kind": "blindness", "severity": "high",
                      "text": f"Market blindness: {what}, while funding markets sit in the {ordinal(gp['market'])} percentile"
                              + (" and the options market is calm" if bu_calm else "")
                              + ". The warning is published; the market isn't pricing it."})
    mb = marginal_buyer(now_scores, ind_rows)
    if mb:
        flags.append(mb)
    traps = seasonal_traps(S, today, ind_rows)
    for t in traps[:4]:
        flags.append({"kind": "seasonal", "severity": "info", "text": t["text"]})
    if (now_scores.get("curve") or 0) >= 85:
        flags.append({"kind": "curve", "severity": "medium", "text": "The yield curve is inverted or near its flattest - historically a 6-24 month lead on recessions, "
                                                                     "and the re-steepening that follows has often come right before the downturn."})
    if gp["fragility"] is not None and gp["fragility"] >= 75 and gp["growth"] is not None and gp["growth"] >= 50:
        flags.append({"kind": "debt-fuelled", "severity": "medium",
                      "text": "Growth is leaning on leverage: " + (drivers_text(ind_rows, "fragility") or "credit outrunning income") + "."})
    if regime == "organic expansion":
        flags.append({"kind": "organic", "severity": "positive",
                      "text": "Organic growth: " + (drivers_text(ind_rows, "growth") or "income, jobs and output rising") + " - without a credit build-up."})
    ins = ((bu.get("sec") or {}).get("insiders") or {})
    if ins.get("buyShare") is not None and bu_sc.get("insiderBuy", (0,))[0] >= 75:
        flags.append({"kind": "insiders", "severity": "positive",
                      "text": f"Insiders are buying: {ins['netBuyers']} of {ins['companiesWithActivity']} companies with insider trades show net buying "
                              f"({ins['clusterBuys']} cluster buys) - management putting its own money in."})
    secb = bu.get("sec") or {}
    if secb.get("ratio") and secb["ratio"] >= 1.6:
        names = ", ".join(f"{r['ticker']} ({', '.join(r['what'])})" for r in secb.get("recent", [])[:4])
        flags.append({"kind": "filings", "severity": "medium",
                      "text": f"8-K red flags are running {secb['ratio']:.1f}x their base rate (bankruptcy, debt acceleration, non-reliance, impairments)"
                              + (f": {names}" if names else "") + "."})

    lvl = level_of(risk)
    head_bits = [f"{lvl.upper()} risk ({ordinal(risk)} percentile of the months since {since})" if risk is not None and not degraded
                 else (f"{lvl.upper()} (provisional - bottom-up only, no FRED history yet)" if risk is not None else "NO READ (no data yet)"),
                 f"regime: {regime}"]
    if blind:
        head_bits.append("market blindness")
    if mb:
        head_bits.append("marginal buyer stepping back")
    notes = []
    for p, (label, what, idx, _) in PILLARS.items():
        ps = g["pillars"].get(p)
        if ps is None:
            continue
        notes.append(f"{label} ({what}): {ps:.0f}/100 - {drivers_text(ind_rows, p) or 'no scored series'}.")

    exp = exposures(now_scores, bu)
    bt = backtest(rows, S, spy)
    hist = [[r["t"].isoformat()[:7], _round(r.get("compositeP")), _round(r.get("fundamental")), _round(r.get("market")),
             _round(r.get("fragility")), _round(r.get("growth")), r.get("regime"), 1 if r.get("blind") else 0]
            for r in rows if r.get("composite") is not None]
    have = sorted(S)
    out = {
        "asOf": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "date": today.isoformat(), "build": BUILD,
        "risk": _round(risk), "level": lvl, "provisional": degraded, "regime": regime, "regimeWhy": why,
        "headline": " · ".join(head_bits),
        "gauges": {k: {"score": _round(g.get(k)), "pct": _round(gp[k]), "chg3m": _round(d_f3) if k == "fundamental" else None,
                       "chg6m": _round(d_f6) if k == "fundamental" else None} for k in ("fundamental", "market", "fragility", "growth", "composite")},
        "pillars": {p: {"label": PILLARS[p][0], "what": PILLARS[p][1], "gauge": PILLARS[p][2], "score": _round(g["pillars"].get(p)),
                        "n": sum(1 for spec in IND if spec[1] == p and now_scores.get(spec[0]) is not None),
                        "of": sum(1 for spec in IND if spec[1] == p)} for p in PILLARS},
        "indicators": ind_rows, "flags": flags, "notes": notes, "traps": traps,
        "bottomUp": bu, "exposures": exp, "backtest": bt, "history": hist,
        "coverage": {"series": len(have), "of": len([s for s in SERIES if SERIES[s][4] != "outcome"]) + 2,
                     "missing": sorted(s for s in SERIES if s not in S), "scored": len(now_scores), "indicators": len(IND)},
        "method": ("Each indicator is scored against its own history (expanding window, publication lags respected: no hindsight); "
                   "pillars average their indicators, the gauges average their pillars; risk = the composite's percentile vs its own "
                   "history. Bottom-up readings are scored against our daily history once 40 days exist (provisional anchors before)."),
        "caveats": ["Backtest uses today's revised data, not what was published at the time (revisions can be large around turns).",
                    "Publication lags are approximations per series.",
                    "Few recessions in the sample (1990, 2001, 2008, 2020): hit rates are indicative, not statistics.",
                    "The 2020 recession was an exogenous shock - no credit indicator could have led it.",
                    "A warning is a probability shift, not a timing call: 2006's data were early by a year."],
    }
    return out, rows, bu


def main():
    today = date.fromisoformat(os.environ["WARNING_NOW"]) if os.environ.get("WARNING_NOW") else datetime.now(timezone.utc).date()
    S = load_series()
    print(f"warning signals {today}: {len(S)} series on file")
    books = load_books()
    daily = []
    try:
        daily = json.loads(DAILY_P.read_text()).get("rows") or []
    except Exception:
        pass
    short = hc = None
    try:
        short = json.loads((DATA / "short" / "index.json").read_text())
    except Exception:
        pass
    try:
        hc = json.loads((DATA / "headcount" / "index.json").read_text())
    except Exception:
        pass
    options = _get_json(f"{RAW}/TRAPP2-OPTIONS/main/data/options/market.json")
    ratings = _get_json(f"{RAW}/TRAPP2-CREDIT/main/data/ratings.json", 60)
    spy_raw = _get_json(f"{RAW}/TRAPP2-1/main/data/history/SPY.json", 60) or []
    spy = sorted((_d(b["date"]), float(b["close"])) for b in spy_raw if isinstance(b, dict) and b.get("date") and b.get("close"))
    out, rows, bu = run(today, S, {"books": books, "sec_dir": DATA / "sec", "short": short, "headcount": hc, "options": options,
                                   "ratings": ratings}, daily, spy)
    OUT_P.parent.mkdir(parents=True, exist_ok=True)
    OUT_P.write_text(json.dumps(out, separators=(",", ":")))
    row = {"d": today.isoformat(), "risk": out["risk"], "level": out["level"], "regime": out["regime"],
           "g": {k: v["score"] for k, v in out["gauges"].items()}, "bu": bu.get("raw") or {}}
    daily = [r for r in daily if r.get("d") != row["d"]] + [row]
    DAILY_P.write_text(json.dumps({"rows": daily[-DAILY_KEEP:]}, separators=(",", ":")))
    print(out["headline"])
    for n in out["notes"]:
        print("  " + n)
    for f in out["flags"]:
        print(f"  [{f['kind']}] {f['text']}")
    b = out["backtest"]
    if b.get("hit"):
        print(f"  backtest: risk >= 75th pct -> hit {b['hit']['hitRate']}% vs base {b['hit']['baseRate']}% ({b['hit']['signalMonths']} months)")
    for r in b.get("recessions") or []:
        print(f"  recession {r['start']}: risk 12m before {r['riskAt12m']}, 6m {r['riskAt6m']}, lead {r['leadMonths']} months")
    _step_summary(out)
    return 0


def _step_summary(out):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if not p:
        return
    lines = [f"### Early warning · {out['date']}", "", f"**{out['headline']}**", ""]
    lines += [f"- {n}" for n in out["notes"]]
    lines += [""] + [f"- **{f['kind']}**: {f['text']}" for f in out["flags"]]
    cov = out["coverage"]
    lines += ["", f"Series on file: {cov['series']} · missing: {', '.join(cov['missing']) or 'none'}"]
    try:
        with open(p, "a") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
