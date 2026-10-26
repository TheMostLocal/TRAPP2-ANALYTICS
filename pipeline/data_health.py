#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/data_health.py
#
#  z157 - DATA HEALTH. Every input the app and the bot depend on, across all the repos, checked for
#  freshness every 30 minutes against what its schedule promises (market-hours aware, Eastern time):
#  intraday quotes and tapes, FX, crypto, the bot journal and equity tape, option chains, regime, quad,
#  Fed, earnings, headcount, credit, research, news. A failing workflow used to show up as a quietly
#  old number (FX six hours stale, a journal that stopped, an earnings table with empty sectors); now
#  it shows up here, on the app's Home page and in the bot's log.
#    -> data/health.json { asOf, summary: {ok, stale, missing, notDue}, checks: [...] }
# ============================================================
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path(__file__).resolve().parent.parent / "data" / "health.json"
RAW = "https://raw.githubusercontent.com/TheMostLocal"
ET = ZoneInfo("America/New_York")
BOOKS = ("TRAPP2", "TRAPP2-1", "TRAPP2-2", "TRAPP2-3")


def fetch(repo, path):
    req = urllib.request.Request(f"{RAW}/{repo}/main/{path}?_={int(datetime.now().timestamp() // 60)}", headers={"User-Agent": "valuatio-health"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode())


def parse(ts):
    if not ts:
        return None
    s = str(ts).strip()
    try:
        if s.isdigit() and len(s) >= 10:
            return datetime.fromtimestamp(int(s[:10]), tz=timezone.utc)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def stamp_of(doc, kind):
    """The freshest timestamp a file states about itself."""
    if kind == "rows":                                           # master.json: newest row fetch
        rows = doc if isinstance(doc, list) else doc.get("rows") or []
        ts = [parse(r.get("fetched_at")) for r in rows if isinstance(r, dict)]
        ts = [t for t in ts if t]
        return max(ts) if ts else None
    if kind == "equity":                                         # bot equity tape: last point
        rows = doc if isinstance(doc, list) else doc.get("points") or []
        return parse(rows[-1].get("t")) if rows else None
    if isinstance(doc, dict):
        for k in ("updatedAt", "generatedAt", "asOf", "generated_at", "fetched_at", "fetchedAt", "lastUpdated", "at", "computedAt"):
            if doc.get(k):
                return parse(doc[k])
        cur = doc.get("current")
        if isinstance(cur, dict):
            for k in ("asOf", "date", "computedAt"):
                if cur.get(k):
                    return parse(cur[k])
    return None


def session(now_et):
    """'regular' 0930-1600, 'extended' 0400-2000, else 'closed' (weekends closed; holidays read as closed by staleness only)."""
    if now_et.weekday() >= 5:
        return "closed"
    m = now_et.hour * 60 + now_et.minute
    return "regular" if 570 <= m < 960 else "extended" if 240 <= m < 1200 else "closed"


def fx_open(now_et):
    """FX trades Sunday 1700 ET → Friday 1700 ET."""
    wd, m = now_et.weekday(), now_et.hour * 60 + now_et.minute
    return not (wd == 5 or (wd == 6 and m < 1020) or (wd == 4 and m >= 1020))


# name, repo, path, kind, rule, feeds the bot
CHECKS = []
for b in BOOKS:
    CHECKS += [(f"{b} quotes", b, "data/master.json", "rows", "intraday", True),
               (f"{b} 15-min tape", b, "data/intraday/latest.json", "doc", "intraday", False)]
CHECKS += [
    ("FX rates", "TRAPP2-1", "data/fx/rates.json", "doc", "fx", True),
    ("Crypto prices", "TRAPP2-1", "data/crypto/prices.json", "doc", "24h", False),
    ("Regime certainty", "TRAPP2-1", "data/regime_certainty.json", "doc", "daily", True),
    ("Macro quad", "TRAPP2-1", "data/macro/quad.json", "doc", "daily", True),
    ("Fed monitor", "TRAPP2-1", "data/fed/monitor.json", "doc", "daily", True),
    ("Bot journal", "TRAPP2-BOT", "data/bot_training_data.json", "doc", "session", True),
    ("Bot equity tape", "TRAPP2-BOT", "data/bot_equity_history.json", "equity", "session", False),
    ("Option chains", "TRAPP2-OPTIONS", "data/options/summary.json", "doc", "session", True),
    ("Earnings season", "TRAPP2-ANALYTICS", "data/earnings/season.json", "doc", "daily", False),
    ("Earnings calendar", "TRAPP2-ANALYTICS", "data/earnings/calendar.json", "doc", "daily", True),
    ("Headcount", "TRAPP2-ANALYTICS", "data/headcount/summary.json", "doc", "weekly", True),
    ("Research grades", "TRAPP2-ANALYTICS", "data/research_grades.json", "doc", "daily", True),
    ("News", "TRAPP2-ANALYTICS", "data/news/latest.json", "doc", "daily", False),
    ("Credit index (bonds / CDS)", "TRAPP2-CREDIT", "data/credit_index.json", "doc", "daily", False),
    # z158: the new official sources
    ("SEC filings (8-K / Form 4)", "TRAPP2-ANALYTICS", "data/sec/index.json", "doc", "daily", True),
    ("FINRA short selling", "TRAPP2-ANALYTICS", "data/short/index.json", "doc", "daily", True),
    ("CBOE volatility", "TRAPP2-1", "data/extras/vol.json", "doc", "daily", True),
    ("Treasury auctions", "TRAPP2-1", "data/extras/treasury.json", "doc", "daily", False),
    ("FX / crypto cross-checks", "TRAPP2-1", "data/extras/checks.json", "doc", "daily", False),
]


def supabase_live_check(now, now_et):
    """z159: the newest live quote in the Analytics Supabase project (the books push after every pass)."""
    import os
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/").replace("/rest/v1", "")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE") or os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not (url and key):
        return None
    lim = limit_minutes("intraday", now_et)
    row = {"name": "Supabase live quotes", "repo": "Supabase (Analytics)", "path": "live_quotes", "feedsBot": False, "rule": "intraday", "maxMinutes": lim}
    try:
        req = urllib.request.Request(f"{url}/rest/v1/live_quotes?select=fetched_at&order=fetched_at.desc&limit=1",
                                     headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.loads(r.read().decode())
        ts = parse(rows[0]["fetched_at"]) if rows else None
        if ts is None:
            row.update(status="missing", note="table empty - books not pushing yet (SUPABASE_LIVE_* secrets?)")
        else:
            age = (now - ts).total_seconds() / 60
            row.update(updated=ts.strftime("%Y-%m-%dT%H:%M:%SZ"), ageMinutes=round(age), status="notDue" if lim is None else ("ok" if age <= lim else "stale"))
            # z161.2: per book - one book pushing looks fine by "newest row" alone (10-26: 246 rows, TRAPP2-1 only)
            cut = (now - timedelta(minutes=lim or 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            counts = {}
            for b in BOOKS:
                rq = urllib.request.Request(f"{url}/rest/v1/live_quotes?select=ticker&book=eq.{b}&fetched_at=gte.{cut}",
                                            headers={"apikey": key, "Authorization": f"Bearer {key}", "Prefer": "count=exact", "Range": "0-0"})
                with urllib.request.urlopen(rq, timeout=30) as r2:
                    cr = r2.headers.get("Content-Range") or ""
                counts[b] = int(cr.split("/")[-1]) if cr.split("/")[-1].isdigit() else 0
            row["perBook"] = counts
            quiet = [b for b, n in counts.items() if n == 0]
            row["note"] = "fresh rows per book: " + " · ".join(f"{b} {n}" for b, n in counts.items())
            if lim is not None and quiet:
                row["status"] = "stale"
                row["note"] += f" - not pushing: {', '.join(quiet)}"
    except Exception as e:
        row.update(status="missing", note=f"{str(e)[:100]} - run supabase/live_schema.sql?")
    return row


def limit_minutes(rule, now_et):
    """How old a file may be right now, or None when it isn't due (market closed)."""
    sess = session(now_et)
    if rule == "intraday":
        return 35 if sess == "regular" else 50 if sess == "extended" else None
    if rule == "session":
        return 50 if sess == "regular" else None
    if rule == "fx":
        return 45 if fx_open(now_et) else None
    if rule == "24h":
        return 45
    if rule == "daily":
        return 60 * (78 if now_et.weekday() in (5, 6, 0) else 30)   # weekday jobs: Saturday → Monday morning reach back to Friday
    if rule == "weekly":
        return 60 * 24 * 5
    return None


def main():
    now = datetime.now(timezone.utc)
    now_et = now.astimezone(ET)
    out = []
    for name, repo, path, kind, rule, bot in CHECKS:
        lim = limit_minutes(rule, now_et)
        row = {"name": name, "repo": repo, "path": path, "feedsBot": bot, "rule": rule, "maxMinutes": lim}
        try:
            doc = fetch(repo, path)
            ts = stamp_of(doc, kind)
            if ts is None:
                row.update(status="unknown", note="no timestamp in the file")
            else:
                age = (now - ts).total_seconds() / 60
                row.update(updated=ts.strftime("%Y-%m-%dT%H:%M:%SZ"), ageMinutes=round(age))
                row["status"] = "notDue" if lim is None else ("ok" if age <= lim else "stale")
        except Exception as e:
            row.update(status="missing", note=str(e)[:120])
        out.append(row)
    sb = supabase_live_check(now, now_et)
    if sb:
        out.append(sb)
    summ = {k: sum(1 for r in out if r["status"] == k) for k in ("ok", "stale", "missing", "notDue", "unknown")}
    bad_bot = [r["name"] for r in out if r["feedsBot"] and r["status"] in ("stale", "missing")]
    doc = {"asOf": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "session": session(now_et), "summary": summ,
           "botInputsDown": bad_bot, "checks": out}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1))
    print(f"[health] {now_et:%a %H:%M} ET · {session(now_et)} · ok {summ['ok']} · stale {summ['stale']} · missing {summ['missing']} · not due {summ['notDue']}"
          + (f" · BOT INPUTS DOWN: {', '.join(bad_bot)}" if bad_bot else ""))
    for r in out:
        if r["status"] in ("stale", "missing", "unknown"):
            print(f"  {r['status'].upper():8} {r['name']:28} {r.get('ageMinutes', '—')} min (limit {r['maxMinutes']}) · {r['repo']}/{r['path']} {r.get('note', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
