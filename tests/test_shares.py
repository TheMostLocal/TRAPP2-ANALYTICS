# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_shares.py
#  z202 - market cap by the formula (pipeline/shares.py): the SEC's cover share count against the count Yahoo's
#  market cap implies, the diluted count as the tie-breaker, the pick rules, the file. Offline, < 1 s.
#  Run: python tests/test_shares.py
# ============================================================
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
os.environ.setdefault("SHARES_NOW", "2026-11-21T15:00:00Z")
import shares as sh  # noqa: E402

TODAY = date(2026, 11, 21)


def test_frame_codes():
    assert sh.frame_codes(TODAY) == ["CY2026Q4I", "CY2026Q3I", "CY2026Q2I"]
    assert sh.frame_codes(date(2026, 2, 1)) == ["CY2026Q1I", "CY2025Q4I", "CY2025Q3I"]


def test_sec_counts_latest_per_filer():
    q4 = {"data": [{"cik": 320193, "val": 14_600_000_000, "end": "2026-10-17", "accn": "a"}, {"cik": 1, "val": "bad", "end": "2026-10-01"}]}
    q3 = {"data": [{"cik": 320193, "val": 14_700_000_000, "end": "2026-07-18", "accn": "b"}, {"cik": 2, "val": 5e8, "end": "2026-08-01"}]}
    c = sh.sec_counts([q4, q3, None])
    assert c[320193] == {"val": 14_600_000_000, "end": "2026-10-17", "accn": "a"} and c[2]["val"] == 5e8 and 1 not in c


def test_pick_rules():
    p = lambda *a: sh.pick(*a, TODAY)[:3]
    # AAPL-like: the two counts agree -> the SEC's, verified
    assert p(14.60e9, "2026-10-17", 14.53e9, 14.72e9, "2026-09-26") == (14.60e9, "sec", "verified")
    # 5 % apart: only the diluted count can decide - beside the SEC -> the SEC's; beside both / absent -> Yahoo, flagged
    assert p(1.05e9, "2026-10-20", 1.0e9, 1.07e9, "2026-09-30") == (1.05e9, "sec", "sec")
    assert p(1.05e9, "2026-10-20", 1.0e9, None, None) == (1.0e9, "yahoo", "near")
    assert p(1.02e9, "2026-10-20", 0.97e9, 1.0e9, "2026-09-30")[1:] == ("yahoo", "near"), "diluted near both: undecided"
    # a 10-for-1 split after the cover date: both SEC counts are pre-split (and agree) -> Yahoo, check
    assert p(1.0e8, "2026-10-01", 1.0e9, 1.0e8, "2026-09-30")[1:] == ("yahoo", "check")
    # an ADR: the cover counts ordinary shares, the price is per ADS -> Yahoo, check
    assert p(25.9e9, "2026-04-15", 5.19e9, None, None)[1:] == ("yahoo", "check")
    assert p(5e8, "2026-09-01", None, None, None) == (5e8, "sec", "sec-only")
    assert p(5e8, "2025-09-01", None, None, None)[1:] == (None, "none")
    assert p(None, None, 7e8, None, None) == (7e8, "yahoo", "yahoo-only")


def test_build_records_and_review():
    rows = {"AAPL": {"price": 342.56, "mcap": 4_978_144_288_451, "shares": 14_532_182_000, "usd": True, "fund": False},
            "GOOGL": {"price": 300.0, "mcap": 3.63e12, "shares": 5.8e9, "usd": True, "fund": False},
            "SPLT": {"price": 50.0, "mcap": 5e10, "shares": 1e9, "usd": True, "fund": False},
            "000660.KS": {"price": 200.0, "mcap": 1e11, "shares": 7e8, "usd": False, "fund": False},
            "SPY": {"price": 689.81, "mcap": None, "shares": None, "usd": True, "fund": True}}
    cik_of = {"AAPL": 320193, "GOOGL": 1652044, "SPLT": 9}
    counts = {320193: {"val": 14_600_000_000, "end": "2026-10-17"}, 9: {"val": 1e8, "end": "2026-10-01"}}
    docs = {"SPLT": {"facts": {"dilutedShares": {"quarterly": [{"end": "2026-09-30", "val": 1.01e8}], "annual": []}}}}
    d = sh.build(rows, cik_of, counts, docs, TODAY)
    a = d["tickers"]["AAPL"]
    assert a["p"] == "sec" and a["st"] == "verified" and a["s"] == 14_600_000_000 and a["se"] == [14_600_000_000, "2026-10-17"]
    assert abs(a["y"] - 14_532_182_000) <= 1000 and a["mc"] == round(342.56 * 14_600_000_000) and a["ym"] == 4_978_144_288_451
    assert abs(a["dx"] - 0.47) < 0.01
    g = d["tickers"]["GOOGL"]
    assert g["p"] == "yahoo" and g["st"] == "yahoo-only" and g["s"] == round(3.63e12 / 300), "Yahoo's IMPLIED count (its market cap / price), not its class-A 'shares'"
    s = d["tickers"]["SPLT"]
    assert s["p"] == "yahoo" and s["st"] == "check", s
    assert "000660.KS" not in d["tickers"] and "SPY" not in d["tickers"]
    assert d["review"][0]["t"] == "SPLT" and "split" in d["review"][0]["why"]
    assert d["counts"]["fromSec"] == 1 and d["counts"]["tickers"] == 3


def test_main_offline():
    real = (sh.get_json, sh.UA, sh.OUT, sh.DATA)
    q4 = {"data": [{"cik": 320193, "val": 14_600_000_000, "end": "2026-10-17", "accn": "a"}]}
    master = [{"ticker": "AAPL", "price": "342.56", "marketcap": "4978144288451", "shares": "14532182000", "currency": "USD"}]

    def fake(url, sec=False, tries=3):
        if url == sh.TICKERS_URL:
            return {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        if url.endswith("CY2026Q4I.json"):
            return q4
        if "/TRAPP2/main/data/master.json" in url:
            return master
        return None
    with tempfile.TemporaryDirectory() as d:
        try:
            sh.get_json, sh.UA, sh.DATA, sh.OUT = fake, "Valuatio research (ops@example.com)", Path(d), Path(d) / "sec" / "shares.json"
            sh.book_rows.__defaults__ = (fake,)
            assert sh.main() == 0
            doc = json.loads(sh.OUT.read_text())
            assert doc["frames"] == {"CY2026Q4I": 1, "CY2026Q3I": 0, "CY2026Q2I": 0} and doc["tickers"]["AAPL"]["st"] == "verified"
            sh.UA = ""
            sh.OUT.unlink()
            assert sh.main() == 0 and not sh.OUT.exists(), "no SEC user agent: nothing written"
        finally:
            sh.get_json, sh.UA, sh.OUT, sh.DATA = real
            sh.book_rows.__defaults__ = (real[0],)


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
