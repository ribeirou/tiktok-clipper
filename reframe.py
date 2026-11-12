"""Reenquadramento 9:16: crop que segue o rosto (suavizado) ou fundo desfocado."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("clipper")
W, H = 1080, 1920
COLOR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
BLUR_FILTER = (
    f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:5[bg];"
    f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
)


def _face_centers(cap: cv2.VideoCapture, step: int = 5) -> list[float | None]:
    """Centro x (0..1) do maior rosto a cada `step` frames; None onde não há rosto."""
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    out: list[float | None] = []
    last: float | None = None
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % step == 0:
            scale = 640 / frame.shape[1]
            gray = cv2.cvtColor(cv2.resize(frame, None, fx=scale, fy=scale), cv2.COLOR_BGR2GRAY)
            faces = det.detectMultiScale(gray, 1.1, 5, minSize=(40, 40))
            last = None
            if len(faces):
                x, _, w, _ = max(faces, key=lambda f: f[2] * f[3])
                last = (x + w / 2) / gray.shape[1]
        out.append(last)
        i += 1
    return out


def smooth_path(centers: list[float | None], window: int = 45) -> np.ndarray:
    """Preenche lacunas, aplica média móvel e devolve o centro x por frame (0..1)."""
    arr = np.array([np.nan if c is None else c for c in centers], dtype=float)
    ok = ~np.isnan(arr)
    if not ok.any():
        return np.full(len(arr), 0.5)  # sem rosto: crop central
    arr = np.interp(np.arange(len(arr)), np.flatnonzero(ok), arr[ok])
    pad = np.pad(arr, window // 2, mode="edge")
    return np.convolve(pad, np.ones(window) / window, mode="valid")[: len(arr)]


def reframe_to_file(src: Path, dst: Path, ffmpeg: str, face_track: bool = True) -> None:
    """Gera dst (vídeo sem áudio, 1080x1920) com crop vertical. Lê frames, recorta e envia ao ffmpeg."""
    cap = cv2.VideoCapture(str(src))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        crop_h = min(h, int(w * 16 / 9))
        crop_w = int(crop_h * 9 / 16) // 2 * 2
        if face_track:
            centers = _face_centers(cap)
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            sampled = centers[::5]
            log.info("rosto detectado em %d/%d amostras", sum(c is not None for c in sampled), len(sampled))
            path = smooth_path(centers)
        else:
            path = None
        _write_frames(cap, dst, ffmpeg, fps, w, h, crop_w, crop_h, path)
    finally:
        cap.release()


def _write_frames(cap: cv2.VideoCapture, dst: Path, ffmpeg: str, fps: float, w: int, h: int,
                  crop_w: int, crop_h: int, path: np.ndarray | None) -> None:
    """Recorta cada frame em torno de path (centro x por frame), redimensiona e envia ao ffmpeg."""
    proc = subprocess.Popen(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
         "-r", str(fps), "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv",  # BGR -> YUV em BT.709
         "-c:v", "libx264", "-crf", "14", "-preset", "veryfast", "-pix_fmt", "yuv420p", *COLOR_TAGS, str(dst)],
        stdin=subprocess.PIPE)
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        cx = path[min(i, len(path) - 1)] if path is not None else 0.5
        x = int(np.clip(cx * w - crop_w / 2, 0, w - crop_w))
        y = (h - crop_h) // 2
        tile = cv2.resize(frame[y:y + crop_h, x:x + crop_w], (W, H), interpolation=cv2.INTER_LANCZOS4)
        proc.stdin.write(tile.tobytes())
        i += 1
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg falhou ao gravar o vídeo reenquadrado")
