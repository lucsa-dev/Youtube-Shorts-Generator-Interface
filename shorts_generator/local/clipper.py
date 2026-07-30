"""Local clipping: ffmpeg subclip + OpenCV face-aware vertical crop.

Two stages per highlight:
  1. Cut the source video to [start, end] with ffmpeg (re-encoded, audio kept).
  2. Reframe the cut to the target aspect ratio. For 9:16 we slide a vertical
     window horizontally across the frame to keep faces centred (Haar
     cascade — same approach as the original repo, no external models).
"""
import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple

from ..config import LOCAL_FACE_SMOOTHING, LOCAL_OUTPUT_DIR


def _safe_filename(title: str, *, max_len: int = 80) -> str:
    """Sanitize a highlight title for use as a filesystem basename."""
    text = (title or "").strip()
    text = text.replace(":", " -")
    text = re.sub(r"[\\/:*?\"<>|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    if not text:
        return "untitled"
    return text[:max_len].rstrip(" ._")


def _short_out_path(out_dir: str, highlight: Dict, index: int) -> str:
    hid = highlight.get("id", index)
    try:
        hid_n = int(hid)
    except (TypeError, ValueError):
        hid_n = index
    slug = _safe_filename(str(highlight.get("title") or f"short_{hid_n:02d}"))
    return os.path.join(out_dir, f"{hid_n:02d} - {slug}.mp4")


def _ratio(aspect_ratio: str) -> float:
    """Parse '9:16' → 9/16, '1:1' → 1.0."""
    try:
        w, h = aspect_ratio.split(":")
        return float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        return 9.0 / 16.0


def _cut_subclip(source_path: str, start: float, end: float, out_path: str) -> str:
    """ffmpeg -ss start -to end → re-encoded mp4 with audio."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", source_path,
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def _probe_video_size(path: str) -> Tuple[int, int]:
    """Return (width, height) of the first video stream via ffprobe."""
    import json

    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        path,
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    streams = (json.loads(proc.stdout) or {}).get("streams") or []
    if not streams:
        raise RuntimeError(f"sem stream de vídeo em {path}")
    w = int(streams[0].get("width") or 0)
    h = int(streams[0].get("height") or 0)
    if w < 2 or h < 2:
        raise RuntimeError(f"dimensões inválidas em {path}: {w}x{h}")
    return w - (w % 2), h - (h % 2)


def inject_thumbnail_first_frame(video_path: str, image_path: str) -> bool:
    """Overlay ``image_path`` onto frame 0 of ``video_path`` (in place).

    Platforms that grab the first frame as a poster then show the custom
    thumbnail. Duration/audio are unchanged — only frame 0 is replaced.
    """
    if not video_path or not image_path:
        return False
    if not os.path.isfile(video_path) or not os.path.isfile(image_path):
        return False
    if os.path.getsize(image_path) <= 0:
        return False

    try:
        w, h = _probe_video_size(video_path)
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError, TypeError) as exc:
        print(f"[clip/local] thumb frame probe failed: {exc}", flush=True)
        return False

    tmp_out = video_path + ".thumbframe.mp4"
    # scale+pad image to exact video size, overlay only on n==0
    filter_complex = (
        f"[1:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[im];"
        f"[0:v][im]overlay=0:0:enable='eq(n\\,0)'[v]"
    )

    def _run(audio_args: list) -> None:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_path,
            "-i", image_path,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            *audio_args,
            tmp_out,
        ]
        subprocess.run(cmd, check=True)

    try:
        try:
            _run(["-c:a", "copy"])
        except subprocess.SubprocessError:
            _run(["-c:a", "aac", "-b:a", "128k"])
        if not os.path.isfile(tmp_out) or os.path.getsize(tmp_out) <= 0:
            return False
        os.replace(tmp_out, video_path)
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[clip/local] thumb frame inject failed: {exc}", flush=True)
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except OSError:
            pass
        return False


def _reframe_vertical(in_path: str, out_path: str, aspect_ratio: str) -> str:
    """Crop the cut clip to the target aspect ratio, tracking faces if possible."""
    try:
        import cv2  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "opencv-python is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    target_ratio = _ratio(aspect_ratio)
    cap = cv2.VideoCapture(in_path)
    if not cap.isOpened():
        raise RuntimeError(f"could not open {in_path}")

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    # Compute the largest crop that fits inside the frame at the target ratio.
    if target_ratio < src_w / src_h:
        crop_h = src_h
        crop_w = int(crop_h * target_ratio)
    else:
        crop_w = src_w
        crop_h = int(crop_w / target_ratio)
    crop_w = max(2, crop_w - (crop_w % 2))
    crop_h = max(2, crop_h - (crop_h % 2))

    # OpenCV 5 removed CascadeClassifier / bundled haarcascades. Prefer Haar
    # when available (OpenCV 4.x); otherwise center-crop without face tracking.
    face_cascade = None
    if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
        cascade_path = getattr(cv2.data, "haarcascades", "") + "haarcascade_frontalface_default.xml"
        if cascade_path and os.path.exists(cascade_path):
            face_cascade = cv2.CascadeClassifier(cascade_path)
            if face_cascade.empty():
                face_cascade = None
    if face_cascade is None:
        print("[clip/local] face cascade unavailable — using center crop", flush=True)

    silent_path = out_path + ".silent.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(silent_path, fourcc, fps, (crop_w, crop_h))

    last_center: Optional[Tuple[int, int]] = None
    smoothing = LOCAL_FACE_SMOOTHING  # how aggressively to chase a new face position
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if face_cascade is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)
            )
            if len(faces) > 0:
                # Pick the largest face — usually the speaker.
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                cx = x + w // 2
                cy = y + h // 2
                if last_center is None:
                    last_center = (cx, cy)
                else:
                    lx, ly = last_center
                    last_center = (
                        int(lx + (cx - lx) * smoothing),
                        int(ly + (cy - ly) * smoothing),
                    )
        if last_center is None:
            last_center = (src_w // 2, src_h // 2)

        cx, cy = last_center
        x0 = max(0, min(src_w - crop_w, cx - crop_w // 2))
        y0 = max(0, min(src_h - crop_h, cy - crop_h // 2))
        cropped = frame[y0:y0 + crop_h, x0:x0 + crop_w]
        writer.write(cropped)

    cap.release()
    writer.release()

    # Re-encode to H.264 for browser playback. OpenCV's mp4v (mpeg4) often
    # yields black video + working audio in Chrome/Firefox.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", silent_path,
        "-i", in_path,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "128k",
        "-map", "0:v:0", "-map", "1:a:0?",
        "-shortest",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    os.remove(silent_path)
    return out_path


def crop_clip_local(
    source_path: str,
    start_time: float,
    end_time: float,
    aspect_ratio: str,
    out_path: str,
) -> str:
    """Cut + reframe one highlight, returning the local mp4 path."""
    cut_path = out_path + ".cut.mp4"
    try:
        _cut_subclip(source_path, start_time, end_time, cut_path)
        _reframe_vertical(cut_path, out_path, aspect_ratio)
    finally:
        if os.path.exists(cut_path):
            os.remove(cut_path)
    return out_path


def crop_highlights_local(
    source_path: str,
    highlights: List[Dict],
    aspect_ratio: str = "9:16",
    out_dir: Optional[str] = None,
    on_short_done=None,
    transcript: Optional[Dict] = None,
    caption_style: Optional[Dict] = None,
) -> List[Dict]:
    from ..captions import apply_karaoke_captions, resolve_style

    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    style = resolve_style(caption_style) if caption_style is not None else None
    results: List[Dict] = []
    total = len(highlights)
    for i, h in enumerate(highlights, 1):
        out_path = _short_out_path(out_dir, h, i)
        print(f"[clip/local] {i}/{total}: {h.get('title', '(untitled)')} → {out_path}", flush=True)
        try:
            start = float(h["start_time"])
            end = float(h["end_time"])
            crop_clip_local(
                source_path,
                start,
                end,
                aspect_ratio,
                out_path,
            )
            if style and style.get("enabled", True):
                apply_karaoke_captions(
                    out_path,
                    transcript,
                    start,
                    end,
                    style,
                    out_path=out_path,
                )
            # If a custom/AI thumbnail was prepared ahead of render, burn it
            # into frame 0 so the vertical clip opens on that poster.
            thumb_frame = (
                h.get("thumbnail_frame")
                or h.get("thumbnail_path")
                or ""
            )
            injected = False
            if thumb_frame and os.path.isfile(str(thumb_frame)):
                injected = inject_thumbnail_first_frame(out_path, str(thumb_frame))
                if injected:
                    print(
                        f"[clip/local] thumbnail → 1º frame: {thumb_frame}",
                        flush=True,
                    )
            short = {**h, "clip_url": out_path}
            if style:
                short["caption_style"] = style
            if injected:
                short["thumbnail_frame_injected"] = True
            # Don't persist internal path hints on the short payload
            short.pop("thumbnail_frame", None)
            short.pop("thumbnail_path", None)
        except Exception as e:
            print(f"[clip/local] {i} failed: {e}", flush=True)
            short = {**h, "clip_url": None, "error": str(e)}
        results.append(short)
        if on_short_done:
            on_short_done(short, i, total)
    return results
