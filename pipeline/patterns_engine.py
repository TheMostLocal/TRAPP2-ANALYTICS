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
  --- v2 ---
  vcp            Volatility Contraction Pattern (Minervini): 2-3 pullbacks, each
                 shallower, rising lows, volume drying up, inside a stage-2 trend
  cup_handle     Cup & Handle (O'Neil): rounded 12-40% cup, rims level, shallow
                 handle in the upper half of the cup
  bull_flag      Bull flag: >=15% pole in <=30 bars, shallow (<=50%) pullback
  bear_flag      Bear flag: >=15% drop in <=30 bars, weak (<=50%) rebound
  asc_triangle   Ascending triangle: flat resistance, rising lows
  desc_triangle  Descending triangle: flat support, falling highs
  breakout_52w   52-week closing high out of a >=10% base on >=1.5x volume
                 (identified = confirmed on the breakout close)

Targets: H&S / doubles / triangles / cup & handle use the classic measured move
(pattern height projected from the breakout level); flags project the pole;
VCP and 52-week breakouts use 2R (twice the distance to the stop).

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

ENGINE_VERSION = "patterns-v2"
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
    "vcp":           {"label": "Volatility Contraction (VCP)", "direction": "bullish", "kind": "pattern"},
    "cup_handle":    {"label": "Cup & Handle", "direction": "bullish", "kind": "pattern"},
    "bull_flag":     {"label": "Bull Flag", "direction": "bullish", "kind": "pattern"},
    "bear_flag":     {"label": "Bear Flag", "direction": "bearish", "kind": "pattern"},
    "asc_triangle":  {"label": "Ascending Triangle", "direction": "bullish", "kind": "pattern"},
    "desc_triangle": {"label": "Descending Triangle", "direction": "bearish", "kind": "pattern"},
    "breakout_52w":  {"label": "52-Week Breakout (volume)", "direction": "bullish", "kind": "pattern"},
}
PATTERN_KEYS = ("hs_top", "hs_bottom", "double_top", "double_bottom", "vcp", "cup_handle",
                "bull_flag", "bear_flag", "asc_triangle", "desc_triangle", "breakout_52w")


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


def _pre_extreme(c, i, s, bars=60):
    """Lowest mirrored close in the `bars` before pivot i (trend into a pattern)."""
    return min(s * x for x in c[max(0, i - bars):i + 1])


def vcp_rules(c, piv, bearish=False, ctx=None):
    """[H1, L1, H2, L2 (, H3, L3)] - 2 or 3 contractions."""
    ctx = ctx or {}
    vol = ctx.get("v")
    s50, s200 = ctx.get("s50") or [], ctx.get("s200") or []
    hs, ls = piv[0::2], piv[1::2]
    depths = [(h["price"] - l["price"]) / h["price"] for h, l in zip(hs, ls)]
    h1 = hs[0]["price"]
    i0 = hs[0]["i"]
    stage2 = (i0 < len(s200) and s200[i0] is not None and s50[i0] is not None
              and c[i0] > s200[i0] and s50[i0] > s200[i0])
    shrinking = all(depths[k + 1] <= 0.8 * depths[k] for k in range(len(depths) - 1))
    rising = all(ls[k + 1]["price"] > ls[k]["price"] for k in range(len(ls) - 1))
    level = all(0.85 * h1 <= h["price"] <= 1.05 * h1 for h in hs)
    span = ls[-1]["i"] - hs[0]["i"]
    dry = None
    if vol:
        def avg(a, b):
            xs = [x for x in vol[a:b + 1] if x]
            return sum(xs) / len(xs) if xs else None
        first, last = avg(hs[0]["i"], ls[0]["i"]), avg(hs[-1]["i"], ls[-1]["i"])
        dry = (last / first) if first and last else None
    crit = [
        _crit("Stage-2 uptrend (price > SMA200, SMA50 > SMA200)", stage2),
        _crit("First pullback 8-40%", 0.08 <= depths[0] <= 0.40, round(depths[0], 3), "0.08-0.40"),
        _crit("Each pullback <= 0.8x the previous", shrinking, [round(d, 3) for d in depths], "shrinking"),
        _crit("Final contraction <= 12%", depths[-1] <= 0.12, round(depths[-1], 3), "<= 0.12"),
        _crit("Rising lows", rising),
        _crit("Highs hold the pivot zone (0.85-1.05x first high)", level),
        _crit("Base 15-250 bars", 15 <= span <= 250, span, "15-250"),
        _crit("Volume dries up (last <= 0.8x first contraction)", dry is not None and dry <= 0.8,
              round(dry, 2) if dry is not None else None, "<= 0.8 (needs volume)"),
    ]
    pivot = hs[-1]["price"]
    geo = {"neckline": [[hs[-1]["i"], pivot], [ls[-1]["i"], pivot]], "extreme": ls[-1]["price"],
           "height": round(2 * (pivot - ls[-1]["price"]), 6)}
    return crit, geo


