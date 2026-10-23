#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/fetch_headcount.py
#
#  z151 - HEADCOUNT. Every company's employee count over time, revenue per employee, and the
#  market's hiring breadth (how many companies are cutting vs growing - the labor side of a slowdown).
#  Sources, best first (each point keeps its source):
#    filing   - FMP's employee-count history (read from 10-K / 10-Q / 20-F), through the key proxy (no
#               key in this repo); quarterly where a company reports it quarterly (Meta, Alphabet…)
#    sec      - SEC XBRL frames, dei:EntityNumberOfEmployees (one request covers every filer for a period)
#    wikidata - Wikidata P1128 "employees" with its point-in-time date (mostly annual, from annual reports)
#    profile  - today's employee count on the books' quote pages, kept as a dated snapshot when it changes
#  Revenue per employee = trailing-12-month revenue at each headcount date (the books' quarterly income
#  statements; annual revenue when quarters are missing) ÷ employees.
#    -> data/headcount/<T>.json, data/headcount/index.json (compact, for the app and the bot),
#       data/headcount/summary.json (market breadth: now, by sector, by year)
# ============================================================
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from earnings_common import OUT as EOUT, num, now_iso, read, write

OUT = EOUT.parent / "headcount"
RAW = "https://raw.githubusercontent.com/TheMostLocal"
BOOKS = ("TRAPP2", "TRAPP2-1", "TRAPP2-2", "TRAPP2-3")
SEC_UA = os.environ.get("SEC_USER_AGENT") or "Valuatio research (github.com/TheMostLocal) valuatio@users.noreply.github.com"
PROXY = (os.environ.get("KEY_PROXY") or "").strip()
FMP_PER_RUN = int(os.environ.get("HEADCOUNT_FMP_PER_RUN", "150"))
PRIORITY = {"filing": 4, "sec": 3, "wikidata": 2, "profile": 1}


def log(*a):
    print("[headcount]", *a, flush=True)


