// Testa supabase-schema.sql num Postgres REAL (PGlite = Postgres em WASM), sem tocar no
// Supabase de produção. Simula o que o Supabase fornece (esquemas auth/storage, papéis).
//
//   cd tests/sql && npm install && npm test
//
// Caminho A: base nova (migração corrida 2x — tem de ser idempotente) + semântica dos créditos.
// Caminho B: base com a versão 2 (a que está em produção) e dados -> atualização para a v3.
import { PGlite } from '@electric-sql/pglite';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const v3 = fs.readFileSync(path.join(here, '..', '..', 'supabase-schema.sql'), 'utf8');
const v2 = fs.readFileSync(path.join(here, 'fixtures', 'schema_v2.sql'), 'utf8');

const STUB = `
create role anon nologin; create role authenticated nologin; create role service_role nologin;
create schema auth; create schema storage;
create table auth.users (id uuid primary key, email text);
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
create table storage.buckets (id text primary key, name text, public boolean, file_size_limit bigint, allowed_mime_types text[]);
`;
const U1 = '11111111-1111-1111-1111-111111111111';
const U2 = '22222222-2222-2222-2222-222222222222';
let fails = 0;
const ok = (cond, msg) => { console.log((cond ? 'OK   ' : 'FAIL ') + msg); if (!cond) fails++; };
const one = async (db, q, p = []) => (await db.query(q, p)).rows[0];
const bal = async (db, u) => (await one(db, 'select paid_credits c from profiles where id=$1', [u])).c;

// ------------------------------------------------------------------------------ caminho A
{
  const db = new PGlite();
  await db.exec(STUB); await db.exec(v3); await db.exec(v3);
  ok(true, 'A: v3 numa base nova, corrida 2 vezes sem erro');
  await db.query("insert into auth.users values ($1,'a@x.com'),($2,'b@x.com')", [U1, U2]);
  ok((await one(db, 'select display_name d from profiles where id=$1', [U1])).d === 'a', 'A: trigger cria perfil com display_name');
  ok((await one(db, "select grant_credits($1,3,'admin','t','k1') g", [U1])).g === true, 'A: grant de 3 créditos');
  ok((await one(db, "select grant_credits($1,3,'admin','t','k1') g", [U1])).g === false, 'A: grant repetido (mesma chave) é ignorado');
  ok(await bal(db, U1) === 3, 'A: saldo = 3');
  const proj = (await one(db, "insert into projects(user_id,name) values ($1,'p') returning id", [U1])).id;
  await db.query("insert into agent_threads(project_id,user_id) values ($1,$2)", [proj, U1]);
  const th = await one(db, 'select status,messages,events,state from agent_threads where project_id=$1', [proj]);
  ok(th.status === 'idle' && Array.isArray(th.messages) && Array.isArray(th.events), 'A: conversa do agente criada com os valores por defeito');
  ok((await one(db, "select relrowsecurity r from pg_class where relname='agent_threads'")).r === true, 'A: conversas do agente com RLS ligado');
  ok(Array.isArray((await one(db, 'select agent_memory m from profiles where id=$1', [U1])).m), 'A: perfil tem memória do agente');
  ok((await one(db, "select value v from app_config where key='schema_version'")).v === 4, 'A: schema_version = 4');
  await db.query('delete from projects where id=$1', [proj]);
  ok((await one(db, 'select count(*)::int n from agent_threads where project_id=$1', [proj])).n === 0, 'A: apagar o projeto apaga a conversa');
  await db.query("insert into jobs(id,user_id,credits_used) values ('j1',$1,2),('j2',$1,5)", [U1]);
  ok((await one(db, "select spend_credits($1,5,'j2') s", [U1])).s === false, 'A: gastar 5 com saldo 3 -> recusado');
  ok((await one(db, "select spend_credits($1,2,'j1') s", [U1])).s === true, 'A: gastar 2 -> ok');
  ok((await one(db, "select spend_credits($1,2,'j1') s", [U1])).s === true, 'A: mesmo job outra vez -> ok, sem cobrar 2x');
  ok(await bal(db, U1) === 1, 'A: saldo = 1 (cobrou só uma vez)');
  ok((await one(db, "select refund_job('j1') r")).r === true, 'A: reembolso do job j1');
  ok((await one(db, "select refund_job('j1') r")).r === false, 'A: reembolso repetido -> ignorado');
  ok((await one(db, "select refund_job('j2') r")).r === false, 'A: reembolso de job que nunca cobrou -> ignorado');
  ok(await bal(db, U1) === 3, 'A: saldo volta a 3');
  ok((await one(db, "select record_purchase($1,'paypal','CAP1',2.00,'pack4',4) r", [U2])).r === true, 'A: compra PayPal credita');
  ok((await one(db, "select record_purchase($1,'paypal','CAP1',2.00,'pack4',4) r", [U2])).r === false, 'A: mesma captura (webhook repetido) não credita 2x');
  ok(await bal(db, U2) === 4, 'A: saldo U2 = 4');
  const pid = (await one(db, "insert into payments(user_id,amount_mt,reference) values ($1,200,'SMS1') returning id", [U2])).id;
  ok((await one(db, 'select confirm_manual_payment($1,1) r', [pid])).r === true, 'A: e-Mola confirmado pelo operador');
  ok((await one(db, 'select confirm_manual_payment($1,1) r', [pid])).r === false, 'A: e-Mola confirmado 2x -> ignorado');
  ok(await bal(db, U2) === 5, 'A: saldo U2 = 5');
  await db.query("insert into promo_codes(code,credits,max_uses) values ('LAUNCH',2,1)");
  ok((await one(db, "select redeem_promo($1,' launch ') r", [U1])).r.ok === true, 'A: código promo resgatado (maiúsculas/espaços)');
  ok((await one(db, "select redeem_promo($1,'LAUNCH') r", [U1])).r.error === 'already_used', 'A: mesmo utilizador 2x -> already_used');
  ok((await one(db, "select redeem_promo($1,'LAUNCH') r", [U2])).r.error === 'exhausted', 'A: max_uses atingido -> exhausted');
  ok((await one(db, "select redeem_promo($1,'NOPE') r", [U2])).r.error === 'invalid', 'A: código inexistente -> invalid');
  for (const u of [U1, U2]) {
    const led = await one(db, 'select coalesce(sum(delta),0)::int s from credit_transactions where user_id=$1', [u]);
    ok(led.s === await bal(db, u), `A: soma do ledger == saldo (${u.slice(0, 4)})`);
  }
  await db.query("update jobs set kind='generate', status='COMPLETED', completed_at=now(), processing_time_s=40, est_cost_usd=0.01 where id='j1'");
  await db.query("insert into events(name, anon_id) values ('page_view','v1'),('page_view','v1'),('page_view','v2')");
  const st = (await one(db, 'select admin_stats() s')).s;
  ok(st.users === 2 && st.completed === 1 && st.visitors === 2 && Number(st.revenue_usd) === 2,
     `A: admin_stats coerente (users=${st.users} completed=${st.completed} visitors=${st.visitors} revenue=${st.revenue_usd})`);
  const au = (await one(db, 'select admin_users(10) u')).u;
  ok(Array.isArray(au) && au.length === 2, 'A: admin_users devolve 2 utilizadores');
  for (const f of ['public.grant_credits(uuid, integer, text, text, text)', 'public.spend_credits(uuid, integer, text)',
                   'public.refund_job(text)', 'public.record_purchase(uuid, text, text, numeric, text, integer)',
                   'public.redeem_promo(uuid, text)', 'public.admin_stats()']) {
    const r = await one(db, "select has_function_privilege('anon',$1,'execute') a, has_function_privilege('authenticated',$1,'execute') b, has_function_privilege('service_role',$1,'execute') c", [f]);
    ok(!r.a && !r.b && r.c, `A: ${f.split('(')[0]} — só a service_role executa`);
  }
  const b = (await db.query('select id, public from storage.buckets order by id')).rows;
  ok(b.length === 3 && b.every((r) => r.public === false), 'A: 3 buckets privados criados');
}

