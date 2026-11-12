"""Entrada por URL (YouTube etc.) via yt-dlp, sem salvar o vídeo inteiro: só áudio leve + trechos."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


def is_url(s: str) -> bool:
    return re.match(r"https?://", s) is not None


def _yt(ffmpeg: str, *args: str) -> None:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--quiet", "--no-warnings", "--ffmpeg-location", ffmpeg]
    if shutil.which("node"):
        cmd += ["--js-runtimes", "node"]  # o YouTube exige um runtime JS para extrair os formatos
    for _ in range(3):  # falhas de rede/ffmpeg no meio do download são pontuais: tenta de novo
        r = subprocess.run(cmd + list(args), capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return
    raise RuntimeError(f"yt-dlp falhou: {r.stderr[-400:]}")


def audio(url: str, folder: Path, ffmpeg: str) -> Path:
    """Baixa só o áudio em baixa qualidade (~0,5 MB/min), suficiente para transcrever."""
    _yt(ffmpeg, "-f", "bestaudio[abr<=64]/worstaudio", "-o", str(folder / "audio.%(ext)s"), url)
    return next(folder.glob("audio.*"))


def _stream_len(ffmpeg: str, path: Path, kind: str) -> float:
    """Duração real (s) do primeiro stream 'a' ou 'v', lida decodificando; o container pode mentir."""
    r = subprocess.run([ffmpeg, "-i", str(path), "-map", f"0:{kind}:0", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    t = re.findall(r"time=(\d+):(\d+):([\d.]+)", r.stderr)
    return float(t[-1][0]) * 3600 + float(t[-1][1]) * 60 + float(t[-1][2]) if t else 0.0


def section(url: str, start: float, end: float, out: Path, ffmpeg: str) -> Path:
    """Baixa só o trecho [start, end] (até 1080p) para out (mp4). Se o áudio vier truncado, rebaixa o áudio e remuxa."""
    span = f"*{start:.2f}-{end:.2f}"
    _yt(ffmpeg, "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
        "--download-sections", span, "-o", str(out.with_suffix(".%(ext)s")), url)
    if _stream_len(ffmpeg, out, "a") < _stream_len(ffmpeg, out, "v") - 1.5:  # bug: áudio da seção termina cedo
        stem = out.with_name(out.stem + "_audio")
        _yt(ffmpeg, "-f", "bestaudio", "--download-sections", span, "-o", f"{stem}.%(ext)s", url)
        audio = next(out.parent.glob(stem.name + ".*"))
        fixed = out.with_name(out.stem + "_fix.mp4")
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(out), "-i", str(audio), "-map", "0:v", "-map", "1:a",
                        "-c", "copy", str(fixed)], check=True)
        fixed.replace(out)
    return out
