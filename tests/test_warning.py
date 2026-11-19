# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_warning.py
#  z196: the early-warning engine (pipeline/warning_signals.py) - publication lags, no-look-ahead scoring, the
#  seasonal-trap / marginal-buyer / blindness detectors, bottom-up breadth, sector exposures, the backtest, and a
#  synthetic 2006-09 housing-credit cycle. Offline. Run: python tests/test_warning.py
# ============================================================
import json
import math
import os
import random
import shutil
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline"))
os.environ["WARNING_OFFLINE"] = "1"
import warning_signals as ws  # noqa: E402

ok = fail = 0


def check(name, cond):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond)
    fail += not cond


def mk(sid, obs):
    m = ws.SERIES[sid]
    return ws.Series(sid, [(d.isoformat(), v) for d, v in obs], m[2], m[3])


# 1. publication lags and as-of sampling --------------------------------------------------------------------------
q = mk("DRCCLACBS", [(date(2006, 10, 1), 4.0), (date(2007, 1, 1), 4.5)])          # lag 145 days
check("a quarter's loss rate is unknown before it is published (2007-05-20 -> the previous quarter)", q.asof(date(2007, 5, 20)) == 4.0)
check("... and known after (2007-05-31)", q.asof(date(2007, 5, 31)) == 4.5)
check("lag 0 (the live read) sees everything on file", q.asof(date(2007, 1, 2), 0) == 4.5)
dly = mk("VIXCLS", [(date(2020, 1, 1) + timedelta(days=i), 10.0 + (i >= 40) * 30) for i in range(60)])
v = dly.asof(date(2020, 2, 25))
check(f"a daily series reads as its 30-day mean ({v:.1f}, between 10 and 40)", 10 < v < 40)
stale = mk("UNRATE", [(date(2000, 1, 1) + timedelta(days=31 * i), 5.0) for i in range(60)])
check("a discontinued monthly series isn't carried forward for years", stale.asof(date(2012, 1, 1)) is None)

# 2. expanding percentiles: no look-ahead -------------------------------------------------------------------------
e = ws.Expanding()
check("first value has no percentile", e.pct(5) is None)
for x in (1, 2, 3, 4):
    e.add(x)
check("percentile vs what came before only (5 -> 100)", e.pct(5) == 100.0 and e.pct(2.5) == 50.0 and e.pct(2) == 37.5)

# 3. transforms ---------------------------------------------------------------------------------------------------
S = {"TOTALSL": mk("TOTALSL", [(date(2024, 1, 1), 100.0), (date(2025, 1, 1), 110.0)]),
     "DSPI": mk("DSPI", [(date(2024, 1, 1), 100.0), (date(2025, 1, 1), 104.0)])}
spec = next(x for x in ws.IND if x[0] == "cred_gap")
g = ws.transformed(spec, S, date(2025, 1, 2), lag0=True)
check(f"credit growth minus income growth = 10% - 4% = {g:.1f}", abs(g - 6.0) < 1e-9)
S2 = {"DCPF3M": mk("DCPF3M", [(date(2008, 9, 1) + timedelta(days=i), 3.0) for i in range(40)]),
      "DTB3": mk("DTB3", [(date(2008, 9, 1) + timedelta(days=i), 0.5) for i in range(40)])}
check("derived commercial-paper spread = CP rate - T-bill", abs(ws.value_at("CPSPREAD", S2, date(2008, 10, 10)) - 2.5) < 1e-9)

# 4. detectors ----------------------------------------------------------------------------------------------------
cc = mk("DRCCLACBS", [(date(2024, 1, 1), 3.0), (date(2024, 4, 1), 3.1), (date(2024, 7, 1), 3.2), (date(2024, 10, 1), 3.4),
                      (date(2025, 1, 1), 3.6), (date(2025, 4, 1), 3.7), (date(2025, 7, 1), 3.9), (date(2025, 10, 1), 4.2),
                      (date(2026, 1, 1), 3.9)] + [(date(2020, 1, 1) + timedelta(days=91 * i), 3.0) for i in range(12)])
traps = ws.seasonal_traps({"DRCCLACBS": cc}, date(2026, 6, 1), [])
check(f"seasonal trap: Q1 delinquencies improved but are worse than a year ago -> flagged ({len(traps)})",
      any(t["key"] == "cc_dq" for t in traps))
check("... and named as the tax-refund season", any("tax-refund" in (t.get("season") or "") for t in traps if t["key"] == "cc_dq"))
mb = ws.marginal_buyer({"cp": 92, "abcp": 85, "baa": 40}, [])
check("marginal buyer: commercial paper + asset-backed paper both at the top of their history -> flag", mb and mb["kind"] == "marginal-buyer")
check("one hot funding series alone is not a flag", ws.marginal_buyer({"cp": 92}, []) is None)

