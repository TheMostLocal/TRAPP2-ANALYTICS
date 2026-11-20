#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/web_build.py
#
#  z197 - THE BUSINESS WEB, first build (STATUS §1e). Owner (11-19): "I need the system to understand every single business
#  inside of the system … how different companies are invested in each other and how they also use each other …
#  Nvidia in the circular financing … it needs to be understood for their profits and their debts."
#
#  In:  config/supply_chains.json   - the 27 chains (from the app) + subsector chains (z197), each link with a confidence
#       config/web_relationships.json - curated, sourced money relationships (owns / funds / commits / supplies /
#                                      underwrites); first slice: the AI complex
#       config/ipo_watch.json        - IPO lock-ups, quiet periods, underwriters and fees
#       data/sec/<T>.json            - SEC XBRL facts (revenue, cash flow, capex, debt, cash)
#       data/earnings/universe.json  - S&P 500 / Nasdaq-100 / Dow members and weights
#       the five books' master.json, TRAPP2-CREDIT ratings.json (raw; optional)
#  Out: data/web/nodes.json   - every ticker in the books: what it is, where it sits (chains, stage), money structure,
#                              index weights, rating, money-web links, upcoming IPO events
#       data/web/money.json   - the money web for the app's WEB view: entities, edges, LOOPS (money that comes back to its
#                              origin), per-company circularity (who it funds that buys from it, and how much)
#       data/web/chains.json  - every chain (the app merges the ones it doesn't have)
#       data/web/ipo.json     - upcoming lock-up releases / quiet-period ends (the bot avoids new longs into big unlocks)
#  Env: WEB_OFFLINE=1 (no raw fetches) · WEB_NOW=YYYY-MM-DD (tests)
# ============================================================
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG = ROOT / "config"
OUT = DATA / "web"
OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER") or "TheMostLocal"
RAW = f"https://raw.githubusercontent.com/{OWNER}"
BOOKS = ("TRAPP2", "TRAPP2-1", "TRAPP2-2", "TRAPP2-3", "TRAPP2-4")
STAGES = (("upstream", "up"), ("midstream", "mid"), ("downstream", "down"))
BUILD = "z197"


def _get_json(url, timeout=60):
    if os.environ.get("WEB_OFFLINE") == "1":
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TRAPP2-ANALYTICS web"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  (unavailable: {url.split('/main/')[-1]} - {type(e).__name__})")
        return None


