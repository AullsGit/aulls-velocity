#!/bin/bash
# A/B do realce do sujeito: mesma referência e clips, sem e com --subject-enhance.
cd "$(dirname "$0")/.."
C="bench/data/user/user_01.mp4 bench/data/user/user_02.mp4 bench/data/user/user_03.mp4"
for r in refs_soft/soft_01 refs_soft/soft_04 refs_aggr/aggr_02 refs_aggr/aggr_10; do
  n=$(basename $r)
  PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m velocity make --ref bench/data/$r.mp4 --clips $C --audio-ref --force --out bench/out/ab_${n}_off.mp4 --report bench/out/ab_${n}_off.json > bench/out/ab_${n}_off.log 2>&1
  PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m velocity make --ref bench/data/$r.mp4 --clips $C --audio-ref --force --subject-enhance --out bench/out/ab_${n}_on.mp4 --report bench/out/ab_${n}_on.json > bench/out/ab_${n}_on.log 2>&1
done
echo done > bench/out/ab_done.flag
