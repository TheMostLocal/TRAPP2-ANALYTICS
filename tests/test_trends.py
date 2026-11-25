# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_trends.py
#  z201 - today's trends (pipeline/trends.py): close-to-close runs and how rare they are, the provisional "a red close
#  makes it N" while the market is open, opens used for the gap and the move from the open, extremes, the S&P 500 from
#  the inside (weight x move, drifted weights, data checks), the run's attribution, the sell-off read, what happened
#  next, the notable list, and the files written each session. Offline, < 2 s.
#  z207: the flows card carries FINRA's reported short interest and the day's short volume, each a notable.
#  z209: and dealer gamma from the index ETFs' open interest (TRAPP2-OPTIONS market.json), a notable.
#  Run: python tests/test_trends.py   (or python -m pytest tests/test_trends.py -q)
# ============================================================
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
os.environ["TRENDS_OFFLINE"] = "1"
os.environ["TRENDS_NOW"] = "2026-11-20T18:05:00Z"
import trends as tr  # noqa: E402


def sessions(n, end=date(2026, 11, 19)):
    """n weekday dates ending on `end`."""
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def rows(closes, end=date(2026, 11, 19), vols=None):
    ds = sessions(len(closes), end)
    return [{"date": d, "close": c, "volume": (vols[i] if vols else 1e9)} for i, (d, c) in enumerate(zip(ds, closes))]


def wave(n, base=100.0):
    """A long, gently rising series with small ups and downs - no long runs."""
    out, x = [], base
    for i in range(n):
        x *= 1.004 if i % 3 != 2 else 0.995
        out.append(round(x, 4))
    return out


def tape(day="2026-11-20", et_min=783, p=None, prev=None, op=None, close=None, open_=True, session="regular"):
    return {"date": day, "session": session, "etMinute": et_min, "etTime": f"{et_min // 60:02d}:{et_min % 60:02d}",
            "marketOpen": open_, "closeMinuteET": 960, "p": p or {}, "prevClose": prev or {}, "open": op or {}, "close": close or {}}


# ---- runs ------------------------------------------------------------------------------------------------------
def test_runs_and_flat_closes():
    c = [10, 11, 12, 12, 11, 10, 9, 9.5]
    assert tr.runs(c) == [(1, 1, 2), (-1, 4, 6), (1, 7, 7)]          # the flat close (12 -> 12) ends a run, starts none
    ds = [f"d{i}" for i in range(len(c))]
    assert tr.current_run(ds, c) == {"dir": "up", "n": 1, "from": "d7", "through": "d7"}
    assert tr.current_run(ds[:4], c[:4]) is None                        # last close flat


def test_run_context_counts_last_and_record():
    c = [100, 99, 98, 97, 96, 97, 96, 95, 94, 95, 94, 93, 92, 91, 90, 91, 90, 89]
    ds = [f"2026-01-{i + 1:02d}" for i in range(len(c))]
    cur = tr.current_run(ds, c)
    assert cur["dir"] == "down" and cur["n"] == 2
    ctx = tr.run_context(ds, c, "down", 4)
    assert ctx["atLeast"] == 2 and ctx["last"] == {"from": "2026-01-11", "to": "2026-01-15", "n": 5}   # 4-run and 5-run before
    assert ctx["record"]["n"] == 5 and ctx["since"] == "2026-01-01"
    assert tr.run_context(ds, c, "down", 2)["atLeast"] == 3                                            # the current one left out
    assert tr.run_context(ds, c, "down", 2, skip_last=False)["atLeast"] == 4


def test_forward_stats_counts_each_run_once_and_skips_the_live_one():
    c = [100, 99, 98, 97, 100, 101, 102, 103, 104, 103, 102, 101, 104, 105, 106, 107, 108, 107, 106, 105]
    out = tr.forward_stats([str(i) for i in range(len(c))], c, "down", 3, horizons=(1, 2))
    assert out["events"] == 2                                            # two earlier 3-runs; the last one (in progress) has no future
    h1 = out["h"]["1"]
    assert h1["count"] == 2 and h1["up"] == 1.0 and 0 < h1["baseUp"] < 1