def cup_handle_rules(c, piv, bearish=False, ctx=None):
    """[left rim H, cup low L, right rim H, handle low L]."""
    HL, LC, HR, LH = piv
    depth = (HL["price"] - LC["price"]) / HL["price"]
    cup = HR["i"] - HL["i"]
    seg = c[HL["i"]:HR["i"] + 1]
    floor = LC["price"] + (HL["price"] - LC["price"]) / 3
    rounded = sum(1 for x in seg if x <= floor) / max(1, len(seg))
    hdepth = (HR["price"] - LH["price"]) / HR["price"]
    pre = min(c[max(0, HL["i"] - 120):HL["i"] + 1])
    crit = [
        _crit("Prior uptrend >= 20% into the left rim", HL["price"] >= 1.20 * pre,
              round(HL["price"] / pre - 1, 3) if pre else None, ">= 0.20"),
        _crit("Cup depth 12-40%", 0.12 <= depth <= 0.40, round(depth, 3), "0.12-0.40"),
        _crit("Cup length 30-300 bars", 30 <= cup <= 300, cup, "30-300"),
        _crit("Rims level (right 0.90-1.05x left)", 0.90 * HL["price"] <= HR["price"] <= 1.05 * HL["price"],
              round(HR["price"] / HL["price"], 3), "0.90-1.05"),
        _crit("Rounded bottom (>= 20% of the cup in its lower third)", rounded >= 0.20, round(rounded, 2), ">= 0.20"),
        _crit("Handle <= 15% and <= half the cup depth", hdepth <= 0.15 and hdepth <= 0.5 * depth,
              round(hdepth, 3), "<= min(0.15, 0.5x cup)"),
        _crit("Handle in the upper half of the cup", LH["price"] >= LC["price"] + 0.5 * (HR["price"] - LC["price"])),
        _crit("Handle 3-50 bars", 3 <= LH["i"] - HR["i"] <= 50, LH["i"] - HR["i"], "3-50"),
    ]
    geo = {"neckline": [[HR["i"], HR["price"]], [LH["i"], HR["price"]]], "extreme": LH["price"],
           "height": round(HR["price"] - LC["price"], 6)}
    return crit, geo


def flag_rules(c, piv, bearish=False, ctx=None):
    """Bull: [L0, H1, L1] (pole up, flag down). Bear: [H0, L1, H1] mirrored."""
    s = 1 if not bearish else -1
    A, B, C = piv
    v = lambda p: s * p["price"]
    pole = abs(B["price"] / A["price"] - 1)
    retr = (v(B) - v(C)) / (v(B) - v(A)) if v(B) != v(A) else 9
    crit = [
        _crit("Pole >= 15%", pole >= 0.15, round(pole, 3), ">= 0.15"),
        _crit("Pole fast (<= 30 bars)", B["i"] - A["i"] <= 30, B["i"] - A["i"], "<= 30"),
        _crit("Flag retraces <= 50% of the pole", 0 < retr <= 0.5, round(retr, 3), "<= 0.50"),
        _crit("Flag 3-30 bars", 3 <= C["i"] - B["i"] <= 30, C["i"] - B["i"], "3-30"),
    ]
    geo = {"neckline": [[B["i"], B["price"]], [C["i"], B["price"]]], "extreme": C["price"],
           "height": round(abs(B["price"] - A["price"]), 6)}
    return crit, geo


