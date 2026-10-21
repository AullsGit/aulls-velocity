"""Cenário A — descobrir, frame a frame, de onde veio cada imagem da referência.

Quando o utilizador envia o MESMO material que foi usado na referência, a edição pode ser
reconstruída em vez de imitada. Para cada plano da referência:
  1. recuperação: SIFT em keyframes dos clips (poucos por segundo) + votação dos vizinhos;
  2. verificação geométrica: RANSAC (semelhança) entre o frame da referência e o candidato;
  3. registo fino: afim por fluxo óptico (Lucas-Kanade + RANSAC) contra os frames vizinhos da
     fonte à taxa NATIVA, escolhendo o instante que melhor correlaciona (NCC); depois seguimento
     frame a frame para a frente e para trás (apanha speed ramps, freezes e zoom/pan/shake com a
     curva real, não um modelo);
  4. cor: matriz afim RGB 3x4 por plano (mínimos quadrados entre a referência e a fonte
     registada, sem contornos nem píxeis saturados) — brilho, contraste, saturação e balanço;
  5. cortes suaves (flash, dip): o frame exato da troca de conteúdo decide-se pelo conteúdo.
Nada é inventado: cada plano tem uma confiança medida (correlação do registo). Planos abaixo do
limiar ficam por alinhar e o motor usa o matching normal (cenário B) só nesses.

Custo: cada clip é descodificado no máximo 3 vezes (índice + 2 passagens sequenciais pelas janelas
que os planos precisam) — com fontes de GOP longo, um seek por plano descodificava o ficheiro
desde o início de cada vez.

Limitações conhecidas (medidas no banco sintético, ver docs/STATUS.md): espelhamento (flip) não
é detetado; em blur/glitch/cross-fade a geometria não é medida (é extrapolada dos vizinhos e a
transição é reaplicada por cima).
"""
from __future__ import annotations

import itertools
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import cv2
import numpy as np

from velocity.io.frames import decode_range as decode
from velocity.io.frames import iter_range
from velocity.io.probe import probe

REF_W = 256  # largura de trabalho da referência
SRC_W = 320  # das fontes (a referência costuma ser um recorte ampliado: um pouco mais de detalhe ajuda)
COLOR_W = 160  # largura das cópias a cores das fontes (só para o ajuste de cor, que é global)
KF_RATE = 3.0  # keyframes/s no índice de recuperação
KF_BUDGET = 1800  # máx. keyframes no total (clips longos ficam com menos por segundo)
MIN_INLIERS = 15
NCC_MIN = 0.6  # correlação do registo abaixo disto: o frame não conta como medido
SHOT_MIN_CONF = 0.8  # confiança mínima para um plano ser reconstruído
ALIGN_WORKERS = int(os.environ.get("VELOCITY_ALIGN_WORKERS", str(min(6, os.cpu_count() or 2))))
REF_MEM_MB = 600  # acima disto a referência é descodificada plano a plano


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def native_fps(info) -> float:
    return float(info.fps_nominal or info.fps_avg or 30.0)


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img


def _norm(w: int, h: int) -> np.ndarray:
    """Índice de píxel -> coordenada normalizada [0,1] (centros de píxel em (i+0.5)/w)."""
    return np.array([[1.0 / w, 0, 0.5 / w], [0, 1.0 / h, 0.5 / h], [0, 0, 1]], np.float64)


def to_normalized(W: np.ndarray, ref_wh: tuple[int, int], src_wh: tuple[int, int]) -> tuple[float, ...]:
    """W (índices da referência -> índices da fonte) -> afim normalizada saída[0,1]^2 -> fonte[0,1]^2."""
    A = _norm(*src_wh) @ np.vstack([W, [0, 0, 1]]) @ np.linalg.inv(_norm(*ref_wh))
    return tuple(round(float(v), 6) for v in A[:2].reshape(-1))


def _pixel_warp(A: tuple[float, ...], ref_wh: tuple[int, int], src_wh: tuple[int, int]) -> np.ndarray:
    """Inverso de to_normalized: afim normalizada -> índices de píxel (referência -> fonte)."""
    M = np.linalg.inv(_norm(*src_wh)) @ np.vstack([np.array(A, np.float64).reshape(2, 3), [0, 0, 1]]) @ _norm(*ref_wh)
    return M[:2].astype(np.float32)


# --------------------------------------------------------------------------- índice
@dataclass
class _ClipIdx:
    path: str
    fps: float
    n_frames: int
    size: tuple[int, int]  # (w, h) de trabalho (SRC_W)
    kf_t: list[float] = field(default_factory=list)
    kf_xy: list[np.ndarray] = field(default_factory=list)
    kf_desc: list[np.ndarray] = field(default_factory=list)


