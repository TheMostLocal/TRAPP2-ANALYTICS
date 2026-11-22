# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_derived.py
#  z203 - market statistics by the formula (pipeline/derived.py): the 52-week closing range, averages, volume,
#  volatility, beta (60 completed months / 252 sessions), returns, drawdown, the flags that stop a fill, Yahoo's range
#  and beta checked against ours, and the file (written once, from checked-out books). Offline, synthetic, < 2 s.
#  Run: python tests/test_derived.py
# ============================================================
import json
import math
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
os.environ.setdefault("DERIVED_NOW", "2026-11-21T15:00:00Z")
os.environ["DERIVED_OFFLINE"] = "1"
import derived as dv  # noqa: E402

TODAY = date(2026, 11, 21)


# ------------------------------------------------------------------------------------------------- synthetic data ---
def weekdays(a, b):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def noise(seed):
    """Deterministic ~N(0,1) (sum of 12 LCG uniforms - 6)."""
    x = seed

    def nxt():
        nonlocal x
        s = 0.0
        for _ in range(12):
            x = (1103515245 * x + 12345) % 2 ** 31
            s += x / 2 ** 31
        return s - 6.0
    return nxt


def market(ds, seed=7, sd=0.01):
    z, c, out = noise(seed), 100.0, []
    for _ in ds:
        c *= math.exp(sd * z())
        out.append(c)
    return out


def follower(mk, beta, seed=11, sd=0.008, start=50.0):
    """A stock whose daily simple return = beta x the market's + noise."""
    z, c, out = noise(seed), start, [start]
    for i in range(1, len(mk)):
        c *= 1 + beta * (mk[i] / mk[i - 1] - 1) + sd * z()
        out.append(c)
    return out


def rows(ds, cs, vol=1_000_000):
    return [{"date": d, "close": round(c, 6), "volume": vol} for d, c in zip(ds, cs)]


DS = weekdays(date(2019, 1, 2), date(2026, 11, 20))
MK = market(DS)
BMAP = dict(zip(DS, MK))


# ---------------------------------------------------------------------------------------------------------- dates ---
def test_dates():
    assert dv.years_back(date(2024, 2, 29)) == date(2023, 2, 28)
    assert dv.months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert dv.months_back(date(2026, 1, 15), 13) == date(2024, 12, 15)
    assert dv.through_month(TODAY) == "2026-10" and dv.through_month(date(2026, 12, 1)) == "2026-11"
    assert dv.at_or_before(["2026-01-02", "2026-01-05"], "2026-01-04") == 0 and dv.at_or_before(["2026-01-05"], "2026-01-01") == -1


# ----------------------------------------------------------------------------------------------------------- beta ---
def test_beta_recovers_the_slope():
    st = follower(MK, 1.5)
    s = dv.stats(DS, st, [1e6] * len(DS), BMAP, TODAY, DS[-1], {"us": True})
    b1, b5 = s["b1"], s["b5"]
    assert b1[1] == 252 and abs(b1[0] - 1.5) < 0.12 and 0.3 < b1[2] <= 1, b1
    assert b5[1] == 60 and abs(b5[0] - 1.5) < 0.35, b5        # 60 points: wider sampling error, same slope
    assert s["ub"] == [b5[0], "m"]
    idx = dv.beta_monthly(*dv.common(DS, st, BMAP), "2026-10")
    assert idx[3][:7] == "2021-10", idx                     # 61 month-ends: Oct 2021 .. Oct 2026 -> 60 returns
    lo = dv.beta_monthly(*dv.common(DS, MK, BMAP), "2026-10")
    assert abs(lo[0] - 1) < 1e-9 and abs(lo[2] - 1) < 1e-9   # the benchmark against itself


def test_unfinished_month_left_out():
    st = follower(MK, 1.0)
    st2 = st[:]
    for i, d in enumerate(DS):                              # November 2026 alone goes wild
        if d >= "2026-11-01":
            st2[i] = st[i] * (3.0 if i % 2 else 0.4)
    a = dv.beta_monthly(*dv.common(DS, st, BMAP), dv.through_month(TODAY))
    b = dv.beta_monthly(*dv.common(DS, st2, BMAP), dv.through_month(TODAY))
    assert a[:3] == b[:3], "the current month is not part of the 60"
    c = dv.beta_monthly(*dv.common(DS, st2, BMAP), "2026-11")
    assert c[0] != a[0]