def triangle_rules(c, piv, bearish=False, ctx=None):
    """Ascending: [H1, L1, H2, L2] flat top, rising lows. Descending (bearish):
    [L1, H1, L2, H2] mirrored (flat bottom, falling highs)."""
    s = 1 if not bearish else -1
    P1, T1, P2, T2 = piv
    v = lambda p: s * p["price"]
    top = max(v(P1), v(P2))
    height = top - v(T1)
    pre = _pre_extreme(c, P1["i"], s)
    crit = [
        _crit("Prior trend into the pattern", v(P1) >= pre + 0.5 * max(height, 1e-9)),
        _crit("Flat edge (within 3%)", abs(P2["price"] - P1["price"]) <= 0.03 * abs(P1["price"]),
              round(abs(P2["price"] / P1["price"] - 1), 3), "<= 0.03"),
        _crit("Converging side (>= 0.25x height)", height > 0 and v(T2) - v(T1) >= 0.25 * height,
              round((v(T2) - v(T1)) / height, 3) if height > 0 else None, ">= 0.25"),
        _crit("Height >= 5% of price", height / abs(top) >= 0.05 if top else False,
              round(height / abs(top), 3) if top else None, ">= 0.05"),
        _crit("Duration 15-150 bars", 15 <= T2["i"] - P1["i"] <= 150, T2["i"] - P1["i"], "15-150"),
    ]
    level = s * top
    geo = {"neckline": [[P1["i"], level], [T2["i"], level]], "extreme": T2["price"], "height": round(height, 6)}
    return crit, geo


# shape (pivot types), rules, bearish
SPECS = {
    "hs_top":        (("HLHLH",), hs_rules, True),
    "hs_bottom":     (("LHLHL",), hs_rules, False),
    "double_top":    (("HLH",), double_rules, True),
    "double_bottom": (("LHL",), double_rules, False),
    "vcp":           (("HLHLHL", "HLHL"), vcp_rules, False),     # longest valid wins
    "cup_handle":    (("HLHL",), cup_handle_rules, False),
    "bull_flag":     (("LHL",), flag_rules, False),
    "bear_flag":     (("HLH",), flag_rules, True),
    "asc_triangle":  (("HLHL",), triangle_rules, False),
    "desc_triangle": (("LHLH",), triangle_rules, True),
}


def _rules(key, c, win, ctx):
    shapes, fn, bearish = SPECS[key]
    if fn in (hs_rules, double_rules):
        return fn(c, win, bearish)
    return fn(c, win, bearish, ctx)


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
    out.update(_outcome(c, conf, direction, neck_fn(conf) + direction * height, extreme))
    return out


def _outcome(c, conf, direction, target, stop):
    """After confirmation on bar `conf`: target before stop within OUTCOME_BARS."""
    n = len(c)
    entry = c[conf]
    out = {"status": "confirmed", "confirmIdx": conf, "entry": entry, "target": round(target, 6),
           "stop": stop, "exitIdx": None}
    extreme = stop
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


def _ctx(c, v=None):
    return {"v": v, "s50": sma_series(c, 50), "s200": sma_series(c, 200)}


def detect_patterns(c, pivots=None, templates=PATTERN_KEYS, v=None, ctx=None):
    """All historical + current identifications. Each is knowable on its
    `identIdx` bar; resolution uses only later bars."""
    pivots = pivots if pivots is not None else zigzag(c)
    ctx = ctx or _ctx(c, v)
    found = []
    types = "".join(p["type"] for p in pivots)
    for key in templates:
        if key == "breakout_52w":
            found += detect_breakouts(c, v, ctx)
            continue
        shapes, fn, bearish = SPECS[key]
        taken = set()
        for shape in shapes:                       # longer shapes first (VCP)
            k = len(shape)
            for a in range(0, len(pivots) - k + 1):
                if types[a:a + k] != shape:
                    continue
                win = pivots[a:a + k]
                ident = win[-1]["confirm"]
                if ident in taken:
                    continue
                crit, geo = _rules(key, c, win, ctx)
                if not all(x["pass"] for x in crit):
                    continue
                taken.add(ident)
                direction = -1 if bearish else 1
                (x1, y1), (x2, y2) = geo["neckline"]
                neck = (lambda x, x1=x1, y1=y1, x2=x2, y2=y2: _line((x1, y1), (x2, y2), x))
                extreme = geo.get("extreme")
                if extreme is None:
                    extreme = (max if bearish else min)(p["price"] for p in win)
                res = _resolve(c, ident, direction, neck, extreme, geo["height"])
                found.append({"template": key, "identIdx": ident, "direction": "bearish" if bearish else "bullish",
                              "pivots": [{"i": p["i"], "price": p["price"], "type": p["type"]} for p in win],
                              "neckline": geo["neckline"], "height": geo["height"], "criteria": crit, **res})
    found.sort(key=lambda d: d["identIdx"])
    return found


