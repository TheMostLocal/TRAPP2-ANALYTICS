#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-CORE       FILE PATH: pipeline/ontime.py
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS  FILE PATH: pipeline/ontime.py   (the same file - keep them identical)
#
#  z189 - START THIS REPO'S SCHEDULED WORKFLOWS ON TIME.
#  GitHub starts this account's cron runs 6-7.5 h late and drops some (11-10..11-16: the 00:30-01:07 UTC Nightlies
#  committed 07:00-08:10 UTC, Analytics' 03:00 UTC run ~10:30 UTC, SEC's 0705 ET run ~1440 ET, the 0900 ET earnings
#  run ~1600 ET, the 0807 ET mid-day regime refresh mid-afternoon), while a workflow_dispatch run starts in seconds.
#  So the loops that already keep time - each book's session loop (intraday_loop.sh, weeknights bridged) and
#  TRAPP2-ANALYTICS' 24/7 health loop (health_loop.sh) - queue the repo's own workflows at their slots with
#  `gh workflow run`, using the job's own token (the loop's workflow grants actions: write; a dispatch is the one
#  thing a GITHUB_TOKEN may start).
#
#  GitHub's schedule stays as the backstop. The heavy workflows' gates (the books' Nightly; Analytics, SEC, earnings,
#  headcount) let a late scheduled start stand down when an on-time or manual run has started since that slot -
#  unless it failed or was cancelled (then the late start is the retry). The light ones (mid-day, TRAPP2-1's extras /
#  Fed / quad / trade / nowcast, the hourly Supabase syncs) simply run again when GitHub gets to them - same result.
#
#  A slot counts as started when ANY run of the workflow (on-time, manual, an on-time cron) was created from 10 min
#  before it on - then nothing is queued. A slot is only queued inside its `late` window; past that the cron backstop
#  has it. One preconditioned slot: Analytics' 03:00 UTC run waits (up to `force` minutes) until all five books'
#  Nightlies have committed, so the grades are computed on tonight's books.
#  Keep the table in step with each workflow's cron and gate.
#
#    python pipeline/ontime.py run     queue every due slot that hasn't started (a line per decision)
#    python pipeline/ontime.py next    seconds to the next slot (strictly ahead, within 26 h), or nothing
#    python pipeline/ontime.py list    this repo's slots for the next 24 h (no API calls)
#  Env: GITHUB_REPOSITORY (owner/repo) · GH_TOKEN · GITHUB_REF_NAME (main) · ONTIME=0 (off) · ONTIME_DRY=1 (say, don't
#       queue) · ONTIME_STATE (per-job memo of settled slots, default $RUNNER_TEMP/ontime_state.json) · ONTIME_NOW (tests)
# ============================================================
import json
import math
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
BOOKS = ("TRAPP2", "TRAPP2-1", "TRAPP2-2", "TRAPP2-3", "TRAPP2-4")
LEAD = timedelta(minutes=10)          # a run created this long before a slot still counts for it
NIGHTLY_MSG = "Nightly equities refresh"


def S(wf, at, tz="ET", days="trading", late=120, **kw):
    """One slot line. at: "HH:MM" (or "*:MM" = every hour) in tz (ET or UTC). days (judged on the ET date at the slot):
    'trading' (NYSE trading day), 'weekdays', 'daily', weekday names ('mon,thu'); combine with commas ('trading,thu').
    late: minutes after the slot it may still be queued. after='nightlies' + force: wait for the books' Nightlies, up to
    `force` minutes after the slot, then queue anyway."""
    return dict(wf=wf, at=list(at), tz=tz, days=days, late=late, **kw)


