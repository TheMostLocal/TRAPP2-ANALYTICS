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
#  z199 - OWNERSHIP FROM THE FILINGS: data/sec/<T>.json now carries each company's 5 %+ holders (Schedule 13D / 13G) and
#         its SEC profile. A holder that is itself a company in the universe (matched by CIK) becomes an 'owns' edge with
#         confidence 'filing' - or confirms the curated edge it duplicates. Passive 13G stakes of asset managers, banks,
#         brokers and life insurers filing as institutions (IA / HC / BK / BD / IC ...) are client money, not cross-
#         ownership, and are left out; a 13D (activist / control) from anyone, and a sponsor's own stake, stay in.
#         The stakes a company files about others count too (its own doc), and a company's corporate 13F holdings
#         (mapped CUSIP -> ticker with data/sec/cusips.json) become owns edges with value, shares and an approximate %
#         (or attach their 13F figures to the edge that already links the pair).
#  z200 - one holder summary per company (GOOG / GOOGL: the chosen ticker carries `own`); chains.json carries the config's
#         revised chains (`rev`: dead / renamed tickers fixed) and 8 more subsector chains - the app swaps them in at load.
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
BUILD = "z200"


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


# z199: 13G holders whose business is holding other people's money - their passive 5 % stakes are client assets, not
# corporate cross-ownership (BlackRock, State Street, the banks' asset-management arms). A 13D (active / control) from
# anyone - a private-equity sponsor, Berkshire, a strategic buyer - is always a relationship.
INSTITUTIONAL = {"Asset Management", "Banks - Diversified", "Banks - Regional", "Capital Markets", "Insurance - Life"}
# the Schedule 13G type codes of a filer holding for clients (13d-1(b) institutions); CO / PN / OO / IN hold their own
PASSIVE_TYPES = {"IA", "HC", "BK", "BD", "IC", "IV", "EP", "SA", "FI", "CP"}


def sec_owners(sec_dir, today=None):
    """z199: one pass over the SEC docs - each company's 5 %+ holders and the 5 %+ stakes it holds in others (Schedule
    13D / 13G), its corporate 13F holdings, its SEC profile, CIK, and shares (the latest diluted count, for an
    approximate % of a 13F stake). -> {"holders", "stakes", "f13", "profiles", "ciks", "shares"} keyed by ticker"""
    out = {k: {} for k in ("holders", "stakes", "f13", "profiles", "ciks", "shares")}
    today = today or date.today()
    sec_dir = Path(sec_dir)
    for p in sorted(sec_dir.glob("*.json")) if sec_dir.exists() else []:
        if p.name in ("index.json", "cusips.json"):
            continue
        try:
            j = json.loads(p.read_text())
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        t = str(j.get("ticker") or p.stem).upper()
        try:
            if j.get("cik"):
                out["ciks"][t] = int(j["cik"])
        except (TypeError, ValueError):
            pass
        for k, src in (("holders", "holders"), ("stakes", "stakes"), ("profiles", "profile")):
            if j.get(src):
                out[k][t] = j[src]
        if (j.get("f13") or {}).get("holdings"):
            out["f13"][t] = j["f13"]
        ds = ((j.get("facts") or {}).get("dilutedShares") or {})
        pts = [x for x in (ds.get("quarterly") or []) + (ds.get("annual") or []) if x.get("val") and x.get("end")]
        if pts:
            last = max(pts, key=lambda x: x["end"])
            try:
                if (today - date.fromisoformat(str(last["end"])[:10])).days <= 460:
                    out["shares"][t] = float(last["val"])
            except (TypeError, ValueError):
                pass
    return out


def _pick_by_cik(ciks, curated=(), books=()):
    """One ticker per CIK (GOOG / GOOGL, BRK-A / BRK-B share one): the one the curated web uses, else one in the books,
    else the first alphabetically."""
    out = {}
    for t, c in ciks.items():
        cur = out.get(c)
        if cur is None or (t not in curated, t not in books, t) < (cur not in curated, cur not in books, cur):
            out[c] = t
    return out


def _passive(h):
    """A 13G filed as an institution holding for clients: under Rule 13d-1(b) (qualified institutions) when the filing
    says which rule; otherwise by its type codes (IA / HC / BK ...), or no type at all."""
    if h.get("rule"):
        return "13d-1(b)" in str(h["rule"])
    types = {x.strip().upper() for x in str(h.get("type") or "").split("/") if x.strip()}
    return not types or bool(types & PASSIVE_TYPES)