class SourceIndex:
    """Keyframes de todos os clips com descritores SIFT, pesquisáveis por vizinhos (FLANN)."""

    def __init__(self, paths: list[str], rate: float | None = None):
        self.sift = cv2.SIFT_create(nfeatures=400)
        infos = [probe(p) for p in paths]
        total = sum(max(i.duration, 0.1) for i in infos)
        self.rate = rate or min(KF_RATE, KF_BUDGET / total)
        self.clips: list[_ClipIdx] = []
        descs, owner = [], []
        for ci, (p, info) in enumerate(zip(paths, infos)):
            fps = native_fps(info)
            c = _ClipIdx(p, fps, max(1, int(round(info.duration * fps))), (SRC_W, _even(SRC_W / info.aspect)))
            for fi, g in enumerate(iter_range(p, SRC_W, self.rate, rgb=False)):
                kp, des = self.sift.detectAndCompute(g, None)
                xy = np.array([k.pt for k in kp], np.float32).reshape(-1, 2)
                des = des if des is not None else np.zeros((0, 128), np.float32)
                c.kf_t.append(fi / self.rate)
                c.kf_xy.append(xy)
                c.kf_desc.append(des)
                if len(des):
                    descs.append(des)
                    owner.append(np.column_stack([np.full(len(des), ci), np.full(len(des), fi)]))
            self.clips.append(c)
        self.desc = np.vstack(descs).astype(np.float32) if descs else np.zeros((0, 128), np.float32)
        self.owner = np.vstack(owner) if owner else np.zeros((0, 2), int)
        self.flann = cv2.flann_Index(self.desc, dict(algorithm=1, trees=4)) if len(self.desc) else None

    def candidates(self, des: np.ndarray, top: int = 5, clip: int | None = None) -> list[tuple[int, int, int]]:
        """(clip, keyframe, votos) mais votados. Cada descritor vota em todos os keyframes cujos
        vizinhos estão quase tão perto como o melhor (keyframes consecutivos são parecidos —
        um teste de rácio clássico apagaria precisamente os bons)."""
        if self.flann is None or des is None or not len(des):
            return []
        k = min(8, len(self.desc))
        idx, dist = self.flann.knnSearch(des.astype(np.float32), k, params={})
        votes: dict[tuple[int, int], int] = {}
        for row_i, row_d in zip(idx, dist):
            best = max(float(row_d[0]), 1e-6)
            seen = set()
            for i_, d_ in zip(row_i, row_d):
                if d_ > best * 1.6:  # distâncias L2 ao quadrado: ~1.26x em distância
                    break
                key = (int(self.owner[i_, 0]), int(self.owner[i_, 1]))
                if (clip is None or key[0] == clip) and key not in seen:
                    seen.add(key)
                    votes[key] = votes.get(key, 0) + 1
        return [(c, f, v) for (c, f), v in sorted(votes.items(), key=lambda kv: -kv[1])[:top]]

    def verify(self, xy: np.ndarray, des: np.ndarray, ci: int, fi: int) -> tuple[np.ndarray | None, int]:
        """Semelhança RANSAC referência -> keyframe (índices de píxel), e nº de inliers."""
        c = self.clips[ci]
        d2 = c.kf_desc[fi]
        if des is None or len(des) < 4 or len(d2) < 4:
            return None, 0
        m = cv2.BFMatcher(cv2.NORM_L2).knnMatch(des, d2, k=2)
        good = [a for a, *b in m if b and a.distance < 0.8 * b[0].distance]
        if len(good) < 6:
            return None, 0
        p_r = np.float32([xy[g.queryIdx] for g in good])
        p_s = np.float32([c.kf_xy[fi][g.trainIdx] for g in good])
        M, inl = cv2.estimateAffinePartial2D(p_r, p_s, method=cv2.RANSAC, ransacReprojThreshold=3.0, maxIters=3000)
        if M is None or inl is None:
            return None, 0
        return M.astype(np.float32), int(inl.sum())


# --------------------------------------------------------------------------- registo
def _ncc_warped(tpl: np.ndarray, wr: np.ndarray, mask: np.ndarray) -> float:
    if cv2.countNonZero(mask) < 0.5 * mask.size:
        return -1.0
    ma, sa = cv2.meanStdDev(tpl, mask=mask)
    mb, sb = cv2.meanStdDev(wr, mask=mask)
    den = float(sa[0, 0]) * float(sb[0, 0])
    if den <= 1e-6:
        return -1.0
    prod = cv2.multiply(tpl - float(ma[0, 0]), wr - float(mb[0, 0]))
    return float(cv2.mean(prod, mask=mask)[0] / den)


