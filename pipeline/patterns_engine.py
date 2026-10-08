#!/usr/bin/env python3
"""
patterns_engine.py - technical templates & chart patterns with a falsifiable
track record. Pure functions over a daily close series; no I/O.

Every template states its rules up front, detects WITHOUT LOOKAHEAD (a pattern
is only "identified" on the bar where all of its pivots were knowable), and is
then judged by what price actually did afterwards. The scanner logs every
identification and its outcome, so each template earns (or loses) its
credibility statistically - per ticker and across the universe.

Data note: the books store DAILY CLOSES (no intraday high/low), so pivots and
breaks are close-based. That is stricter than wick-based charting (a pattern
must hold on closing prices), and the rules below are written for it.

Templates
---------
  minervini      Minervini Trend Template - 10 criteria (RS >= 70, price above
                 the 50/150/200-day SMAs, SMAs stacked, 200-day rising, >= 30%
                 above the 52-week low, within 25% of the 52-week high).
                 Signal: a FRESH 10/10 (score was < 10 for the previous 10 bars).
                 Judged on the 63-day (3-month) return vs SPY.
  hs_top         Head & Shoulders top (bearish reversal)
  hs_bottom      Inverse Head & Shoulders (bullish reversal)
  double_top     Double top (bearish reversal)
  double_bottom  Double bottom (bullish reversal)

Pattern lifecycle (hs_*, double_*)
----------------------------------
  identified  the last pivot is confirmed and every rule passes
  confirmed   close beyond the neckline within CONFIRM_BARS of identification
  invalidated price closes beyond the pattern's extreme first (the opposite
              breakout) - the pattern was MISIDENTIFIED: unsuccessful
  expired     neither within CONFIRM_BARS - never confirmed: unsuccessful
  success     after confirmation, the measured-move target is reached before
              the stop (pattern extreme) and within OUTCOME_BARS
  failure     after confirmation, the stop is hit first: unsuccessful
  timeout     after confirmation, neither within OUTCOME_BARS (return recorded)
  open        not enough bars yet to judge (live)
"""
import math

ENGINE_VERSION = "patterns-v1"
CONFIRM_BARS = 40
OUTCOME_BARS = 60
MINERVINI_FWD = 63
FWD_BARS = 20            # standard forward-return horizon reported for every signal

TEMPLATES = {
    "minervini":     {"label": "Minervini Trend Template", "direction": "bullish", "kind": "template"},
    "hs_top":        {"label": "Head & Shoulders (top)", "direction": "bearish", "kind": "pattern"},
    "hs_bottom":     {"label": "Inverse Head & Shoulders", "direction": "bullish", "kind": "pattern"},
    "double_top":    {"label": "Double Top", "direction": "bearish", "kind": "pattern"},
    "double_bottom": {"label": "Double Bottom", "direction": "bullish", "kind": "pattern"},
}