# ---- the tape: provisional vs final, opens -------------------------------------------------------------------------
def test_tape_point_states():
    t = tape(p={"^GSPC": 7012.27}, prev={"^GSPC": 7175.83}, op={"^GSPC": 7150.39})
    pt = tr.tape_point("^GSPC", t, "2026-11-19")
    assert pt["final"] is False and pt["close"] == 7012.27 and pt["open"] == 7150.39 and pt["asOf"] == "13:03"
    assert tr.tape_point("^GSPC", t, "2026-11-20") is None                                   # the history already has today
    assert tr.tape_point("^GSPC", tape(et_min=540, p={"^GSPC": 1}), "2026-11-19") is None    # before the open
    assert tr.tape_point("^GSPC", tape(session="weekend", p={"^GSPC": 1}), "2026-11-19") is None
    fin = tr.tape_point("^GSPC", tape(et_min=965, p={"^GSPC": 7000}, close={"^GSPC": 7001.5}, open_=False), "2026-11-19")
    assert fin["final"] is True and fin["close"] == 7001.5                                   # the official close wins
    late = tr.tape_point("^GSPC", tape(et_min=1000, p={"^GSPC": 7000}, open_=False), "2026-11-19")
    assert late["final"] is True                                                             # well after the bell, the last price
    assert tr.phase_of(t) == "session" and tr.phase_of(tape(et_min=540, open_=False)) == "premarket"
    assert tr.phase_of(tape(et_min=990, open_=False)) == "after-close" and tr.phase_of(tape(session="holiday")) == "closed"
    old = os.environ.get("TRENDS_NOW")
    os.environ["TRENDS_NOW"] = "2026-11-21T15:00:00Z"                          # z202: Friday's tape read on Saturday
    try:
        assert tr.phase_of(tape(et_min=1170, open_=False)) == "closed"
    finally:
        if old is None:
            os.environ.pop("TRENDS_NOW", None)
        else:
            os.environ["TRENDS_NOW"] = old


def spx_like():
    """300 sessions of a calm rise, then the owner's week: 6 lower closes (Nov 12-19, 2026)."""
    c = wave(294, 5500) + [7619.38]                                     # ends ~7,359: Nov 11 is an up close
    c += [7592.40, 7561.67, 7416.59, 7340.53, 7323.63, 7175.83]
    return rows(c)


def test_card_counts_completed_closes_and_says_what_today_makes_it():
    r = spx_like()
    t = tape(p={"^GSPC": 7012.27}, prev={"^GSPC": 7175.83}, op={"^GSPC": 7150.39})
    c = tr.card("^GSPC", "S&P 500", r, t, with_volume=True)
    assert c["date"] == "2026-11-20" and not c["final"] and c["dir"] == "red" and c["asOf"] == "13:03"
    assert c["streak"] == {**c["streak"], "dir": "down", "n": 6, "from": "2026-11-12", "through": "2026-11-19"}
    assert c["provisional"]["now"] == "red" and c["provisional"]["red"]["n"] == 7 and c["provisional"]["green"]["n"] == 1
    # "next" = the same scenarios from completed sessions, keyed to the last completed close (the app's live headline)
    assert c["next"]["after"] == "2026-11-19" and c["next"]["red"] == c["provisional"]["red"] and c["next"]["green"]["n"] == 1
    assert abs(c["gap"] - (7150.39 / 7175.83 - 1) * 100) < 1e-3 and abs(c["fromOpen"] - (7012.27 / 7150.39 - 1) * 100) < 1e-3
    assert c["chg"] == round((7012.27 / 7175.83 - 1) * 100, 3) and "dataCheck" not in c
    assert c["high52"]["date"] == "2026-11-11" and c["high52"]["dd"] < -7.9
    assert c["ma"]["50"]["above"] is False
    assert "weeks" in c and c["week"]["chg"] < -7                          # Fri Nov 13 -> now
    # a green tape flips the scenario: the run would end at 6
    g = tr.card("^GSPC", "S&P 500", r, tape(p={"^GSPC": 7200.0}, prev={"^GSPC": 7175.83}))
    assert g["provisional"]["now"] == "green" and g["provisional"]["red"]["n"] == 7 and g["streak"]["n"] == 6


def test_card_final_from_the_tape_and_prev_close_mismatch_flagged():
    r = spx_like()
    c = tr.card("^GSPC", "S&P 500", r, tape(et_min=965, p={"^GSPC": 7000}, close={"^GSPC": 7010.0}, prev={"^GSPC": 7300.0}, open_=False))
    assert c["final"] is True and c["streak"]["n"] == 7 and c["streak"]["through"] == "2026-11-20" and "provisional" not in c
    assert c["next"]["after"] == "2026-11-20" and c["next"]["red"]["n"] == 8 and c["next"]["green"]["n"] == 1   # the next session
    pre = tr.card("^GSPC", "S&P 500", r, None)                              # before the open: no tape, scenarios still there
    assert pre["final"] and pre["streak"]["n"] == 6 and pre["next"]["after"] == "2026-11-19" and pre["next"]["red"]["n"] == 7
    assert "dataCheck" in c                                                # the tape's prior close disagrees with the history


def test_card_distribution_days_and_ma_crosses():
    c = [100 + i * 0.1 for i in range(260)]
    vols = [1e9] * 260
    for k in (250, 253, 256):                                              # three 0.3 % declines on rising volume
        c[k] = c[k - 1] * 0.997
        vols[k] = vols[k - 1] * 1.2
        for j in range(k + 1, 260):
            c[j] = c[j] * 0.997
    card = tr.card("X", "X", rows(c, vols=vols), None, with_volume=True)
    assert card["distribution"]["count"] == 3
    assert card["cross"]["state"] == "golden"