# Each book's slots mirror its workflows' crons; the Eastern ones are what the ET gates keep (twin EDT / EST crons).
TABLE = {
    "TRAPP2-ANALYTICS": [
        S("sec.yml", ["07:05", "18:10"], days="weekdays", late=150),
        S("earnings.yml", ["09:00", "17:30", "21:00"], days="weekdays", late=90),
        S("headcount.yml", ["06:40"], days="mon,thu", late=240),
        S("analytics.yml", ["14:30"], tz="UTC", days="weekdays", late=120),
        S("analytics.yml", ["03:00"], tz="UTC", days="trading", late=420, after="nightlies", force=360),
        S("supabase-sync.yml", ["*:20"], tz="UTC", days="daily", late=30),
        S("ticker-snapshot-sync.yml", ["*:30"], tz="UTC", days="daily", late=30),
    ],
    "TRAPP2": [
        S("nightly.yml", ["00:30"], tz="UTC", late=540),
        S("midday.yml", ["08:07", "11:32"]),
    ],
    "TRAPP2-1": [
        S("nightly.yml", ["00:30"], tz="UTC", late=540),
        S("midday.yml", ["08:07", "11:32"]),
        S("extras.yml", ["07:25", "18:20"]),
        S("fed-monitor.yml", ["09:00", "14:45"], late=90),
        S("fed-monitor.yml", ["17:00"], days="trading,thu", late=90),
        S("macro-quad.yml", ["09:15"]),
        S("macro-trade-refresh.yml", ["09:35"]),
        S("quad-nowcast.yml", ["22:30"], tz="UTC"),
    ],
    "TRAPP2-2": [
        S("nightly.yml", ["00:41"], tz="UTC", late=540),
        S("midday.yml", ["08:07", "11:32"]),
    ],
    "TRAPP2-3": [
        S("nightly.yml", ["00:51"], tz="UTC", late=540),
        S("midday.yml", ["08:11", "11:36"]),
    ],
    "TRAPP2-4": [
        S("nightly.yml", ["01:07"], tz="UTC", late=540),
        S("midday.yml", ["08:14", "11:39"]),
    ],
}

# ------------------------------------------------------------------
#  NYSE calendar - the same rules as TRAPP2-CORE append_intraday_snapshot.market_holidays (self-contained here, because
#  TRAPP2-ANALYTICS has no CORE checkout; tests/test_ontime.py checks the two agree).
# ------------------------------------------------------------------
def _easter(y):
    a = y % 19
    b, c = divmod(y, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(y, month, day + 1)


def _observed(d, allow_friday=True):
    if d.weekday() == 5:
        return d - timedelta(days=1) if allow_friday else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nth(y, m, wd, n):
    d = date(y, m, 1)
    return d + timedelta(days=(wd - d.weekday()) % 7 + 7 * (n - 1))


def _last(y, m, wd):
    d = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - wd) % 7)


_HOL = {}


def holidays(y):
    if y not in _HOL:
        h = {_nth(y, 1, 0, 3), _nth(y, 2, 0, 3), _easter(y) - timedelta(days=2), _last(y, 5, 0),
             _observed(date(y, 7, 4)), _nth(y, 9, 0, 1), _nth(y, 11, 3, 4), _observed(date(y, 12, 25))}
        ny = _observed(date(y, 1, 1), allow_friday=False)       # a Saturday New Year is not observed on Dec 31
        if ny:
            h.add(ny)
        if y >= 2022:
            h.add(_observed(date(y, 6, 19)))
        _HOL[y] = h
    return _HOL[y]


def trading_day(d):
    return d.weekday() < 5 and d not in holidays(d.year)


_WD = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def day_ok(days, inst):
    d = inst.astimezone(ET).date()
    toks = [t.strip().lower() for t in str(days).split(",") if t.strip()]
    names = {_WD[t] for t in toks if t in _WD}
    if "weekdays" in toks:
        names |= {0, 1, 2, 3, 4}
    if names and d.weekday() not in names:
        return False
    if "trading" in toks and not trading_day(d):
        return False
    return True


