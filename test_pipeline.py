"""Auto-checagem rápida: python test_pipeline.py"""
from captions import build_ass
from reframe import smooth_path
from render import keep_intervals, remap_words, slug
from select_clips import Clip, snap, validate
from transcribe import Word


def demo() -> None:
    # palavras: frase 1 (0–2s), pausa de 3s, frase 2 (5–7s)
    w = [Word(0, 0.5, "Olá"), Word(0.6, 1.2, "pessoal."), Word(4.2, 4.8, "Agora"), Word(5, 5.6, "vamos"),
         Word(5.7, 6.5, "começar.")]
    keeps = keep_intervals(w, 7.0)
    assert len(keeps) == 2, keeps
    r = remap_words(w, keeps)
    assert r[2].start < w[2].start - 2, r  # a pausa de ~3s foi removida
    assert abs((r[3].start - r[2].start) - (w[3].start - w[2].start)) < 1e-6  # ritmo interno preservado

    c = snap(Clip(0.3, 6.0, "x"), w, 1, 10)
    assert (c.start, c.end) == (0, 6.5), c  # início/fim caem em limites de frase/palavra

    assert len(validate([{"start": 0, "end": 30, "title": "a"}, {"start": 10, "end": 40, "title": "b"},
                         {"start": "x"}], 20, 60, 100)) == 1
    p = smooth_path([0.2, None, None, 0.8, 0.8, 0.8], window=3)
    assert len(p) == 6 and 0.2 <= p.min() and p.max() <= 0.8
    assert slug("Gancho: Ação & Reação!") == "gancho_acao_reacao"

    style = dict(font="Arial", size=80, color="#FFFFFF", highlight="#FFE600", outline="#000000",
                 outline_width=6, margin_v=400, max_words=3)
    ass = build_ass(w, style)
    assert ass.count("Dialogue:") == 5 and "&H00E6FF&" in ass  # cor ASS é BGR
    print("ok")


if __name__ == "__main__":
    demo()
