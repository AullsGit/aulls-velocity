-- AwlsVelocity — esquema completo da base de dados (Supabase / Postgres), versão 3.
--
-- COMO USAR: Supabase → SQL Editor → New query → colar TODO este ficheiro → Run.
-- É idempotente: pode correr numa base nova OU por cima das versões anteriores (v1/v2) sem
-- perder dados — só acrescenta tabelas/colunas e substitui funções.
--
-- Regras de segurança:
--  * O backend usa a service_role key. O browser NUNCA fala diretamente com esta base de dados.
--  * Todas as funções que mexem em créditos são `security definer` e ficam EXECUTÁVEIS SÓ pela
--    service_role (ver o bloco "revoke" no fim) — por defeito o Postgres deixa qualquer papel,
--    incluindo `anon`, chamar funções do esquema public via /rest/v1/rpc.
--  * RLS ligado em todas as tabelas: cada utilizador só lê as suas próprias linhas.

-- ============================================================================ perfis
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  free_edits_used integer not null default 0,      -- legado (v1); já não é lido
  paid_credits integer not null default 0,         -- SALDO de créditos (fonte: credit_transactions)
  created_at timestamptz not null default now()
);
alter table public.profiles add column if not exists display_name text;
alter table public.profiles add column if not exists plan text not null default 'pay_per_use';
alter table public.profiles add column if not exists onboarded boolean not null default false;

create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, email, display_name)
    values (new.id, new.email, split_part(coalesce(new.email, ''), '@', 1))
  on conflict (id) do nothing;
  return new;
end;
$$ language plpgsql security definer set search_path = public;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

-- ========================================================================== projetos
create table if not exists public.projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null default 'Untitled',
  status text not null default 'draft',            -- draft | processing | completed | failed
  files jsonb not null default '[]'::jsonb,        -- [{id, kind: source|reference|music, path, name, size, duration_s, ...}]
  settings jsonb not null default '{}'::jsonb,
  reference_summary jsonb,                         -- estilo identificado (mostrado no passo 2)
  reference_dna jsonb,                             -- análise completa (reutilizada na geração)
  timeline jsonb,                                  -- timeline editável da última geração
  output_path text,
  thumbnail_path text,
  duration_s numeric,
  resolution text,
  share_token text unique,
  is_public boolean not null default false,
  last_job_id text,
  sources_purged boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists projects_user_idx on public.projects (user_id, updated_at desc);

-- ============================================================================== jobs
create table if not exists public.jobs (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'QUEUED',
  profile text,
  is_public boolean not null default false,        -- legado (v1)
  video_url text,                                  -- legado (v1)
  report jsonb,
  error text,                                      -- legado (v1)
  created_at timestamptz not null default now()
);
alter table public.jobs alter column status set default 'QUEUED';
alter table public.jobs add column if not exists project_id uuid references public.projects(id) on delete set null;
alter table public.jobs add column if not exists kind text not null default 'generate';   -- generate | analyze | render_edit
alter table public.jobs add column if not exists stage text;
alter table public.jobs add column if not exists progress real not null default 0;
alter table public.jobs add column if not exists provider text;                           -- local | runpod
alter table public.jobs add column if not exists provider_job_id text;
alter table public.jobs add column if not exists worker_id text;
alter table public.jobs add column if not exists worker_type text;
alter table public.jobs add column if not exists credits_used integer not null default 0;
alter table public.jobs add column if not exists refunded boolean not null default false;
alter table public.jobs add column if not exists started_at timestamptz;
alter table public.jobs add column if not exists completed_at timestamptz;
alter table public.jobs add column if not exists failed_at timestamptz;
alter table public.jobs add column if not exists cancelled_at timestamptz;
alter table public.jobs add column if not exists queue_time_s real;
alter table public.jobs add column if not exists processing_time_s real;
alter table public.jobs add column if not exists gpu_time_s real;
alter table public.jobs add column if not exists est_cost_usd numeric;
alter table public.jobs add column if not exists video_duration_s real;
alter table public.jobs add column if not exists resolution text;
alter table public.jobs add column if not exists error_message text;                      -- para o utilizador
alter table public.jobs add column if not exists error_detail text;                       -- só para o admin
alter table public.jobs add column if not exists idempotency_key text;
alter table public.jobs add column if not exists output_path text;
alter table public.jobs add column if not exists metrics jsonb not null default '{}'::jsonb;
create index if not exists jobs_user_idx on public.jobs (user_id, created_at desc);
create index if not exists jobs_active_idx on public.jobs (status) where status in ('QUEUED', 'PROCESSING');
create unique index if not exists jobs_idem_uniq on public.jobs (user_id, idempotency_key) where idempotency_key is not null;
-- estados da v1/v2 -> estados novos
update public.jobs set status = 'QUEUED' where status = 'queued';
update public.jobs set status = 'PROCESSING' where status = 'running';
update public.jobs set status = 'COMPLETED' where status = 'done';
update public.jobs set status = 'FAILED' where status in ('error', 'rejected');

