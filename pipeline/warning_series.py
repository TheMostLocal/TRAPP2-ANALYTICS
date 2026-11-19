#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/warning_series.py
#
#  z196 - THE DIGGING, part 1: the public data that called 2007-08 in advance, fetched with its full history.
#  Owner (11-19), after Steve Eisman's account of shorting subprime: "The warning was never hidden ... in the case of
#  08, it was published every month in plain English for anyone willing to read." The remittance reports he read
#  were private servicer data; their public, long-history equivalents are the Federal Reserve's loss rates, the Senior
#  Loan Officer survey, the funding markets, the labour market, house prices and household / corporate leverage - all
#  on FRED, most of them back to the 1980s-90s, so every signal can be checked against 2001, 2008 and 2020.
#
#  Writes data/warning/series/<ID>.json  {"id", "fetchedAt", "source", "obs": [[YYYY-MM-DD, value], ...]}
#         data/warning/series/_fetch.json (per series: ok / error, observations, last date)
#  Source: the FRED API when FRED_API_KEY is set (Actions secret - the same free key TRAPP2-1 uses), else FRED's
#  public CSV download (no key). A series that fails keeps its previous file. A series FRED doesn't have is reported
#  and skipped - warning_signals.py works with whatever is on file.
#    python pipeline/warning_series.py            (all)      python pipeline/warning_series.py NFCI ICSA   (some)
# ============================================================
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "warning" / "series"
START = os.environ.get("WARNING_START", "1985-01-01")
UA = "TRAPP2-ANALYTICS warning-signals (github.com/TheMostLocal)"