# ---- the S&P 500 from the inside ---------------------------------------------------------------------------------
def test_internals_weights_drift_breadth_sectors_and_data_checks():
    weights = {"BIG": 0.5, "MID": 0.3, "SML": 0.2, "BAD": 0.0001, "OFF": 0.05}
    base = {"BIG": 100.0, "MID": 50.0, "SML": 20.0}                         # holdings-date prices
    prices = {"BIG": (90.0, 100.0), "MID": (51.0, 50.0), "SML": (20.4, 20.0), "BAD": (1.0, 10.0)}   # BAD: -90 %
    sectors = {"BIG": "Technology", "MID": "Energy", "SML": "Energy"}
    ins = tr.internals(prices, weights, base, sectors)
    assert ins["priced"] == 3 and ins["up"] == 2 and ins["down"] == 1 and ins["suspect"][0]["t"] == "BAD"
    big = next(x for x in ins["top"] if x["t"] == "BIG")
    assert big["move"] == -10.0 and big["bp"] < -450                        # ~ -10 % x ~48 % weight
    assert ins["cw"] < ins["ew"] and ins["sectors"][0]["sector"] == "Technology"
    assert abs(sum(x["bp"] for x in ins["sectors"]) - ins["explained"] * 100) < 0.5
    w2 = tr.drifted({"A": 0.5, "B": 0.5}, {"A": 10, "B": 10}, {"A": 20, "B": 10})
    assert abs(w2["A"] - 2 / 3) < 1e-9                                      # A doubled since the holdings date
    assert tr.internals({}, weights, base, sectors) is None


def test_streak_attribution_sums_sessions():
    days = [("2026-11-18", {"NVDA": [-2.0, -15.0], "XOM": [1.0, 2.0]}, {"NVDA": "Technology", "XOM": "Energy"}),
            ("2026-11-19", {"NVDA": [-3.0, -20.0], "XOM": [0.5, 1.0]}, None),
            ("2026-11-20", None, None)]
    a = tr.streak_attribution(days)
    assert a["days"] == 2 and a["bp"] == -32.0 and a["top"][0] == {"t": "NVDA", "bp": -35.0, "sector": "Technology"}
    assert a["sectors"][0]["sector"] == "Technology" and a["bottom"][0]["t"] == "XOM"


# ---- the read and the notable list -------------------------------------------------------------------------------
def test_sell_off_read_states():
    spx = {"streak": {"dir": "down", "n": 6}, "provisional": {"now": "red", "red": {"n": 7}}, "high52": {"dd": -10.3},
           "ma": {"50": {"above": False, "pct": -8.1}}, "distribution": {"count": 8}}
    vix = {"close": 32.5, "_series": (list("abcdefg"), [19, 19, 20, 21, 25, 28.4, 32.5])}
    ins = {"pctUp": 45.6, "ew": -0.99}
    r = tr.sell_off_read(spx, vix, ins, None)
    assert r["state"] == "heavy sell-off" and r["overlay"] == "correction" and r["on"] == 5 and r["of"] == 6 and r["provisional"]
    calm = tr.sell_off_read({"streak": {"dir": "up", "n": 3}, "high52": {"dd": -1}, "ma": {"50": {"above": True, "pct": 2}}, "distribution": {"count": 1}},
                            {"close": 14, "_series": (list("abcdefg"), [14] * 7)}, {"pctUp": 70, "ew": 0.5}, None)
    assert calm["state"] == "calm" and calm["overlay"] is None and calm["on"] == 0
    bear = tr.sell_off_read({"high52": {"dd": -22}, "ma": {}}, None, None, None)
    assert bear["overlay"] == "bear market" and any(x["on"] is None for x in bear["criteria"])


def test_build_and_notables_tell_the_story():
    r = spx_like()
    hist = {"^GSPC": r, "^VIX": rows([15.0] * 290 + [19, 20, 22, 24, 25.5, 28.44])}
    for s, _ in tr.INDICES + tr.SECTORS + tr.CROSS:
        hist.setdefault(s, rows(wave(300, 50)))
    t = tape(p={"^GSPC": 7012.27, "^VIX": 32.47}, prev={"^GSPC": 7175.83, "^VIX": 28.44}, op={"^GSPC": 7150.39})
    universe = {"indices": {"SPY": {"updatedAt": "2026-10-21T13:00:00Z", "members": [
        {"ticker": "NVDA", "weight": 0.08}, {"ticker": "AVGO", "weight": 0.03}, {"ticker": "XOM", "weight": 0.02}, {"ticker": "WMT", "weight": 0.02}]}}}
    member = {"date": "2026-11-20", "p": {"NVDA": 135.1, "AVGO": 300.0, "XOM": 120.0, "WMT": 103.0},
              "prevClose": {"NVDA": 145.98, "AVGO": 340.6, "XOM": 118.4, "WMT": 100.2}}
    doc = tr.build(hist, t, [member], universe, {"NVDA": "Technology", "AVGO": "Technology", "XOM": "Energy", "WMT": "Consumer Defensive"},
                   {"NVDA": 200.0, "AVGO": 350.0, "XOM": 115.0, "WMT": 98.0},
                   {"2026-11-19": {"NVDA": [-3.3, -25.0], "AVGO": [-4.0, -10.0]}}, {"risk": 39.5, "level": "low", "regime": "late cycle", "date": "2026-11-20"})
    assert doc["state"] == "intraday" and doc["phase"] == "session" and doc["date"] == "2026-11-20"
    txt = [n["text"] for n in doc["notable"]]
    first = doc["notable"][0]
    assert first["kind"] == "provisional" and "a red close makes it 7 straight lower closes" in first["text"] and "after opening -0.35%" in first["text"]
    assert any(t.startswith("S&P 500: 6 straight lower closes (from Nov 12, 2026)") for t in txt)
    assert any("Inside the S&P 500: 2 of 4 priced members rose" in t for t in txt)
    assert any(t.startswith("The slower read (early warning") and "market-led sell-off" in t for t in txt)
    ins = doc["internals"]
    assert ins["top"][0]["t"] == "NVDA" and doc["weights"]["asOf"] == "2026-10-21"
    a = doc["runAttribution"]
    assert a["days"] == 2 and a["top"][0]["t"] == "NVDA" and a["top"][0]["bp"] < -25       # Nov 19 on file + today
    assert doc["read"]["state"] in ("sell-off", "heavy sell-off") and doc["next"]["ifToday"] is True
    assert "_series" not in json.dumps(doc["indices"]) and doc["_members"]["NVDA"][0] < -7


