#!/usr/bin/env python3
"""
sync_analytics_to_supabase.py — push the analytics backend -> Supabase
(TRAPP2-ANALYTICS project). Server-side (GitHub Actions), SERVICE-ROLE key.

WHAT IT PUSHES (each section is independent — one failing never blocks the rest):
  1. research_grades   <- data/research_grades.json  (byTicker -> one row per ticker)
  2. regime_snapshots  <- TRAPP2-1/data/regime_current.json + regime_history.json
                         (id='current' for the app's fast read + one row per date)
  3. regime_timeline   <- TRAPP2-1/data/regime_history.json (compact, chartable series)
  4. analytics_kv      <- TRAPP2-1/data/macro/*.json  (key='macro:<SERIES>')  +
                         regime:drivers / regime:probabilities / regime:scores

WHY THE OLD VERSION PUSHED 0 ROWS:
  The previous file was a copy of the BOT sync. It read d.get("trades") from
  research_grades.json — which has no "trades" array — so it always upserted an
  empty list. research_grades.json keys its per-ticker grades under "byTicker".

TIMEZONE:
  updated_at is written as Eastern wall-clock time tagged +00:00, so the Supabase
  table editor (which renders timestamptz in UTC) shows local Eastern time instead
  of being 4 hours ahead. See now_iso().

Env (repo secrets — already set):
  SUPABASE_URL           https://xxxx.supabase.co   (the ANALYTICS project)
  SUPABASE_SERVICE_ROLE  the service_role key (bypasses RLS for writes)
  (SUPABASE_SERVICE_KEY / SUPABASE_KEY / SUPABASE_ANON_KEY are also accepted)
"""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ------------------------------------------------------------------ config ---
URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = (os.environ.get("SUPABASE_SERVICE_ROLE")
       or os.environ.get("SUPABASE_SERVICE_KEY")
       or os.environ.get("SUPABASE_KEY")
       or os.environ.get("SUPABASE_ANON_KEY") or "")

# TRAPP2-1 is the canonical home for BOTH regime and macro (matches the app's
# CANONICAL_REGIME_BASE / resolveMacroBase).
RAW_T1   = "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-1/main/data"
RAW_ANA  = "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-ANALYTICS/main/data"
GRADES_LOCAL = "data/research_grades.json"   # freshest copy (this repo's checkout)

# FRED series carried in TRAPP2-1/data/macro (+ quad). Auto-discovered at runtime
# via the GitHub contents API; this list is the fallback if that call fails.
MACRO_FALLBACK = [
    "BAMLH0A0HYM2", "CPIAUCSL", "DCOILWTICO", "DEXUSEU", "DFF", "DGS1", "DGS10",
    "DGS1MO", "DGS2", "DGS20", "DGS3", "DGS30", "DGS3MO", "DGS5", "DGS6MO",
    "DGS7", "GDP", "INDPRO", "M2SL", "PAYEMS", "PCEPI", "T10Y2Y", "T10Y3M",
    "UNRATE", "VIXCLS",
]
MACRO_LABELS = {
    "BAMLH0A0HYM2": "HY OAS (credit spread)", "CPIAUCSL": "CPI (headline)",
    "DCOILWTICO": "WTI crude oil", "DEXUSEU": "USD per EUR",
    "DFF": "Fed funds (effective)", "DGS1MO": "1M Treasury yield",
    "DGS3MO": "3M Treasury yield", "DGS6MO": "6M Treasury yield",
    "DGS1": "1Y Treasury yield", "DGS2": "2Y Treasury yield",
    "DGS3": "3Y Treasury yield", "DGS5": "5Y Treasury yield",
    "DGS7": "7Y Treasury yield", "DGS10": "10Y Treasury yield",
    "DGS20": "20Y Treasury yield", "DGS30": "30Y Treasury yield",
    "GDP": "GDP (nominal)", "INDPRO": "Industrial production",
    "M2SL": "M2 money supply", "PAYEMS": "Nonfarm payrolls",
    "PCEPI": "PCE price index", "T10Y2Y": "10Y minus 2Y spread",
    "T10Y3M": "10Y minus 3M spread", "UNRATE": "Unemployment rate",
    "VIXCLS": "VIX",
}
RECENT_OBS = 90   # observations kept per macro series (compact; full history stays on GitHub)


# --------------------------------------------------------------- timezone ----
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None


