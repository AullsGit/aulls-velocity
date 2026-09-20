"""
Biblioteca de VFX — filtros FFmpeg reais que dão um acabamento mais
"produzido" ao vídeo final. Não tentam detectar/copiar o VFX exato da
referência (isso exigiria um modelo visual muito mais sofisticado) —
são efeitos de acabamento aplicados de forma consistente, ao estilo dos
presets que editores usam manualmente (grão, aberração cromática leve,
etc.), configuráveis via config.py.
"""


def film_grain_filter(strength: float = 8.0) -> str:
    """Grão de filme sutil — dá textura, evita o visual 'digital limpo demais'."""
    return f"noise=alls={strength}:allf=t"


def chromatic_aberration_filter(shift_px: int = 2) -> str:
    """
    Aberração cromática leve (desalinha ligeiramente os canais R/B) —
    efeito comum em edições de estilo 'cinematic'/lifestyle.
    """
    return (
        f"split=3[r][g][b];"
        f"[r]lutrgb=g=0:b=0[r2];"
        f"[g]lutrgb=r=0:b=0[g2];"
        f"[b]lutrgb=r=0:g=0[b2];"
        f"[r2]crop=iw-{shift_px}:ih:0:0,pad=iw+{shift_px}:ih:{shift_px}:0[r3];"
        f"[b2]crop=iw-{shift_px}:ih:{shift_px}:0,pad=iw+{shift_px}:ih:0:0[b3];"
        f"[r3][g2]blend=all_mode=screen[rg];"
        f"[rg][b3]blend=all_mode=screen"
    )


def glitch_flash_filter(duration: float = 0.08) -> str:
    """
    'Flash' de glitch rápido — RGB shift + leve distorção, pensado para
    ser aplicado só nos primeiros frames de um corte (efeito de impacto).
    Usa-se tipicamente combinado com um `trim`/`overlay` externo; aqui
    devolve-se o filtro base de distorção de cor.
    """
    return "hue=s=0,eq=contrast=1.4:brightness=0.05"


def light_leak_overlay_filter(intensity: float = 0.15) -> str:
    """
    Aproximação simples de 'light leak' usando ajuste de brilho/saturação
    + vinheta invertida. 'intensity' (0-1) escala o efeito — quanto maior,
    mais forte o brilho quente e a saturação extra.
    Não é um asset de vídeo real de light leak (não temos acesso a stock
    footage), é uma aproximação sintética via filtros de cor.
    """
    brightness = 0.02 + intensity * 0.12
    saturation = 1.0 + intensity * 0.35
    return (
        f"eq=brightness={brightness:.4f}:saturation={saturation:.4f},"
        f"vignette=angle=PI/4:mode=backward:eval=init"
    )


AVAILABLE_VFX = {
    "film_grain": film_grain_filter,
    "chromatic_aberration": chromatic_aberration_filter,
    "light_leak": light_leak_overlay_filter,
}

# Intensidade por defeito de cada efeito, usada quando o preset não
# especifica um valor em VFX_STRENGTH.
DEFAULT_STRENGTH = {
    "film_grain": 8.0,
    "chromatic_aberration": 2,
    "light_leak": 0.15,
}


def build_vfx_chain(vfx_names: list, strengths: dict = None) -> str:
    """
    Recebe uma lista de nomes de VFX (chaves de AVAILABLE_VFX) e um dict
    opcional {nome: intensidade} — devolve a cadeia de filtros combinada,
    separada por vírgula, pronta a anexar a uma cadeia -vf existente.
    Efeitos que usam split/blend internamente (como chromatic_aberration)
    não são combináveis com outros na mesma chain simples — usa apenas 1
    desse tipo por clip.
    """
    strengths = strengths or {}
    simple_filters = []
    for name in vfx_names:
        if name not in AVAILABLE_VFX:
            continue
        if name == "chromatic_aberration":
            # Efeito complexo (split/blend) - não pode ser encadeado com
            # vírgula junto de filtros simples; deve ser usado sozinho.
            continue
        strength = strengths.get(name, DEFAULT_STRENGTH.get(name))
        simple_filters.append(AVAILABLE_VFX[name](strength))
    return ",".join(simple_filters)