def test_reversal_and_lopsided_breadth_items():
    c = {"sym": "^GSPC", "name": "S&P 500", "date": "2026-11-19", "chg": -2.02, "gap": 0.9, "fromOpen": -2.89, "dir": "red", "final": True}
    ins = {"pctUp": 8.0, "up": 35, "down": 402, "priced": 437, "cw": -2.04, "ew": -1.79, "top": [{"t": "NVDA", "move": -3.3, "bp": -25.0}],
           "sectors": [], "suspect": [{"t": "SNDK", "move": -89.0}]}
    out = tr.notables([c], ins, None)
    txt = " | ".join(n["text"] for n in out)
    assert "S&P 500 opened higher (+0.90%) and closed lower (-2.02%) - a 2.89% slide from the open." in txt
    assert "A lopsided down day: 8% of the priced S&P 500 members rose" in txt and "Data check: SNDK -89%" in txt


# ---- the files, each session ---------------------------------------------------------------------------------------
def test_main_writes_latest_daily_history_and_redoes_provisional_records():
    hist = {"^GSPC": spx_like(), "^VIX": rows([15.0] * 290 + [19, 20, 22, 24, 25.5, 28.44])}
    for s, _ in tr.INDICES + tr.SECTORS + tr.CROSS:
        hist.setdefault(s, rows(wave(300, 50)))
    live = tape(p={"^GSPC": 7012.27}, prev={"^GSPC": 7175.83}, op={"^GSPC": 7150.39})
    day19 = {"date": "2026-11-19", "close": {"NVDA": 145.98}, "prevClose": {"NVDA": 150.96}, "open": {"^GSPC": 7389.26}}
    day20 = {"date": "2026-11-20", "close": {"NVDA": 135.0, "^GSPC": 7001.1}, "prevClose": {"NVDA": 145.98}, "open": {"^GSPC": 7150.39}}

    def fake(url, tries=3, timeout=40):
        if "/data/history/" in url:
            sym = url.rsplit("/", 1)[1][:-5].replace("%5E", "^").replace("%3D", "=")
            return hist.get(sym) if "TRAPP2-1/" in url else None
        if url.endswith("TRAPP2-1/main/data/intraday/latest.json"):
            return live
        if url.endswith("/data/intraday/2026-11-19.json"):
            return day19
        if url.endswith("/data/intraday/2026-11-20.json"):
            return day20
        if url.endswith("TRAPP2/main/data/intraday/latest.json"):
            return {"date": "2026-11-20", "p": {"NVDA": 135.1}, "prevClose": {"NVDA": 145.98}}
        return None
    real = (tr.fetch_json, tr.DATA, tr.OUT)
    with tempfile.TemporaryDirectory() as d:
        tr.DATA, tr.OUT = Path(d), Path(d) / "trends"
        (Path(d) / "earnings").mkdir()
        (Path(d) / "earnings" / "universe.json").write_text(json.dumps({"indices": {"SPY": {"updatedAt": "2026-10-21", "members": [{"ticker": "NVDA", "weight": 0.08}]}}}))
        (tr.OUT / "daily").mkdir(parents=True)
        (tr.OUT / "daily" / "2026-11-18.json").write_text(json.dumps({"state": "intraday", "date": "2026-11-18"}))   # left provisional
        (tr.OUT / "weights_base.json").write_text(json.dumps({"asOf": "2026-10-21", "prices": {"NVDA": 200.0}}))
        tr.fetch_json = fake
        try:
            assert tr.main() == 0
            latest = json.loads((tr.OUT / "latest.json").read_text())
            assert latest["date"] == "2026-11-20" and latest["state"] == "intraday" and "_members" not in latest
            assert not (tr.OUT / "daily" / "2026-11-20.json").exists()          # intraday: latest.json only
            d19 = json.loads((tr.OUT / "daily" / "2026-11-19.json").read_text())
            assert d19["state"] == "final" and d19["backfilled"] and d19["indices"]["^GSPC"]["gap"] > 0.8        # the day file's official open
            d18 = json.loads((tr.OUT / "daily" / "2026-11-18.json").read_text())
            assert d18.get("state") != "final" or d18.get("backfilled")              # no tape for the 18th here: left as it was
            h = json.loads((tr.OUT / "history.json").read_text())["rows"]
            assert h[-1]["date"] == "2026-11-19" and h[-1]["run"] == -6 and h[-1]["gap"] > 0.8
            # a later intraday run: latest.json moves, history.json (nothing new) is not rewritten
            before = (tr.OUT / "history.json").read_bytes()
            live.update(etMinute=843, etTime="14:03", p={"^GSPC": 7050.0})
            assert tr.main() == 0
            assert json.loads((tr.OUT / "latest.json").read_text())["etTime"] == "14:03"
            assert (tr.OUT / "history.json").read_bytes() == before
            # after the bell: the official close makes the day final - its record is written and joins the history
            live.update(etMinute=965, etTime="16:05", close={"^GSPC": 7001.1})
            assert tr.main() == 0
            today = json.loads((tr.OUT / "daily" / "2026-11-20.json").read_text())
            assert today["state"] == "final" and today["members"]["NVDA"][0] < -7 and today["indices"]["^GSPC"]["streak"]["n"] == 7
            h = json.loads((tr.OUT / "history.json").read_text())["rows"]
            assert h[-1]["date"] == "2026-11-20" and h[-1]["run"] == -7 and h[-1]["spx"] == 7001.1 and h[-1]["read"]
            assert h[-2]["date"] == "2026-11-19" and len({r["date"] for r in h}) == len(h)
            assert today["indices"]["^GSPC"]["next"]["after"] == "2026-11-20" and today["indices"]["^GSPC"]["next"]["red"]["n"] == 8
            assert "next" not in json.loads((tr.OUT / "latest.json").read_text())["sectors"]["XLK"]       # lean: indices only
            # a re-run with nothing new (after the close): no file rewritten -> no commit
            snap = {f: (tr.OUT / f).read_bytes() for f in ("latest.json", "history.json", "daily/2026-11-20.json")}
            os.environ["TRENDS_NOW"] = "2026-11-20T23:45:00Z"                   # a later clock: only asOf would differ
            try:
                assert tr.main() == 0
            finally:
                os.environ["TRENDS_NOW"] = "2026-11-20T18:05:00Z"
            assert all((tr.OUT / f).read_bytes() == b for f, b in snap.items())
            # z202 - the weekend: the history now holds the 20th and the tape is closed - the day is read from the
            # history, and its official open (the gap, the move from the open) still comes from TRAPP2-1's day tape
            hist["^GSPC"] = hist["^GSPC"] + [{"date": "2026-11-20", "close": 7001.1, "volume": 6e9}]
            live.update(session="closed", marketOpen=False)
            os.environ["TRENDS_NOW"] = "2026-11-21T15:00:00Z"
            try:
                assert tr.main() == 0
            finally:
                os.environ["TRENDS_NOW"] = "2026-11-20T18:05:00Z"
            sat = json.loads((tr.OUT / "latest.json").read_text())
            g = sat["indices"]["^GSPC"]
            assert sat["date"] == "2026-11-20" and sat["state"] == "final" and g["streak"]["n"] == 7, (sat["date"], sat["state"])
            assert abs(g["gap"] - (7150.39 / 7012.27 - 1) * 100) < 0.01 or abs(g["gap"] - (7150.39 / hist["^GSPC"][-2]["close"] - 1) * 100) < 0.01, g.get("gap")
            assert json.loads((tr.OUT / "daily" / "2026-11-20.json").read_text())["indices"]["^GSPC"]["gap"] == g["gap"]
        finally:
            tr.fetch_json, tr.DATA, tr.OUT = real