def slots(entry, around):
    """The entry's slot instants (UTC) from two days before `around` to two days after."""
    tz = ET if entry["tz"] == "ET" else UTC
    loc = around.astimezone(tz)
    out = set()
    for dd in range(-2, 3):
        d = (loc + timedelta(days=dd)).date()
        for at in entry["at"]:
            hh, mm = at.split(":")
            for h in (range(24) if hh == "*" else [int(hh)]):
                inst = datetime(d.year, d.month, d.day, h, int(mm), tzinfo=tz).astimezone(UTC)
                if day_ok(entry["days"], inst):
                    out.add(inst)
    return sorted(out)


# ------------------------------------------------------------------
def now_utc():
    v = os.environ.get("ONTIME_NOW", "").strip()
    if v:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC)
    return datetime.now(UTC)


def repo_full():
    return os.environ.get("ONTIME_REPO") or os.environ.get("GITHUB_REPOSITORY", "")


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def label(t):
    return f"{t.astimezone(ET):%a %H:%M} ET ({t.astimezone(UTC):%H:%M} UTC)"


def gh(args, timeout=30):
    """(stdout, error) of a gh call; stdout is None on any failure."""
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except Exception as e:                       # gh missing, timeout
        return None, str(e)[:160]
    if p.returncode != 0:
        return None, ((p.stderr or "") + (p.stdout or "")).strip().replace("\n", " ")[:200]
    return p.stdout, ""


class State:
    """Per-job memo of slots already settled, so the loops don't ask GitHub again every pass."""
    def __init__(self):
        base = os.environ.get("RUNNER_TEMP") or "/tmp"
        self.path = Path(os.environ.get("ONTIME_STATE") or Path(base) / "ontime_state.json")
        try:
            self.d = json.loads(self.path.read_text())
        except Exception:
            self.d = {}

    def get(self, k):
        return self.d.get(k)

    def put(self, k, v):
        self.d[k] = v

    def save(self, now):
        cut = iso(now - timedelta(days=3))
        self.d = {k: v for k, v in self.d.items() if k.split("@", 1)[-1] >= cut}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.d, indent=0, sort_keys=True))
        except Exception:
            pass


_RUNS = {}


def runs_of(wf):
    """The workflow's latest runs [(created, event, conclusion)], or None when GitHub can't be read."""
    if wf not in _RUNS:
        out, err = gh(["run", "list", "-R", repo_full(), "-w", wf, "-L", "40",
                       "--json", "databaseId,event,createdAt,status,conclusion"])
        if out is None:
            print(f"::warning::ontime: can't list {wf} runs ({err}) - not queueing it (the cron backstop has it)")
            _RUNS[wf] = None
        else:
            try:
                rows = json.loads(out or "[]")
                _RUNS[wf] = [(datetime.fromisoformat(str(r.get("createdAt")).replace("Z", "+00:00")),
                              r.get("event"), r.get("conclusion")) for r in rows if r.get("createdAt")]
            except Exception as e:
                print(f"::warning::ontime: unreadable run list for {wf} ({str(e)[:80]})")
                _RUNS[wf] = None
    return _RUNS[wf]


def nightlies_pending(slot, st):
    """Books whose Nightly hasn't committed since 6 h before the slot (after the close). Unreadable counts as pending;
    a book seen fresh is remembered for the slot (st), so later passes only ask about the rest."""
    owner = repo_full().split("/")[0] or "TheMostLocal"
    since = iso(slot - timedelta(hours=6))
    jq = f'[.[] | select(.commit.message | startswith("{NIGHTLY_MSG}"))] | length'
    pending = []
    for b in BOOKS:
        key = f"nightly:{b}@{iso(slot)}"
        if st.get(key):
            continue
        out, err = gh(["api", f"repos/{owner}/{b}/commits?path=data/master.json&since={since}&per_page=100", "--jq", jq])
        try:
            n = int((out or "").strip() or "0") if out is not None else -1
        except ValueError:
            n = -1
        if n > 0:
            st.put(key, "fresh")
        else:
            pending.append(b if n == 0 else f"{b} (unreadable)")
    return pending