# ------------------------------------------------------------- indicators ----
def sma_series(c, n):
    out = [None] * len(c)
    s = 0.0
    for i, v in enumerate(c):
        s += v
        if i >= n:
            s -= c[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rolling_sd_returns(c, n=100):
    """sd of daily returns over the trailing n bars, known at each bar."""
    out = [None] * len(c)
    rets = [0.0] + [(c[i] / c[i - 1] - 1) if c[i - 1] else 0.0 for i in range(1, len(c))]
    s = s2 = 0.0
    for i in range(1, len(c)):
        r = rets[i]
        s += r
        s2 += r * r
        if i > n:
            o = rets[i - n]
            s -= o
            s2 -= o * o
        k = min(i, n)
        if k >= 20:
            var = max(0.0, (s2 - s * s / k) / (k - 1))
            out[i] = math.sqrt(var)
    return out


def zigzag_threshold(sd):
    """Reversal size that counts as a swing: 2 sd x sqrt(5 days), 3%..12%."""
    if sd is None:
        return 0.05
    return max(0.03, min(0.12, 2.0 * sd * math.sqrt(5)))


def zigzag(c):
    """Swing pivots with NO lookahead. Each pivot carries `confirm` = the bar on
    which the reversal from it first reached the threshold - the earliest bar a
    trader could have known it was a pivot.
    -> [{"i", "price", "type": "H"|"L", "confirm"}]"""
    if len(c) < 30:
        return []
    sd = rolling_sd_returns(c)
    pivots = []
    trend = 0                  # +1 tracking a high, -1 tracking a low, 0 unknown
    ext_i = 0
    hi_i, lo_i = 0, 0
    for j in range(1, len(c)):
        thr = zigzag_threshold(sd[j])
        if trend == 0:
            if c[j] > c[hi_i]:
                hi_i = j
            if c[j] < c[lo_i]:
                lo_i = j
            if c[hi_i] >= c[lo_i] * (1 + thr) and hi_i > lo_i:
                pivots.append({"i": lo_i, "price": c[lo_i], "type": "L", "confirm": j})
                trend, ext_i = 1, hi_i
            elif c[lo_i] <= c[hi_i] * (1 - thr) and lo_i > hi_i:
                pivots.append({"i": hi_i, "price": c[hi_i], "type": "H", "confirm": j})
                trend, ext_i = -1, lo_i
            continue
        if trend == 1:
            if c[j] >= c[ext_i]:
                ext_i = j
            elif c[j] <= c[ext_i] * (1 - thr):
                pivots.append({"i": ext_i, "price": c[ext_i], "type": "H", "confirm": j})
                trend, ext_i = -1, j
        else:
            if c[j] <= c[ext_i]:
                ext_i = j
            elif c[j] >= c[ext_i] * (1 + thr):
                pivots.append({"i": ext_i, "price": c[ext_i], "type": "L", "confirm": j})
                trend, ext_i = 1, j
    return pivots


def _line(p1, p2, x):
    (x1, y1), (x2, y2) = p1, p2
    if x2 == x1:
        return y1
    return y1 + (y2 - y1) * (x - x1) / (x2 - x1)


# ------------------------------------------------------- pattern rules ----
def _crit(name, ok, value=None, need=None):
    return {"rule": name, "pass": bool(ok), "value": value, "need": need}


def hs_rules(c, piv, bearish=True):
    """Rules for a 5-pivot window [LS, T1, H, T2, RS]. For the inverse pattern the
    same rules run on the mirrored series (bearish=False)."""
    s = 1 if bearish else -1
    LS, T1, H, T2, RS = piv
    v = lambda p: s * p["price"]                     # mirrored price for inverse
    neck_at = lambda x: _line((T1["i"], v(T1)), (T2["i"], v(T2)), x)
    height = v(H) - neck_at(H["i"])
    pre_lo = min(v({"price": x}) for x in c[max(0, LS["i"] - 60):LS["i"] + 1])
    left = H["i"] - LS["i"]
    right = RS["i"] - H["i"]
    span = RS["i"] - LS["i"]
    crit = [
        _crit("Prior trend into the pattern", v(LS) >= pre_lo + 0.5 * max(height, 1e-9),
              round(v(LS) - pre_lo, 4), ">= 0.5x pattern height"),
        _crit("Head beyond both shoulders", v(H) > v(LS) + 0.15 * height and v(H) > v(RS) + 0.15 * height,
              round(min(v(H) - v(LS), v(H) - v(RS)) / height, 3) if height > 0 else None, ">= 0.15x height"),
        _crit("Shoulders level (price symmetry)", height > 0 and abs(v(LS) - v(RS)) <= 0.35 * height,
              round(abs(v(LS) - v(RS)) / height, 3) if height > 0 else None, "<= 0.35x height"),
        _crit("Time symmetry", right > 0 and 0.4 <= left / right <= 2.5,
              round(left / right, 2) if right else None, "left/right 0.4-2.5"),
        _crit("Neckline not too skewed", height > 0 and abs(v(T2) - v(T1)) <= 0.35 * height,
              round(abs(v(T2) - v(T1)) / height, 3) if height > 0 else None, "<= 0.35x height"),
        _crit("Shoulders clear of the neckline", height > 0 and v(LS) - neck_at(LS["i"]) >= 0.25 * height
              and v(RS) - neck_at(RS["i"]) >= 0.25 * height, None, ">= 0.25x height"),
        _crit("Duration 15-250 bars", 15 <= span <= 250, span, "15-250"),
    ]
    geo = {"neckline": [[T1["i"], T1["price"]], [T2["i"], T2["price"]]], "height": round(height, 6)}
    return crit, geo


def double_rules(c, piv, bearish=True):
    """Rules for a 3-pivot window [P1, T, P2] (tops: H,L,H; bottoms: L,H,L)."""
    s = 1 if bearish else -1
    P1, T, P2 = piv
    v = lambda p: s * p["price"]
    height = (v(P1) + v(P2)) / 2 - v(T)
    pre_lo = min(s * x for x in c[max(0, P1["i"] - 60):P1["i"] + 1])
    sep = P2["i"] - P1["i"]
    crit = [
        _crit("Prior trend into the pattern", v(P1) >= pre_lo + 0.5 * max(height, 1e-9),
              round(v(P1) - pre_lo, 4), ">= 0.5x pattern height"),
        _crit("Peaks level", height > 0 and abs(v(P1) - v(P2)) <= 0.20 * height,
              round(abs(v(P1) - v(P2)) / height, 3) if height > 0 else None, "<= 0.20x height"),
        _crit("Meaningful depth", height > 0 and height / abs(P1["price"]) >= 0.04,
              round(height / abs(P1["price"]), 3) if P1["price"] else None, ">= 4% of price"),
        _crit("Separation 10-150 bars", 10 <= sep <= 150, sep, "10-150"),
    ]
    geo = {"neckline": [[T["i"], T["price"]], [P2["i"], T["price"]]], "height": round(height, 6)}
    return crit, geo


def _resolve(c, start, direction, neck_fn, extreme, height):
    """Confirmation + outcome, scanning ONLY bars after identification.
    direction: -1 bearish (break DOWN through neckline), +1 bullish."""
    n = len(c)
    out = {"status": "open", "confirmIdx": None, "exitIdx": None, "target": None, "stop": extreme}
    conf = None
    for j in range(start + 1, min(n, start + 1 + CONFIRM_BARS)):
        if direction < 0 and c[j] > extreme or direction > 0 and c[j] < extreme:
            out.update(status="invalidated", exitIdx=j)
            return out
        if direction < 0 and c[j] < neck_fn(j) or direction > 0 and c[j] > neck_fn(j):
            conf = j
            break
    if conf is None:
        if start + CONFIRM_BARS < n:
            out["status"] = "expired"
        return out
    entry = c[conf]
    target = neck_fn(conf) + direction * height
    out.update(status="confirmed", confirmIdx=conf, entry=entry, target=round(target, 6))
    mfe = mae = 0.0
    for j in range(conf + 1, min(n, conf + 1 + OUTCOME_BARS)):
        r = direction * (c[j] / entry - 1)
        mfe, mae = max(mfe, r), min(mae, r)
        if direction < 0 and c[j] <= target or direction > 0 and c[j] >= target:
            out.update(status="success", exitIdx=j)
            break
        if direction < 0 and c[j] > extreme or direction > 0 and c[j] < extreme:
            out.update(status="failure", exitIdx=j)
            break
    else:
        if conf + OUTCOME_BARS < n:
            out.update(status="timeout", exitIdx=conf + OUTCOME_BARS)
    if out["exitIdx"] is not None:
        out["returnPct"] = round(direction * (c[out["exitIdx"]] / entry - 1) * 100, 2)
    out["mfePct"], out["maePct"] = round(mfe * 100, 2), round(mae * 100, 2)
    if conf + FWD_BARS < n:
        out["fwd20Pct"] = round(direction * (c[conf + FWD_BARS] / entry - 1) * 100, 2)
    return out


def detect_patterns(c, pivots=None, templates=("hs_top", "hs_bottom", "double_top", "double_bottom")):
    """All historical + current identifications. Each is knowable on its
    `identIdx` bar; resolution uses only later bars."""
    pivots = pivots if pivots is not None else zigzag(c)
    found = []
    seqs = {"hs_top": ("HLHLH", 5, hs_rules, True), "hs_bottom": ("LHLHL", 5, hs_rules, False),
            "double_top": ("HLH", 3, double_rules, True), "double_bottom": ("LHL", 3, double_rules, False)}
    types = "".join(p["type"] for p in pivots)
    for key in templates:
        pat, k, rules, bearish = seqs[key]
        for a in range(0, len(pivots) - k + 1):
            if types[a:a + k] != pat:
                continue
            win = pivots[a:a + k]
            crit, geo = rules(c, win, bearish)
            if not all(x["pass"] for x in crit):
                continue
            ident = win[-1]["confirm"]
            direction = -1 if bearish else 1
            (x1, y1), (x2, y2) = geo["neckline"]
            neck = (lambda x, x1=x1, y1=y1, x2=x2, y2=y2: _line((x1, y1), (x2, y2), x))
            extreme = (max if bearish else min)(p["price"] for p in win)
            # height is positive in pattern units -> measured-move target =
            # neckline (at the break) + direction x height
            res = _resolve(c, ident, direction, neck, extreme, geo["height"])
            found.append({"template": key, "identIdx": ident, "direction": "bearish" if bearish else "bullish",
                          "pivots": [{"i": p["i"], "price": p["price"], "type": p["type"]} for p in win],
                          "neckline": geo["neckline"], "height": geo["height"], "criteria": crit, **res})
    found.sort(key=lambda d: d["identIdx"])
    return found


def latest_candidate(c, pivots, key):
    """The most recent pivot window of the right shape, with every rule's
    pass/fail - shown even when it isn't a valid pattern (manual review)."""
    seqs = {"hs_top": ("HLHLH", 5, hs_rules, True), "hs_bottom": ("LHLHL", 5, hs_rules, False),
            "double_top": ("HLH", 3, double_rules, True), "double_bottom": ("LHL", 3, double_rules, False)}
    pat, k, rules, bearish = seqs[key]
    types = "".join(p["type"] for p in pivots)
    for a in range(len(pivots) - k, -1, -1):
        if types[a:a + k] == pat:
            win = pivots[a:a + k]
            crit, geo = rules(c, win, bearish)
            return {"template": key, "pivots": [{"i": p["i"], "price": p["price"], "type": p["type"]} for p in win],
                    "neckline": geo["neckline"], "criteria": crit, "passes": sum(x["pass"] for x in crit),
                    "of": len(crit), "valid": all(x["pass"] for x in crit), "identIdx": win[-1]["confirm"]}
    return None


# ------------------------------------------------------- Minervini TT ----
def rs_raw(c, i):
    """IBD-style weighted 12-month performance (40% latest quarter)."""
    if i < 252 or not c[i - 252]:
        return None
    q = lambda a, b: c[i - a] / c[i - b] - 1 if c[i - b] else 0.0
    return 0.4 * q(0, 63) + 0.2 * q(63, 126) + 0.2 * q(126, 189) + 0.2 * q(189, 252)


def minervini_series(c, rs_pct=None):
    """Per-bar Minervini evaluation. rs_pct: per-bar RS percentile (0-99) or None.
    -> list of None | {"score", "of", "criteria"}"""
    s50, s150, s200 = sma_series(c, 50), sma_series(c, 150), sma_series(c, 200)
    out = [None] * len(c)
    for i in range(len(c)):
        if i < 252 or s200[i] is None or s200[i - 22] is None:
            continue
        win = c[i - 251:i + 1]
        hi52, lo52 = max(win), min(win)
        p = c[i]
        rp = rs_pct[i] if rs_pct else None
        checks = [
            ("RP > 70", rp is not None and rp > 70),
            ("Price > SMA 50", p > s50[i]), ("Price > SMA 150", p > s150[i]), ("Price > SMA 200", p > s200[i]),
            ("SMA 50 > SMA 150", s50[i] > s150[i]), ("SMA 50 > SMA 200", s50[i] > s200[i]),
            ("SMA 150 > SMA 200", s150[i] > s200[i]),
            ("Price 30% > 52W Low", p >= lo52 * 1.30), ("Price w/in 25% of 52W High", p >= hi52 * 0.75),
            ("SMA 200 Rising", s200[i] > s200[i - 22]),
        ]
        out[i] = {"score": sum(1 for _, ok in checks if ok), "of": len(checks),
                  "criteria": [{"rule": n, "pass": bool(ok)} for n, ok in checks],
                  "values": {"RP": rp, "vs52wHighPct": round((p / hi52 - 1) * 100, 1),
                             "vs52wLowPct": round((p / lo52 - 1) * 100, 1),
                             "sma50": round(s50[i], 4), "sma150": round(s150[i], 4), "sma200": round(s200[i], 4)}}
    return out


def minervini_signals(c, ev, spy_by_idx=None):
    """Fresh 10/10 entries + their 63-day outcome vs SPY (success = beat SPY)."""
    sigs = []
    n = len(c)
    for i in range(len(c)):
        e = ev[i]
        if not e or e["score"] < e["of"]:
            continue
        prev = [ev[k]["score"] if ev[k] else 0 for k in range(max(0, i - 10), i)]
        if not prev or max(prev) >= e["of"]:
            continue
        d = {"template": "minervini", "identIdx": i, "direction": "bullish", "entry": c[i],
             "score": e["score"], "of": e["of"], "status": "open"}
        if i + FWD_BARS < n:
            d["fwd20Pct"] = round((c[i + FWD_BARS] / c[i] - 1) * 100, 2)
        if i + MINERVINI_FWD < n:
            r = c[i + MINERVINI_FWD] / c[i] - 1
            d["returnPct"] = round(r * 100, 2)
            spy_r = None
            if spy_by_idx:
                a, b = spy_by_idx(i), spy_by_idx(i + MINERVINI_FWD)
                if a and b:
                    spy_r = b / a - 1
            d["excessPct"] = round((r - spy_r) * 100, 2) if spy_r is not None else None
            beat = (r - spy_r) if spy_r is not None else r
            d["status"] = "success" if beat > 0 else "failure"
            d["exitIdx"] = i + MINERVINI_FWD
        sigs.append(d)
    return sigs


def rule_names():
    """Rule labels + thresholds in evaluation order (for compact outputs/UI)."""
    c = [100 + i for i in range(400)]
    piv5 = [{"i": 100, "price": 130, "type": "H"}, {"i": 115, "price": 115, "type": "L"},
            {"i": 135, "price": 145, "type": "H"}, {"i": 155, "price": 116, "type": "L"},
            {"i": 170, "price": 131, "type": "H"}]
    piv3 = [{"i": 100, "price": 140, "type": "H"}, {"i": 120, "price": 120, "type": "L"},
            {"i": 140, "price": 141, "type": "H"}]
    hs = [[x["rule"], x["need"]] for x in hs_rules(c, piv5, True)[0]]
    db = [[x["rule"], x["need"]] for x in double_rules(c, piv3, True)[0]]
    mv = [[x["rule"], None] for x in minervini_series([100.0 + i * 0.1 for i in range(300)], [90] * 300)[-1]["criteria"]]
    return {"minervini": mv, "hs_top": hs, "hs_bottom": hs, "double_top": db, "double_bottom": db}


# ----------------------------------------------------------- statistics ----
def wilson(k, n, z=1.96):
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, centre - half) * 100, 1), round(min(1.0, centre + half) * 100, 1)