-- ================================================================ ledger de créditos
create table if not exists public.credit_transactions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  delta integer not null,
  balance_after integer not null,
  reason text not null,              -- generation | refund | purchase | manual_payment | promo | admin
  ref_id text,
  idempotency_key text not null unique,
  created_at timestamptz not null default now()
);
create index if not exists credit_tx_user_idx on public.credit_transactions (user_id, created_at desc);

-- ====================================================================== pagamentos
create table if not exists public.payments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  amount_mt numeric,
  reference text,
  status text not null default 'pending',          -- pending | confirmed | rejected
  created_at timestamptz not null default now(),
  confirmed_at timestamptz
);
alter table public.payments alter column amount_mt drop not null;
alter table public.payments add column if not exists provider text not null default 'emola';
alter table public.payments add column if not exists provider_payment_id text;
alter table public.payments add column if not exists currency text not null default 'MT';
alter table public.payments add column if not exists amount_usd numeric;
alter table public.payments add column if not exists pack_id text;
alter table public.payments add column if not exists credits integer not null default 0;
create unique index if not exists payments_provider_payment_id_uniq
  on public.payments (provider, provider_payment_id) where provider_payment_id is not null;

-- ============================================================ preço, pacotes, promo
create table if not exists public.app_config (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.credit_packs (
  id text primary key,
  credits integer not null check (credits > 0),
  price_cents integer not null check (price_cents > 0),
  currency text not null default 'USD',
  label text not null,
  active boolean not null default true,
  sort integer not null default 0
);
insert into public.credit_packs (id, credits, price_cents, currency, label, active, sort) values
  ('pack4', 4, 200, 'USD', '4 generations', true, 1),
  ('pack10', 10, 500, 'USD', '10 generations', true, 2)
on conflict (id) do nothing;

create table if not exists public.promo_codes (
  code text primary key,
  credits integer not null check (credits > 0),
  max_uses integer not null default 1,
  uses integer not null default 0,
  expires_at timestamptz,
  active boolean not null default true,
  created_at timestamptz not null default now()
);
create table if not exists public.promo_redemptions (
  user_id uuid not null references auth.users(id) on delete cascade,
  code text not null references public.promo_codes(code) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, code)
);