def _rolling(c, n, fn):
    """Rolling max/min of the n bars BEFORE i (exclusive), O(len) via deque."""
    from collections import deque
    out = [None] * len(c)
    dq = deque()
    better = (lambda a, b: a >= b) if fn is max else (lambda a, b: a <= b)
    for i in range(len(c)):
        while dq and dq[0] < i - n:
            dq.popleft()
        if i >= n:
            out[i] = c[dq[0]]
        while dq and better(c[i], c[dq[-1]]):
            dq.pop()
        dq.append(i)
    return out


def breakout_eval(c, v, i, hi252, lo60, vavg50, hi_prev20):
    """Rules for a 52-week breakout on bar i (lists every rule, pass or fail)."""
    prior_hi = hi252[i]
    vr = (v[i] / vavg50[i]) if (v and v[i] and vavg50[i]) else None
    base = (1 - lo60[i] / prior_hi) if (prior_hi and lo60[i]) else None
    crit = [
        _crit("New 52-week closing high", prior_hi is not None and c[i] > prior_hi,
              round(c[i] / prior_hi - 1, 4) if prior_hi else None, "> 0"),
        _crit("Fresh (no 52-week high in the prior 20 bars)", not _made_high_recently(c, i, hi252)),
        _crit("Out of a base (>= 10% pullback in the last 60 bars)", base is not None and base >= 0.10,
              round(base, 3) if base is not None else None, ">= 0.10"),
        _crit("Volume >= 1.5x the 50-day average", vr is not None and vr >= 1.5,
              round(vr, 2) if vr is not None else None, ">= 1.5 (needs volume)"),
    ]
    return crit


def _made_high_recently(c, i, hi252, bars=20):
    for k in range(max(253, i - bars), i):
        if hi252[k] is not None and c[k] > hi252[k]:
            return True
    return False


def _breakout_series(c, v):
    hi252 = _rolling(c, 252, max)
    lo60 = _rolling(c, 60, min)
    hi_prev20 = _rolling(c, 20, max)
    vavg50 = [None] * len(c)              # mean volume of the 50 bars BEFORE i
    if v:
        pre = [0.0]
        for x in v:
            pre.append(pre[-1] + (x or 0))
        for i in range(50, len(c)):
            m = (pre[i] - pre[i - 50]) / 50
            vavg50[i] = m if m > 0 else None
    return hi252, lo60, vavg50, hi_prev20


def detect_breakouts(c, v, ctx=None):
    if not v or len(c) < 260:
        return []
    hi252, lo60, vavg50, hi_prev20 = _breakout_series(c, v)
    found = []
    for i in range(253, len(c)):
        if hi252[i] is None or c[i] <= hi252[i]:
            continue
        crit = breakout_eval(c, v, i, hi252, lo60, vavg50, hi_prev20)
        if not all(x["pass"] for x in crit):
            continue
        low10 = min(c[i - 10:i])
        stop = max(low10, 0.90 * c[i])
        res = _outcome(c, i, 1, c[i] + 2 * (c[i] - stop), stop)
        prior_i = max(range(i - 252, i), key=lambda k: c[k])
        found.append({"template": "breakout_52w", "identIdx": i, "direction": "bullish",
                      "pivots": [{"i": prior_i, "price": c[prior_i], "type": "H"}, {"i": i, "price": c[i], "type": "B"}],
                      "neckline": [[prior_i, hi252[i]], [i, hi252[i]]], "height": round(c[i] - stop, 6),
                      "criteria": crit, **res})
    return found


