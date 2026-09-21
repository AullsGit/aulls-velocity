"""Avalia o Velocity Detector: positivos (refs/refs_soft/refs_aggr/refs_pro) vs negativos (vídeos crus do utilizador + sintéticos).
Uso: venv\Scripts\python.exe bench\detector_eval.py   (AVISO: limiares afinados com estes mesmos dados -> otimista)"""
import glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from velocity.reference.analyzer import analyze_reference
from velocity.detector.velocity_detector import detect
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
groups = [("POS", "refs"), ("POS", "refs_soft"), ("POS", "refs_aggr"), ("POS", "refs_pro"), ("NEG", "user"), ("NEG", "synthetic/neg_")]
seen, tp, fn, tn, fp = set(), 0, [], 0, []
for lab, g in groups:
    pat = os.path.join(R, "bench", "data", (g + "*.mp4") if "neg_" in g else os.path.join(g, "*.mp4"))
    for p in sorted(glob.glob(pat)):
        d = analyze_reference(p, with_audio=("neg_" not in p))
        key = (d.n_frames, tuple(d.cut_frames()))
        if key in seen and lab == "POS": continue
        seen.add(key)
        r = detect(d); nm = f"{g.split('/')[0]}/{os.path.basename(p)[:-4]}"
        ok = r.is_velocity == (lab == "POS")
        if lab == "POS": tp += r.is_velocity; (None if r.is_velocity else fn.append((nm, r.score)))
        else: tn += (not r.is_velocity); (None if not r.is_velocity else fp.append((nm, r.score)))
        print(f"{lab} {nm:22s} {'OK ' if ok else 'ERR'} score={r.score:.2f} {r.subscores}")
npos = len({k for k in seen}) - 0
print(f"\nrecall (únicos): {tp}/{tp+len(fn)}  falsos negativos: {fn}\nespecificidade: {tn}/{tn+len(fp)}  falsos positivos: {fp}")