# ---- z206: who was forced to trade -------------------------------------------------------------------------------
def fake_factors(formed="2026-11-19"):
    """factors.json as pipeline/factors.py writes it: 10 names, fifths of 2, a year of spreads with sd 1 %."""
    uni = [f"N{k}" for k in range(10)]
    return {"build": "z206", "date": formed,
            "stats": {"ew": {"mean": 0.0, "sd": 1.0, "n": 252}, "si": {"mean": 0.0, "sd": 1.0, "n": 252},
                      "mom": {"mean": 0.0, "sd": 1.0, "n": 252}, "rev": {"mean": 0.0, "sd": 1.0, "n": 252},
                      "beta": {"mean": 0.0, "sd": 1.0, "n": 252}},
            "baskets": {"formedOn": formed, "universe": uni,
                        "mom": {"bottom": ["N0", "N1"], "top": ["N8", "N9"]}, "rev": {"bottom": ["N0", "N1"], "top": ["N8", "N9"]},
                        "beta": {"bottom": ["N4", "N5"], "top": ["N0", "N1"]}, "si": ["N0", "N2"]},
            "series": {"dates": ["2026-11-18", formed], "ew": [0.1, -2.0], "si": [0.2, 0.5], "mom": [0.1, -2.6], "rev": [0.0, 0.3],
                       "beta": [0.2, -1.0], "siL": [0.3, -1.5], "momL": [0.1, -3.0], "momS": [0.0, -0.4], "n": [10, 10]},
            "last": {"date": formed, "character": {"label": "de-grossing"}}}