def get_json(url, headers=None, timeout=60, data=None):
    req = urllib.request.Request(url, data=data, headers=dict({"User-Agent": "valuatio-headcount"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- universe
def universe():
    """{T: {name, sector, employees (profile), revenue (TTM), currency}} - S&P members + the books' US stocks."""
    out = {}
    for b in BOOKS:
        try:
            rows = get_json(f"{RAW}/{b}/main/data/master.json")
            rows = rows if isinstance(rows, list) else rows.get("rows") or []
            for x in rows:
                t = str(x.get("ticker") or "").upper()
                if not t or t.startswith("^") or t.endswith(("=X", "=F", "-USD")) or "." in t:
                    continue
                if str(x.get("asset_class") or "Equity") not in ("Equity", ""):
                    continue
                out.setdefault(t, {"name": x.get("name"), "sector": x.get("sector"), "book": b,
                                   "employees": num(x.get("employees")), "revenue": num(x.get("revenue")),
                                   "currency": x.get("financialCurrency") or x.get("currency") or "USD"})
        except Exception as e:
            log(f"{b} master: {e}")
    uni = read(EOUT / "universe.json", {}) or {}
    for m in ((uni.get("indices") or {}).get("SPY") or {}).get("members") or []:
        out.setdefault(m["ticker"], {"name": m.get("name"), "sector": m.get("sector"), "book": None})
        if not out[m["ticker"]].get("sector"):
            out[m["ticker"]]["sector"] = m.get("sector")
    return out


# ---------------------------------------------------------------- SEC frames
def sec_points(tickers):
    """{T: [(date, employees, form)]} from dei:EntityNumberOfEmployees frames, every filer per period."""
    out = {}
    try:
        cik_map = get_json("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": SEC_UA})
        by_cik = {int(v["cik_str"]): str(v["ticker"]).upper().replace(".", "-") for v in cik_map.values()}
    except Exception as e:
        log(f"SEC ticker map unavailable ({e}) - SEC skipped")
        return out
    cache = read(OUT / "_sec_frames.json", {}) or {}
    this_year = date.today().year
    periods = [f"CY{y}" for y in range(2012, this_year + 1)] + [f"CY{y}Q{q}I" for y in range(2012, this_year + 1) for q in (1, 2, 3, 4)]
    hits = 0
    for p in periods:
        y = int(p[2:6])
        if p in cache and y < this_year - 1:
            rows = cache[p]
        else:
            rows = []
            for unit in ("pure", "employee", "Employee"):
                try:
                    j = get_json(f"https://data.sec.gov/api/xbrl/frames/dei/EntityNumberOfEmployees/{unit}/{p}.json", headers={"User-Agent": SEC_UA})
                    rows = [[r.get("cik"), r.get("end") or r.get("instant"), r.get("val"), r.get("form") or ""] for r in j.get("data") or []]
                    break
                except Exception:
                    continue
            cache[p] = rows
            time.sleep(0.15)
        for cik, end, val, form in rows:
            t = by_cik.get(int(cik or 0))
            if t in tickers and val and end:
                out.setdefault(t, []).append((str(end)[:10], int(val), form))
                hits += 1
    write(OUT / "_sec_frames.json", cache)
    log(f"SEC frames: {hits} points for {len(out)} companies")
    return out


# ---------------------------------------------------------------- Wikidata
def wikidata_points(tickers):
    """{T: [(date, employees)]} - P1128 statements with their P585 point in time, via the QIDs the books'
    company-facts files already carry."""
    qid = {}
    for t, info in tickers.items():
        b = info.get("book")
        if not b:
            continue
        try:
            j = get_json(f"{RAW}/{b}/main/data/company/{urllib.parse.quote(t)}.json", timeout=20)
            if j.get("qid"):
                qid[j["qid"]] = t
        except Exception:
            pass
    out = {}
    q = list(qid)
    for i in range(0, len(q), 120):
        vals = " ".join(f"wd:{x}" for x in q[i:i + 120])
        sparql = f"""SELECT ?item ?emp ?when WHERE {{ VALUES ?item {{ {vals} }}
          ?item p:P1128 ?st . ?st ps:P1128 ?emp . OPTIONAL {{ ?st pq:P585 ?when }} }}"""
        try:
            j = get_json("https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(sparql),
                         headers={"User-Agent": "Valuatio headcount (github.com/TheMostLocal)", "Accept": "application/sparql-results+json"}, timeout=90)
            for b in j["results"]["bindings"]:
                item = b["item"]["value"].rsplit("/", 1)[-1]
                when = (b.get("when") or {}).get("value", "")[:10]
                emp = num(b["emp"]["value"])
                if item in qid and when and emp and emp > 0:
                    out.setdefault(qid[item], []).append((when, int(emp)))
        except Exception as e:
            log(f"Wikidata batch {i // 120 + 1}: {e}")
        time.sleep(1.0)
    log(f"Wikidata: {sum(len(v) for v in out.values())} points for {len(out)} companies ({len(qid)} with a QID)")
    return out


# ---------------------------------------------------------------- FMP through the key proxy
def fmp_points(order):
    """{T: [(date, employees, form)]} - the company's filings history, FMP_PER_RUN tickers a run (oldest
    first). Through the key proxy, so this repo holds no FMP key; skipped if the plan doesn't include it."""
    out = {}
    if not PROXY:
        log("FMP: KEY_PROXY not set - skipped")
        return out
    for n, t in enumerate(order[:FMP_PER_RUN]):
        rows = None
        for path in ("stable/historical-employee-count", "stable/employee-count"):
            url = f"https://financialmodelingprep.com/{path}?symbol={urllib.parse.quote(t)}&limit=120"
            try:
                rows = get_json(PROXY + "?u=" + urllib.parse.quote(url, safe=""), timeout=40)
                if isinstance(rows, list):
                    break
            except urllib.error.HTTPError as e:
                if e.code in (401, 402, 403) and n == 0:
                    log(f"FMP: {e.code} - employee counts are not on this FMP plan; skipped")
                    return out
            except Exception:
                pass
            rows = None
        for r in rows or []:
            dt = str(r.get("periodOfReport") or r.get("filingDate") or "")[:10]
            v = num(r.get("employeeCount"))
            if dt and v and v > 0:
                out.setdefault(t, []).append((dt, int(v), r.get("formType") or ""))
        time.sleep(0.4)
    log(f"FMP: {sum(len(v) for v in out.values())} points for {len(out)} companies")
    return out


# ---------------------------------------------------------------- merge + metrics
def merge(existing, adds):
    """Points by date; within 20 days of each other the better source wins."""
    pts = {p["date"]: p for p in existing}
    for p in adds:
        clash = [k for k in pts if abs((d(k) - d(p["date"])).days) <= 20]
        if clash:
            k = clash[0]
            if PRIORITY[p["source"]] > PRIORITY[pts[k]["source"]] or (PRIORITY[p["source"]] == PRIORITY[pts[k]["source"]] and p["date"] >= k):
                del pts[k]
                pts[p["date"]] = p
        else:
            pts[p["date"]] = p
    return sorted(pts.values(), key=lambda p: p["date"])


def near(points, target, tol):
    best = None
    for p in points:
        gap = abs((d(p["date"]) - target).days)
        if gap <= tol and (best is None or gap < best[0]):
            best = (gap, p)
    return best[1] if best else None


def ttm_revenue(fin, at):
    """Trailing-12-month revenue at a date: the 4 quarters ending on / before it (latest within 100 days),
    else the fiscal year ending within 100 days of it."""
    if not fin:
        return None
    q = sorted([x for x in fin.get("incomeQuarterly") or [] if d(x.get("date")) and num(x.get("revenue")) is not None], key=lambda x: x["date"])
    q = [x for x in q if d(x["date"]) <= at]
    if len(q) >= 4 and (at - d(q[-1]["date"])).days <= 100:
        last4 = q[-4:]
        if (d(last4[-1]["date"]) - d(last4[0]["date"])).days <= 300:
            return sum(num(x["revenue"]) for x in last4)
    a = [x for x in fin.get("income") or [] if d(x.get("date")) and num(x.get("revenue")) is not None]
    a = [x for x in a if abs((d(x["date"]) - at).days) <= 100]
    return num(a[0]["revenue"]) if a else None


def metrics(points, fin, info):
    if not points:
        return {}
    last = points[-1]
    ld = d(last["date"])
    ya = near(points[:-1], ld - timedelta(days=365), 75)
    qa = near(points[:-1], ld - timedelta(days=91), 30)
    peak = max(points, key=lambda p: p["employees"])
    recent = [p for p in points if (ld - d(p["date"])).days <= 400]
    rpe = []
    for p in points[-24:]:
        r = ttm_revenue(fin, d(p["date"]))
        if r and p["employees"]:
            rpe.append({"date": p["date"], "revenue": round(r), "employees": p["employees"], "rpe": round(r / p["employees"])})
    cur = None
    if info.get("revenue") and last["employees"]:
        cur = {"revenue": info["revenue"], "employees": last["employees"], "rpe": round(info["revenue"] / last["employees"]), "basis": "TTM revenue on the quote page ÷ latest headcount"}
    return {"latest": last, "yoyPct": round((last["employees"] / ya["employees"] - 1) * 100, 2) if ya else None, "yoyFrom": ya["date"] if ya else None,
            "qoqPct": round((last["employees"] / qa["employees"] - 1) * 100, 2) if qa else None,
            "peak": {"date": peak["date"], "employees": peak["employees"]},
            "fromPeakPct": round((last["employees"] / peak["employees"] - 1) * 100, 2),
            "cadence": "quarterly" if len(recent) >= 3 else "annual", "revenuePerEmployee": rpe, "rpeNow": cur,
            "currency": (fin or {}).get("currency") or info.get("currency") or "USD"}


def breadth(docs, uni):
    """Who is cutting vs growing (latest vs ~a year earlier), by sector, and the same by calendar year."""
    def summarize(rows):
        if not rows:
            return None
        cut = [r for r in rows if r[1] <= -2]
        grow = [r for r in rows if r[1] >= 2]
        tot0 = sum(r[2] for r in rows)
        tot1 = sum(r[3] for r in rows)
        return {"companies": len(rows), "pctCutting": round(len(cut) / len(rows) * 100, 1), "pctGrowing": round(len(grow) / len(rows) * 100, 1),
                "medianYoY": round(statistics.median(r[1] for r in rows), 2), "aggregateYoY": round((tot1 / tot0 - 1) * 100, 2) if tot0 else None,
                "employees": tot1}
    today = date.today()
    now_rows = []
    for t, doc in docs.items():
        m = doc.get("metrics") or {}
        if m.get("yoyPct") is None or (today - d(m["latest"]["date"])).days > 460:
            continue
        ya = near(doc["points"][:-1], d(m["latest"]["date"]) - timedelta(days=365), 75)
        now_rows.append((t, m["yoyPct"], ya["employees"], m["latest"]["employees"], (uni.get(t) or {}).get("sector") or "Other"))
    sectors = {}
    for s in sorted({r[4] for r in now_rows}):
        sectors[s] = summarize([r for r in now_rows if r[4] == s])
    years = []
    for y in range(2014, today.year + 1):
        rows = []
        for t, doc in docs.items():
            a = near(doc["points"], date(y - 1, 12, 31), 75)
            b = near(doc["points"], date(y, 12, 31), 75)
            if a and b and a["date"] != b["date"] and a["employees"]:
                rows.append((t, (b["employees"] / a["employees"] - 1) * 100, a["employees"], b["employees"], ""))
        s = summarize(rows)
        if s and s["companies"] >= 20:
            years.append(dict(s, year=y))
    big = [r for r in now_rows if r[3] >= 1000]
    return {"asOf": now_iso(), "now": summarize(now_rows), "sectors": sectors, "byYear": years,
            "cutting": [{"ticker": r[0], "yoyPct": round(r[1], 1), "employees": r[3]} for r in sorted(big, key=lambda r: r[1])[:15]],
            "growing": [{"ticker": r[0], "yoyPct": round(r[1], 1), "employees": r[3]} for r in sorted(big, key=lambda r: -r[1])[:15]],
            "note": "cutting ≤ −2 % YoY, growing ≥ +2 %; latest headcount within 15 months vs the point nearest a year earlier (±75 days)"}


def main():
    uni = universe()
    log(f"{len(uni)} companies")
    tick = set(uni)
    old = {t: (read(OUT / f"{t}.json") or {}) for t in tick}
    adds = {t: [] for t in tick}
    for t, rows in sec_points(tick).items():
        adds[t] += [{"date": dt, "employees": v, "source": "sec", "form": f} for dt, v, f in rows]
    for t, rows in wikidata_points(uni).items():
        adds[t] += [{"date": dt, "employees": v, "source": "wikidata"} for dt, v in rows]
    order = sorted(tick, key=lambda t: (old[t].get("fmpAt") or "", t))
    fmp = fmp_points(order)
    for t, rows in fmp.items():
        adds[t] += [{"date": dt, "employees": v, "source": "filing", "form": f} for dt, v, f in rows]
    today = date.today().isoformat()
    for t, info in uni.items():
        e = info.get("employees")
        if not (e and e > 0):
            continue
        e = int(e)
        known = merge(old[t].get("points") or [], adds[t])
        reported = [p for p in known if p["source"] != "profile"]
        prev = [p for p in known if p["source"] == "profile"]
        # the quote page usually repeats the latest reported number - a snapshot is a new point only when it
        # says something the reports don't (a different number from the latest report and the last snapshot)
        if reported and reported[-1]["employees"] == e:
            continue
        if prev and prev[-1]["employees"] == e:
            continue
        adds[t].append({"date": today, "employees": e, "source": "profile"})
    docs = {}
    for t, info in uni.items():
        pts = merge(old[t].get("points") or [], adds[t])
        if not pts:
            continue
        fin = None
        if info.get("book") and len(pts) >= 1:
            try:
                fin = get_json(f"{RAW}/{info['book']}/main/data/financials/{urllib.parse.quote(t)}.json", timeout=30)
            except Exception:
                fin = None
        doc = {"ticker": t, "name": info.get("name"), "sector": info.get("sector"), "points": pts, "updatedAt": now_iso(),
               "fmpAt": now_iso() if t in fmp else old[t].get("fmpAt")}
        doc["metrics"] = metrics(pts, fin, info)
        write(OUT / f"{t}.json", doc)
        docs[t] = doc
    index = {t: {"employees": doc["metrics"]["latest"]["employees"], "date": doc["metrics"]["latest"]["date"],
                 "yoyPct": doc["metrics"].get("yoyPct"), "fromPeakPct": doc["metrics"].get("fromPeakPct"),
                 "cadence": doc["metrics"].get("cadence"), "rpe": (doc["metrics"].get("rpeNow") or {}).get("rpe"), "points": len(doc["points"])}
             for t, doc in docs.items() if doc.get("metrics")}
    write(OUT / "index.json", {"asOf": now_iso(), "tickers": index})
    s = breadth(docs, uni)
    write(OUT / "summary.json", s)
    n = s.get("now") or {}
    log(f"{len(docs)} companies with headcount · now: {n.get('pctCutting')} % cutting, {n.get('pctGrowing')} % growing, aggregate {n.get('aggregateYoY')} % YoY over {n.get('companies')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
