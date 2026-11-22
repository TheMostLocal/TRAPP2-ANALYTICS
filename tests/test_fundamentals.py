# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_fundamentals.py
#  z204 - fundamentals and multiples by the formula, as filed (pipeline/fundamentals.py) and the facts they need
#  (pipeline/fetch_sec.py FACTS_VERSION 4): the trailing twelve months (FY + quarters since - the same quarters a year
#  earlier; four quarters; no stale year when a filing is missing), per-share figures across splits (and not across an
#  acquisition paid in shares), debt / cash / common equity, the multiples, the check against Yahoo on the same period
#  and the pick, the transitional rules for facts read before z204, secondary listings, and the file. Offline, < 1 s.
#  Run: python tests/test_fundamentals.py
# ============================================================
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
os.environ["DERIVED_OFFLINE"] = "1"
os.environ.setdefault("FUND_NOW", "2026-11-22T03:10:00Z")
import fundamentals as fd  # noqa: E402
import fetch_sec as fs  # noqa: E402

TODAY = date(2026, 11, 22)


def rec_(end, val, fp="Q1", form="10-Q", filed=None):
    return {"end": end, "val": val, "fy": int(end[:4]), "fp": fp, "form": form, "filed": filed or end}


def item(annual=(), quarterly=(), concept="X", latest=None):
    d = {"concept": concept, "annual": [rec_(e, v, "FY", "10-K") for e, v in annual], "quarterly": [rec_(e, v) for e, v in quarterly]}
    if latest:
        d["latest"] = {"end": latest[0], "val": latest[1]}
    return d


# ---------------------------------------------------------------------------------------------- fetch_sec v4 ---
def test_facts_v4_revenue_total_first_and_new_items():
    def pt(start, end, val, form="10-Q", filed=None):
        return {"start": start, "end": end, "val": val, "form": form, "filed": filed or end, "fy": 2026, "fp": "Q"}
    us = {
        "Revenues": {"units": {"USD": [pt("2026-01-01", "2026-03-31", 1000), pt("2025-01-01", "2025-12-31", 3800, "10-K")]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            pt("2026-01-01", "2026-03-31", 40), pt("2025-01-01", "2025-12-31", 150, "10-K"), pt("2026-04-01", "2026-06-30", 1100)]}},
        "CommonStockDividendsPerShareDeclared": {"units": {"USD/shares": [
            pt("2026-01-01", "2026-03-31", 0.25), pt("2026-01-01", "2026-06-30", 0.52), pt("2026-01-01", "2026-09-30", 0.80)]}},
        "LongTermDebtCurrent": {"units": {"USD": [{"end": "2026-09-30", "val": 5e8, "form": "10-Q", "filed": "2026-10-30", "fy": 2026, "fp": "Q3"}]}},
        "PreferredStockValue": {"units": {"USD": [{"end": "2026-09-30", "val": 2e9, "form": "10-Q", "filed": "2026-10-30", "fy": 2026, "fp": "Q3"}]}},
    }
    f = fs.facts_from({"facts": {"us-gaap": us}})
    q = {x["end"]: x["val"] for x in f["revenue"]["quarterly"]}
    assert q["2026-03-31"] == 1000 and q["2026-06-30"] == 1100, q          # the total wins its period; a contract-only period stays
    assert f["revenue"]["annual"][-1]["val"] == 3800
    dq = {x["end"]: round(x["val"], 2) for x in f["dps"]["quarterly"]}
    assert dq == {"2026-03-31": 0.25, "2026-06-30": 0.27, "2026-09-30": 0.28}, dq   # derived from the year-to-date figures
    assert f["debtCurrent"]["latest"]["val"] == 5e8 and f["preferred"]["latest"]["val"] == 2e9
    assert fs.FACTS_VERSION == 4 and fs.KEEP["dps"] == (3, 7) and fs.KEEP["debtCurrent"] == (2, 3) and "da" in fs.FLOW_USD


