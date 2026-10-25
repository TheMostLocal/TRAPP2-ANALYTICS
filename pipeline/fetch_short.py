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


def http(url, data=None, headers=None, method=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method, headers=dict({"User-Agent": "valuatio-short"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


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
    while len(files) < days and tries < 40:
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
        r20 = r[:20]
        mu = statistics.mean(r20)
        sd = statistics.pstdev(r20) if len(r20) >= 5 else 0
        out[t] = {"date": ser[0][0], "ratio1d": round(r[0] * 100, 1), "ratio5d": round(statistics.mean(r[:5]) * 100, 1),
                  "ratio20d": round(mu * 100, 1), "z": round((r[0] - mu) / sd, 2) if sd > 0 else None, "totalVolume": int(ser[0][2])}
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
    rows, offset = [], 0
    for _ in range(12):
        body = {"limit": 5000, "offset": offset, "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": start, "endDate": end}]}
        try:
            b = http(f"{API}/data/group/otcMarket/name/consolidatedShortInterest", data=json.dumps(body).encode(), method="POST",
                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/json", "Content-Type": "application/json"})
        except urllib.error.HTTPError as e:
            log(f"short interest: FINRA answered {e.code} for consolidatedShortInterest - {'not on this API tier' if e.code in (401, 403, 404) else 'error'}; skipped")
            return {}
        page = json.loads(b.decode() or "[]")
        page = page if isinstance(page, list) else (page.get("data") or [])
        rows += page
        if len(page) < 5000:
            break
        offset += 5000
    tset = set(tickers)
    latest = {}
    for r in rows:
        t = sym(r.get("symbolCode") or r.get("issueSymbolIdentifier"))
        if t not in tset:
            continue
        sd = str(r.get("settlementDate") or "")[:10]
        if t not in latest or sd > latest[t]["settlementDate"]:
            cur, prev = r.get("currentShortPositionQuantity"), r.get("previousShortPositionQuantity")
            latest[t] = {"settlementDate": sd, "shares": cur, "prevShares": prev,
                         "changePct": round((cur - prev) / prev * 100, 1) if (cur is not None and prev) else r.get("changePercent"),
                         "avgDailyVolume": r.get("averageDailyVolumeQuantity"), "daysToCover": r.get("daysToCoverQuantity")}
    log(f"short interest: {len(rows)} rows · {len(latest)} of our companies · latest settlement {max((v['settlementDate'] for v in latest.values()), default='—')}")
    return latest


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
    (OUT / "index.json").write_text(json.dumps({"asOf": now_iso(), "volume": vol or old.get("volume") or {},
                                                "interest": si or old.get("interest") or {},
                                                "note": "short share of volume = Reg SHO short volume ÷ total reported volume (FINRA consolidated NMS); interest = FINRA consolidated short interest"},
                                               separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
