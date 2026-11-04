"""Ponta-a-ponta com o MOTOR REAL, pelo mesmo caminho que a produção usa:
signup -> projeto -> upload direto (URL assinada) -> análise da referência -> geração paga
(worker real numa thread, como o fornecedor `local`) -> vídeo final válido -> editor ->
re-exportação sem nova geração.

Os vídeos de entrada são gerados aqui (ffmpeg lavfi), por isso o teste não depende de material
externo. Demora ~1-3 min (gera vídeos de verdade).
"""
import io
import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from studio import api, auth, compute, config, storage
from studio.compute.local import LocalProvider
from studio.store import memory, set_store

pytestmark = pytest.mark.e2e


def _ff(args: list[str]) -> None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-500:]


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    # referência: 8 planos de 1s de fontes diferentes, cortes secos, com áudio de "batidas"
    srcs = ["testsrc2", "mandelbrot", "rgbtestsrc", "smptebars", "testsrc", "cellauto", "testsrc2", "mandelbrot"]
    inputs, chains = [], []
    for i, s in enumerate(srcs):
        inputs += ["-f", "lavfi", "-t", "1", "-i", f"{s}=size=360x640:rate=30" if s not in ("cellauto",) else f"{s}=size=360x640:rate=30:rule=110"]
        chains.append(f"[{i}:v]scale=360:640,setsar=1,fps=30,format=yuv420p[v{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(len(srcs))) + f"concat=n={len(srcs)}:v=1:a=0[v]"
    ref = str(d / "ref.mp4")
    _ff([*inputs, "-f", "lavfi", "-t", "8", "-i", "aevalsrc='0.6*sin(2*PI*220*t)*exp(-12*mod(t,0.5))':s=44100",
         "-filter_complex", graph, "-map", "[v]", "-map", f"{len(srcs)}:a", "-c:v", "libx264", "-preset", "veryfast",
         "-c:a", "aac", "-shortest", ref])
    # clips do utilizador: material DIFERENTE do da referência (cenário B: edição por estilo, estendida
    # a 25-30 s). Com os mesmos geradores da referência, o motor reconheceria o material e
    # reconstruiria a edição (cenário A) — isso é testado em tests/test_reconstruct.py.
    a, b = str(d / "a.mp4"), str(d / "b.mp4")
    _ff(["-f", "lavfi", "-t", "6", "-i", "life=size=480x854:rate=30:mold=10:ratio=0.1:death_color=#202020:life_color=#ff8800",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", a])
    _ff(["-f", "lavfi", "-t", "6", "-i", "sierpinski=size=480x854:rate=30", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", b])
    return {"ref": ref, "a": a, "b": b}


def _upload(c, pid, kind, path):
    data = open(path, "rb").read()
    r = c.post(f"/api/projects/{pid}/files", json={"kind": kind, "name": os.path.basename(path), "size": len(data),
                                                   "duration_s": 6})
    assert r.status_code == 200, r.text
    up = r.json()["upload"]
    assert c.put(up["url"], files={"file": (os.path.basename(path), io.BytesIO(data), "video/mp4")}).status_code == 200
    r = c.post(f"/api/projects/{pid}/files/{r.json()['file']['id']}/complete")
    assert r.status_code == 200, r.text
    return r.json()


def _wait(c, job_id, timeout=400):
    t0, stages = time.time(), []
    while time.time() - t0 < timeout:
        j = c.get(f"/api/jobs/{job_id}").json()["job"]
        if j["stage"] and (not stages or stages[-1] != j["stage"]):
            stages.append(j["stage"])
        if j["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return j, stages
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} não terminou em {timeout}s (etapas: {stages})")


def _duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def test_fluxo_completo_com_motor_real(media, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_ROOT", str(tmp_path / "storage"))
    store = memory.MemoryStore()
    set_store(store)
    auth.set_auth(auth.MemoryAuth())
    storage.set_storage(storage.LocalStorage())
    compute.set_provider(LocalProvider(workers=1))
    api.reset_services()
    api._hits.clear()
    from studio.server import app
    app.state.maintenance = None
    c = TestClient(app, base_url="http://testserver")

    assert c.post("/api/auth/signup", json={"email": "e2e@x.com", "password": "password123"}).status_code == 200
    me = c.get("/api/me").json()
    store.grant_credits(me["id"], 2, "admin", "test", "e2e")
    pid = c.post("/api/projects", json={"name": "E2E"}).json()["project"]["id"]

    res = _upload(c, pid, "reference", media["ref"])
    analysis, _ = _wait(c, res["job"]["id"], 120)
    assert analysis["status"] == "COMPLETED", analysis
    summary = c.get(f"/api/projects/{pid}").json()["project"]["reference_summary"]
    assert summary["n_shots"] >= 4 and summary["tags"], summary

    _upload(c, pid, "source", media["a"])
    _upload(c, pid, "source", media["b"])
    r = c.post(f"/api/projects/{pid}/generate", json={"profile": "fast", "force": True}, headers={"Idempotency-Key": "e2e-1"})
    assert r.status_code == 200, r.text
    job, stages = _wait(c, r.json()["job"]["id"])
    assert job["status"] == "COMPLETED", store.get_job(job["id"])
    assert {"analyzing_reference", "rendering"} <= set(stages) or "complete" in stages, stages
    assert c.get("/api/me").json()["credits"] == 1

    p = c.get(f"/api/projects/{pid}").json()["project"]
    assert p["status"] == "completed" and p["can_edit"] and p["thumbnail_url"]
    out_local = storage.get_storage().local_path("outputs", store.get_project(pid)["output_path"])
    d = _duration(out_local)
    assert 20 <= d <= 32, d
    assert c.get(p["thumbnail_url"]).status_code == 200
    j = store.get_job(job["id"])
    assert j["processing_time_s"] and j["metrics"]["timings_s"]["render"] > 0

    # editor: apaga metade dos planos e põe texto -> re-exporta sem nova geração nem nova análise
    st = c.get(f"/api/projects/{pid}/editor").json()
    doc = st["doc"]
    n0 = len(doc["shots"])
    doc["shots"] = doc["shots"][: max(1, n0 // 2)]
    doc["texts"] = [{"text": "E2E", "start_s": 0.0, "end_s": 1.0}]
    r = c.post(f"/api/projects/{pid}/edit", json={"doc": doc})
    assert r.status_code == 200, r.text
    ej, _ = _wait(c, r.json()["job"]["id"])
    assert ej["status"] == "COMPLETED", store.get_job(ej["id"])
    out2 = storage.get_storage().local_path("outputs", store.get_project(pid)["output_path"])
    assert out2 != out_local and not os.path.exists(out_local)  # o vídeo anterior foi limpo
    assert _duration(out2) < d - 3
    assert c.get("/api/me").json()["credits"] == 1  # re-exportar não cobrou
