"""O ciclo do agente: pede ao modelo, executa as ferramentas, volta a pedir, até ele acabar.

Regras que este ciclo garante:
  * o histórico só cresce. O conteúdo devolvido pela API é guardado e reenviado como veio (os blocos
    de pensamento ficam ligados ao que os precede), e o prompt e as ferramentas são fixos;
  * cada passo é guardado antes do seguinte, por isso um reinício do servidor retoma o turno;
  * cada `tool_use` recebe sempre um `tool_result`, mesmo quando falha ou o utilizador cancela;
  * uma resposta cortada ou recusada nunca executa as ferramentas que trazia.
"""
from __future__ import annotations

import logging
import time

import anthropic

from studio import config
from studio.agent.prompts import SYSTEM_PROMPT
from studio.agent.tools import ToolFailure, Toolbox, TurnCancelled, tool_definitions
from studio.errors import ApiError

log = logging.getLogger("awls.agent")

BETAS = ["server-side-fallback-2026-07-01", "thinking-display-updates-2026-08-18", "thinking-binding-controls-2026-08-01", "compact-2026-01-12"]
INTERRUPTED_NOTE = "This part of the response was interrupted before it finished."
MAX_CONTINUE = 3  # respostas seguidas cortadas por max_tokens antes de desistir

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4, timeout=600.0)
    return _client


def set_client(c) -> None:
    """Só para testes."""
    global _client
    _client = c


def _as_dicts(content) -> list[dict]:
    """Blocos da resposta como JSON guardável; sem campos a None, que a API recusa de volta."""
    out = []
    for b in content:
        d = b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else dict(b)
        out.append(d)
    return out


def request_params(messages: list[dict]) -> dict:
    return dict(
        model=config.AGENT_MODEL, max_tokens=config.AGENT_MAX_TOKENS, betas=BETAS, fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT}], tools=tool_definitions(), messages=messages,
        thinking={"type": "adaptive", "display": "updates", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
        output_config={"effort": config.AGENT_EFFORT}, cache_control={"type": "ephemeral"},
        context_management={"edits": [{"type": "compact_20260112"}]},
    )


def _pending_tool_uses(messages: list[dict]) -> list[dict]:
    """`tool_use` da última resposta ainda sem resultado (o processo morreu a meio de uma ferramenta)."""
    if not messages or messages[-1]["role"] != "assistant":
        return []
    content = messages[-1]["content"]
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _call_model(ctx, messages: list[dict]):
    """Uma resposta do modelo, com o texto e as notas de progresso a chegarem à interface em direto."""
    client = get_client()
    with client.beta.messages.stream(**request_params(messages)) as stream:
        for ev in stream:
            ctx.check_cancel()
            if getattr(ev, "type", None) == "content_block_delta":
                d = ev.delta
                dt = getattr(d, "type", None)
                if dt == "text_delta":
                    ctx.live_text(d.text)
                elif dt == "thinking_delta" and d.thinking:
                    ctx.live_status(d.thinking)
        return stream.get_final_message()


