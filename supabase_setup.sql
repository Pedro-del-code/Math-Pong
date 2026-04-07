-- Execute isso no SQL Editor do Supabase

create table if not exists players (
  id    uuid primary key default gen_random_uuid(),
  name  text not null,
  turma text not null,
  wins  integer not null default 0,
  created_at timestamptz default now(),
  unique (name, turma)
);

-- Index para o leaderboard
create index if not exists players_wins_idx on players (wins desc);
