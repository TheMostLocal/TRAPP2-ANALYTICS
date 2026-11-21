# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_trends.py
#  z201 - today's trends (pipeline/trends.py): close-to-close runs and how rare they are, the provisional "a red close
#  makes it N" while the market is open, opens used for the gap and the move from the open, extremes, the S&P 500 from
#  the inside (weight x move, drifted weights, data checks), the run's attribution, the sell-off read, what happened
#  next, the notable list, and the files written each session. Offline, < 2 s.
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