def _read(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _flag(v):
    """master.json carries isetf as the string 'true' / 'false' (bool('false') is True - z197 dry-run bug)."""
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def book_row(r, b):
    return {"name": r.get("name"), "sector": r.get("sector"), "industry": r.get("industry"), "book": b,
            "assetClass": r.get("asset_class"), "isEtf": _flag(r.get("isetf")), "mc": _num(r.get("marketcap")),
            "price": _num(r.get("price")), "currency": r.get("currency")}


def books_from(masters):
    """{book: [master rows]} -> {ticker: row}; the first book that lists a ticker owns it (BOOKS order)."""
    out = {}
    for b in BOOKS:
        for r in (masters.get(b) or []):
            if not isinstance(r, dict):
                continue
            t = str(r.get("ticker") or "").upper()
            if t and t not in out:
                out[t] = book_row(r, b)
    return out


def load_books():
    return books_from({b: _get_json(f"{RAW}/{b}/main/data/master.json") or [] for b in BOOKS})


# ---- money structure from SEC XBRL facts ---------------------------------------------------------------------------
FACT_KEYS = {"revenue": "rev", "operatingIncome": "opInc", "netIncome": "ni", "operatingCashFlow": "ocf", "capex": "capex",
             "cash": "cash", "longTermDebt": "debt", "equity": "eq",
             # z198 (fetch_sec FACTS_VERSION 3): the money structure behind the relationships
             "rpo": "rpo", "purchaseObligations": "purchOb", "equityMethod": "eqMethod", "equitySecurities": "eqListed",
             "equityPrivate": "eqPrivate"}


def latest_annual(fact):
    """Trailing twelve months when the SEC doc has it (z187 v2 docs), else the latest instant / annual figure."""
    fact = fact or {}
    for k in ("ttm", "latest"):
        v = fact.get(k)
        if isinstance(v, dict) and v.get("val") is not None and v.get("end"):
            return v["val"], v["end"]
    rows = [r for r in (fact.get("annual") or []) if r.get("end") and r.get("val") is not None]
    if not rows:
        return None, None
    r = max(rows, key=lambda r: r["end"])
    return r["val"], r["end"]


def sec_facts(sec_dir, today):
    out = {}
    d = Path(sec_dir)
    if not d.exists():
        return out
    for p in d.glob("*.json"):
        if p.name == "index.json":
            continue
        try:
            j = json.loads(p.read_text())
        except Exception:
            continue
        rec = facts_record(j, today)
        if rec:
            out[str(j.get("ticker") or p.stem).upper()] = rec
    return out


def facts_record(j, today, max_age=460):
    """One company's money structure. Each line item stands on its own date: a figure whose period ended more than
    ~15 months ago is dropped and named in staleKeys (pre-z187 docs kept MSFT's 2010 'Revenues' while cash was current -
    one shared date hid it). v = the SEC doc's facts version (1 = pre-merge series, refresh pending)."""
    f = (j or {}).get("facts") or {}
    rec, stale, ends = {}, [], []
    for k, short in FACT_KEYS.items():
        v, end = latest_annual(f.get(k))
        if v is None:
            continue
        try:
            age = (today - date.fromisoformat(str(end)[:10])).days
        except (TypeError, ValueError):
            continue
        if age > max_age:
            stale.append(short)
            continue
        rec[short] = v
        ends.append(str(end)[:10])
    if not rec and not stale:
        return None
    rec["fy"] = max(ends) if ends else None
    rec["v"] = (j or {}).get("factsV") or 1
    if stale:
        rec["staleKeys"] = stale
    if "rev" not in rec:
        rec["stale"] = True          # no current revenue -> ratios that need it stay blank
    return rec


def index_weights(universe):
    out = {}
    for idx, v in ((universe or {}).get("indices") or {}).items():
        for m in v.get("members") or []:
            t = str(m.get("ticker") or "").upper()
            w = _num(m.get("weight"))
            if t:
                out.setdefault(t, {})[idx] = round(w * 100, 4) if w is not None and w < 1 else (round(w, 4) if w is not None else None)
    return out


# ---- the money web -------------------------------------------------------------------------------------------------
def flow(e):
    """Money / value direction of an edge: owns and funds - investor -> investee (a warrant: value from the issuer to the
    holder), commits - buyer -> seller, supplies - customer pays supplier (to -> from), underwrites - issuer pays fees."""
    t = e["type"]
    if t == "owns":
        return (e["to"], e["from"]) if e.get("instrument") == "warrant" else (e["from"], e["to"])
    if t in ("funds", "commits"):
        return e["from"], e["to"]
    if t in ("supplies", "underwrites"):
        return e["to"], e["from"]
    return e["from"], e["to"]


def find_loops(edges, max_len=4):
    """Simple cycles in the money-flow graph (length 2..max_len) - money or value that comes back to where it started.
    -> [{"path": [A, B, (C), A], "edges": [ids]}], each cycle once."""
    g = {}
    for e in edges:
        a, b = flow(e)
        g.setdefault(a, []).append((b, e["id"]))
    seen, loops = set(), []

    def dfs(start, node, path, eids):
        for nxt, eid in g.get(node, []):
            if nxt == start and len(path) >= 2:
                key = tuple(sorted(path))
                ekey = (key, tuple(sorted(eids + [eid])))
                if ekey not in seen:
                    seen.add(ekey)
                    loops.append({"path": path + [start], "edges": eids + [eid]})
            elif nxt not in path and len(path) < max_len and nxt > start:
                dfs(start, nxt, path + [nxt], eids + [eid])
    for s in sorted(g):
        dfs(s, s, [s], [])
    return loops


def circularity(edges, loops, facts):
    """Per company: the counterparties it funds / owns that are committed buyers of it, the money each way, and the
    commitments back as a multiple of its annual revenue (multi-year commitments vs one year's revenue - a scale, not a
    ratio of like for like)."""
    out = {}
    pairs = {}
    for e in edges:
        if e["type"] in ("owns", "funds") and e.get("instrument") != "warrant":
            pairs.setdefault((e["from"], e["to"]), []).append(e)
    for (a, b), fund in pairs.items():
        back = [x for x in edges if x["type"] == "commits" and x["from"] == b and x["to"] == a]
        if not back:
            continue
        c = out.setdefault(a, {"fundedBuyers": [], "investedUSD": 0.0, "committedBackUSD": 0.0, "committedBackGW": 0.0})
        inv = sum(e.get("amountUSD") or 0.0 for e in fund)
        com = sum(x.get("amountUSD") or 0.0 for x in back)
        gw = sum(x.get("gw") or 0.0 for x in back)
        c["fundedBuyers"].append({"to": b, "how": " + ".join(sorted({e["type"] for e in fund})), "investedUSD": inv or None,
                                  "committedBackUSD": com or None, "committedBackGW": gw or None,
                                  "edges": [e["id"] for e in fund] + [x["id"] for x in back]})
        c["investedUSD"] += inv
        c["committedBackUSD"] += com
        c["committedBackGW"] += gw
    # warrants granted by a seller to a buyer (AMD -> OpenAI): equity handed to a customer that commits to buy
    for e in edges:
        if e["type"] == "owns" and e.get("instrument") == "warrant":
            seller, buyer = e["to"], e["from"]
            back = [x for x in edges if x["type"] == "commits" and x["from"] == buyer and x["to"] == seller]
            if back:
                c = out.setdefault(seller, {"fundedBuyers": [], "investedUSD": 0.0, "committedBackUSD": 0.0, "committedBackGW": 0.0})
                c["fundedBuyers"].append({"to": buyer, "how": "warrant (equity to a customer)", "pct": e.get("pct"),
                                          "committedBackUSD": sum(x.get("amountUSD") or 0.0 for x in back) or None,
                                          "committedBackGW": sum(x.get("gw") or 0.0 for x in back) or None,
                                          "edges": [e["id"]] + [x["id"] for x in back]})
                c["committedBackGW"] += sum(x.get("gw") or 0.0 for x in back)
    for a, c in out.items():
        f = facts.get(a) or {}
        rev = f.get("rev")                                       # current only - facts_record drops stale line items
        c["revenueUSD"] = rev
        c["committedBackToRevenue"] = round(c["committedBackUSD"] / rev, 3) if rev and c["committedBackUSD"] else None
        c["loops"] = sum(1 for L in loops if a in L["path"])
        for k in ("investedUSD", "committedBackUSD", "committedBackGW"):
            c[k] = round(c[k], 2) or None
    return out


def backlog_view(edges, facts):
    """z198 - for each company that tracked buyers have committed to: the commitments in our web vs the backlog it
    reports (RPO), and vs a year's revenue. A commitment from a buyer that is itself funded by this company (or by its
    suppliers) is the circular part of the backlog. Indicative: an announced commitment may not be in RPO yet (signed
    later, conditional, capacity-based), so a share above 100 % means 'announced beyond what's booked'.
    -> {ticker: {"commitIn": $, "commitGW", "from": [...], "rpo", "rpoEnd", "shareOfRpo", "yearsOfRevenue"}}"""
    out = {}
    for e in edges:
        if e.get("type") != "commits":
            continue
        b = out.setdefault(e["to"], {"commitIn": 0.0, "commitGW": 0.0, "from": []})
        b["commitIn"] += e.get("amountUSD") or 0.0
        b["commitGW"] += e.get("gw") or 0.0
        b["from"].append({"buyer": e["from"], "usd": e.get("amountUSD"), "gw": e.get("gw"), "edge": e["id"]})
    for t, b in out.items():
        f = facts.get(t) or {}
        rpo, rev = f.get("rpo"), f.get("rev")
        b["rpo"] = rpo
        b["shareOfRpo"] = round(b["commitIn"] / rpo, 3) if rpo and b["commitIn"] else None
        b["yearsOfRevenue"] = round(b["commitIn"] / rev, 2) if rev and b["commitIn"] else None
        b["commitIn"] = b["commitIn"] or None
        b["commitGW"] = b["commitGW"] or None
    return out


def ipo_events(ipo_cfg, today, horizon=400):
    ev = []
    for t, v in ((ipo_cfg or {}).get("ipos") or {}).items():
        for e in v.get("events") or []:
            d = e.get("date")
            days = None
            if d:
                try:
                    days = (date.fromisoformat(d) - today).days
                except ValueError:
                    continue
                if days < -5 or days > horizon:
                    continue
            ev.append(dict(e, ticker=t, name=v.get("name"), daysAway=days))
    ev.sort(key=lambda e: (e["daysAway"] is None, e["daysAway"] if e["daysAway"] is not None else 0))
    return ev


def chain_index(chains):
    """ticker -> [[chain id, stage, confidence, role]]"""
    out = {}
    for ch in chains:
        for key, st in STAGES:
            for m in ch.get(key) or []:
                t = str(m.get("ticker") or "").upper()
                if t:
                    out.setdefault(t, []).append([ch["id"], st, m.get("confidence"), m.get("role")])
    return out


def build(today=None, books=None, sec_dir=None, universe=None, ratings=None):
    today = today or date.today()
    chains_doc = _read(CFG / "supply_chains.json", {}) or {}
    chains = chains_doc.get("chains") or []
    rel = _read(CFG / "web_relationships.json", {}) or {}
    ipo_cfg = _read(CFG / "ipo_watch.json", {}) or {}
    books = books if books is not None else {}
    facts = sec_facts(sec_dir or DATA / "sec", today)
    idxw = index_weights(universe if universe is not None else _read(DATA / "earnings" / "universe.json", {}))
    rt = (ratings or {}).get("ratings") if isinstance(ratings, dict) else None
    edges = rel.get("edges") or []
    loops = find_loops(edges)
    circ = circularity(edges, loops, facts)
    backlog = backlog_view(edges, facts)
    cidx = chain_index(chains)
    ipos = ipo_events(ipo_cfg, today)
    # nodes: every ticker in the books + every web / chain participant
    tickers = set(books) | set(cidx) | {e["from"] for e in edges} | {e["to"] for e in edges}
    ents = rel.get("entities") or {}
    notes = rel.get("nodeNotes") or {}
    nodes = {}
    for t in sorted(tickers):
        b = books.get(t) or {}
        ent = ents.get(t) or {}
        n = {"name": b.get("name") or ent.get("name") or t, "sector": b.get("sector") or ent.get("sector"),
             "industry": b.get("industry") or ent.get("industry"), "book": b.get("book"), "kind": ent.get("kind") or ("fund" if b.get("isEtf") else "public" if b else "outside the books")}
        if b.get("mc"):
            n["mc"] = b["mc"]
        if cidx.get(t):
            n["chains"] = cidx[t]
        if facts.get(t):
            n["facts"] = facts[t]
        if idxw.get(t):
            n["idx"] = idxw[t]
        if rt and isinstance(rt.get(t), dict):
            n["rating"] = {"r": rt[t].get("r"), "n": rt[t].get("n")}
        mine = [e for e in edges if t in (e["from"], e["to"])]
        if mine:
            n["money"] = {"owns": sum(1 for e in mine if e["type"] == "owns" and e["from"] == t),
                          "ownedBy": sum(1 for e in mine if e["type"] == "owns" and e["to"] == t),
                          "funds": sum(1 for e in mine if e["type"] == "funds" and e["from"] == t),
                          "commitsOutUSD": sum(e.get("amountUSD") or 0 for e in mine if e["type"] == "commits" and e["from"] == t) or None,
                          "commitsInUSD": sum(e.get("amountUSD") or 0 for e in mine if e["type"] == "commits" and e["to"] == t) or None,
                          "edges": [e["id"] for e in mine]}
        if circ.get(t):
            n["circularity"] = {k: circ[t][k] for k in ("investedUSD", "committedBackUSD", "committedBackGW", "committedBackToRevenue", "loops")}
        if backlog.get(t):
            n["backlog"] = backlog[t]
        if notes.get(t):
            n["notes"] = notes[t]
        up = [e for e in ipos if e["ticker"] == t]
        if up:
            n["ipo"] = up
        nodes[t] = n
    in_books = set(books)
    missing = sorted(t for t in cidx if books and t not in in_books and t.replace(".", "").isalnum() and "." not in t)
    asof = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    money = {"asOf": asof, "build": BUILD, "types": rel.get("types"), "sources": rel.get("sources"), "entities": ents,
             "edges": edges, "loops": loops, "circularity": circ, "backlog": backlog, "nodeNotes": notes,
             "names": {t: nodes[t]["name"] for t in nodes if any(t in (e["from"], e["to"]) for e in edges)},
             "sectors": {t: nodes[t].get("sector") for t in nodes if any(t in (e["from"], e["to"]) for e in edges)}}
    stats = {"tickers": len(nodes), "inBooks": len(in_books), "withChains": sum(1 for t in nodes if nodes[t].get("chains")),
             "withFacts": sum(1 for t in nodes if nodes[t].get("facts")), "withIndexWeight": sum(1 for t in nodes if nodes[t].get("idx")),
             "chains": len(chains), "subsectorChains": sum(1 for c in chains if c.get("level") == "subsector"),
             "moneyEdges": len(edges), "loops": len(loops), "chainNamesNotInBooks": missing}
    return {"nodes": {"asOf": asof, "build": BUILD, "stats": stats, "nodes": nodes},
            "money": money,
            "chains": {"asOf": asof, "build": BUILD, "note": chains_doc.get("note"), "chains": chains},
            "ipo": {"asOf": asof, "build": BUILD, "note": ipo_cfg.get("note"), "events": ipos, "ipos": ipo_cfg.get("ipos") or {}},
            "stats": stats}


def main():
    today = date.fromisoformat(os.environ["WEB_NOW"]) if os.environ.get("WEB_NOW") else datetime.now(timezone.utc).date()
    books = load_books()
    ratings = _get_json(f"{RAW}/TRAPP2-CREDIT/main/data/ratings.json")
    out = build(today, books, DATA / "sec", None, ratings)
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ("nodes", "money", "chains", "ipo"):
        (OUT / f"{name}.json").write_text(json.dumps(out[name], separators=(",", ":"), default=str))
    s = out["stats"]
    print(f"business web {today}: {s['tickers']} tickers ({s['inBooks']} in the books) · {s['withChains']} placed in {s['chains']} chains "
          f"({s['subsectorChains']} subsector) · {s['withFacts']} with SEC facts · {s['withIndexWeight']} with index weights · "
          f"{s['moneyEdges']} money edges, {s['loops']} loops")
    for L in out["money"]["loops"]:
        print("  loop:", " -> ".join(L["path"]), L["edges"])
    if s["chainNamesNotInBooks"]:
        print(f"  chain names not in any book ({len(s['chainNamesNotInBooks'])}): {' '.join(s['chainNamesNotInBooks'])}")
    for e in out["ipo"]["events"][:8]:
        print(f"  ipo: {e['ticker']} {e.get('date') or e.get('rule')} {e['kind']} - {e['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
