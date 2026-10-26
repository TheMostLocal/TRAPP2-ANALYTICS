-- ============================================================
--  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
--  >>> FILE PATH:   supabase/live_schema.sql
--  z159 - PHASE 2: live market data. Run ONCE in the Analytics Supabase project → SQL Editor → New query →
--  paste → Run. Safe to re-run. The books write with the service key (it bypasses RLS); the app reads with
--  the anon key, which these policies allow to SELECT and nothing else. The same data is public in the
--  book repos already. Size: ~1,000 quote rows + ~1,000 bar rows a day (~10 KB each), 4 days kept.
-- ============================================================

create table if not exists public.live_quotes (
  ticker      text primary key,
  book        text,
  price       double precision,
  change      double precision,
  changepct   double precision,
  priceopen   double precision,
  high        double precision,
  low         double precision,
  closeyest   double precision,
  volume      double precision,
  marketcap   double precision,
  currency    text,
  date        date,
  fetched_at  timestamptz,
  pushed_at   timestamptz not null default now()
);
create index if not exists live_quotes_fetched_at_idx on public.live_quotes (fetched_at desc);

create table if not exists public.live_bars_day (
  ticker      text not null,
  date        date not null,
  book        text,
  pc          double precision,          -- prior close
  oo          double precision,          -- official open (0930 ET)
  oc          double precision,          -- official close (1600 ET)
  ext         jsonb,                     -- pre / post-market quote {s, px, ref, pct, t}
  b           jsonb not null,            -- [[t (epoch s), o, h, l, c, v], ...] 5-minute bars
  updated_at  timestamptz,
  pushed_at   timestamptz not null default now(),
  primary key (ticker, date)
);
create index if not exists live_bars_day_date_idx on public.live_bars_day (date);

alter table public.live_quotes   enable row level security;
alter table public.live_bars_day enable row level security;

drop policy if exists "read live_quotes"   on public.live_quotes;
drop policy if exists "read live_bars_day" on public.live_bars_day;
create policy "read live_quotes"   on public.live_quotes   for select to anon, authenticated using (true);
create policy "read live_bars_day" on public.live_bars_day for select to anon, authenticated using (true);

grant select on public.live_quotes, public.live_bars_day to anon, authenticated;
grant all    on public.live_quotes, public.live_bars_day to service_role;