def table():
    name = repo_full().split("/")[-1]
    return name, TABLE.get(name, [])


def cmd_run():
    if os.environ.get("ONTIME", "1") == "0":
        return 0
    name, entries = table()
    if not entries:
        return 0
    now = now_utc()
    dry = os.environ.get("ONTIME_DRY", "") not in ("", "0", "false")
    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    st = State()
    for e in entries:
        for s in slots(e, now):
            if not (s <= now < s + timedelta(minutes=e["late"])):
                continue
            key = f"{e['wf']}@{iso(s)}"
            if st.get(key):
                continue
            late_min = int((now - s).total_seconds() // 60)
            runs = runs_of(e["wf"])
            if runs is None:
                continue
            seen = [r for r in runs if r[0] >= s - LEAD]
            if seen:
                c, ev, concl = max(seen, key=lambda r: r[0])
                st.put(key, f"started {iso(c)} ({ev})")
                print(f"ontime: {e['wf']} {label(s)} already started {c.astimezone(ET):%H:%M} ET ({ev}{', ' + concl if concl else ''})")
                continue
            if e.get("after") == "nightlies":
                pend = nightlies_pending(s, st)
                if pend and now < s + timedelta(minutes=e.get("force", e["late"])):
                    print(f"ontime: {e['wf']} {label(s)} waits for the Nightlies: {', '.join(pend)}")
                    continue
                if pend:
                    print(f"ontime: {e['wf']} {label(s)} - Nightlies still pending ({', '.join(pend)}), queueing anyway")
            if dry:
                print(f"ontime: DRY - would queue {e['wf']} for {label(s)} ({late_min} min after the slot)")
                continue
            out, err = gh(["workflow", "run", e["wf"], "--ref", branch, "-R", repo_full()])
            if out is None:
                print(f"::warning::ontime: could not queue {e['wf']} for {label(s)}: {err}")
                continue
            st.put(key, f"queued {iso(now)}")
            _RUNS.pop(e["wf"], None)
            print(f"ontime: queued {e['wf']} for {label(s)}" + (f", {late_min} min after it" if late_min >= 2 else ""))
    st.save(now)
    return 0


def next_seconds(now=None):
    if os.environ.get("ONTIME", "1") == "0":
        return None
    now = now or now_utc()
    _, entries = table()
    ahead = [s for e in entries for s in slots(e, now) if s > now and s - now <= timedelta(hours=26)]
    if not ahead:
        return None
    return int(math.ceil((min(ahead) - now).total_seconds())) + 1


def cmd_list():
    name, entries = table()
    now = now_utc()
    print(f"ontime: {name or '(no repo)'} · {len(entries)} line(s) · now {label(now)}")
    for e in entries:
        nxt = [s for s in slots(e, now) if now - timedelta(minutes=e["late"]) <= s <= now + timedelta(hours=24)]
        extra = f" · after the Nightlies (forced +{e.get('force')} min)" if e.get("after") else ""
        print(f"  {e['wf']:28} {e['tz']:3} {','.join(e['at']):22} {e['days']:12} late ≤{e['late']} min{extra}")
        for s in nxt[:4 if e["at"][0].startswith("*") else 12]:
            due = s <= now < s + timedelta(minutes=e["late"])
            print(f"      {label(s)}{'  ← due' if due else ''}")
    n = next_seconds(now)
    print(f"  next slot in {n} s" if n is not None else "  no slot in the next 26 h")
    return 0


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "run").lower()
    try:
        if cmd == "run":
            return cmd_run()
        if cmd == "next":
            n = next_seconds()
            if n is not None:
                print(n)
            return 0
        if cmd == "list":
            return cmd_list()
    except Exception as e:                       # never break the loop that calls this (next: nothing on stdout)
        print(f"::warning::ontime {cmd}: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr if cmd == "next" else sys.stdout)
        return 0
    print("usage: ontime.py run | next | list")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