# ------------------------------------------------------------------------------------------ range, returns, etc. ---
def test_range_returns_drawdown():
    ds = weekdays(date(2024, 1, 2), date(2026, 11, 20))
    cs = [100.0 + i * 0.1 for i in range(len(ds))]          # a straight line up ...
    k = ds.index("2026-03-02")
    cs[k] = 90.0                                            # ... with one low day inside the 52 weeks
    s = dv.stats(ds, cs, [1e6] * len(ds), BMAP, TODAY, ds[-1], {})
    assert s["h52"] == [dv.rnd(cs[-1]), ds[-1]] and s["l52"] == [90.0, "2026-03-02"], (s["h52"], s["l52"])
    assert s["fh"] == 0.0 and s["fl"] == round(100 * (cs[-1] / 90 - 1), 2)
    j = dv.at_or_before(ds, "2025-11-20")
    assert s["r"]["1y"] == round(100 * (cs[-1] / cs[j] - 1), 2)
    y = dv.at_or_before(ds, "2025-12-31")
    assert ds[y] == "2025-12-31" and s["r"]["ytd"] == round(100 * (cs[-1] / cs[y] - 1), 2)
    assert "5y" not in s["r"] and "3y" not in s["r"]       # history too short
    assert s["dd"][1] == ds[k - 1] and s["dd"][2] == "2026-03-02" and s["dd"][0] == round(100 * (90 / cs[k - 1] - 1), 2)
    assert s["m50"] == dv.rnd(sum(cs[-50:]) / 50) and s["u52"] == 1 and "q" not in s


def test_short_history_and_since():
    ds = weekdays(date(2026, 6, 1), date(2026, 11, 20))
    cs = [10 + i * 0.01 for i in range(len(ds))]
    s = dv.stats(ds, cs, [5e5] * len(ds), BMAP, TODAY, ds[-1], {"us": True})
    assert s["since"] == "2026-06-01" and "short" in s["q"] and s["l52"] == [10.0, "2026-06-01"]
    assert "b5" not in s and "s252" not in s and "ub" not in s     # < 24 months, < 200 sessions


def test_volatility_annualized():
    ds = weekdays(date(2025, 1, 1), date(2026, 11, 20))
    cs, c = [], 100.0
    for i in range(len(ds)):
        c *= math.exp(0.01 if i % 2 else -0.01)
        cs.append(c)
    s = dv.stats(ds, cs, [1e6] * len(ds), BMAP, TODAY, ds[-1], {})
    per = len(ds) - dv.bisect_right(ds, dv.years_back(date(2026, 11, 20)).isoformat())
    sd20 = math.sqrt(sum((0.01 if (len(ds) - 20 + i) % 2 else -0.01) ** 2 for i in range(20)) / 19)
    assert s["s20"] == round(100 * sd20 * math.sqrt(per), 1) and s["ppy"] == per == 261     # weekdays, no holidays: 261 a year
    # a 7-day market (crypto) annualizes by the days a year holds
    ds7 = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(700)]
    s7 = dv.stats(ds7, [100 * math.exp(0.01 * (i % 2)) for i in range(700)], [1e6] * 700, BMAP, TODAY, DS[-1], {})
    assert s7["ppy"] == 365


def test_volume_stats():
    n = len(DS)
    v = [1000] * (n - 1) + [3000]
    s = dv.stats(DS, MK, v, BMAP, TODAY, DS[-1], {})
    assert s["a20"] == round((19 * 1000 + 3000) / 20) and s["a63"] == round((62 * 1000 + 3000) / 63) and s["rv"] == 3.0
    assert s["dv20"] == int(dv.rnd(sum(c * x for c, x in zip(MK[-20:], v[-20:])) / 20, 4))
    s0 = dv.stats(DS, MK, [0] * n, BMAP, TODAY, DS[-1], {})          # an index level, a rate: no volume
    assert "a20" not in s0 and "a63" not in s0 and "rv" not in s0 and "dv20" not in s0
    assert dv.avg_volume([None] * 10 + [100] * 10, 20) is None      # half the window missing
    assert dv.avg_volume([0] * 5 + [100] * 15, 20) == 75            # a 0 is a session without trades