// ------------------------------------------------------------------------------ caminho B
{
  const db = new PGlite();
  await db.exec(STUB); await db.exec(v2);
  await db.query("insert into auth.users values ($1,'old@x.com')", [U1]);
  await db.query('update profiles set paid_credits = 7 where id=$1', [U1]);
  await db.query("insert into jobs(id,user_id,status,profile) values ('old1',$1,'done','standard'),('old2',$1,'error','fast')", [U1]);
  await db.query("insert into payments(user_id,amount_mt,reference,status) values ($1,200,'X','confirmed')", [U1]);
  const holeBefore = (await one(db, "select has_function_privilege('anon','public.grant_paypal_credits(uuid, text, numeric, text, integer)','execute') a")).a;
  ok(holeBefore === true, 'B: confirmado que na v2 o papel anon PODIA executar grant_paypal_credits (buraco)');
  await db.exec(v3);
  ok(true, 'B: v3 aplicada por cima da v2 sem erro');
  ok(await bal(db, U1) === 7, 'B: saldo antigo preservado (7)');
  const js = (await db.query('select id, status, kind from jobs order by id')).rows;
  ok(js[0].status === 'COMPLETED' && js[1].status === 'FAILED' && js[0].kind === 'generate', 'B: estados antigos migrados (done->COMPLETED, error->FAILED)');
  ok((await one(db, 'select count(*)::int n from payments')).n === 1, 'B: pagamentos antigos preservados');
  const holeAfter = (await one(db, "select has_function_privilege('anon','public.grant_paypal_credits(uuid, text, numeric, text, integer)','execute') a")).a;
  ok(holeAfter === false, 'B: buraco fechado — anon já não executa grant_paypal_credits');
  await db.exec(v3);
  ok(true, 'B: v3 corrida outra vez por cima (idempotente)');
}

console.log(fails ? `\n${fails} FALHA(S)` : '\nTODOS OS TESTES SQL PASSARAM');
process.exit(fails ? 1 : 0);
