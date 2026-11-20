#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/fetch_sec.py
#  z197 - facts queue: Business Web names first; unreadable companies (ETFs: no XBRL facts) stamped so they stop
#         jumping the queue every run.
#  z198 - five money-structure items (RPO / backlog, purchase obligations, equity-method, listed and private equity
#         stakes); FACTS_VERSION 3 re-reads every company over ~a week, Business Web names first.
#  z199 - WHO OWNS 5 %+ OF EVERY COMPANY, from the filings: Schedule 13D / 13G (structured XML since Dec 2024) filed
#         about each company - each reporting person's name, CIK (13D), percent of the class, shares, type (IA / CO /
#         IN …), the 13G rule (13d-1(b) institution / (c) passive / (d) pre-IPO), the event date, and a 13D's stated
#         purpose (activists say what they want) -> holders. A company's own submissions also list the schedules IT
#         filed about others -> stakes (NVIDIA's 7 % of CoreWeave). Each filing read once (s13Seen), even when two docs
#         list it; the unread queue lives in each doc (s13Pend) and drains run after run - Business Web names first, then
#         every company's newest before anyone's second. Mass filers' own portfolio filings (asset managers, banks) are
#         skipped. Plus the company's SEC profile from the same submissions read: SIC industry, state, fiscal year end,
#         former names. A self-test gates it (tests/test_sec.py; a failure sets SEC_SCHED13_DOCS=0 for that run).
#         CORPORATE 13F: a company that files 13F-HR as a manager (NVIDIA, Alphabet, Amazon, Berkshire) - its newest
#         quarter's information table, read once per filing (cover first: an institution-size table, > 300 entries, is
#         recorded as skipped), holdings per CUSIP (value, shares, class; options apart) -> doc.f13. CUSIP <-> ticker from
#         the schedules -> data/sec/cusips.json (13F holdings, ours and TRAPP2-1's, are keyed by CUSIP).
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
#  z187 - facts are CURRENT: every alternative name of a line item is read and merged by period (Apple's revenue was
#               "Revenues" to 2018 and RevenueFromContractWithCustomer... since - the table showed FY2015-2018 as the
#               latest years; UNH / V / QCOM equity stopped years ago the same way), the quarters a 10-Q only reports
#               year-to-date (cash flow, every Q4) are derived from the YTD figures, and each flow item carries its
#               trailing-12-month total (ttm) and each balance item its latest value. Companies filed before z187 refresh first.
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
SCHED13_DOCS = int(os.environ.get("SEC_SCHED13_DOCS", "600"))          # z199: 13D / 13G XML reads per run
SCHED13_DAYS = 500                                                      # filings older than this aren't read
SCHED13_PER_CO = 40                                                     # unread filings kept queued per company (s13Pend)
SCHED13_FORMS = {"SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}
SCHED13_MASS = 60                    # a company that files more of its own than this is an institution reporting its portfolio
SCHED13_ROWS = 15                    # holders / stakes kept per company (the card shows 15)
SCHED13_KEEP = 40                    # filings kept per company and side (s13 / s13out), newest
F13_DOCS = int(os.environ.get("SEC_F13_DOCS", "60"))                   # z199: corporate 13F reads per run (3 requests each)
F13_MAX_ROWS = 300                   # a longer information table is an institution's client portfolio - not read
F13_KEEP = 120                       # holdings kept per filer (largest by value)

ITEMS = {"1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy / receivership", "1.04": "mine safety",
         "1.05": "cybersecurity incident", "2.01": "acquisition / disposition completed", "2.02": "results of operations",
         "2.03": "new debt obligation", "2.04": "debt acceleration", "2.05": "restructuring / exit costs", "2.06": "material impairment",
         "3.01": "delisting notice", "3.02": "unregistered equity sale", "3.03": "holders' rights modified", "4.01": "auditor change",
         "4.02": "non-reliance on past financials", "5.01": "change in control", "5.02": "officer / director change",
         "5.03": "bylaws amended", "5.07": "shareholder vote", "7.01": "Reg FD disclosure", "8.01": "other events", "9.01": "exhibits"}
RED = {"1.03", "4.02", "3.01", "4.01", "2.04", "2.06"}
CAUTION = {"5.02", "2.05", "1.05", "5.01"}
# z187: every name a company may file the line item under, in priority order - merged by period, so a company that
# switched names (ASC 606 moved most revenue to RevenueFromContractWithCustomer... in 2018) keeps a current series
CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet", "RevenuesNetOfInterestExpense", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
    "netIncome": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
    "epsDiluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
    "operatingIncome": ["OperatingIncomeLoss"],
    "operatingCashFlow": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"],
    "longTermDebt": ["LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "dilutedShares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    # z198 - THE BUSINESS WEB, P1: the money structure behind the relationships (owner 11-19: "how much of a company is
    # tied into another … understood for their profits and their debts"). Non-dimensional us-gaap facts only - the
    # companyfacts API carries no axis members, so customer concentration (ConcentrationRiskPercentage by customer)
    # needs the filing itself (a later build).
    "rpo": ["RevenueRemainingPerformanceObligation"],                       # contracted backlog not yet recognised
    "purchaseObligations": ["PurchaseObligation", "UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount"],
    "equityMethod": ["EquityMethodInvestments"],                            # stakes with significant influence
    "equitySecurities": ["EquitySecuritiesFvNi", "MarketableSecuritiesEquitySecurities"],   # listed stakes at fair value
    "equityPrivate": ["EquitySecuritiesWithoutReadilyDeterminableFairValueAmount"]}          # private stakes (measurement alternative)
FLOW_USD = {"revenue", "netIncome", "operatingIncome", "operatingCashFlow", "capex"}    # quarters derivable from YTD
INSTANT = {"cash", "longTermDebt", "equity", "rpo", "purchaseObligations", "equityMethod", "equitySecurities", "equityPrivate"}
SHORT_KEEP = {"rpo", "purchaseObligations", "equityMethod", "equitySecurities", "equityPrivate"}   # z198: latest + a year of trend
FACTS_VERSION = 3          # z198: +the five money-structure items -> every company re-reads (Business Web names first)


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


def sched13_list(sub, today, days=SCHED13_DAYS):
    """z199: the structured Schedule 13D / 13G filings about this company (newest first): [(filed, acc, doc, form)]."""
    r = (sub.get("filings") or {}).get("recent") or {}
    cut = (today - timedelta(days=days)).isoformat()
    rows = [(fd, acc, doc, form) for form, fd, acc, doc in zip(r.get("form") or [], r.get("filingDate") or [], r.get("accessionNumber") or [], r.get("primaryDocument") or [])
            if form in SCHED13_FORMS and fd >= cut]
    return sorted(rows, reverse=True)


def _ln(tag):
    return str(tag).rsplit("}", 1)[-1]


def _xfirst(el, *names):
    for x in el.iter():
        if _ln(x.tag) in names and (x.text or "").strip():
            return x.text.strip()
    return None


def _xnum(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _mdy(v):
    """MM/DD/YYYY (the schedules' format) or YYYY-MM-DD -> YYYY-MM-DD."""
    v = str(v or "").strip()
    try:
        if "/" in v:
            m, d, y = v.split("/")
            return date(int(y), int(m), int(d)).isoformat()
        return date.fromisoformat(v[:10]).isoformat()
    except Exception:
        return None


def parse_sched13(xml_bytes):
    """One Schedule 13D / 13G XML (the two schemas differ: 13G issuerCik / classPercent / reportingPerson-
    BeneficiallyOwnedAggregateNumberOfShares under coverPageHeaderReportingPersonDetails; 13D issuerCIK / percentOfClass /
    aggregateAmountOwned under reportingPersons/reportingPersonInfo, with the person's CIK and Item 4's purpose).
    -> {kind, amendment, issuerCik, issuerName, cusip, event, filerCik, persons: [{name, cik, pct, shares, type}], purpose}"""
    root = ET.fromstring(xml_bytes)
    form = (_xfirst(root, "submissionType") or "").upper()
    kind = "13D" if "13D" in form else "13G"
    persons = []
    for el in root.iter():
        if _ln(el.tag) not in ("coverPageHeaderReportingPersonDetails", "reportingPersonInfo"):
            continue
        name = _xfirst(el, "reportingPersonName")
        if not name:
            continue
        types = [(x.text or "").strip() for x in el.iter() if _ln(x.tag) == "typeOfReportingPerson" and (x.text or "").strip()]
        persons.append({"name": name[:120], "cik": (lambda c: int(c) if c and c.isdigit() else None)(_xfirst(el, "reportingPersonCIK")),
                        "pct": _xnum(_xfirst(el, "classPercent", "percentOfClass")),
                        "shares": _xnum(_xfirst(el, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares", "aggregateAmountOwned")),
                        "type": "/".join(types[:3]) or None})
    head = next((x for x in root.iter() if _ln(x.tag) == "headerData"), None)
    filer = _xfirst(head, "cik") if head is not None else None
    ic = _xfirst(root, "issuerCik", "issuerCIK")
    purpose = _xfirst(root, "transactionPurpose") if kind == "13D" else None
    # 13G: the rule it is filed under - 13d-1(b) a qualified institution holding in the ordinary course (client money),
    # 13d-1(c) a passive investor under 20 %, 13d-1(d) an exempt holder (owned before the company registered: pre-IPO)
    rules = sorted({re.sub(r"(?i)^rule\s*", "", (x.text or "").strip()) for x in root.iter()
                    if _ln(x.tag) == "designateRulePursuantThisScheduleFiled" and (x.text or "").strip()})
    return {"kind": kind, "amendment": form.endswith("/A"), "issuerCik": int(ic) if ic and ic.isdigit() else None,
            "issuerName": _xfirst(root, "issuerName"), "cusip": _xfirst(root, "issuerCusip", "issuerCUSIP"),
            "cls": (_xfirst(root, "securitiesClassTitle", "titleOfClassOfSecurities") or "")[:60] or None,
            "event": _mdy(_xfirst(root, "eventDateRequiresFilingThisStatement", "dateOfEvent")),
            "filerCik": int(filer) if filer and filer.isdigit() else None, "persons": persons,
            "rule": "/".join(rules) or None, "purpose": (purpose or "")[:280] or None}


def compact13(rec, filed=None, acc=None):
    """z199: what we keep of one filing - its lead reporting person (the largest stake in it) and how many filed together."""
    ps = [p for p in rec.get("persons") or [] if p.get("name")]
    if not ps:
        return None
    lead = max(ps, key=lambda p: (p.get("pct") or 0))
    return {"kind": rec.get("kind"), "amend": bool(rec.get("amendment")), "event": rec.get("event"), "filed": filed, "acc": acc,
            "cusip": rec.get("cusip"), "cls": rec.get("cls"),
            "filerCik": rec.get("filerCik"), "name": lead["name"], "cik": lead.get("cik") or rec.get("filerCik"),
            "pct": lead.get("pct"), "shares": lead.get("shares"), "type": lead.get("type"), "n": len(ps), "rule": rec.get("rule"),
            "purpose": rec.get("purpose") if rec.get("kind") == "13D" else None}


def self_filed(acc, cik):
    """The accession number starts with the filer's CIK when it files for itself (not through an agent)."""
    try:
        return int(str(acc).split("-")[0]) == int(cik)
    except (TypeError, ValueError):
        return False


def filed_by_it(sub, cik):
    """z199: the accessions of 13D / 13G filings this company filed about OTHER companies. Two tells: the accession starts
    with its own CIK (filed for itself, not through an agent), or the file number is another subject's - a schedule's
    file number (005-…) belongs to the company it is about, so the one most of its schedules share is its own."""
    r = (sub.get("filings") or {}).get("recent") or {}
    forms, accs, fns = r.get("form") or [], r.get("accessionNumber") or [], r.get("fileNumber") or []
    rows = [(a, (fns[i] if i < len(fns) else "") or "") for i, (f, a) in enumerate(zip(forms, accs)) if f in SCHED13_FORMS]
    counts = {}
    for _, fn in rows:
        if fn.startswith("005-"):
            counts[fn] = counts.get(fn, 0) + 1
    mine = max(counts, key=counts.get) if counts and max(counts.values()) >= 2 else None
    return {a for a, fn in rows if self_filed(a, cik) or (mine and fn.startswith("005-") and fn != mine)}


def queue13(sub, today, cik, seen):
    """z199: this company's unread 13D / 13G filings (about it, or filed by it about others), newest first, capped. A mass
    filer's own filings (an asset manager / bank reporting its clients' portfolio: more than SCHED13_MASS of them) are
    left out - not strategic stakes, and they'd eat the budget. -> ([[filed, acc, doc, form]], mass filer?)"""
    rows = sched13_list(sub, today)
    by_it = filed_by_it(sub, cik)
    own = {r[1] for r in rows if r[1] in by_it}
    mass = len(own) > SCHED13_MASS
    if mass:
        rows = [r for r in rows if r[1] not in own]
    return [[fd, acc, pdoc, form] for fd, acc, pdoc, form in rows if acc not in seen][:SCHED13_PER_CO], mass


def holders_from(records):
    """z199: the current 5 %+ holders from the 13D / 13G filings on file: one row per holder and class (Alphabet's A and C
    shares are filed separately), its latest filing wins (an amendment to below 5 % keeps the holder at its new, lower
    stake - the exit is information too). -> rows by stake."""
    best = {}
    for r in records or []:
        if not r or not r.get("name"):
            continue
        key = (r.get("filerCik") or r.get("cik") or r["name"].upper(), r.get("cusip") or "")
        prev = best.get(key)
        if prev is None or (r.get("filed") or "") > (prev.get("filed") or ""):
            best[key] = r
    return sorted(({k: v for k, v in r.items() if k not in ("amend", "filerCik") and v is not None} for r in best.values()),
                  key=lambda x: -(x.get("pct") or 0))[:SCHED13_ROWS]


def stakes_from(records):
    """z199: the 5 %+ stakes this company holds in OTHER companies - the schedules it filed (its own submissions list
    them as well as the ones filed about it): one row per issuer and class, the latest filing wins. -> rows by stake."""
    best = {}
    for r in records or []:
        if not r or not r.get("issuerCik"):
            continue
        key = (r["issuerCik"], r.get("cusip") or "")
        prev = best.get(key)
        if prev is None or (r.get("filed") or "") > (prev.get("filed") or ""):
            best[key] = r
    return sorted(({k: v for k, v in r.items() if k not in ("amend", "filerCik") and v is not None} for r in best.values()),
                  key=lambda x: -(x.get("pct") or 0))[:SCHED13_ROWS]


def _cusips(v):
    """The 9-character CUSIPs in a filing's CUSIP field (some list two classes, some add spaces or dashes)."""
    v = str(v or "").upper()
    flat = re.sub(r"[\s-]", "", v)
    if len(flat) == 9 and flat.isalnum():
        return [flat]
    return sorted({x for x in re.split(r"[^0-9A-Z]+", v) if len(x) == 9})


def cusip_map(docs, cik):
    """z199: CUSIP <-> ticker from the schedules - every 13D / 13G names its issuer's CIK and CUSIP, so the companies
    with a 5 % holder on file get their CUSIPs for free (what 13F holdings are keyed by; TRAPP2-1's 13F index has CUSIPs
    and no tickers). A CIK with two listed classes (GOOG / GOOGL) maps to both - the class title says which.
    -> {"byCusip": {cusip: ticker | [tickers]}, "byTicker": {ticker: [cusips]}, "classes": {cusip: title}}"""
    by_cik = {}
    for t, c in (cik or {}).items():
        by_cik.setdefault(c, set()).add(t)
    by, cls = {}, {}
    for t, d in docs.items():
        for side, key in (("s13", None), ("s13out", "issuerCik")):
            for r in d.get(side) or []:
                ts = by_cik.get(r.get(key)) if key else (by_cik.get(cik.get(t)) or {t})
                for cu in _cusips(r.get("cusip")) if ts else ():
                    by.setdefault(cu, set()).update(ts)
                    if r.get("cls"):
                        cls[cu] = r["cls"]
    by_t = {}
    for cu, ts in by.items():
        for t in ts:
            by_t.setdefault(t, set()).add(cu)
    return {"byCusip": {cu: (sorted(ts)[0] if len(ts) == 1 else sorted(ts)) for cu, ts in sorted(by.items())},
            "byTicker": {t: sorted(v) for t, v in sorted(by_t.items())}, "classes": dict(sorted(cls.items()))}


def profile_from(sub):
    """z199: what the SEC knows about the company itself (the submissions read we already make)."""
    fn = [{"name": f.get("name"), "to": str(f.get("to") or "")[:10]} for f in (sub.get("formerNames") or []) if f.get("name")]
    return {k: v for k, v in {"sic": sub.get("sic") or None, "sicDescription": sub.get("sicDescription") or None,
                              "category": sub.get("category") or None, "fiscalYearEnd": sub.get("fiscalYearEnd") or None,
                              "stateOfIncorporation": sub.get("stateOfIncorporation") or None, "entityType": sub.get("entityType") or None,
                              "exchanges": [x for x in (sub.get("exchanges") or []) if x] or None,
                              "formerNames": fn[:4] or None}.items() if v}


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


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _concept_points(us, n):
    """[(start|None, end, val, form, fy, fp, filed)] for one concept, the latest filing's figure per (start, end)."""
    units = (us.get(n) or {}).get("units") or {}
    vals = units.get("USD") or units.get("USD/shares") or units.get("shares") or []
    best = {}
    for v in vals:
        end, start, form = v.get("end"), v.get("start"), v.get("form") or ""
        if not end or v.get("val") is None or not (form.startswith("10-K") or form.startswith("10-Q")):
            continue
        k = (start, end)
        if k not in best or (v.get("filed") or "") >= best[k][6]:
            best[k] = (start, end, v.get("val"), form, v.get("fy"), v.get("fp"), v.get("filed") or "")
    return list(best.values())


def facts_from(cf):
    """Compact as-filed series per line item: annual (10-K, ~1-year periods) and quarterly (~3-month). z187: all the
    item's concept names merged by period (priority order breaks ties), quarters a 10-Q reports only year-to-date
    derived from the YTD figures (Q2 = H1 - Q1, Q3 = 9M - H1, Q4 = FY - 9M; marked derived), plus ttm / latest."""
    us = ((cf or {}).get("facts") or {}).get("us-gaap") or {}
    out = {}
    for key, names in CONCEPTS.items():
        ann, qtr, ytd, used = {}, {}, {}, {}
        for rank, n in enumerate(names):
            pts = _concept_points(us, n)
            for start, end, val, form, fy, fp, filed in pts:
                rec = {"end": end, "val": val, "fy": fy, "fp": fp, "form": form, "filed": filed}
                if start:
                    try:
                        days = _days(start, end)
                    except ValueError:
                        continue
                    if 330 <= days <= 400 and form.startswith("10-K"):
                        bucket = ann
                    elif 80 <= days <= 100:
                        bucket = qtr
                    elif 170 <= days <= 290 and form.startswith("10-Q"):
                        ytd.setdefault(start, {}).setdefault(end, (val, filed))   # 6- / 9-month year-to-date
                        continue
                    else:
                        continue
                    if key in FLOW_USD and bucket is ann:
                        ytd.setdefault(start, {}).setdefault(end, (val, filed))   # the full year closes the chain
                    if key in FLOW_USD and bucket is qtr:
                        ytd.setdefault(start, {}).setdefault(end, (val, filed))  # Q1 opens it
                else:
                    bucket = ann if form.startswith("10-K") else qtr
                prev = bucket.get(end)
                # higher-priority concept wins a period it covers; within one concept the latest filing wins
                if prev is None or used.get((id(bucket), end), 99) > rank or (used.get((id(bucket), end)) == rank and filed >= prev["filed"]):
                    bucket[end] = rec
                    used[(id(bucket), end)] = rank
                    used.setdefault(("concept", end), n)
        if key in FLOW_USD:                                                    # fill quarters from year-to-date figures
            for start, chain in ytd.items():
                ends = sorted(chain)
                prev_end, prev_val = None, None
                for e in ends:
                    val, filed = chain[e]
                    try:
                        span = _days(start, e)
                    except ValueError:
                        break
                    if prev_end is None:
                        if 80 <= span <= 100:
                            prev_end, prev_val = e, val
                            continue
                        break                                                 # no Q1 for this year - can't difference
                    gap = _days(prev_end, e)
                    if 80 <= gap <= 100 and e not in qtr and val is not None and prev_val is not None:
                        qtr[e] = {"end": e, "val": val - prev_val, "fy": None, "fp": None, "form": "derived (YTD)", "filed": filed,
                                  "derived": True}
                    if 80 <= gap <= 100:
                        prev_end, prev_val = e, val
                    else:
                        break
        if not (ann or qtr):
            continue
        a = sorted(ann.values(), key=lambda x: x["end"])[-(4 if key in SHORT_KEEP else 10):]
        q = sorted(qtr.values(), key=lambda x: x["end"])[-(4 if key in SHORT_KEEP else 12):]
        latest_end = max([x["end"] for x in a] + [x["end"] for x in q])
        doc = {"concept": used.get(("concept", latest_end)) or names[0], "annual": a, "quarterly": q}
        if key in FLOW_USD and len(q) >= 4:
            last4 = q[-4:]
            try:
                span = _days(last4[0]["end"], last4[-1]["end"])
            except ValueError:
                span = 0
            if 250 <= span <= 300 and all(x.get("val") is not None for x in last4):
                doc["ttm"] = {"end": last4[-1]["end"], "val": sum(x["val"] for x in last4)}
        if key in INSTANT:
            pts = a + q
            last = max(pts, key=lambda x: x["end"])
            doc["latest"] = {"end": last["end"], "val": last["val"]}
        out[key] = doc
    return out


def read_sched13(pend, docs, cik, today, prio, budget, fetch=None):
    """z199: read the pending 13D / 13G XMLs and fold them into each company's holders. Order: Business Web names first,
    then breadth before depth - every company's newest unread filing before anyone's second - newest first within each
    round. A filing that can't be read is marked seen (skipped next time). The queue lives in each doc (s13Pend), so a
    company's backlog drains run after run without waiting for its submissions to be re-read. -> number read."""
    fetch = fetch or (lambda url: get(url, raw=True))
    q = sorted((t not in prio, k, -int(fd.replace("-", "")), t, fd, acc, pdoc, form)
               for t, rows in pend.items() for k, (fd, acc, pdoc, form) in enumerate(sorted(rows, reverse=True)) if fd)
    n, touched, got, empty = 0, set(), {}, 0
    for _, _, _, t, fd, acc, pdoc, form in q:
        doc = docs.get(t)
        if doc is None:
            continue
        c = cik.get(t) if isinstance(cik, dict) else None
        if acc not in got:                                   # GOOG and GOOGL (filer and subject too) list the same filing: read it once
            if n >= budget:
                continue
            try:
                rec = parse_sched13(fetch(f"https://www.sec.gov/Archives/edgar/data/{c}/{acc.replace('-', '')}/{pdoc.split('/')[-1]}"))
                rec.update(kind="13D" if "13D" in form else "13G", amendment=form.endswith("/A"))   # the index's form type rules
                got[acc] = (rec.get("issuerCik"), rec.get("issuerName"), compact13(rec, fd, acc))
                empty += got[acc][2] is None
            except Exception as e:
                log(f"{t}: {form} {acc}: {type(e).__name__}")
                got[acc] = (None, None, None)                # unreadable once = skipped next time
            n += 1
        ic, iname, r = got[acc]
        if r and ic and c and ic != c:                       # filed BY this company about another: a stake it holds
            if not any(x.get("acc") == acc for x in doc.get("s13out") or []):
                doc.setdefault("s13out", []).append(dict(r, issuerCik=ic, issuer=(iname or "")[:120] or None))
        elif r and not any(x.get("acc") == acc for x in doc.get("s13") or []):
            doc.setdefault("s13", []).append(dict(r))
        doc.setdefault("s13Seen", {})[acc] = fd
        touched.add(t)
    cut = (today - timedelta(days=SCHED13_DAYS)).isoformat()
    for t in touched:
        doc = docs[t]
        doc["s13Seen"] = {k: v for k, v in doc.get("s13Seen", {}).items() if v >= cut}
        doc["s13Pend"] = [x for x in doc.get("s13Pend") or [] if x[1] not in doc["s13Seen"] and x[0] >= cut]
        doc["s13"] = sorted([r for r in doc.get("s13") or [] if (r.get("filed") or "") >= cut], key=lambda r: r.get("filed") or "")[-SCHED13_KEEP:]
        doc["holders"] = holders_from(doc["s13"])
        if doc.get("s13out") is not None:
            doc["s13out"] = sorted([r for r in doc["s13out"] if (r.get("filed") or "") >= cut], key=lambda r: r.get("filed") or "")[-SCHED13_KEEP:]
            doc["stakes"] = stakes_from(doc["s13out"])
        doc["holdersAt"] = now_iso()
        write(OUT / f"{t}.json", doc)
    if empty:                                                # a schema change shows here, not as silently empty holders
        log(f"13D/13G: {empty} of {n} filings read had no reporting persons - check the XML element names")
    return n


# ---- z199: corporate 13F - the stakes a company reports as an institutional manager (NVIDIA, Alphabet, Amazon,
# Berkshire file 13F-HR): every listed equity it holds at quarter end, under 5 % too. Keyed by CUSIP - mapped to tickers
# with data/sec/cusips.json in the web build. Institutions (thousands of rows) are skipped by table size.
def f13_latest(sub, today, days=200):
    """The company's newest 13F-HR (a 13F is listed only under the manager that files it) -> (filed, acc, doc) or None."""
    r = (sub.get("filings") or {}).get("recent") or {}
    cut = (today - timedelta(days=days)).isoformat()
    rows = [(fd, acc, doc) for form, fd, acc, doc in zip(r.get("form") or [], r.get("filingDate") or [], r.get("accessionNumber") or [],
                                                         r.get("primaryDocument") or []) if form == "13F-HR" and fd >= cut]
    return max(rows) if rows else None


def _md_y(v):
    """13F's MM-DD-YYYY -> YYYY-MM-DD."""
    try:
        m, d, y = str(v).strip().split("-")
        return date(int(y), int(m), int(d)).isoformat()
    except Exception:
        return _mdy(v)


def parse_f13_cover(xml_bytes):
    """The 13F-HR cover / summary page -> {period, entries, total, amendment}."""
    root = ET.fromstring(xml_bytes)
    n = _xnum(_xfirst(root, "tableEntryTotal"))
    return {"period": _md_y(_xfirst(root, "reportCalendarOrQuarter", "periodOfReport")), "entries": int(n) if n is not None else None,
            "total": _xnum(_xfirst(root, "tableValueTotal")), "amendment": (_xfirst(root, "isAmendment") or "").lower() == "true"}


def f13_info_name(index_json):
    """The information table's file name in a filing's index.json (its name varies; it is the XML that isn't the cover)."""
    items = ((index_json or {}).get("directory") or {}).get("item") or []
    xmls = [str(i.get("name") or "") for i in items if str(i.get("name") or "").lower().endswith(".xml")]
    xmls = [x for x in xmls if "primary_doc" not in x.lower()]
    pick = [x for x in xmls if any(k in x.lower() for k in ("info", "table", "13f"))] or xmls
    return pick[0] if pick else None


def parse_f13_table(xml_bytes):
    """The 13F information table -> holdings per CUSIP (rows split across managers / discretion summed); share positions
    and options kept apart. -> [{cusip, name, cls, value, shares, putCall}] largest value first."""
    root = ET.fromstring(xml_bytes)
    agg = {}
    for el in root.iter():
        if _ln(el.tag) != "infoTable":
            continue
        cu = (_xfirst(el, "cusip") or "").upper().replace(" ", "")
        if len(cu) != 9:
            continue
        pc = (_xfirst(el, "putCall") or "").upper() or None
        typ = (_xfirst(el, "sshPrnamtType") or "SH").upper()
        if typ != "SH" and not pc:
            continue                                         # principal amounts (bonds, notes) - not equity
        k = (cu, pc)
        h = agg.setdefault(k, {"cusip": cu, "name": (_xfirst(el, "nameOfIssuer") or "")[:80], "cls": (_xfirst(el, "titleOfClass") or "")[:40],
                               "value": 0.0, "shares": 0.0, "putCall": pc})
        h["value"] += _xnum(_xfirst(el, "value")) or 0.0
        h["shares"] += _xnum(_xfirst(el, "sshPrnamt")) or 0.0
    return sorted(agg.values(), key=lambda h: -h["value"])


def read_f13(pend, docs, cik, prio, budget, fetch=None, fetch_json=None):
    """z199: read the pending corporate 13F-HRs (web names first, newest first): the cover first - an institution-size
    table is recorded as skipped, not read - then the information table. -> number of filings read."""
    fetch = fetch or (lambda url: get(url, raw=True))
    fetch_json = fetch_json or (lambda url: get(url))
    n = 0
    for _, _, t, (fd, acc, pdoc) in sorted((t not in prio, -int(v[0].replace("-", "")), t, v) for t, v in pend.items()):
        if n >= budget:
            break
        doc, c = docs.get(t), (cik or {}).get(t)
        if doc is None or not c:
            continue
        base = f"https://www.sec.gov/Archives/edgar/data/{c}/{acc.replace('-', '')}"
        rec = {"acc": acc, "filed": fd}
        try:
            cov = parse_f13_cover(fetch(f"{base}/{pdoc.split('/')[-1]}"))
            rec.update(period=cov["period"], entries=cov["entries"])
            if (cov["entries"] or 0) > F13_MAX_ROWS:
                rec["skipped"] = f"{cov['entries']} entries - an institution's portfolio"
            else:
                name = f13_info_name(fetch_json(f"{base}/index.json"))
                hold = parse_f13_table(fetch(f"{base}/{name}")) if name else []
                rec.update(total=round(sum(h["value"] for h in hold)), n=len(hold),
                           holdings=[{k: (round(v) if k in ("value", "shares") else v) for k, v in h.items() if v not in (None, "")} for h in hold[:F13_KEEP]])
        except Exception as e:
            log(f"{t}: 13F-HR {acc}: {type(e).__name__}")
            rec["error"] = type(e).__name__
        doc["f13"] = rec
        doc["f13Acc"] = acc                                  # read once per quarter's filing, whatever happened
        write(OUT / f"{t}.json", doc)
        n += 1
    return n


def web_priority(root=None):
    """z197: the companies the Business Web leans on (config/web_relationships.json edges + config/ipo_watch.json) refresh
    pre-z187 facts first - money-flow ratios shouldn't wait a week behind the alphabet."""
    cfg = Path(root or ROOT) / "config"
    out = set()
    try:
        for e in json.loads((cfg / "web_relationships.json").read_text()).get("edges") or []:
            out.update(str(e.get(k) or "").upper() for k in ("from", "to"))
    except Exception:
        pass
    try:
        out.update(str(t).upper() for t in (json.loads((cfg / "ipo_watch.json").read_text()).get("ipos") or {}))
    except Exception:
        pass
    out.discard("")
    return out


def facts_queue(tick, old, prio, n):
    """z197: which companies re-read XBRL facts this run. Pre-z187 docs first (Business Web names first among them), then
    the oldest read. A company whose facts can't be read (an ETF / trust with no XBRL facts -> 404) is stamped when it
    fails (factsErr), so it waits its turn instead of jumping the queue every run - from 11-16 to 11-19 SPY, DIA and the
    other never-read funds took most of the 150 reads each run and MSFT / AVGO / AMD / META still carried 2010-18 revenue."""
    def key(t):
        d = old.get(t) or {}
        done = (d.get("factsV") or 0) >= FACTS_VERSION
        return (done, done or t not in prio, d.get("factsAt") or "", t)
    return set(sorted(tick, key=key)[:n])


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
    mass13 = []                                                              # z199: mass filers (institutions)
    f13pend = {}                                                             # z199: ticker -> its newest unread 13F-HR
    # z187: companies whose facts were read before the concept merge (stale series) refresh first, then the oldest
    # z197: Business Web names first among them; unreadable ones stamped (see facts_queue)
    facts_order = facts_queue(tick, old, web_priority(), FACTS_PER_RUN)
    order = order + [t for t in tick if t in facts_order and t not in set(order)]
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
        try:
            doc["profile"] = profile_from(sub)                                # z199
        except Exception:
            pass
        doc["s13Pend"], mass = queue13(sub, today, c, doc.get("s13Seen") or {})
        if mass:
            mass13.append(t)
        nf = f13_latest(sub, today)                          # z199: a corporate 13F, once per quarter's filing
        if nf and nf[1] != doc.get("f13Acc"):
            f13pend[t] = nf
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
                doc["factsV"] = FACTS_VERSION
                doc.pop("factsErr", None)
            except Exception as e:
                log(f"{t}: facts {e}")
                doc["factsAt"] = now_iso()                    # z197: to the back of its queue, not the front every run
                if not doc.get("facts"):
                    doc["factsV"] = FACTS_VERSION             # nothing to keep fresh (ETF / trust): rotate with the read ones
                doc["factsErr"] = f"{type(e).__name__}: {e}"[:120]
            facts_budget -= 1
        doc["secAt"] = now_iso()
        write(OUT / f"{t}.json", doc)
        old[t] = doc
    # z199: Schedule 13D / 13G - Business Web names first, then the newest filings, within a budget
    pend13 = {t: [tuple(x) for x in d["s13Pend"]] for t, d in old.items() if d.get("s13Pend")}
    try:
        read13 = read_sched13(pend13, old, cik, today, web_priority(), SCHED13_DOCS)
    except Exception as e:                                   # never let the ownership read stop the index
        log(f"13D/13G read failed: {type(e).__name__}: {e}")
        read13 = 0
    try:                                                     # z199: corporate 13F holdings
        read_f13n = read_f13(f13pend, old, cik, web_priority(), F13_DOCS)
    except Exception as e:
        log(f"13F read failed: {type(e).__name__}: {e}")
        read_f13n = 0
    try:                                                     # z199: CUSIP <-> ticker for the 13F side
        cm = cusip_map(old, cik)
        write(OUT / "cusips.json", dict(asOf=now_iso(), note="from Schedule 13D / 13G filings: issuer CIK + CUSIP; a list = one CIK, several listed classes", **cm))
    except Exception as e:
        log(f"cusip map failed: {type(e).__name__}: {e}")
        cm = {"byCusip": {}}
    idx = {}
    c30 = (today - timedelta(days=30)).isoformat()
    for t, doc in old.items():
        if not doc.get("secAt"):
            continue
        ev = doc.get("events") or []
        recent = [e for e in ev if e["date"] >= c30]
        idx[t] = {"events30": len(recent), "red30": sorted({i for e in recent for i in e["red"]}), "caution30": sorted({i for e in recent for i in e["caution"]}),
                  "lastEvent": ({"date": ev[0]["date"], "labels": ev[0]["labels"]} if ev else None), "insider": doc.get("insider"),
                  "hasFacts": bool(doc.get("facts")), "secAt": doc.get("secAt"),
                  **{k: v for k, v in {"holders5": sum(1 for h in doc.get("holders") or [] if (h.get("pct") or 0) >= 5),
                                       "topHolder": (((doc.get("holders") or [{}])[0] or {}).get("name") or "")[:40],
                                       "stakes5": sum(1 for h in doc.get("stakes") or [] if (h.get("pct") or 0) >= 5),
                                       "f13n": len((doc.get("f13") or {}).get("holdings") or []),
                                       "sic": (doc.get("profile") or {}).get("sic")}.items() if v}}
    write(OUT / "index.json", {"asOf": now_iso(), "tickers": idx, "items": ITEMS, "red": sorted(RED), "caution": sorted(CAUTION)})
    flagged = [t for t, v in idx.items() if v["red30"]]
    log(f"index: {len(idx)} companies · red flags in the last 30 days: {', '.join(flagged[:20]) or 'none'} · Form 4 reads left {f4_budget} · facts left {facts_budget}"
        f" · 13D/13G read {read13} (queued {sum(len(d.get('s13Pend') or []) for d in old.values())}"
        f"{'; own portfolio filings skipped for ' + ', '.join(mass13[:12]) if mass13 else ''}) · CUSIPs mapped {len(cm.get('byCusip') or {})}"
        f" · 13F-HR read {read_f13n} of {len(f13pend)} ({', '.join(t for t in sorted(f13pend) if (old.get(t) or {}).get('f13', {}).get('holdings'))[:200] or 'none with a corporate-size table'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
