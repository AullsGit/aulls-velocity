"""Valida o perfil de look contra as etiquetas do utilizador (pastas refs_soft / refs_aggr).

Uso: venv\\Scripts\\python.exe bench\\look_eval.py
Vídeos repetidos entre pastas (mesmo nº de frames e mesmos cortes) contam uma só vez.
AVISO: os limiares de agressividade foram escolhidos olhando para estes mesmos dados;
sem conjunto de teste separado, a exatidão aqui é otimista.
"""
import glob
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from velocity.reference.analyzer import analyze_reference  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
rows, seen = [], {}
for label, folder in (("soft", "refs_soft"), ("aggr", "refs_aggr")):
    for p in sorted(glob.glob(os.path.join(ROOT, "bench", "data", folder, "*.mp4"))):
        d = analyze_reference(p)
        key = (d.n_frames, tuple(d.cut_frames()))
        if key in seen:
            continue
        seen[key] = 1
        rows.append((label, os.path.basename(p)[:-4], d))

ok = 0
print(f"{'id':8s} {'etiqueta':8s} {'aggr':>5s} {'motion':>7s} {'trans/s':>7s} {'punch/s':>7s} {'shake':>6s} {'sharp':>6s}  efeitos medidos")
for label, name, d in rows:
    lk = d.look
    pred = "aggr" if lk.aggressiveness >= 0.5 else "soft"
    ok += pred == label
    cnt = Counter(t.type for t in d.effects)
    print(f"{name:8s} {label:8s} {lk.aggressiveness:5.2f} {lk.motion_energy_mean:7.4f} {lk.effect_transition_rate:7.2f} "
          f"{lk.punch_rate:7.2f} {lk.shake_fraction:6.2f} {lk.sharpness:6.0f}  {dict(cnt)}  {'OK' if pred == label else 'X  <-- errado'}")
print(f"\nclassificação suave/agressivo correta: {ok}/{len(rows)} (vídeos únicos)")