# ------------------------------------------------------------------------------------------------ flags & fills ---
def test_flags_stop_fills():
    st = follower(MK, 1.2)
    # stale: no session for 10+ days while the S&P traded
    s = dv.stats(DS[:-12], st[:-12], [1e6] * (len(DS) - 12), BMAP, TODAY, DS[-1], {"us": True})
    assert "stale" in s["q"] and "u52" not in s and "ub" not in s
    # a x3 jump inside the 52 weeks: no range fill, and no beta whose span holds it
    j = DS.index("2026-06-01")
    sj = st[:j] + [c * 3 for c in st[j:]]
    s = dv.stats(DS, sj, [1e6] * len(DS), BMAP, TODAY, DS[-1], {"us": True})
    jq = [q for q in s["q"] if q.startswith("jump:2026-06-01:x")]
    assert jq and abs(float(jq[0].split(":x")[1]) / 3 - 1) < 0.1 and "u52" not in s and "ub" not in s
    # a jump 3 years ago: the range is clean, the 5-year beta is not - the 1-year beta fills
    j = DS.index("2023-06-01")
    sj = st[:j] + [c * 3 for c in st[j:]]
    s = dv.stats(DS, sj, [1e6] * len(DS), BMAP, TODAY, DS[-1], {"us": True})
    assert s["u52"] == 1 and s["ub"] == [s["b1"][0], "d"]
    # our last close off the book's close for the same session: nothing filled
    s = dv.stats(DS, st, [1e6] * len(DS), BMAP, TODAY, DS[-1], {"us": True, "date": DS[-1], "price": st[-1] * 1.1})
    assert any(q.startswith("px:x0.909") for q in s["q"]) and "u52" not in s and "ub" not in s
    s = dv.stats(DS, st, [1e6] * len(DS), BMAP, TODAY, DS[-1], {"us": True, "date": DS[-1], "price": st[-1] * 1.005})
    assert "q" not in s and s["u52"] == 1
    # not a US-listed USD equity: no beta fill (the numbers are still there)
    s = dv.stats(DS, st, [1e6] * len(DS), BMAP, TODAY, DS[-1], {"us": False})
    assert "ub" not in s and s["b5"]
    m = {"cls": "equity", "ccy": "USD"}
    assert dv.us_equity("LOW", m) and not dv.us_equity("7203.T", m) and not dv.us_equity("BTC-USD", m)
    assert not dv.us_equity("^GSPC", m) and not dv.us_equity("SHEL", {"cls": "equity", "ccy": "GBP"}) and dv.us_equity("BRK-B", m)


def test_yahoo_range_checks():
    ds = weekdays(date(2025, 1, 2), date(2026, 11, 20))
    cs = [50 + 10 * math.sin(i / 40) for i in range(len(ds))]
    base = dv.stats(ds, cs, [1e6] * len(ds), BMAP, TODAY, ds[-1], {})
    oh, ol = base["h52"][0], base["l52"][0]
    run = lambda yh, yl: dv.stats(ds, cs, [1e6] * len(ds), BMAP, TODAY, ds[-1], {"h52": yh, "l52": yl})
    s = run(oh * 1.02, ol * 0.98)
    assert "x52" not in s and "w52" not in s                       # closes inside the intraday range
    assert run(oh * 0.9, ol * 0.98)["x52"] == 1                    # a close above Yahoo's intraday high
    assert run(oh * 2.03, ol * 0.98)["x52"] == 2                   # a 2-for-1 Yahoo never re-based
    assert run(oh * 1.4, ol * 0.98)["w52"] == 1                    # a spike, not a split factor
    assert dv.split_factor(4.1) == 4 and dv.split_factor(1.52) == 1.5 and dv.split_factor(1.34) is None


