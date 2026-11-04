"""O agente com o MODELO VERDADEIRO, ponta a ponta, sem browser (precisa de ANTHROPIC_API_KEY).

Os testes (tests/test_agent_e2e.py) usam um guião no lugar do modelo: provam as ferramentas e o ciclo,
não a qualidade das decisões. Isto é o que prova o resto, e diz quanto custou.

    python bench/agent_live.py --ref R.mp4 --clips a.mp4 b.mp4 --ask "O que pedes ao agente" [--ask "correção seguinte"]

Escreve a conversa e os vídeos apresentados em bench/out/agent_live/<data>/.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ref", required=True)
    ap.add_argument("--clips", nargs="+", required=True)
    ap.add_argument("--music")
    ap.add_argument("--ask", action="append", required=True, help="uma mensagem por turno, por ordem")
    ap.add_argument("--timeout", type=int, default=1800, help="segundos por turno")
    a = ap.parse_args()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("Falta ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2
    os.environ.setdefault("DEV_SIGNUP_CREDITS", "20")
    os.environ.setdefault("VELOCITY_NO_TEXT", "1")
    out_dir = os.path.join(ROOT, "bench", "out", "agent_live", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    from fastapi.testclient import TestClient
    from studio import storage
    from studio.server import app
    from studio.store import get_store

    c = TestClient(app, base_url="http://testserver")
    with c:  # corre o arranque da app (monitor de jobs)
        creds = {"email": f"live{secrets.token_hex(3)}@awls.test", "password": secrets.token_urlsafe(12)}
        c.post("/api/auth/signup", json=creds).raise_for_status()
        pid = c.post("/api/agent/threads", json={"name": "agent live"}).json()["project"]["id"]

        def upload(kind: str, path: str) -> None:
            d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip() or 0)
            r = c.post(f"/api/projects/{pid}/files", json={"kind": kind, "name": os.path.basename(path), "size": os.path.getsize(path), "duration_s": d})
            r.raise_for_status()
            with open(path, "rb") as fh:
                c.put(r.json()["upload"]["url"], files={"file": (os.path.basename(path), fh, "video/mp4")}).raise_for_status()
            c.post(f"/api/projects/{pid}/files/{r.json()['file']['id']}/complete").raise_for_status()

        upload("reference", a.ref)
        for p in a.clips:
            upload("source", p)
        if a.music:
            upload("music", a.music)

        for text in a.ask:
            print(f"\n>>> {text}")
            t0 = time.time()
            r = c.post(f"/api/agent/threads/{pid}/messages", json={"text": text})
            r.raise_for_status()
            seen = r.json()["thread"]["last_rev"]
            while time.time() - t0 < a.timeout:
                th = c.get(f"/api/agent/threads/{pid}?after={seen}").json()["thread"]
                for ev in th["events"]:
                    if ev["type"] in ("assistant", "status", "error", "cancelled", "need_credits"):
                        print(f"[{ev['type']}] {ev.get('text') or ev.get('message') or ''}")
                    elif ev["type"] == "tool":
                        print(f"[tool] {ev['name']}")
                    elif ev["type"] == "video":
                        print(f"[vídeo] {ev['version']}: {ev.get('note')}")
                seen = max(seen, th["last_rev"])
                if th["status"] == "idle":
                    break
                time.sleep(1)
            print(f"--- turno: {time.time() - t0:.0f}s")

        t = get_store().get_thread(pid)
        st = storage.get_storage()
        for v in t["state"].get("versions") or []:
            if v.get("output_path") and getattr(st, "is_local", False):
                shutil.copyfile(st.local_path("outputs", v["output_path"]), os.path.join(out_dir, f"{v['id']}_{v['quality']}.mp4"))
        json.dump({"events": t["events"], "usage": t["usage"], "versions": [{k: v.get(k) for k in ("id", "quality", "variant", "note", "duration_s", "resolution", "presented", "credits")} for v in t["state"].get("versions") or []],
                   "plan": t["state"].get("plan")}, open(os.path.join(out_dir, "conversa.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\nCusto estimado do modelo: {t['usage']}\nResultados em {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
