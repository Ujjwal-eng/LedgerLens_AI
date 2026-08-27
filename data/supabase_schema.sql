-- ============================================================
-- LedgerLens AI — Supabase schema
-- Run this once in the Supabase SQL editor (Project → SQL Editor).
-- Safe to re-run: everything is IF NOT EXISTS / ON CONFLICT.
-- ============================================================

create extension if not exists pgcrypto;

-- ------------------------------------------------------------
-- invoices: one row per processed (or in-flight/pending) invoice.
-- This is the durable replacement for the old in-memory REGISTRY /
-- web_app_state.json. "Approval queue" is just this table filtered
-- to pending_approval IS NOT NULL — there's no separate queue table.
-- ------------------------------------------------------------
create table if not exists public.invoices (
    thread_id           text primary key,
    user_id             uuid not null references auth.users(id) on delete cascade,
    filename            text not null,
    -- "supabase://<bucket>/<path>" for a real user upload, or an
    -- absolute local path for a bundled sample invoice (samples ship
    -- with the app and are shared by everybody, so they never live
    -- in per-user Storage).
    source_path         text,
    created_at          timestamptz not null default now(),
    invoice              jsonb,
    extraction_error     text,
    guardrail_passed     boolean,
    guardrail_violations jsonb not null default '[]'::jsonb,
    compliance           jsonb,
    risk                 jsonb,
    decision             text,
    pending_approval     jsonb,
    contract             jsonb
);

create index if not exists invoices_user_created_idx
    on public.invoices (user_id, created_at desc);

-- Adds a nullable timestamp that only gets set once an invoice reaches a
-- terminal decision (approved_exported / rejected), separate from
-- created_at (which is stamped once, at upload time, and never changes).
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS decided_at timestamptz;

-- ::jsonb parses the existing stringified rows back into real JSON
-- objects in the same statement, so no data is lost.
alter table public.invoices
    alter column decision type jsonb using decision::jsonb;

-- ------------------------------------------------------------
-- staged_uploads: browser PDFs that have been uploaded but not yet
-- paired with a vendor contract / run through the graph.
-- ------------------------------------------------------------
create table if not exists public.staged_uploads (
    upload_id    text primary key,
    user_id      uuid not null references auth.users(id) on delete cascade,
    filename     text not null,
    source_path  text not null,   -- always "supabase://<bucket>/<path>"
    created_at   timestamptz not null default now(),
    contract     jsonb
);

create index if not exists staged_uploads_user_idx
    on public.staged_uploads (user_id);


-- ------------------------------------------------------------
-- temp_contracts: manually-added ("temporary") per-user vendor
-- contracts. Never touches the shared vendor_contracts.json file.
-- ------------------------------------------------------------
create table if not exists public.temp_contracts (
    id                    uuid primary key default gen_random_uuid(),
    user_id               uuid not null references auth.users(id) on delete cascade,
    vendor_key            text not null,   -- lowercased vendor_name, used for lookups
    vendor_name           text not null,
    gstin                 text not null,
    payment_terms_days    integer not null,
    max_invoice_amount    numeric not null,
    discount_percentage   numeric,
    pricing_rules         jsonb not null default '{}'::jsonb,
    clauses               jsonb not null default '[]'::jsonb,
    created_at            timestamptz not null default now(),
    unique (user_id, vendor_key)
);

create index if not exists temp_contracts_user_idx
    on public.temp_contracts (user_id);

-- ------------------------------------------------------------
-- Row Level Security. The FastAPI backend talks to Postgres with the
-- SERVICE ROLE key (which bypasses RLS) and enforces ownership in
-- application code — same pattern the app already used for its old
-- in-memory dicts. These policies exist as defense-in-depth in case
-- anything is ever queried with the anon/user key directly.
-- ------------------------------------------------------------
alter table public.invoices        enable row level security;
alter table public.staged_uploads  enable row level security;
alter table public.temp_contracts  enable row level security;

drop policy if exists "invoices_owner_all" on public.invoices;
create policy "invoices_owner_all" on public.invoices
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "staged_uploads_owner_all" on public.staged_uploads;
create policy "staged_uploads_owner_all" on public.staged_uploads
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "temp_contracts_owner_all" on public.temp_contracts;
create policy "temp_contracts_owner_all" on public.temp_contracts
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ------------------------------------------------------------
-- Storage bucket for uploaded invoice PDFs. PRIVATE — no public
-- policy is added, because the backend only ever touches it with the
-- service role key (which bypasses Storage policies too) and hands
-- the browser short-lived signed URLs when it needs to show a PDF.
-- ------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('invoices', 'invoices', false)
on conflict (id) do nothing;