def summarize(dets, baseline_fwd20=None):
    """Aggregate stats for a list of detections of ONE template."""
    by = {}
    for d in dets:
        by[d["status"]] = by.get(d["status"], 0) + 1
    n = len(dets)
    is_template = dets and dets[0]["template"] == "minervini"
    if is_template:
        judged = by.get("success", 0) + by.get("failure", 0)
        wins = by.get("success", 0)
    else:
        judged = sum(by.get(s, 0) for s in ("success", "failure", "timeout"))
        wins = by.get("success", 0)
    rets = [d["returnPct"] for d in dets if d.get("returnPct") is not None and d["status"] != "open"]
    f20 = [d["fwd20Pct"] for d in dets if d.get("fwd20Pct") is not None]
    lo, hi = wilson(wins, judged)
    out = {"n": n, "byStatus": by, "judged": judged, "successRate": round(wins / judged * 100, 1) if judged else None,
           "successCI95": [lo, hi], "avgReturnPct": round(sum(rets) / len(rets), 2) if rets else None,
           "avgFwd20Pct": round(sum(f20) / len(f20), 2) if f20 else None}
    if not is_template:
        conf = sum(by.get(s, 0) for s in ("confirmed", "success", "failure", "timeout"))
        denom = conf + by.get("invalidated", 0) + by.get("expired", 0)
        out["confirmRate"] = round(conf / denom * 100, 1) if denom else None
        unsuccessful = by.get("invalidated", 0) + by.get("expired", 0) + by.get("failure", 0)
        out["identificationSuccessRate"] = (round(wins / (wins + unsuccessful) * 100, 1)
                                           if (wins + unsuccessful) else None)
        out["identCI95"] = list(wilson(wins, wins + unsuccessful))
    if baseline_fwd20 is not None and out["avgFwd20Pct"] is not None:
        out["edgeVsBaselinePct"] = round(out["avgFwd20Pct"] - baseline_fwd20, 2)
    return out
