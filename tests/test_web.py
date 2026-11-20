# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_web.py
#  z197 - the Business Web build (pipeline/web_build.py) and the SEC facts queue (pipeline/fetch_sec.py).
#  z199 - ownership from the filings: filing_edges (corporate 5 %+ stakes in, institutions' client money out, curated
#         edges confirmed, one ticker per CIK), sec_owners, nodes' own / sic / formerNames.
#  Offline: python -m pytest tests/test_web.py -q
# ============================================================
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
os.environ["WEB_OFFLINE"] = "1"

import web_build as wb  # noqa: E402
import fetch_sec as fs  # noqa: E402

TODAY = date(2026, 11, 19)


def E(i, typ, a, b, **kw):
    return dict({"id": i, "type": typ, "from": a, "to": b}, **kw)


# ---- edges, flows, loops --------------------------------------------------------------------------------------------
def test_flow_directions():
    assert wb.flow(E("1", "owns", "GOOGL", "ANT")) == ("GOOGL", "ANT")
    assert wb.flow(E("2", "owns", "OPENAI", "AMD", instrument="warrant")) == ("AMD", "OPENAI")   # equity handed to the holder
    assert wb.flow(E("3", "commits", "ANT", "GOOGL")) == ("ANT", "GOOGL")
    assert wb.flow(E("4", "supplies", "CRWV", "MSFT")) == ("MSFT", "CRWV")                     # the customer pays the supplier
    assert wb.flow(E("5", "underwrites", "MS", "ANT")) == ("ANT", "MS")                        # the issuer pays the fees


def test_two_cycle_found_once():
    edges = [E("a", "owns", "GOOGL", "ANT"), E("b", "commits", "ANT", "GOOGL")]
    loops = wb.find_loops(edges)
    assert len(loops) == 1
    assert loops[0]["path"][0] == loops[0]["path"][-1]
    assert sorted(loops[0]["edges"]) == ["a", "b"]


def test_parallel_edges_are_separate_loops_not_duplicates():
    # owns AND funds back to the same buyer: two distinct money paths, each listed once
    edges = [E("a", "owns", "GOOGL", "ANT"), E("f", "funds", "GOOGL", "ANT"), E("b", "commits", "ANT", "GOOGL")]
    loops = wb.find_loops(edges)
    assert len(loops) == 2
    assert len({tuple(sorted(L["edges"])) for L in loops}) == 2


def test_four_cycle_and_length_cap():
    edges = [E("1", "commits", "ANT", "MSFT"), E("2", "owns", "MSFT", "OPENAI"), E("3", "owns", "NVDA", "OPENAI"),
             E("4", "commits", "OPENAI", "NVDA"), E("5", "owns", "NVDA", "ANT"), E("6", "commits", "OPENAI", "MSFT")]
    loops = wb.find_loops(edges, max_len=4)
    paths = [tuple(L["path"]) for L in loops]
    assert any(len(p) == 5 for p in paths)                     # A -> B -> C -> D -> A
    assert all(len(p) <= 5 for p in paths)
    assert not wb.find_loops(edges, max_len=2) or all(len(L["path"]) == 3 for L in wb.find_loops(edges, max_len=2))


def test_no_loop_without_return_flow():
    assert wb.find_loops([E("a", "owns", "GOOGL", "ANT"), E("b", "commits", "ANT", "AMZN")]) == []


# ---- circularity ----------------------------------------------------------------------------------------------------
def test_circularity_counts_commitments_once_per_pair():
    edges = [E("a", "owns", "GOOGL", "ANT", amountUSD=20e9), E("f", "funds", "GOOGL", "ANT", amountUSD=20e9),
             E("b", "commits", "ANT", "GOOGL", amountUSD=200e9, gw=5)]
    facts = {"GOOGL": {"rev": 400e9}}
    c = wb.circularity(edges, wb.find_loops(edges), facts)["GOOGL"]
    assert c["investedUSD"] == 40e9
    assert c["committedBackUSD"] == 200e9                      # not 400 (z197 dry-run double count)
    assert c["committedBackGW"] == 5
    assert c["committedBackToRevenue"] == 0.5
    assert len(c["fundedBuyers"]) == 1 and c["fundedBuyers"][0]["how"] == "funds + owns"


