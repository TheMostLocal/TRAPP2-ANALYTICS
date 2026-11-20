#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/warning_lab.py
#
#  z197 - THE HYPOTHESIS LAB. Owner (11-19): "This isn't an art, it's a science and this system is the tester. We need the
#  system to think on hypotheses and be proven right or wrong, but it should be exploring at all times … with more focus
#  and data points assumptions can turn into evidence."
#
#  A hypothesis = a signal (conditions on the publication-lagged monthly panel) -> an outcome within a horizon:
#    DD15_12 / DD20_18 / DD10_6  a 15 / 20 / 10 % drawdown within 12 / 18 / 6 months (daily closes)
#    REC12 / REC24               an NBER recession starting within 12 / 24 months
#    ABOVE_MED12                 the next 12 months' return above the median 12-month return
#  Tested every run on all history: base rate, rate when the signal is on, lift, how many distinct episodes it caught, and
#  a p-value from circular-shift permutations (the signal slid along history keeps its persistence, so overlapping
#  months can't fake significance). Status: supported / rejected / inconclusive.
#  EXPLORING: every run also tries every indicator at its extremes against drawdowns and recessions (plus a rotating
#  slice of two-indicator combinations), controls false discoveries (Benjamini-Hochberg) and keeps what survives as
#  'discovered - exploratory'.
#  PROVEN RIGHT OR WRONG: whenever a hypothesis' signal is on, the lab writes a dated prediction to its ledger; when the
#  horizon passes it is graded against what actually happened - an out-of-sample record nothing can backfill.
# ============================================================
import bisect
import hashlib
import math
from datetime import date, timedelta

OUTCOMES = {
    "DD10_6": ("a 10 % drawdown within 6 months", 183, ("dd", -0.10)),
    "DD15_12": ("a 15 % drawdown within 12 months", 365, ("dd", -0.15)),
    "DD20_12": ("a 20 % drawdown within 12 months", 365, ("dd", -0.20)),
    "DD20_18": ("a 20 % drawdown within 18 months", 548, ("dd", -0.20)),
    "REC12": ("a recession starting within 12 months", 366, ("rec", None)),
    "REC24": ("a recession starting within 24 months", 731, ("rec", None)),
    "ABOVE_MED12": ("an above-median 12-month return", 365, ("ret", None)),
}

