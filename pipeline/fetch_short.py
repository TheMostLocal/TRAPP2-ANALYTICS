#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/fetch_short.py
#
#  z158 - SHORT SELLING, from FINRA (official):
#    volume   - Reg SHO daily short-sale volume for every NMS stock (FINRA's public daily files, no key):
#               short share of volume today, 5- and 20-day averages, and how unusual today is (z-score)
#    interest - consolidated short interest (twice a month; FINRA API with FINRA_CLIENT_ID / SECRET):
#               shares short, change, average daily volume, days to cover. Probed like the bond data -
#               if the free API tier doesn't include it the log says so and the rest still runs.
#  Our universe only (S&P members + the books' US stocks).  -> data/short/index.json
#
#  z207 - THE NEWEST SETTLEMENT, WHOLE. The interest pull pages through every settlement of the last 45 days, and FINRA
#  returns about 20,000 issues a settlement in settlement / symbol order - three settlements filled the old 12-page
#  (60,000-row) cap, so the newest one stopped part-way through the alphabet (11-25: A-O on 11-13, P-Z still on 10-30).
#  Now: up to 40 pages, stopping at FINRA's own Record-Total; a log line if the cap is ever reached; a "settlements"
#  summary (the newest date, how many of our companies it covers, the one before); FINRA's stock-split flag carried
#  (a split between settlements makes the change in shares short meaningless, so it is not computed); today's short share
#  of volume scored against the 20 sessions BEFORE it (today was inside its own baseline, which shrank every z).
# ============================================================
import base64
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "short"
TOKEN_URL = "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token?grant_type=client_credentials"
API = "https://api.finra.org"


