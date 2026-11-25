# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_factors.py
#  z206 - the factor monitor (pipeline/factors.py): the universe rules (US common stocks, no funds, short % of float in
#  (0, 100 %]), a close missing from the history taken from the book's day tape, the baskets formed on the PRIOR close
#  only (no look-ahead), the spreads and their z-scores, the day's label (short-covering rally, de-grossing, risk-off
#  rotation, mega-cap selling ...) and the file written once (an unchanged re-run writes nothing). Offline, < 5 s.
#  z207 - the official short interest: FINRA's shares short / the SEC share count (a share over 100 % dropped, a split
#  leaving the change out), the basket ranked on it (Yahoo only under 80 % FINRA coverage), the crowded rule, the
#  reported block (dollars short at today's prices, a part-way settlement flagged), the short-volume block.
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
            fx.load_short.__defaults__ = (lambda rel: None,)
            assert fx.main() == 0
            doc = json.loads(fx.OUT.read_text())
            assert doc["build"] == "z207" and doc["counts"]["eligibleLast"] == 40 and doc["bench"] == "SPY"
            assert doc["baskets"]["siSource"] == "yahoo" and doc["reported"] is None
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
            fx.load_short.__defaults__ = (None,)


# ---- z207: the official short interest -----------------------------------------------------------------------------
def official_for(names, pct, dtc=None, chg=None, settle="2026-11-13"):
    out = {}
    for k, t in enumerate(names):
        p = pct(k)
        prev = 1e6 / (1 + (chg(k) if chg else 0) / 100)
        out[t] = {"pct": p, "dtc": dtc(k) if dtc else 3.0, "chg": chg(k) if chg else 0.0, "settle": settle, "shares": 1e6, "prev": prev}
    return out


def test_load_short_is_finra_over_the_sec_count():
    idx = {"interest": {"AAA": {"settlementDate": "2026-11-13", "shares": 1_200_000, "prevShares": 1_000_000, "changePct": 20.0, "daysToCover": 8.1},
                        "BBB": {"settlementDate": "2026-11-13", "shares": 5_000_000, "prevShares": 4_000_000, "changePct": 25.0},
                        "CCC": {"settlementDate": "2026-11-13", "shares": 300_000, "prevShares": 100_000, "changePct": 200.0, "split": True},
                        "DDD": {"settlementDate": "2026-11-13", "shares": 300_000}},
           "volume": {"AAA": {"date": "2026-11-24", "ratio1d": 61.0, "ratio20d": 50.0, "z": 2.4}, "EEE": {"date": "2026-11-24"}}}
    shares = {"tickers": {"AAA": {"s": 10_000_000}, "BBB": {"s": 4_000_000}, "CCC": {"s": 3_000_000}}}
    off, vol = fx.load_short(lambda rel: idx if rel == "short/index.json" else shares if rel == "sec/shares.json" else None)
    assert sorted(off) == ["AAA", "CCC"]                                        # BBB: more short than outstanding; DDD: no count
    assert abs(off["AAA"]["pct"] - 0.12) < 1e-12 and off["AAA"]["dtc"] == 8.1 and off["AAA"]["chg"] == 20.0 and off["AAA"]["prev"] == 1e6
    assert off["CCC"]["chg"] is None and off["CCC"]["prev"] is None             # split between settlements: no change
    assert vol == {"AAA": {"r1": 61.0, "r20": 50.0, "z": 2.4, "date": "2026-11-24"}}