def test_flows_from_the_tapes_label_relief_and_notes():
    # an up day (+3 % on a calm series: many sd): the week's losers (N0, N1) +12 %, its winners +1 %, the rest +3 %
    hist = {"^GSPC": rows(wave(300, 5000))}
    last = hist["^GSPC"][-1]["close"]
    spx = tr.card("^GSPC", "S&P 500", hist["^GSPC"], tape(p={"^GSPC": last * 1.03}, prev={"^GSPC": last}, op={"^GSPC": last * 1.028}))
    prices = {f"N{k}": (100 * (1.12 if k < 2 else 1.01 if k >= 8 else 1.03), 100.0) for k in range(10)}
    read = {"state": "sell-off", "overlay": None}
    fl = tr.day_flows(fake_factors(), prices, spx, {"^GSPC": spx}, {"cw": 3.6, "ew": 4.2, "pctUp": 90}, read, [])
    assert fl["source"] == "tapes" and fl["formedOn"] == "2026-11-19" and fl["coverage"] == {"priced": 10, "of": 10}
    assert abs(fl["ret"]["rev"] - 11.0) < 1e-6 and fl["z"]["rev"] == 11.0 and fl["z"]["mom"] == -11.0
    assert fl["label"] == "short-covering rally" and fl["flags"]["bounce"] and fl["flags"]["unwind"]
    assert fl["idx"]["gapShare"] > 0.9 and fl["provisional"] is True
    assert fl["relief"] is not None and fl["relief"]["n"] == 0                  # a calm record: no day like it before
    notes = tr.flow_notes(fl, spx)
    assert notes[0]["kind"] == "flows" and notes[0]["sev"] == 3 and notes[0]["text"].startswith("Positioning so far: short-covering rally")
    assert "not reported flows" in notes[0]["text"]
    # too few of the baskets' names priced yet: no label
    few = tr.day_flows(fake_factors(), {"N0": (101, 100)}, spx, {"^GSPC": spx}, None, read, [])
    assert few["label"] is None and few["coverage"] == {"priced": 1, "of": 10}


def test_flows_for_a_day_the_nightly_series_already_holds_and_the_day_after():
    hist = {"^GSPC": rows(wave(300, 5000))}
    spx = tr.card("^GSPC", "S&P 500", hist["^GSPC"], None)                        # the 19th, final, from the history
    fl = tr.day_flows(fake_factors(formed="2026-11-19"), {}, spx, {"^GSPC": spx}, None, None, [])
    assert fl["source"] == "history" and fl["z"]["mom"] == -2.6 and fl["ret"]["ew"] == -2.0
    # the session after a short-covering rally, down: no follow-through
    last = hist["^GSPC"][-1]["close"]
    spx2 = tr.card("^GSPC", "S&P 500", hist["^GSPC"], tape(p={"^GSPC": last * 0.988}, prev={"^GSPC": last}, op={"^GSPC": last * 0.99}))
    prior = [{"date": "2026-11-19", "chg": 4.73, "flow": "short-covering rally"}]
    fl2 = tr.day_flows(fake_factors(), {f"N{k}": (99.0, 100.0) for k in range(10)}, spx2, {"^GSPC": spx2}, None, None, prior)
    assert fl2["after"] == {"prior": "2026-11-19", "priorChg": 4.73, "chg": spx2["chg"]}
    t = [n["text"] for n in tr.flow_notes(fl2, spx2)]
    assert any(x.startswith("No follow-through so far: S&P 500 -1.20% the session after the short-covering rally of Nov 19, 2026 (+4.73%)") for x in t), t
    # the prior session's label from factors.json when the history row has none (the first z206 day)
    fac = fake_factors()
    fac["last"] = {"date": "2026-11-19", "character": {"label": "short-covering rally"}}
    fl3 = tr.day_flows(fac, {f"N{k}": (99.0, 100.0) for k in range(10)}, spx2, {"^GSPC": spx2}, None, None, [{"date": "2026-11-19", "chg": 4.7}])
    assert fl3["after"]["prior"] == "2026-11-19"
    # baskets formed too long before the session (a stale nightly file) and the day not in its series: no flows
    assert tr.day_flows(fake_factors(formed="2026-11-10"), {"N0": (1, 1)}, spx2, {"^GSPC": spx2}, None, None, []) is None