def test_circularity_warrant_to_customer():
    edges = [E("w", "owns", "OPENAI", "AMD", instrument="warrant", pct=10), E("c", "commits", "OPENAI", "AMD", gw=6)]
    c = wb.circularity(edges, wb.find_loops(edges), {})
    assert "AMD" in c and c["AMD"]["committedBackGW"] == 6
    assert c["AMD"]["fundedBuyers"][0]["how"].startswith("warrant")
    assert "OPENAI" not in c                                   # the holder doesn't fund the issuer


def test_circularity_ratio_blank_without_current_revenue():
    edges = [E("a", "owns", "MSFT", "OPENAI"), E("b", "commits", "OPENAI", "MSFT", amountUSD=250e9)]
    c = wb.circularity(edges, wb.find_loops(edges), {"MSFT": {"cash": 1}})["MSFT"]
    assert c["committedBackUSD"] == 250e9 and c["committedBackToRevenue"] is None and c["revenueUSD"] is None


# ---- SEC facts -------------------------------------------------------------------------------------------------------
def test_facts_record_drops_stale_line_items_individually():
    doc = {"ticker": "MSFT", "facts": {
        "revenue": {"annual": [{"end": "2010-06-30", "val": 62.5e9}]},                       # pre-z187 'Revenues' series
        "cash": {"latest": {"end": "2026-09-30", "val": 30e9}, "annual": [{"end": "2026-06-30", "val": 28e9}]}}}
    r = wb.facts_record(doc, TODAY)
    assert "rev" not in r and r["cash"] == 30e9
    assert r["staleKeys"] == ["rev"] and r["stale"] is True and r["v"] == 1 and r["fy"] == "2026-09-30"


def test_facts_record_prefers_ttm():
    doc = {"factsV": 2, "facts": {"revenue": {"ttm": {"end": "2026-09-30", "val": 450e9},
                                              "annual": [{"end": "2025-12-31", "val": 402.8e9}]}}}
    r = wb.facts_record(doc, TODAY)
    assert r["rev"] == 450e9 and r["v"] == 2 and "stale" not in r


def test_sec_facts_reads_a_directory():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "NVDA.json").write_text(json.dumps({"ticker": "NVDA", "facts": {"revenue": {"annual": [{"end": "2026-01-25", "val": 215.9e9}]}}}))
        Path(d, "index.json").write_text("{}")
        Path(d, "BAD.json").write_text("{not json")
        f = wb.sec_facts(d, TODAY)
    assert list(f) == ["NVDA"] and f["NVDA"]["rev"] == 215.9e9


def test_isetf_string_false_is_not_a_fund():
    b = wb.books_from({"TRAPP2": [{"ticker": "NVDA", "isetf": "false"}, {"ticker": "SPY", "isetf": "true"}],
                       "TRAPP2-1": [{"ticker": "NVDA", "isetf": "true"}, {"ticker": "TLT", "isetf": True}]})
    assert b["NVDA"]["isEtf"] is False and b["NVDA"]["book"] == "TRAPP2"   # first book wins
    assert b["SPY"]["isEtf"] is True and b["TLT"]["isEtf"] is True


# ---- IPO calendar ---------------------------------------------------------------------------------------------------
def test_ipo_events_window_and_order():
    cfg = {"ipos": {"ANT": {"name": "Anthropic", "events": [
        {"date": "2027-02-03", "kind": "lock-up", "major": True}, {"date": "2026-11-29", "kind": "quiet-period"},
        {"date": "2026-11-01", "kind": "old"}, {"date": "2028-06-01", "kind": "too far"}, {"rule": "Q3 results", "kind": "lock-up"}]}}}
    ev = wb.ipo_events(cfg, TODAY)
    kinds = [e["kind"] for e in ev]
    assert kinds[:2] == ["quiet-period", "lock-up"] and "old" not in kinds and "too far" not in kinds
    assert ev[0]["daysAway"] == 10 and ev[-1]["daysAway"] is None and ev[-1]["ticker"] == "ANT"