def _eastern_now():
    """Current US/Eastern wall-clock time (DST-aware)."""
    if _ET is not None:
        return datetime.now(_ET)
    # Fallback if tzdata is unavailable: compute the EDT/EST offset by hand.
    u = datetime.now(timezone.utc)
    y = u.year

    def nth_sunday(month, n):
        d = datetime(y, month, 1, tzinfo=timezone.utc)
        first_sun = 1 + ((6 - d.weekday()) % 7)   # Mon=0 .. Sun=6
        return first_sun + (n - 1) * 7
    dst_start = datetime(y, 3, nth_sunday(3, 2), 7, tzinfo=timezone.utc)   # 2nd Sun Mar 07:00Z
    dst_end = datetime(y, 11, nth_sunday(11, 1), 6, tzinfo=timezone.utc)   # 1st Sun Nov 06:00Z
    off = -4 if dst_start <= u < dst_end else -5
    return u + timedelta(hours=off)


def now_iso():
    """Eastern wall-clock time, tagged +00:00 so Supabase's UTC display shows local."""
    return _eastern_now().replace(tzinfo=timezone.utc).isoformat()


# ------------------------------------------------------------------ http -----
def _req(method, path, body=None, headers=None):
    h = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "valuatio-analytics-sync"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  . fetch failed {url} -> {e}")
        return None


def push(table, rows, conflict):
    """Chunked upsert. Returns rows accepted."""
    if not rows:
        print(f"  {table}: nothing to push")
        return 0
    sent = 0
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        st, body = _req("POST", f"/rest/v1/{table}?on_conflict={conflict}", chunk,
                        {"Prefer": "resolution=merge-duplicates,return=minimal"})
        if st in (200, 201, 204):
            sent += len(chunk)
        else:
            print(f"  {table}: chunk {i} -> HTTP {st}: {body[:240]}")
    print(f"  {table}: upserted {sent}/{len(rows)}")
    return sent


def _num(v):
    try:
        f = float(v)
        return None if (f != f or f in (float("inf"), float("-inf"))) else f
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- 1. grades -----
def push_research_grades():
    d = None
    if os.path.exists(GRADES_LOCAL):
        try:
            d = json.load(open(GRADES_LOCAL))
        except Exception as e:
            print(f"  grades: local read failed ({e}) — falling back to raw")
    if d is None:
        d = fetch_json(f"{RAW_ANA}/research_grades.json")
    if not isinstance(d, dict):
        print("  grades: no research_grades.json")
        return 0
    by = d.get("byTicker") or {}
    ne = d.get("nonEquity") or {}             # foreign/FX/crypto/futures grades, if present
    now = now_iso()
    rows = []
    for src in (by, ne):
        if isinstance(src, dict):
            for tk, g in src.items():
                if not tk or not isinstance(g, dict):
                    continue
                g = dict(g)
                g.setdefault("ticker", tk)
                rows.append({"ticker": str(tk).upper(), "grades": g, "updated_at": now})
    return push("research_grades", rows, "ticker")


# ------------------------------------------------------------- 2. regime -----
def _load_regime():
    cur = fetch_json(f"{RAW_T1}/regime_current.json")
    hist = fetch_json(f"{RAW_T1}/regime_history.json")
    return cur, (hist if isinstance(hist, list) else [])


def push_regime(cur, hist):
    """Mirror the app: id='current' (fast read) + one row per dated snapshot."""
    now = now_iso()
    rows, seen = [], set()

    def add(_id, snap):
        if not _id or _id in seen:
            return
        seen.add(_id)
        rows.append({"id": _id, "snapshot": snap, "updated_at": now})

    if isinstance(cur, dict):
        c = dict(cur)
        c.setdefault("computed_at", now)          # fills the table's computed_at column
        add("current", c)                         # the app reads regime_snapshots?id=eq.current
        if c.get("date"):
            add(c["date"], c)
    for h in hist:
        if isinstance(h, dict) and h.get("date"):
            add(h["date"], h)
    if not rows:
        print("  regime_snapshots: regime not available (TRAPP2-1 fetch returned nothing)")
        return 0
    return push("regime_snapshots", rows, "id")


# ---------------------------------------------------- 3. regime_timeline -----
def push_regime_timeline(hist):
    """Compact, chartable daily series -> dedicated regime_timeline table."""
    now = now_iso()
    rows = []
    for h in hist:
        if not (isinstance(h, dict) and h.get("date")):
            continue
        scores = h.get("scores") or {}
        top = None
        if isinstance(scores, dict) and scores:
            top = max(scores.items(), key=lambda kv: abs(_num(kv[1]) or 0))[0]
        rows.append({
            "id": h["date"],
            "entry": {
                "date": h.get("date"),
                "regime": h.get("regime"),
                "confidence": h.get("confidence"),
                "scores": scores,
                "probabilities": h.get("current_probabilities") or h.get("probabilities"),
                "topDriver": top,
            },
            "updated_at": now,
        })
    if not rows:
        print("  regime_timeline: no regime_history.json")
        return 0
    return push("regime_timeline", rows, "id")


