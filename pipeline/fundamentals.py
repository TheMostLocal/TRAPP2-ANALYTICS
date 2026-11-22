# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/fundamentals.py
#  z204 - FUNDAMENTALS AND MULTIPLES BY THE FORMULA, AS FILED ("derive, don't fetch", owner 11-21 0951 ET: "P/E ...
#  rely on the formulas ... compare with Yahoo and take the most accurate"). For every company with SEC XBRL facts
#  (data/sec/<T>.json - pipeline/fetch_sec.py reads the 10-K / 10-Q figures as filed, in USD), the trailing twelve
#  months and the latest balance sheet are built from the filings themselves, then checked against what Yahoo
#  reports for the same thing in the books' master rows:
#    * TTM (flows): the last fiscal year + the quarters filed since - the same quarters a year earlier
#      (FY + 9M - prior 9M, the standard trailing construct), else the last four quarters; per-share figures
#      (diluted EPS, dividends per share) re-based when a split falls between the filings (the weighted diluted
#      share count of each period against the latest's - a split factor x2, x25, 1:10 ... - never a buyback)
#    * revenue (a bank: net revenue when it files one), net income, operating income, EBITDA (operating income +
#      D&A), operating cash flow, capex, free cash flow (OCF - capex), gross profit, interest expense, diluted EPS,
#      dividends per share
#    * balance sheet at its latest date: equity, cash (+ short-term investments = Yahoo's "total cash"), debt =
#      long-term (non-current) + current maturities + short-term borrowings / commercial paper, leases apart (Yahoo's
#      "total debt" may include them); a component only when it is dated with the rest
#    * the multiples at the book's price: P/E, P/S, P/B, EV (market cap + debt - total cash), EV / sales, EV / EBITDA,
#      EV / EBIT, FCF yield, earnings yield, dividend yield, ROE, margins, net debt / EBITDA, interest cover. Market
#      cap = price x the SEC's share count (data/sec/shares.json, z202) when it has one, else the book's
#  CHECK, field by field (eps, revenue, net income, EBITDA, equity, debt, cash, dividend yield) against Yahoo's - in
#  one currency (USD filers only; a 20-F / IFRS filer has no us-gaap facts), on the SAME period (our period end vs
#  Yahoo's statement date, 45 days): agree (<= 2 %; 5 % EBITDA / debt / cash - definitions differ at the edges) /
#  near (<= 10 %) / check. And the PICK each consumer follows (`use`): "sec" when verified, when ours is the newer
#  filing, or when Yahoo has none; "yahoo" when our facts are older (a re-read is pending) or ours is missing; none
#  (disputed - a same-period disagreement beyond 10 %) - neither is used for a decision until a human or a newer
#  filing settles it (the review list names them).
#  OUT: data/derived/fundamentals.json (the legend is in its "keys"); rewritten only when more than its time changed.
#  WHEN: .github/workflows/analytics.yml after the market statistics (the books are checked out there; data/sec is
#  this repo's). Env: FUND_NOW=ISO (tests) · DERIVED_OFFLINE=1 / DERIVED_BOOKS=<dir> (as derived.py: the books).
# ============================================================
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import derived as dv  # noqa: E402  - the books' masters from the checkouts (raw URL fallback), the shared helpers

