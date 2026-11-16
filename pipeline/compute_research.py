#!/usr/bin/env python3
"""
compute_research.py — backend mirror of the frontend's Research-tab engine.

Pulls master.json from all three data repos, normalizes pipeline field names,
computes the SAME 23 metrics / percentile ranks / letter grades the app
computes, and writes:

    data/research_grades.json
      { generatedAt, universeSize, metrics: [...], byTicker: { T: {
          gradeScore, grade, coverage, ranks: { key: {rank,total,pct,value} } } } }

The frontend then prefers these backend values, keeps its own computation as a
cross-check, and FLAGS any divergence — backend computes, frontend verifies.
Formulas and grade thresholds are kept 1:1 with the app so a divergence means
DATA drift, not method drift.

z187 - back in step with the app, in one currency:
  * the app's CURRENT method: 29 metrics (adds current / quick / payout ratio, PEG, fund expense ratio and - new in both -
    Price / Sales), category-weighted grade (Profitability .28 · Growth .20 · Valuation .20 · Balance Sheet .16 ·
    Income .10 · Cost .06; Scale is context, not graded), per-metric weights inside Valuation (P/E .35, P/B .40,
    P/S .50, EV/EBITDA 1.3, EV/Revenue 1.2) and the P/E / P/B dampening. This file still averaged 23 metrics flat,
    size included - so the grade the BOT trades on favoured big companies and disagreed with the app's.
  * USD: statements converted from the reporting currency (financialCurrency), market cap from the quote currency -
    the app's ensureRowNormalized, plus stock-based comp / goodwill / intangibles. TSM ranked #19 on revenue in TWD,
    its cash / market cap was TWD over USD (1.9 -> ~0.06).
  * market-cap multiples (P/B, P/S, EV ratios, cash / market cap) are left out for a row whose market cap disagrees with
    its own P/E (valuationNote "mcap?": an ADR ratio or a single share class); P/E, per share, still counts.
  * percentiles round like the app (half up), 1-year return = 252 sessions, Sharpe annualised arithmetically.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# v2: reads from LOCAL checkouts (the workflow checks out all three data repos
# into books/) — no HTTP fragility, and direct access to data/history/*.json
# for technicals + non-equity grading. Falls back to raw URLs when a checkout
# is missing (e.g. running locally).
import math

# Repo owner — resolved at runtime so the pipeline follows the repos to any
# GitHub account. Actions sets GITHUB_REPOSITORY_OWNER automatically;
# VALUATIO_OWNER (repo variable/env) overrides; TheMostLocal is the fallback.
_GH_OWNER = (__import__("os").environ.get("VALUATIO_OWNER")
             or __import__("os").environ.get("GITHUB_REPOSITORY_OWNER")
             or "TheMostLocal").strip()

BOOKS = ["books/TRAPP2", "books/TRAPP2-1", "books/TRAPP2-2", "books/TRAPP2-3", "books/TRAPP2-4"]   # z178: + TRAPP2-4
REPOS = [
    f"https://raw.githubusercontent.com/{_GH_OWNER}/TRAPP2/main/data/master.json",
    f"https://raw.githubusercontent.com/{_GH_OWNER}/TRAPP2-1/main/data/master.json",
    f"https://raw.githubusercontent.com/{_GH_OWNER}/TRAPP2-2/main/data/master.json",
    f"https://raw.githubusercontent.com/{_GH_OWNER}/TRAPP2-3/main/data/master.json",
    f"https://raw.githubusercontent.com/{_GH_OWNER}/TRAPP2-4/main/data/master.json",   # z178
]
OUT = Path(__file__).resolve().parent.parent / "data" / "research_grades.json"
FX_URL = f"https://raw.githubusercontent.com/{_GH_OWNER}/TRAPP2-1/main/data/fx/rates.json"
SUBUNIT = {"GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01), "ZAc": ("ZAR", 0.01), "ZAC": ("ZAR", 0.01),
           "ILA": ("ILS", 0.01), "ILa": ("ILS", 0.01), "USX": ("USD", 0.01), "USd": ("USD", 0.01)}
# statement (reporting-currency) money fields - the app's ensureRowNormalized list + SBC, goodwill, intangibles (z187)
FIN_MONEY = ("totalAssets", "totalLiabilities", "totalEquity", "bookValue", "cash", "totalDebt", "freeCashFlow", "revenue",
             "netIncome", "ebitda", "stockBasedComp", "goodwill", "intangibleAssets", "intangibles")

# Mirror of the app's normalizeRowFields (pipeline snake_case → app camelCase).
ALIASES = {
    "dividend_yield": "dividendYield", "marketcap": "marketCap",
    "changepct": "changesPercentage", "closeyest": "priorClose",
    "priceopen": "open", "volumeavg": "avgVolume", "web_url": "website",
}

# Symbol patterns that the app classifies as fx/index/future/crypto — those are
# excluded from fundamental ranking (ETFs stay IN, matching the frontend).
def is_unrankable(ticker):
    t = ticker.upper()
    return ("=" in t or t.startswith("^") or t.endswith("-USD")
            or t.endswith("-USDT") or t.endswith("=F") or t.endswith("=X"))


def num(v):
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def normalize(row):
    for snake, camel in ALIASES.items():
        if row.get(snake) not in (None, "") and row.get(camel) in (None, ""):
            row[camel] = row[snake]
    # dividend_yield arrives as PERCENT in the pipeline; app treats it as decimal.
    dy = num(row.get("dividendYield"))
    if dy is not None and row.get("dividend_yield") not in (None, "") and dy == num(row.get("dividend_yield")):
        row["dividendYield"] = dy / 100.0
    return row


def rotce(r):
    ni, eq = num(r.get("netIncome")), num(r.get("totalEquity"))
    if ni is None or not eq:
        return None
    tangible = eq
    intang = num(r.get("intangibleAssets")) or num(r.get("intangibles"))
    gw = num(r.get("goodwill"))
    if intang is not None:
        tangible -= intang
    if gw is not None:
        tangible -= gw
    if tangible <= 0:
        return None          # not meaningful for negative tangible equity
    return ni / tangible


def ratio(a, b):
    a, b = num(a), num(b)
    return (a / b) if (a is not None and b and b > 0) else None


def pos(v):
    v = num(v)
    return v if (v is not None and v > 0) else None


def load_fx():
    """USD per unit of each currency - TRAPP2-1/data/fx/rates.json (the rates the app and the bot use)."""
    d = None
    try:
        d = json.loads((Path("books/TRAPP2-1") / "data" / "fx" / "rates.json").read_text())
    except Exception:
        try:
            with urllib.request.urlopen(urllib.request.Request(FX_URL, headers={"User-Agent": "ValuatioAnalytics"}), timeout=30) as r:
                d = json.load(r)
        except Exception as e:
            print(f"  FX rates unavailable ({e}) - only USD rows convert", file=sys.stderr)
    out = {"USD": 1.0}
    for c, v in ((d or {}).get("rates") or {}).items():
        up = num(v.get("usdPer")) if isinstance(v, dict) else num(v)
        if up and up > 0:
            out[str(c).upper()] = up
    return out


def _main_ccy(c):
    c = str(c or "").strip()
    return SUBUNIT.get(c, (c.upper(), 1.0)) if c else ("USD", 1.0)


def to_usd(row, fx):
    """z187 - the app's ensureRowNormalized: market cap from the quote currency's main unit, statements from the
    reporting currency, both to USD (the pipeline's fxQuote / fxFin when the row carries them). A currency with no
    rate leaves that side's money fields out (None) instead of mixing currencies. Ratios are never touched."""
    q, _ = _main_ccy(row.get("currency"))
    f, _ = _main_ccy(row.get("financialCurrency") or row.get("currency"))
    rq = num(row.get("fxQuote")) or fx.get(q)
    rf = num(row.get("fxFin")) or fx.get(f)
    if q != "USD":
        mc = num(row.get("marketCap"))
        row["marketCap"] = (mc * rq) if (mc is not None and rq) else None
    if f != "USD":
        for k in FIN_MONEY:
            v = num(row.get(k))
            if v is not None:
                row[k] = (v * rf) if rf else None
    row["_usd"] = bool((q == "USD" or rq) and (f == "USD" or rf))
    return row


def _suspect(r):
    return "mcap?" in str(r.get("valuationNote") or "")


def _legacy(r):
    """A row written before z187 (no valuationNote): its P/B and EV ratios are Yahoo's, which divide a USD price by
    local-currency statements for ADRs (TSM P/B 76, ASML 1,284) and a class-B price by class-A book (BRK-B 0.001)."""
    return "valuationNote" not in r


def _cross_ccy(r):
    return _main_ccy(r.get("currency"))[0] != _main_ccy(r.get("financialCurrency") or r.get("currency"))[0]


def price_to_book(r):
    """The pipeline's P/B (z187: market cap / latest-quarter common equity, USD, at the row's price); a row from before
    z187: market cap / total equity, both in USD here - never Yahoo's."""
    if _suspect(r):
        return None
    if not _legacy(r):
        return pos(r.get("priceToBook"))
    if not r.get("_usd"):
        return None
    mc, eq = num(r.get("marketCap")), num(r.get("totalEquity"))
    return (mc / eq) if (mc and mc > 0 and eq and eq > 0) else None


def _mc_multiple(field):
    """EV / EBITDA, EV / Revenue: the pipeline's (z187, USD); a row from before z187 only when it reports in the currency
    it trades in (Yahoo's cross-currency EV ratios are wrong - ASML EV / Revenue 768)."""
    def get(r):
        if _suspect(r) or (_legacy(r) and _cross_ccy(r)):
            return None
        return pos(r.get(field))
    return get


def price_to_sales(r):
    """The pipeline's P/S (z187: at the row's price, both sides in USD); a row from before z187: market cap / revenue,
    both converted to USD here."""
    if _suspect(r):
        return None
    v = pos(r.get("priceToSales"))
    if v is not None:
        return v
    if not _legacy(r) or not r.get("_usd"):
        return None
    return ratio(r.get("marketCap"), r.get("revenue"))


def div_yield(r):
    v = num(r.get("dividendYield"))
    if v is not None and v > 0:
        return v if v < 0.5 else (v / 100 if v <= 40 else None)
    raw = num(r.get("dividend_yield"))
    return raw / 100 if (raw is not None and 0 < raw <= 40) else None


def payout(r):
    v = num(r.get("payoutRatio"))
    if v is None or v <= 0:
        return None
    if v > 2:
        v = v / 100
    return v if 0 < v <= 2 else None


def expense_ratio(r):
    raw = r.get("expenseRatio")
    if raw in (None, ""):
        raw = r.get("expense ratio") if r.get("expense ratio") not in (None, "") else r.get("expenseratio")
    try:
        v = float(str(raw).replace("%", "").replace(",", "").strip()) if raw not in (None, "") else None
    except ValueError:
        v = None
    if v is None or v != v or v <= 0 or v > 20:
        return None
    if v > 0.05:
        v = v / 100
    return v if 0 < v <= 0.20 else None


def peg(r):
    v = num(r.get("pegRatio"))
    return v if (v is not None and 0 < v < 10) else None


# (key, category, higher_is_better, getter) - 1:1 with the app's RESEARCH_METRICS (z187 adds Price / Sales to both)
METRICS = [
    ("roe",          "Profitability", True,  lambda r: num(r.get("returnOnEquity"))),
    ("rotce",        "Profitability", True,  rotce),
    ("roa",          "Profitability", True,  lambda r: num(r.get("returnOnAssets"))),
    ("grossMargin",  "Profitability", True,  lambda r: num(r.get("grossMargin"))),
    ("opMargin",     "Profitability", True,  lambda r: num(r.get("operatingMargin"))),
    ("netMargin",    "Profitability", True,  lambda r: num(r.get("profitMargin"))),
    ("fcfMargin",    "Profitability", True,  lambda r: (lambda rev, fcf: fcf / rev if (rev and rev > 0 and fcf is not None) else None)(num(r.get("revenue")), num(r.get("freeCashFlow")))),
    ("sbcPctRev",    "Profitability", False, lambda r: (lambda rev, sbc: sbc / rev if (rev and rev > 0 and sbc is not None and sbc > 0) else None)(num(r.get("revenue")), num(r.get("stockBasedComp")))),
    ("revGrowth",    "Growth",        True,  lambda r: num(r.get("revenueGrowth"))),
    ("epsGrowth",    "Growth",        True,  lambda r: num(r.get("earningsGrowth"))),
    ("pe",           "Valuation",     False, lambda r: pos(r.get("pe"))),
    ("pb",           "Valuation",     False, price_to_book),
    ("ps",           "Valuation",     False, price_to_sales),
    ("evEbitda",     "Valuation",     False, _mc_multiple("evToEbitda")),
    ("evRev",        "Valuation",     False, _mc_multiple("evToRevenue")),
    ("debtEquity",   "Balance Sheet", False, lambda r: ratio(r.get("totalDebt"), r.get("totalEquity"))),
    ("cashRatio",    "Balance Sheet", True,  lambda r: None if _suspect(r) else (lambda c, m: c / m if (c is not None and m and m > 0) else None)(num(r.get("cash")), num(r.get("marketCap")))),
    ("currentRatio", "Balance Sheet", True,  lambda r: pos(r.get("currentRatio"))),
    ("quickRatio",   "Balance Sheet", True,  lambda r: pos(r.get("quickRatio"))),
    ("divYield",     "Income",        True,  div_yield),
    ("payoutRatio",  "Income",        False, payout),
    ("expenseRatio", "Cost",          False, expense_ratio),
    ("peg",          "Valuation",     False, peg),
    ("revenue",      "Scale",         True,  lambda r: num(r.get("revenue"))),
    ("netIncome",    "Scale",         True,  lambda r: num(r.get("netIncome"))),
    ("ebitda",       "Scale",         True,  lambda r: num(r.get("ebitda"))),
    ("fcf",          "Scale",         True,  lambda r: num(r.get("freeCashFlow"))),
    ("marketCap",    "Scale",         True,  lambda r: num(r.get("marketCap"))),
    ("totalAssets",  "Scale",         True,  lambda r: num(r.get("totalAssets"))),
]
# the app's RESEARCH_CATEGORY_WEIGHTS / RESEARCH_METRIC_WEIGHTS / RESEARCH_METRIC_DAMPEN (Scale is not graded)
CATEGORY_WEIGHTS = {"Profitability": 0.28, "Growth": 0.20, "Valuation": 0.20, "Balance Sheet": 0.16, "Income": 0.10, "Cost": 0.06}
METRIC_WEIGHTS = {"pe": 0.35, "pb": 0.4, "ps": 0.5, "evEbitda": 1.3, "evRev": 1.2}
DAMPEN = {"pe": 0.78, "pb": 0.72}


def js_round(x):
    """Math.round (half up) - Python's round() is half-to-even, so ties ranked a point apart from the app."""
    return int(math.floor(x + 0.5))


def rank_metric(vals, higher):
    """[(ticker, value)] -> {ticker: {rank, total, pct, value}} - the app's ranking (stable sort, 100 = best)."""
    vals = sorted(vals, key=lambda x: x[1], reverse=higher)
    total = len(vals)
    return {t: {"rank": i + 1, "total": total, "pct": js_round((1 - i / (total - 1)) * 100) if total > 1 else 100, "value": v}
            for i, (t, v) in enumerate(vals)}


def category_grade(ranks):
    """The app's computeResearchRankings grade: dampen, weight inside the category, weight the categories, renormalised
    over the categories the ticker has."""
    cat_sum, cat_w = {}, {}
    for key, cat, _, _ in METRICS:
        e = ranks.get(key)
        if not e or cat not in CATEGORY_WEIGHTS:
            continue
        pct = e["pct"]
        d = DAMPEN.get(key, 0)
        if d:
            pct = pct + (50 - pct) * d
        w = METRIC_WEIGHTS.get(key, 1.0)
        cat_sum[cat] = cat_sum.get(cat, 0.0) + pct * w
        cat_w[cat] = cat_w.get(cat, 0.0) + w
    scores, num_, den = {}, 0.0, 0.0
    for cat, w in CATEGORY_WEIGHTS.items():
        if cat_w.get(cat):
            avg = cat_sum[cat] / cat_w[cat]
            scores[cat] = js_round(avg)
            num_ += avg * w
            den += w
    return (num_ / den if den else None), scores


# Exact mirror of researchGradeLetter().
def grade_letter(score):
    if score is None:
        return None
    for cut, g in [(93, "A+"), (85, "A"), (78, "A-"), (70, "B+"), (62, "B"),
                   (54, "B-"), (46, "C+"), (38, "C"), (30, "C-"), (22, "D+"), (14, "D")]:
        if score >= cut:
            return g
    return "F"


def fetch_rows(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ValuatioAnalytics"}), timeout=30) as r:
            d = json.load(r)
        rows = d if isinstance(d, list) else (d.get("rows") or list(d.values()))
        if isinstance(rows, dict):
            rows = list(rows.values())
        return rows
    except Exception as e:
        print(f"  ✗ {url.split('/')[4]}: {e}", file=sys.stderr)
        return []


def load_history(book_dir, ticker):
    """[[date, close], ...] or [{date, close|price, ...}] → list of closes (asc)."""
    p = Path(book_dir) / "data" / "history" / f"{ticker}.json"
    if not p.exists():
        return None
    try:
        rows = json.loads(p.read_text())
        closes = []
        for e in rows:
            if isinstance(e, dict):
                v = num(e.get("close") if e.get("close") is not None else e.get("price"))
            elif isinstance(e, (list, tuple)) and len(e) >= 2:
                v = num(e[1])
            else:
                v = None
            if v is not None and v > 0:
                closes.append(v)
        return closes if len(closes) >= 30 else None
    except Exception:
        return None


def technicals(closes):
    """Per-ticker technicals — formulas mirror the app's feature functions so
    the bot's backend fast-path scores the way local scoring would."""
    px = closes[-260:]
    n = len(px)
    last = px[-1]
    # Trend: least-squares slope over last 60 + R² (mirrors featureTrendStrength)
    w = px[-60:] if n >= 60 else px
    m = len(w)
    xs = list(range(m))
    mx, my = sum(xs) / m, sum(w) / m
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, w))
    varx = sum((x - mx) ** 2 for x in xs) or 1
    slope = cov / varx
    pred = [my + slope * (x - mx) for x in xs]
    ss_res = sum((y - p) ** 2 for y, p in zip(w, pred))
    ss_tot = sum((y - my) ** 2 for y in w) or 1
    r2 = max(0.0, 1 - ss_res / ss_tot)
    slope_pct = (slope / my) if my else 0
    trend_signed = max(-1, min(1, slope_pct * 40)) * min(1, r2 * 1.5)
    # Momentum 0..1: position of price vs 20/60-day range blend
    lo60, hi60 = min(px[-60:]), max(px[-60:])
    momentum01 = (last - lo60) / (hi60 - lo60) if hi60 > lo60 else 0.5
    # Mean-reversion 0..1: distance below/above SMA20 (oversold → >0.5 bullish)
    sma20 = sum(px[-20:]) / min(20, n)
    dev = (last - sma20) / sma20 if sma20 else 0
    meanrev01 = max(0.0, min(1.0, 0.5 - dev * 4))
    # Returns / risk
    rets = [(px[i] - px[i - 1]) / px[i - 1] for i in range(1, n)]
    mean_r = sum(rets) / len(rets)
    vol_d = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets))
    vol_ann = vol_d * math.sqrt(252)
    ret_1y = (last / px[-253] - 1) if n >= 253 else ((last / px[0] - 1) if n >= 200 else None)   # z187: 252 sessions
    ret_3m = (last / px[-63] - 1) if n >= 64 else None
    peak, max_dd = px[0], 0.0
    for v in px:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)
    ann_ret = mean_r * 252                       # z187: arithmetic annualisation (compounding the daily mean overstated it)
    sharpe = (ann_ret / vol_ann) if vol_ann > 0 else None
    # Trend persistence: % of 20d windows moving the same direction as 1y
    return {
        "trendSigned": round(trend_signed, 4), "trendR2": round(r2, 3),
        "momentum01": round(momentum01, 4), "meanRev01": round(meanrev01, 4),
        "volAnn": round(vol_ann, 4), "ret1y": round(ret_1y, 4) if ret_1y is not None else None,
        "ret3m": round(ret_3m, 4) if ret_3m is not None else None,
        "maxDD": round(max_dd, 4), "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "points": n,
    }


