"""Transcrição com timestamps por palavra. Backends intercambiáveis atrás de transcribe()."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("clipper")


@dataclass
class Word:
    start: float
    end: float
    text: str


class TranscribeError(RuntimeError):
    pass


def _faster_whisper(path: Path, lang: str | None, model: str, cfg: dict) -> list[Word]:
    """Roda local. Usa GPU (CUDA) se existir e funcionar; senão CPU."""
    from faster_whisper import WhisperModel
    import ctranslate2

    def run(device: str, compute: str) -> list[Word]:
        m = WhisperModel(model, device=device, compute_type=compute,
                         download_root=cfg.get("model_dir"))
        segs, info = m.transcribe(str(path), language=lang, word_timestamps=True, vad_filter=True)
        log.info("idioma: %s (%.0f%%)", info.language, info.language_probability * 100)
        return [Word(round(w.start, 3), round(w.end, 3), w.word.strip())
                for s in segs for w in (s.words or []) if w.word.strip()]

    if ctranslate2.get_cuda_device_count() > 0:
        try:
            return run("cuda", "float16")
        except Exception as e:  # faltam libs CUDA (cublas/cudnn) etc.
            log.warning("GPU indisponível (%s); usando CPU.", str(e)[:80])
    return run("cpu", "int8")


def _voicestudio(path: Path, lang: str | None, model: str, cfg: dict) -> list[Word]:
    """API local do VoiceStudio (OpenAI-compatível), com timestamps por palavra."""
    import requests

    url = cfg.get("voicestudio_url", "http://127.0.0.1:3900").rstrip("/") + "/v1/audio/transcriptions"
    data = {"model": "whisper-1", "response_format": "verbose_json", "timestamp_granularities[]": "word"}
    if lang:
        data["language"] = lang
    try:
        with open(path, "rb") as f:
            r = requests.post(url, data=data, files={"file": f}, timeout=cfg.get("voicestudio_timeout", 1800))
        r.raise_for_status()
    except requests.RequestException as e:
        raise TranscribeError(f"VoiceStudio indisponível: {e}") from e
    words = [Word(w["start"], w["end"], w["word"].strip()) for w in r.json().get("words", [])]
    if not words:
        raise TranscribeError("VoiceStudio não retornou timestamps por palavra")
    return words


BACKENDS: dict[str, Callable[..., list[Word]]] = {
    "faster-whisper": _faster_whisper,
    "voicestudio": _voicestudio,
}


def transcribe(video_path: str | Path, lang: str | None = "pt", backend: str = "faster-whisper",
               model: str = "small", cfg: dict | None = None, cache: str | Path | None = None) -> list[Word]:
    """Retorna as palavras do vídeo. Cache em JSON (padrão: ao lado do vídeo)."""
    cfg = cfg or {}
    path = Path(video_path)
    cache = Path(cache) if cache else path.with_suffix(path.suffix + ".transcript.json")
    if cache.exists():
        log.info("transcrição em cache: %s", cache)
        return [Word(**w) for w in json.loads(cache.read_text(encoding="utf-8"))["words"]]

    if backend not in BACKENDS:
        raise TranscribeError(f"transcritor desconhecido: {backend} (use {', '.join(BACKENDS)})")
    try:
        words = BACKENDS[backend](path, lang, model, cfg)
    except Exception as e:
        if backend == "faster-whisper":
            raise TranscribeError(f"faster-whisper falhou: {e}") from e
        log.warning("%s falhou (%s). Caindo para faster-whisper.", backend, e)
        words = _faster_whisper(path, lang, model, cfg)
    if not words:
        raise TranscribeError("nenhuma fala detectada no vídeo")

    cache.write_text(json.dumps({"lang": lang, "words": [asdict(w) for w in words]},
                                ensure_ascii=False), encoding="utf-8")
    return words