def run_turn(ctx) -> None:
    """Executa um turno inteiro. `ctx`: service.TurnContext (estado, eventos, persistência)."""
    box = Toolbox(ctx)
    messages = ctx.messages
    iterations = cut_offs = 0
    # o processo morreu a meio de uma ferramenta: repete-a (os jobs são idempotentes pelo id da chamada)
    pending = _pending_tool_uses(messages)
    if pending:
        _run_tools(ctx, box, pending, messages)
    while True:
        iterations += 1
        if iterations > config.AGENT_MAX_ITERATIONS or ctx.turn_input_tokens() > config.AGENT_MAX_TURN_INPUT_TOKENS:
            ctx.emit({"type": "error", "code": "turn_limit", "message": "Parei aqui: esta tarefa está a ficar longa de mais para uma só mensagem. Diz-me se devo continuar."})
            return
        ctx.check_cancel()
        try:
            final = _call_model(ctx, messages)
        except TurnCancelled:
            raise
        except ValueError as e:  # JSON de ferramenta que o SDK não conseguiu ler nem de forma tolerante
            log.warning("resposta com JSON ilegível: %s", e)
            cut_offs += 1
            if cut_offs > MAX_CONTINUE:
                ctx.emit({"type": "error", "code": "bad_response", "message": "Não consegui concluir este passo. Tenta de novo, por favor."})
                return
            continue
        ctx.add_usage(final)
        content = _as_dicts(final.content)
        reason = final.stop_reason
        texts = [b["text"] for b in content if b.get("type") == "text" and b.get("text")]
        for b in content:
            if b.get("type") == "thinking" and b.get("thinking") and b["thinking"] != INTERRUPTED_NOTE:
                ctx.emit({"type": "status", "text": b["thinking"]})
        ctx.clear_live()
        if texts:
            ctx.emit({"type": "assistant", "text": "\n\n".join(texts).strip()})
        if reason == "refusal":
            # nada desta resposta entra no histórico nem é executado; a próxima mensagem começa limpa
            ctx.emit({"type": "error", "code": "refusal", "message": "Não posso ajudar com esse pedido. Se quiseres, diz-me de outra forma o que pretendes para o vídeo."})
            return
        messages.append({"role": "assistant", "content": content})
        ctx.save_messages()
        if reason == "pause_turn":
            continue
        tools = [b for b in content if b.get("type") == "tool_use"]
        if reason in ("max_tokens", "model_context_window_exceeded"):
            cut_offs += 1
            if tools:  # entrada possivelmente truncada: não se executa, mas tem de ter resultado
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": b["id"], "is_error": True, "content": "A resposta foi cortada antes de esta chamada terminar; não foi executada. Tenta de novo com menos texto."}
                    for b in tools]})
                ctx.save_messages()
            elif cut_offs <= MAX_CONTINUE:
                messages.append({"role": "user", "content": "Continue."})
                ctx.save_messages()
            if cut_offs > MAX_CONTINUE:
                ctx.emit({"type": "error", "code": "too_long", "message": "A resposta ficou longa de mais. Pede-me um passo mais pequeno, por favor."})
                return
            continue
        if not tools:
            return
        _run_tools(ctx, box, tools, messages)


def _run_tools(ctx, box: Toolbox, tools: list[dict], messages: list[dict]) -> None:
    """Corre as ferramentas por ordem e devolve todos os resultados na mesma mensagem."""
    results = []
    cancelled = False
    for b in tools:
        if cancelled:
            results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True, "content": "Cancelado pela pessoa."})
            continue
        ctx.tool_started(b["id"], b["name"], b.get("input") or {})
        t0 = time.time()
        try:
            r = box.run(b["name"], b.get("input") or {}, b["id"])
            content: list[dict] = [{"type": "text", "text": r.text}]
            for img in r.images:
                content.append({"type": "text", "text": img.get("label", "")})
                content.append(ctx.image_block(img))
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": content})
            ctx.tool_finished(b["id"], ok=True, seconds=time.time() - t0, images=r.images)
        except TurnCancelled:
            cancelled = True
            results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True, "content": "Cancelado pela pessoa."})
            ctx.tool_finished(b["id"], ok=False, seconds=time.time() - t0, cancelled=True)
        except (ToolFailure, ApiError) as e:
            msg = e.message if isinstance(e, ApiError) else str(e)
            results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True, "content": msg})
            ctx.tool_finished(b["id"], ok=False, seconds=time.time() - t0, error=msg)
        except Exception:
            log.exception("ferramenta %s falhou", b["name"])
            results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True, "content": "Erro interno nesta ferramenta. Tenta outro caminho ou explica o problema à pessoa."})
            ctx.tool_finished(b["id"], ok=False, seconds=time.time() - t0, error="internal")
    messages.append({"role": "user", "content": results})
    ctx.save_messages()
    if cancelled:
        raise TurnCancelled()
