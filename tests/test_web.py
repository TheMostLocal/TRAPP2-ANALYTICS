# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   tests/test_web.py
#  z197 - the Business Web build (pipeline/web_build.py) and the SEC facts queue (pipeline/fetch_sec.py).
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