def _warp(src: np.ndarray, W: np.ndarray, wh: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    wr = cv2.warpAffine(src, W, wh, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_CONSTANT, borderValue=-1)
    return wr, (wr >= 0).astype(np.uint8)


class _Frame:
    """Frame da referência pronto a registar: cinzento (uint8; float sob pedido) e cantos para o
    fluxo óptico."""

    __slots__ = ("u8", "pts", "_g", "stats")

    def __init__(self, rgb: np.ndarray):
        self.u8 = _gray(rgb)
        self._g = None
        m, sd = cv2.meanStdDev(self.u8)
        self.stats = (float(m[0, 0]), float(sd[0, 0]))
        p = cv2.goodFeaturesToTrack(self.u8, maxCorners=300, qualityLevel=0.005, minDistance=6, blockSize=5)
        self.pts = p.reshape(-1, 1, 2).astype(np.float32) if p is not None else np.zeros((0, 1, 2), np.float32)

    @property
    def g(self) -> np.ndarray:
        if self._g is None:
            self._g = self.u8.astype(np.float32)
        return self._g


def _resid(g: np.ndarray, wr: np.ndarray, mask: np.ndarray) -> float:
    """Resíduo LOCAL depois do registo: percentil 98 da diferença absoluta (intensidades
    normalizadas). O registo global compensa o deslize da câmara entre frames vizinhos, por isso
    a correlação global quase não distingue o instante certo; o que distingue é o que se move por
    conta própria (pessoas, objetos) — e isso aparece como uma mancha de resíduo alto."""
    if cv2.countNonZero(mask) < 0.5 * mask.size:
        return 1e9
    mt, st_ = cv2.meanStdDev(g, mask=mask)
    mw, sw = cv2.meanStdDev(wr, mask=mask)
    # a meia resolução (pyrDown já suaviza): a mancha de um objeto que se move continua bem visível
    a = cv2.pyrDown((g - float(mt[0, 0])) / (float(st_[0, 0]) + 1e-3))
    b = cv2.pyrDown((wr - float(mw[0, 0])) / (float(sw[0, 0]) + 1e-3))
    m2 = cv2.erode(cv2.resize(mask, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_NEAREST), np.ones((3, 3), np.uint8))
    d = np.abs(a - b)[m2 > 0]
    if d.size < 100:
        return 1e9
    k = int(0.98 * (d.size - 1))
    return float(np.partition(d, k)[k])


def _refine(fr: _Frame, src: np.ndarray, W: np.ndarray, iters: int = 2) -> tuple[np.ndarray, float, float]:
    """Afina W (referência -> fonte) com Lucas-Kanade: deforma a fonte por W, segue os cantos da
    referência na imagem deformada e compõe a correção afim (RANSAC: o sujeito que se move por
    conta própria fica de fora). A fonte deformada é normalizada à média/desvio
    da referência antes do fluxo, o que o torna robusto a flash, dip e cor diferente.
    Devolve (W, NCC, resíduo local — ver _resid)."""
    h, w = fr.u8.shape
    g = fr.g
    W = W.astype(np.float64)
    for _ in range(iters):
        wr, mask = _warp(src, W.astype(np.float32), (w, h))
        cover = cv2.countNonZero(mask) / mask.size
        if cover < 0.4 or len(fr.pts) < 12:
            break
        # normalização de intensidade só para o fluxo: com a fonte a cobrir quase tudo, as
        # estatísticas do frame inteiro da referência servem (e poupam metade do custo)
        mt, st_ = fr.stats if cover > 0.95 else (float(v[0, 0]) for v in cv2.meanStdDev(g, mask=mask))
        mw, sw = cv2.meanStdDev(wr, mask=mask)
        gain = st_ / (float(sw[0, 0]) + 1e-3)
        wn = cv2.convertScaleAbs(wr, alpha=gain, beta=mt - float(mw[0, 0]) * gain)
        wn[mask == 0] = fr.u8[mask == 0]
        p1, ok, _ = cv2.calcOpticalFlowPyrLK(fr.u8, wn, fr.pts, None, winSize=(21, 21), maxLevel=3,
                                             criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
        ok = ok.reshape(-1).astype(bool)
        if ok.sum() < 12:
            break
        C, _ = cv2.estimateAffine2D(fr.pts[ok], p1[ok], method=cv2.RANSAC, ransacReprojThreshold=1.0, maxIters=500)
        if C is None:
            break
        W = (np.vstack([W, [0, 0, 1]]) @ np.vstack([C, [0, 0, 1]]))[:2]
        if np.abs(C[:, :2] - np.eye(2)).max() < 2e-3 and np.abs(C[:, 2]).max() < 0.03:
            break
    W = W.astype(np.float32)
    wr, mask = _warp(src, W, (w, h))
    return W, _ncc_warped(g, wr, mask), _resid(g, wr, mask)


@dataclass
class ShotAlign:
    shot: int
    start: int  # frames nativos da referência [start, end)
    end: int
    clip: int | None = None
    confidence: float = 0.0
    src_frames: list[int] = field(default_factory=list)
    warps: list[tuple[float, ...]] = field(default_factory=list)  # normalizadas (ver to_normalized)
    measured: list[bool] = field(default_factory=list)
    corr: list[float | None] = field(default_factory=list)
    color: list[list[float]] | None = None
    speed: float | None = None  # velocidade média medida (1 = tempo real)
    notes: list[str] = field(default_factory=list)
    # False quando o plano NÃO começa onde começava o plano do DNA (`shot`): nasceu ou foi aparado
    # pela reanálise de segmento, por isso a transição de entrada do DNA não é dele
    inherit_transition: bool = True

    @property
    def ok(self) -> bool:
        return self.clip is not None and self.confidence >= SHOT_MIN_CONF and bool(self.src_frames)


@dataclass
class AlignResult:
    fps: float  # taxa nativa da referência
    n_frames: int
    size: tuple[int, int]  # resolução nativa da referência
    bounds: list[int]  # fronteiras dos planos em frames nativos
    shots: list[ShotAlign]
    clip_paths: list[str]
    clip_fps: list[float]
    seconds: float = 0.0
    timings: dict = field(default_factory=dict)
    # inícios (nativos) dos planos do DNA: um plano que começa num destes herda a transição do DNA;
    # os outros nasceram da reanálise de segmento
    dna_starts: list[int] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Fração da duração da referência reconstruída com confiança."""
        return sum(s.end - s.start for s in self.shots if s.ok) / max(self.n_frames, 1)


def _color_fit(ref_rgb: list[np.ndarray], src_rgb: list[np.ndarray], Ws: list[np.ndarray]) -> list[list[float]] | None:
    """Matriz 3x4 (fonte -> referência). `Ws` mapeiam píxeis da referência para píxeis de `src_rgb`."""
    xs, ys = [], []
    for r, s, W in zip(ref_rgb, src_rgb, Ws):
        h, w = r.shape[:2]
        wr = cv2.warpAffine(s, W, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        valid = cv2.warpAffine(np.full(s.shape[:2], 255, np.uint8), W, (w, h), flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP)
        valid = cv2.erode(valid, np.ones((5, 5), np.uint8)) > 0
        g = cv2.cvtColor(r, cv2.COLOR_RGB2GRAY).astype(np.float32)
        edge = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
        ok = valid & (edge < np.percentile(edge, 60)) & (r.min(2) > 3) & (r.max(2) < 252) & (wr.min(2) > 3) & (wr.max(2) < 252)
        ys.append(r[ok].astype(np.float64))
        xs.append(wr[ok].astype(np.float64))
    if not xs:
        return None
    X, Y = np.vstack(xs), np.vstack(ys)
    if len(X) < 500:
        return None
    if len(X) > 60000:
        sel = np.random.default_rng(0).choice(len(X), 60000, replace=False)
        X, Y = X[sel], Y[sel]
    Xa = np.column_stack([X, np.ones(len(X))])
    M = None
    keep = np.ones(len(X), bool)
    for _ in range(2):  # 2ª passagem sem os resíduos grandes (desalinhamento residual, compressão)
        M, *_ = np.linalg.lstsq(Xa[keep], Y[keep], rcond=None)
        res = np.abs(Xa @ M - Y).max(1)
        keep = res <= max(3 * float(np.median(res)), 4.0)
    return [[round(float(v), 5) for v in row] for row in M.T]  # 3 linhas x (r, g, b, offset)


# ------------------------------------------------------------------ janelas das fontes
class _SrcFrames:
    """Frames de UM clip (cinzento SRC_W + cor COLOR_W), só os pedidos, lidos numa passagem."""

    def __init__(self, c: _ClipIdx, ranges: list[tuple[int, int]]):
        self.c = c
        self.g: dict[int, np.ndarray] = {}
        self.rgb: dict[int, np.ndarray] = {}
        self._gf: dict[int, np.ndarray] = {}
        self.add(ranges)

    def add(self, ranges: list[tuple[int, int]]):
        rs = sorted((max(0, a), min(self.c.n_frames - 1, b)) for a, b in ranges if b >= 0 and a < self.c.n_frames)
        rs = [r for r in rs if r[0] <= r[1]]
        if not rs:
            return
        need = set()
        for a, b in rs:
            need.update(j for j in range(a, b + 1) if j not in self.g)
        if not need:
            return
        lo, hi = min(need), max(need)
        cw, ch = self.c.size
        for j, f in enumerate(iter_range(self.c.path, SRC_W, self.c.fps, lo, hi - lo + 1, rgb=True, height=ch), start=lo):
            if j in need:
                self.g[j] = _gray(f)
                self.rgb[j] = cv2.resize(f, (COLOR_W, _even(COLOR_W * ch / cw)), interpolation=cv2.INTER_AREA)

    def gray_f(self, j: int) -> np.ndarray | None:
        if j not in self.g:
            return None
        if j not in self._gf:
            self._gf[j] = self.g[j].astype(np.float32)
        return self._gf[j]

    def __contains__(self, j: int) -> bool:
        return j in self.g


@dataclass
class _Hit:
    clip: int
    picks: list[int]  # frames (locais ao plano) das amostras
    cands: dict  # amostra -> [(keyframe, semelhança RANSAC referência -> keyframe), ...]


MAX_KF_CANDS = 6  # keyframes candidatos por amostra (cenas que se repetem no tempo — ver _retrieve)


def _retrieve(rgb: np.ndarray, measurable: np.ndarray, idx: SourceIndex) -> tuple[_Hit | None, str, dict]:
    """Escolhe o clip e, para cada amostra, VÁRIOS keyframes candidatos. Num plano de câmara fixa
    (ou num cenário que a câmara percorre devagar) o mesmo fundo aparece em muitos instantes e o
    SIFT verifica todos — o instante certo decide-se depois, pelo resíduo local (_anchors).
    Devolve (hit, nota, {amostra: _Frame})."""
    n = len(rgb)
    stable = [k for k in range(n) if measurable[k]] or list(range(n))
    picks = sorted({stable[int(q * (len(stable) - 1))] for q in (0.15, 0.5, 0.85)})
    frs = {k: _Frame(rgb[k]) for k in picks}
    feats = {}
    for k in picks:
        kp, des = idx.sift.detectAndCompute(frs[k].u8, None)
        feats[k] = (np.array([p.pt for p in kp], np.float32).reshape(-1, 2), des)

    def verified(k: int, clip: int | None, top: int) -> list[tuple[int, int, np.ndarray, int]]:
        xy, des = feats[k]
        out = []
        for ci, fi, _ in idx.candidates(des, top=top, clip=clip):
            M, inl = idx.verify(xy, des, ci, fi)
            if M is not None and inl >= MIN_INLIERS:
                out.append((ci, fi, M, inl))
        return sorted(out, key=lambda r: -r[3])

    first = {k: verified(k, None, 5) for k in picks}
    score: dict[int, int] = {}
    for rows in first.values():
        if rows:
            score[rows[0][0]] = score.get(rows[0][0], 0) + rows[0][3]
    if not score:
        return None, "sem correspondência verificada nos clips", frs
    ci = max(score, key=score.get)
    cands = {}
    for k in picks:
        rows = verified(k, ci, 12)
        if rows:
            top_inl = rows[0][3]
            cands[k] = [(fi, M) for _, fi, M, inl in rows if inl >= max(MIN_INLIERS, 0.4 * top_inl)][:MAX_KF_CANDS]
    picks = [k for k in picks if cands.get(k)]
    if not picks:
        return None, "amostras inconsistentes entre clips", frs
    return _Hit(ci, picks, cands), "", frs


PROP_RAD = 3  # frames testados à volta de cada hipótese propagada


def _anchor_ranges(hit: _Hit, idx: SourceIndex, ref_fps: float) -> list[tuple[int, int]]:
    """Janelas da fonte à volta de cada keyframe candidato — e as mesmas janelas deslocadas para
    as OUTRAS amostras (hipótese de velocidade 1, ver _anchors)."""
    c = idx.clips[hit.clip]
    R = int(math.ceil(c.fps / idx.rate)) + 1
    nominal = c.fps / ref_fps
    out = []
    for k in hit.picks:
        for fi, _ in hit.cands[k]:
            jc = int(round(c.kf_t[fi] * c.fps))
            for k2 in hit.picks:
                off = int(round((k2 - k) * nominal))
                out.append((jc + off - R - PROP_RAD, jc + off + R + PROP_RAD))
    return out


def _badness(rows: list[tuple[float, int, np.ndarray, float]]) -> dict[int, float]:
    """Quão mau é cada candidato (rs, j, W, ncc), relativo ao melhor do conjunto: média do resíduo
    local (decide em cenas rígidas, onde a correlação quase não muda) e da falta de correlação
    (decide quando o registo é bom, onde o percentil do resíduo é ruidoso). 1.0 ~ o melhor."""
    if not rows:
        return {}
    rs0 = min(r[0] for r in rows) + 1e-3
    cc0 = max(r[3] for r in rows)
    return {r[1]: 0.5 * r[0] / rs0 + 0.5 * (1.0 - r[3] + 0.005) / (1.0 - cc0 + 0.005) for r in rows}


def _local_minima(rows: list[tuple[float, int, np.ndarray, float]], keep: int = 4, sep: int = 3) -> list:
    """Os `keep` melhores (_badness) separados por >= sep frames da fonte."""
    out = []
    bad = _badness(rows)
    for r in sorted(rows, key=lambda r: bad[r[1]]):
        if all(abs(r[1] - o[1]) >= sep for o in out):
            out.append(r)
        if len(out) >= keep:
            break
    return out


def _anchors(frs: dict[int, _Frame], hit: _Hit, idx: SourceIndex, src: _SrcFrames,
             ref_fps: float) -> list[tuple[int, int, np.ndarray, float]]:
    """Instante exato de cada amostra. Para cada keyframe candidato, testa os frames à volta (de 2
    em 2) e guarda os mínimos locais do resíduo; depois escolhe os instantes das amostras EM
    CONJUNTO: o menor resíduo total entre as combinações temporalmente coerentes (velocidade
    positiva e parecida entre amostras). Um instante isolado pode enganar (fundo igual noutro
    momento, movimento repetitivo); três instantes coerentes entre si quase nunca enganam."""
    c = idx.clips[hit.clip]
    R = int(math.ceil(c.fps / idx.rate)) + 1
    per: dict[int, list] = {}
    for k in hit.picks:
        rows, seen = [], set()
        for fi, M in hit.cands[k]:
            jc = int(round(c.kf_t[fi] * c.fps))
            # varrimento a partir do keyframe para os dois lados, cada candidato arrancando da
            # geometria já afinada do vizinho (entre frames próximos a câmara mexe pouco): converge
            # em 1-2 iterações em vez de recomeçar sempre da semelhança RANSAC do keyframe
            for side in (range(jc, jc + R + 1, 2), range(jc - 2, jc - R - 1, -2)):
                W_warm, warm_ok = M, False
                for j in side:
                    g = src.gray_f(j) if j not in seen else None
                    seen.add(j)
                    if g is None:
                        continue
                    W, cc, rs = _refine(frs[k], g, W_warm, iters=1 if warm_ok else 3)
                    if cc >= NCC_MIN:
                        rows.append((rs, j, W, cc))
                        W_warm, warm_ok = W, True
        per[k] = rows
    # propagação: as melhores hipóteses de cada amostra dão um palpite para as outras (velocidade 1);
    # basta UMA amostra ter acertado no keyframe para as três ficarem certas
    nominal = c.fps / ref_fps
    seeds = {k: _local_minima(per.get(k, []), keep=3) for k in hit.picks}
    for k, mins_k in seeds.items():
        for rs0, j0, W0, cc0 in mins_k:
            for k2 in hit.picks:
                if k2 == k:
                    continue
                jp = int(round(j0 + (k2 - k) * nominal))
                tested = {r[1] for r in per.get(k2, [])}
                for j in range(jp - PROP_RAD, jp + PROP_RAD + 1):
                    g = src.gray_f(j) if j not in tested else None
                    if g is not None:
                        W, cc, rs = _refine(frs[k2], g, W0, iters=3)
                        if cc >= NCC_MIN:
                            per.setdefault(k2, []).append((rs, j, W, cc))
    for k in list(per):
        mins = _local_minima(per[k])
        refined = []
        for rs0, j0, W0, cc0 in mins:  # +-1 à volta de cada mínimo (a grelha é de 2 em 2)
            W0, cc0, rs0 = _refine(frs[k], src.gray_f(j0), W0, iters=2)
            best = (rs0, j0, W0, cc0)
            trio = [best]
            for j in (j0 - 1, j0 + 1):
                g = src.gray_f(j)
                if g is not None:
                    W, cc, rs = _refine(frs[k], g, W0, iters=2)
                    if cc >= NCC_MIN:
                        trio.append((rs, j, W, cc))
            bt = _badness(trio)
            refined.append(min(trio, key=lambda r: bt[r[1]]))
        per[k] = refined
    per = {k: v for k, v in per.items() if v}
    ks = sorted(per)
    if not ks:
        return []
    if len(ks) == 1:
        r = min(per[ks[0]], key=lambda r: r[0])
        return [(ks[0], r[1], r[2], r[3])]
    nominal = c.fps / ref_fps  # frames da fonte por frame da referência a velocidade 1
    best_combo, best_cost = None, float("inf")
    bad = {k: _badness(per[k]) for k in ks}
    for combo in itertools.product(*(per[k] for k in ks)):
        cost = sum(bad[k][r[1]] for k, r in zip(ks, combo)) / len(ks)  # 1.0 = cada amostra no seu melhor
        speed = (combo[-1][1] - combo[0][1]) / ((ks[-1] - ks[0]) * nominal)
        if speed <= 0.05:
            cost += 3.0  # parado ou para trás: possível (freeze, reverse), mas raro — precisa de muita evidência
        if speed > 8:
            cost += 3.0
        for k, r in zip(ks[1:-1], combo[1:-1]):  # amostras do meio: perto da linha das pontas
            line = combo[0][1] + (combo[-1][1] - combo[0][1]) * (k - ks[0]) / (ks[-1] - ks[0])
            cost += 0.5 * max(0.0, abs(r[1] - line) - 1.0)  # +-1 frame: quantização (câmara lenta repete frames)
        if cost < best_cost:
            best_combo, best_cost = combo, cost
    return [(k, r[1], r[2], r[3]) for k, r in zip(ks, best_combo)]


def _track_range(anchors, n: int, c: _ClipIdx, ref_fps: float) -> tuple[int, int, float]:
    if len(anchors) >= 2:
        step0 = float(np.polyfit([a[0] for a in anchors], [a[1] for a in anchors], 1)[0])
    else:
        step0 = c.fps / ref_fps
    step0 = float(np.clip(step0, -4.0, 6.0))
    k0, j0 = anchors[0][0], anchors[0][1]
    ends = [j0 + step0 * (0 - k0), j0 + step0 * (n - 1 - k0)]
    pad = 6 + int(0.35 * abs(ends[1] - ends[0]))
    return int(min(ends)) - pad, int(math.ceil(max(ends))) + pad, step0


def _track(si: int, start: int, end: int, ref_rgb: np.ndarray, frs: list[_Frame], measurable: np.ndarray,
           geo_skip: np.ndarray, hit: _Hit, anchors, step0: float, idx: SourceIndex, src: _SrcFrames, ref_fps: float) -> ShotAlign:
    """Segue o plano frame a frame a partir da melhor amostra. Em cada frame: prevê o instante da
    fonte pela linha que passa pelas amostras (speed ramps por troços), afina a geometria nesse
    frame e compara com os vizinhos (+-1; +-2 quando a velocidade é alta), cada um com a SUA
    geometria afinada (escolhe o menor resíduo local) — o
    deslocamento da câmara entre frames vizinhos não enviesa a escolha do instante.
    `geo_skip`: frames deformados pela própria transição (blur/glitch) — aí a geometria seria a do
    efeito, não a da câmara, e é interpolada dos vizinhos. Flash e dip mudam só a intensidade (o
    registo normaliza-a), por isso esses frames SÃO medidos."""
    out = ShotAlign(si, start, end, clip=hit.clip)
    c = idx.clips[hit.clip]
    n = len(frs)
    J = np.full(n, -10 ** 9, int)
    Wk: list[np.ndarray | None] = [None] * n
    corr = np.full(n, np.nan)
    a0 = max(anchors, key=lambda a: a[3])
    J[a0[0]], Wk[a0[0]], corr[a0[0]] = a0[1], a0[2], a0[3]

    # previsão do instante: a linha (por troços) que passa pelas amostras — fiáveis, escolhidas em
    # conjunto. A tendência dos frames recentes deriva em câmara lenta (frames repetidos têm
    # resíduos quase iguais) e um erro de +1 repetido acelerava a previsão sem limite.
    ak = np.array(sorted(a[0] for a in anchors), float)
    aj = np.array([a[1] for a in sorted(anchors, key=lambda a: a[0])], float)

    def predict(k: int) -> tuple[float, float]:
        if len(ak) == 1:
            return aj[0] + step0 * (k - ak[0]), step0
        i = int(np.clip(np.searchsorted(ak, k) - 1, 0, len(ak) - 2))
        slope = (aj[i + 1] - aj[i]) / max(ak[i + 1] - ak[i], 1e-6)
        return aj[i] + slope * (k - ak[i]), slope

    def run(direction: int):
        W_prev = a0[2]
        for k in (range(a0[0] + 1, n) if direction > 0 else range(a0[0] - 1, -1, -1)):
            pred, step = predict(k)
            jp = int(round(pred))
            g = src.gray_f(jp)
            if geo_skip[k] or g is None:
                continue
            W, cc, rs = _refine(frs[k], g, W_prev, iters=2)
            cands = [(rs, jp, W, cc)] if cc >= NCC_MIN else []
            rad = 2 if abs(step) > 1.2 else 1
            for j in range(jp - rad, jp + rad + 1):
                g2 = src.gray_f(j) if j != jp else None
                if g2 is not None:
                    W2, cc2, rs2 = _refine(frs[k], g2, W, iters=2)
                    if cc2 >= NCC_MIN:
                        cands.append((rs2, j, W2, cc2))
            if cands:
                bd = _badness(cands)
                best = min(cands, key=lambda r: bd[r[1]])
                J[k], Wk[k], corr[k] = best[1], best[2], best[3]
                W_prev = best[2]

    run(+1)
    run(-1)
    meas = ~np.isnan(corr)
    if meas.sum() < max(3, 0.4 * measurable.sum()):
        out.clip = None
        out.notes.append(f"poucos frames registados ({int(meas.sum())}/{n})")
        return out

    ks = np.flatnonzero(meas)
    P = np.array([Wk[k].reshape(-1) for k in ks])
    allk = np.arange(n)

    def fill(vals: np.ndarray) -> np.ndarray:
        """Interpola entre medidos; extrapola linearmente (4 vizinhos) nas pontas."""
        y = np.interp(allk, ks, vals)
        for sel, edge in ((ks[:4], allk < ks[0]), (ks[-4:], allk > ks[-1])):
            if len(sel) >= 2 and edge.any():
                a, b = np.polyfit(sel, vals[np.searchsorted(ks, sel)], 1)
                y[edge] = a * allk[edge] + b
        return y

    Wf = np.column_stack([fill(P[:, i]) for i in range(6)])
    Jf = np.rint(fill(J[ks].astype(float))).astype(int)
    out.src_frames = [int(np.clip(j, 0, c.n_frames - 1)) for j in Jf]
    out.warps = [to_normalized(Wf[k].reshape(2, 3), (frs[0].g.shape[1], frs[0].g.shape[0]), c.size) for k in range(n)]
    out.measured = [bool(m) for m in meas]
    out.corr = [round(float(v), 4) if not np.isnan(v) else None for v in corr]
    out.confidence = round(float(np.nanmedian(corr)), 4)
    if len(ks) >= 2:
        out.speed = round(float(np.polyfit(ks, J[ks], 1)[0]) * ref_fps / c.fps, 4)
    # cor: até 10 frames medidos e fora de transições, espalhados pelo plano
    kc = np.array([k for k in ks if measurable[k]]) if any(measurable[k] for k in ks) else ks
    sel = [k for k in kc[np.linspace(0, len(kc) - 1, min(10, len(kc))).astype(int)] if J[k] in src.rgb]
    if sel:
        cs = np.diag([COLOR_W / c.size[0], src.rgb[J[sel[0]]].shape[0] / c.size[1], 1.0])
        out.color = _color_fit([ref_rgb[k] for k in sel], [src.rgb[J[k]] for k in sel],
                               [(cs @ np.vstack([Wk[k], [0, 0, 1]]))[:2].astype(np.float32) for k in sel])
    return out


# ---------------------------------------------------------------------- cortes
def refine_cuts(ref_path: str, fps: float, cut_times: list[float], hard: list[bool]) -> tuple[list[int], int]:
    """Cortes em frames NATIVOS. Cortes secos: o frame de maior diferença a +-2 do estimado
    (o analisador trabalha a 30 fps; numa referência a 24/25/60 fps o arredondamento erra 1 frame)."""
    g = decode(ref_path, 96, fps, rgb=False).astype(np.float32)
    n = len(g)
    d = np.zeros(n)
    if n > 1:
        d[1:] = np.abs(np.diff(g, axis=0)).mean(axis=(1, 2))
    cuts, last = [], 0
    for t, is_hard in zip(cut_times, hard):
        f = int(round(t * fps))
        if is_hard:
            lo_, hi_ = max(last + 1, f - 2), min(n - 1, f + 2)
            if lo_ <= hi_:
                f = lo_ + int(np.argmax(d[lo_:hi_ + 1]))
        f = int(np.clip(f, last + 1, n - 1))
        cuts.append(f)
        last = f
    return cuts, n


def _predict(sa: ShotAlign, f: int, ref_fps: float, clip_fps: float) -> tuple[int, tuple[float, ...]]:
    """(frame da fonte, afim normalizada) previstos para o frame f da referência, fora do plano
    (extrapolação linear dos 4 frames mais próximos da ponta)."""
    n = len(sa.src_frames)
    k = f - sa.start
    after = k >= n
    side = sa.src_frames[-4:] if after else sa.src_frames[:4]
    kk = np.arange(n - len(side), n) if after else np.arange(len(side))
    wsel = np.array(sa.warps[-4:] if after else sa.warps[:4], np.float64)
    if len(kk) >= 2:
        j = float(np.polyval(np.polyfit(kk, side, 1), k))
        A = tuple(float(np.polyval(np.polyfit(kk, wsel[:, i], 1), k)) for i in range(6))
    else:
        j = side[0] + (sa.speed or 1.0) * clip_fps / ref_fps * (k - kk[0])
        A = tuple(wsel[0])
    return int(round(j)), A


def _rebound(sa: ShotAlign, new_start: int, new_end: int, ref_fps: float, clip_fps: float, n_src: int):
    """Muda as fronteiras do plano, prevendo os frames acrescentados (marcados como não medidos)."""
    pre = [_predict(sa, f, ref_fps, clip_fps) for f in range(new_start, sa.start)]
    post = [_predict(sa, f, ref_fps, clip_fps) for f in range(sa.end, new_end)]
    a, b = max(0, new_start - sa.start), len(sa.src_frames) - max(0, sa.end - new_end)
    clipj = lambda j: int(np.clip(j, 0, n_src - 1))  # noqa: E731
    sa.src_frames = [clipj(j) for j, _ in pre] + sa.src_frames[a:b] + [clipj(j) for j, _ in post]
    sa.warps = [A for _, A in pre] + sa.warps[a:b] + [A for _, A in post]
    sa.measured = [False] * len(pre) + sa.measured[a:b] + [False] * len(post)
    sa.corr = [None] * len(pre) + sa.corr[a:b] + [None] * len(post)
    sa.start, sa.end = new_start, new_end


def refine_soft_cuts(res: AlignResult, ref_frs: dict[int, _Frame], soft: list[bool], idx: SourceIndex,
                     srcs: dict[int, _SrcFrames]) -> list[str]:
    """Em transições suaves (flash, dip...), o analisador marca o corte no meio do efeito e pode
    falhar 1-2 frames. Com os dois planos vizinhos alinhados, o corte certo vê-se pelo CONTEÚDO:
    o primeiro frame que correlaciona melhor com a previsão do plano seguinte do que com a do
    anterior (a correlação ignora o branco/preto da transição)."""
    notes = []
    for bi, is_soft in enumerate(soft):
        A_, B_ = res.shots[bi], res.shots[bi + 1]
        if not (is_soft and A_.ok and B_.ok):
            continue
        b = res.bounds[bi + 1]
        lo, hi = max(A_.start + 2, b - 2), min(B_.end - 2, b + 3)
        if hi <= lo:
            continue
        wins = []
        for f in range(lo, hi):
            fr = ref_frs.get(f)
            if fr is None:
                wins.append(sum(wins) > len(wins) / 2)
                continue
            ref_wh = (fr.g.shape[1], fr.g.shape[0])
            sc = []
            for sa in (A_, B_):
                c = idx.clips[sa.clip]
                j, A = (sa.src_frames[f - sa.start], sa.warps[f - sa.start]) if sa.start <= f < sa.end else _predict(sa, f, res.fps, c.fps)
                g = srcs[sa.clip].gray_f(int(np.clip(j, 0, c.n_frames - 1)))
                if g is None:
                    sc.append(-1.0)
                    continue
                wr, mask = _warp(g, _pixel_warp(A, ref_wh, c.size), ref_wh)
                sc.append(-_resid(fr.g, wr, mask))
            wins.append(sc[1] > sc[0])
        new_b = next((f for i_, f in enumerate(range(lo, hi)) if all(wins[i_:])), b)
        if new_b != b:
            ca, cb = idx.clips[A_.clip], idx.clips[B_.clip]
            _rebound(A_, A_.start, new_b, res.fps, ca.fps, ca.n_frames)
            _rebound(B_, new_b, B_.end, res.fps, cb.fps, cb.n_frames)
            res.bounds[bi + 1] = new_b
            notes.append(f"corte {bi + 1}: {b} -> {new_b} (pelo conteúdo)")
    return notes


# ---------------------------------------------------------------------- orquestração
PROBE_RATE = 1.5  # keyframes/s do índice da sonda (só decide SE o material é o da referência)


def align_reference(ref_path: str, dna, clip_paths: list[str], log=lambda m: None, probe_first: bool = True) -> AlignResult | None:
    """Alinha todos os planos do DNA às fontes.

    `probe_first`: antes de tudo, um índice leve e os 3 planos mais longos — se nenhum tiver
    correspondência, desiste cedo (cenário B: material diferente) sem pagar o resto.
    Depois do alinhamento, os planos cujo seguimento perde o conteúdo numa das pontas (o
    analisador não viu um corte suave, p.ex. um cross-fade) são divididos e a parte perdida é
    alinhada como plano próprio (reanálise de segmento)."""
    t0 = time.time()
    tm: dict[str, float] = {}
    info = probe(ref_path)
    fps = native_fps(info)
    trans = [s_.transition_in for s_ in dna.shots[1:]]
    n_est = int(round(info.duration * fps))
    rh = _even(REF_W / info.aspect)

    if probe_first and dna.shots:
        pidx = SourceIndex(clip_paths, rate=PROBE_RATE)
        longest = sorted(dna.shots, key=lambda sh: sh.start_frame - sh.end_frame)[:3]
        found = False
        for sh in longest:
            a_, b_ = int(round(sh.start_frame / dna.fps * fps)), int(round(sh.end_frame / dna.fps * fps))
            rgb = decode(ref_path, REF_W, fps, a_, max(1, b_ - a_), rgb=True, height=rh)
            if len(rgb) and _retrieve(rgb, np.ones(len(rgb), bool), pidx)[0] is not None:
                found = True
                break
        tm["probe"] = round(time.time() - t0, 2)
        if not found:
            log(f"alinhamento: os planos mais longos não têm correspondência nos clips — material diferente "
                f"(cenário B); sonda {tm['probe']:.1f}s")
            return None

    cuts, n = refine_cuts(ref_path, fps, [t.cut_frame / dna.fps for t in trans], [t.type == "cut" for t in trans])
    bounds = [0] + cuts + [n]
    measurable = np.ones(n, bool)
    geo_skip = np.zeros(n, bool)
    for t in trans:
        if t.type != "cut":
            a, b = int(math.floor(t.start_frame / dna.fps * fps)), int(math.ceil(t.end_frame / dna.fps * fps))
            measurable[max(0, a):min(n, b)] = False
            if t.type in ("blur", "glitch"):
                geo_skip[max(0, a):min(n, b)] = True
    for ev in getattr(dna, "fx_events", []) or []:
        measurable[max(0, int(ev.start_frame / dna.fps * fps)):min(n, int(math.ceil(ev.end_frame / dna.fps * fps)))] = False
    t1 = time.time()
    idx = SourceIndex(clip_paths)
    tm["index"] = round(time.time() - t1, 2)
    log(f"alinhamento: índice com {sum(len(c.kf_t) for c in idx.clips)} keyframes ({idx.rate:.1f}/s) em {tm['index']:.1f}s")

    # referência à taxa nativa (inteira se couber em memória; senão segmento a segmento)
    whole = n_est * REF_W * rh * 3 <= REF_MEM_MB * 1024 * 1024
    ref_all = decode(ref_path, REF_W, fps, rgb=True, height=rh) if whole else None
    if ref_all is not None and len(ref_all) < n:  # o ficheiro pode ter menos frames do que o contado
        n = len(ref_all)
        bounds[-1] = n

    def ref_seg(a: int, b: int) -> np.ndarray:
        return ref_all[a:b] if ref_all is not None else decode(ref_path, REF_W, fps, a, b - a, rgb=True, height=rh)

    srcs: dict[int, _SrcFrames] = {}
    ref_frs: dict[int, _Frame] = {}  # frames junto às fronteiras (para os cortes suaves)

    def align_segments(segs: list[tuple[int, int, int]], ex: ThreadPoolExecutor) -> list[ShotAlign]:
        """segs: (início, fim, plano do DNA de origem) em frames nativos."""
        hits, pick_frs, notes = {}, {}, {}

        def retrieve(i: int):
            a, b, _ = segs[i]
            rgb = ref_seg(a, b)
            hits[i], notes[i], pick_frs[i] = _retrieve(rgb, measurable[a:a + len(rgb)], idx)

        list(ex.map(retrieve, range(len(segs))))
        by_clip: dict[int, list] = {}

        def add(ci: int):
            if ci in srcs:
                srcs[ci].add(by_clip[ci])
            else:
                srcs[ci] = _SrcFrames(idx.clips[ci], by_clip[ci])

        # 1ª passagem pelas fontes: janelas à volta dos keyframes candidatos (e propagadas)
        for i, h in hits.items():
            if h is not None:
                by_clip.setdefault(h.clip, []).extend(_anchor_ranges(h, idx, fps))
        list(ex.map(add, list(by_clip)))
        anchors = dict(zip(hits, ex.map(lambda i: _anchors(pick_frs[i], hits[i], idx, srcs[hits[i].clip], fps) if hits[i] else [],
                                        list(hits))))
        # 2ª passagem: a janela que cobre cada segmento inteiro (prevista pelas amostras)
        ranges: dict[int, tuple[int, int, float]] = {}
        by_clip = {}
        for i, an in anchors.items():
            if an:
                ranges[i] = _track_range(an, segs[i][1] - segs[i][0], idx.clips[hits[i].clip], fps)
                by_clip.setdefault(hits[i].clip, []).append(ranges[i][:2])
        list(ex.map(add, list(by_clip)))

        def track(i: int) -> ShotAlign:
            a, b, origin = segs[i]
            if not anchors.get(i):
                sa = ShotAlign(origin, a, b)
                sa.notes.append(notes.get(i) or "registo fino falhou")
                return sa
            rgb = ref_seg(a, b)
            b = a + len(rgb)
            frs = [pick_frs[i].get(k) or _Frame(f) for k, f in enumerate(rgb)]
            for k in list(range(min(3, len(frs)))) + list(range(max(0, len(frs) - 3), len(frs))):
                ref_frs[a + k] = frs[k]
            h = hits[i]
            return _track(origin, a, b, rgb, frs, measurable[a:b], geo_skip[a:b], h, anchors[i], ranges[i][2], idx,
                          srcs[h.clip], fps)

        return list(ex.map(track, range(len(segs))))

    with ThreadPoolExecutor(max_workers=ALIGN_WORKERS) as ex:
        shots = align_segments([(bounds[i], bounds[i + 1], i) for i in range(len(bounds) - 1)], ex)
        tm["align"] = round(time.time() - t1 - tm["index"], 2)
        # reanálise de segmento: pontas longas que o seguimento não conseguiu medir
        extra, inherit = [], []
        for sa in shots:
            if not sa.ok:
                continue
            meas = np.flatnonzero(sa.measured)
            ln = sa.end - sa.start
            min_run = max(8, int(0.2 * ln))
            head, tail = int(meas[0]), ln - 1 - int(meas[-1])
            new_start, new_end = sa.start, sa.end
            if head >= min_run and measurable[sa.start:sa.start + head].mean() > 0.6:
                extra.append((sa.start, sa.start + head, sa.shot))
                inherit.append(sa.inherit_transition)  # a cabeça fica com a transição de entrada
                sa.inherit_transition = False
                new_start = sa.start + head
            if tail >= min_run and measurable[sa.end - tail:sa.end].mean() > 0.6:
                extra.append((sa.end - tail, sa.end, sa.shot))
                inherit.append(False)
                new_end = sa.end - tail
            if (new_start, new_end) != (sa.start, sa.end):
                c = idx.clips[sa.clip]
                _rebound(sa, new_start, new_end, fps, c.fps, c.n_frames)
        if extra:
            pieces = align_segments(extra, ex)
            for pc, inh in zip(pieces, inherit):
                pc.inherit_transition = inh
                pc.notes.append("segmento reanalisado: o analisador não marcou este corte (provável cross-fade)")
            shots = sorted(shots + pieces, key=lambda s_: s_.start)
            log(f"alinhamento: {len(extra)} segmento(s) reanalisado(s) — {sum(p.ok for p in pieces)} com conteúdo encontrado")
        tm["resegment"] = round(time.time() - t1 - tm["index"] - tm["align"], 2)
    dna_starts = list(bounds[:-1])
    bounds = [s_.start for s_ in shots] + [n]
    res = AlignResult(fps=fps, n_frames=n, size=(info.width, info.height), bounds=bounds, shots=shots,
                      clip_paths=list(clip_paths), clip_fps=[c.fps for c in idx.clips], dna_starts=dna_starts)
    soft = [not (b_ in dna_starts and trans[dna_starts.index(b_) - 1].type == "cut") for b_ in bounds[1:-1]]
    for note in refine_soft_cuts(res, ref_frs, soft, idx, srcs):
        log(f"alinhamento: {note}")
    res.seconds = round(time.time() - t0, 2)
    res.timings = tm
    log(f"alinhamento: {sum(s_.ok for s_ in res.shots)}/{len(res.shots)} planos reconstruíveis, "
        f"cobertura {res.coverage:.0%}, {res.seconds:.1f}s {tm}")
    return res
