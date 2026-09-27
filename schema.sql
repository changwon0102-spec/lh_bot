-- Run once in Supabase > SQL Editor. No external extensions required.
-- announcements contains ONLY successfully delivered announcements.
-- post_id is namespaced by source: LH panId, SH board id + seq.
-- created_at is the successful insert time; published_at is the board date.
begin;

create table if not exists public.announcements (
    source text not null check (source in ('LH', 'SH')),
    post_id text not null,
    title text not null,
    url text not null,
    published_at date,
    summary jsonb not null,
    telegram_message_id bigint not null,
    created_at timestamptz not null default now(),
    primary key (source, post_id)
);

-- A durable reservation/outbox closes the select-then-send concurrency race.
-- sending/uncertain never retry automatically: Telegram has no idempotency key.
create table if not exists public.delivery_jobs (
    source text not null check (source in ('LH', 'SH')),
    post_id text not null,
    payload jsonb not null,
    status text not null check (status in
        ('preparing', 'failed', 'skipped', 'sending', 'uncertain', 'sent')),
    claim_token uuid not null,
    lease_until timestamptz not null,
    attempts integer not null default 1,
    summary jsonb,
    message text,
    last_error text,
    updated_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    primary key (source, post_id)
);
create index if not exists delivery_jobs_retry_idx
    on public.delivery_jobs (updated_at)
    where status in ('failed', 'preparing');

alter table public.announcements enable row level security;
alter table public.delivery_jobs enable row level security;
revoke all on public.announcements, public.delivery_jobs from anon, authenticated;
grant select, insert, update on public.announcements, public.delivery_jobs to service_role;

-- A 45-minute lease exceeds the 30-minute Actions timeout.
-- All functions use caller privileges; only the server role can execute them.
create or replace function public.claim_announcement(
    p_source text, p_post_id text, p_payload jsonb, p_token uuid
) returns boolean language plpgsql security invoker set search_path = '' as $$
declare claimed boolean := false;
begin
    if exists (select 1 from public.announcements
               where source = p_source and post_id = p_post_id) then
        return false;
    end if;
    insert into public.delivery_jobs as j
        (source, post_id, payload, status, claim_token, lease_until)
    values (p_source, p_post_id, p_payload, 'preparing', p_token, now() + interval '45 minutes')
    on conflict (source, post_id) do update set
        status = 'preparing', claim_token = p_token,
        lease_until = now() + interval '45 minutes',
        payload = p_payload, attempts = j.attempts + 1,
        updated_at = now(), last_error = null
    where j.status = 'failed'
       or (j.status = 'preparing' and j.lease_until < now())
    returning true into claimed;
    return coalesce(claimed, false);
end;
$$;

create or replace function public.begin_delivery(
    p_source text, p_post_id text, p_token uuid, p_summary jsonb, p_message text
) returns boolean language plpgsql security invoker set search_path = '' as $$
declare changed boolean := false;
begin
    update public.delivery_jobs set status = 'sending', summary = p_summary,
        message = p_message, updated_at = now()
    where source = p_source and post_id = p_post_id and claim_token = p_token
      and status = 'preparing' and lease_until > now()
    returning true into changed;
    return coalesce(changed, false);
end;
$$;

-- Called only AFTER Telegram returns ok=true and a message_id.
-- Announcement insertion and job completion are one database transaction.
create or replace function public.complete_delivery(
    p_source text, p_post_id text, p_token uuid, p_message_id bigint
) returns boolean language plpgsql security invoker set search_path = '' as $$
declare j public.delivery_jobs%rowtype;
begin
    select * into j from public.delivery_jobs
    where source = p_source and post_id = p_post_id and claim_token = p_token
    for update;
    if not found then return false; end if;
    if j.status = 'sent' then return true; end if;
    if j.status <> 'sending' then return false; end if;
    insert into public.announcements
        (source, post_id, title, url, published_at, summary, telegram_message_id)
    values (p_source, p_post_id, j.payload->>'title', j.payload->>'url',
        nullif(j.payload->>'published_at', '')::date, j.summary, p_message_id)
    on conflict (source, post_id) do nothing;
    update public.delivery_jobs set status = 'sent', updated_at = now()
    where source = p_source and post_id = p_post_id;
    return true;
end;
$$;

revoke all on function public.claim_announcement(text,text,jsonb,uuid) from public, anon, authenticated;
revoke all on function public.begin_delivery(text,text,uuid,jsonb,text) from public, anon, authenticated;
revoke all on function public.complete_delivery(text,text,uuid,bigint) from public, anon, authenticated;
grant execute on function public.claim_announcement(text,text,jsonb,uuid) to service_role;
grant execute on function public.begin_delivery(text,text,uuid,jsonb,text) to service_role;
grant execute on function public.complete_delivery(text,text,uuid,bigint) to service_role;

comment on table public.announcements is 'Successfully notified housing announcements; (source, post_id) deduplicates notifications.';
comment on table public.delivery_jobs is 'Durable delivery state. Check Telegram manually before retrying sending/uncertain rows.';
commit;