def test_the_basket_ranks_on_finra_and_the_crowded_rule():
    data, rows, b = make_world(60)
    pdata = panel_data(data, DATES)
    names = sorted(rows)
    # FINRA's ranking is the reverse of Yahoo's: S00 the most shorted (29.5 %); 8 %+ = S00 .. S43; S45 7 % short with
    # 8 days to cover (listed); S50 4.5 % with 9 days (not: under 5 %)
    off = official_for(names, lambda k: (59 - k) / 200, dtc=lambda k: {45: 8.0, 50: 9.0}.get(k, 3.0))
    doc = fx.build(rows, ("SPY", DATES, b), pdata, official=off)
    bk = doc["baskets"]
    assert bk["siSource"] == "finra" and doc["counts"]["finraCover"] == 1.0
    assert bk["si"][:3] == ["S00", "S01", "S02"] and len(bk["si"]) == fx.SI_TOP and bk["siPct"]["S00"] == 29.5 and bk["siDtc"]["S00"] == 3.0
    assert set(doc["crowded"]) == {f"S{k:02d}" for k in range(44)} | {"S45"}
    assert doc["crowded"]["S45"] == {"si": 7.0, "dtc": 8.0, "chg": 0.0, "settle": "2026-11-13", "src": "finra"}
    # FINRA on only 10 of the 60: the basket ranks on Yahoo; a Yahoo-only name at 10 %+ is listed as Yahoo's
    few = {t: off[t] for t in names[:10]}
    doc2 = fx.build(rows, ("SPY", DATES, b), pdata, official=few)
    assert doc2["baskets"]["siSource"] == "yahoo"
    yahoo_crowded = {t for t in names[10:] if rows[t]["si"] >= fx.SI_CROWDED}
    assert {t for t, v in doc2["crowded"].items() if v["src"] == "yahoo"} == yahoo_crowded


def test_reported_block_dollars_short_and_a_partial_settlement():
    off = {"A": {"pct": 0.20, "dtc": 9.0, "chg": -20.0, "settle": "2026-11-13", "shares": 800.0, "prev": 1000.0},
           "B": {"pct": 0.06, "dtc": 2.0, "chg": 50.0, "settle": "2026-11-13", "shares": 300.0, "prev": 200.0},
           "C": {"pct": 0.02, "dtc": 1.0, "chg": 10.0, "settle": "2026-11-13", "shares": 110.0, "prev": 100.0},
           "D": {"pct": 0.30, "dtc": 4.0, "chg": 5.0, "settle": "2026-10-30", "shares": 105.0, "prev": 100.0}}
    px = {"A": 10.0, "B": 100.0, "C": 50.0, "D": 20.0}
    r = fx.reported_block(["A", "B", "D"], off, px, {"A", "B", "C", "D"})
    assert r["settle"] == "2026-11-13" and r["names"] == 3 and r["cover"] == 0.75 and r["partial"] is True
    # the basket, in dollars at today's prices: A 8,000 vs 10,000, B 30,000 vs 20,000 (D is on the older settlement)
    assert r["basket"]["chg"] == round((38000 / 30000 - 1) * 100, 2) and r["basket"]["n"] == 2 and r["basket"]["dtc"] == 5.5
    assert r["basket"]["median"] == 15.0
    assert r["universe"]["chg"] == round(((8000 + 30000 + 5500) / (10000 + 20000 + 5000) - 1) * 100, 2)
    assert r["universe"]["adding"] == 2 and r["universe"]["covering"] == 1
    assert r["covered"] == [{"t": "A", "chg": -20.0, "pct": 20.0}] and r["added"] == [{"t": "B", "chg": 50.0, "pct": 6.0}]   # C under 5 %
    whole = fx.reported_block(["A"], {k: v for k, v in off.items() if k != "D"}, px, {"A", "B", "C"})
    assert whole["partial"] is False and whole["cover"] == 1.0


def test_short_volume_block():
    vol = {"A": {"r1": 60.0, "r20": 50.0, "z": 2.5, "date": "2026-11-24"}, "B": {"r1": 40.0, "r20": 45.0, "z": -1.0, "date": "2026-11-24"},
           "C": {"r1": 55.0, "r20": 52.0, "z": 0.5, "date": "2026-11-24"}, "D": {"r1": 90.0, "r20": 50.0, "z": 9.0, "date": "2026-11-20"}}
    v = fx.short_volume_block(vol, ["A", "B"], {"A", "B", "C", "D"})
    assert v == {"date": "2026-11-24", "names": 3, "ratio": 55.0, "ratio20": 50.0, "basketZ": 0.75, "high": 1}   # D: an older day


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