# id -> (label, unit, frequency, publication lag in days after the observation date, group)
# The lag keeps the backtest honest: a value counts from the day it was published, not from the period it describes
# (bank loss rates for a quarter arrive ~2 months after it ends; payrolls a week after the month).
SERIES = {
    # --- loss data: what borrowers are failing to pay (the public "remittance reports") ---------------------------
    "DRCCLACBS": ("Credit-card delinquency rate (banks)", "%", "q", 145, "loss"),
    "CORCCACBS": ("Credit-card charge-off rate (banks)", "%", "q", 145, "loss"),
    "DRCLACBS": ("Consumer-loan delinquency rate (banks)", "%", "q", 145, "loss"),
    "DRSFRMACBS": ("Residential-mortgage delinquency rate (banks)", "%", "q", 145, "loss"),
    "CORSFRMACBS": ("Residential-mortgage charge-off rate (banks)", "%", "q", 145, "loss"),
    "DRCRELEXFACBS": ("Commercial real-estate delinquency rate (banks)", "%", "q", 145, "loss"),
    "DRBLACBS": ("Business-loan delinquency rate (banks)", "%", "q", 145, "loss"),
    "DRALACBS": ("All-loan delinquency rate (banks)", "%", "q", 145, "loss"),
    # --- lenders: who is pulling back (Senior Loan Officer survey; FRED dates it at the survey month) --------------
    "DRTSCILM": ("Banks tightening C&I loans, large/mid firms (net %)", "net %", "q", 40, "lenders"),
    "DRTSCIS": ("Banks tightening C&I loans, small firms (net %)", "net %", "q", 40, "lenders"),
    "DRTSCLCC": ("Banks tightening credit-card standards (net %)", "net %", "q", 40, "lenders"),
    "DRSDCILM": ("Banks seeing stronger C&I loan demand (net %)", "net %", "q", 40, "lenders"),
    # --- funding and market plumbing: is the marginal buyer still buying ------------------------------------------
    "BAA10Y": ("Baa corporate spread over 10-year Treasury", "pts", "d", 1, "funding"),
    "NFCI": ("Chicago Fed financial conditions (NFCI)", "index", "w", 5, "funding"),
    "NFCICREDIT": ("NFCI credit subindex", "index", "w", 5, "funding"),
    "NFCIRISK": ("NFCI risk subindex", "index", "w", 5, "funding"),
    "NFCILEVERAGE": ("NFCI leverage subindex", "index", "w", 5, "fragility"),
    "STLFSI4": ("St. Louis Fed financial stress index", "index", "w", 7, "funding"),
    "VIXCLS": ("VIX (S&P 500 implied volatility)", "pts", "d", 1, "funding"),
    "DCPF3M": ("3-month AA financial commercial paper rate", "%", "d", 1, "funding"),
    "DTB3": ("3-month Treasury bill", "%", "d", 1, "funding"),
    "ABCOMP": ("Asset-backed commercial paper outstanding", "$bn", "w", 7, "funding"),
    "BAMLH0A0HYM2": ("High-yield option-adjusted spread (ICE BofA; FRED keeps ~3 years)", "%", "d", 1, "funding"),
    "T10Y3M": ("10-year minus 3-month Treasury (the curve)", "pts", "d", 1, "fragility"),
    # --- borrowers: can they pay (labour) --------------------------------------------------------------------------
    "ICSA": ("Initial jobless claims", "k", "w", 5, "labor"),
    "CCSA": ("Continuing jobless claims", "k", "w", 12, "labor"),
    "UNRATE": ("Unemployment rate", "%", "m", 37, "labor"),
    "SAHMREALTIME": ("Sahm rule (real time)", "pts", "m", 37, "labor"),
    "TEMPHELPS": ("Temporary-help employment", "k", "m", 37, "labor"),
    "PAYEMS": ("Nonfarm payrolls", "k", "m", 37, "labor"),
    # --- collateral: what backs the loans --------------------------------------------------------------------------
    "CSUSHPINSA": ("Case-Shiller US home prices", "index", "m", 90, "collateral"),
    "MSACSR": ("Months' supply of new homes", "months", "m", 56, "collateral"),
    "PERMIT": ("Building permits", "k SAAR", "m", 48, "collateral"),
    "HOUST": ("Housing starts", "k SAAR", "m", 48, "collateral"),
    "MORTGAGE30US": ("30-year mortgage rate", "%", "w", 1, "collateral"),
    # --- fragility: how much leverage rides on it -------------------------------------------------------------------
    "TOTALSL": ("Consumer credit outstanding", "$bn", "m", 68, "fragility"),
    "REVOLSL": ("Revolving consumer credit", "$bn", "m", 68, "fragility"),
    "DSPI": ("Disposable personal income", "$bn SAAR", "m", 60, "fragility"),
    "BCNSDODNS": ("Nonfinancial corporate debt (securities + loans)", "$m", "q", 160, "fragility"),
    "GDP": ("Nominal GDP", "$bn SAAR", "q", 120, "fragility"),
    "HDTGPDUSQ163N": ("Household debt to GDP (BIS)", "%", "q", 210, "fragility"),
    "TOTLL": ("Bank loans and leases", "$bn", "w", 9, "fragility"),
    "PSAVERT": ("Personal saving rate", "%", "m", 60, "fragility"),
    "TDSP": ("Household debt service ratio", "% of income", "q", 170, "fragility"),
    # --- organic growth: income, jobs, production, orders, profits ------------------------------------------------
    "W875RX1": ("Real personal income ex transfers", "$bn 2017", "m", 60, "growth"),
    "INDPRO": ("Industrial production", "index", "m", 47, "growth"),
    "NEWORDER": ("Core capital-goods orders", "$m", "m", 65, "growth"),
    "RSAFS": ("Retail sales", "$m", "m", 46, "growth"),
    "CPIAUCSL": ("CPI", "index", "m", 45, "growth"),
    "CFNAIMA3": ("Chicago Fed activity index (3-month avg)", "index", "m", 55, "growth"),
    "JTSJOL": ("Job openings", "k", "m", 70, "growth"),
    "CP": ("Corporate profits after tax", "$bn SAAR", "q", 150, "growth"),
    "OPHNFB": ("Labour productivity", "index", "q", 130, "growth"),
    # --- outcomes (backtest only) -----------------------------------------------------------------------------------
    "USREC": ("NBER recession indicator", "0/1", "m", 0, "outcome"),
    "NASDAQCOM": ("Nasdaq Composite (long equity history)", "index", "d", 1, "outcome"),
}