# 5. bottom-up ----------------------------------------------------------------------------------------------------
tmp = tempfile.mkdtemp()
today = date(2026, 11, 19)
books = {}
for i in range(40):
    t = f"C{i:02d}"
    books[t] = {"sector": "Financial Services" if i < 20 else "Technology", "industry": "Credit Services" if i < 5 else "Software",
                "price": 10.0 + i, "high52": 12.0 + i, "low52": 9.5 + i, "currency": "USD"}
    ev = []
    if i == 0:
        ev.append({"date": (today - timedelta(days=5)).isoformat(), "items": ["1.03"], "red": ["1.03"], "caution": []})
    if i == 1:   # a merger: delisting notice filed with the completed acquisition - not distress
        ev.append({"date": (today - timedelta(days=3)).isoformat(), "items": ["2.01", "3.01", "5.01"], "red": ["3.01"], "caution": ["5.01"]})
    if i in (2, 3):
        ev.append({"date": (today - timedelta(days=100)).isoformat(), "items": ["2.06"], "red": ["2.06"], "caution": []})
    json.dump({"ticker": t, "events": ev}, open(os.path.join(tmp, f"{t}.json"), "w"))
json.dump({"tickers": {f"C{i:02d}": {"insider": {"buyers": 2 if i < 10 else 0, "sellers": 1, "cluster": i < 3}} for i in range(40)}},
          open(os.path.join(tmp, "index.json"), "w"))
sb = ws.sec_breadth(tmp, books, today)
check(f"8-K red flags: one bankruptcy in 30 days (weight 3), the merger delisting excluded ({sb['events30']})", sb["events30"] == 3.0)
check(f"ratio vs the 31-180-day base rate, shrunk toward 1 ({sb['ratio']})", sb["ratio"] and sb["ratio"] > 1.5)
check("recent red filings name what they are", sb["recent"][0]["what"] == ["bankruptcy"] and sb["recent"][0]["ticker"] == "C00")
check(f"insider breadth: 10 of 40 companies net buying, 3 cluster buys ({sb['insiders']})",
      sb["insiders"]["netBuyers"] == 10 and sb["insiders"]["clusterBuys"] == 3)
bu = ws.bottom_up(today, books=books, sec_dir=tmp,
                  options={"asOf": "x", "breadth": {"invertedShare": 0.5, "skewHighShare": 0.3, "unusualPutLedShare": 0.6},
                           "appSignal": {"indexIV": 0.25}, "sectors": {"Financial Services": {"n": 30, "invertedShare": 0.7, "skewHighShare": 0.5}}})
sc, gg = ws.score_bottom_up(bu, [])
check(f"bottom-up scores use provisional anchors before 40 days of history ({sc['optInverted']})", "provisional" in sc["optInverted"][1])
daily = [{"bu": {"optInverted": 0.1 + 0.005 * i}} for i in range(60)]
sc2, _ = ws.score_bottom_up(bu, daily)
check(f"... and percentiles of our own history after ({sc2['optInverted']})", "percentile" in sc2["optInverted"][1] and sc2["optInverted"][0] > 90)
ex = ws.exposures({"cc_dq": 95, "cc_co": 92, "cons_dq": 88, "sloos_cc": 90}, bu)
ec = (ex["industry"].get("Credit Services") or {}).get("e") or 0
check(f"consumer-credit stress exposes Credit Services (e={ec})", ec > 0.5)
check("options + filings breadth hot in a sector -> sector exposure (Financial Services)", "Financial Services" in ex["sector"])
shutil.rmtree(tmp)

# 6. a synthetic housing-credit cycle: losses rise first, markets later ---------------------------------------------
rnd = random.Random(7)
_phase = {}


def wig(d, key, amp):
    """Smooth, persistent variation (two slow cycles, a phase per series) + a little noise - real series drift; they
    don't jump to a random percentile every month."""
    a, b = _phase.setdefault(key, (rnd.random() * 6.28, rnd.random() * 6.28))
    x = d.year + (d.month - 1) / 12 + d.day / 365
    return amp * (0.7 * math.sin(6.283 * x / 6.5 + a) + 0.3 * math.sin(6.283 * x / 2.3 + b)) + amp * 0.05 * rnd.gauss(0, 1)


def series_q(f, start=1985, end=2026, key=None, amp=0.15):
    key = key or str(id(f))
    return [(date(y, m, 1), f(date(y, m, 1)) + wig(date(y, m, 1), key, amp)) for y in range(start, end + 1) for m in (1, 4, 7, 10)
            if date(y, m, 1) <= date(end, 10, 1)]