# ------------------------------------------------------------------------------------------------------- TTM ---
def test_ttm_constructs():
    fy_only = item(annual=[("2025-12-31", 100)], quarterly=[("2025-09-30", 30)])
    assert fd.ttm(fy_only) == (100, "2025-12-31", "FY")
    chained = item(annual=[("2025-12-31", 100)],
                   quarterly=[("2025-03-31", 20), ("2025-06-30", 25), ("2025-09-30", 30), ("2026-03-31", 22), ("2026-06-30", 28)])
    assert fd.ttm(chained) == (100 + 22 + 28 - 20 - 25, "2026-06-30", "FY+2Q")
    four = item(quarterly=[("2025-12-31", 1), ("2026-03-31", 2), ("2026-06-30", 3), ("2026-09-30", 4)])
    assert fd.ttm(four) == (10, "2026-09-30", "4Q")
    gap = item(annual=[("2024-12-31", 90)], quarterly=[("2025-03-31", 1), ("2025-09-30", 3), ("2026-03-31", 2), ("2026-09-30", 4)])
    assert fd.ttm(gap) is None                               # a missing 10-K: no current TTM, not a 2024 year passed off as today
    near = item(annual=[("2025-12-31", 100)], quarterly=[("2026-03-31", 26)])     # its year-earlier quarter is missing
    assert fd.ttm(near) == (100, "2025-12-31", "FY~")
    wk53 = item(annual=[("2025-09-27", 400)], quarterly=[("2024-12-28", 90), ("2025-12-27", 110)])   # 52/53-week years
    assert fd.ttm(wk53) == (420, "2025-12-27", "FY+1Q")


def test_split_rebasing_and_not_an_acquisition():
    # a 4-for-1 split between the 10-K (FY 2025, pre-split) and the 2026 10-Qs (which restate their comparatives)
    eps = item(annual=[("2025-12-31", 8.0)],
               quarterly=[("2025-03-31", 0.5), ("2025-06-30", 0.5), ("2025-09-30", 0.5), ("2026-03-31", 0.6), ("2026-06-30", 0.6),
                          ("2026-09-30", 0.6)])
    sh = item(annual=[("2025-12-31", 100e6)],
              quarterly=[("2025-03-31", 400e6), ("2025-06-30", 400e6), ("2025-09-30", 400e6), ("2026-03-31", 400e6),
                         ("2026-06-30", 400e6), ("2026-09-30", 400e6)])
    rebase, hits, unclear = fd.share_rebaser({"dilutedShares": sh})
    v, end, how = fd.ttm(eps, rebase)
    assert abs(v - (2.0 + 1.8 - 1.5)) < 1e-9 and hits == {"2025-12-31": 4.0} and not unclear
    # an acquisition paid in shares: the count steps up 1.5x through the year - the year agrees with its own quarters
    sh2 = item(annual=[("2025-12-31", 125e6)], quarterly=[("2025-03-31", 100e6), ("2025-06-30", 110e6), ("2025-09-30", 140e6),
                                                         ("2026-03-31", 150e6)])
    rebase, hits, unclear = fd.share_rebaser({"dilutedShares": sh2})
    assert rebase({"end": "2025-12-31"}, "a") is None and not hits and not unclear
    # a 1-for-10 reverse split between two quarters, and a count that jumps by no split at all
    sh3 = item(quarterly=[("2026-03-31", 500e6), ("2026-06-30", 50e6)])
    rebase, hits, unclear = fd.share_rebaser({"dilutedShares": sh3})
    assert abs(rebase({"end": "2026-03-31"}, "q") - 0.1) < 1e-12 and not unclear
    # a re-based EPS counts only where Yahoo confirms it (an Up-C exchange or a merger can look like a split)
    split = facts(epsDiluted=eps, dilutedShares=sh)
    r = fd.company({"facts": split, "factsV": 4}, master(eps=None), today=TODAY)
    assert any(f.startswith("split:") for f in r["q"]) and r["use"]["eps"] is None and "pe" not in r["m"]
    r = fd.company({"facts": split, "factsV": 4}, master(eps=2.3), today=TODAY)
    assert r["y"]["eps"][2] == "agree" and r["use"]["eps"] == "sec"                 # 2.0 + 1.8 - 1.5 = 2.30, as Yahoo has it
    sh4 = item(quarterly=[("2026-03-31", 100e6), ("2026-06-30", 500e6), ("2026-09-30", 125e6)])
    assert fd.share_rebaser({"dilutedShares": sh4})[2] is True          # x5 then /4: no clean basis
    assert fd.split_factor(25.3) == 25 and fd.split_factor(0.1) == 0.1 and fd.split_factor(1.5) is None