# ---- chains + the shipped configs -----------------------------------------------------------------------------------
def test_chain_index_and_shipped_configs():
    chains = json.loads((ROOT / "config" / "supply_chains.json").read_text())["chains"]
    ids = [c["id"] for c in chains]
    assert len(ids) == len(set(ids)) and len(chains) >= 40
    assert sum(1 for c in chains if c.get("level") == "subsector") >= 16
    for c in chains:
        for key, _ in wb.STAGES:
            for m in c.get(key) or []:
                assert (m.get("ticker") or m.get("name")) and m.get("confidence") in ("filing", "disclosed", "reported", "inferred"), (c["id"], m)
    idx = wb.chain_index(chains)
    assert "NVDA" in idx and any(r[1] == "mid" or r[1] == "down" for r in idx["NVDA"])
    rel = json.loads((ROOT / "config" / "web_relationships.json").read_text())
    src = rel["sources"]
    seen = set()
    for e in rel["edges"]:
        assert e["id"] not in seen
        seen.add(e["id"])
        assert e["type"] in rel["types"], e
        assert e.get("confidence") in ("filing", "disclosed", "reported", "inferred"), e
        for ev in e.get("events") or []:
            assert not ev.get("src") or ev["src"] in src, (e["id"], ev.get("src"))
    ipo = json.loads((ROOT / "config" / "ipo_watch.json").read_text())
    for t, v in ipo["ipos"].items():
        for ev in v.get("events") or []:
            assert ev.get("date") or ev.get("rule"), (t, ev)


def test_build_offline_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "GOOGL.json").write_text(json.dumps({"ticker": "GOOGL", "factsV": 2, "facts": {"revenue": {"ttm": {"end": "2026-09-30", "val": 450e9}}}}))
        books = wb.books_from({"TRAPP2": [{"ticker": "GOOGL", "name": "Alphabet", "isetf": "false", "marketcap": "3.5e12"},
                                          {"ticker": "SPY", "isetf": "true"}]})
        out = wb.build(TODAY, books, d, {"indices": {"SPY": {"members": [{"ticker": "GOOGL", "weight": 0.04}]}}}, None)
    n = out["nodes"]["nodes"]
    assert n["GOOGL"]["kind"] == "public" and n["SPY"]["kind"] == "fund" and n["GOOGL"]["idx"]["SPY"] == 4.0
    assert n["GOOGL"]["facts"]["rev"] == 450e9
    assert n["GOOGL"]["circularity"]["committedBackUSD"] and n["GOOGL"]["circularity"]["loops"] >= 1
    assert n["ANT"]["kind"] in ("public", "outside the books", "private") or n["ANT"]["name"]
    assert out["money"]["loops"] and out["stats"]["moneyEdges"] == len(out["money"]["edges"])
    assert all(len(json.dumps(out[k], separators=(",", ":"), default=str)) < 2_000_000 for k in ("money", "chains", "ipo"))


# ---- fetch_sec facts queue ------------------------------------------------------------------------------------------
def test_facts_queue_web_names_first_then_oldest_and_failures_rotate():
    old = {"AAA": {"factsAt": "2026-11-01T00:00:00Z"},                 # v1, old
           "MSFT": {"factsAt": "2026-11-05T00:00:00Z"},                # v1, web name
           "SPY": {},                                                  # never read (an ETF that 404s)
           "ZZZ": {"factsV": fs.FACTS_VERSION, "factsAt": "2026-10-01T00:00:00Z"},    # already current - after every older doc
           "NEW": {"factsV": fs.FACTS_VERSION, "factsAt": "2026-11-18T00:00:00Z", "factsErr": "HTTPError: 404"}}
    q = sorted(fs.facts_queue(list(old), old, {"MSFT"}, 1))
    assert q == ["MSFT"]
    q3 = fs.facts_queue(list(old), old, {"MSFT"}, 3)
    assert q3 == {"MSFT", "SPY", "AAA"}
    q4 = fs.facts_queue(list(old), old, set(), 4)
    assert "ZZZ" in q4 and "NEW" not in q4                    # a stamped failure waits its turn