# ----------------------------------------------------------- 4. macro/kv -----
def _discover_macro():
    api = "https://api.github.com/repos/GoodGlobeLLC/TRAPP2-1/contents/data/macro"
    hdr = {"User-Agent": "valuatio-analytics-sync", "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"
    try:
        req = urllib.request.Request(api, headers=hdr)
        with urllib.request.urlopen(req, timeout=30) as r:
            listing = json.loads(r.read().decode())
        names = [x["name"][:-5] for x in listing
                 if isinstance(x, dict) and x.get("name", "").endswith(".json")]
        names = [n for n in names if n != "quad"]
        if names:
            return sorted(names)
    except Exception as e:
        print(f"  macro: contents API unavailable ({e}) — using fallback list")
    return MACRO_FALLBACK


def push_macro():
    now = now_iso()
    rows = []

    # FRED series -> compact summary (latest + prior + recent window).
    for sid in _discover_macro():
        j = fetch_json(f"{RAW_T1}/macro/{sid}.json")
        if not isinstance(j, dict):
            continue
        obs = [o for o in (j.get("observations") or []) if _num(o.get("value")) is not None]
        if not obs:
            continue
        latest, prior = obs[-1], (obs[-2] if len(obs) > 1 else None)
        lv, pv = _num(latest.get("value")), (_num(prior.get("value")) if prior else None)
        rows.append({
            "key": f"macro:{sid}",
            "value": {
                "label": MACRO_LABELS.get(sid, sid),
                "series_id": sid,
                "latest": lv, "latest_date": latest.get("date"),
                "prior": pv, "prior_date": (prior.get("date") if prior else None),
                "change": (round(lv - pv, 6) if (lv is not None and pv is not None) else None),
                "fetched_at": j.get("fetched_at"),
                "n_obs": len(obs),
                "recent": [{"date": o["date"], "value": _num(o["value"])} for o in obs[-RECENT_OBS:]],
            },
            "updated_at": now,
        })

    # Growth/Inflation quad.
    quad = fetch_json(f"{RAW_T1}/macro/quad.json")
    if isinstance(quad, dict) and quad.get("current"):
        rows.append({
            "key": "macro:quad",
            "value": {
                "label": "Growth/Inflation Quad",
                "current": quad["current"],
                "history": (quad.get("history") or [])[-24:],
                "generated_at": quad.get("generated_at"),
            },
            "updated_at": now,
        })

    # regime:* convenience keys (parity with the app's analytics_kv push).
    cur = fetch_json(f"{RAW_T1}/regime_current.json")
    if isinstance(cur, dict):
        if cur.get("drivers"):
            rows.append({"key": "regime:drivers", "value": {"label": "Regime drivers", "items": cur["drivers"]}, "updated_at": now})
        if cur.get("current_probabilities"):
            rows.append({"key": "regime:probabilities", "value": {"label": "Regime probabilities", "items": cur["current_probabilities"]}, "updated_at": now})
        if cur.get("scores"):
            rows.append({"key": "regime:scores", "value": {"label": "Regime scores", "items": cur["scores"]}, "updated_at": now})

    return push("analytics_kv", rows, "key")


# ------------------------------------------------------------------ main -----
def main():
    if not URL or not KEY:
        print("X Missing creds. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE (or SUPABASE_SERVICE_KEY).")
        return 1
    print(f"Analytics -> Supabase  ({URL})")
    total = 0
    try:
        total += push_research_grades()
    except Exception as e:
        print(f"  grades: ERROR {e}")
    cur, hist = (None, [])
    try:
        cur, hist = _load_regime()
        total += push_regime(cur, hist)
    except Exception as e:
        print(f"  regime_snapshots: ERROR {e}")
    try:
        total += push_regime_timeline(hist)
    except Exception as e:
        print(f"  regime_timeline: ERROR {e}")
    try:
        total += push_macro()
    except Exception as e:
        print(f"  analytics_kv/macro: ERROR {e}")
    print(f"OK analytics sync complete — {total} row(s) upserted across all tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
