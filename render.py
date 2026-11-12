"""Corte, remoção de silêncio, reenquadre, legenda queimada, loudnorm, fade e exportação."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from captions import write_ass
from reframe import BLUR_FILTER, COLOR_TAGS, reframe_to_file
from transcribe import Word

log = logging.getLogger("clipper")


def find_ffmpeg() -> str:
    """ffmpeg do PATH, ou o binário do pacote imageio-ffmpeg; senão explica como instalar."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise SystemExit("ffmpeg não encontrado. Instale: Windows `winget install Gyan.FFmpeg` | "
                         "macOS `brew install ffmpeg` | Linux `sudo apt install ffmpeg` "
                         "(ou `pip install imageio-ffmpeg`).")


def run(cmd: list[str], cwd: Path | None = None) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou:\n{r.stderr[-800:]}")


def check_video(ffmpeg: str, path: Path) -> None:
    """Falha cedo se o arquivo não existe ou não tem vídeo legível."""
    if not path.is_file():
        raise SystemExit(f"arquivo não encontrado: {path}")
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if "Video:" not in r.stderr:
        raise SystemExit(f"arquivo inválido (sem vídeo legível): {path}")


def slug(title: str) -> str:
    """Título limpo para nome de arquivo: sem acento nem caracteres especiais."""
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")[:40] or "clip"


def keep_intervals(words: list[Word], dur: float, max_gap: float = 0.7, keep_gap: float = 0.25) -> list[tuple[float, float]]:
    """Intervalos a manter: pausas maiores que max_gap são encurtadas para keep_gap."""
    keeps: list[list[float]] = []
    for i, w in enumerate(words):
        a = max(w.start - keep_gap / 2, 0)
        b = min(w.end + keep_gap / 2, words[i + 1].start if i + 1 < len(words) else dur)
        if keeps and (w.start - words[i - 1].end) <= max_gap:
            keeps[-1][1] = b  # pausa curta: continua o mesmo intervalo
        else:
            keeps.append([a, b])
    return [(a, b) for a, b in keeps]


def remap_words(words: list[Word], keeps: list[tuple[float, float]]) -> list[Word]:
    """Converte os tempos das palavras para a linha do tempo sem os silêncios cortados."""
    out, offset = [], 0.0
    ki = 0
    for w in words:
        while ki < len(keeps) - 1 and w.start >= keeps[ki][1]:
            offset += keeps[ki][1] - keeps[ki][0]
            ki += 1
        base = keeps[ki][0]
        out.append(Word(w.start - base + offset, w.end - base + offset, w.text))
    return out


def render_clip(ffmpeg: str, src: Path, t0: float, t1: float, words: list[Word], out: Path,
                style: dict, cfg: dict, layout: str = "crop", face_track: bool = True,
                remove_silence: bool = False, captions: bool = True) -> None:
    """Pipeline de um clipe. `words` em tempo absoluto do vídeo de origem; `src` começa em tempo `src_offset`
    já embutido em t0/t1 (tempos relativos ao arquivo src)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 1) corte preciso (re-encode) -> seg.mp4
        seg = tmp / "seg.mp4"
        run([ffmpeg, "-y", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(src),
             "-c:v", "libx264", "-crf", "14", "-preset", "veryfast", "-c:a", "aac", "-b:a", "256k", str(seg)])
        dur = t1 - t0
        rel = [Word(w.start - t0, w.end - t0, w.text) for w in words]

        # 2) remover silêncios longos (opcional), legendas remapeadas junto
        if remove_silence:
            keeps = keep_intervals(rel, dur)
            expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keeps)
            cut = tmp / "cut.mp4"
            run([ffmpeg, "-y", "-i", str(seg), "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB",
                 "-af", f"aselect='{expr}',asetpts=N/SR/TB", "-c:v", "libx264", "-crf", "14",
                 "-preset", "veryfast", "-c:a", "aac", "-b:a", "256k", str(cut)])
            seg, rel = cut, remap_words(rel, keeps)
            dur = sum(b - a for a, b in keeps)
            log.info("silêncios removidos: %.1fs -> %.1fs", t1 - t0, dur)

        # 3) legendas ASS (tempos relativos ao clipe)
        write_ass(rel, style, tmp / "cap.ass")
        fonts = cfg.get("fonts_dir", "C:/Windows/Fonts").replace(":", "\\:")
        sub = f"ass=cap.ass:fontsdir='{fonts}'"
        fade = cfg.get("fade", 0.25)
        tail = f"{sub}," if captions else ""  # sem legenda queimada: só o fade
        vfade =f"fade=t=in:st=0:d={fade},fade=t=out:st={max(dur - fade, 0):.3f}:d={fade}"
        afilt = (f"loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=in:st=0:d={fade},"
                 f"afade=t=out:st={max(dur - fade, 0):.3f}:d={fade}")
        enc = ["-c:v", "libx264", "-preset", "medium", "-b:v", cfg.get("video_bitrate", "6M"),
               "-maxrate", cfg.get("max_bitrate", "8M"), "-bufsize", "12M", "-pix_fmt", "yuv420p",
               "-r", str(cfg.get("fps", 30)), "-c:a", "aac", "-b:a", cfg.get("audio_bitrate", "192k"),
               "-movflags", "+faststart", *COLOR_TAGS]

        # 4) reenquadre + queima + áudio
        if layout == "blur":
            vf = f"{BLUR_FILTER},{tail}{vfade}"
            run([ffmpeg, "-y", "-i", str(seg), "-vf", vf, "-af", afilt, *enc, str(out.resolve())], cwd=tmp)
        else:
            framed = tmp / "framed.mp4"
            reframe_to_file(seg, framed, ffmpeg, face_track)
            run([ffmpeg, "-y", "-i", str(framed), "-i", str(seg), "-map", "0:v", "-map", "1:a",
                 "-vf", f"{tail}{vfade}", "-af", afilt, "-shortest", *enc, str(out.resolve())], cwd=tmp)