def test_facts_from_money_structure_items():
    """z198: RPO, purchase obligations and equity stakes come through as instants with a 'latest', a year of trend."""
    def pt(end, val, form="10-Q"):
        return {"end": end, "val": val, "form": form, "filed": end, "fy": int(end[:4]), "fp": "Q3"}
    us = {"RevenueRemainingPerformanceObligation": {"units": {"USD": [pt(f"20{y}-{m:02d}-30", 100e9 + y * 1e9 + m) for y in range(20, 27) for m in (3, 6, 9)]
                                                               + [pt("2025-12-31", 400e9, "10-K")]}},
          "EquitySecuritiesWithoutReadilyDeterminableFairValueAmount": {"units": {"USD": [pt("2026-09-30", 37e9), pt("2025-12-31", 20e9, "10-K")]}},
          "PurchaseObligation": {"units": {"USD": [pt("2026-09-30", 50e9)]}}}
    f = fs.facts_from({"facts": {"us-gaap": us}})
    assert f["rpo"]["latest"]["end"] == "2026-09-30" and len(f["rpo"]["quarterly"]) == 4 and len(f["rpo"]["annual"]) <= 4
    assert f["equityPrivate"]["latest"]["val"] == 37e9 and f["purchaseObligations"]["latest"]["val"] == 50e9
    assert fs.FACTS_VERSION >= 3 and "rpo" in fs.INSTANT


def test_backlog_view_tracked_commitments_vs_rpo():
    edges = [E("o", "commits", "OPENAI", "ORCL", amountUSD=300e9, gw=4.5), E("m", "commits", "META", "ORCL", amountUSD=20e9),
             E("x", "owns", "NVDA", "OPENAI", amountUSD=30e9)]
    b = wb.backlog_view(edges, {"ORCL": {"rpo": 455e9, "rev": 60e9}})
    assert b["ORCL"]["commitIn"] == 320e9 and b["ORCL"]["shareOfRpo"] == round(320 / 455, 3) and b["ORCL"]["yearsOfRevenue"] == round(320 / 60, 2)
    assert {x["buyer"] for x in b["ORCL"]["from"]} == {"OPENAI", "META"} and "NVDA" not in b
    nb = wb.backlog_view(edges, {})
    assert nb["ORCL"]["shareOfRpo"] is None and nb["ORCL"]["rpo"] is None      # no RPO on file: the $ stand alone


# ---- z199: ownership from the filings (Schedule 13D / 13G) ------------------------------------------------------------
def H(name, pct, cik, kind="13G", typ="CO", **kw):
    return dict({"name": name, "pct": pct, "cik": cik, "kind": kind, "type": typ, "filed": "2026-10-01", "event": "2026-09-30",
                 "acc": f"acc-{name}-{pct}"}, **kw)


BOOKS199 = {"NVDA": {"industry": "Semiconductors"}, "BLK": {"industry": "Asset Management"}, "KKR": {"industry": "Asset Management"},
            "BRK-B": {"industry": "Insurance - Diversified"}, "CRWV": {"industry": "Software - Infrastructure"},
            "GOOG": {"industry": "Internet Content & Information"}, "GOOGL": {"industry": "Internet Content & Information"},
            "XYZ": {"industry": "Specialty Retail"}, "JPM": {"industry": "Banks - Diversified"}}
CIKS199 = {"NVDA": 1045810, "BLK": 1364742, "KKR": 1404912, "BRK-B": 1067983, "CRWV": 1769628, "GOOG": 1652044, "GOOGL": 1652044,
           "XYZ": 777, "JPM": 19617}


def test_pick_by_cik_prefers_curated_then_books():
    m = wb._pick_by_cik({"GOOG": 1, "GOOGL": 1, "BRK-A": 2, "BRK-B": 2, "ZZ": 3}, curated={"GOOGL"}, books={"BRK-B", "GOOG", "GOOGL"})
    assert m == {1: "GOOGL", 2: "BRK-B", 3: "ZZ"}
    assert wb._pick_by_cik({"GOOG": 1, "GOOGL": 1})[1] == "GOOG"                    # nothing to prefer: the first


