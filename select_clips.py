"""Escolha dos melhores trechos: arquivo manual, API do Claude ou fallback por densidade de fala."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from transcribe import Word

log = logging.getLogger("clipper")

PROMPT = """Você escolhe os {n} melhores momentos de um vídeo para virarem clipes de TikTok.
Regras: gancho forte nos primeiros segundos; ideia completa (começo, meio e fim); nunca cortar frase no meio;
duração entre {lo} e {hi} segundos; trechos sem sobreposição.
Responda SOMENTE com um array JSON, sem texto extra, no formato:
[{{"start": 12.3, "end": 48.0, "title": "titulo curto", "reason": "por que funciona"}}]
Os tempos são em segundos e vêm dos marcadores [t] da transcrição.

TRANSCRIÇÃO:
{text}"""


@dataclass
class Clip:
    start: float
    end: float
    title: str
    reason: str = ""


def _boundary(a: Word, b: Word) -> bool:
    """True se há fim de frase / pausa entre as palavras a e b."""
    return a.text[-1:] in ".?!" or b.start - a.end > 0.6


def format_transcript(words: list[Word]) -> str:
    """Uma linha por frase, com o tempo de início entre colchetes."""
    lines, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        if i + 1 == len(words) or (_boundary(w, words[i + 1]) and len(cur) >= 6) or len(cur) >= 40:
            lines.append(f"[{cur[0].start:.1f}] " + " ".join(x.text for x in cur))
            cur = []
    return "\n".join(lines)


def validate(raw: object, lo: float, hi: float, total: float) -> list[Clip]:
    """Valida o JSON (da API ou de arquivo) e descarta itens inválidos ou sobrepostos."""
    if not isinstance(raw, list):
        raise ValueError("o JSON deve ser uma lista de clipes")
    clips: list[Clip] = []
    for i, c in enumerate(raw):
        try:
            s, e = float(c["start"]), float(c["end"])
            title = str(c.get("title") or f"clip {i + 1}")
        except (KeyError, TypeError, ValueError):
            log.warning("item %d inválido, ignorado: %r", i + 1, c)
            continue
        if not (0 <= s < e <= total + 1) or not (lo - 5 <= e - s <= hi + 5):
            log.warning("item %d fora dos limites (%.1f–%.1f), ignorado", i + 1, s, e)
            continue
        if any(s < o.end and e > o.start for o in clips):
            log.warning("item %d sobrepõe outro, ignorado", i + 1)
            continue
        clips.append(Clip(s, e, title, str(c.get("reason", ""))))
    if not clips:
        raise ValueError("nenhum clipe válido")
    return clips


def snap(c: Clip, words: list[Word], lo: float, hi: float) -> Clip:
    """Ajusta início/fim para limites de frase (e portanto de palavra), respeitando lo..hi."""
    n = len(words)
    starts = [i for i in range(n) if i == 0 or _boundary(words[i - 1], words[i])]
    ends = [i for i in range(n) if i == n - 1 or _boundary(words[i], words[i + 1])]
    si = min(starts, key=lambda i: abs(words[i].start - c.start))
    s = words[si].start
    ok = [i for i in ends if i >= si and lo <= words[i].end - s <= hi]
    if ok:
        ei = min(ok, key=lambda i: abs(words[i].end - c.end))
    else:  # sem fim de frase na faixa: termina na palavra mais próxima do alvo
        ei = min((i for i in range(si, n) if words[i].end - s <= hi),
                 key=lambda i: abs(words[i].end - c.end), default=si)
    return Clip(words[si].start, words[ei].end, c.title, c.reason)


def _from_claude(words: list[Word], n: int, lo: float, hi: float, model: str) -> list[Clip]:
    import anthropic

    client = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=model, max_tokens=8000,
        messages=[{"role": "user", "content": PROMPT.format(n=n, lo=lo, hi=hi, text=format_transcript(words))}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("resposta sem JSON")
    return validate(json.loads(m.group(0)), lo, hi, words[-1].end)


def _fallback(words: list[Word], n: int, lo: float, hi: float) -> list[Clip]:
    """Janelas de frases consecutivas; nota = palavras/segundo, com penalidade por pausas longas."""
    sents, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        if i + 1 == len(words) or _boundary(w, words[i + 1]):
            sents.append(cur)
            cur = []
    cands = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            s, e = sents[i][0].start, sents[j][-1].end
            if e - s > hi:
                break
            if e - s >= lo:
                ws = [w for k in range(i, j + 1) for w in sents[k]]
                pauses = sum(max(0, b.start - a.end - 0.4) for a, b in zip(ws, ws[1:]))
                cands.append((len(ws) / (e - s) - pauses / (e - s), s, e))
    chosen: list[Clip] = []
    for _, s, e in sorted(cands, reverse=True):
        if len(chosen) == n:
            break
        if all(s >= c.end or e <= c.start for c in chosen):
            chosen.append(Clip(s, e, f"trecho {len(chosen) + 1}", "trecho denso em fala"))
    return sorted(chosen, key=lambda c: c.start)


def select_clips(words: list[Word], n: int, lo: float, hi: float, model: str,
                 clips_file: str | Path | None = None) -> list[Clip]:
    """clips_file > API do Claude (se houver chave) > fallback local. Sempre ajustado a limites de frase."""
    total = words[-1].end
    clips: list[Clip] | None = None
    if clips_file:
        clips = validate(json.loads(Path(clips_file).read_text(encoding="utf-8")), lo, hi, total)
        log.info("clipes lidos de %s", clips_file)
    elif os.environ.get("ANTHROPIC_API_KEY"):
        try:
            clips = _from_claude(words, n, lo, hi, model)
            log.info("Claude (%s) escolheu %d clipes", model, len(clips))
        except Exception as e:
            log.warning("falha na API do Claude (%s). Usando fallback local.", str(e)[:120])
    else:
        log.warning("ANTHROPIC_API_KEY ausente. Usando fallback local.")
    if clips is None:
        clips = _fallback(words, n, lo, hi)
    return [snap(c, words, lo, hi) for c in clips[:n]]
