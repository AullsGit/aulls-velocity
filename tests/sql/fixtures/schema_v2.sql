-- Aulls Velocity — esquema da base de dados (Supabase / Postgres).
-- Corre isto UMA VEZ no SQL Editor do teu projeto Supabase (dashboard → SQL Editor → New query).
--
-- Contas de utilizador ficam em auth.users (gerido pelo próprio Supabase Auth — não criamos
-- essa tabela). As tabelas abaixo guardam só o que é específico do produto: perfil/créditos,
-- histórico de gerações, e declarações de pagamento.

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  free_edits_used integer not null default 0,
  paid_credits integer not null default 0,
  created_at timestamptz not null default now()
);

-- Cria automaticamente uma linha em profiles sempre que alguém se regista.
create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, email) values (new.id, new.email)
  on conflict (id) do nothing;
  return new;
end;
$$ language plpgsql security definer set search_path = public;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

create table if not exists public.jobs (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'queued',
  profile text,
  is_public boolean not null default false,
  video_url text,
  report jsonb,
  error text,
  created_at timestamptz not null default now()
);

create table if not exists public.payments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  amount_mt numeric,               -- fluxo antigo (e-Mola, Moçambique) — mantido, ver "provider"
  reference text,
  status text not null default 'pending', -- pending | confirmed | rejected
  created_at timestamptz not null default now(),
  confirmed_at timestamptz
);

-- Migração idempotente (segura de correr outra vez mesmo que a tabela já exista de uma
-- versão anterior deste ficheiro): acrescenta as colunas do fluxo PayPal sem tocar nos
-- dados já lá guardados. "provider" distingue os dois fluxos que coexistem: 'emola'
-- (declaração manual, amount_mt+reference) e 'paypal' (automático, confirmado por
-- webhook — nunca por o cliente simplesmente dizer "paguei").
alter table public.payments alter column amount_mt drop not null;
alter table public.payments add column if not exists provider text not null default 'emola';
alter table public.payments add column if not exists provider_payment_id text;
alter table public.payments add column if not exists currency text not null default 'MT';
alter table public.payments add column if not exists amount_usd numeric;
alter table public.payments add column if not exists pack_id text;
alter table public.payments add column if not exists credits integer not null default 1;

-- Nunca processar a mesma captura do PayPal duas vezes (o PayPal pode reenviar o mesmo
-- webhook mais que uma vez — isto é o que impede creditar 2x pela mesma compra).
create unique index if not exists payments_provider_payment_id_uniq
  on public.payments (provider, provider_payment_id) where provider_payment_id is not null;

-- SEM plano grátis: só há edição com créditos pagos (PayPal ou e-Mola confirmado).
-- "free_edits_used" fica na tabela só por compatibilidade com dados antigos; deixou de ser lido.
create or replace function public.consume_edit(p_user_id uuid)
returns void as $$
begin
  update public.profiles set paid_credits = greatest(paid_credits - 1, 0) where id = p_user_id;
end;
$$ language plpgsql security definer set search_path = public;

create or replace function public.can_edit(p_user_id uuid)
returns boolean as $$
declare
  v_credits int;
begin
  select paid_credits into v_credits from public.profiles where id = p_user_id;
  return coalesce(v_credits, 0) > 0;
end;
$$ language plpgsql stable security definer set search_path = public;

-- Concede créditos depois de um pagamento PayPal confirmado pelo webhook (nunca pelo
-- frontend). Idempotente: se "provider_payment_id" já existir, não faz nada e devolve
-- false — protege contra o PayPal reenviar o mesmo evento (replay).
create or replace function public.grant_paypal_credits(
  p_user_id uuid, p_payment_id text, p_amount_usd numeric, p_pack_id text, p_credits int
) returns boolean as $$
declare
  v_inserted uuid;
begin
  insert into public.payments (user_id, provider, provider_payment_id, currency, amount_usd,
                                pack_id, credits, status, confirmed_at)
    values (p_user_id, 'paypal', p_payment_id, 'USD', p_amount_usd, p_pack_id, p_credits,
            'confirmed', now())
    on conflict (provider, provider_payment_id) do nothing
    returning id into v_inserted;
  if v_inserted is not null then
    update public.profiles set paid_credits = paid_credits + p_credits where id = p_user_id;
    return true;
  end if;
  return false; -- já tinha sido processado antes (replay do webhook)
end;
$$ language plpgsql security definer set search_path = public;

-- Confirmação de pagamento (só o operador chama isto, a partir do painel /admin).
-- Atómico: marca o pagamento como confirmado e dá 1 crédito, ou não faz nada se já
-- não estiver "pending" (evita confirmar o mesmo pagamento duas vezes).
create or replace function public.confirm_payment(p_payment_id uuid)
returns void as $$
declare
  v_user_id uuid;
begin
  update public.payments set status = 'confirmed', confirmed_at = now()
    where id = p_payment_id and status = 'pending'
    returning user_id into v_user_id;
  if v_user_id is not null then
    update public.profiles set paid_credits = paid_credits + 1 where id = v_user_id;
  end if;
end;
$$ language plpgsql security definer set search_path = public;

-- RLS: o backend usa a service_role key (que ignora RLS) para tudo — estas políticas
-- são só uma rede de segurança extra, caso algum dia o browser fale direto com o Supabase.
alter table public.profiles enable row level security;
alter table public.jobs enable row level security;
alter table public.payments enable row level security;

drop policy if exists "profiles: dono lê" on public.profiles;
create policy "profiles: dono lê" on public.profiles for select using (auth.uid() = id);

drop policy if exists "jobs: dono lê" on public.jobs;
create policy "jobs: dono lê" on public.jobs for select using (auth.uid() = user_id);
drop policy if exists "jobs: público lê publicados" on public.jobs;
create policy "jobs: público lê publicados" on public.jobs for select using (is_public = true);

drop policy if exists "payments: dono lê" on public.payments;
create policy "payments: dono lê" on public.payments for select using (auth.uid() = user_id);

-- Storage: bucket público para os vídeos gerados (assim sobrevivem a reinícios do
-- servidor, que no plano grátis do Render tem disco não-persistente).
insert into storage.buckets (id, name, public)
  values ('videos', 'videos', true)
  on conflict (id) do nothing;