def test_filing_edges_corporate_kept_institutions_out_curated_confirmed():
    holders = {"CRWV": [H("NVIDIA CORPORATION", 6.6, 1045810),                                   # confirms the curated edge
                        H("BLACKROCK, INC.", 7.0, 1364742, typ="HC", rule="13d-1(b)"),            # client money: out
                        H("KKR GROUP PARTNERSHIP L.P.", 12.0, 1404912, typ="PN", rule="13d-1(d)"),  # a sponsor's pre-IPO stake: in
                        H("VANGUARD GROUP INC", 9.0, 102909, typ="IA"),                           # not a company we list: no edge
                        H("JPMORGAN CHASE & CO", 5.2, 19617, typ="HC/BK")],                       # a bank's client holdings: out
               "GOOG": [H("BERKSHIRE HATHAWAY INC", 5.5, 1067983, typ="HC/CO")],
               "GOOGL": [H("BERKSHIRE HATHAWAY INC", 5.5, 1067983, typ="HC/CO")],                 # same company: one edge
               "XYZ": [H("BLACKROCK, INC.", 10.0, 1364742, kind="13D", typ="HC", purpose="seek board seats"),   # 13D: in
                       H("NVIDIA CORPORATION", 4.1, 1045810)],                                    # below 5 %: no edge
               "NVDA": [H("NVIDIA CORPORATION", 9.0, 1045810)]}                                   # itself (a plan / treasury): no
    curated = [E("c1", "owns", "NVDA", "CRWV", confidence="disclosed", events=[]), E("c2", "owns", "GOOGL", "ANT")]
    new, confirmed = wb.filing_edges(holders, CIKS199, curated, BOOKS199, {"NVDA", "CRWV", "GOOGL", "ANT"})
    got = {(e["from"], e["to"]): e for e in new}
    assert set(got) == {("KKR", "CRWV"), ("BRK-B", "GOOGL"), ("BLK", "XYZ")}, got.keys()
    assert confirmed == 1 and curated[0]["pctFiled"] == 6.6 and curated[0]["events"][-1]["acc"] == "acc-NVIDIA CORPORATION-6.6"
    assert curated[0]["holder"] == "NVIDIA CORPORATION" and curated[0]["confidence"] == "disclosed"     # curated stays curated
    k = got[("KKR", "CRWV")]
    assert k["confidence"] == "filing" and k["pct"] == 12.0 and k["id"] == "F-KKR-CRWV" and k["kind13"] == "13G"
    assert got[("BLK", "XYZ")]["purpose"] == "seek board seats" and got[("BLK", "XYZ")]["holder"] == "BLACKROCK, INC."
    # run twice on the same curated list: the confirmation is not appended again
    wb.filing_edges(holders, CIKS199, curated, BOOKS199, {"NVDA", "CRWV", "GOOGL", "ANT"})
    assert len(curated[0]["events"]) == 1


def test_passive_rule_beats_type_codes():
    assert wb._passive({"rule": "13d-1(b)", "type": "CO"}) is True              # a qualified institution, whatever the code
    assert wb._passive({"rule": "13d-1(d)", "type": "HC"}) is False             # an exempt (pre-IPO) holder holds its own
    assert wb._passive({"type": "IA"}) is True and wb._passive({"type": "PN"}) is False and wb._passive({}) is True


def test_filing_edges_from_the_owners_side_and_latest_wins():
    """The stake read from the owner's own filing list (NVIDIA's doc) makes the edge before CoreWeave's doc is read; when
    both sides are on file the latest filing wins; a curated edge records an exit below 5 % rather than dropping it."""
    stakes = {"NVDA": [dict(H("NVIDIA Corporation", 7.0, None, filed="2025-05-15"), issuerCik=1769628)]}
    new, _ = wb.filing_edges({}, CIKS199, [], BOOKS199, set(), stakes)
    assert [(e["from"], e["to"], e["pct"]) for e in new] == [("NVDA", "CRWV", 7.0)]
    holders = {"CRWV": [H("NVIDIA Corporation", 6.6, 1045810, filed="2026-10-02")]}
    new, _ = wb.filing_edges(holders, CIKS199, [], BOOKS199, set(), stakes)
    assert [(e["from"], e["to"], e["pct"]) for e in new] == [("NVDA", "CRWV", 6.6)]          # one edge, the latest filing
    curated = [E("c1", "owns", "NVDA", "CRWV", events=[])]
    exit_ = {"CRWV": [H("NVIDIA Corporation", 3.9, 1045810, filed="2026-11-14")]}
    new, conf = wb.filing_edges(exit_, CIKS199, curated, BOOKS199, {"NVDA", "CRWV"}, stakes)
    assert new == [] and conf == 1 and curated[0]["pctFiled"] == 3.9


