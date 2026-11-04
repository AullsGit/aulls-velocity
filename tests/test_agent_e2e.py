"""O agente de ponta a ponta: API real, ferramentas reais, worker real, motor real.

O único substituto é o modelo: um guião determinista no lugar da API da Anthropic (`ScriptedModel`).
Prova que as ferramentas fazem o trabalho e que o ciclo se comporta. NÃO prova a qualidade das
decisões do modelo verdadeiro; isso testa-se com ANTHROPIC_API_KEY (bench/agent_live.py).
"""
import io
import json
import os
import subprocess
import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from studio import agent_api, api, auth, compute, config, storage
from studio.agent import loop as agent_loop
from studio.compute.local import LocalProvider
from studio.store import memory, set_store

pytestmark = pytest.mark.e2e


# ------------------------------------------------------------------------ material
def _ff(args):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, timeout=180)
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-500:]


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    srcs = ["testsrc2", "mandelbrot", "rgbtestsrc", "smptebars", "testsrc", "testsrc2"]
    inputs, chains = [], []
    for i, s in enumerate(srcs):
        inputs += ["-f", "lavfi", "-t", "1", "-i", f"{s}=size=360x640:rate=30"]
        chains.append(f"[{i}:v]scale=360:640,setsar=1,fps=30,format=yuv420p[v{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(len(srcs))) + f"concat=n={len(srcs)}:v=1:a=0[v]"
    ref = str(d / "ref.mp4")
    _ff([*inputs, "-f", "lavfi", "-t", "6", "-i", "aevalsrc='0.6*sin(2*PI*220*t)*exp(-12*mod(t,0.5))':s=44100", "-filter_complex", graph, "-map", "[v]", "-map",
         f"{len(srcs)}:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-shortest", ref])
    clips = []
    for i, (s, tone) in enumerate((("life=size=360x640:rate=30:mold=10:ratio=0.3", 500), ("cellauto=size=360x640:rate=30:rule=110", 1500))):
        p = str(d / f"clip{i}.mp4")
        _ff(["-f", "lavfi", "-t", "8", "-i", s, "-f", "lavfi", "-t", "8", "-i", f"sine=frequency={tone}", "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-c:a", "aac", "-shortest", p])
        clips.append(p)
    return {"ref": ref, "clips": clips}


# ------------------------------------------------------------------ o modelo falso
class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for b in self.msg.content:
            if b.type == "text":
                yield NS(type="content_block_delta", delta=NS(type="text_delta", text=b.text))
            elif b.type == "thinking" and b.thinking:
                yield NS(type="content_block_delta", delta=NS(type="thinking_delta", thinking=b.thinking))

    def get_final_message(self):
        if isinstance(self.msg, Exception):
            raise self.msg
        return self.msg


class Block(NS):
    def model_dump(self, mode="json", exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


def text(t):
    return Block(type="text", text=t)


def tool(name, tid=None, **inp):
    tool.n += 1
    return Block(type="tool_use", id=tid or f"toolu_{tool.n}", name=name, input=inp)


tool.n = 0


def reply(*blocks, stop=None):
    has_tool = any(b.type == "tool_use" for b in blocks)
    return NS(content=list(blocks), stop_reason=stop or ("tool_use" if has_tool else "end_turn"),
              usage=NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=900, cache_creation_input_tokens=0))


class ScriptedModel:
    """`script`: lista de respostas, ou funções que recebem as mensagens e devolvem uma resposta."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.uploaded = []
        self.beta = NS(messages=NS(stream=self._stream))
        self.files = NS(upload=self._upload, delete=lambda fid: None)

    def _stream(self, **params):
        self.requests.append(json.loads(json.dumps(params, default=str)))
        if not self.script:
            return FakeStream(reply(text("(fim do guião)")))
        step = self.script.pop(0)
        if callable(step):
            step = step(params["messages"])
        return FakeStream(step)

    def _upload(self, file):
        self.uploaded.append(file)
        return NS(id=f"file_{len(self.uploaded)}")


# ---------------------------------------------------------------------- ambiente
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_ROOT", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "AGENT_ENABLED", True)
    monkeypatch.setattr(config, "DEV_SIGNUP_CREDITS", 5)
    store = memory.MemoryStore()
    set_store(store)
    auth.set_auth(auth.MemoryAuth())
    storage.set_storage(storage.LocalStorage())
    compute.set_provider(LocalProvider(workers=1))
    api.reset_services()
    agent_api.reset_service()
    api._hits.clear()
    from studio.server import app
    app.state.maintenance = None
    return store, app


def signup(c, email="ana@x.com"):
    assert c.post("/api/auth/signup", json={"email": email, "password": "password123"}).status_code == 200


def upload(c, pid, kind, path):
    data = open(path, "rb").read()
    name = os.path.basename(path)
    r = c.post(f"/api/projects/{pid}/files", json={"kind": kind, "name": name, "size": len(data), "duration_s": 6.0})
    assert r.status_code == 200, r.text
    assert c.put(r.json()["upload"]["url"], files={"file": (name, io.BytesIO(data), "video/mp4")}).status_code == 200
    fid = r.json()["file"]["id"]
    assert c.post(f"/api/projects/{pid}/files/{fid}/complete").status_code == 200
    return fid


def wait(c, pid, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/agent/threads/{pid}").json()
        if r["thread"]["status"] == "idle":
            return r
        time.sleep(0.25)
    raise AssertionError("o turno não terminou")


def wait_analysis(c, pid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = c.get(f"/api/projects/{pid}").json()["project"]
        if p["reference_summary"] or (p.get("settings") or {}).get("analysis_error"):
            return
        time.sleep(0.25)
    raise AssertionError("análise não terminou")


def new_project(c, media):
    pid = c.post("/api/agent/threads", json={"name": "Carros"}).json()["project"]["id"]
    upload(c, pid, "reference", media["ref"])
    a = upload(c, pid, "source", media["clips"][0])
    b = upload(c, pid, "source", media["clips"][1])
    wait_analysis(c, pid)
    return pid, a, b


def last_version(store, pid, k=-1):
    return store.get_thread(pid)["state"]["versions"][k]


def tool_results(messages):
    out = {}
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out[b["tool_use_id"]] = b
    return out


def _text_of(res):
    c = res["content"]
    return c if isinstance(c, str) else next((b.get("text", "") for b in c if b.get("type") == "text"), "")


# ------------------------------------------------------------------------ fluxo
def test_conversa_completa_com_correcao(env, media):
    store, app = env
    c = TestClient(app, base_url="http://testserver")
    signup(c)
    pid, a, b = new_project(c, media)
    credits0 = c.get("/api/me").json()["credits"]

    captured = {}

    def after_preview(messages):  # lê o id da versão que a ferramenta devolveu, como o modelo faria
        for r in reversed(messages):
            if r["role"] == "user" and isinstance(r["content"], list):
                for bl in r["content"]:
                    if isinstance(bl, dict) and bl.get("type") == "tool_result" and "version" in _text_of(bl):
                        captured["preview"] = json.loads(_text_of(bl))["version"]
                        return reply(Block(type="thinking", thinking="Vou verificar o resultado."), tool("verify_output", version=captured["preview"]),
                                     tool("compare_frames", times_s=[1.0, 3.0], version=captured["preview"]))
        raise AssertionError("sem versão")

    def after_final(messages):
        for r in reversed(messages):
            if r["role"] == "user" and isinstance(r["content"], list):
                for bl in r["content"]:
                    if isinstance(bl, dict) and bl.get("type") == "tool_result" and '"quality": "standard"' in _text_of(bl):
                        captured["final"] = json.loads(_text_of(bl))["version"]
                        return reply(tool("present_version", version=captured["final"], note="Primeira versão"))
        raise AssertionError("sem versão final")

    def fix_final(messages):
        for r in reversed(messages):
            if r["role"] == "user" and isinstance(r["content"], list):
                for bl in r["content"]:
                    if isinstance(bl, dict) and bl.get("type") == "tool_result" and '"quality": "standard"' in _text_of(bl):
                        captured["fix"] = json.loads(_text_of(bl))["version"]
                        return reply(tool("present_version", version=captured["fix"]))
        raise AssertionError("sem versão corrigida")

    model = ScriptedModel([
        reply(text("Vou estudar a referência."), tool("analyze_reference"), tool("analyze_clips")),
        reply(tool("build_edit", mode="cuts_only", audio="reference", audio_volume=0.8, clip_volume=0.2)),
        reply(tool("update_plan", ops=[{"op": "set_captions", "items": [{"text": "uma linha de letra", "start_s": 0.5, "end_s": 2.0}]},
                                       {"op": "set_watermark", "handles": [{"platform": "tiktok", "handle": "Awls"}, {"platform": "instagram", "handle": "Awlls"}]}])),
        reply(tool("render", quality="preview")),
        after_preview,
        reply(tool("render", quality="standard", note="final")),
        after_final,
        reply(text("Está pronto: 6 segundos, com a música e a legenda da referência.")),
        # 2.ª mensagem: pedir uma alteração
        reply(tool("get_plan")),
        reply(tool("update_plan", ops=[{"op": "split_shot", "at_s": 0.5}, {"op": "set_transition", "index": 2, "type": "dissolve", "duration_s": 0.5}], why="mais dinâmica")),
        reply(tool("render", quality="standard")),
        fix_final,
        reply(tool("remember", preference="Gosta de dissoluções suaves")),
        reply(text("Feito, ficou mais dinâmica.")),
    ])
    agent_loop.set_client(model)

    r = c.post(f"/api/agent/threads/{pid}/messages", json={"text": "Edita os meus vídeos como a referência. Só áudio e legenda."})
    assert r.status_code == 200, r.text
    assert c.post(f"/api/agent/threads/{pid}/messages", json={"text": "outra"}).status_code == 409  # um turno de cada vez
    v = wait(c, pid)
    th = v["thread"]
    types = [e["type"] for e in th["events"]]
    assert types[0] == "user" and "analysis" in types and "video" in types and types[-1] == "assistant", types
    assert not [e for e in th["events"] if e["type"] == "tool" and e["status"] != "done"], [e for e in th["events"] if e["type"] == "tool"]
    assert any(e["type"] == "status" and "verificar" in e["text"] for e in th["events"])

    # o que o modelo recebeu: imagens para ver, e análise real
    results = tool_results(store.get_thread(pid)["messages"])
    with_images = [x for x in results.values() if isinstance(x["content"], list) and any(b.get("type") == "image" for b in x["content"])]
    assert len(with_images) >= 3 and model.uploaded
    report = next(json.loads(_text_of(x).split("\ncaptions")[0]) for x in results.values() if '"shots"' in _text_of(x) and '"pace"' in _text_of(x))
    assert report["n_shots"] >= 4 and report["audio"]["has_audio"]
    assert [] == [x for x in results.values() if x.get("is_error")], [x for x in results.values() if x.get("is_error")]

    # acompanhar um turno não passa pela base de dados, e só vem o que mudou
    assert c.get(f"/api/agent/threads/{pid}?after={th['last_rev']}").json()["thread"]["events"] == []

    # o vídeo é mesmo aquilo que foi pedido
    vid = captured["final"]
    url = v["thread"]["versions"][vid]["url"]
    assert url and v["project"]["has_output"] and v["project"]["resolution"]
    ver = last_version(store, pid)
    out_path = storage.get_storage().local_path("outputs", ver["output_path"])
    assert os.path.getsize(out_path) > 10000
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", out_path], capture_output=True, text=True)
    streams = json.loads(probe.stdout)["streams"]
    assert {s["codec_type"] for s in streams} == {"video", "audio"}
    assert c.get("/api/me").json()["credits"] == credits0 - 1  # só a final paga; a pré-visualização é grátis
    assert c.get(v["thread"]["versions"][vid]["download"], follow_redirects=False).status_code == 302

    # verificação medida devolvida ao modelo
    verif = next(json.loads(_text_of(x)) for x in results.values() if '"issues"' in _text_of(x))
    assert verif["ok"] and verif["metrics"]["has_audio"] and verif["metrics"]["duration_s"] > 5

    # 2.ª mensagem: histórico só cresce, contexto mantido
    before = json.dumps(store.get_thread(pid)["messages"], sort_keys=True)
    n_before = len(store.get_thread(pid)["messages"])
    r = c.post(f"/api/agent/threads/{pid}/messages", json={"text": "Mais dinâmica, por favor"})
    assert r.status_code == 200, r.text
    v = wait(c, pid)
    msgs = store.get_thread(pid)["messages"]
    assert json.dumps(msgs[:n_before], sort_keys=True) == before
    assert any("<project>" in b.get("text", "") for b in msgs[n_before]["content"])
    assert any(e["type"] == "video" and e["version"] == captured["fix"] for e in v["thread"]["events"])
    fix = last_version(store, pid)
    plan_after = fix["plan"]
    assert any(i.get("transition_in", {}) and i["transition_in"]["type"] == "dissolve" for t in plan_after["tracks"] if t["kind"] == "video" for i in t["items"])
    assert c.get("/api/me").json()["credits"] == credits0 - 2  # uma correção final tem o seu preço
    # preferência aprendida para os próximos projetos, e visível na mensagem seguinte
    assert c.get("/api/agent/config").json()["memory"] == ["Gosta de dissoluções suaves"]
    # o modelo recebeu sempre o mesmo início: prompt e ferramentas não mudam
    assert all(rq["system"] == model.requests[0]["system"] and rq["tools"] == model.requests[0]["tools"] for rq in model.requests)
    # o editor manual abre o que o agente fez
    ed = c.get(f"/api/projects/{pid}/editor")
    assert ed.status_code == 200 and {t["style"] for t in ed.json()["doc"]["texts"]} == {"box", "watermark"}


def test_ninguem_ve_a_conversa_de_outra_pessoa(env, media):
    store, app = env
    c1 = TestClient(app, base_url="http://testserver")
    signup(c1, "a@x.com")
    pid = c1.post("/api/agent/threads", json={}).json()["project"]["id"]
    c2 = TestClient(app, base_url="http://testserver")
    signup(c2, "b@x.com")
    assert c2.get(f"/api/agent/threads/{pid}").status_code == 404
    assert c2.post(f"/api/agent/threads/{pid}/messages", json={"text": "oi"}).status_code == 404
    assert c2.post(f"/api/agent/threads/{pid}/cancel").status_code == 404


def test_agente_desligado_explica_e_o_resto_funciona(env, monkeypatch):
    store, app = env
    monkeypatch.setattr(config, "AGENT_ENABLED", False)
    c = TestClient(app, base_url="http://testserver")
    signup(c)
    assert c.get("/api/agent/config").json()["enabled"] is False
    pid = c.post("/api/agent/threads", json={}).json()["project"]["id"]
    r = c.post(f"/api/agent/threads/{pid}/messages", json={"text": "oi"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "agent_disabled"


def test_erros_de_ferramenta_voltam_ao_modelo_que_recupera(env, media):
    store, app = env
    c = TestClient(app, base_url="http://testserver")
    signup(c)
    pid, a, b = new_project(c, media)
    model = ScriptedModel([
        reply(tool("update_plan", ops=[{"op": "remove_shot", "index": 0}]), tool("get_plan"), tool("analyze_reference", nao_existe=1), tool("inventada")),
        reply(text("Afinal ainda não há plano; explico à pessoa.")),
    ])
    agent_loop.set_client(model)
    assert c.post(f"/api/agent/threads/{pid}/messages", json={"text": "corta"}).status_code == 200
    v = wait(c, pid)
    res = tool_results(store.get_thread(pid)["messages"])
    assert len(res) == 4 and all(x["is_error"] for x in res.values()), res  # um resultado por chamada, todos na mesma mensagem
    assert "build_edit" in _text_of(next(iter(res.values())))
    assert v["thread"]["events"][-1]["type"] == "assistant"


def test_resposta_cortada_ou_recusada_nao_executa_ferramentas(env, media):
    store, app = env
    c = TestClient(app, base_url="http://testserver")
    signup(c)
    pid, a, b = new_project(c, media)
    model = ScriptedModel([
        reply(tool("render", quality="hd"), stop="max_tokens"),
        reply(text("Vou continuar com menos."), tool("view_frames", source="reference", times_s=[1.0]), stop="refusal"),
    ])
    agent_loop.set_client(model)
    assert c.post(f"/api/agent/threads/{pid}/messages", json={"text": "faz"}).status_code == 200
    v = wait(c, pid)
    assert not store.get_thread(pid)["state"].get("versions")
    assert v["thread"]["events"][-1]["code"] == "refusal"
    res = tool_results(store.get_thread(pid)["messages"])
    assert list(res.values())[0]["is_error"] and "cortada" in _text_of(list(res.values())[0])
    assert store.get_thread(pid)["messages"][-1]["role"] == "user"  # a recusa não fica no histórico


def test_sem_creditos_avisa_a_app_e_nao_gera_a_final(env, media):
    store, app = env
    c = TestClient(app, base_url="http://testserver")
    signup(c)
    pid, a, b = new_project(c, media)
    uid = c.get("/api/me").json()["id"]
    store.grant_credits(uid, -5, "admin", "t", "zerar")
    assert c.get("/api/me").json()["credits"] == 0
    model = ScriptedModel([
        reply(tool("build_edit", mode="cuts_only")),
        reply(tool("render", quality="standard")),
        reply(text("Faltam-te créditos para a versão final.")),
    ])
    agent_loop.set_client(model)
    assert c.post(f"/api/agent/threads/{pid}/messages", json={"text": "faz"}).status_code == 200
    v = wait(c, pid)
    assert any(e["type"] == "need_credits" for e in v["thread"]["events"])
    res = tool_results(store.get_thread(pid)["messages"])
    assert any(x.get("is_error") and "créditos" in _text_of(x) for x in res.values())
    assert not store.get_thread(pid)["state"].get("versions")


def test_parar_a_meio_e_seguir_na_mensagem_seguinte(env, media):
    store, app = env
    c = TestClient(app, base_url="http://testserver")
    signup(c)
    pid, a, b = new_project(c, media)
    started = []

    def slow(messages):
        started.append(1)
        return reply(tool("analyze_reference"), tool("analyze_clips"))

    model = ScriptedModel([slow, reply(text("Retomei."))])
    agent_loop.set_client(model)
    assert c.post(f"/api/agent/threads/{pid}/messages", json={"text": "começa"}).status_code == 200
    t0 = time.time()
    while not started and time.time() - t0 < 30:
        time.sleep(0.02)
    assert c.post(f"/api/agent/threads/{pid}/cancel").status_code == 200
    v = wait(c, pid)
    msgs = store.get_thread(pid)["messages"]
    tool_uses = [b["id"] for m in msgs if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
    assert set(tool_uses) <= set(tool_results(msgs))  # nenhum tool_use sem resultado: a API recusaria a conversa
    assert v["thread"]["events"][-1]["type"] in ("cancelled", "assistant")
    assert c.post(f"/api/agent/threads/{pid}/messages", json={"text": "continua"}).status_code == 200
    assert wait(c, pid)["thread"]["events"][-1]["text"] == "Retomei."


def test_reinicio_a_meio_retoma_sem_cobrar_nem_repetir(env, media):
    """O processo morre depois de o modelo pedir um render: ao arrancar, o turno continua e o
    job (idempotente pelo id da chamada) não é repetido nem cobrado duas vezes."""
    store, app = env
    c = TestClient(app, base_url="http://testserver")
    signup(c)
    pid, a, b = new_project(c, media)
    uid = c.get("/api/me").json()["id"]
    credits0 = store.balance(uid)
    model = ScriptedModel([reply(tool("build_edit", mode="cuts_only", audio="none")), reply(tool("render", quality="standard", tid="toolu_morre")), reply(text("Terminado."))])
    agent_loop.set_client(model)

    # simula a morte: a resposta com o tool_use está guardada, mas o resultado não
    svc = agent_api.service()
    real = agent_loop._run_tools
    state = {"calls": 0}

    def dies(ctx, box, tools, messages):
        state["calls"] += 1
        if tools[0]["id"] == "toolu_morre":
            raise SystemExit  # sai do turno sem guardar nada, como um processo morto
        return real(ctx, box, tools, messages)

    agent_loop._run_tools = dies
    try:
        assert c.post(f"/api/agent/threads/{pid}/messages", json={"text": "faz"}).status_code == 200
        t0 = time.time()
        while state["calls"] < 2 and time.time() - t0 < 120:
            time.sleep(0.05)
        time.sleep(0.5)
    finally:
        agent_loop._run_tools = real
    store.update_thread(pid, {"status": "running"})  # no processo morto o estado ficou 'running'
    svc._running.discard(pid)
    assert svc.recover() == 1
    v = wait(c, pid)
    assert v["thread"]["events"][-1]["text"] == "Terminado."
    assert len(store.get_thread(pid)["state"]["versions"]) == 1
    assert store.balance(uid) == credits0 - 1
    assert sum(1 for j in store.list_jobs(project_id=pid, limit=50) if j["kind"] == "agent_render") == 1


def test_recriar_o_estilo_com_musica_propria_e_cor_da_referencia(env, media, tmp_path):
    store, app = env
    c = TestClient(app, base_url="http://testserver")
    signup(c)
    pid, a, b = new_project(c, media)
    music = str(tmp_path / "musica.wav")
    _ff(["-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*330*t)*exp(-10*mod(t,0.4))':s=44100:d=10", music])
    data = open(music, "rb").read()
    r = c.post(f"/api/projects/{pid}/files", json={"kind": "music", "name": "musica.wav", "size": len(data), "duration_s": 10.0})
    assert c.put(r.json()["upload"]["url"], files={"file": ("musica.wav", io.BytesIO(data), "audio/wav")}).status_code == 200
    mid = r.json()["file"]["id"]
    assert c.post(f"/api/projects/{pid}/files/{mid}/complete").status_code == 200

    def render_check(messages):
        rs = tool_results(messages)
        version = next(json.loads(_text_of(x))["version"] for x in rs.values() if '"version"' in _text_of(x))
        return reply(tool("verify_output", version=version), tool("view_frames", source=version, times_s=[1.0, 2.0]))

    model = ScriptedModel([
        reply(tool("analyze_audio", source=mid)),
        reply(tool("build_edit", mode="style", audio=mid, match_color=0.7, format="1:1")),
        reply(tool("update_plan", ops=[{"op": "snap_cuts_to_beats", "max_shift_s": 0.3}, {"op": "set_transition", "index": 1, "type": "dissolve", "duration_s": 0.4},
                                       {"op": "update_shot", "index": 0, "zoom_start": 1.0, "zoom_end": 1.4, "speed_curve": [1, 2, 0.5]},
                                       {"op": "set_effects", "add": [{"type": "zoom_punch", "at_s": 1.5, "duration_s": 0.3, "magnitude": 0.15}, {"type": "flash", "at_s": 3.0, "duration_s": 0.2}]},
                                       {"op": "add_text", "text": "TITULO", "start_s": 0.2, "end_s": 1.5}])),
        reply(tool("render", quality="preview")),
        render_check,
        reply(tool("render", quality="preview", variant="no_music")),
        reply(text("Pronto.")),
    ])
    agent_loop.set_client(model)
    assert c.post(f"/api/agent/threads/{pid}/messages", json={"text": "Recria o estilo com a minha música"}).status_code == 200
    wait(c, pid)
    res = tool_results(store.get_thread(pid)["messages"])
    assert not [x for x in res.values() if x.get("is_error")], [_text_of(x) for x in res.values() if x.get("is_error")]
    audio = next(json.loads(_text_of(x)) for x in res.values() if '"beats_s"' in _text_of(x))
    assert audio["has_audio"] and audio["bpm"] and len(audio["beats_s"]) > 5
    th = store.get_thread(pid)
    assert th["state"]["grade"] and th["state"]["beats"] == audio["beats_s"]
    versions = th["state"]["versions"]
    assert [v["variant"] for v in versions] == ["main", "no_music"] and all(v["credits"] == 0 for v in versions)
    assert versions[0]["resolution"] == "540x540"  # o formato pedido, à resolução da pré-visualização
    plan = versions[0]["plan"]
    assert plan["look"]["matrix"]
    assert {t["kind"] for t in plan["tracks"]} == {"video", "audio", "text"}
    verify = next(json.loads(_text_of(x)) for x in res.values() if '"issues"' in _text_of(x))
    assert verify["ok"], verify
    st = storage.get_storage()
    for v in versions:
        streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", st.local_path("outputs", v["output_path"])],
                                            capture_output=True, text=True).stdout)["streams"]
        assert {x["codec_type"] for x in streams} == {"video", "audio"}
