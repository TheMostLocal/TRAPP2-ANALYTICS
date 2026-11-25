# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_factors.py
#  z206 - the factor monitor (pipeline/factors.py): the universe rules (US common stocks, no funds, short % of float in
#  (0, 100 %]), a close missing from the history taken from the book's day tape, the baskets formed on the PRIOR close
#  only (no look-ahead), the spreads and their z-scores, the day's label (short-covering rally, de-grossing, risk-off
#  rotation, mega-cap selling ...) and the file written once (an unchanged re-run writes nothing). Offline, < 5 s.
#  Run: python tests/test_factors.py   (or python -m pytest tests/test_factors.py -q)
# ============================================================
import json
import math
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
os.environ["DERIVED_OFFLINE"] = "1"
os.environ["FACTORS_NOW"] = "2026-11-24T03:00:00Z"
import factors as fx  # noqa: E402

N_DAYS = fx.WINDOW + fx.MOM_LONG + 40


def sessions(n, end=date(2026, 11, 23)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


DATES = sessions(N_DAYS)


def bench_closes(last_jump=0.0):
    """A benchmark with alternating small moves (sd ~0.8 %) and an optional move on the last session."""
    c, x = [], 4000.0
    for i in range(N_DAYS):
        x *= 1 + (0.008 if i % 2 == 0 else -0.0075)
        c.append(x)
    if last_jump:
        c[-1] = c[-2] * (1 + last_jump)
    return c


def make_world(n=60, last=None, beta_of=None):
    """n names: name k has a drift that ranks it for momentum, a beta (beta_of(k)) to the benchmark's daily moves and
    idiosyncratic noise; `last(k, ret)` may override the last session's return."""
    b = bench_closes()
    rb = [None] + [b[i] / b[i - 1] - 1 for i in range(1, N_DAYS)]
    data, rows = {}, {}
    for k in range(n):
        t = f"S{k:02d}"
        beta = beta_of(k) if beta_of else 0.5 + k / n
        drift = (k - n / 2) * 0.00004
        c, x = [], 50.0
        for i in range(N_DAYS):
            if i:
                noise = 0.004 * math.sin(i * (k + 3) * 0.7)
                r = drift + beta * rb[i] + noise
                if i == N_DAYS - 1 and last:
                    r = last(k, r)
                x *= 1 + r
            c.append(round(x, 4))
        data[t] = [{"date": d, "close": cc, "volume": 400000} for d, cc in zip(DATES, c)]
        rows[t] = {"book": "TRAPP2", "si": 0.05 + 0.4 * (k % 10 == 0) * (k / n), "siRaw": None, "cap": 5e9}
    return data, rows, b


def panel_data(data, dates):
    pos = {d: i for i, d in enumerate(dates)}
    out = {}
    for t, rs in data.items():
        c, v = [None] * len(dates), [None] * len(dates)
        for r in rs:
            k = pos.get(r["date"])
            if k is not None:
                c[k], v[k] = r["close"], r["volume"]
        out[t] = (c, v)
    return out


# ---- inputs ----------------------------------------------------------------------------------------------------
def test_universe_rows_rules():
    masters = {"TRAPP2": [{"ticker": "AAA", "asset_class": "Equity", "currency": "USD", "shortPctFloat": "0.12", "marketcap": "5e9"},
                          {"ticker": "FND", "asset_class": "Equity", "currency": "USD", "isetf": "true"},              # a fund
                          {"ticker": "CEF", "asset_class": "Equity", "currency": "USD", "expenseRatio": "0.0085"},     # only funds carry one
                          {"ticker": "GEF-B", "asset_class": "Equity", "currency": "USD", "shortPctFloat": "1.489", "marketcap": "2e9"},
                          {"ticker": "SAP.DE", "asset_class": "Equity", "currency": "EUR", "marketcap": "2e11"},
                          {"ticker": "ES=F", "asset_class": "Future", "currency": "USD"}],
               "TRAPP2-4": [{"ticker": "AAA", "asset_class": "Equity", "currency": "USD", "shortPctFloat": "0.5"},       # first book wins
                            {"ticker": "BBB", "asset_class": "Equity", "currency": "USD", "marketCap": "3e9"}]}
    rows = fx.universe_rows(lambda b, rel: masters.get(b))
    assert sorted(rows) == ["AAA", "BBB", "GEF-B"]
    assert rows["AAA"] == {"book": "TRAPP2", "si": 0.12, "siRaw": 0.12, "cap": 5e9}
    assert rows["GEF-B"]["si"] is None and rows["GEF-B"]["siRaw"] > 1         # over 100 % of float: a data check, not a short
    assert rows["BBB"]["cap"] == 3e9 and rows["BBB"]["si"] is None


def test_bench_falls_back_to_spy_in_trapp2_when_the_macro_book_is_out():
    spy = [{"date": d, "close": c, "volume": 1e8} for d, c in zip(DATES, bench_closes())]
    hist = lambda b, s: spy if (b, s) == ("TRAPP2", "SPY") else None
    sym, ds, cs = fx.load_bench(hist)
    assert sym == "SPY" and len(ds) == N_DAYS


def test_a_missing_close_comes_from_the_day_tape():
    data, rows, _ = make_world(30)
    pdata = panel_data(data, DATES)
    pdata["S03"][0][-1] = None                                                 # TRAPP2-4 on 11-23: a volume, no close
    pdata["S04"][0][-1] = None
    tape = {"close": {"S03": 61.5}}                                            # S04 isn't on the tape: stays missing
    got = fx.patch_from_tapes(pdata, rows, DATES, read=lambda b, rel: tape if rel == f"data/intraday/{DATES[-1]}.json" else None)
    assert got == {DATES[-1]: 1} and pdata["S03"][0][-1] == 61.5 and pdata["S04"][0][-1] is None


# ---- the panel, the baskets, the spreads ---------------------------------------------------------------------------
def test_baskets_use_the_prior_close_only():
    data, rows, b = make_world(40)
    P = fx.Panel(DATES, b, panel_data(data, DATES), rows)
    j = P.n - 2
    f1 = P.form(j, j)
    for t in P.names:                                                         # anything after close j must not matter
        P.c[t][j + 1] = (P.c[t][j + 1] or 1) * (5 if t < "S20" else 0.2)
    P._beta.clear()
    assert P.form(j, j) == f1
    assert len(f1["mom"][1]) == len(f1["mom"][0]) == 8 and set(f1["mom"][1]).isdisjoint(f1["mom"][0])
    assert sum(t >= "S30" for t in f1["mom"][1]) >= 6 and sum(t < "S10" for t in f1["mom"][0]) >= 6   # drift ranks momentum (noise aside)
    assert all(t >= "S30" for t in f1["beta"][1]) and all(t < "S10" for t in f1["beta"][0])   # beta 0.5 .. 1.5 by name


def test_eligibility_price_dollar_volume_and_cap():
    data, rows, b = make_world(30)
    for r in data["S01"]:
        r["close"] = 3.0                                                       # under $5
    for r in data["S02"]:
        r["volume"] = 1000                                                     # $50K a day
    rows["S03"]["cap"] = 5e8                                                   # under $1B
    P = fx.Panel(DATES, b, panel_data(data, DATES), rows)
    el = P.eligible(P.n - 1)
    assert "S00" in el and not {"S01", "S02", "S03"} & set(el)


def test_spreads_arithmetic():
    ret = {"A": 0.10, "B": 0.00, "C": -0.10, "D": 0.02, "E": None}
    form = {"universe": ["A", "B", "C", "D", "E"], "mom": (["C"], ["A"]), "rev": (["C"], ["A"]), "beta": (["B"], ["D"])}
    s = fx.spreads(ret, form, ["A", "C", "D"])
    assert s["n"] == 4 and abs(s["ew"] - 0.005) < 1e-12
    assert abs(s["mom"] - 0.20) < 1e-12 and abs(s["rev"] + 0.20) < 1e-12 and abs(s["beta"] - 0.02) < 1e-12
    assert abs(s["si"] - (0.02 / 3 - 0.005)) < 1e-12 and s["momL"] == 0.10 and s["momS"] == -0.10


# ---- the label -----------------------------------------------------------------------------------------------------
def test_character_rules():
    c = fx.character({"si": 0.5, "mom": 0.4, "rev": 3.4, "beta": 3.6}, {"ew": 3.0, "rev": 4.47, "beta": 7.26},
                     {"chg": 4.7, "sigma": 2.85, "gap": 4.59, "gapShare": 0.97, "vixChg": -25.8, "credit": 0.25,
                      "creditImplied": 0.9, "cwEw": 1.8, "pctUp": 83})
    assert c["label"] == "short-covering rally" and c["flags"] == {"bounce": True, "riskOn": True, "hedges": True, "creditLag": True}
    ks = [x["k"] for x in c["signals"]]
    assert ks[:2] == ["rev", "beta"] and "hedges" in ks and "credit" in ks and "narrow" in ks
    assert "97% of the S&P 500's move came at the open" in json.dumps(c) and "credit didn't follow" in json.dumps(c)
    assert fx.character({"si": 2.4}, {"siL": 6, "ew": 3}, {"sigma": 1.5})["label"] == "short-covering rally"
    assert fx.character({"mom": -2.5, "si": 1.2}, {}, {"sigma": -1.4})["label"] == "de-grossing"
    assert fx.character({"mom": -2.5, "si": 0.2}, {}, {"sigma": -1.4})["label"] == "momentum unwind"
    assert fx.character({"rev": -2.6, "beta": -2.9}, {}, {"sigma": -2.0})["label"] == "losers liquidated"
    assert fx.character({"beta": -2.5}, {}, {"sigma": -0.63, "chg": -1.2, "cwEw": -1.16, "pctUp": 63})["label"] == "risk-off rotation"
    assert fx.character({}, {}, {"sigma": -0.6, "chg": -1.2, "cwEw": -1.2})["label"] == "mega-cap selling"
    assert fx.character({}, {}, {"sigma": 1.3, "pctUp": 75})["label"] == "broad rally"
    assert fx.character({}, {}, {"sigma": 1.3, "pctUp": 75, "credit": 0.0, "creditImplied": 0.6})["label"] == "rally"   # credit lagged
    assert fx.character({}, {}, {"sigma": 0.2})["label"] is None


# ---- the build and the file ----------------------------------------------------------------------------------------
def test_build_the_losers_bounce_on_an_up_day():
    # the last session: the market +4 % (~5 sd of its alternating moves); the week's losers (low drift, early names)
    # jump - a reversal; S00 / S10 ... carry the high short interest
    def last(k, r):
        return 0.03 + (0.06 if k < 12 else 0.0)
    data, rows, _ = make_world(60, last=last)
    b = bench_closes(last_jump=0.04)
    pdata = {t: (c, v) for t, (c, v) in panel_data(data, DATES).items()}
    doc = fx.build(rows, ("SPY", DATES, b), pdata)
    L = doc["last"]
    assert doc["date"] == DATES[-1] and doc["bench"] == "SPY" and len(doc["series"]["dates"]) == fx.WINDOW
    assert L["spx"]["sigma"] > 3 and L["character"]["up"]
    assert L["z"]["mom"] < -2 and L["character"]["label"] == "short-covering rally", L
    assert any(e["date"] == DATES[-1] for e in doc["events"])
    assert doc["baskets"]["formedOn"] == DATES[-1] and doc["counts"]["siBasket"] == len(doc["baskets"]["si"]) <= fx.SI_TOP
    assert set(doc["crowded"]) == {t for t in rows if rows[t]["si"] >= fx.SI_CROWDED}
    st = doc["stats"]["mom"]
    assert st["n"] == fx.WINDOW and st["sd"] > 0


def test_main_writes_once():
    data, rows, b = make_world(40)
    masters = {"TRAPP2": [{"ticker": t, "asset_class": "Equity", "currency": "USD", "marketcap": "5e9",
                           "shortPctFloat": str(rows[t]["si"])} for t in rows]}
    spy = [{"date": d, "close": c, "volume": 1e8} for d, c in zip(DATES, b)]

    def read(book, rel):
        if rel == "data/master.json":
            return masters.get(book)
        if rel.startswith("data/history/"):
            t = rel.rsplit("/", 1)[1][:-5]
            return spy if (book, t) == ("TRAPP2", "SPY") else data.get(t) if book == "TRAPP2" else None
        return None
    real = (fx.dv.read_book_file, fx.OUT)
    with tempfile.TemporaryDirectory() as d:
        fx.OUT = Path(d) / "derived" / "factors.json"
        fx.dv.read_book_file = read
        old_hist = fx.dv.history
        fx.dv.history = lambda book, sym: read(book, f"data/history/{sym}.json")
        try:
            # the module-level defaults captured the originals - call with the patched ones
            fx.universe_rows.__defaults__ = (read,)
            fx.load_bench.__defaults__ = (fx.dv.history,)
            fx.load_panel.__defaults__ = (fx.dv.history, 8)
            fx.patch_from_tapes.__defaults__ = (read, fx.TAPE_PATCH_DAYS)
            assert fx.main() == 0
            doc = json.loads(fx.OUT.read_text())
            assert doc["build"] == "z206" and doc["counts"]["eligibleLast"] == 40 and doc["bench"] == "SPY"
            before = fx.OUT.stat().st_mtime_ns
            os.environ["FACTORS_NOW"] = "2026-11-24T14:30:00Z"           # a later run, nothing new: not rewritten
            try:
                assert fx.main() == 0
            finally:
                os.environ["FACTORS_NOW"] = "2026-11-24T03:00:00Z"
            assert fx.OUT.stat().st_mtime_ns == before
        finally:
            fx.dv.read_book_file, fx.OUT = real
            fx.dv.history = old_hist
            fx.universe_rows.__defaults__ = (real[0],)
            fx.load_bench.__defaults__ = (old_hist,)
            fx.load_panel.__defaults__ = (old_hist, 8)
            fx.patch_from_tapes.__defaults__ = (real[0], fx.TAPE_PATCH_DAYS)


if __name__ == "__main__":                       # plain-python runner (the workflow has no pytest)
    ok = fail = 0
    for name, fn in sorted((k, v) for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)):
        try:
            fn()
            ok += 1
            print(f"PASS {name}")
        except Exception as ex:                  # noqa: BLE001
            import traceback
            fail += 1
            print(f"FAIL {name}: {type(ex).__name__} {ex}")
            traceback.print_exc(limit=3)
    print(f"\n{ok} passed, {fail} failed")
    sys.exit(1 if fail else 0)