def test_filing_edges_feed_loops():
    """A filed stake closes a loop with a curated commitment back - the circular-financing check sees filed ownership."""
    holders = {"CRWV": [H("NVIDIA CORPORATION", 6.6, 1045810)]}
    edges = [E("k", "commits", "CRWV", "NVDA", amountUSD=6.3e9)]
    new, _ = wb.filing_edges(holders, CIKS199, edges, BOOKS199, set())
    loops = wb.find_loops(edges + new)
    assert len(loops) == 1 and set(loops[0]["edges"]) == {"k", "F-NVDA-CRWV"}


def test_sec_owners_reads_holders_profiles_and_ciks():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "CRWV.json").write_text(json.dumps({"ticker": "CRWV", "cik": 1769628, "holders": [H("NVIDIA CORPORATION", 6.6, 1045810)],
                                                    "profile": {"sic": "7374", "sicDescription": "Services-Computer Processing"}}))
        Path(d, "NOCIK.json").write_text(json.dumps({"ticker": "NOCIK", "cik": "n/a"}))
        Path(d, "index.json").write_text("{}")
        Path(d, "BAD.json").write_text("{not json")
        Path(d, "NVDA.json").write_text(json.dumps({"ticker": "NVDA", "cik": 1045810,
                                                    "stakes": [dict(H("NVIDIA Corporation", 6.6, None), issuerCik=1769628, issuer="CoreWeave, Inc.")]}))
        Path(d, "cusips.json").write_text(json.dumps({"byCusip": {"21873S108": "CRWV"}}))
        so = wb.sec_owners(d, TODAY)
    holders, stakes, profiles, ciks = so["holders"], so["stakes"], so["profiles"], so["ciks"]
    assert list(holders) == ["CRWV"] and profiles["CRWV"]["sic"] == "7374" and ciks == {"CRWV": 1769628, "NVDA": 1045810}
    assert stakes["NVDA"][0]["issuerCik"] == 1769628
    assert wb.sec_owners("/no/such/dir") == {"holders": {}, "stakes": {}, "f13": {}, "profiles": {}, "ciks": {}, "shares": {}}


def test_build_carries_filed_ownership_into_nodes_and_money():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "CRWV.json").write_text(json.dumps({"ticker": "CRWV", "cik": 1769628, "profile": {"sic": "7374", "sicDescription": "Services-Computer Processing",
                                                    "formerNames": [{"name": "Atlantic Crypto Corp", "to": "2019-04-01"}]},
                                                    "holders": [H("KKR GROUP PARTNERSHIP L.P.", 12.0, 1404912, typ="PN"), H("VANGUARD GROUP INC", 4.0, 102909, typ="IA")]}))
        Path(d, "KKR.json").write_text(json.dumps({"ticker": "KKR", "cik": 1404912}))
        books = wb.books_from({"TRAPP2": [{"ticker": "CRWV", "name": "CoreWeave", "isetf": "false"},
                                          {"ticker": "KKR", "name": "KKR & Co", "isetf": "false", "industry": "Asset Management"}]})
        out = wb.build(TODAY, books, d, None, None)
    n = out["nodes"]["nodes"]
    assert n["CRWV"]["own"] == {"n5": 1, "sum5": 12.0, "top": ["KKR GROUP PARTNERSHIP L.P.", 12.0]}
    assert n["CRWV"]["sic"] == ["7374", "Services-Computer Processing"] and n["CRWV"]["formerNames"] == ["Atlantic Crypto Corp"]
    assert any(e["id"] == "F-KKR-CRWV" for e in out["money"]["edges"]) and out["stats"]["filingEdges"] == 1
    assert out["stats"]["withHolders"] == 1 and n["KKR"]["money"]["owns"] >= 1