def latest_candidate(c, pivots, key, v=None, ctx=None):
    """The most recent setup of the right shape, with every rule's pass/fail -
    shown even when it isn't a valid pattern (manual review)."""
    ctx = ctx or _ctx(c, v)
    if key == "breakout_52w":
        if not v or len(c) < 260:
            return None
        series = _breakout_series(c, v)
        i = len(c) - 1
        crit = breakout_eval(c, v, i, *series)
        hi = series[0][i]
        prior_i = max(range(i - 252, i), key=lambda k: c[k])
        return {"template": key, "pivots": [{"i": prior_i, "price": c[prior_i], "type": "H"}, {"i": i, "price": c[i], "type": "B"}],
                "neckline": [[prior_i, hi], [i, hi]], "criteria": crit, "passes": sum(x["pass"] for x in crit),
                "of": len(crit), "valid": all(x["pass"] for x in crit), "identIdx": i}
    shapes, fn, bearish = SPECS[key]
    types = "".join(p["type"] for p in pivots)
    best = None
    for shape in shapes:
        k = len(shape)
        for a in range(len(pivots) - k, -1, -1):
            if types[a:a + k] == shape:
                win = pivots[a:a + k]
                crit, geo = _rules(key, c, win, ctx)
                cand = {"template": key, "pivots": [{"i": p["i"], "price": p["price"], "type": p["type"]} for p in win],
                        "neckline": geo["neckline"], "criteria": crit, "passes": sum(x["pass"] for x in crit),
                        "of": len(crit), "valid": all(x["pass"] for x in crit), "identIdx": win[-1]["confirm"]}
                if best is None or cand["identIdx"] > best["identIdx"] or (cand["valid"] and not best["valid"]
                                                                         and cand["identIdx"] == best["identIdx"]):
                    best = cand
                break
    return best


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


RULE_NAMES = None


def rule_names():
    """Rule labels + thresholds in evaluation order, derived from the rule
    functions themselves on a fixed synthetic series (so UI labels can't drift)."""
    global RULE_NAMES
    if RULE_NAMES:
        return RULE_NAMES
    c = [100.0 + i * 0.05 for i in range(600)]
    v = [1000.0] * 600
    ctx = _ctx(c, v)
    P = lambda i, p, t: {"i": i, "price": p, "type": t, "confirm": i + 3}
    wins = {
        "hs_top": [P(300, 130, "H"), P(315, 115, "L"), P(335, 145, "H"), P(355, 116, "L"), P(370, 131, "H")],
        "double_top": [P(300, 140, "H"), P(320, 120, "L"), P(340, 141, "H")],
        "vcp": [P(300, 140, "H"), P(310, 120, "L"), P(320, 139, "H"), P(330, 128, "L")],
        "cup_handle": [P(200, 140, "H"), P(260, 110, "L"), P(320, 138, "H"), P(330, 130, "L")],
        "bull_flag": [P(300, 100, "L"), P(310, 125, "H"), P(318, 118, "L")],
        "asc_triangle": [P(300, 140, "H"), P(310, 125, "L"), P(320, 141, "H"), P(330, 132, "L")],
    }
    out = {}
    for key in SPECS:
        base = {"hs_bottom": "hs_top", "double_bottom": "double_top", "bear_flag": "bull_flag",
                "desc_triangle": "asc_triangle"}.get(key, key)
        crit, _ = _rules(key, c, wins[base], ctx)
        out[key] = [[x["rule"], x["need"]] for x in crit]
    s = _breakout_series(c, v)
    out["breakout_52w"] = [[x["rule"], x["need"]] for x in breakout_eval(c, v, 599, *s)]
    out["minervini"] = [[x["rule"], None] for x in minervini_series([100.0 + i * 0.1 for i in range(300)], [90] * 300)[-1]["criteria"]]
    RULE_NAMES = out
    return out


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