def series_m(f, start=1985, key=None, amp=0.02, mult=True):
    key = key or str(id(f))
    out, d = [], date(start, 1, 1)
    while d <= date(2026, 10, 1):
        out.append((d, f(d) * (1 + wig(d, key, amp)) if mult else f(d) + wig(d, key, amp)))
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def series_w(f, start=1985, key=None, amp=0.06, mult=True):
    key = key or str(id(f))
    days = [date(start, 1, 6) + timedelta(days=7 * i) for i in range(int((date(2026, 11, 1) - date(start, 1, 6)).days / 7))]
    return [(d, f(d) * (1 + wig(d, key, amp)) if mult else f(d) + wig(d, key, amp)) for d in days]


def yrs(d):
    return d.year + (d.month - 1) / 12


def ramp(d, a, b, v0, v1):
    x = yrs(d)
    return v0 if x <= a else v1 if x >= b else v0 + (v1 - v0) * (x - a) / (b - a)


def bump(d, a, b, c, h):
    """Rise from a to b, fall back by c (years) - one credit cycle of height h."""
    return ramp(d, a, b, 0, h) - ramp(d, b + 0.2, c, 0, h)


# three earlier, milder cycles (1990, 2001; a 1998 / 2011 / 2020 market scare) and the 2006-09 housing-credit cycle
loss = lambda d: 3.0 + bump(d, 1989.5, 1991.5, 1994, 1.2) + bump(d, 2000.5, 2002, 2004, 0.8) + bump(d, 2005.9, 2009.3, 2012, 3.0)
mtg = lambda d: 2.0 + bump(d, 1989.5, 1991.5, 1994, 1.0) + bump(d, 2000.5, 2002, 2004, 0.5) + bump(d, 2005.6, 2010, 2014, 8.0)
sloos = lambda d: 0.0 + bump(d, 1990.0, 1990.9, 1992.5, 45.0) + bump(d, 2000.6, 2001.3, 2002.8, 50.0) + bump(d, 2007.4, 2008.8, 2010.5, 70.0) + bump(d, 2020.1, 2020.4, 2021.3, 70.0)
claims = lambda d: 330.0 + bump(d, 1990.3, 1991.2, 1993, 140.0) + bump(d, 2000.8, 2001.8, 2003.5, 120.0) + bump(d, 2007.3, 2009.2, 2012, 320.0) + bump(d, 2020.2, 2020.3, 2021.5, 500.0)
unrate = lambda d: 5.0 + bump(d, 1990.4, 1992.4, 1996, 2.0) + bump(d, 2001.0, 2003.4, 2006, 1.8) + bump(d, 2007.4, 2009.8, 2016, 5.0) + bump(d, 2020.2, 2020.3, 2022, 9.0)
hpi = lambda d: 100.0 * math.exp(0.05 * (yrs(d) - 1985)) * (1 - ramp(d, 2006.5, 2009.5, 0, 0.30) + ramp(d, 2012, 2018, 0, 0.30))
baa = lambda d: 1.8 + bump(d, 1990.5, 1990.9, 1992, 0.8) + bump(d, 1998.6, 1998.8, 1999.5, 0.6) + bump(d, 2001.5, 2002.8, 2004, 1.2) + bump(d, 2007.5, 2008.9, 2010.5, 4.0) + bump(d, 2020.2, 2020.25, 2021, 2.0)
vix = lambda d: 14.0 + bump(d, 1990.5, 1990.8, 1991.3, 12.0) + bump(d, 1998.6, 1998.75, 1999.2, 20.0) + bump(d, 2001.6, 2002.6, 2003.5, 20.0) + bump(d, 2007.5, 2008.8, 2009.8, 45.0) + bump(d, 2011.6, 2011.7, 2012.2, 20.0) + bump(d, 2020.15, 2020.25, 2021, 50.0)
nfci = lambda d: -0.5 + bump(d, 1990.5, 1990.9, 1992, 0.8) + bump(d, 1998.6, 1998.8, 1999.5, 0.6) + bump(d, 2001.6, 2002.6, 2003.6, 0.8) + bump(d, 2007.5, 2008.8, 2010, 3.0) + bump(d, 2020.15, 2020.25, 2021, 1.5)
growth = lambda d: 100.0 * math.exp(0.025 * (yrs(d) - 1985)) * (1 - bump(d, 1990.5, 1991.2, 1992.5, 0.02) - bump(d, 2001.0, 2001.8, 2003, 0.02) - bump(d, 2008.0, 2009.5, 2011, 0.06) - bump(d, 2020.15, 2020.3, 2021, 0.08))
eq = lambda d: 1000.0 * math.exp(0.08 * (yrs(d) - 1985)) * (1 - bump(d, 1990.5, 1990.8, 1991.3, 0.2) - bump(d, 2000.2, 2002.7, 2006, 0.5) - bump(d, 2007.8, 2009.2, 2013, 0.5) - bump(d, 2020.1, 2020.22, 2020.7, 0.33))
S = {
    "DRCCLACBS": mk("DRCCLACBS", series_q(loss)), "CORCCACBS": mk("CORCCACBS", series_q(lambda d: loss(d) + 1)),
    "DRSFRMACBS": mk("DRSFRMACBS", series_q(mtg)), "DRTSCILM": mk("DRTSCILM", series_q(sloos, amp=8.0)),
    "ICSA": mk("ICSA", series_w(claims)), "UNRATE": mk("UNRATE", series_m(unrate)), "CSUSHPINSA": mk("CSUSHPINSA", series_m(hpi)),
    "BAA10Y": mk("BAA10Y", series_w(baa)), "VIXCLS": mk("VIXCLS", series_w(vix, amp=0.15)),
    "NFCI": mk("NFCI", series_w(nfci, amp=0.15, mult=False)),
    "W875RX1": mk("W875RX1", series_m(growth)), "INDPRO": mk("INDPRO", series_m(growth)), "PAYEMS": mk("PAYEMS", series_m(growth)),
    "NASDAQCOM": mk("NASDAQCOM", [(date(1985, 1, 1) + timedelta(days=i), eq(date(1985, 1, 1) + timedelta(days=i))) for i in range(0, 15300, 1)]),
    "USREC": mk("USREC", [(d, 1.0 if (date(2007, 12, 1) <= d <= date(2009, 6, 1)) or (date(2001, 3, 1) <= d <= date(2001, 11, 1))
                           or (date(2020, 2, 1) <= d <= date(2020, 4, 1)) or (date(1990, 7, 1) <= d <= date(1991, 3, 1)) else 0.0)
                           for d, _ in series_m(lambda d: 1.0)]),
}
out, rows, bu = ws.run(date(2026, 11, 19), S, {}, [], None)
by = {r["t"].isoformat()[:7]: r for r in rows}
blind = [k for k, r in by.items() if r.get("blind")]
check(f"market blindness flagged while losses rose and markets were calm (2006-07): {[m for m in blind if m < '2008'][:8]}",
      any("2006-01" <= m <= "2007-09" for m in blind))