def test_node_own_sums_within_one_class():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "GOOGL.json").write_text(json.dumps({"ticker": "GOOGL", "cik": 1652044, "holders": [
            H("VANGUARD GROUP INC", 7.6, 102909, typ="IA", cusip="02079K305"), H("BLACKROCK, INC.", 6.4, 1364742, typ="HC", cusip="02079K305"),
            H("VANGUARD GROUP INC", 7.1, 102909, typ="IA", cusip="02079K107")]}))
        out = wb.build(TODAY, wb.books_from({"TRAPP2": [{"ticker": "GOOGL", "name": "Alphabet", "isetf": "false"}]}), d, None, None)
    own = out["nodes"]["nodes"]["GOOGL"]["own"]
    assert own["n5"] == 3 and own["sum5"] == 14.0                    # class A's two holders, not 21.1 across both classes


def test_f13_edges_corporate_holdings_mapped_and_attached():
    books = dict(BOOKS199, **{"BRK-B": {"industry": "Insurance - Diversified", "sector": "Financial Services", "name": "Berkshire Hathaway"},
                              "AAPL": {"name": "Apple"}, "TINY": {"name": "Tiny Co"}, "ARM": {"name": "Arm Holdings"},
                              "NVDA": {"industry": "Semiconductors", "name": "NVIDIA"}})
    ciks = dict(CIKS199, AAPL=320193, TINY=5, ARM=1973239)
    cus = {"21873S108": "CRWV", "042068205": "ARM", "037833100": "AAPL", "888888888": "TINY", "02079K305": ["GOOG", "GOOGL"], "N97284108": "NBIS"}
    shares = {"CRWV": 480e6, "AAPL": 15.0e9, "TINY": 1e6, "GOOGL": 12.1e9}
    f13 = {"NVDA": {"period": "2026-09-30", "acc": "a-nv", "holdings": [
               {"cusip": "21873S108", "value": 3.6e9, "shares": 24182460},                  # also a 13G edge: attached
               {"cusip": "042068205", "value": 1.5e8, "shares": 1.1e6},                     # Arm: no share count on file -> no %
               {"cusip": "N97284108", "value": 2e7, "shares": 2e5, "putCall": "CALL"},      # an option: out
               {"cusip": "999999999", "value": 5e6, "shares": 1e5}]},                       # not mapped: out
           "BRK-B": {"period": "2026-09-30", "acc": "a-brk", "holdings": [
               {"cusip": "037833100", "value": 60e9, "shares": 300e6},                      # 2 %: in
               {"cusip": "888888888", "value": 5e6, "shares": 5000},                        # 0.5 %, $5M: a float position - out
               {"cusip": "02079K305", "value": 2e9, "shares": 10e6}]},                      # two classes -> one ticker (GOOGL, curated)
           "BLK": {"period": "2026-09-30", "acc": "a-blk", "holdings": [{"cusip": "037833100", "value": 1e11, "shares": 1e9}]}}   # institution: out
    edges = [E("F-NVDA-CRWV", "owns", "NVDA", "CRWV", pct=6.6, confidence="filing", kind13="13G")]
    new, att = wb.f13_edges(f13, cus, ciks, books, edges, {"GOOGL"}, shares)
    got = {(e["from"], e["to"]): e for e in new}
    assert set(got) == {("NVDA", "ARM"), ("BRK-B", "AAPL"), ("BRK-B", "GOOGL")}, got.keys()
    assert att == 1 and edges[0]["f13"]["valueUSD"] == 3.6e9 and edges[0]["f13"]["pctApprox"] == round(100 * 24182460 / 480e6, 2)
    a = got[("BRK-B", "AAPL")]
    assert a["pct"] == 2.0 and a["pctApprox"] is True and a["kind13"] == "13F" and a["valueUSD"] == 60e9 and a["id"] == "F13-BRK-B-AAPL"
    assert a["label"] == "Berkshire Hathaway: $60.0bn of Apple (13F, 2026-09-30)" and a["events"][0]["acc"] == "a-brk"
    assert got[("NVDA", "ARM")]["pct"] is None and got[("NVDA", "ARM")]["pctApprox"] is False