# ------------------------------------------------------------------------------------------------ a company ---
def flow(fy, prior, now, concept="X"):
    """A flow item: FY 2025 = fy, the quarters of 2025 = prior, the quarters of 2026 filed so far = now.
    Its TTM = fy + sum(now) - sum(prior[:len(now)])."""
    qs = list(zip(("2025-03-31", "2025-06-30", "2025-09-30"), prior)) + list(zip(("2026-03-31", "2026-06-30", "2026-09-30"), now))
    return item(annual=[("2025-12-31", fy)], quarterly=qs, concept=concept)


def facts(**over):
    f = {"revenue": flow(400e9, (95e9, 100e9, 105e9), (100e9, 103e9, 107e9), "Revenues"),           # TTM 410 B
         "netIncome": flow(100e9, (24e9, 25e9, 26e9), (26e9, 27e9, 27e9)),                          # 105 B
         "epsDiluted": flow(10.0, (2.4, 2.5, 2.6), (2.6, 2.7, 2.7)),                                # 10.50
         "operatingIncome": flow(120e9, (29e9, 30e9, 31e9), (30e9, 32e9, 34e9)),                    # 126 B
         "da": flow(20e9, (5e9, 5e9, 5e9), (5.5e9, 5.5e9, 5.5e9)),                                  # 21.5 B -> EBITDA 147.5 B
         "operatingCashFlow": flow(110e9, (26e9, 27e9, 28e9), (28e9, 29e9, 30e9)),                  # 116 B
         "capex": flow(10e9, (2e9, 2e9, 3e9), (3e9, 3e9, 3e9)),                                     # 12 B -> FCF 104 B
         "equity": item(latest=("2026-09-30", 300e9)), "preferred": item(latest=("2026-09-30", 20e9)),
         "cash": item(latest=("2026-09-30", 30e9)), "stInvest": item(latest=("2026-09-30", 20e9)),
         "longTermDebt": item(latest=("2026-09-30", 80e9), concept="LongTermDebtNoncurrent"),
         "debtCurrent": item(latest=("2026-09-30", 10e9)), "shortDebt": item(latest=("2026-09-30", 5e9)),
         "opLease": item(latest=("2026-09-30", 7e9)), "finLease": item(latest=("2026-09-30", 1e9))}
    f.update(over)
    return f


def master(**over):
    m = {"price": 200.0, "date": "2026-11-20", "marketcap": 2e12, "financialCurrency": "USD", "sector": "Technology",
         "eps": 10.5, "revenueUSD": 410e9, "revenueAsOf": "2026-09-30", "netIncome": 105e9, "bookUSD": 280e9, "bookAsOf": "2026-09-30",
         "totalDebt": 103e9, "cash": 50e9, "dividend_yield": None}
    m.update(over)
    return m


