-- ============================================================
--  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
--  >>> FILE PATH:   supabase/heartbeat.sql
--  z161 - THE HEARTBEAT: starts the loops from Supabase when GitHub's scheduler doesn't.
--  2026-10-26: GitHub created no scheduled run for the books' intraday loops all morning, the FX loop ended at its
--  budget at 0447 ET with nothing to start the next one, and data health stopped at 0709 ET. Once a loop runs it now
--  keeps itself alive (z161 chain_next); this makes sure each one STARTS. pg_cron (Supabase's built-in scheduler)
--  calls GitHub's "run workflow" API through pg_net; a start while a loop is already running just queues behind it.
--
--  Run in the Analytics Supabase project → SQL Editor, in two steps:
--   1. once, with your token (fine-grained PAT, resource owner TheMostLocal, repositories TRAPP2, TRAPP2-1,
--      TRAPP2-2, TRAPP2-3, TRAPP2-BOT, TRAPP2-OPTIONS, TRAPP2-ANALYTICS, permission Actions: Read and write):
--        select vault.create_secret('github_pat_…', 'gh_dispatch_token');
--      (to replace it later: select vault.update_secret((select id from vault.secrets where name = 'gh_dispatch_token'), 'github_pat_…');)
--   2. this whole file (safe to re-run; it replaces the jobs by name).
--  Check: select * from cron.job;   ·   last calls: select status_code, content, created from net._http_response order by created desc limit 20;
--  (GitHub answers 204 with an empty body when a run was queued.)
-- ============================================================

create extension if not exists pg_cron;
create extension if not exists pg_net;

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
  return req;
end;
$$;
revoke all on function public.kick_workflow(text, text) from public, anon, authenticated;

-- times are UTC (pg_cron); each covers EDT and EST. Loops that are already running only get a queued follow-on.
select cron.schedule('valuatio-intraday', '7 8-23 * * 1-5',
  $$ select public.kick_workflow(r, 'intraday.yml') from unnest(array['TRAPP2', 'TRAPP2-1', 'TRAPP2-2', 'TRAPP2-3']) as r $$);
select cron.schedule('valuatio-fx-crypto', '23 * * * *',
  $$ select public.kick_workflow('TRAPP2-1', 'fx-crypto-refresh.yml') $$);
select cron.schedule('valuatio-bot', '21 12-21 * * 1-5',
  $$ select public.kick_workflow('TRAPP2-BOT', 'bot_loop.yml') $$);
select cron.schedule('valuatio-options', '13 13-20 * * 1-5',
  $$ select public.kick_workflow('TRAPP2-OPTIONS', 'options-loop.yml') $$);
select cron.schedule('valuatio-health', '29,59 * * * *',
  $$ select public.kick_workflow('TRAPP2-ANALYTICS', 'health.yml') $$);
