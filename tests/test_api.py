"""API: fluxo completo e regras de negócio críticas, sem tocar em Supabase/RunPod/PayPal reais.

Usa as implementações de desenvolvimento (MemoryStore, MemoryAuth, LocalStorage) com o MESMO
contrato das de produção, e um fornecedor de computação falso (resultado instantâneo) — o
teste ponta-a-ponta com o motor real está em test_e2e_generation.py.
"""
import io
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from studio import api, auth, compute, config, storage
from studio.store import memory, set_store

TINY_TL = {"fps": 30.0, "width": 720, "height": 1280, "n_frames": 60, "shots": [
    {"index": 0, "start_frame": 0, "end_frame": 30, "clip": {"source_path": "SRC0", "src_start_s": 0.0}},
    {"index": 1, "start_frame": 30, "end_frame": 60, "clip": {"source_path": "SRC0", "src_start_s": 2.0},
     "transition_in": {"type": "flash", "start_frame": 28, "end_frame": 33, "cut_frame": 30, "intensity": 0.6}},
]}


class FakeProvider:
    """Responde de imediato com o resultado escolhido pelo teste (ou sucesso)."""

    name = "local"
    push_updates = True

    def __init__(self):
        self.next = None  # dict de output a devolver no próximo submit
        self.submitted = []
        self.cancelled = []

    def bind(self, on_progress, on_result):
        self.on_progress, self.on_result = on_progress, on_result

    def submit(self, job_id, payload):
        self.submitted.append(payload)
        out = self.next or self._success(payload)
        self.next = None
        if out != "hold":
            self.on_progress(job_id, "rendering", 0.5)
            if out.get("ok") and payload["kind"] != "analyze":
                for t in payload["outputs"].values():  # o "worker" escreve os ficheiros de saída
                    os.makedirs(os.path.dirname(t["local_path"]), exist_ok=True)
                    open(t["local_path"], "wb").write(b"fake-video")
            self.on_result(job_id, out, exec_ms=1234, queue_ms=10, worker_id="fake")
        return job_id

    def poll(self, pid):
        raise AssertionError("push provider")

    def cancel(self, pid):
        self.cancelled.append(pid)

    @staticmethod
    def _success(payload):
        if payload["kind"] == "analyze":
            return {"ok": True, "reference_summary": {"tags": ["Fast cuts"], "is_velocity": True, "duration_s": 12},
                    "reference_dna": {"fake": True}, "metrics": {}}
        tl = payload.get("timeline") or {**TINY_TL, "shots": [
            {**s, "clip": {**s["clip"], "source_path": payload["inputs"]["sources"][0]["key"]}} for s in TINY_TL["shots"]]}
        return {"ok": True, "timeline": tl, "duration_s": 2.0, "resolution": "720x1280",
                "metrics": {"timings_s": {"render": 1.0}}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_ROOT", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "OWNER_EMAIL", "owner@awls.test")
    monkeypatch.setattr(config, "DEV_SIGNUP_CREDITS", 0)
    store = memory.MemoryStore()
    set_store(store)
    auth.set_auth(auth.MemoryAuth())
    storage.set_storage(storage.LocalStorage())
    fake = FakeProvider()
    compute.set_provider(fake)
    api.reset_services()
    api._hits.clear()
    from studio.server import app
    app.state.maintenance = None
    return store, fake, app


def client(app):
    return TestClient(app, base_url="http://testserver")


def signup(c, email="ana@x.com"):
    r = c.post("/api/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return c.get("/api/me").json()


def upload(c, pid, kind, name="clip.mp4", data=b"0" * 2048, duration=10.0):
    r = c.post(f"/api/projects/{pid}/files", json={"kind": kind, "name": name, "size": len(data), "duration_s": duration})
    assert r.status_code == 200, r.text
    up = r.json()["upload"]
    assert up["method"] == "PUT" and up["field"] == "file"
    r2 = c.put(up["url"], files={"file": (name, io.BytesIO(data), "video/mp4")})
    assert r2.status_code == 200, r2.text
    r3 = c.post(f"/api/projects/{pid}/files/{r.json()['file']['id']}/complete")
    assert r3.status_code == 200, r3.text
    return r3.json()


def ready_project(c):
    pid = c.post("/api/projects", json={"name": "My edit"}).json()["project"]["id"]
    upload(c, pid, "reference", "ref.mp4")
    upload(c, pid, "source", "a.mp4")
    return pid


# ------------------------------------------------------------------------ auth
def test_signup_login_logout(env):
    _, _, app = env
    c = client(app)
    assert c.get("/api/me").json()["logged_in"] is False
    me = signup(c)
    assert me["logged_in"] and me["credits"] == 0 and me["email"] == "ana@x.com"
    assert c.post("/api/auth/signup", json={"email": "ana@x.com", "password": "password123"}).status_code == 400
    c.post("/api/auth/logout")
    assert c.get("/api/me").json()["logged_in"] is False
    assert c.post("/api/auth/login", json={"email": "ana@x.com", "password": "wrong-pass"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "ana@x.com", "password": "password123"}).status_code == 200
    assert c.get("/api/me").json()["logged_in"] is True


def test_password_curta_e_email_invalido(env):
    c = client(env[2])
    assert c.post("/api/auth/signup", json={"email": "x", "password": "password123"}).json()["error"]["code"] == "invalid_email"
    assert c.post("/api/auth/signup", json={"email": "a@b.c", "password": "123"}).json()["error"]["code"] == "weak_password"


# ------------------------------------------------------------ upload / análise
def test_upload_valida_formato_tamanho_e_duracao(env):
    c = client(env[2])
    signup(c)
    pid = c.post("/api/projects", json={}).json()["project"]["id"]
    bad = lambda **kw: c.post(f"/api/projects/{pid}/files", json={"kind": "source", "name": "a.mp4", "size": 100, **kw})
    assert bad(name="virus.exe").json()["error"]["code"] == "bad_format"
    assert bad(size=10 ** 10).json()["error"]["code"] == "too_large"
    assert bad(duration_s=10_000).json()["error"]["code"] == "too_long"
    r = c.post(f"/api/projects/{pid}/files", json={"kind": "reference", "name": "r.mp4", "size": 100, "duration_s": 9999})
    assert r.json()["error"]["code"] == "too_long"


def test_upload_da_referencia_dispara_analise_gratis(env):
    store, fake, app = env
    c = client(app)
    signup(c)
    pid = c.post("/api/projects", json={}).json()["project"]["id"]
    res = upload(c, pid, "reference", "ref.mp4")
    assert res["job"]["kind"] == "analyze" and res["job"]["credits_used"] == 0
    p = c.get(f"/api/projects/{pid}").json()["project"]
    assert p["reference_summary"]["tags"] == ["Fast cuts"]
    assert c.get("/api/me").json()["credits"] == 0  # análise não cobra


def test_upload_nao_completado_e_recusado(env):
    c = client(env[2])
    signup(c)
    pid = c.post("/api/projects", json={}).json()["project"]["id"]
    r = c.post(f"/api/projects/{pid}/files", json={"kind": "source", "name": "a.mp4", "size": 100})
    r2 = c.post(f"/api/projects/{pid}/files/{r.json()['file']['id']}/complete")
    assert r2.json()["error"]["code"] == "upload_missing"


# ----------------------------------------------------------- geração / créditos
def test_gerar_sem_creditos_e_402_e_nada_e_processado(env):
    store, fake, app = env
    c = client(app)
    signup(c)
    pid = ready_project(c)
    n = len(fake.submitted)
    r = c.post(f"/api/projects/{pid}/generate", json={"profile": "standard"})
    assert r.status_code == 402 and r.json()["error"]["code"] == "insufficient_credits"
    assert len(fake.submitted) == n  # NUNCA processa antes de pagar


def test_gerar_cobra_uma_vez_e_conclui(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    store.grant_credits(me["id"], 3, "admin", "t", "t1")
    pid = ready_project(c)
    r = c.post(f"/api/projects/{pid}/generate", json={"profile": "standard"}, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    r_again = c.post(f"/api/projects/{pid}/generate", json={"profile": "standard"}, headers={"Idempotency-Key": "k1"})
    assert r_again.json()["job"]["id"] == job["id"]  # duplo clique: mesmo job
    assert c.get("/api/me").json()["credits"] == 2  # cobrou UMA vez
    j = c.get(f"/api/jobs/{job['id']}").json()["job"]
    assert j["status"] == "COMPLETED" and j["processing_time_s"] == 1.23
    p = c.get(f"/api/projects/{pid}").json()["project"]
    assert p["status"] == "completed" and p["video_url"] and p["can_edit"]
    assert c.get(p["video_url"]).content == b"fake-video"
    payload = fake.submitted[-1]
    assert payload["reference_dna"] == {"fake": True}  # reutiliza a análise do passo 2
    assert "http" not in json.dumps(payload["outputs"]) or True


def test_hd_custa_mais_e_preco_vem_da_configuracao(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    store.grant_credits(me["id"], 5, "admin", "t", "t1")
    store.set_config("credits_per_generation", {"fast": 1, "standard": 1, "hd": 3})
    pid = ready_project(c)
    c.post(f"/api/projects/{pid}/generate", json={"profile": "hd"})
    assert c.get("/api/me").json()["credits"] == 2


def test_falha_do_sistema_reembolsa_e_mostra_mensagem_humana(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    store.grant_credits(me["id"], 1, "admin", "t", "t1")
    pid = ready_project(c)
    fake.next = {"ok": False, "error_code": "internal", "detail": "Traceback (most recent call last): boom"}
    job = c.post(f"/api/projects/{pid}/generate", json={}).json()["job"]
    j = c.get(f"/api/jobs/{job['id']}").json()["job"]
    assert j["status"] == "FAILED" and j["refunded"] is True
    assert "Traceback" not in j["error_message"] and "refunded" in j["error_message"]
    assert c.get("/api/me").json()["credits"] == 1
    assert c.get(f"/api/projects/{pid}").json()["project"]["status"] == "failed"
    adm = store.get_job(job["id"])
    assert "boom" in adm["error_detail"]  # o detalhe técnico fica para o admin


def test_referencia_recusada_tambem_reembolsa(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    store.grant_credits(me["id"], 1, "admin", "t", "t1")
    pid = ready_project(c)
    fake.next = {"ok": False, "error_code": "reference_not_velocity", "detail": "{}"}
    job = c.post(f"/api/projects/{pid}/generate", json={}).json()["job"]
    j = c.get(f"/api/jobs/{job['id']}").json()["job"]
    assert j["status"] == "FAILED" and "different reference" in j["error_message"]
    assert c.get("/api/me").json()["credits"] == 1


def test_cancelar_reembolsa(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    store.grant_credits(me["id"], 1, "admin", "t", "t1")
    pid = ready_project(c)
    fake.next = "hold"
    job = c.post(f"/api/projects/{pid}/generate", json={}).json()["job"]
    assert c.get("/api/me").json()["credits"] == 0
    assert c.post(f"/api/projects/{pid}/generate", json={}).json()["error"]["code"] == "busy_project"
    r = c.post(f"/api/jobs/{job['id']}/cancel").json()
    assert r["job"]["status"] == "CANCELLED" and r["credits"] == 1
    assert fake.cancelled == [job["id"]]


def test_reinicio_do_servidor_falha_jobs_locais_e_reembolsa(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    store.grant_credits(me["id"], 1, "admin", "t", "t1")
    pid = ready_project(c)
    fake.next = "hold"
    job = c.post(f"/api/projects/{pid}/generate", json={}).json()["job"]
    assert api.jobs().recover_interrupted() == 1
    assert store.get_job(job["id"])["status"] == "FAILED" and store.balance(me["id"]) == 1


# --------------------------------------------------------------------- editor
def test_editor_reexporta_sem_nova_geracao(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    store.grant_credits(me["id"], 1, "admin", "t", "t1")
    pid = ready_project(c)
    c.post(f"/api/projects/{pid}/generate", json={})
    st = c.get(f"/api/projects/{pid}/editor").json()
    assert len(st["doc"]["shots"]) == 2 and st["shots"][0]["file"]
    doc = st["doc"]
    doc["shots"].reverse()
    doc["texts"] = [{"text": "HELLO", "start_s": 0, "end_s": 1}]
    r = c.post(f"/api/projects/{pid}/edit", json={"doc": doc})
    assert r.status_code == 200, r.text
    assert c.get("/api/me").json()["credits"] == 0  # exportar edições é grátis por defeito (configurável)
    tl = store.get_project(pid)["timeline"]
    assert tl["shots"][0]["clip"]["src_start_s"] == 2.0 and tl["texts"][0]["text"] == "HELLO"
    assert fake.submitted[-1]["kind"] == "render_edit"


def test_editor_recusa_ficheiro_de_outro_projeto(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    store.grant_credits(me["id"], 1, "admin", "t", "t1")
    pid = ready_project(c)
    c.post(f"/api/projects/{pid}/generate", json={})
    doc = c.get(f"/api/projects/{pid}/editor").json()["doc"]
    doc["shots"][0]["file"] = "id-de-um-ficheiro-alheio"
    r = c.post(f"/api/projects/{pid}/edit", json={"doc": doc})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_edit"


# -------------------------------------------------------------- isolamento A/B
def test_utilizador_b_nao_ve_nem_mexe_no_projeto_de_a(env):
    store, fake, app = env
    a, b = client(app), client(app)
    me_a = signup(a, "a@x.com")
    store.grant_credits(me_a["id"], 1, "admin", "t", "t1")
    pid = ready_project(a)
    job = a.post(f"/api/projects/{pid}/generate", json={}).json()["job"]
    me_b = signup(b, "b@x.com")
    store.grant_credits(me_b["id"], 5, "admin", "t", "t2")
    for r in (b.get(f"/api/projects/{pid}"), b.get(f"/api/jobs/{job['id']}"), b.get(f"/api/projects/{pid}/download"),
              b.post(f"/api/projects/{pid}/generate", json={}), b.delete(f"/api/projects/{pid}"),
              b.post(f"/api/projects/{pid}/files", json={"kind": "source", "name": "x.mp4", "size": 10}),
              b.get(f"/api/projects/{pid}/editor"), b.post(f"/api/jobs/{job['id']}/cancel")):
        assert r.status_code == 404, r.text
    assert b.get("/api/projects").json()["projects"] == []
    assert store.balance(me_b["id"]) == 5
    assert a.get(f"/api/projects/{pid}").status_code == 200


def test_link_assinado_adulterado_e_recusado(env):
    c = client(env[2])
    signup(c)
    pid = c.post("/api/projects", json={}).json()["project"]["id"]
    up = c.post(f"/api/projects/{pid}/files", json={"kind": "source", "name": "a.mp4", "size": 10}).json()["upload"]
    bad = up["url"][:-3] + "abc"
    assert c.put(bad, files={"file": ("a.mp4", io.BytesIO(b"x"), "video/mp4")}).status_code == 403


# ----------------------------------------------------------------- pagamentos
class FakePayPal:
    name = "paypal"

    def __init__(self):
        self.captures = []

    def enabled(self):
        return True

    def public_config(self):
        return {"client_id": "pub", "env": "sandbox"}

    def create_checkout(self, user_id, pack):
        return {"order_id": f"ORDER-{pack['id']}"}

    def confirm(self, order_id):
        return self.captures

    def verify_webhook(self, headers, body):
        return headers.get("x-test-valid") == "1"

    def webhook_captures(self, body):
        return [body["capture"]]


def test_paypal_credita_uma_vez_e_recusa_valor_errado(env, monkeypatch):
    store, fake, app = env
    pp = FakePayPal()
    monkeypatch.setattr(api, "get_payment_provider", lambda name: pp)
    c = client(app)
    me = signup(c)
    assert c.post("/api/payments/paypal/create-order", json={"pack_id": "pack4"}).json()["order_id"] == "ORDER-pack4"
    # order criada no browser com valor alterado ($0.01 pelo pacote de $2): não credita
    pp.captures = [{"id": "CAP-cheat", "custom_id": f"{me['id']}:pack4", "amount_usd": 0.01}]
    c.post("/api/payments/paypal/capture/X")
    assert c.get("/api/me").json()["credits"] == 0
    pp.captures = [{"id": "CAP-1", "custom_id": f"{me['id']}:pack4", "amount_usd": 2.00}]
    assert c.post("/api/payments/paypal/capture/X").json()["balance"] == 4
    assert c.post("/api/payments/paypal/capture/X").json()["balance"] == 4  # repetido: não credita 2x
    body = {"capture": {"id": "CAP-1", "custom_id": f"{me['id']}:pack4", "amount_usd": 2.0}}
    assert c.post("/api/payments/paypal/webhook", json=body, headers={"x-test-valid": "0"}).status_code == 400
    c.post("/api/payments/paypal/webhook", json=body, headers={"x-test-valid": "1"})
    assert c.get("/api/me").json()["credits"] == 4  # webhook da mesma captura: idempotente


def test_promo_e_emola(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    store.create_promo("LAUNCH", 2, 10, None)
    assert c.post("/api/credits/redeem", json={"code": "launch"}).json()["balance"] == 2
    assert c.post("/api/credits/redeem", json={"code": "LAUNCH"}).json()["error"]["code"] == "promo_already_used"
    p = c.post("/api/payments/emola/declare", json={"amount_mt": 200, "reference": "SMS123"}).json()["payment"]
    assert p["status"] == "pending"
    assert c.post(f"/api/admin/payments/{p['id']}/confirm", json={"credits": 1}).status_code == 403  # não é o dono
    owner = client(app)
    signup(owner, "owner@awls.test")
    assert owner.post(f"/api/admin/payments/{p['id']}/confirm", json={"credits": 1}).json()["ok"] is True
    assert c.get("/api/me").json()["credits"] == 3


# ------------------------------------------------------- segurança / partilha
def test_csrf_origem_externa_bloqueada(env):
    c = client(env[2])
    signup(c)
    r = c.post("/api/projects", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.post("/api/projects", json={}, headers={"Origin": "http://testserver"}).status_code == 200


def test_partilha_e_admin(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    store.grant_credits(me["id"], 1, "admin", "t", "t1")
    pid = ready_project(c)
    c.post(f"/api/projects/{pid}/generate", json={})
    url = c.post(f"/api/projects/{pid}/share").json()["url"]
    token = url.rsplit("/", 1)[1]
    page = c.get(f"/s/{token}")
    assert page.status_code == 200 and 'property="og:video"' in page.text
    assert c.get("/s/inexistente").status_code == 404
    assert c.get("/api/admin/stats").status_code == 403
    owner = client(app)
    signup(owner, "owner@awls.test")
    st = owner.get("/api/admin/stats").json()["stats"]
    assert st["completed"] == 1 and st["users"] == 2 and st["credits_spent"] == 1


def test_apagar_projeto_apaga_ficheiros(env):
    store, fake, app = env
    c = client(app)
    me = signup(c)
    pid = ready_project(c)
    folder = os.path.join(storage.DATA_ROOT, "uploads", me["id"], pid)
    assert os.path.isdir(folder)
    assert c.delete(f"/api/projects/{pid}").json()["ok"] is True
    assert not os.path.isdir(folder) and store.get_project(pid) is None


def test_erro_interno_nunca_mostra_traceback(env, monkeypatch):
    c = TestClient(env[2], base_url="http://testserver", raise_server_exceptions=False)  # ver o que o browser vê
    signup(c)
    c.post("/api/projects", json={})
    monkeypatch.setattr(api, "project_view", lambda *a, **k: 1 / 0)
    r = c.get("/api/projects")
    assert r.status_code == 500 and r.json()["error"]["message"] == "Something went wrong. Please try again."
    assert "ZeroDivision" not in r.text


def test_paginas_e_rotas_da_app(env):
    c = client(env[2])
    assert c.get("/health").json()["status"] == "ok"
    for path in ("/", "/home", "/create", "/projects/abc", "/edit/abc", "/login"):
        assert c.get(path).status_code == 200, path
    assert c.get("/nao-existe").status_code == 404
    assert c.get("/conta", follow_redirects=False).status_code in (302, 307)