def test_company_ttm_balance_sheet_multiples():
    r = fd.company({"facts": facts(), "factsV": 4}, master(), today=TODAY)
    t, b, m = r["t"], r["b"], r["m"]
    assert t["rev"] == 410e9 and t["eps"] == 10.5 and t["ebitda"] == 147.5e9 and t["fcf"] == 104e9 and r["end"] == "2026-09-30", t
    assert r["how"]["rev"] == "FY+3Q" and t["ni"] == 105e9 and t["oi"] == 126e9
    assert b["eq"] == 280e9 and b["eqT"] == 300e9 and b["cashT"] == 50e9
    assert b["debt"] == 80e9 + 10e9 + 5e9 + 1e9                        # non-current + current + short-term + finance leases
    assert r["y"]["eps"][2] == "agree" and r["y"]["rev"][2] == "agree" and r["y"]["eq"][2] == "agree" and r["y"]["debt"][2] == "agree"
    assert r["use"] == {"eps": "sec", "rev": "sec", "ni": "sec", "ebitda": "sec", "eq": "sec", "debt": "sec", "cash": "sec"}, r["use"]
    assert abs(m["pe"] - 200 / 10.5) < 1e-3 and abs(m["ps"] - 2e12 / 410e9) < 1e-3 and abs(m["pb"] - 2e12 / 280e9) < 1e-3
    assert m["ev"] == fd.sig(2e12 + 96e9 - 50e9, 5) and abs(m["eve"] - (2e12 + 46e9) / 147.5e9) < 1e-3
    assert abs(m["fcfy"] - 104e9 / 2e12) < 1e-4 and abs(m["roe"] - 105e9 / 280e9) < 1e-4 and abs(m["om"] - 126 / 410) < 1e-4
    # "LongTermDebt" already holds the current maturities; a "LongTermDebtAndCapitalLeaseObligations" line holds the leases
    r2 = fd.company({"facts": facts(longTermDebt=item(latest=("2026-09-30", 90e9), concept="LongTermDebt")), "factsV": 4}, master(), today=TODAY)
    assert r2["b"]["debt"] == 90e9 + 5e9 + 1e9
    r3 = fd.company({"facts": facts(longTermDebt=item(latest=("2026-09-30", 81e9), concept="LongTermDebtAndCapitalLeaseObligations")),
                     "factsV": 4}, master(), today=TODAY)
    assert r3["b"]["debt"] == 81e9 + 10e9 + 5e9
    # Yahoo's total debt with the operating leases: compared on that definition, and named
    r4 = fd.company({"facts": facts(), "factsV": 4}, master(totalDebt=103e9), today=TODAY)
    assert r4["y"]["debt"][2] == "agree" and r4["y"]["debt"][3] == "incl. operating leases"


def test_check_bands_periods_and_picks():
    base = {"facts": facts(), "factsV": 4}
    r = fd.company(base, master(eps=10.0, revenueUSD=300e9), today=TODAY)
    assert r["y"]["eps"][2] == "near" and r["use"]["eps"] == "sec"
    assert r["y"]["rev"][2] == "check" and r["use"]["rev"] is None                   # same period, 37 % apart: disputed
    assert "ps" not in r["m"] and "pe" in r["m"]                                    # no multiple on a disputed denominator
    r = fd.company(base, master(revenueAsOf="2026-12-31", eps=11.0), today=TODAY)
    assert r["y"]["rev"][2] == "older" and r["use"]["rev"] == "yahoo"              # Yahoo has a later quarter
    r = fd.company(base, master(revenueAsOf="2026-06-30"), today=TODAY)
    assert r["y"]["rev"][2] == "newer" and r["use"]["rev"] == "sec"                # ours is the later filing
    r = fd.company(base, master(eps=None), today=TODAY)
    assert r["y"]["eps"][2] == "only" and r["use"]["eps"] == "sec"
    r = fd.company(base, master(eps=10.48), today=TODAY)
    assert r["y"]["eps"][2] == "agree"                                             # within two cents
    r = fd.company({"facts": facts(epsDiluted=flow(0.05, (0.01, 0.01, 0.01), (0.01, 0.0, 0.0))), "factsV": 4}, master(eps=-0.06), today=TODAY)
    assert r["y"]["eps"][2] == "check"                                             # a profit here, a loss there