def test_flows_carry_finras_reported_short_interest_and_short_volume():
    # z207: factors.json's FINRA blocks ride on the flows card (both return paths) and become notables
    fac = fake_factors()
    fac["reported"] = {"settle": "2026-11-13", "partial": False, "cover": 0.99, "names": 1490,
                       "basket": {"chg": -8.4, "median": -6.0, "dtc": 5.0, "n": 50},
                       "universe": {"chg": 1.2, "adding": 152, "covering": 173},
                       "covered": [{"t": "FCN", "chg": -31.5, "pct": 6.6}, {"t": "AAL", "chg": -19.6, "pct": 11.9}],
                       "added": [{"t": "CTRE", "chg": 121.4, "pct": 8.1}]}
    fac["shortVolume"] = {"date": "2026-11-24", "names": 1492, "ratio": 51.1, "ratio20": 52.1, "basketZ": 1.4, "high": 35}
    hist = {"^GSPC": rows(wave(300, 5000))}
    last = hist["^GSPC"][-1]["close"]
    spx = tr.card("^GSPC", "S&P 500", hist["^GSPC"], tape(p={"^GSPC": last * 1.01}, prev={"^GSPC": last}, op={"^GSPC": last * 1.005}))
    fl = tr.day_flows(fac, {f"N{k}": (101.0, 100.0) for k in range(10)}, spx, {"^GSPC": spx}, None, None, [])
    assert fl["reported"]["basket"]["chg"] == -8.4 and fl["shortVolume"]["basketZ"] == 1.4 and "FINRA" in fl["basis"]
    t = {n["kind"]: n for n in tr.flow_notes(fl, spx)}
    rp = t["short interest"]
    assert rp["sev"] == 2 and rp["text"].startswith("Reported short interest (FINRA, settled Nov 13, 2026): the 50 most-shorted names -8.4% in dollars short")
    assert "median name -6.0%, 5.0 days to cover" in rp["text"] and "152 added 10%+, 173 covered 10%+" in rp["text"]
    assert "Covered most: FCN -32% (7% short), AAL -20% (12% short)." in rp["text"] and "Added most: CTRE +121% (8% short)." in rp["text"]
    assert t["short volume"]["text"].startswith("Short sales Nov 24, 2026: the most-shorted 50 averaged +1.4 sd") and "market makers" in t["short volume"]["text"]
    # a small move between settlements: context (sev 1); a quiet short-volume day: no line; a part-way settlement says so
    fac["reported"]["basket"]["chg"], fac["reported"]["partial"], fac["reported"]["cover"], fac["shortVolume"]["basketZ"] = 1.0, True, 0.67, 0.3
    fl2 = tr.day_flows(fac, {f"N{k}": (101.0, 100.0) for k in range(10)}, spx, {"^GSPC": spx}, None, None, [])
    t2 = {n["kind"]: n for n in tr.flow_notes(fl2, spx)}
    assert t2["short interest"]["sev"] == 1 and "only 67% of names on it yet" in t2["short interest"]["text"] and "short volume" not in t2
    # too few names priced: no label, the FINRA blocks still carried
    few = tr.day_flows(fac, {"N0": (101, 100)}, spx, {"^GSPC": spx}, None, None, [])
    assert few["label"] is None and few["reported"]["settle"] == "2026-11-13"


def test_dealer_gamma_beside_the_price_signature():
    # z209: TRAPP2-OPTIONS market.json's dealer block -> flows.dealer (index ETFs, fresh reads only) and a notable
    mk = {"dealer": {"SPY": {"spot": 717.28, "asOf": "2026-11-25T20:51:13Z", "gex": -6181258143, "flip": 727.85, "flipPct": 1.47,
                             "callWall": 730.0, "putWall": 700.0, "byTenor": {"0dte": -9.2e8, "1-7": -3.05e9, "8-35": 0, "36+": -2.2e9},
                             "change": {"callChgPct": 2.0, "putChgPct": -6.3, "common": 7, "prior": "2026-11-24"}},
                     "QQQ": {"spot": 633.57, "asOf": "2026-11-18T20:51:13Z", "gex": 1.2e9},          # a week old: left out
                     "XLE": {"spot": 90.0, "asOf": "2026-11-25T20:51:13Z", "gex": 1e8}}}           # not an index ETF
    dv = tr.dealer_view(mk, "2026-11-25")
    assert sorted(dv) == ["SPY"] and dv["SPY"]["flip"] == 727.85 and dv["SPY"]["change"]["putChgPct"] == -6.3
    assert tr.dealer_view({"dealer": None}, "2026-11-25") is None and tr.dealer_view(None, "2026-11-25") is None
    spx = {"name": "S&P 500", "sigma": 1.0}
    n = [x for x in tr.flow_notes({"date": "2026-11-25", "label": None, "dealer": dv}, spx) if x["kind"] == "dealer gamma"][0]
    assert n["sev"] == 2 and n["text"].startswith("Dealer gamma (SPY options, open interest x gamma): -$6.2B per 1% move - negative: dealers' hedging adds to moves")
    assert "above ~$728 (the flip, +1.5%) it turns positive; call wall $730, put wall $700." in n["text"]
    assert "Put open interest -6.3%, calls +2.0% on the 7 expiries both days hold (the prior session's trading) - hedges closed." in n["text"]
    assert "assumes dealers are long the calls customers sell" in n["text"]
    pos = dict(dv["SPY"], gex=2.4e9, flip=700.5, flipPct=-2.34, change={"callChgPct": 1.0, "putChgPct": 1.1, "common": 7})
    n2 = [x for x in tr.flow_notes({"dealer": {"SPY": pos}}, spx) if x["kind"] == "dealer gamma"][0]
    assert n2["sev"] == 1 and "+$2.4B per 1% move - positive: dealers' hedging damps moves; below ~$700 (the flip, -2.3%) it turns negative" in n2["text"]
    assert "hedges" not in n2["text"].split("(the prior session's trading)")[1]