def filing_edges(holders, ciks, edges, books, curated=(), stakes=None):
    """z199: a 5 %+ stake one company in the universe holds in another, straight from its Schedule 13D / 13G -> an 'owns'
    edge with confidence 'filing'. Both sides of a filing count: the holders filed about a company (the owner matched by
    its CIK) and the stakes a company filed about others (the issuer matched by its CIK) - whichever doc was read first;
    where both have it, the latest filing wins. Where the curated web already has that owner -> company pair, the filing
    CONFIRMS it (an event on the curated edge, pctFiled - under 5 % when the filing reports an exit) instead of a
    duplicate. Passive 13G stakes of asset managers / banks are skipped. -> (new edges, confirmations)"""
    by_cik = _pick_by_cik(ciks, set(curated), set(books))
    have = {(e["from"], e["to"]): e for e in edges if e.get("type") == "owns"}
    best = {}                                          # (owner, subject) -> its latest filing row

    def offer(owner, t, h):
        if not owner or not t or owner == t:
            return
        k = (owner, t)
        if k not in best or (h.get("filed") or "", h.get("pct") or 0) > (best[k].get("filed") or "", best[k].get("pct") or 0):
            best[k] = h
    for t, hs in holders.items():
        if by_cik.get(ciks.get(t)) in (None, t):       # skip the other share class of the same company
            for h in hs or []:
                offer(by_cik.get(h.get("cik")), t, h)
    for t, ss in (stakes or {}).items():
        if by_cik.get(ciks.get(t)) in (None, t):
            for h in ss or []:
                offer(t, by_cik.get(h.get("issuerCik")), h)
    out, confirmed = [], 0
    for (owner, t), h in sorted(best.items()):
        if h.get("kind") != "13D" and (books.get(owner) or {}).get("industry") in INSTITUTIONAL and _passive(h):
            continue
        ev = {"date": h.get("event") or h.get("filed"), "text": f"Schedule {h.get('kind')}: {h.get('pct')} % of the class"
              + (f" ({h.get('type')})" if h.get("type") else "") + (f", Rule {h.get('rule')}" if h.get("rule") else ""),
              "src": "SEC", "acc": h.get("acc"), "confidence": "filing"}
        cur = have.get((owner, t))
        if cur is not None:
            if not any(x.get("acc") == h.get("acc") for x in cur.get("events") or []):
                cur.setdefault("events", []).append(ev)
            cur["pctFiled"] = h.get("pct")
            cur["holder"] = h.get("name")
            confirmed += 1
            continue
        if (h.get("pct") or 0) < 5:
            continue                                   # below 5 %: an exit, not a stake
        out.append({"id": f"F-{owner}-{t}", "type": "owns", "from": owner, "to": t, "pct": h.get("pct"), "confidence": "filing",
                    "label": f"{(books.get(owner) or {}).get('name') or owner}: {h.get('pct')} % of {(books.get(t) or {}).get('name') or t}"
                             f" (Schedule {h.get('kind')})", "events": [ev],
                    "kind13": h.get("kind"), "holder": h.get("name"), "rule": h.get("rule"), "purpose": h.get("purpose")})
    return out, confirmed


def _usd(v):
    v = float(v or 0)
    return f"${v / 1e9:.1f}bn" if v >= 1e9 else f"${v / 1e6:.0f}M"


