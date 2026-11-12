"""CLI: vídeo longo -> clipes verticais 9:16 com legenda queimada.

Exemplo:
  python clipper.py video.mp4 --clips 5 --min 20 --max 60 --lang pt --output ./clips
  python clipper.py https://youtu.be/XXXX --clips 5          # baixa só áudio + trechos escolhidos
"""
from __future__ import annotations

import argparse
import json
import logging
import tempfile
from dataclasses import asdict
from pathlib import Path

import yaml
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

import fetch
from render import check_video, find_ffmpeg, render_clip, slug
from select_clips import select_clips
from transcribe import TranscribeError, Word, transcribe

log = logging.getLogger("clipper")
HERE = Path(__file__).parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", help="arquivo de vídeo ou URL")
    p.add_argument("--clips", type=int, default=5, help="quantidade de clipes")
    p.add_argument("--min", type=float, default=30, dest="min_s", help="duração mínima (s)")
    p.add_argument("--max", type=float, default=60, dest="max_s", help="duração máxima (s)")
    p.add_argument("--lang", default="pt", help="idioma da fala ('auto' detecta)")
    p.add_argument("--transcriber", default="faster-whisper", choices=["faster-whisper", "voicestudio"])
    p.add_argument("--whisper-model", default=None, help="tiny/base/small/medium/large-v3 (padrão: config.yaml)")
    p.add_argument("--layout", default="crop", choices=["crop", "blur"], help="crop 9:16 ou vídeo com fundo desfocado")
    p.add_argument("--no-face-track", action="store_true", help="crop central fixo")
    p.add_argument("--remove-silence", action="store_true", help="encurta pausas longas")
    p.add_argument("--caption-style", default=None, help="estilo definido em config.yaml")
    p.add_argument("--no-captions", action="store_true", help="não queima legenda (para legendar em outra ferramenta)")
    p.add_argument("--output", default="./clips", help="pasta de saída")
    p.add_argument("--clips-file", help="JSON com os clipes (start, end, title, reason), ignora a API")
    p.add_argument("--transcript", help="caminho do JSON de transcrição (usa se existir, senão grava)")
    p.add_argument("--transcribe-only", action="store_true", help="só transcreve e sai")
    return p.parse_args()


def load_config() -> dict:
    cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass
    return cfg


def clip_words(words: list[Word], a: float, b: float) -> list[Word]:
    return [w for w in words if w.start >= a - 0.01 and w.end <= b + 0.01]


def cut_times(words: list[Word], a: float, b: float) -> tuple[float, float]:
    """Folga de ~0,1s sem invadir a palavra vizinha: o corte cai no meio da pausa entre palavras."""
    prev = [w for w in words if w.end <= a + 0.01 and w.start < a - 0.01]
    nxt = [w for w in words if w.start >= b - 0.01 and w.end > b + 0.01]
    t0 = max(a - 0.1, (prev[-1].end + a) / 2) if prev else max(a - 0.1, 0)
    t1 = min(b + 0.15, (b + nxt[0].start) / 2) if nxt else b + 0.15
    return t0, t1


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[RichHandler(show_path=False, markup=False)])
    cfg = load_config()
    ffmpeg = find_ffmpeg()
    lang = None if args.lang == "auto" else args.lang
    style_name = args.caption_style or cfg.get("default_style", "tiktok")
    if style_name not in cfg["caption_styles"]:
        raise SystemExit(f"estilo '{style_name}' não existe. Opções: {', '.join(cfg['caption_styles'])}")
    style = cfg["caption_styles"][style_name]
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    model = args.whisper_model or cfg.get("whisper_model", "small")
    cfg["model_dir"] = cfg.get("model_dir") or None

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        url = args.video if fetch.is_url(args.video) else None

        # 1) fonte + transcrição
        if url:
            log.info("[1/4] baixando só o áudio (temporário)...")
            source = fetch.audio(url, tmp, ffmpeg)
        else:
            source = Path(args.video)
            check_video(ffmpeg, source)
        log.info("[1/4] transcrevendo (%s, modelo %s)...", args.transcriber, model)
        try:
            words = transcribe(source, lang, args.transcriber, model, cfg, cache=args.transcript)
        except TranscribeError as e:
            raise SystemExit(f"erro na transcrição: {e}")
        log.info("%d palavras, %.0fs de fala", len(words), words[-1].end)
        if args.transcribe_only:
            log.info("transcrição salva. Fim (--transcribe-only).")
            return

        # 2) seleção
        log.info("[2/4] escolhendo %d trechos de %.0f–%.0fs...", args.clips, args.min_s, args.max_s)
        try:
            clips = select_clips(words, args.clips, args.min_s, args.max_s, cfg["claude_model"], args.clips_file)
        except (ValueError, OSError) as e:
            raise SystemExit(f"erro na seleção de clipes: {e}")
        for i, c in enumerate(clips, 1):
            log.info("  %d. %.1f–%.1f (%.0fs) %s", i, c.start, c.end, c.end - c.start, c.title)
        (out_dir / "clips.json").write_text(json.dumps([asdict(c) for c in clips], ensure_ascii=False, indent=2),
                                            encoding="utf-8")

        # 3) corte + render
        log.info("[3/4] renderizando...")
        with Progress(TextColumn("{task.description}"), BarColumn(), TimeElapsedColumn()) as prog:
            task = prog.add_task("clipes", total=len(clips))
            for i, c in enumerate(clips, 1):
                t0, t1 = cut_times(words, c.start, c.end)
                name = out_dir / f"clip_{i:02d}_{slug(c.title)}.mp4"
                prog.update(task, description=f"clip {i}/{len(clips)}: {name.name}")
                if url:  # baixa só o trecho, com 2s de folga
                    a = max(t0 - 2, 0)
                    src = fetch.section(url, a, t1 + 2, tmp / f"sec_{i}.mp4", ffmpeg)
                    shift = a
                else:
                    src, shift = source, 0.0
                shifted = [Word(w.start - shift, w.end - shift, w.text) for w in clip_words(words, c.start, c.end)]
                try:
                    render_clip(ffmpeg, src, t0 - shift, t1 - shift, shifted, name, style, cfg,
                                args.layout, not args.no_face_track, args.remove_silence, not args.no_captions)
                except RuntimeError as e:
                    log.error("clip %d falhou: %s", i, e)
                prog.advance(task)
    log.info("[4/4] pronto: %s", out_dir.resolve())


if __name__ == "__main__":
    main()
