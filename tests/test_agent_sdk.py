"""O ciclo com o SDK verdadeiro da Anthropic contra um servidor HTTP local: sem isto, os testes só
provariam o ciclo contra o nosso próprio guião. Confirma o formato real dos pedidos (cabeçalhos beta,
campos), a leitura do streaming e que o conteúdo devolvido volta à API tal como veio (assinaturas
dos blocos de pensamento incluídas)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace as NS

import anthropic
import pytest

from studio import config
from studio.agent import loop as agent_loop
from studio.agent.tools import tool_definitions


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message(blocks, stop):
    """O que a API envia em streaming: blocos delta a delta, com o id e a assinatura originais."""
    evs = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [], "stop_reason": None, "stop_sequence": None,
                                                 "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2}}}]
    for i, b in enumerate(blocks):
        if b["type"] == "thinking":
            evs += [{"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": b["thinking"]}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}}]
        elif b["type"] == "text":
            evs += [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}}]
        elif b["type"] == "tool_use":
            evs += [{"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}}]
        evs.append({"type": "content_block_stop", "index": i})
    evs += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 20}}, {"type": "message_stop"}]
    return sse(evs)


class Api(BaseHTTPRequestHandler):
    requests = []
    script = []
    reject_features_once = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path.startswith("/v1/files"):
            out = json.dumps({"id": "file_abc", "type": "file", "filename": "x.jpg", "mime_type": "image/jpeg", "size_bytes": 3, "created_at": "2026-01-01T00:00:00Z"}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(out)
            return
        req = json.loads(body)
        Api.requests.append({"headers": dict(self.headers), "body": req})
        if Api.reject_features_once and "fallbacks" in req:
            Api.reject_features_once = False
            out = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "fallbacks: not supported"}}).encode()
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(Api.script.pop(0))


@pytest.fixture()
def api_server(monkeypatch):
    Api.requests, Api.script, Api.reject_features_once = [], [], False
    srv = HTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    agent_loop.set_client(anthropic.Anthropic(api_key="test-key", base_url=f"http://127.0.0.1:{srv.server_port}", max_retries=0))
    monkeypatch.setattr(agent_loop, "_compat_mode", False)
    yield Api
    srv.shutdown()
    agent_loop.set_client(None)


class Ctx:
    """O mínimo do TurnContext: regista o que o ciclo faz."""

    def __init__(self, messages):
        self.messages = messages
        self.events = []
        self.live = ""
        self.status = ""
        self.tools = []
        self.usage = []
        self.state = {}

    def check_cancel(self):
        pass

    def live_text(self, t):
        self.live += t

    def live_status(self, t):
        self.status += t

    def clear_live(self):
        pass

    def emit(self, ev):
        self.events.append(ev)

    def save_messages(self):
        pass

    def add_usage(self, final):
        self.usage.append(final.usage)

    def turn_input_tokens(self):
        return 0

    def tool_started(self, *a):
        self.tools.append(a[1])

    def tool_finished(self, *a, **k):
        pass

    def project(self):
        return {"files": [], "timeline": None}

    def wait_upload_analysis(self):
        pass

    def save_state(self):
        pass


def test_pedido_real_e_historico_devolvido_tal_como_veio(api_server):
    thinking = {"type": "thinking", "thinking": "Vou ver o plano.", "signature": "sig-abc"}
    tool_use = {"type": "tool_use", "id": "toolu_1", "name": "get_plan", "input": {}}
    api_server.script = [message([thinking, tool_use], "tool_use"), message([{"type": "text", "text": "Ainda não há plano."}], "end_turn")]
    ctx = Ctx([{"role": "user", "content": [{"type": "text", "text": "olá"}]}])
    agent_loop.run_turn(ctx)

    first, second = api_server.requests
    b = first["body"]
    assert b["model"] == config.AGENT_MODEL and b["stream"] is True
    assert b["fallbacks"] == "default"
    assert b["thinking"]["type"] == "adaptive" and b["thinking"]["display"] == "updates"
    assert b["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert b["output_config"]["effort"] == config.AGENT_EFFORT
    assert b["cache_control"] == {"type": "ephemeral"}
    assert b["tools"] == tool_definitions() and all(t["eager_input_streaming"] for t in b["tools"])
    betas = first["headers"]["anthropic-beta"] if "anthropic-beta" in first["headers"] else first["headers"]["Anthropic-Beta"]
    for name in agent_loop.BETAS:
        assert name in betas

    # 2.º pedido: o que veio da API volta byte a byte; resultado da ferramenta na mensagem seguinte
    assert second["body"]["messages"][0] == b["messages"][0]
    assert second["body"]["system"] == b["system"] and second["body"]["tools"] == b["tools"]
    assistant = second["body"]["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == thinking
    assert assistant["content"][1]["id"] == "toolu_1" and assistant["content"][1]["name"] == "get_plan"
    tool_msg = second["body"]["messages"][2]
    assert tool_msg["role"] == "user" and tool_msg["content"][0]["tool_use_id"] == "toolu_1" and tool_msg["content"][0]["is_error"] is True

    assert ctx.tools == ["get_plan"]
    assert [e["type"] for e in ctx.events] == ["status", "assistant"]
    assert ctx.events[0]["text"] == "Vou ver o plano."
    assert "Ainda não há plano." in ctx.live and "Vou ver o plano." in ctx.status


def test_api_recusa_funcionalidade_opcional_e_o_agente_continua(api_server):
    api_server.reject_features_once = True
    api_server.script = [message([{"type": "text", "text": "Olá."}], "end_turn")]
    ctx = Ctx([{"role": "user", "content": [{"type": "text", "text": "olá"}]}])
    agent_loop.run_turn(ctx)
    assert [e["type"] for e in ctx.events] == ["assistant"]
    last = api_server.requests[-1]["body"]
    assert "fallbacks" not in last and "context_management" not in last and last["system"] and last["tools"]


def test_imagem_vai_para_a_files_api_e_o_historico_guarda_so_o_id(api_server, tmp_path):
    from studio.agent.service import AgentService, TurnContext
    p = tmp_path / "x.jpg"
    p.write_bytes(b"\xff\xd8\xff")
    storage = NS(is_local=True, local_path=lambda b, q: str(p))
    store = NS(update_thread=lambda *a, **k: None)
    svc = AgentService(store, storage, lambda: None)
    ctx = TurnContext(svc, {"project_id": "p1"}, "u1")
    block = ctx.image_block({"path": "a/b/x.jpg", "label": "frames"})
    assert block == {"type": "image", "source": {"type": "file", "file_id": "file_abc"}}
    assert ctx.state["files_api"] == ["file_abc"]