check(f"no blindness flags in the quiet years between cycles (1995-1999): {[m for m in blind if '1995-01' <= m <= '1999-12']}",
      not any("1995-01" <= m <= "1999-12" for m in blind))
check("no organic-expansion call while loss data sit in their top 15%", not any(by[m]["regime"] == "organic expansion" and (by[m]["pillars"].get("loss") or 0) >= 85 for m in by))
check("organic expansion called in the boom before it (2004-06)", any(by[m]["regime"] == "organic expansion" for m in by if "2004-01" <= m <= "2006-06"))
check("stress spreading / acute once markets react (2007-08 .. 2008-12)", all(by[m]["regime"] in ("stress spreading", "acute stress", "contraction") for m in by if "2007-09" <= m <= "2008-12"))
check("stress building before the markets cracked", any(by[m]["regime"] == "stress building" for m in by if "2006-06" <= m <= "2007-12"))
check("acute stress at the market peak (2008-09 .. 2009-03)", any(by[m]["regime"] == "acute stress" for m in by if "2008-09" <= m <= "2009-03"))
rec = next((r for r in out["backtest"]["recessions"] if r["start"] == "2007-12"), None)
check(f"backtest: fundamentals led the 2007-12 recession ({rec})", rec and (rec["fundamentalLeadMonths"] or 0) >= 6)
check(f"backtest buckets and hit rate computed ({out['backtest'].get('hit')})", out["backtest"]["buckets"] and out["backtest"].get("hit"))
check("earned flags are booleans", isinstance(out["backtest"]["earned"].get("throttle"), bool) and isinstance(out["backtest"]["earned"].get("organicUpside"), bool))
check("history: monthly rows [month, risk, F, M, Fr, G, regime, blind]", out["history"] and len(out["history"][-1]) == 8)
check("plain-English notes per pillar", any(n.startswith("Loss data") for n in out["notes"]))
s = json.dumps(out)
check(f"output is JSON and compact ({len(s) // 1024} KB)", len(s) < 400_000)

# 7. degraded: no FRED history on file -> a provisional read from the bottom-up layer -------------------------------
out2, _, _ = ws.run(date(2026, 11, 19), {}, {"options": {"breadth": {"invertedShare": 0.5}}}, [], None)
check(f"no series: provisional risk from bottom-up only ({out2['risk']}, {out2['level']})", out2["provisional"] and out2["risk"] is not None)
out3, _, _ = ws.run(date(2026, 11, 19), {}, {}, [], None)
check("nothing at all: no read, no crash", out3["risk"] is None and out3["level"] == "unknown")

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
