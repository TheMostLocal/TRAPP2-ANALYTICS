-- ============================================================
--  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
--  >>> FILE PATH:   supabase/heartbeat.sql
--  THE HEARTBEAT (z161, z163, z172): starts the loops from Supabase when GitHub's scheduler doesn't.
--  GitHub created only a few of the scheduled runs on 10-26..10-30; since z168 every loop keeps itself alive
--  Monday-Friday once started - this makes sure each one STARTS (Monday mornings, or after any break).
--  pg_cron (Supabase's built-in scheduler) calls GitHub's "run workflow" API through pg_net; a start while a loop
--  is already running just queues behind it (never two at once).
--
--  Run in the PORT+ANALYTICS Supabase project -> SQL Editor, in two steps:
--   1. once, with your token (fine-grained PAT; resource owner TheMostLocal; repositories TRAPP2, TRAPP2-1, TRAPP2-2,
--      TRAPP2-3, TRAPP2-4, TRAPP2-BOT, TRAPP2-OPTIONS, TRAPP2-ANALYTICS, TRAPP2-QUANT; permission Actions: Read and write):
--        select vault.create_secret('github_pat_...', 'gh_dispatch_token');
--      (to replace it later: select vault.update_secret((select id from vault.secrets where name = 'gh_dispatch_token'), 'github_pat_...');)
--   2. this whole file (safe to re-run: it replaces the jobs by name).
--  Check:  select jobname, schedule from cron.job order by 1;     (8 valuatio-* jobs)
--          select * from public.heartbeat_recent;                (what GitHub answered each kick; 204 = started)
--  Stop:   select cron.unschedule(jobname) from cron.job where jobname like 'valuatio-%';
-- ============================================================

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- z172: every kick is logged, so heartbeat_recent can show GitHub's answer next to it (RLS on, no policy: the API's
-- anon / authenticated roles see nothing; the SQL editor does)
create table if not exists public.heartbeat_log (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  repo        text,
  workflow    text,
  request_id  bigint
);
alter table public.heartbeat_log enable row level security;
revoke all on table public.heartbeat_log from anon, authenticated;

create or replace function public.kick_workflow(repo text, workflow text)
returns bigint
language plpgsql
security definer
set search_path = public, extensions, vault, net
as $$
declare
  tok text;
  req bigint;
begin
  select decrypted_secret into tok from vault.decrypted_secrets where name = 'gh_dispatch_token' limit 1;
  if tok is null then
    raise notice 'gh_dispatch_token is not in the vault - see the top of supabase/heartbeat.sql';
    insert into public.heartbeat_log (repo, workflow, request_id) values (repo, workflow, null);
    return null;
  end if;
  select net.http_post(
    url     := format('https://api.github.com/repos/TheMostLocal/%s/actions/workflows/%s/dispatches', repo, workflow),
    body    := jsonb_build_object('ref', 'main'),
    headers := jsonb_build_object('Authorization', 'Bearer ' || tok, 'Accept', 'application/vnd.github+json',
                                  'X-GitHub-Api-Version', '2022-11-28', 'User-Agent', 'valuatio-heartbeat',
                                  'Content-Type', 'application/json'),
    timeout_milliseconds := 10000
  ) into req;
  insert into public.heartbeat_log (repo, workflow, request_id) values (repo, workflow, req);
  return req;
end;
$$;
revoke all on function public.kick_workflow(text, text) from public, anon, authenticated;

-- what each kick got back, newest first (pg_net keeps replies about 6 hours)
create or replace view public.heartbeat_recent with (security_invoker = true) as
  select l.at, l.repo, l.workflow, r.status_code,
         case when l.request_id is null then 'no token in the vault (step 1)'
              when r.id is null then 'no reply yet'
              when r.status_code = 204 then 'started (or queued behind the running job)'
              when r.status_code = 401 then 'token rejected - expired or mistyped: make a new one, then vault.update_secret'
              when r.status_code = 403 then 'token lacks Actions: Read and write on this repo'
              when r.status_code = 404 then 'not found - is this repo in the token? is the workflow file named so?'
              when r.status_code = 422 then 'the workflow has no manual (workflow_dispatch) trigger'
              else coalesce(r.error_msg, left(r.content::text, 160)) end as meaning
    from public.heartbeat_log l
    left join net._http_response r on r.id = l.request_id
   order by l.at desc
   limit 60;
revoke all on public.heartbeat_recent from anon, authenticated;

-- times are UTC (pg_cron); each covers EDT and EST - the loops keep their own Eastern-time rules.
select cron.schedule('valuatio-intraday', '7 8-23 * * 1-5',
  $$ select public.kick_workflow(r, 'intraday.yml') from unnest(array['TRAPP2', 'TRAPP2-1', 'TRAPP2-2', 'TRAPP2-3', 'TRAPP2-4']) as r $$);   -- z178: + TRAPP2-4
select cron.schedule('valuatio-fx-crypto', '23 * * * *',
  $$ select public.kick_workflow('TRAPP2-1', 'fx-crypto-refresh.yml') $$);
select cron.schedule('valuatio-bot', '21 12-21 * * 1-5',
  $$ select public.kick_workflow('TRAPP2-BOT', 'bot_loop.yml') $$);
select cron.schedule('valuatio-options', '13 13-20 * * 1-5',
  $$ select public.kick_workflow('TRAPP2-OPTIONS', 'options-loop.yml') $$);
select cron.schedule('valuatio-health', '29,59 * * * *',
  $$ select public.kick_workflow('TRAPP2-ANALYTICS', 'health.yml') $$);
-- z163: the quant engine - 0920 ET either way (it waits inside the job when early)
select cron.schedule('valuatio-quant-engine', '20 13,14 * * 1-5',
  $$ select public.kick_workflow('TRAPP2-QUANT', 'engine.yml') $$);
-- z172: the quant backtest (the gate) every weekday evening - 1845 EST / 1945 EDT
select cron.schedule('valuatio-quant-backtest', '45 23 * * 1-5',
  $$ select public.kick_workflow('TRAPP2-QUANT', 'backtest.yml') $$);
-- z172: keep a week of the log
select cron.schedule('valuatio-heartbeat-prune', '5 4 * * *',
  $$ delete from public.heartbeat_log where at < now() - interval '7 days' $$);