def f13_edges(f13, cusip_by, ciks, books, edges, curated=(), shares=None):
    """z199: corporate 13F holdings -> 'owns' edges with confidence 'filing' (kind13 '13F'): every listed equity a
    company in the universe reports holding at quarter end, mapped from CUSIP to ticker (data/sec/cusips.json), its value
    and shares, and an approximate % of the issuer (shares / the issuer's latest diluted count; ≈). A holding the web
    already links (curated or a 13D / 13G edge) gets the 13F figures attached (`f13`) instead of a second edge. Left
    out: institutions (INSTITUTIONAL industries - client money), options, unmapped CUSIPs, and for financial-sector
    holders (Berkshire, insurers' float portfolios) positions under ~1 % of the issuer and under $1 bn.
    -> (new edges, attached)"""
    by_cik = _pick_by_cik(ciks, set(curated), set(books))

    def tick(cu):
        t = (cusip_by or {}).get(cu)
        if isinstance(t, list):
            t = t[0] if t else None
        if not t:
            return None
        return by_cik.get(ciks.get(t), t) if ciks.get(t) else t
    have = {(e["from"], e["to"]): e for e in edges if e.get("type") == "owns"}
    out, attached = [], 0
    for owner, f in sorted((f13 or {}).items()):
        if by_cik.get(ciks.get(owner)) not in (None, owner):
            continue                                         # BRK-A / BRK-B: one filer
        b = books.get(owner) or {}
        if b.get("industry") in INSTITUTIONAL:
            continue
        fin = b.get("sector") in ("Financial Services", "Financials")
        for h in f.get("holdings") or []:
            if h.get("putCall"):
                continue
            t = tick(h.get("cusip"))
            if not t or t == owner:
                continue
            so = (shares or {}).get(t)
            pct = round(100.0 * h["shares"] / so, 2) if so and h.get("shares") else None
            if pct is not None and pct > 100:
                pct = None                                   # ADS vs ordinary shares, or a bad count - no %
            if fin and not ((pct or 0) >= 1 or (h.get("value") or 0) >= 1e9):
                continue
            info = {"valueUSD": h.get("value"), "shares": h.get("shares"), "period": f.get("period"), "acc": f.get("acc"), "pctApprox": pct}
            cur = have.get((owner, t))
            if cur is not None:
                cur["f13"] = info
                attached += 1
                continue
            oname, tname = b.get("name") or owner, (books.get(t) or {}).get("name") or t
            e = {"id": f"F13-{owner}-{t}", "type": "owns", "from": owner, "to": t, "confidence": "filing", "kind13": "13F",
                 "valueUSD": h.get("value"), "shares": h.get("shares"), "pct": pct, "pctApprox": pct is not None,
                 "label": f"{oname}: {_usd(h.get('value'))} of {tname} (13F, {f.get('period')})",
                 "events": [{"date": f.get("period"), "text": f"13F-HR: {int(h.get('shares') or 0):,} shares, {_usd(h.get('value'))} at quarter end",
                             "src": "SEC", "acc": f.get("acc"), "confidence": "filing"}]}
            out.append(e)
            have[(owner, t)] = e
    return out, attached


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
    edges = list(rel.get("edges") or [])
    # z199: 5 %+ stakes from Schedule 13D / 13G - corporate cross-ownership straight from the filings
    so = sec_owners(sec_dir or DATA / "sec", today)
    holders, stakes, profiles, ciks = so["holders"], so["stakes"], so["profiles"], so["ciks"]
    curated_names = {x for e in edges for x in (e["from"], e["to"])}
    f_edges, f_confirmed = filing_edges(holders, ciks, edges, books, curated_names, stakes)
    edges += f_edges
    # z199: corporate 13F holdings, CUSIPs mapped with the map the SEC run builds from the same filings
    cus = (_read(Path(sec_dir or DATA / "sec") / "cusips.json", {}) or {}).get("byCusip") or {}
    f13e, f13_att = f13_edges(so["f13"], cus, ciks, books, edges, curated_names, so["shares"])
    edges += f13e
    one_class = _pick_by_cik(ciks, curated_names, set(books))         # z200: GOOG / GOOGL - one carries the holder summary
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
        pf = profiles.get(t) or {}
        if pf.get("sic"):
            n["sic"] = [pf.get("sic"), pf.get("sicDescription")]
        if pf.get("formerNames"):
            n["formerNames"] = [f.get("name") for f in pf["formerNames"]][:3]
        hs = [h for h in holders.get(t) or [] if (h.get("pct") or 0) >= 5]
        if hs and one_class.get(ciks.get(t)) not in (None, t):
            hs = []                                                   # the other share class: summarised once, on the chosen ticker
        if hs:
            cls = {}
            for h in hs:                                # sum within one class: the one most holders filed on
                cls[h.get("cusip") or ""] = cls.get(h.get("cusip") or "", 0) + 1
            main = max(cls, key=cls.get)
            n["own"] = {"n5": len(hs), "sum5": round(sum(h.get("pct") or 0 for h in hs if (h.get("cusip") or "") == main), 1),
                        "top": [hs[0].get("name"), hs[0].get("pct")]}
        st = [h for h in stakes.get(t) or [] if (h.get("pct") or 0) >= 5]
        if st:
            n["stakes"] = {"n5": len(st), "top": [st[0].get("issuer"), st[0].get("pct")]}
        f3 = so["f13"].get(t)
        if f3:
            n["f13"] = {"period": f3.get("period"), "n": f3.get("n"), "total": f3.get("total")}
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
             "moneyEdges": len(edges), "filingEdges": len(f_edges), "filingConfirmed": f_confirmed,
             "withHolders": len(holders), "withStakes": len(stakes), "withSic": sum(1 for t in nodes if nodes[t].get("sic")),
             "f13Filers": len(so["f13"]), "f13Edges": len(f13e), "f13Attached": f13_att, "cusipsMapped": len(cus),
             "loops": len(loops), "chainNamesNotInBooks": missing}
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
          f"{s['moneyEdges']} money edges ({s['filingEdges']} from 13D / 13G filings, {s['filingConfirmed']} curated confirmed by one; "
          f"{s['withHolders']} companies with 5 %+ holders on file; {s['f13Edges']} from {s['f13Filers']} corporate 13Fs, "
          f"{s['f13Attached']} attached; {s['cusipsMapped']} CUSIPs mapped), {s['loops']} loops")
    for L in out["money"]["loops"]:
        print("  loop:", " -> ".join(L["path"]), L["edges"])
    if s["chainNamesNotInBooks"]:
        print(f"  chain names not in any book ({len(s['chainNamesNotInBooks'])}): {' '.join(s['chainNamesNotInBooks'])}")
    for e in out["ipo"]["events"][:8]:
        print(f"  ipo: {e['ticker']} {e.get('date') or e.get('rule')} {e['kind']} - {e['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