# Non-equity grading metrics: risk-adjusted behavior, NOT fundamentals — ETFs,
# leveraged ETFs, FX, metals, bonds, crypto have no ROE; they have conduct.
NONEQ_METRICS = [
    ("ret1y",   True),    # historical movement — 1y return
    ("ret3m",   True),    # recent movement
    ("sharpe",  True),    # return per unit of risk
    ("volAnn",  False),   # conservativeness — lower vol scores higher
    ("maxDD",   True),    # drawdown is negative; closer to 0 (higher) = safer
    ("trendR2", True),    # trend consistency / orderliness
]


def asset_class(row):
    """Mirror of the app's split: equities (incl. ETFs that slipped into the
    equity books get caught by isetf) vs everything else."""
    t = row["ticker"].upper()
    if "=" in t or t.startswith("^") or t.endswith("-USD") or t.endswith("-USDT"):
        return "fx/future/crypto"
    isetf = str(row.get("isetf", "")).lower() in ("true", "1", "yes")
    isfund = str(row.get("isfund", "")).lower() in ("true", "1", "yes")
    if isetf or isfund:
        return "etf/fund"
    ac = (row.get("asset_class") or row.get("assetClass") or "").lower()
    if ac and ac not in ("equity", "stock", "equities"):
        return ac
    return "equity"