def test_build_maps_13f_with_the_cusip_file():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "NVDA.json").write_text(json.dumps({"ticker": "NVDA", "cik": 1045810, "f13": {"period": "2026-09-30", "acc": "a", "n": 1, "total": 1.5e8,
                                                    "holdings": [{"cusip": "042068205", "value": 1.5e8, "shares": 1.1e6}]}}))
        Path(d, "ARM.json").write_text(json.dumps({"ticker": "ARM", "cik": 1973239,
                                                   "facts": {"dilutedShares": {"quarterly": [{"end": "2026-09-30", "val": 1.06e9}]}}}))
        Path(d, "cusips.json").write_text(json.dumps({"byCusip": {"042068205": "ARM"}}))
        books = wb.books_from({"TRAPP2": [{"ticker": "NVDA", "name": "NVIDIA", "isetf": "false"}, {"ticker": "ARM", "name": "Arm", "isetf": "false"}]})
        out = wb.build(TODAY, books, d, None, None)
    e = next(x for x in out["money"]["edges"] if x["id"] == "F13-NVDA-ARM")
    assert e["pct"] == round(100 * 1.1e6 / 1.06e9, 2) and out["stats"]["f13Edges"] == 1 and out["stats"]["cusipsMapped"] == 1
    assert out["nodes"]["nodes"]["NVDA"]["f13"] == {"period": "2026-09-30", "n": 1, "total": 1.5e8}


def test_node_own_once_per_company_across_share_classes():
    with tempfile.TemporaryDirectory() as d:
        for t in ("GOOG", "GOOGL"):
            Path(d, f"{t}.json").write_text(json.dumps({"ticker": t, "cik": 1652044, "holders": [H("VANGUARD GROUP INC", 7.6, 102909, typ="IA")]}))
        books = wb.books_from({"TRAPP2": [{"ticker": "GOOG", "name": "Alphabet C", "isetf": "false"}, {"ticker": "GOOGL", "name": "Alphabet A", "isetf": "false"}]})
        out = wb.build(TODAY, books, d, None, None)
    n = out["nodes"]["nodes"]
    assert "own" in n["GOOGL"] and "own" not in n["GOOG"]           # the curated web uses GOOGL


def test_chain_audit_z200_no_dead_tickers_new_subsectors():
    chains = json.loads((ROOT / "config" / "supply_chains.json").read_text())["chains"]
    ids = {c["id"] for c in chains}
    dead = {"PXD", "HES", "LTHM", "X", "CTLT", "WBA", "CNHI", "K", "HAYN", "L3HRS", "SQ", "JNPR", "DISH", "NOVA", "RDFN", "PARA", "IPG",
            "SPLK", "PFPT", "KNBE", "GPS", "AIR.PA", "Rolls-Royce", "WFRBF"}
    left = [(c["id"], m.get("ticker")) for c in chains for k, _ in wb.STAGES for m in c.get(k) or [] if m.get("ticker") in dead]
    assert not left, left
    new = {"aero_engines", "aero_structures", "aero_systems", "aero_aftermarket", "defense_munitions", "nuclear_fuel", "glp1_obesity", "payments_rails"}
    assert new <= ids and sum(1 for c in chains if c.get("level") == "subsector") >= 24
    for c in chains:
        if c.get("level") == "subsector":
            assert c.get("parent") in ids and c.get("citations"), c["id"]
        for k, _ in wb.STAGES:
            for m in c.get(k) or []:
                assert not (m.get("ticker") and " " in m["ticker"]), (c["id"], m)          # a name in the ticker field
    revised = [c for c in chains if c.get("rev")]
    assert len(revised) >= 16 and all(any("z200 audit" in x for x in c.get("citations") or []) for c in revised)   # each says what changed


def test_web_priority_reads_configs():
    p = fs.web_priority(ROOT)
    assert {"NVDA", "GOOGL", "MSFT", "ANT", "AMD"} <= p and "" not in p


if __name__ == "__main__":                       # plain-python runner (the workflow has no pytest)
    ok = fail = 0
    for name, fn in sorted((k, v) for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)):
        try:
            fn()
            ok += 1
            print(f"PASS {name}")
        except Exception as ex:                  # noqa: BLE001
            fail += 1
            print(f"FAIL {name}: {type(ex).__name__} {ex}")
    print(f"\n{ok} passed, {fail} failed")
    sys.exit(1 if fail else 0)
