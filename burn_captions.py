"""Queima legenda em um vídeo já editado (ex.: o corte do editonny).

  python burn_captions.py corte.mp4 palavras.json saida.mp4 --caption-style yellow

palavras.json: lista de {text, start, end} ou {words: [...]}, com tempos relativos ao vídeo.
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

import yaml

from captions import write_ass
from reframe import COLOR_TAGS
from render import find_ffmpeg, run
from transcribe import Word


# grafias que o Whisper erra -> grafia correta (A Vigília: Gwayne é o personagem do Grifo)
NAMES = {"gwen": "Gwayne", "gweny": "Gwayne", "gwenny": "Gwayne", "gwene": "Gwayne", "gwaine": "Gwayne",
         "goine": "Gwayne", "griff": "Grifo", "gryff": "Grifo", "grifo": "Grifo"}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video")
    p.add_argument("words")
    p.add_argument("output")
    p.add_argument("--caption-style", default="yellow")
    p.add_argument("--fix", help="JSON [[indice, 'texto', n_palavras_seguintes_a_absorver], ...] para corrigir erros do Whisper")
    a = p.parse_args()

    cfg = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))
    style = cfg["caption_styles"][a.caption_style]
    raw = json.loads(Path(a.words).read_text(encoding="utf-8-sig"))
    raw = raw["words"] if isinstance(raw, dict) else raw
    fixes = json.loads(Path(a.fix).read_text(encoding="utf-8-sig")) if a.fix else []
    def key(w: dict) -> str:
        return re.sub(r"[^\w-]", "", w["text"]).lower()

    for old, text, _ in (f for f in fixes if isinstance(f[0], str) and " " in f[0]):  # ["duas palavras", "certa", 0]
        seq = [p.lower() for p in old.split()]
        i = 0
        while i + len(seq) <= len(raw):
            if [key(w) for w in raw[i:i + len(seq)]] == seq:
                tail = re.search(r"[^\w-]*$", raw[i + len(seq) - 1]["text"]).group(0)
                raw[i] = {"text": text + tail, "start": raw[i]["start"], "end": raw[i + len(seq) - 1]["end"]}
                del raw[i + 1:i + len(seq)]
            i += 1
    for old, text, _ in (f for f in fixes if isinstance(f[0], str) and " " not in f[0]):  # ["palavra errada", "certa", 0]: troca em todo o clipe
        for w in raw:
            m = re.fullmatch(r"([\w-]+)([^\w-]*)", w["text"].strip())
            if m and m.group(1).lower() == old.lower():
                w["text"] = (text + m.group(2)) if text else ""
        raw = [w for w in raw if w["text"]]
    for i, text, merge in sorted((f for f in fixes if not isinstance(f[0], str)), reverse=True):
        if not text:  # texto vazio remove a palavra i (e as `merge` seguintes)
            del raw[i:i + 1 + merge]
            continue
        raw[i] = {"text": text, "start": raw[i]["start"], "end": raw[i + merge]["end"]}  # corrige a palavra i e absorve as `merge` seguintes
        del raw[i + 1:i + 1 + merge]
    for w in raw:  # normaliza nomes que o Whisper escreve de vários jeitos (ajuste em names.json)
        m = re.fullmatch(r"([\w-]+)([^\w-]*)", w["text"].strip())
        if m and m.group(1).lower() in NAMES:
            w["text"] = NAMES[m.group(1).lower()] + m.group(2)
    words, prev_end = [], 0.0
    for w in raw:
        s = max(float(w["start"]), prev_end)  # tempos crescentes
        e = max(float(w["end"]), s + 0.05)
        words.append(Word(s, e, w["text"].strip()))
        prev_end = e

    out = Path(a.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fonts = cfg.get("fonts_dir", "C:/Windows/Fonts").replace(":", "\\:")
    with tempfile.TemporaryDirectory() as td:
        write_ass(words, style, Path(td) / "cap.ass")
        run([find_ffmpeg(), "-y", "-i", str(Path(a.video).resolve()), "-vf", f"ass=cap.ass:fontsdir='{fonts}'",
             "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-maxrate", "10M", "-bufsize", "20M",
             "-pix_fmt", "yuv420p", "-r", str(cfg.get("fps", 30)), *COLOR_TAGS, "-c:a", "copy",
             "-movflags", "+faststart", str(out)], cwd=Path(td))
    print(out)


if __name__ == "__main__":
    main()