-- ======================================================================= analytics
create table if not exists public.events (
  id bigserial primary key,
  name text not null,
  user_id uuid,
  anon_id text,
  props jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists events_name_idx on public.events (name, created_at desc);

-- ======================================================== funções atómicas de créditos
-- Gastar créditos ao CRIAR um job (antes de qualquer processamento). Idempotente por job:
-- repetir o mesmo pedido não cobra duas vezes.
create or replace function public.spend_credits(p_user uuid, p_amount integer, p_job text)
returns boolean as $$
declare
  v_bal integer;
  v_key text := 'spend:' || p_job;
begin
  if exists (select 1 from public.credit_transactions where idempotency_key = v_key) then
    return true;
  end if;
  if p_amount <= 0 then
    return true;
  end if;
  select paid_credits into v_bal from public.profiles where id = p_user for update;
  if v_bal is null or v_bal < p_amount then
    return false;
  end if;
  update public.profiles set paid_credits = paid_credits - p_amount where id = p_user
    returning paid_credits into v_bal;
  insert into public.credit_transactions (user_id, delta, balance_after, reason, ref_id, idempotency_key)
    values (p_user, -p_amount, v_bal, 'generation', p_job, v_key);
  return true;
end;
$$ language plpgsql security definer set search_path = public;

-- Devolver os créditos de um job que não entregou vídeo. Idempotente (marca jobs.refunded).
create or replace function public.refund_job(p_job text)
returns boolean as $$
declare
  v_user uuid;
  v_amount integer;
  v_bal integer;
begin
  select user_id, credits_used into v_user, v_amount
    from public.jobs where id = p_job and not refunded for update;
  if v_user is null or coalesce(v_amount, 0) <= 0 then
    return false;
  end if;
  if not exists (select 1 from public.credit_transactions where idempotency_key = 'spend:' || p_job) then
    return false;
  end if;
  update public.profiles set paid_credits = paid_credits + v_amount where id = v_user
    returning paid_credits into v_bal;
  insert into public.credit_transactions (user_id, delta, balance_after, reason, ref_id, idempotency_key)
    values (v_user, v_amount, v_bal, 'refund', p_job, 'refund:' || p_job);
  update public.jobs set refunded = true where id = p_job;
  return true;
end;
$$ language plpgsql security definer set search_path = public;

create or replace function public.grant_credits(p_user uuid, p_amount integer, p_reason text, p_ref text, p_key text)
returns boolean as $$
declare
  v_bal integer;
begin
  if exists (select 1 from public.credit_transactions where idempotency_key = p_key) then
    return false;
  end if;
  insert into public.profiles (id) values (p_user) on conflict (id) do nothing;
  update public.profiles set paid_credits = paid_credits + p_amount where id = p_user
    returning paid_credits into v_bal;
  insert into public.credit_transactions (user_id, delta, balance_after, reason, ref_id, idempotency_key)
    values (p_user, p_amount, v_bal, p_reason, p_ref, p_key);
  return true;
end;
$$ language plpgsql security definer set search_path = public;

-- Compra confirmada por um fornecedor (PayPal...). Idempotente pelo id do pagamento no
-- fornecedor: webhook + confirmação síncrona do mesmo pagamento creditam UMA vez.
create or replace function public.record_purchase(p_user uuid, p_provider text, p_ppid text,
                                                  p_amount_usd numeric, p_pack text, p_credits integer)
returns boolean as $$
declare
  v_id uuid;
begin
  insert into public.payments (user_id, provider, provider_payment_id, currency, amount_usd, pack_id,
                               credits, status, confirmed_at)
    values (p_user, p_provider, p_ppid, 'USD', p_amount_usd, p_pack, p_credits, 'confirmed', now())
    on conflict (provider, provider_payment_id) where provider_payment_id is not null do nothing
    returning id into v_id;
  if v_id is null then
    return false;
  end if;
  perform public.grant_credits(p_user, p_credits, 'purchase', v_id::text,
                               'purchase:' || p_provider || ':' || p_ppid);
  return true;
end;
$$ language plpgsql security definer set search_path = public;

-- e-Mola: o operador confirma em /admin depois de ver o dinheiro na conta.
create or replace function public.confirm_manual_payment(p_payment uuid, p_credits integer)
returns boolean as $$
declare
  v_user uuid;
begin
  if p_credits <= 0 then
    return false;
  end if;
  update public.payments set status = 'confirmed', confirmed_at = now(), credits = p_credits
    where id = p_payment and status = 'pending' and provider = 'emola'
    returning user_id into v_user;
  if v_user is null then
    return false;
  end if;
  perform public.grant_credits(v_user, p_credits, 'manual_payment', p_payment::text, 'manual:' || p_payment::text);
  return true;
end;
$$ language plpgsql security definer set search_path = public;

create or replace function public.redeem_promo(p_user uuid, p_code text)
returns jsonb as $$
declare
  c public.promo_codes%rowtype;
begin
  select * into c from public.promo_codes where code = upper(trim(p_code)) for update;
  if not found or not c.active then
    return jsonb_build_object('ok', false, 'error', 'invalid');
  end if;
  if c.expires_at is not null and c.expires_at < now() then
    return jsonb_build_object('ok', false, 'error', 'expired');
  end if;
  if exists (select 1 from public.promo_redemptions where user_id = p_user and code = c.code) then
    return jsonb_build_object('ok', false, 'error', 'already_used');
  end if;
  if c.uses >= c.max_uses then
    return jsonb_build_object('ok', false, 'error', 'exhausted');
  end if;
  begin
    insert into public.promo_redemptions (user_id, code) values (p_user, c.code);
  exception when unique_violation then
    return jsonb_build_object('ok', false, 'error', 'already_used');
  end;
  update public.promo_codes set uses = uses + 1 where code = c.code;
  perform public.grant_credits(p_user, c.credits, 'promo', c.code, 'promo:' || c.code || ':' || p_user::text);
  return jsonb_build_object('ok', true, 'credits', c.credits);
end;
$$ language plpgsql security definer set search_path = public;

-- ============================================================== métricas (painel admin)
create or replace function public.admin_stats()
returns jsonb as $$
  with gens as (select * from public.jobs where kind = 'generate'),
       done as (select * from gens where status = 'COMPLETED'),
       pays as (select * from public.payments where status = 'confirmed'),
       first_done as (select user_id, min(completed_at) as t from done group by user_id),
       n as (select (select count(*) from public.profiles) as users,
                    (select count(*) from done) as done_count,
                    (select count(distinct user_id) from done) as creators,
                    (select coalesce(sum(est_cost_usd), 0) from public.jobs) as cost,
                    (select count(est_cost_usd) from public.jobs) as cost_n,
                    (select coalesce(sum(amount_usd), 0) from pays where currency = 'USD') as rev_usd)
  select jsonb_build_object(
    'users', n.users,
    'visitors', (select count(distinct anon_id) from public.events where name = 'page_view' and anon_id is not null),
    'generations', (select count(*) from gens),
    'completed', n.done_count,
    'failed', (select count(*) from gens where status = 'FAILED'),
    'queued', (select count(*) from public.jobs where status = 'QUEUED'),
    'processing', (select count(*) from public.jobs where status = 'PROCESSING'),
    'avg_processing_s', (select round(avg(processing_time_s)::numeric, 1) from done),
    'avg_queue_s', (select round(avg(queue_time_s)::numeric, 1) from done),
    'compute_cost_usd', round(n.cost::numeric, 4),
    'avg_cost_per_video_usd', case when n.cost_n > 0 and n.done_count > 0 then round((n.cost / n.done_count)::numeric, 4) end,
    'revenue_usd', round(n.rev_usd::numeric, 2),
    'revenue_mt', (select round(coalesce(sum(amount_mt), 0)::numeric, 2) from pays where currency = 'MT'),
    'est_profit_usd', round((n.rev_usd - n.cost)::numeric, 2),
    'credits_spent', (select coalesce(sum(-delta), 0) from public.credit_transactions where reason = 'generation'),
    'credits_refunded', (select coalesce(sum(delta), 0) from public.credit_transactions where reason = 'refund'),
    'users_with_video', n.creators,
    'paying_users', (select count(distinct user_id) from pays),
    'conversion_to_paid', case when n.users > 0 then round(((select count(distinct user_id) from pays)::numeric / n.users), 3) end,
    'videos_per_user', case when n.creators > 0 then round((n.done_count::numeric / n.creators), 2) end,
    'avg_time_to_first_video_s', (select round(avg(extract(epoch from f.t - p.created_at))::numeric, 1)
                                  from first_done f join public.profiles p on p.id = f.user_id),
    'returning_users', (select count(*) from (select user_id from gens group by user_id
                                              having count(distinct (created_at at time zone 'utc')::date) >= 2) r),
    'revenue_per_user_usd', case when n.users > 0 then round((n.rev_usd / n.users)::numeric, 3) end
  ) from n;
$$ language sql stable security definer set search_path = public;

create or replace function public.admin_users(p_limit integer default 100)
returns jsonb as $$
  select coalesce(jsonb_agg(to_jsonb(u) order by u.created_at desc), '[]'::jsonb) from (
    select p.id, p.email, p.display_name, p.paid_credits, p.created_at,
           (select count(*) from public.jobs j where j.user_id = p.id and j.kind = 'generate') as generations,
           (select count(*) from public.jobs j where j.user_id = p.id and j.kind = 'generate' and j.status = 'COMPLETED') as completed,
           (select coalesce(sum(-t.delta), 0) from public.credit_transactions t where t.user_id = p.id and t.reason = 'generation') as credits_spent
      from public.profiles p order by p.created_at desc limit p_limit
  ) u;
$$ language sql stable security definer set search_path = public;

-- ================================================================================ RLS
alter table public.profiles enable row level security;
alter table public.projects enable row level security;
alter table public.jobs enable row level security;
alter table public.payments enable row level security;
alter table public.credit_transactions enable row level security;
alter table public.app_config enable row level security;
alter table public.credit_packs enable row level security;
alter table public.promo_codes enable row level security;
alter table public.promo_redemptions enable row level security;
alter table public.events enable row level security;

drop policy if exists "profiles: dono lê" on public.profiles;
create policy "profiles: dono lê" on public.profiles for select using (auth.uid() = id);
drop policy if exists "projects: dono lê" on public.projects;
create policy "projects: dono lê" on public.projects for select using (auth.uid() = user_id);
drop policy if exists "jobs: dono lê" on public.jobs;
create policy "jobs: dono lê" on public.jobs for select using (auth.uid() = user_id);
drop policy if exists "jobs: público lê publicados" on public.jobs;  -- v1: deixou de fazer sentido
drop policy if exists "payments: dono lê" on public.payments;
create policy "payments: dono lê" on public.payments for select using (auth.uid() = user_id);
drop policy if exists "credit_tx: dono lê" on public.credit_transactions;
create policy "credit_tx: dono lê" on public.credit_transactions for select using (auth.uid() = user_id);
drop policy if exists "packs: todos leem ativos" on public.credit_packs;
create policy "packs: todos leem ativos" on public.credit_packs for select using (active);
-- app_config, promo_codes, promo_redemptions, events: sem políticas = só a service_role acede.

-- ===================================================== funções: só a service_role executa
do $$
declare
  f text;
begin
  foreach f in array array[
    'public.spend_credits(uuid, integer, text)',
    'public.refund_job(text)',
    'public.grant_credits(uuid, integer, text, text, text)',
    'public.record_purchase(uuid, text, text, numeric, text, integer)',
    'public.confirm_manual_payment(uuid, integer)',
    'public.redeem_promo(uuid, text)',
    'public.admin_stats()',
    'public.admin_users(integer)'
  ] loop
    execute format('revoke execute on function %s from public, anon, authenticated', f);
    execute format('grant execute on function %s to service_role', f);
  end loop;
  -- Funções das versões 1/2 (ainda existem em bases antigas): fechar também.
  foreach f in array array[
    'public.consume_edit(uuid)',
    'public.can_edit(uuid)',
    'public.confirm_payment(uuid)',
    'public.grant_paypal_credits(uuid, text, numeric, text, integer)'
  ] loop
    if to_regprocedure(f) is not null then
      execute format('revoke execute on function %s from public, anon, authenticated', f);
      execute format('grant execute on function %s to service_role', f);
    end if;
  end loop;
end $$;

-- ============================================================================ storage
-- Buckets PRIVADOS: o browser só recebe URLs assinadas e temporárias, emitidas pelo backend
-- depois de verificar que o ficheiro pertence ao utilizador. 50 MB = limite do plano Free.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types) values
  ('uploads', 'uploads', false, 52428800, array['video/*', 'audio/*', 'image/*']),
  ('outputs', 'outputs', false, 524288000, array['video/mp4', 'application/json']),
  ('thumbs', 'thumbs', false, 5242880, array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do update set public = excluded.public,
  file_size_limit = excluded.file_size_limit, allowed_mime_types = excluded.allowed_mime_types;

insert into public.app_config (key, value) values ('schema_version', '3'::jsonb)
on conflict (key) do update set value = excluded.value, updated_at = now();
