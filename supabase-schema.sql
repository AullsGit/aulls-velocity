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
  amount_mt numeric not null,
  reference text,
  status text not null default 'pending', -- pending | confirmed | rejected
  created_at timestamptz not null default now(),
  confirmed_at timestamptz
);

-- Regista o consumo de uma edição: usa a grátis primeiro, depois um crédito pago.
-- Função (não lógica em Python) para não haver corrida entre pedidos simultâneos.
create or replace function public.consume_edit(p_user_id uuid)
returns void as $$
declare
  v_free int;
begin
  select free_edits_used into v_free from public.profiles where id = p_user_id for update;
  if v_free < 1 then
    update public.profiles set free_edits_used = free_edits_used + 1 where id = p_user_id;
  else
    update public.profiles set paid_credits = greatest(paid_credits - 1, 0) where id = p_user_id;
  end if;
end;
$$ language plpgsql security definer set search_path = public;

create or replace function public.can_edit(p_user_id uuid)
returns boolean as $$
declare
  v_free int; v_credits int;
begin
  select free_edits_used, paid_credits into v_free, v_credits
    from public.profiles where id = p_user_id;
  return coalesce(v_free, 0) < 1 or coalesce(v_credits, 0) > 0;
end;
$$ language plpgsql stable security definer set search_path = public;

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
