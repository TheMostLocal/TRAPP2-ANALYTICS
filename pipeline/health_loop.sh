#!/usr/bin/env bash
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/health_loop.sh
#  z168 - DATA HEALTH THAT KEEPS ITSELF RUNNING. The check is scheduled every 30 minutes, but on 10-29 / 10-30
#  GitHub started it only every 4-6 hours, so the Home card showed a 6-hour-old picture. One job now runs the
#  check at :19 and :49 for ~170 minutes, committing each result, then queues its own next run (24/7).
#  GitHub's schedule and the Supabase heartbeat stay as backstops; a start while one runs just queues.
#  z189 - THE ON-TIME CLOCK FOR THIS REPO'S WORKFLOWS. GitHub starts this account's crons 6-7 h late (SEC's 0705 ET run
#  landed ~1440 ET, the 0900 ET earnings run ~1600 ET, Analytics' 03:00 UTC run ~10:30 UTC), so after each check - and
#  at each slot, waking from its sleep for it - the loop queues whatever is due with pipeline/ontime.py: SEC, earnings,
#  headcount, Analytics (03:00 UTC once the five books' Nightlies are in; 14:30 UTC), the hourly Supabase syncs.
#  Each workflow's own cron stays as the backstop. ONTIME=0 turns it off.
#  Env: LOOP_BUDGET_MIN (170), GH_TOKEN (the job's own token, for chaining and queueing), SUPABASE_URL / SUPABASE_SERVICE_ROLE.
# ============================================================
set -u
BRANCH="${GITHUB_REF_NAME:-main}"
BUDGET=$(( ${LOOP_BUDGET_MIN:-170} * 60 ))
START=$(date +%s)
git config user.name "valuatio-health"
git config user.email "actions@github.com"

chain_next() {
  [ -n "${GH_TOKEN:-}" ] || { echo "::notice::GH_TOKEN not set - not chaining"; return 0; }
  local wf="${GITHUB_WORKFLOW_REF:-}"
  wf="${wf%@*}"; wf="${wf##*/}"
  { [ -n "$wf" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; } || return 0
  if gh workflow run "$wf" --ref "$BRANCH" -R "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
    echo "chained: the next run of $wf is queued"
  else
    echo "::warning::could not queue the next run of $wf (does the workflow grant actions: write?)"
  fi
}

one_pass() {
  git fetch -q origin "$BRANCH" 2>/dev/null && { git merge -q --ff-only FETCH_HEAD 2>/dev/null || git reset -q --hard FETCH_HEAD; }
  python pipeline/data_health.py || { echo "::warning::health check failed this pass"; return 0; }
  git add data/health.json
  if git diff --staged --quiet; then echo "  no change"; return 0; fi
  git commit -q -m "health: $(TZ=America/New_York date +%Y-%m-%d\ %H%M) ET [skip ci]"
  for k in 1 2 3 4 5; do
    if git push -q origin "HEAD:$BRANCH"; then echo "  pushed (attempt $k)"; return 0; fi
    git fetch -q origin "$BRANCH"; git merge -X ours "origin/$BRANCH" --no-edit -q 2>/dev/null || true; sleep $(( k * 3 ))
  done
  echo "::warning::push failed after 5 attempts"
}

# z189: queue this repo's workflows on time (pipeline/ontime.py - best-effort, a failure is a log line). nap sleeps
# like `sleep`, but wakes on the way for each slot and queues it.
ontime_run()  { [ "${ONTIME:-1}" = "1" ] && [ -f pipeline/ontime.py ] && python pipeline/ontime.py run; return 0; }
ontime_next() { [ "${ONTIME:-1}" = "1" ] && [ -f pipeline/ontime.py ] && python pipeline/ontime.py next 2>/dev/null; return 0; }
nap() {
  local end now n
  end=$(( $(date +%s) + $1 ))
  while now=$(date +%s); [ "$now" -lt "$end" ]; do
    n=$(ontime_next)
    if [ -n "$n" ] && [ "$n" -ge 0 ] 2>/dev/null && [ $(( now + n )) -le $(( end + 5 )) ]; then
      [ "$n" -gt 0 ] && sleep "$n"
      ontime_run
    else
      sleep $(( end - now ))
    fi
  done
}

next_wait() {   # seconds to the next :19 or :49 (minute ≡ 19 mod 30), at least 60 s away
  local now m s w
  now=$(date +%s); m=$(( (now / 60) % 60 )); s=$(( now % 60 ))
  w=$(( ( (19 - m % 30 + 30) % 30 ) * 60 - s ))
  [ "$w" -lt 60 ] && w=$(( w + 1800 ))
  echo "$w"
}

i=0
while :; do
  i=$((i + 1))
  echo "── check $i · $(TZ=America/New_York date +'%a %H:%M') ET"
  one_pass
  ontime_run                       # z189: anything due now (a slot passed while the check ran, or this job just started)
  w=$(next_wait)
  if [ $(( $(date +%s) - START + w + 120 )) -gt "$BUDGET" ]; then
    echo "budget reached after $i check(s) - queueing the next run (24/7)"
    chain_next
    break
  fi
  echo "  next check in $(( w / 60 )) min"
  nap "$w"
done
