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

BOOKS = ["books/TRAPP2", "books/TRAPP2-1", "books/TRAPP2-2", "books/TRAPP2-3"]
REPOS = [
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2/main/data/master.json",
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-1/main/data/master.json",
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-2/main/data/master.json",
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-3/main/data/master.json",
]
OUT = Path(__file__).resolve().parent.parent / "data" / "research_grades.json"

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


# (key, higher_is_better, getter) — 1:1 with the app's RESEARCH_METRICS.
METRICS = [
    ("roe",         True,  lambda r: num(r.get("returnOnEquity"))),
    ("rotce",       True,  rotce),
    ("roa",         True,  lambda r: num(r.get("returnOnAssets"))),
    ("grossMargin", True,  lambda r: num(r.get("grossMargin"))),
    ("opMargin",    True,  lambda r: num(r.get("operatingMargin"))),
    ("netMargin",   True,  lambda r: num(r.get("profitMargin"))),
    ("fcfMargin",   True,  lambda r: ratio(r.get("freeCashFlow"), r.get("revenue"))),
    ("sbcPctRev",   False, lambda r: (lambda s: s if (s is not None and s > 0) else None)(ratio(r.get("stockBasedComp"), r.get("revenue")))),
    ("revGrowth",   True,  lambda r: num(r.get("revenueGrowth"))),
    ("epsGrowth",   True,  lambda r: num(r.get("earningsGrowth"))),
    ("pe",          False, lambda r: pos(r.get("pe"))),
    ("pb",          False, lambda r: pos(r.get("priceToBook"))),
    ("evEbitda",    False, lambda r: pos(r.get("evToEbitda"))),
    ("evRev",       False, lambda r: pos(r.get("evToRevenue"))),
    ("debtEquity",  False, lambda r: ratio(r.get("totalDebt"), r.get("totalEquity"))),
    ("cashRatio",   True,  lambda r: ratio(r.get("cash"), r.get("marketCap"))),
    ("divYield",    True,  lambda r: pos(r.get("dividendYield"))),
    ("revenue",     True,  lambda r: num(r.get("revenue"))),
    ("netIncome",   True,  lambda r: num(r.get("netIncome"))),
    ("ebitda",      True,  lambda r: num(r.get("ebitda"))),
    ("fcf",         True,  lambda r: num(r.get("freeCashFlow"))),
    ("marketCap",   True,  lambda r: num(r.get("marketCap"))),
    ("totalAssets", True,  lambda r: num(r.get("totalAssets"))),
]

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
    ret_1y = (last / px[0] - 1) if n >= 200 else None
    ret_3m = (last / px[-63] - 1) if n >= 64 else None
    peak, max_dd = px[0], 0.0
    for v in px:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)
    ann_ret = ((1 + mean_r) ** 252 - 1)
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
                "pct": round((1 - i / (total - 1)) * 100) if total > 1 else 100,
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

    # ---- Equity grading (23 fundamental metrics — unchanged) ----
    per_metric = {}
    for key, higher, get in METRICS:
        vals = []
        for r in equities:
            v = get(r)
            if v is not None:
                vals.append((r["ticker"], v))
        vals.sort(key=lambda x: x[1], reverse=higher)
        total = len(vals)
        per_metric[key] = {
            t: {"rank": i + 1, "total": total,
                "pct": round((1 - i / (total - 1)) * 100) if total > 1 else 100,
                "value": v}
            for i, (t, v) in enumerate(vals)
        }
    by_ticker = {}
    for r in equities:
        ranks, pct_sum, covered = {}, 0, 0
        for key, _, _ in METRICS:
            e = per_metric[key].get(r["ticker"])
            ranks[key] = e
            if e:
                pct_sum += e["pct"]
                covered += 1
        score = (pct_sum / covered) if covered else None
        by_ticker[r["ticker"]] = {
            "ticker": r["ticker"], "name": r.get("name") or r["ticker"],
            "sector": r.get("sector"), "assetClass": "equity",
            "gradeScore": round(score, 2) if score is not None else None,
            "grade": grade_letter(score), "coverage": covered,
            "coverageTotal": len(METRICS), "ranks": ranks,
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
        "metrics": [k for k, _, _ in METRICS],
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
