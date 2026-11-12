"""Legendas ASS palavra por palavra (estilo TikTok): palavra atual destacada, 3–4 palavras por vez."""
from __future__ import annotations

from pathlib import Path

from transcribe import Word

W, H = 1080, 1920


def _bgr(hex_rgb: str) -> str:
    """'#RRGGBB' -> '&HBBGGRR&' (ordem de cores do ASS)."""
    h = hex_rgb.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&"


def _ts(t: float) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _groups(words: list[Word], max_words: int, max_chars: int = 20) -> list[list[Word]]:
    """Agrupa palavras; quebra por limite de palavras/caracteres, fim de frase ou pausa."""
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or sum(len(x.text) + 1 for x in cur) + len(w.text) > max_chars
                    or w.start - cur[-1].end > 0.5 or cur[-1].text[-1:] in ".?!"):
            groups.append(cur)
            cur = []
        cur.append(w)
    return groups + [cur] if cur else groups


def build_ass(words: list[Word], style: dict) -> str:
    """Gera o conteúdo do .ass. `words` com tempos relativos ao início do clipe."""
    size, outline = style["size"], style["outline_width"]
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
        "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,"
        "MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Cap,{style['font']},{size},{_bgr(style['color'])},&H000000FF&,{_bgr(style['outline'])},"
        f"&H64000000&,{-1 if style.get('bold', True) else 0},0,0,0,100,100,0,0,1,{outline},2,2,"
        f"{style.get('margin_h', 70)},{style.get('margin_h', 70)},{style['margin_v']},1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    hl, base = _bgr(style["highlight"]), _bgr(style["color"])
    pop = style.get("pop", 112)
    up = style.get("uppercase", True)
    lines = []
    groups = _groups(words, style["max_words"], style.get("max_chars", 20))
    for gi, g in enumerate(groups):
        limit = groups[gi + 1][0].start if gi + 1 < len(groups) else g[-1].end + 0.3
        for i, w in enumerate(g):
            start = w.start
            end = g[i + 1].start if i + 1 < len(g) else min(w.end + 0.25, limit)
            parts = []
            for j, x in enumerate(g):
                t = x.text.upper() if up else x.text
                parts.append(f"{{\\c{hl}\\fscx{pop}\\fscy{pop}}}{t}{{\\c{base}\\fscx100\\fscy100}}" if j == i else t)
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(max(end, start + 0.05))},Cap,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"


def write_ass(words: list[Word], style: dict, path: Path) -> None:
    path.write_text(build_ass(words, style), encoding="utf-8")