# The seeded hypotheses - each one a lesson from an episode (warning_history.EPISODES), stated so it can fail.
SEEDED = [
    dict(id="H01", statement="Market blindness (fundamentals worsening, markets calm) is followed by a 20 % drawdown within 18 months",
         lesson="2006-07: delinquencies climbed for a year under the tightest spreads on record", signal=[("flag", "blind", "==", 1)], outcome="DD20_18"),
    dict(id="H02", statement="Loss data in their top 15 % of stress precede a 20 % drawdown within 18 months",
         lesson="2007-08, 1990: the loss rates led", signal=[("pillar", "loss", ">=", 85)], outcome="DD20_18"),
    dict(id="H03", statement="An inverted yield curve (10y below 3m) precedes a recession within 24 months",
         lesson="1989, 2000, 2006-07, 2019", signal=[("x", "curve", "<", 0)], outcome="REC24"),
    dict(id="H04", statement="Fed funds up 1.5 pts or more in 12 months precede a 15 % drawdown within 12 months",
         lesson="1994, 2000, 2018, 2022", signal=[("x", "fed_tight", ">=", 1.5)], outcome="DD15_12"),
    dict(id="H05", statement="Oil up 50 % or more in a year precedes a recession within 12 months",
         lesson="1973, 1990, 2008", signal=[("x", "oil_shock", ">=", 50)], outcome="REC12"),
    dict(id="H06", statement="Stock market value / GDP in its top decile is followed by below-median 12-month returns",
         lesson="2000, 2021", signal=[("score", "val_gdp", ">=", 90)], outcome="ABOVE_MED12", expect="lower"),
    dict(id="H07", statement="Top-decile valuation while the Fed tightens precedes a 20 % drawdown within 18 months",
         lesson="2000 (and 1987, 2022): a rate shock into prices far ahead of profits",
         signal=[("score", "val_gdp", ">=", 90), ("x", "fed_tight", ">=", 1.0)], outcome="DD20_18"),
    dict(id="H08", statement="Initial claims up 20 % or more year on year precede a recession within 12 months",
         lesson="labour turns before the recession is declared", signal=[("x", "claims", ">=", 20)], outcome="REC12"),
    dict(id="H09", statement="The marginal buyer stepping back (commercial paper or asset-backed paper at a stress extreme) precedes a 15 % drawdown within 12 months",
         lesson="summer 2007: the buyers of ABCP and CDOs stopped", signal=[("anyscore", ("cp", "abcp"), ">=", 85)], outcome="DD15_12"),
    dict(id="H10", statement="Banks tightening business credit (net 30 %+) precede a recession within 12 months",
         lesson="1990, 2000-01, 2007-08", signal=[("x", "sloos_ci", ">=", 30)], outcome="REC12"),
    dict(id="H11", statement="Organic expansion is followed by an above-median 12-month return",
         lesson="growth on income, not debt", signal=[("regime", "organic expansion")], outcome="ABOVE_MED12"),
    dict(id="H12", statement="Debt-fuelled late cycle precedes a 20 % drawdown within 18 months",
         lesson="credit outrunning income ends in a de-leveraging", signal=[("regime", "late cycle (debt-fuelled)")], outcome="DD20_18"),
    dict(id="H13", statement="Acute stress (funding markets in their top decile) is followed by an above-median 12-month return (capitulation)",
         lesson="2009-03, 2020-03: the panic was the bottom - or 2008-10: it wasn't", signal=[("regime", "acute stress")], outcome="ABOVE_MED12"),
    dict(id="H14", statement="Baa spreads widening 0.75 pt or more in 6 months precede a recession within 12 months",
         lesson="credit leads equities into recessions", signal=[("x", "baa_d", ">=", 0.75)], outcome="REC12"),
    dict(id="H15", statement="Composite risk at the 90th percentile or higher precedes a 20 % drawdown within 12 months",
         lesson="the composite itself", signal=[("gauge", "compositeP", ">=", 90)], outcome="DD20_12"),
    dict(id="H16", statement="Euphoria (Nasdaq up 50 % or more in a year) precedes a 20 % drawdown within 18 months",
         lesson="1999-2000, 2020-21", signal=[("x", "euphoria", ">=", 50)], outcome="DD20_18"),
    dict(id="H17", statement="A rate shock (10-year yield up 1.5 pts in 6 months) precedes a 10 % drawdown within 6 months",
         lesson="1987, 1994, 2022", signal=[("x", "rate_shock", ">=", 1.5)], outcome="DD10_6"),
    dict(id="H18", statement="Commercial real-estate delinquencies in their top decile precede a recession within 12 months",
         lesson="1990 (S&L / CRE), 2008", signal=[("score", "cre_dq", ">=", 90)], outcome="REC12"),
]
STATUS_RULES = "supported: 12+ signal months, 2+ distinct episodes caught, lift >= 1.5, permutation p <= 0.10 · rejected: 12+ signal months and lift <= 1.0 or p >= 0.5 · else inconclusive"
LEDGER_KEEP = 4000