def percentile_grade(rows_with_vals, metrics):
    """Generic: rank each metric across the cohort, average percentiles, grade."""
    per_metric = {}
    for key, higher in metrics:
        vals = [(t, v[key]) for t, v in rows_with_vals.items() if v.get(key) is not None]
        vals.sort(key=lambda x: x[1], reverse=higher)
        total = len(vals)
        per_metric[key] = {
            t: {"rank": i + 1, "total": total,
                "pct": js_round((1 - i / (total - 1)) * 100) if total > 1 else 100,
                "value": v}
            for i, (t, v) in enumerate(vals)
        }
    out = {}
    for t in rows_with_vals:
        ranks, s, c = {}, 0, 0
        for key, _ in metrics:
            e = per_metric[key].get(t)
            ranks[key] = e
            if e:
                s += e["pct"]
                c += 1
        score = (s / c) if c else None
        out[t] = {"gradeScore": round(score, 2) if score is not None else None,
                  "grade": grade_letter(score), "coverage": c,
                  "coverageTotal": len(metrics), "ranks": ranks}
    return out


def main():
    rows, seen, book_of = [], set(), {}
    for k, book in enumerate(BOOKS):
        mj = Path(book) / "data" / "master.json"
        if mj.exists():
            try:
                d = json.loads(mj.read_text())
                book_rows = d if isinstance(d, list) else (d.get("rows") or list(d.values()))
            except Exception as e:
                print(f"  ✗ {book}: {e}", file=sys.stderr)
                book_rows = []
        else:
            book_rows = fetch_rows(REPOS[k])      # fallback when not checked out
        for r in book_rows:
            t = (r.get("ticker") or "").upper()
            if t and t not in seen:
                seen.add(t)
                r["ticker"] = t
                rows.append(normalize(r))
                book_of[t] = book
    print(f"{len(rows)} tickers across books")
    fx = load_fx()
    for r in rows:
        to_usd(r, fx)
    print(f"USD: {sum(1 for r in rows if not r.get('_usd'))} rows without an FX rate for their currency (money fields left out)")

    # ---- Technicals for EVERY ticker with history (feeds the bot fast-path) ----
    tech = {}
    for r in rows:
        closes = load_history(book_of[r["ticker"]], r["ticker"])
        if closes:
            try:
                tech[r["ticker"]] = technicals(closes)
            except Exception:
                pass
    print(f"technicals computed for {len(tech)} tickers")

    # ---- Split universes ----
    classes = {r["ticker"]: asset_class(r) for r in rows}
    equities = [r for r in rows if classes[r["ticker"]] == "equity"]
    nonequities = [r for r in rows if classes[r["ticker"]] != "equity"]
    print(f"{len(equities)} equities · {len(nonequities)} non-equities")

    # ---- Equity grading (z187: the app's method - 29 metrics, category-weighted, Scale ungraded) ----
    per_metric = {}
    for key, _, higher, get in METRICS:
        vals = []
        for r in equities:
            v = get(r)
            if v is not None and math.isfinite(v):
                vals.append((r["ticker"], v))
        per_metric[key] = rank_metric(vals, higher)
    by_ticker = {}
    for r in equities:
        ranks = {key: per_metric[key].get(r["ticker"]) for key, _, _, _ in METRICS}
        covered = sum(1 for e in ranks.values() if e)
        score, cats = category_grade(ranks)
        by_ticker[r["ticker"]] = {
            "ticker": r["ticker"], "name": r.get("name") or r["ticker"],
            "sector": r.get("sector"), "assetClass": "equity",
            "gradeScore": round(score, 2) if score is not None else None,
            "grade": grade_letter(score), "coverage": covered,
            "coverageTotal": len(METRICS), "categoryScores": cats, "ranks": ranks,
            "valuationNote": r.get("valuationNote") or None,
        }

    # ---- Non-equity grading (risk-adjusted conduct, cohort-ranked) ----
    ne_vals = {r["ticker"]: tech.get(r["ticker"], {}) for r in nonequities if tech.get(r["ticker"])}
    ne_grades = percentile_grade(ne_vals, NONEQ_METRICS)
    non_equity = {}
    for r in nonequities:
        g = ne_grades.get(r["ticker"])
        non_equity[r["ticker"]] = {
            "ticker": r["ticker"], "name": r.get("name") or r["ticker"],
            "assetClass": classes[r["ticker"]],
            **(g or {"gradeScore": None, "grade": None, "coverage": 0,
                     "coverageTotal": len(NONEQ_METRICS), "ranks": {}}),
        }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universeSize": len(equities),
        "nonEquityUniverse": len(nonequities),
        "metrics": [k for k, _, _, _ in METRICS],
        "method": "z187 category-weighted (app computeResearchRankings), USD",
        "categoryWeights": CATEGORY_WEIGHTS, "metricWeights": METRIC_WEIGHTS, "dampen": DAMPEN,
        "nonEquityMetrics": [k for k, _ in NONEQ_METRICS],
        "byTicker": by_ticker,
        "nonEquity": non_equity,
        "tech": tech,
    }, separators=(",", ":")))
    graded = sum(1 for t in by_ticker.values() if t["grade"])
    ne_graded = sum(1 for t in non_equity.values() if t["grade"])
    print(f"✓ research_grades.json: {graded} equities + {ne_graded} non-equities graded, {len(tech)} technicals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
