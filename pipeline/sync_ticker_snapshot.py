#!/usr/bin/env python3
"""
sync_ticker_snapshot.py — build the flat `ticker_snapshot` table from GitHub.

Reads the data that already lives in the repos:
  - TRAPP2/data/master.json        (US equities + ETFs, ~336 rows)
  - TRAPP2-2/data/master.json      (more US equities, ~260 rows)
  - TRAPP2-1/data/master.json      (FX / crypto / futures / foreign — optional)
  - TRAPP2-ANALYTICS/data/research_grades.json  (overall + per-category grades)
  - TRAPP2/data/signals.json       (engine signal consensus — optional)

…merges them per ticker into ONE denormalized row, and upserts to Supabase so
the app/brain/bot can screen with plain SQL instead of downloading fat JSON.

GitHub stays the source of truth + history; this is a read-optimized projection.

Stdlib only (urllib + json). No pip installs. Designed to run in a GitHub Action
with two secrets already present in your repos:
    SUPABASE_URL          e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_ROLE the service-role key (bypasses RLS for writes)

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE=... python3 sync_ticker_snapshot.py
"""

import json
import os
import sys
import urllib.request
import urllib.error

# Repo owner — resolved at runtime so the pipeline follows the repos to any
# GitHub account. Actions sets GITHUB_REPOSITORY_OWNER automatically;
# VALUATIO_OWNER (repo variable/env) overrides; TheMostLocal is the fallback.
_GH_OWNER = (__import__("os").environ.get("VALUATIO_OWNER")
             or __import__("os").environ.get("GITHUB_REPOSITORY_OWNER")
             or "TheMostLocal").strip()

# ---- config ---------------------------------------------------------------
RAW = f"https://raw.githubusercontent.com/{_GH_OWNER}"
MASTER_SOURCES = [
    (f"{RAW}/TRAPP2/main/data/master.json", "TRAPP2"),
    (f"{RAW}/TRAPP2-2/main/data/master.json", "TRAPP2-2"),
    (f"{RAW}/TRAPP2-1/main/data/master.json", "TRAPP2-1"),  # non-equities; 404 is fine
    (f"{RAW}/TRAPP2-3/main/data/master.json", "TRAPP2-3"),  # ~200 equities not in TRAPP2/-2
]
GRADES_URL = f"{RAW}/TRAPP2-ANALYTICS/main/data/research_grades.json"
SIGNALS_URL = f"{RAW}/TRAPP2/main/data/signals.json"

# ---- Supabase credentials (same block in every Valuatio sync script) --------
# Picks whichever configured key is actually a SERVICE key (legacy JWT with
# role=service_role, or a new sb_secret_ key), so a wrong value in ONE of the
# two secret names can't silently downgrade writes to anon. Never prints keys.
import base64 as _sb_b64, json as _sb_json, os as _sb_os, re as _sb_re
def _sb_claims(k):
    try:
        seg = k.split(".")[1]; seg += "=" * (-len(seg) % 4)
        return _sb_json.loads(_sb_b64.urlsafe_b64decode(seg))
    except Exception:
        return {}
def _sb_kind(k):
    k = (k or "").strip()
    if not k: return "missing"
    if k.startswith("sb_secret_"): return "secret"
    if k.startswith("sb_publishable_"): return "publishable"
    if k.count(".") == 2: return _sb_claims(k).get("role") or "jwt(no role)"
    return "unrecognized"