def test_checks_counts():
    t = {"A": {"b5": [1.10, 60, 0.4], "a63": 1000, "d": "2026-11-20", "px": 10.0},
         "B": {"b5": [1.50, 60, 0.4], "a63": 2000, "d": "2026-11-20", "px": 10.0},
         "C": {"b5": [0.90, 60, 0.4], "d": "2026-11-20", "px": 10.0},
         "D": {"b5": [2.00, 60, 0.4], "d": "2026-11-20", "px": 10.0, "q": ["jump:2024-01-02:x2.10"]}}
    m = {"A": {"beta": 1.08, "adv": 1100, "country": "United States", "date": "2026-11-20", "price": 10.0},
         "B": {"beta": 1.20, "adv": 1000, "country": ""},
         "C": {"beta": 0.40, "country": "Netherlands"},
         "D": {"beta": 0.50, "country": "United States"}}
    c = dv.checks(t, m)
    assert (c["beta"]["agree"], c["beta"]["check"], c["beta"]["foreign"]) == (1, 1, 1), c["beta"]
    assert c["beta"]["list"][0]["t"] == "B" and c["adv"]["agree"] == 1 and c["adv"]["list"][0]["x"] == 2.0
    assert c["px"]["agree"] == 1


# ------------------------------------------------------------------------------------------------------- the file ---
def test_main_from_books_on_disk():
    st = follower(MK, 0.8)
    with tempfile.TemporaryDirectory() as tmp:
        books = Path(tmp) / "books"
        for b in dv.BOOKS:
            (books / b / "data" / "history").mkdir(parents=True)
            (books / b / "data" / "master.json").write_text("[]")
        (books / "TRAPP2-1" / "data" / "history" / "^GSPC.json").write_text(json.dumps(rows(DS, MK)))
        (books / "TRAPP2" / "data" / "master.json").write_text(json.dumps([
            {"ticker": "aaa", "asset_class": "Equity", "currency": "USD", "beta": "", "price": st[-1], "date": DS[-1]},
            {"ticker": "NOHIST", "asset_class": "Equity"}]))
        (books / "TRAPP2-4" / "data" / "master.json").write_text(json.dumps([{"ticker": "AAA", "beta": 9}]))   # first book wins
        (books / "TRAPP2-2" / "data" / "history" / "AAA.json").write_text(json.dumps(rows(DS, st)))           # found in another book
        real = (dv.OUT, os.environ.get("DERIVED_BOOKS"))
        os.environ["DERIVED_BOOKS"] = str(books)
        dv.OUT = Path(tmp) / "data" / "derived" / "market.json"
        try:
            assert dv.main() == 0 and dv.OUT.exists()
            doc = json.loads(dv.OUT.read_text())
            a = doc["tickers"]["AAA"]
            assert doc["build"] == dv.BUILD and doc["bench"] == "^GSPC" and doc["date"] == DS[-1] and doc["through"] == "2026-10"
            assert a["ub"][1] == "m" and abs(a["ub"][0] - 0.8) < 0.3 and a["u52"] == 1
            assert doc["counts"]["computed"] == 1 and doc["noHistory"] == ["NOHIST"] and doc["counts"]["fills"]["beta"] == 1
            m0 = dv.OUT.stat().st_mtime_ns
            os.environ["DERIVED_NOW"] = "2026-11-21T18:00:00Z"            # a later run, same data: not rewritten
            assert dv.main() == 0 and dv.OUT.stat().st_mtime_ns == m0
            assert dv.same(doc, dict(doc, at="x")) and not dv.same(doc, dict(doc, date="x"))
        finally:
            os.environ["DERIVED_NOW"] = "2026-11-21T15:00:00Z"
            dv.OUT = real[0]
            if real[1] is None:
                os.environ.pop("DERIVED_BOOKS", None)
            else:
                os.environ["DERIVED_BOOKS"] = real[1]


def test_no_benchmark_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        books = Path(tmp) / "books"
        (books / "TRAPP2" / "data").mkdir(parents=True)
        (books / "TRAPP2" / "data" / "master.json").write_text(json.dumps([{"ticker": "AAA"}]))
        for b in dv.BOOKS[1:]:
            (books / b / "data").mkdir(parents=True)
        real = dv.OUT
        os.environ["DERIVED_BOOKS"] = str(books)
        dv.OUT = Path(tmp) / "m.json"
        try:
            assert dv.main() == 0 and not dv.OUT.exists()
        finally:
            dv.OUT = real
            os.environ.pop("DERIVED_BOOKS", None)


if __name__ == "__main__":
    ok = fail = 0
    for name, fn in sorted((k, v) for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)):
        try:
            fn()
            ok += 1
            print(f"PASS {name}")
        except Exception as ex:                  # noqa: BLE001
            fail += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {ex}")
    print(f"\n{ok} passed, {fail} failed")
    sys.exit(1 if fail else 0)