def log(msg):
    print(msg, flush=True)


def _get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _clean(rows):
    out = []
    for d, v in rows:
        d = str(d or "")[:10]
        if len(d) != 10 or v in (None, "", "."):
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if x == x and abs(x) != float("inf"):
            out.append([d, x])
    out.sort(key=lambda r: r[0])
    return out


def fetch_api(sid, key):
    q = urllib.parse.urlencode({"series_id": sid, "api_key": key, "file_type": "json", "observation_start": START})
    d = json.loads(_get("https://api.stlouisfed.org/fred/series/observations?" + q))
    return _clean((o.get("date"), o.get("value")) for o in d.get("observations") or [])


def fetch_csv(sid):
    q = urllib.parse.urlencode({"id": sid, "cosd": START})
    text = _get("https://fred.stlouisfed.org/graph/fredgraph.csv?" + q)
    rd = csv.reader(io.StringIO(text))
    head = next(rd, None) or []
    if len(head) < 2 or "<" in "".join(head):
        raise ValueError("not a CSV (series missing?)")
    return _clean((r[0], r[1]) for r in rd if len(r) >= 2)


def fetch(sid, key, tries=3):
    last = None
    for k in range(tries):
        try:
            return (fetch_api(sid, key), "fred-api") if key else (fetch_csv(sid), "fredgraph-csv")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404):            # FRED has no such series - don't retry
                break
        except Exception as e:                  # network hiccup / rate limit
            last = e
        time.sleep(2.0 * (k + 1))
    raise RuntimeError(f"{type(last).__name__}: {str(last)[:120]}")


def main(argv):
    key = (os.environ.get("FRED_API_KEY") or "").strip()
    want = [a.upper() for a in argv if a.strip()] or list(SERIES)
    OUT.mkdir(parents=True, exist_ok=True)
    rep_p = OUT / "_fetch.json"
    try:
        rep = json.loads(rep_p.read_text())
    except Exception:
        rep = {}
    rep.setdefault("series", {})
    ok = bad = 0
    log(f"FRED: {len(want)} series from {START} via {'the API (FRED_API_KEY)' if key else 'the public CSV download (no key)'}")
    for sid in want:
        try:
            obs, src = fetch(sid, key)
            if len(obs) < 8:
                raise RuntimeError(f"only {len(obs)} observations")
            p = OUT / f"{sid}.json"
            try:
                same = json.loads(p.read_text()).get("obs") == obs
            except Exception:
                same = False
            if not same:                          # unchanged data -> no rewrite (no daily git churn for 2 MB of history)
                p.write_text(json.dumps(
                    {"id": sid, "label": (SERIES.get(sid) or ("",))[0], "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "source": src, "obs": obs}, separators=(",", ":")))
            rep["series"][sid] = {"ok": True, "n": len(obs), "first": obs[0][0], "last": obs[-1][0], "source": src, "changed": not same}
            ok += 1
            log(f"  {sid:15s} {len(obs):6d} obs  {obs[0][0]} .. {obs[-1][0]}")
        except Exception as e:
            prev = (rep["series"].get(sid) or {})
            rep["series"][sid] = dict(prev, ok=False, error=str(e)[:160], at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            bad += 1
            log(f"  {sid:15s} FAILED ({e}) - previous file kept" if (OUT / f"{sid}.json").exists() else f"  {sid:15s} FAILED ({e})")
        time.sleep(0.6 if key else 1.0)          # FRED API: 120 requests / minute
    rep["asOf"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rep["source"] = "fred-api" if key else "fredgraph-csv"
    rep_p.write_text(json.dumps(rep, indent=1, sort_keys=True))
    log(f"done: {ok} fetched, {bad} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
