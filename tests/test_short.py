# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_short.py
#  z207 - pipeline/fetch_short.py: the short-interest pull pages to FINRA's Record-Total (no 60,000-row stop part-way
#  through the newest settlement), says so if it ever hits its page cap, keeps each company at its newest settlement,
#  leaves the change out across a stock split, summarises how much of the universe the newest settlement covers; and
#  today's short share of volume is scored against the sessions BEFORE it. Offline (FINRA mocked), < 1 s.
#  Run: python tests/test_short.py   (or python -m pytest tests/test_short.py -q)
# ============================================================
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import fetch_short as fs  # noqa: E402


def fake_finra(n_rows, total_header=True, page=None):
    """An http() stand-in: FINRA's token, then n_rows of short interest in pages (the first 60 % on 2026-10-30, the
    rest on 2026-11-13, symbols S0000 ...), and the Record-Total header."""
    page = page or fs.PAGE
    calls = []

    def http(url, data=None, headers=None, method=None, timeout=60, want_headers=False):
        if "oauth2" in url:
            return json.dumps({"access_token": "t"}).encode()
        body = json.loads(data.decode())
        calls.append(body["offset"])
        rows = []
        for i in range(body["offset"], min(body["offset"] + body["limit"], n_rows)):
            old = i < int(n_rows * 0.6)
            k = i if old else i - int(n_rows * 0.6)
            rows.append({"symbolCode": f"S{k:04d}", "settlementDate": "2026-10-30" if old else "2026-11-13",
                         "currentShortPositionQuantity": 1100 if not old else 1000, "previousShortPositionQuantity": 1000,
                         "averageDailyVolumeQuantity": 500, "daysToCoverQuantity": 2.2, "stockSplitFlag": "S" if k == 7 and not old else None})
        hdr = {"Record-Total": str(n_rows)} if total_header else {}
        return json.dumps(rows).encode(), hdr
    return http, calls


def with_env(fn):
    def run():
        old = (fs.http, os.environ.get("FINRA_CLIENT_ID"), os.environ.get("FINRA_CLIENT_SECRET"), fs.PAGE)
        os.environ["FINRA_CLIENT_ID"], os.environ["FINRA_CLIENT_SECRET"] = "id", "secret"
        try:
            fn()
        finally:
            fs.http, fs.PAGE = old[0], old[3]
            for k, v in (("FINRA_CLIENT_ID", old[1]), ("FINRA_CLIENT_SECRET", old[2])):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    run.__name__ = fn.__name__
    return run


@with_env
def test_pages_to_the_record_total_and_keeps_the_newest_settlement():
    fs.PAGE = 100                                     # small pages: 1,250 rows = 13 pages - past the old 12-page stop
    fs.http, calls = fake_finra(1250, page=100)
    tickers = [f"S{k:04d}" for k in range(750)]
    got = fs.short_interest(tickers)
    assert calls == list(range(0, 1300, 100))         # every page, then stop at FINRA's total
    s = fs.settlements(got)
    assert s == {"latest": "2026-11-13", "names": 500, "previous": "2026-10-30", "previousNames": 250}
    assert got["S0001"]["settlementDate"] == "2026-11-13" and got["S0001"]["changePct"] == 10.0
    assert got["S0600"]["settlementDate"] == "2026-10-30"          # only on the older settlement
    assert got["S0007"]["changePct"] is None and got["S0007"]["split"] is True


@with_env
def test_stops_on_a_short_page_without_the_header_and_warns_at_the_cap():
    fs.PAGE = 100
    fs.http, calls = fake_finra(450, total_header=False, page=100)
    fs.short_interest([f"S{k:04d}" for k in range(300)])
    assert calls == [0, 100, 200, 300, 400]
    logs = []
    real_log, real_cap = fs.log, fs.MAX_PAGES
    fs.log, fs.MAX_PAGES = (lambda *a: logs.append(" ".join(map(str, a)))), 3
    try:
        fs.http, calls = fake_finra(1000, page=100)
        fs.short_interest(["S0001"])
    finally:
        fs.log, fs.MAX_PAGES = real_log, real_cap
    assert calls == [0, 100, 200] and any("WARNING" in m and "3-page cap" in m and "of 1000" in m for m in logs)


def test_today_is_scored_against_the_sessions_before_it():
    # 21 sessions: 20 at 40 % / 50 % alternating, then today at 70 %
    days = []
    d = date(2026, 11, 24)
    while len(days) < 21:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    ratio = {dd: (0.70 if i == 0 else (0.40 if i % 2 else 0.50)) for i, dd in enumerate(days)}
    real = (fs.daily_file, fs.time.sleep, fs.date)

    class FakeDate(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 24)
    fs.daily_file = lambda dd: {"AAA": (ratio[dd] * 1000, 1000.0)} if dd in ratio else None
    fs.time.sleep, fs.date = (lambda s: None), FakeDate
    try:
        out = fs.short_volume(["AAA"], days=20)
    finally:
        fs.daily_file, fs.time.sleep, fs.date = real
    a = out["AAA"]
    import statistics
    base = [0.40 if i % 2 else 0.50 for i in range(1, 21)]
    assert a["date"] == "2026-11-24" and a["ratio1d"] == 70.0
    assert a["z"] == round((0.70 - statistics.mean(base)) / statistics.stdev(base), 2)       # ~ +4.4 sd, today not in its baseline
    assert a["ratio20d"] == round(statistics.mean([0.70] + base[:19]) * 100, 1)


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