def test_transitional_rules_before_v4():
    r = fd.company({"facts": facts(), "factsV": 3}, master(totalDebt=50e9), today=TODAY)
    assert r["y"]["debt"][2] == "partial" and r["use"]["debt"] == "yahoo" and r["use"]["cash"] == "yahoo"
    assert "ev" not in r["m"]
    comp = facts(revenue=flow(40e9, (10e9, 10e9, 10e9), (10e9, 10e9, 11e9), "RevenueFromContractWithCustomerExcludingAssessedTax"))
    r = fd.company({"facts": comp, "factsV": 2}, master(), today=TODAY)
    assert r["use"]["rev"] == "yahoo" and "component?" in r["y"]["rev"]            # one component of a REIT's revenue
    pref = facts(preferred=item())
    r = fd.company({"facts": pref, "factsV": 3}, master(bookUSD=250e9), today=TODAY)
    assert r["y"]["eq"][2] == "check" and r["use"]["eq"] == "yahoo"                # ours still holds the preferred stock
    r = fd.company({"facts": facts(), "factsV": 1}, master(revenueUSD=300e9), today=TODAY)
    assert r["use"]["rev"] == "yahoo"                                              # a pre-z187 doc disagreeing: ours is the suspect


def test_secondary_listings_and_funds():
    docs = {"DTE": {"cik": 936340, "facts": facts(), "factsV": 4}, "DTB": {"cik": 936340, "facts": facts(), "factsV": 4},
            "UVXY": {"cik": 1, "facts": facts(), "factsV": 4}}
    mast = {"DTE": master(), "DTB": master(price=14.5, marketcap=None, eps=None, revenueUSD=None, bookUSD=None),
            "UVXY": master(isetf="true")}
    assert fd.primaries(docs, mast) == {"DTB"}
    doc = fd.build(docs, mast, today=TODAY)
    assert "UVXY" not in doc["tickers"]
    dtb = doc["tickers"]["DTB"]
    assert "class?" in dtb["q"] and "no-cap" in dtb["q"] and dtb["use"]["eps"] is None and "pe" not in dtb["m"]
    assert doc["tickers"]["DTE"]["use"]["eps"] == "sec" and doc["counts"]["secondary"] == 1


def test_file_written_once_from_disk():
    with tempfile.TemporaryDirectory() as tmp:
        sec = Path(tmp) / "sec"
        sec.mkdir()
        (sec / "AAA.json").write_text(json.dumps({"ticker": "AAA", "cik": 5, "facts": facts(), "factsV": 4}))
        (sec / "index.json").write_text("{}")
        (sec / "BAD.json").write_text("{nope")
        books = Path(tmp) / "books"
        for b in fd.BOOKS:
            (books / b / "data").mkdir(parents=True)
            (books / b / "data" / "master.json").write_text("[]")
        (books / "TRAPP2" / "data" / "master.json").write_text(json.dumps([dict(master(), ticker="aaa", asset_class="Equity")]))
        real = (fd.SEC_DIR, fd.OUT, fd.SHARES, os.environ.get("DERIVED_BOOKS"))
        fd.SEC_DIR, fd.OUT, fd.SHARES = sec, Path(tmp) / "out" / "fundamentals.json", Path(tmp) / "none.json"
        os.environ["DERIVED_BOOKS"] = str(books)
        try:
            assert fd.main() == 0 and fd.OUT.exists()
            d = json.loads(fd.OUT.read_text())
            assert d["build"] == fd.BUILD and list(d["tickers"]) == ["AAA"] and d["counts"]["computed"] == 1
            assert d["tickers"]["AAA"]["mc"] == [2e12, "book"] and d["checks"]["eps"]["agree"] == 1
            m0 = fd.OUT.stat().st_mtime_ns
            os.environ["FUND_NOW"] = "2026-11-22T14:40:00Z"
            assert fd.main() == 0 and fd.OUT.stat().st_mtime_ns == m0                  # the same figures: not rewritten
            fd.SHARES.write_text(json.dumps({"tickers": {"AAA": {"s": 9.5e9}}}))
            assert fd.main() == 0 and json.loads(fd.OUT.read_text())["tickers"]["AAA"]["mc"] == [1.9e12, "sec"]   # price x the SEC count
        finally:
            os.environ["FUND_NOW"] = "2026-11-22T03:10:00Z"
            fd.SEC_DIR, fd.OUT, fd.SHARES = real[0], real[1], real[2]
            if real[3] is None:
                os.environ.pop("DERIVED_BOOKS", None)
            else:
                os.environ["DERIVED_BOOKS"] = real[3]


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