def log(*a):
    print("[short]", *a, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sym(s):
    return str(s or "").upper().replace(".", "-").replace("/", "-").strip()


MAX_PAGES, PAGE = 40, 5000
SPLIT_FLAGS = {"S", "Y", "T", "1", "TRUE"}


def http(url, data=None, headers=None, method=None, timeout=60, want_headers=False):
    req = urllib.request.Request(url, data=data, method=method, headers=dict({"User-Agent": "valuatio-short"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return (body, r.headers) if want_headers else body


def daily_file(d):
    """{SYMBOL: (short, total)} from FINRA's consolidated NMS short-volume file for one day, or None."""
    try:
        txt = http(f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt", timeout=40).decode("utf-8", "replace")
    except Exception:
        return None
    out = {}
    for line in txt.splitlines()[1:]:
        p = line.split("|")
        if len(p) < 5:
            continue
        try:
            sv, tv = float(p[2]), float(p[4])
        except ValueError:
            continue
        if tv > 0:
            out[sym(p[1])] = (sv, tv)
    return out or None


def short_volume(tickers, days=20):
    files, d = [], date.today()
    tries = 0
    while len(files) < days + 1 and tries < 45:
        if d.weekday() < 5:
            f = daily_file(d)
            if f:
                files.append((d.isoformat(), f))
        d -= timedelta(days=1)
        tries += 1
        time.sleep(0.2)
    out = {}
    for t in tickers:
        ser = [(dd, f[t][0] / f[t][1], f[t][1]) for dd, f in files if t in f]
        if not ser:
            continue
        r = [x[1] for x in ser]
        base = r[1:days + 1]                                  # z207: the sessions before today, not including it
        mu, sd = (statistics.mean(base), statistics.stdev(base)) if len(base) >= 5 else (None, 0)
        out[t] = {"date": ser[0][0], "ratio1d": round(r[0] * 100, 1), "ratio5d": round(statistics.mean(r[:5]) * 100, 1),
                  "ratio20d": round(statistics.mean(r[:days]) * 100, 1),
                  "z": round((r[0] - mu) / sd, 2) if sd > 0 else None, "totalVolume": int(ser[0][2])}
    log(f"daily short volume: {len(files)} sessions read ({files[-1][0] if files else '—'} … {files[0][0] if files else '—'}) · {len(out)} of our companies")
    return out


def short_interest(tickers):
    cid, sec = os.environ.get("FINRA_CLIENT_ID", "").strip(), os.environ.get("FINRA_CLIENT_SECRET", "").strip()
    if not (cid and sec):
        log("short interest skipped - FINRA_CLIENT_ID / FINRA_CLIENT_SECRET not set in this repo")
        return {}
    try:
        tok = json.loads(http(TOKEN_URL, data=b"", method="POST", headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()}, timeout=30).decode())["access_token"]
    except Exception as e:
        log(f"short interest: FINRA sign-in failed ({e})")
        return {}
    start, end = (date.today() - timedelta(days=45)).isoformat(), date.today().isoformat()
    rows, offset, total = [], 0, None
    for _ in range(MAX_PAGES):
        body = {"limit": PAGE, "offset": offset, "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": start, "endDate": end}]}
        try:
            b, hdr = http(f"{API}/data/group/otcMarket/name/consolidatedShortInterest", data=json.dumps(body).encode(), method="POST",
                          headers={"Authorization": f"Bearer {tok}", "Accept": "application/json", "Content-Type": "application/json"},
                          want_headers=True)
        except urllib.error.HTTPError as e:
            log(f"short interest: FINRA answered {e.code} for consolidatedShortInterest - {'not on this API tier' if e.code in (401, 403, 404) else 'error'}; skipped")
            return {}
        if total is None:
            try:
                total = int(str(hdr.get("Record-Total") or "").strip())
            except ValueError:
                total = None
        page = json.loads(b.decode() or "[]")
        page = page if isinstance(page, list) else (page.get("data") or [])
        rows += page
        if len(page) < PAGE or (total is not None and len(rows) >= total):
            break
        offset += PAGE
    else:
        log(f"short interest: WARNING - stopped at the {MAX_PAGES}-page cap ({len(rows)} rows"
            + (f" of {total}" if total else "") + ") - the newest settlement may be incomplete")
    latest = pick_latest(rows, set(tickers))
    s = settlements(latest)
    log(f"short interest: {len(rows)} rows" + (f" (FINRA: {total})" if total is not None else "") + f" · {len(latest)} of our companies · "
        f"newest settlement {s.get('latest') or '—'} covers {s.get('names', 0)} of them"
        + (f", the one before ({s['previous']}) {s['previousNames']}" if s.get("previous") else ""))
    return latest


def pick_latest(rows, tset):
    """Each of our companies at its newest settlement in the rows FINRA returned."""
    latest = {}
    for r in rows:
        t = sym(r.get("symbolCode") or r.get("issueSymbolIdentifier"))
        if t not in tset:
            continue
        sd = str(r.get("settlementDate") or "")[:10]
        if t not in latest or sd > latest[t]["settlementDate"]:
            cur, prev = r.get("currentShortPositionQuantity"), r.get("previousShortPositionQuantity")
            split = str(r.get("stockSplitFlag") or "").strip().upper() in SPLIT_FLAGS
            latest[t] = {"settlementDate": sd, "shares": cur, "prevShares": prev,
                         "changePct": None if split else (round((cur - prev) / prev * 100, 1) if (cur is not None and prev) else r.get("changePercent")),
                         "avgDailyVolume": r.get("averageDailyVolumeQuantity"), "daysToCover": r.get("daysToCoverQuantity")}
            if split:
                latest[t]["split"] = True
    return latest


def settlements(latest):
    """{latest, names, previous, previousNames} - how much of our universe the newest settlement covers."""
    from collections import Counter
    c = Counter(v["settlementDate"] for v in latest.values() if v.get("settlementDate"))
    if not c:
        return {}
    ds = sorted(c, reverse=True)
    out = {"latest": ds[0], "names": c[ds[0]]}
    if len(ds) > 1:
        out.update(previous=ds[1], previousNames=c[ds[1]])
    return out


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fetch_headcount import universe
    tickers = sorted(universe())
    vol = short_volume(tickers)
    si = short_interest(tickers)
    old = {}
    try:
        old = json.loads((OUT / "index.json").read_text())
    except Exception:
        pass
    OUT.mkdir(parents=True, exist_ok=True)
    interest = si or old.get("interest") or {}
    (OUT / "index.json").write_text(json.dumps({"asOf": now_iso(), "volume": vol or old.get("volume") or {},
                                                "interest": interest, "settlements": settlements(interest),
                                                "note": "short share of volume = Reg SHO short volume ÷ total reported volume (FINRA consolidated NMS); interest = FINRA consolidated short interest"},
                                               separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
