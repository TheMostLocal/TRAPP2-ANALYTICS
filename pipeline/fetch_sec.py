#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/fetch_sec.py
#
#  z158 - SEC EDGAR, official and free:
#    events   - 8-K filings (last 180 days) with their item numbers in words, and red flags:
#               1.03 bankruptcy, 4.02 non-reliance on past financials, 3.01 delisting notice, 4.01 auditor
#               change, 2.04 debt acceleration, 2.06 impairment; caution: 5.02 officer / director change,
#               2.05 restructuring, 1.05 cybersecurity incident
#    insiders - Form 4 (last 90 days), each filing's XML read once: open-market buys (P) and sales (S) by
#               officers, directors and 10 % holders; net value, distinct buyers / sellers, cluster buying
#    facts    - XBRL company facts AS FILED: revenue, net income, diluted EPS, operating income, operating
#               cash flow, capex, cash, long-term debt, equity, diluted shares - annual (10-K) and quarterly
#  Timely: EDGAR's "current filings" feed is read each run and any company with a new 8-K or Form 4 is
#  refreshed first; the rest rotate (oldest first). SEC asks for <= 10 requests / s and a contact in the
#  User-Agent (SEC_USER_AGENT).
#    -> data/sec/<T>.json, data/sec/index.json
# ============================================================
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "sec"
UA = (os.environ.get("SEC_USER_AGENT") or "").strip()
PER_RUN = int(os.environ.get("SEC_COMPANIES_PER_RUN", "400"))
FORM4_DOCS = int(os.environ.get("SEC_FORM4_DOCS", "700"))
FACTS_PER_RUN = int(os.environ.get("SEC_FACTS_PER_RUN", "150"))

ITEMS = {"1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy / receivership", "1.04": "mine safety",
         "1.05": "cybersecurity incident", "2.01": "acquisition / disposition completed", "2.02": "results of operations",
         "2.03": "new debt obligation", "2.04": "debt acceleration", "2.05": "restructuring / exit costs", "2.06": "material impairment",
         "3.01": "delisting notice", "3.02": "unregistered equity sale", "3.03": "holders' rights modified", "4.01": "auditor change",
         "4.02": "non-reliance on past financials", "5.01": "change in control", "5.02": "officer / director change",
         "5.03": "bylaws amended", "5.07": "shareholder vote", "7.01": "Reg FD disclosure", "8.01": "other events", "9.01": "exhibits"}
RED = {"1.03", "4.02", "3.01", "4.01", "2.04", "2.06"}
CAUTION = {"5.02", "2.05", "1.05", "5.01"}
CONCEPTS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "netIncome": ["NetIncomeLoss"], "epsDiluted": ["EarningsPerShareDiluted"], "operatingIncome": ["OperatingIncomeLoss"],
    "operatingCashFlow": ["NetCashProvidedByUsedInOperatingActivities"], "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"], "longTermDebt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "equity": ["StockholdersEquity"], "dilutedShares": ["WeightedAverageNumberOfDilutedSharesOutstanding"]}


def log(*a):
    print("[sec]", *a, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def write(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, separators=(",", ":")))


def get(url, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=60) as r:
        b = r.read()
    time.sleep(0.12)
    return b if raw else json.loads(b.decode())