def test_relief_base_counts_big_up_days_in_drawdowns():
    c, x = [], 100.0
    for i in range(400):                                   # calm, then a 12 % fall, a 2-sd+ jump, and the tape after it
        x *= 1.001 if i % 2 else 0.9995
        c.append(x)
    for _ in range(12):
        x *= 0.99
        c.append(x)
    pre = x
    x *= 1.05
    c.append(x)
    for _ in range(25):
        x *= 0.995
        c.append(x)
    ds = sessions(len(c))
    rb = tr.relief_base(ds, c)
    assert rb["n"] == 1 and rb["last"] == [ds[412]] and rb["nextDown"] == 1.0 and rb["retest"] == 1.0 and c[413] < c[412]
    assert pre == c[411]


def test_main_reads_the_etfs_when_the_macro_book_is_out():
    """z206 - 11-24: every TRAPP2-1 file 404. The read goes on from TRAPP2's SPY / QQQ / DIA / IWM / sector SPDRs /
    HYG / TLT and its tape; the history keeps the index's rows; every card says where it came from."""
    spy = rows(wave(300, 500))
    etf = {"SPY": spy}
    for s in ("QQQ", "DIA", "IWM", "HYG", "TLT") + tuple(x for x, _ in tr.SECTORS):
        etf[s] = rows(wave(300, 80))
    last = spy[-1]["close"]
    trapp2_tape = tape(p={"SPY": last * 0.988, "XLK": 79.0, "NVDA": 130.0}, prev={"SPY": last, "XLK": 80.0, "NVDA": 135.0},
                       op={"SPY": last * 0.99})

    def fake(url, tries=3, timeout=40):
        if "TRAPP2-1/" in url:
            return None                                                         # the macro book: 404
        if "/TRAPP2/main/data/history/" in url:
            return etf.get(url.rsplit("/", 1)[1][:-5])
        if url.endswith("TRAPP2/main/data/intraday/latest.json"):
            return trapp2_tape
        return None
    real = (tr.fetch_json, tr.DATA, tr.OUT)
    with tempfile.TemporaryDirectory() as d:
        tr.DATA, tr.OUT = Path(d), Path(d) / "trends"
        (Path(d) / "earnings").mkdir()
        (Path(d) / "earnings" / "universe.json").write_text(json.dumps({"indices": {"SPY": {"updatedAt": "2026-10-21", "members": [{"ticker": "NVDA", "weight": 0.08}]}}}))
        (tr.OUT / "daily").mkdir(parents=True)
        for k, dd in enumerate(sessions(12)):
            (tr.OUT / "daily" / f"{dd}.json").write_text(json.dumps({"state": "final", "date": dd, "members": {}}))
        old_rows = [{"date": "2026-11-18", "spx": 7400.0, "chg": -0.1, "run": -5}, {"date": "2026-11-19", "spx": 7175.83, "chg": -2.0, "run": -6}]
        (tr.OUT / "history.json").write_text(json.dumps({"build": "z202", "rows": old_rows}))
        (tr.OUT / "weights_base.json").write_text(json.dumps({"asOf": "2026-10-21", "prices": {"NVDA": 200.0}}))
        tr.fetch_json = fake
        try:
            assert tr.main() == 0
            doc = json.loads((tr.OUT / "latest.json").read_text())
            g = doc["indices"]["^GSPC"]
            assert doc["proxy"]["^GSPC"] == "SPY" and doc["proxy"]["XLK"] == "XLK" and g["name"] == "S&P 500 (SPY)"
            assert doc["date"] == "2026-11-20" and abs(g["chg"] + 1.2) < 1e-6 and abs(g["gap"] + 1.0) < 1e-6
            assert doc["indices"]["^IXIC"]["name"] == "Nasdaq-100 (QQQ)" and doc["indices"]["^NYA"] is None and doc["vol"]["^VIX"] is None
            assert doc["notable"][0]["id"] == "data:macro" and "S&P 500 = SPY" in doc["notable"][0]["text"] and "no VIX" in doc["notable"][0]["text"]
            r = doc["read"]
            assert [c["on"] for c in r["criteria"] if c["k"] == "fear"] == [None] and r["of"] == 6       # no VIX: 6 criteria known
            h = json.loads((tr.OUT / "history.json").read_text())
            assert h["proxy"]["^GSPC"] == "SPY" and h["rows"] == old_rows                 # intraday: the index's rows kept as they were
        finally:
            tr.fetch_json, tr.DATA, tr.OUT = real


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
            traceback.print_exc(limit=2)
    print(f"\n{ok} passed, {fail} failed")
    sys.exit(1 if fail else 0)