BUILD = "z204"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SEC_DIR = DATA / "sec"
SHARES = DATA / "sec" / "shares.json"
OUT = DATA / "derived" / "fundamentals.json"
BOOKS = dv.BOOKS
SAME_PERIOD = 45                       # days between our period end and Yahoo's statement date to call it the same period
BANDS = {"eps": 0.02, "rev": 0.02, "ni": 0.02, "eq": 0.02, "ebitda": 0.05, "debt": 0.05, "cash": 0.05, "dy": 0.05}
NEAR = 0.10
EPS_CENTS = 0.02
REVIEW = 60
SPLITS = (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100)   # 3-for-2 left out: issuance moves counts that much
KEYS = {
    "end": "the TTM's period end (latest quarter or fiscal year)", "fy": "last fiscal year end",
    "how": "TTM construct per item: FY | FY+nQ (FY + n quarters - the same n a year earlier) | 4Q | FY~ (the latest quarter not chained)",
    "filed": "[form, date] of the newest filing used", "v": "the SEC doc's facts version",
    "t": "TTM as filed: eps (diluted, USD/share), dps, rev, ni, oi, ebitda, da, ocf, capex, fcf, gp, int (USD)",
    "te": "an item's own period end where it differs from end", 
    "b": "balance sheet at its latest date bd: eq (common = eqT - pref), eqT, pref, cash, sti, cashT (= cash + sti), debt "
         "(ltd + current + short-term + finance leases), ltd, dc, sd, opl / fil (operating / finance leases)",
    "dil": "[weighted diluted shares, period end]", "px": "the book's price", "pd": "its date",
    "mc": "[market cap, 'sec' = price x the SEC share count | 'book']",
    "m": "multiples at px from OUR denominators where the pick is ours: pe, ps, pb, ev, evs (EV/sales), eve (EV/EBITDA), "
         "evo (EV/EBIT), fcfy, ey, dy, roe, gm, om, nm, nde (net debt / EBITDA), ic (interest cover)",
    "y": "Yahoo's figure, the gap and the band per field: [yahoo, ours/yahoo - 1, band(, the debt definition that matched)]; "
         "band agree | near | check | newer (ours is the later period) | older | only (Yahoo has none) | partial (debt / cash "
         "before the z204 re-read)",
    "use": "per field: 'sec' | 'yahoo' | null (disputed - neither for decisions)",
    "rc": "the revenue concept the TTM came from",
    "q": "flags: split:<end>:x<k> (a per-share figure re-based) · basis? (share counts jump by no split factor) · eps>price · "
         "class? (another listing of the same filer - a share class, preferred or note) · no-cap (no market cap in the book) - "
         "these four: our per-share / revenue / book figures are used only where Yahoo agrees · neg-rev · stale (newest "
         "period > 15 months old) · fy-only (the latest quarter could not be chained)",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now_utc():
    v = os.environ.get("FUND_NOW")
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else datetime.now(timezone.utc)


num = dv._num


def _days(a, b):
    return (date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days


def sig(x, n=6):
    return None if x is None else float(f"{x:.{n}g}")


def r4(x):
    return None if x is None else round(x, 4)


def split_factor(r):
    """A share-count ratio that is a split (x2, x25, 1:10 ...) -> the factor (2.0, or 0.1 for a 1-for-10), else None.
    Buybacks and issuance move a count a few % between two quarters; a split moves it by an exact factor."""
    if not r or r <= 0:
        return None
    k = r if r >= 1 else 1 / r
    if k < 1.9:
        return None
    for f in SPLITS:
        if abs(k / f - 1) <= 0.06:
            return float(f) if r >= 1 else 1.0 / f
    return None


# --------------------------------------------------------------------------------------------------------- TTM ---
def _series(item):
    a = sorted((x for x in (item or {}).get("annual") or [] if num(x.get("val")) is not None and x.get("end")), key=lambda x: x["end"])
    q = sorted((x for x in (item or {}).get("quarterly") or [] if num(x.get("val")) is not None and x.get("end")), key=lambda x: x["end"])
    return a, q


def _year_before(q, end):
    """The quarterly record ending a year before `end` (52/53-week years: 357..373 days)."""
    best = None
    for x in q:
        d = _days(x["end"], end)
        if 357 <= d <= 373 and (best is None or abs(d - 364) < abs(_days(best["end"], end) - 364)):
            best = x
    return best


def _consecutive(ends):
    return all(80 <= _days(ends[i - 1], ends[i]) <= 100 for i in range(1, len(ends)))


def ttm(item, rebase=None):
    """Trailing twelve months of a flow item as filed -> (value, end, how) or None.
    rebase(record, kind) -> the factor dividing a per-share record (a split since it was filed), or None."""
    a, q = _series(item)
    if not a and not q:
        return None

    def val(x, kind):
        v = num(x["val"])
        f = rebase(x, kind) if rebase else None
        return v / f if f else v

    fy = a[-1] if a else None
    if fy:
        after = [x for x in q if x["end"] > fy["end"]]
        if not after:
            return val(fy, "a"), fy["end"], "FY"
        if len(after) <= 3 and 80 <= _days(fy["end"], after[0]["end"]) <= 100 and _consecutive([x["end"] for x in after]):
            prior = [_year_before(q, x["end"]) for x in after]
            if all(prior):
                v = val(fy, "a") + sum(val(x, "q") for x in after) - sum(val(x, "q") for x in prior)
                return v, after[-1]["end"], f"FY+{len(after)}Q"
    if len(q) >= 4 and _consecutive([x["end"] for x in q[-4:]]) and (not fy or q[-1]["end"] > fy["end"]):
        return sum(val(x, "q") for x in q[-4:]), q[-1]["end"], "4Q"
    if fy and (not q or q[-1]["end"] <= fy["end"] or _days(fy["end"], q[-1]["end"]) <= 100):
        return val(fy, "a"), fy["end"], "FY" if not q or q[-1]["end"] <= fy["end"] else "FY~"
    return None                                              # a gap in the filings: no current TTM rather than a stale year


def latest(item):
    """An instant item's latest value -> (value, end, concept) or None."""
    if not item:
        return None
    lt = item.get("latest")
    if lt and num(lt.get("val")) is not None:
        return num(lt["val"]), lt["end"], item.get("concept")
    a, q = _series(item)
    pts = a + q
    if not pts:
        return None
    x = max(pts, key=lambda r: r["end"])
    return num(x["val"]), x["end"], item.get("concept")


def share_rebaser(facts):
    """Per-share records (diluted EPS, dividends) re-based to the LATEST share basis. A split shows in the weighted
    diluted counts as filed: between two consecutive quarters the count moves by the exact factor (a quarter filed
    before the split against one filed after it - the comparatives a later 10-Q restates are already re-based); a
    fiscal year filed before the split sits a factor away from its own quarters once those are re-based. An
    acquisition paid in shares moves the count gradually (Capital One + Discover: 1.5x over the year), so a year's
    count against its own quarters does not flag it; 3-for-2 is left out for the same reason.
    -> rebase(record, 'a' | 'q') = the factor to divide that per-share record by (None = same basis), hits {end: factor}."""
    a, q = _series(facts.get("dilutedShares"))
    sa = {x["end"]: num(x["val"]) for x in a if num(x["val"])}
    sq = {x["end"]: num(x["val"]) for x in q if num(x["val"])}
    qs = sorted(sq)
    if not qs:
        return None, {}, False
    qf = {}                                                  # each quarter's factor to the latest basis
    f = 1.0
    qf[qs[-1]] = 1.0
    for j in range(len(qs) - 1, 0, -1):
        if _days(qs[j - 1], qs[j]) <= 200:                   # consecutive, or one fiscal Q4 between (10-Qs have none)
            k = split_factor(sq[qs[j]] / sq[qs[j - 1]])
            if k:
                f *= k
        qf[qs[j - 1]] = f
    hits = {}

    def fy_factor(end):
        s = sa.get(end)
        inside = sorted(sq[e] * qf[e] for e in qs if 0 <= _days(e, end) < 360)
        if not s or len(inside) < 2:
            return None
        return split_factor(inside[len(inside) // 2] / s)

    def rebase(rec, kind):
        k = fy_factor(rec["end"]) if kind == "a" else (qf.get(rec["end"]) if qf.get(rec["end"], 1.0) != 1.0 else None)
        if k:
            hits[rec["end"]] = k
        return k
    # counts that jump both ways, or by products that are no split (x1.25, x5.7): the basis can't be told apart from
    # issuance / a de-SPAC / share classes - the per-share figures then need Yahoo's agreement to be used
    unclear = any(abs(v - 1) > 1e-9 and split_factor(v) is None for v in qf.values())
    return rebase, hits, unclear


# ------------------------------------------------------------------------------------------------- one company ---
NONCURRENT_LTD = {"LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"}


def company(doc, m, sh=None, today=None, secondary=False):
    """One SEC doc + its master row (as read by read_masters) + its shares.json entry -> the record, or None.
    secondary = another listing of a company whose filings another ticker already prices (a share class, a preferred,
    a note): per-share figures and market-cap multiples then need Yahoo's agreement."""
    f = (doc or {}).get("facts") or {}
    if not f:
        return None
    today = today or now_utc().date()
    rebase, hits, unclear = share_rebaser(f)
    q, t, te, how = (["class?"] if secondary else []), {}, {}, {}
    if not num(m.get("marketcap")):
        q.append("no-cap")                                   # no market cap in the book: a note / preferred / unit, not the common
    ends = []

    def put(key, item, per_share=False):
        r = ttm(f.get(item), rebase if per_share else None)
        if r is None or r[0] is None:
            return None
        t[key] = sig(r[0]) if not per_share else round(r[0], 4)
        te[key] = r[1]
        how[key] = r[2]
        ends.append(r[1])
        return r
    rev = put("rev", "revenueNet") if (m.get("sector") == "Financial Services" and f.get("revenueNet")) else None
    rc = "RevenuesNetOfInterestExpense" if rev else None
    if not rev:
        rev = put("rev", "revenue")
        rc = (f.get("revenue") or {}).get("concept")
    if rev and rev[0] < 0:
        q.append("neg-rev")
        for d in (t, te, how):
            d.pop("rev", None)
    for key, item in (("ni", "netIncome"), ("oi", "operatingIncome"), ("ocf", "operatingCashFlow"), ("capex", "capex"),
                      ("da", "da"), ("gp", "grossProfit"), ("int", "interestExpense")):
        put(key, item)
    for key, item in (("eps", "epsDiluted"), ("dps", "dps")):
        put(key, item, per_share=True)
    if hits:
        q.append("split:" + ",".join(f"{e}:x{(1 / k if k < 1 else k):g}{'' if k >= 1 else '-rev'}" for e, k in sorted(hits.items())))
    if unclear:
        q.append("basis?")
    px0 = m.get("price")
    if px0 and t.get("eps") is not None and abs(t["eps"]) > px0:
        q.append("eps>price")                                # a P/E under 1: a share-basis or scale problem, not a bargain
    if "ocf" in t and "capex" in t and te["ocf"] == te["capex"]:
        t["fcf"], te["fcf"] = sig(t["ocf"] - abs(t["capex"])), te["ocf"]
    if "oi" in t and "da" in t and te["oi"] == te["da"]:
        t["ebitda"], te["ebitda"] = sig(t["oi"] + abs(t["da"])), te["oi"]
    # balance sheet at its latest date
    b, bd = {}, None
    eq = latest(f.get("equity"))
    cash = latest(f.get("cash"))
    if eq:
        bd = eq[1]
    elif cash:
        bd = cash[1]

    def at_bd(item):
        x = latest(f.get(item))
        return x if (x and bd and abs(_days(x[1], bd)) <= 5) else None
    if bd:
        for key, item in (("eqT", "equity"), ("pref", "preferred"), ("cash", "cash"), ("sti", "stInvest"), ("ltd", "longTermDebt"),
                          ("dc", "debtCurrent"), ("sd", "shortDebt"), ("opl", "opLease"), ("fil", "finLease")):
            x = at_bd(item)
            if x:
                b[key] = sig(x[0])
        if "eqT" in b:                                        # common equity: P/B's denominator (Yahoo's book value)
            b["eq"] = sig(b["eqT"] - (b.get("pref") or 0))
        ltd = at_bd("longTermDebt")
        parts = [b.get("ltd")]
        if ltd and ltd[2] in NONCURRENT_LTD:                 # "LongTermDebt" already holds the current maturities
            parts.append(b.get("dc"))
        parts.append(b.get("sd"))
        if not (ltd and ltd[2] == "LongTermDebtAndCapitalLeaseObligations"):
            parts.append(b.get("fil"))                       # finance leases are debt (unless the line already holds them)
        if any(p is not None for p in parts):
            b["debt"] = sig(sum(p for p in parts if p is not None))
        if "cash" in b:
            b["cashT"] = sig(b["cash"] + (b.get("sti") or 0))
    dil = latest(f.get("dilutedShares"))                     # the newest period's weighted diluted count
    end = max(ends) if ends else bd
    if not end:
        return None
    a_fy = [x["end"] for x in (_series(f.get("revenue"))[0] or _series(f.get("netIncome"))[0])]
    rec = {"end": end, "fy": max(a_fy) if a_fy else None, "how": how, "t": t, "te": te, "v": doc.get("factsV") or 1}
    if rc:
        rec["rc"] = rc
    if b:
        rec["b"], rec["bd"] = b, bd
    if dil and dil[0]:
        rec["dil"] = [sig(dil[0]), dil[1]]
    newest = newest_filing(f)
    if newest:
        rec["filed"] = newest
    if (today - date.fromisoformat(end)).days > 460:
        q.append("stale")
    if any(v == "FY~" for v in how.values()):
        q.append("fy-only")
    # price, market cap, multiples
    px, pd = m.get("price"), m.get("date")
    mc = None
    if px and sh and num(sh.get("s")):
        mc = [sig(px * num(sh["s"])), "sec"]
    elif num(m.get("marketcap")):
        mc = [sig(num(m["marketcap"])), "book"]
    if px:
        rec["px"], rec["pd"] = px, pd
    if mc:
        rec["mc"] = mc
    if q:
        rec["q"] = q
    y, use = check(rec, m)
    if y:
        rec["y"] = y
    rec["use"] = use
    # the multiples by the formula take a denominator only where the pick is ours ("sec"); the flows nobody checks
    # (operating income, cash flow, capex, gross profit, interest, D&A) only when current with the rest
    cur = {k for k, e in te.items() if rec["end"] and abs(_days(e, rec["end"])) <= SAME_PERIOD} if "stale" not in q else set()
    t_ok = {k: v for k, v in t.items() if (use.get(k) == "sec" if k in ("eps", "rev", "ni", "ebitda") else
                                           (use.get("dy") == "sec" if k == "dps" else k in cur))}
    b_ok = {k: v for k, v in b.items() if (k != "eq" or use.get("eq") == "sec")}
    if use.get("debt") != "sec" or use.get("cash") != "sec":
        b_ok.pop("debt", None)
        b_ok.pop("cashT", None)
    rec["m"] = multiples(t_ok, b_ok, px, mc[0] if mc else None)
    rec["te"] = {k: e for k, e in te.items() if e != end}           # an item's own end only where it isn't the record's
    if not rec["te"]:
        del rec["te"]
    return rec


def newest_filing(f):
    best = None
    for item in f.values():
        if not isinstance(item, dict):
            continue
        for x in (item.get("annual") or []) + (item.get("quarterly") or []):
            fl, fm = x.get("filed") or "", x.get("form") or ""
            if fl and not fm.startswith("derived") and (best is None or fl > best[1]):
                best = [fm, fl]
    return best


def multiples(t, b, px, mc):
    def div(a, c, pos=True):
        if a is None or c is None or c == 0 or (pos and c < 0):
            return None
        return a / c
    m = {}
    eps, rev, ni, oi, ebitda, fcf = t.get("eps"), t.get("rev"), t.get("ni"), t.get("oi"), t.get("ebitda"), t.get("fcf")
    eq, debt, cashT = b.get("eq"), b.get("debt"), b.get("cashT")
    if px:
        m["pe"] = div(px, eps)                                # negative earnings: no P/E (n/m), the earnings yield shows it
        m["ey"] = div(eps, px)
        m["dy"] = div(t.get("dps"), px) if t.get("dps") is not None else None
    if mc:
        m["ps"] = div(mc, rev)
        m["pb"] = div(mc, eq)                                 # negative book: none (Yahoo shows a negative ratio)
        m["fcfy"] = div(fcf, mc)
        if debt is not None and cashT is not None:
            ev = mc + debt - cashT
            m["ev"] = ev
            m["evs"] = div(ev, rev)
            m["eve"] = div(ev, ebitda)
            m["evo"] = div(ev, oi)
    m["roe"] = div(ni, eq)
    m["gm"] = div(t.get("gp"), rev)
    m["om"] = div(oi, rev)
    m["nm"] = div(ni, rev)
    if debt is not None and cashT is not None:
        m["nde"] = div(debt - cashT, ebitda)
    if oi is not None and t.get("int"):
        m["ic"] = div(oi, abs(t["int"]))
    return {k: (sig(v, 5) if k == "ev" else r4(v)) for k, v in m.items() if v is not None}


# ------------------------------------------------------------------------------------------------------- check ---
YF = {"eps": ("eps", "revenueAsOf"), "rev": ("revenueUSD", "revenueAsOf"), "ni": ("netIncome", "revenueAsOf"),
      "ebitda": ("ebitdaUSD", "revenueAsOf"), "eq": ("bookUSD", "bookAsOf"), "debt": ("totalDebt", "bookAsOf"),
      "cash": ("cash", "bookAsOf")}


def field(rec, key):
    """Our figure for a checked field: a TTM flow, total cash, debt or equity, the dividend yield."""
    t, b = rec.get("t") or {}, rec.get("b") or {}
    if key in ("eps", "rev", "ni", "ebitda"):
        return t.get(key)
    if key == "cash":
        return b.get("cashT")
    if key == "dy":
        dps, px = t.get("dps"), rec.get("px")
        return round(dps / px, 5) if (dps is not None and px) else None
    return b.get(key)


def check(rec, m):
    """Ours vs Yahoo's, field by field -> (y, use). Only for a USD filer: the master's statements are the
    reporting currency's (USD-converted fields used), our facts are USD only. Debt: Yahoo's total sometimes holds
    the operating leases - ours is compared with and without them, the closer definition named (y.debt[3])."""
    y, use = {}, {}
    usd = str(m.get("financialCurrency") or "USD").upper() in ("USD", "")
    b = rec.get("b") or {}
    v = rec.get("v") or 1
    for key, (yk, ak) in YF.items():
        ours = field(rec, key)
        end = (rec.get("te") or {}).get(key, rec["end"]) if key in ("eps", "rev", "ni", "ebitda") else rec.get("bd")
        yv = num(m.get(yk)) if usd else None
        ya = str(m.get(ak) or "")[:10] or None
        alt = None
        if key == "debt" and ours is not None and yv and b.get("opl"):
            if abs((ours + b["opl"]) / yv - 1) < abs(ours / yv - 1):
                ours, alt = ours + b["opl"], "incl. operating leases"
        if ours is None and yv is None:
            continue
        if ours is None:
            use[key] = "yahoo"
            continue
        if key in ("debt", "cash") and v < 4:
            y[key] = [sig(yv) if yv else None, None, "partial"]   # before z204's re-read: no current debt / short-term investments
            use[key] = "yahoo"                               # ours incomplete - never a dispute, never ours
            continue
        flags = rec.get("q") or []
        listing = "class?" in flags or "no-cap" in flags     # maybe not the common stock
        per_share = listing or "basis?" in flags or "eps>price" in flags or any(str(f).startswith("split:") for f in flags)
        unsure = per_share if key in ("eps", "dy") else listing if key in ("rev", "eq") else False
        if yv is None or yv == 0:
            y[key] = [None, None, "only"]
            use[key] = None if unsure else "sec"          # a re-based or doubtful figure nobody else confirms: not used
            continue
        gap = ours / yv - 1
        if ya and end and _days(ya, end) > SAME_PERIOD:
            band = "newer"
        elif ya and end and _days(end, ya) > SAME_PERIOD:
            band = "older"
        else:
            g = abs(gap)
            band = "agree" if g <= BANDS[key] else "near" if g <= NEAR else "check"
            if key == "eps":                                  # four quarters rounded to the cent: 2 cents either way is the same figure
                dd = abs(ours - yv)
                band = "agree" if (g <= BANDS[key] or dd <= EPS_CENTS) else "near" if (g <= NEAR or dd <= 2.5 * EPS_CENTS) else "check"
                if (ours > 0) != (yv > 0) and dd > 2.5 * EPS_CENTS:
                    band = "check"                            # a profit on one side, a loss on the other
        y[key] = [sig(yv), round(gap, 4), band] + ([alt] if alt else [])
        use[key] = "sec" if band in ("agree", "near", "newer") else "yahoo" if band == "older" else None
        if band == "check" and v < 2:
            use[key] = "yahoo"                               # a pre-z187 doc (one concept name, no merge): ours is the suspect
        if key == "eq" and band == "check" and v < 4 and 0 < gap <= 0.6:
            use[key] = "yahoo"                               # before z204's re-read ours still holds the preferred stock
            y[key].append("incl. preferred?")                # (a bank's book value is its COMMON equity)
        if band == "newer" and unsure:
            use[key] = None                                  # a figure nobody can confirm: a re-based / doubtful per-share one, or a listing that may not be the common
        if (band == "check" and key == "rev" and v < 4 and gap < -0.25 and rec.get("rc") != "Revenues"):
            use[key] = "yahoo"                               # before z204's re-read the contract-revenue name came first - for a
            y[key].append("component?")                      # REIT / insurer / lender that is one component of revenue
    # dividend yield: ours (dps / price) vs Yahoo's percent
    dps, px = (rec.get("t") or {}).get("dps"), rec.get("px")
    dy, ydy = (dps / px if (dps is not None and px) else None), num(m.get("dividend_yield"))
    if dy is not None and ydy is not None and usd:
        ydy = ydy / 100.0
        if ydy == 0 and dy == 0:
            band = "agree"
        elif ydy == 0 or dy == 0:
            band = "check" if max(dy, ydy) > 0.002 else "agree"
        else:
            g = abs(dy / ydy - 1)
            band = "agree" if g <= 0.05 else "near" if g <= 0.15 else "check"   # declared vs paid, the price moved
        y["dy"] = [round(ydy, 5), round(dy / ydy - 1, 4) if ydy else None, band]
        use["dy"] = "sec" if band in ("agree", "near") else None
    elif dy is not None and usd and ydy is None:
        flags = rec.get("q") or []
        doubtful = "class?" in flags or "no-cap" in flags or "basis?" in flags or any(str(f).startswith("split:") for f in flags)
        y["dy"] = [None, None, "only"]
        use["dy"] = None if doubtful else "sec"
    return y, use


# ---------------------------------------------------------------------------------------------------- the books ---
MFIELDS = ("price", "date", "sector", "financialCurrency", "marketcap", "eps", "revenueUSD", "revenueAsOf", "netIncome",
           "ebitdaUSD", "bookUSD", "bookAsOf", "totalDebt", "cash", "dividend_yield", "pe", "priceToSales",
           "priceToBook", "evToEbitda", "asset_class", "isetf")


def read_masters(read=None):
    """{ticker: the master fields the check needs} - the first book listing a ticker wins (as derived.py)."""
    read = read or dv.read_book_file
    out = {}
    for bk in BOOKS:
        d = read(bk, "data/master.json")
        rows = d if isinstance(d, list) else ((d or {}).get("rows") or []) if isinstance(d, dict) else []
        if isinstance(rows, dict):
            rows = list(rows.values())
        for r in rows:
            if not isinstance(r, dict):
                continue
            tk = str(r.get("ticker") or "").upper().strip()
            if not tk or tk in out:
                continue
            rec = {k: r.get(k) for k in MFIELDS}
            rec["price"] = num(r.get("price"))
            rec["date"] = str(r.get("date") or "")[:10] or None
            rec["book"] = bk
            out[tk] = rec
    return out


def sec_docs(d=None):
    d = Path(d or SEC_DIR)
    out = {}
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        if p.name in ("index.json", "cusips.json", "shares.json"):
            continue
        j = dv._read(p)
        if isinstance(j, dict) and j.get("facts"):
            out[p.stem.upper()] = j
    return out


def primaries(docs, mast):
    """Several tickers on one CIK (share classes GOOG / GOOGL, preferreds and notes DTE / DTB / DTG, a trust's funds):
    the one with the largest market cap in the books is the company's common; the others are secondary listings."""
    by = {}
    for tk, d in docs.items():
        if tk in mast and d.get("cik"):
            by.setdefault(d["cik"], []).append(tk)
    out = set()
    for cik, tks in by.items():
        best = max(tks, key=lambda x: (num(mast[x].get("marketcap")) or 0, -len(x), x))
        out.update(x for x in tks if x != best)
    return out


def build(docs, mast, shares=None, today=None):
    today = today or now_utc().date()
    shr = ((shares or {}).get("tickers") or {}) if isinstance(shares, dict) else {}
    tick, review = {}, []
    bands = {}
    second = primaries(docs, mast)
    for tk in sorted(docs):
        m = mast.get(tk)
        if not m:
            continue                                         # facts for a ticker no book lists: nothing to price
        if str(m.get("isetf") or "").lower() == "true" or str(m.get("asset_class") or "Equity").lower() not in ("equity", ""):
            continue                                         # a fund / trust: its filing is not a company's
        try:
            rec = company(docs[tk], m, shr.get(tk), today, secondary=tk in second)
        except Exception as e:                               # noqa: BLE001 - one odd filing must not stop the run
            log(f"  ✗ {tk}: {type(e).__name__} {e}")
            continue
        if not rec:
            continue
        tick[tk] = rec
        for key, (yv, gap, band, *_) in (rec.get("y") or {}).items():
            bands.setdefault(key, {}).setdefault(band, 0)
            bands[key][band] += 1
            if band == "check":
                review.append({"t": tk, "f": key, "ours": field(rec, key), "yahoo": yv, "gap": gap,
                               "end": rec["end"] if key in ("eps", "rev", "ni", "ebitda") else rec.get("bd"),
                               "how": rec["how"].get(key), "v": rec["v"]})
    review.sort(key=lambda e: -abs(e["gap"]) if e["gap"] is not None else 0)
    counts = {"docs": len(docs), "computed": len(tick),
              "withTTM": sum(1 for r in tick.values() if r["t"].get("rev") is not None or r["t"].get("ni") is not None),
              "eps": sum(1 for r in tick.values() if r["t"].get("eps") is not None),
              "ebitda": sum(1 for r in tick.values() if r["t"].get("ebitda") is not None),
              "debt": sum(1 for r in tick.values() if (r.get("b") or {}).get("debt") is not None),
              "dps": sum(1 for r in tick.values() if r["t"].get("dps") is not None),
              "split": sum(1 for r in tick.values() if any(str(x).startswith("split") for x in r.get("q") or [])),
              "secondary": sum(1 for r in tick.values() if "class?" in (r.get("q") or [])),
              "stale": sum(1 for r in tick.values() if "stale" in (r.get("q") or [])),
              "factsV": {str(k): sum(1 for r in tick.values() if r["v"] == k) for k in sorted({r["v"] for r in tick.values()}, key=str)},
              "use": {k: {u or "none": sum(1 for r in tick.values() if k in r["use"] and r["use"][k] == u) for u in ("sec", "yahoo", None)}
                      for k in ("eps", "rev", "ni", "ebitda", "eq", "debt", "cash", "dy")}}
    return {"build": BUILD, "at": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "keys": KEYS,
            "method": ("as filed (SEC XBRL, USD): TTM = last FY + quarters since - the same quarters a year earlier (else 4 "
                       "quarters); per-share figures re-based across splits by the weighted diluted counts; debt = non-current "
                       "+ current + short-term borrowings; total cash = cash + short-term investments; EV = market cap + debt "
                       "- total cash; Yahoo checked on the same period (45 days)"),
            "bands": BANDS, "counts": counts, "checks": bands, "review": review[:REVIEW], "tickers": tick}


def main():
    t0 = time.time()
    docs = sec_docs()
    if not docs:
        log("fundamentals: no SEC docs with facts - nothing written")
        return 0
    mast = read_masters()
    if not mast:
        log("fundamentals: no master rows read - nothing written")
        return 0
    doc = build(docs, mast, dv._read(SHARES))
    c, ch = doc["counts"], doc["checks"]
    line = " · ".join(f"{k} " + "/".join(f"{ch.get(k, {}).get(b, 0)} {b}" for b in ("agree", "near", "check", "newer", "older", "only"))
                      for k in ("eps", "rev", "ni", "eq", "ebitda", "debt", "cash", "dy"))
    log(f"fundamentals {BUILD}: {c['computed']} companies as filed (of {c['docs']} SEC docs with facts; facts versions {c['factsV']}) · "
        f"EPS {c['eps']}, EBITDA {c['ebitda']}, debt {c['debt']}, dividends {c['dps']} · re-based across a split {c['split']} · "
        f"stale {c['stale']} · vs Yahoo: {line} · {time.time() - t0:.1f}s")
    prior = dv._read(OUT)
    if dv.same(prior, doc):
        log("fundamentals: unchanged since the last run - not rewritten")
        return 0
    dv._write(OUT, doc)
    try:
        shown = OUT.relative_to(ROOT)
    except ValueError:
        shown = OUT
    log(f"fundamentals: wrote {shown} ({OUT.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