def current_ciks(form):
    """CIKs with a filing of `form` in EDGAR's current-filings feed (the newest ~400)."""
    ciks = set()
    for start in range(0, 400, 100):
        try:
            b = get(f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb=&owner=include&start={start}&count=100&output=atom", raw=True).decode("utf-8", "replace")
        except Exception as e:
            log(f"current {form} feed: {e}")
            break
        found = re.findall(r"/edgar/data/(\d+)/", b)
        ciks |= {int(c) for c in found}
        if len(found) < 50:
            break
    return ciks


def events_from(sub, today):
    r = (sub.get("filings") or {}).get("recent") or {}
    rows = zip(r.get("form") or [], r.get("filingDate") or [], r.get("accessionNumber") or [], r.get("items") or [], r.get("primaryDocument") or [])
    out = []
    cut = (today - timedelta(days=180)).isoformat()
    for form, fd, acc, items, doc in rows:
        if form not in ("8-K", "8-K/A") or fd < cut:
            continue
        its = [i.strip() for i in str(items or "").split(",") if i.strip()]
        out.append({"date": fd, "form": form, "items": its, "labels": [ITEMS.get(i, i) for i in its],
                    "red": sorted(set(its) & RED), "caution": sorted(set(its) & CAUTION), "acc": acc, "doc": doc})
    return out


def form4_list(sub, today):
    r = (sub.get("filings") or {}).get("recent") or {}
    cut = (today - timedelta(days=90)).isoformat()
    return [(fd, acc, doc) for form, fd, acc, doc in zip(r.get("form") or [], r.get("filingDate") or [], r.get("accessionNumber") or [], r.get("primaryDocument") or [])
            if form == "4" and fd >= cut]


def parse_form4(xml_bytes):
    """[(date, code, shares, price, acquired/disposed, owner, role)] - non-derivative transactions."""
    root = ET.fromstring(xml_bytes)
    owner = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "").strip()
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    role = []
    if rel is not None:
        if (rel.findtext("isDirector") or "").strip() in ("1", "true"):
            role.append("director")
        if (rel.findtext("isOfficer") or "").strip() in ("1", "true"):
            role.append((rel.findtext("officerTitle") or "officer").strip())
        if (rel.findtext("isTenPercentOwner") or "").strip() in ("1", "true"):
            role.append("10% owner")
    out = []
    for t in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = (t.findtext("transactionCoding/transactionCode") or "").strip()
        d = (t.findtext("transactionDate/value") or "").strip()[:10]
        try:
            sh = float(t.findtext("transactionAmounts/transactionShares/value") or 0)
            px = float(t.findtext("transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        ad = (t.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or "").strip()
        out.append({"date": d, "code": code, "shares": sh, "price": px, "ad": ad, "owner": owner, "role": ", ".join(role)})
    return out


def insider_summary(tx, today):
    cut = (today - timedelta(days=90)).isoformat()
    tx = [t for t in tx if t.get("date", "") >= cut]
    buys = [t for t in tx if t["code"] == "P"]
    sells = [t for t in tx if t["code"] == "S"]
    bv = sum(t["shares"] * t["price"] for t in buys)
    sv = sum(t["shares"] * t["price"] for t in sells)
    c30 = (today - timedelta(days=30)).isoformat()
    recent_buyers = {t["owner"] for t in buys if t["date"] >= c30}
    return {"buyValue": round(bv), "sellValue": round(sv), "net": round(bv - sv), "buyers": len({t["owner"] for t in buys}),
            "sellers": len({t["owner"] for t in sells}), "cluster": len(recent_buyers) >= 3,
            "ceoCfoBuy": any(re.search(r"\b(CEO|Chief Executive|CFO|Chief Financial)\b", t["role"], re.I) for t in buys)}


def facts_from(cf):
    """Compact as-filed series per concept: annual (10-K, ~1-year periods) and quarterly (10-Q, ~3-month)."""
    us = ((cf or {}).get("facts") or {}).get("us-gaap") or {}
    out = {}
    for key, names in CONCEPTS.items():
        for n in names:
            units = (us.get(n) or {}).get("units") or {}
            vals = units.get("USD") or units.get("USD/shares") or units.get("shares") or []
            if not vals:
                continue
            ann, qtr, seen = {}, {}, set()
            for v in vals:
                end, start, form = v.get("end"), v.get("start"), v.get("form") or ""
                if not end:
                    continue
                k = (end, start)
                if start:
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                    bucket = ann if (330 <= days <= 400 and form.startswith("10-K")) else qtr if (80 <= days <= 100 and form.startswith("10-Q")) else None
                else:
                    bucket = ann if form.startswith("10-K") else qtr if form.startswith("10-Q") else None
                if bucket is None:
                    continue
                prev = bucket.get(end)
                if prev is None or (v.get("filed") or "") >= prev["filed"]:      # the latest filing's figure for that period
                    bucket[end] = {"end": end, "val": v.get("val"), "fy": v.get("fy"), "fp": v.get("fp"), "form": form, "filed": v.get("filed") or ""}
            if ann or qtr:
                out[key] = {"concept": n, "annual": sorted(ann.values(), key=lambda x: x["end"])[-10:], "quarterly": sorted(qtr.values(), key=lambda x: x["end"])[-12:]}
                break
    return out


def universe():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fetch_headcount import universe as hc_universe
    return hc_universe()


def main():
    if not UA or "@" not in UA:
        log("SEC_USER_AGENT not set - skipped")
        return 0
    today = date.today()
    uni = universe()
    cmap = get("https://www.sec.gov/files/company_tickers.json")
    cik = {str(v["ticker"]).upper().replace(".", "-"): int(v["cik_str"]) for v in cmap.values()}
    tick = [t for t in uni if t in cik]
    hot = current_ciks("8-K") | current_ciks("4")
    old = {t: read(OUT / f"{t}.json") or {} for t in tick}
    first = [t for t in tick if cik[t] in hot]
    rest = sorted([t for t in tick if t not in first], key=lambda t: (old[t].get("secAt") or "", t))
    order = (first + rest)[:PER_RUN]
    log(f"{len(tick)} companies with a CIK · {len(first)} with new 8-K / Form 4 filings first · {len(order)} this run")
    f4_budget, facts_budget = FORM4_DOCS, FACTS_PER_RUN
    facts_order = set(sorted(tick, key=lambda t: (old[t].get("factsAt") or "", t))[:FACTS_PER_RUN])
    for t in order:
        c = cik[t]
        doc = dict(old[t]) or {"ticker": t}
        doc.update(ticker=t, cik=c)
        try:
            sub = get(f"https://data.sec.gov/submissions/CIK{c:010d}.json")
        except Exception as e:
            log(f"{t}: submissions {e}")
            continue
        doc["events"] = events_from(sub, today)
        seen = dict(doc.get("form4Seen") or {})
        tx = [x for x in doc.get("insiderTx") or [] if x.get("date", "") >= (today - timedelta(days=120)).isoformat()]
        for fd, acc, pdoc in form4_list(sub, today):
            if acc in seen or f4_budget <= 0:
                continue
            xml_name = pdoc.split("/")[-1]                          # the raw XML, not the xsl-rendered view
            try:
                rows = parse_form4(get(f"https://www.sec.gov/Archives/edgar/data/{c}/{acc.replace('-', '')}/{xml_name}", raw=True))
                tx += [dict(r, acc=acc) for r in rows]
                seen[acc] = fd
            except Exception:
                seen[acc] = fd                                       # unreadable once = skipped next time
            f4_budget -= 1
        doc["form4Seen"] = {k: v for k, v in seen.items() if v >= (today - timedelta(days=120)).isoformat()}
        doc["insiderTx"] = sorted(tx, key=lambda x: x.get("date", ""))[-80:]
        doc["insider"] = insider_summary(doc["insiderTx"], today)
        if t in facts_order and facts_budget > 0:
            try:
                doc["facts"] = facts_from(get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c:010d}.json"))
                doc["factsAt"] = now_iso()
            except Exception as e:
                log(f"{t}: facts {e}")
            facts_budget -= 1
        doc["secAt"] = now_iso()
        write(OUT / f"{t}.json", doc)
        old[t] = doc
    idx = {}
    c30 = (today - timedelta(days=30)).isoformat()
    for t, doc in old.items():
        if not doc.get("secAt"):
            continue
        ev = doc.get("events") or []
        recent = [e for e in ev if e["date"] >= c30]
        idx[t] = {"events30": len(recent), "red30": sorted({i for e in recent for i in e["red"]}), "caution30": sorted({i for e in recent for i in e["caution"]}),
                  "lastEvent": ({"date": ev[0]["date"], "labels": ev[0]["labels"]} if ev else None), "insider": doc.get("insider"),
                  "hasFacts": bool(doc.get("facts")), "secAt": doc.get("secAt")}
    write(OUT / "index.json", {"asOf": now_iso(), "tickers": idx, "items": ITEMS, "red": sorted(RED), "caution": sorted(CAUTION)})
    flagged = [t for t, v in idx.items() if v["red30"]]
    log(f"index: {len(idx)} companies · red flags in the last 30 days: {', '.join(flagged[:20]) or 'none'} · Form 4 reads left {f4_budget} · facts left {facts_budget}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
