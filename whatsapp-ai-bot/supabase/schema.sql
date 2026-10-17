-- Run this in Supabase SQL Editor.
create table if not exists public.chat_memory (
  phone text primary key,
  history jsonb not null default '[]'::jsonb,
  updated_at timestamptz not null default now()
);

-- This table is accessed by the server using the service-role key only.
-- Do not expose SUPABASE_SERVICE_ROLE_KEY in client-side code.
alter table public.chat_memory enable row level security;