# ------------------------------------------------------------------------------------------------------------------
#  outcomes
# ------------------------------------------------------------------------------------------------------------------
class Market:
    """Daily closes (for drawdowns / returns) and recession starts."""
    def __init__(self, dates, vals, rec_starts):
        self.d, self.v = list(dates or []), list(vals or [])
        self.rec = sorted(rec_starts or [])
        self._med = None

    def last(self):
        return self.d[-1] if self.d else None

    def window(self, t, days):
        i = bisect.bisect_right(self.d, t) - 1
        if i < 0 or not self.d or self.d[-1] < t + timedelta(days=days):
            return None
        j = bisect.bisect_right(self.d, t + timedelta(days=days)) - 1
        return self.v[i], self.v[i + 1:j + 1], self.v[j]

    def median12(self, months):
        if self._med is None:
            rets = []
            for t in months:
                w = self.window(t, 365)
                if w and w[0] > 0:
                    rets.append(w[2] / w[0] - 1)
            rets.sort()
            self._med = rets[len(rets) // 2] if rets else 0.0
        return self._med

    def outcome(self, code, t, months=None):
        """1 / 0, or None when not yet known (the horizon hasn't passed) or no data."""
        _, days, (kind, thr) = OUTCOMES[code]
        if kind == "rec":
            if not self.rec:
                return None
            last_known = self.d[-1] if self.d else None
            if last_known and t + timedelta(days=days) > last_known + timedelta(days=200):
                return None             # recessions are declared late; don't call it 'no recession' too soon
            return int(any(t < s <= t + timedelta(days=days) for s in self.rec))
        w = self.window(t, days)
        if not w or w[0] <= 0 or not w[1]:
            return None
        p0, path, p1 = w
        if kind == "dd":
            return int(min(path) / p0 - 1 <= thr)
        if kind == "ret":
            return int(p1 / p0 - 1 > self.median12(months or []))
        return None


# ------------------------------------------------------------------------------------------------------------------
#  signals
# ------------------------------------------------------------------------------------------------------------------
def cond_value(row, c):
    kind = c[0]
    if kind == "flag":
        return 1 if row.get(c[1]) else 0
    if kind == "pillar":
        return (row.get("pillars") or {}).get(c[1])
    if kind == "score":
        return (row.get("scores") or {}).get(c[1])
    if kind == "x":
        return (row.get("xs") or {}).get(c[1])
    if kind == "gauge":
        return row.get(c[1])
    if kind == "regime":
        return row.get("regime")
    if kind == "anyscore":
        vs = [(row.get("scores") or {}).get(k) for k in c[1]]
        vs = [v for v in vs if v is not None]
        return max(vs) if vs else None
    return None


def _cmp(v, op, thr):
    if v is None:
        return None
    if op == ">=":
        return v >= thr
    if op == "<=":
        return v <= thr
    if op == ">":
        return v > thr
    if op == "<":
        return v < thr
    if op == "==":
        return v == thr
    return None


def signal_on(row, conds):
    """True / False, or None when an input is missing."""
    out = True
    for c in conds:
        if c[0] == "regime":
            v = row.get("regime")
            if v is None:
                return None
            ok = v == c[1]
        else:
            ok = _cmp(cond_value(row, c), c[2], c[3])
            if ok is None:
                return None
        out = out and ok
    return out


def describe(conds, labels):
    parts = []
    for c in conds:
        if c[0] == "regime":
            parts.append(f"regime = {c[1]}")
        elif c[0] == "flag":
            parts.append(f"{c[1]} flag on")
        elif c[0] == "anyscore":
            parts.append(f"max({', '.join(labels.get(k, (k,))[0] for k in c[1])}) {c[2]} {c[3]}")
        else:
            nm = labels.get(c[1], (c[1],))[0] if c[0] in ("score", "x") else c[1]
            parts.append(f"{nm} {'score' if c[0] == 'score' else 'value' if c[0] == 'x' else ''} {c[2]} {c[3]}".replace("  ", " "))
    return " AND ".join(parts)


# ------------------------------------------------------------------------------------------------------------------
#  testing
# ------------------------------------------------------------------------------------------------------------------
def _episodes(ys_idx, months, gap=12):
    """Distinct events: outcome-positive months closer than `gap` months belong to one episode."""
    eps, last = [], None
    for i in ys_idx:
        if last is None or i - last > gap:
            eps.append([i])
        else:
            eps[-1].append(i)
        last = i
    return eps


class Panel:
    """The monthly rows with every outcome computed once (outcomes are the slow part)."""
    def __init__(self, rows, market):
        self.rows, self.market = rows, market
        self.months = [r["t"] for r in rows]
        self._y = {}

    def y(self, code):
        if code not in self._y:
            self._y[code] = [self.market.outcome(code, t, self.months) for t in self.months]
        return self._y[code]


def _bits(xs):
    v = 0
    for i, x in enumerate(xs):
        if x:
            v |= 1 << i
    return v


def test(panel, conds, outcome, expect="higher", shifts=240):
    """-> dict(n, n1, p0, p1, lift, p, caught, events, status) on the months where both signal and outcome are known.
    p: circular-shift permutation on bitsets (each shift slides the whole signal along history)."""
    rows = panel.rows
    ys = panel.y(outcome)
    sig = [signal_on(r, conds) for r in rows]
    idx = [i for i in range(len(rows)) if sig[i] is not None and ys[i] is not None]
    n = len(idx)
    res = {"n": n, "n1": 0, "p0": None, "p1": None, "lift": None, "p": None, "caught": 0, "events": 0, "status": "untestable"}
    if n < 60:
        return res
    s = [1 if sig[i] else 0 for i in idx]
    y = [ys[i] for i in idx]
    n1 = sum(s)
    p0 = sum(y) / n
    res.update(n1=n1, p0=round(p0, 4))
    pos = [k for k in range(n) if y[k]]
    ev = _episodes(pos, idx)
    res["events"] = len(ev)
    if n1 == 0:
        res["status"] = "inconclusive"
        return res
    sb, yb = _bits(s), _bits(y)
    hits = (sb & yb).bit_count()
    p1 = hits / n1
    lift = (p1 / p0) if p0 > 0 else None
    caught = 0
    for e in ev:
        if any(s[j] for j in range(max(0, e[0] - 18), min(n, e[-1] + 1))):
            caught += 1
    mask = (1 << n) - 1
    beat = tot = 0
    step = max(1, (n - 24) // shifts)
    for sh in range(12, n - 12, step):
        rot = ((sb << sh) | (sb >> (n - sh))) & mask
        q = (rot & yb).bit_count()
        tot += 1
        if (q >= hits if expect == "higher" else q <= hits):
            beat += 1
    p = (beat + 1) / (tot + 1) if tot else None
    res.update(p1=round(p1, 4), lift=None if lift is None else round(lift, 3), p=None if p is None else round(p, 4), caught=caught)
    good = (lift is not None and (lift >= 1.5 if expect == "higher" else lift <= 1 / 1.5))
    bad = (lift is not None and (lift <= 1.0 if expect == "higher" else lift >= 1.0))
    if n1 >= 12 and caught >= 2 and good and p is not None and p <= 0.10:
        res["status"] = "supported"
    elif n1 >= 12 and (bad or (p is not None and p >= 0.5)):
        res["status"] = "rejected"
    else:
        res["status"] = "inconclusive"
    return res


def bh(pvals, q=0.10):
    """Benjamini-Hochberg: indices of discoveries at false-discovery rate q."""
    order = sorted((p, i) for i, p in enumerate(pvals) if p is not None)
    m = len(order)
    keep = set()
    kmax = 0
    for rank, (p, i) in enumerate(order, 1):
        if p <= q * rank / m:
            kmax = rank
    for rank, (p, i) in enumerate(order, 1):
        if rank <= kmax:
            keep.add(i)
    return keep


def explore(panel, indicators, today, labels, max_pairs=60):
    """Every indicator at its extremes vs. drawdowns and recessions, plus a rotating slice of pairs. -> discoveries."""
    cands = []
    for k, pillar in indicators:
        if pillar == "growth":
            cands.append(([("score", k, "<=", 10)], "REC12"))
        else:
            for thr in (85, 95):
                for oc in ("DD20_18", "REC12"):
                    cands.append(([("score", k, ">=", thr)], oc))
    stress = [k for k, p in indicators if p != "growth"]
    pairs = [(a, b) for i, a in enumerate(stress) for b in stress[i + 1:]]
    if pairs:
        seed = int(hashlib.sha256(today.isoformat().encode()).hexdigest()[:8], 16)
        start = seed % len(pairs)
        for a, b in (pairs[start:] + pairs[:start])[:max_pairs]:
            cands.append(([("score", a, ">=", 85), ("score", b, ">=", 85)], "DD20_18"))
    tested = []
    for conds, oc in cands:
        r = test(panel, conds, oc, shifts=160)
        if r["n1"] >= 12 and r["p"] is not None:
            tested.append((conds, oc, r))
    keep = bh([t[2]["p"] for t in tested], 0.10)
    out = []
    for i in sorted(keep, key=lambda i: tested[i][2]["p"]):
        conds, oc, r = tested[i]
        if (r.get("lift") or 0) < 1.5 or r["caught"] < 2:
            continue
        out.append({"id": "X" + hashlib.sha1(repr((conds, oc)).encode()).hexdigest()[:8], "signal": [list(c) for c in conds], "outcome": oc,
                    "statement": f"{describe(conds, labels)} -> {OUTCOMES[oc][0]}", **r, "status": "discovered (exploratory)"})
    return {"tested": len(tested), "candidates": len(cands), "discoveries": out[:25], "fdr": 0.10}


# ------------------------------------------------------------------------------------------------------------------
#  the ledger: predictions made now, graded when their horizon passes
# ------------------------------------------------------------------------------------------------------------------
def update_ledger(ledger, hyps, now_row, today, market):
    month = today.strftime("%Y-%m")
    have = {(p["h"], p["month"]) for p in ledger}
    for h in hyps:
        on = signal_on(now_row, [tuple(c) for c in h["signal"]]) if now_row else None
        h["nowOn"] = bool(on) if on is not None else None
        if on and (h["id"], month) not in have:
            days = OUTCOMES[h["outcome"]][1]
            ledger.append({"h": h["id"], "month": month, "opened": today.isoformat(), "due": (today + timedelta(days=days)).isoformat(),
                           "outcome": h["outcome"], "expect": h.get("expect", "higher"), "result": None})
    for p in ledger:
        if p.get("result") is None and p["due"] <= today.isoformat():
            y = market.outcome(p["outcome"], date.fromisoformat(p["opened"]))
            if y is not None:
                hit = bool(y) if p.get("expect", "higher") == "higher" else (not y)
                p["result"] = "right" if hit else "wrong"
                p["graded"] = today.isoformat()
    ledger[:] = ledger[-LEDGER_KEEP:]
    graded = [p for p in ledger if p.get("result")]
    return {"opened": len(ledger), "pending": sum(1 for p in ledger if not p.get("result")), "graded": len(graded),
            "right": sum(1 for p in graded if p["result"] == "right"), "wrong": sum(1 for p in graded if p["result"] == "wrong"),
            "since": ledger[0]["opened"] if ledger else None}


def run_lab(rows, market, now_row, today, labels, indicators, prev=None):
    """Re-test every hypothesis, explore, update the prediction ledger. -> the lab document.
    labels: {key: (label, pillar)} · indicators: [(key, pillar)] for exploration."""
    prev = prev or {}
    panel = Panel(rows, market)
    hyps = []
    for h in SEEDED:
        r = test(panel, h["signal"], h["outcome"], expect=h.get("expect", "higher"))
        hyps.append({"id": h["id"], "statement": h["statement"], "lesson": h["lesson"], "signal": [list(c) for c in h["signal"]],
                     "outcome": h["outcome"], "expect": h.get("expect", "higher"), "kind": "seeded", **r})
    ex = explore(panel, indicators, today, labels)
    # discoveries become standing hypotheses: tested every run from now on, and graded live
    keep = {h["id"]: h for h in (prev.get("auto") or [])}
    for d in ex["discoveries"]:
        keep.setdefault(d["id"], {"id": d["id"], "statement": d["statement"], "signal": d["signal"], "outcome": d["outcome"],
                                  "expect": "higher", "kind": "auto", "found": today.isoformat()})
    auto = []
    for h in list(keep.values())[-40:]:
        r = test(panel, [tuple(c) for c in h["signal"]], h["outcome"])
        auto.append(dict(h, **r))
    ledger = list(prev.get("ledger") or [])
    record = update_ledger(ledger, hyps + auto, now_row, today, market)
    counts = {}
    for h in hyps + auto:
        counts[h["status"]] = counts.get(h["status"], 0) + 1
    return {"asOf": today.isoformat(), "rules": STATUS_RULES, "hypotheses": hyps, "auto": auto, "exploration": ex,
            "ledger": ledger, "record": record, "counts": counts,
            "method": ("Signals on the publication-lagged monthly panel; outcomes from daily closes and NBER recession starts; "
                       "p-values by circular-shift permutation (the signal slid along history keeps its persistence); "
                       "exploration controls the false-discovery rate (Benjamini-Hochberg, 10 %); every 'on' signal is logged "
                       "as a dated prediction and graded when its horizon passes.")}