def _sb_url():
    u = (_sb_os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    return _sb_re.sub(r"/rest/v1$", "", u)
def _sb_pick_key():
    names = ("SUPABASE_SERVICE_ROLE", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY", "SUPABASE_ANON_KEY")
    vals = [(n, (_sb_os.environ.get(n) or "").strip()) for n in names]
    have = [(n, v) for n, v in vals if v]
    good = [(n, v) for n, v in have if _sb_kind(v) in ("service_role", "secret")]
    name, key = (good or have or [(None, "")])[0]
    print("[supabase] " + (", ".join(f"{n}={_sb_kind(v)}" for n, v in have) or "no keys set")
          + f" -> using {name or 'none'}")
    if key and _sb_kind(key) not in ("service_role", "secret"):
        print(f"::warning::{name} is a '{_sb_kind(key)}' key, not service_role/secret - "
              "service-only tables (ticker_snapshot, regime_timeline, bot_equity) will reject writes")
    distinct = {v for n, v in have if n in names[:2]}
    if len(distinct) > 1:
        print("::warning::SUPABASE_SERVICE_ROLE and SUPABASE_SERVICE_KEY differ - set both to the same service key")
    ref = _sb_claims(key).get("ref") if key.count(".") == 2 else None
    m = _sb_re.match(r"https://([a-z0-9]+)\.supabase\.co$", _sb_url())
    if ref and m and ref != m.group(1):
        print(f"::error::key belongs to Supabase project '{ref}' but SUPABASE_URL points at '{m.group(1)}' (keys from the other project?)")
    return key
def _sb_finite(obj):
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _sb_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sb_finite(v) for v in obj]
    return obj
# -----------------------------------------------------------------------------
SUPABASE_URL = _sb_url()
SERVICE_KEY = _sb_pick_key()
TABLE = "ticker_snapshot"
BATCH = 200  # rows per upsert request


from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None


def _eastern_now():
    if _ET is not None:
        return datetime.now(_ET)
    u = datetime.now(timezone.utc); y = u.year
    def nth_sunday(month, n):
        d = datetime(y, month, 1, tzinfo=timezone.utc)
        return 1 + ((6 - d.weekday()) % 7) + (n - 1) * 7
    start = datetime(y, 3, nth_sunday(3, 2), 7, tzinfo=timezone.utc)
    end = datetime(y, 11, nth_sunday(11, 1), 6, tzinfo=timezone.utc)
    return u + timedelta(hours=(-4 if start <= u < end else -5))


def now_iso():
    """Eastern wall-clock tagged +00:00 so Supabase's UTC display shows local time."""
    return _eastern_now().replace(tzinfo=timezone.utc).isoformat()


def _num(v):
    """Coerce to float or None — master.json mixes strings and numbers."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        # guard against inf/nan which Postgres rejects in JSON
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    except (TypeError, ValueError):
        return None


def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "valuatio-snapshot"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  (skip, 404) {url}")
            return None
        print(f"  ! HTTP {e.code} for {url}")
        return None
    except Exception as e:
        print(f"  ! fetch failed {url}: {e}")
        return None


def load_master_rows():
    """All equity/ETF rows across the master sources, keyed by ticker."""
    rows = {}
    for url, repo in MASTER_SOURCES:
        data = fetch_json(url)
        if not isinstance(data, list):
            continue
        n = 0
        for row in data:
            t = (row.get("ticker") or row.get("symbol") or "").strip().upper()
            if not t:
                continue
            # first source wins; later ones only fill gaps (avoids overwriting
            # a richer TRAPP2 row with a thinner duplicate)
            if t not in rows:
                row["_repo"] = repo
                rows[t] = row
                n += 1
        print(f"  {repo}: +{n} rows ({len(data)} in file)")
    return rows


def load_grades():
    data = fetch_json(GRADES_URL)
    out = {}
    if isinstance(data, dict):
        bt = data.get("byTicker") or {}
        ne = data.get("nonEquity") or {}
        for src in (bt, ne):
            for t, g in src.items():
                out[t.strip().upper()] = g
    print(f"  grades: {len(out)} tickers")
    return out


def load_signals():
    data = fetch_json(SIGNALS_URL)
    out = {}
    if isinstance(data, dict):
        sig = data.get("signals") or {}
        # signals.json holds macro/liquidity/etc AND per-ticker entries; keep
        # only dict entries that look like a per-ticker signal record.
        for k, v in sig.items():
            if isinstance(v, dict) and ("tier" in v or "confidence" in v):
                out[k.strip().upper()] = v
    print(f"  signals: {len(out)} entries")
    return out


def build_rows(master, grades, signals):
    snapshot = []
    for t, m in master.items():
        g = grades.get(t, {})
        s = signals.get(t, {})
        ranks = g.get("ranks") if isinstance(g.get("ranks"), dict) else None
        row = {
            "ticker": t,
            "updated_at": now_iso(),
            "name": m.get("name") or g.get("name"),
            "sector": m.get("sector") or g.get("sector") or None,
            "industry": m.get("industry") or None,
            "asset_class": m.get("asset_class") or g.get("assetClass") or None,
            "exchange": m.get("exchange") or None,
            "currency": m.get("currency") or None,
            # price block
            "price": _num(m.get("price") if m.get("price") is not None else m.get("close")),
            "close_yest": _num(m.get("closeyest")),
            "change_pct": _num(m.get("changepct")),
            "volume": _num(m.get("volume")),
            "volume_avg": _num(m.get("volumeavg")),
            "high52": _num(m.get("high52")),
            "low52": _num(m.get("low52")),
            "beta": _num(m.get("beta")),
            "market_cap": _num(m.get("marketcap")),
            # financials
            "pe": _num(m.get("pe")),
            "peg": _num(m.get("pegRatio")),
            "pb": _num(m.get("priceToBook")),
            "ev_ebitda": _num(m.get("evToEbitda")),
            "ev_rev": _num(m.get("evToRevenue")),
            "dividend_yield": _num(m.get("dividend_yield")),
            "gross_margin": _num(m.get("grossMargin")),
            "op_margin": _num(m.get("operatingMargin")),
            "net_margin": _num(m.get("profitMargin")),
            "fcf_margin": None,  # derived below if possible
            "roe": _num(m.get("returnOnEquity")),
            "roa": _num(m.get("returnOnAssets")),
            "rev_growth": _num(m.get("revenueGrowth")),
            "eps_growth": _num(m.get("earningsGrowth")),
            "debt_equity": _num(m.get("debtToEquity")),
            "current_ratio": _num(m.get("currentRatio")),
            "revenue": _num(m.get("revenue")),
            "net_income": _num(m.get("netIncome")),
            "ebitda": _num(m.get("ebitda")),
            "free_cash_flow": _num(m.get("freeCashFlow")),
            "eps": _num(m.get("eps")),
            "shares": _num(m.get("shares")),
            "short_pct_float": _num(m.get("shortPctFloat")),
            # grade
            "grade": g.get("grade"),
            "grade_score": _num(g.get("gradeScore")),
            "coverage": int(g["coverage"]) if isinstance(g.get("coverage"), (int, float)) else None,
            "coverage_total": int(g["coverageTotal"]) if isinstance(g.get("coverageTotal"), (int, float)) else None,
            "ranks": ranks,
            # signals
            "signal_tier": s.get("tier"),
            "signal_confidence": _num(s.get("confidence")),
            "signal_note": s.get("note"),
            # bookkeeping
            "data_date": m.get("date"),
            "source_repo": m.get("_repo"),
        }
        # derive FCF margin if we have the pieces
        fcf, rev = row["free_cash_flow"], row["revenue"]
        if fcf is not None and rev:
            row["fcf_margin"] = round(fcf / rev, 6)
        snapshot.append(row)
    return snapshot


def upsert(rows):
    if not SUPABASE_URL or not SERVICE_KEY:
        print("! SUPABASE_URL / SUPABASE_SERVICE_ROLE not set — dry run only.")
        print(f"  would upsert {len(rows)} rows.")
        # show a sample so a local dry run is still useful
        if rows:
            print("  sample row:", json.dumps(rows[0], default=str)[:400])
        return 0
    url = f"{SUPABASE_URL}/rest/v1/{TABLE}?on_conflict=ticker"
    headers = {
        "Content-Type": "application/json",
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        body = json.dumps(_sb_finite(chunk), allow_nan=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
                done += len(chunk)
                print(f"  upserted {done}/{len(rows)}")
        except urllib.error.HTTPError as e:
            print(f"  ! upsert HTTP {e.code}: {e.read().decode('utf-8')[:300]}")
            raise
    return done


def main():
    print("Loading master rows…")
    master = load_master_rows()
    print("Loading grades…")
    grades = load_grades()
    print("Loading signals…")
    signals = load_signals()
    print(f"Building snapshot for {len(master)} tickers…")
    rows = build_rows(master, grades, signals)
    graded = sum(1 for r in rows if r["grade"])
    priced = sum(1 for r in rows if r["price"] is not None)
    print(f"  {priced} priced, {graded} graded")
    print("Upserting to Supabase…")
    n = upsert(rows)
    print(f"Done. {n} rows synced.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
