"""Web UI for the AI YouTube Shorts Generator.

Run:
    uvicorn web.app:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import threading
import traceback
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import dotenv_values, load_dotenv, set_key
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
UPLOAD_DIR = ROOT / "uploads"
JOBS_DIR = ROOT / "jobs"
STATIC_DIR = Path(__file__).resolve().parent / "static"

sys.path.insert(0, str(ROOT))
load_dotenv(ENV_PATH)

UPLOAD_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="AI YouTube Shorts Generator", version="1.0.0")

# In-memory job store (also persisted as JSON under jobs/)
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()

# Editable from the web Config UI. API keys, Whisper, LLM providers stay in .env only.
CONFIG_KEYS = [
    "CONTENT_LANGUAGE",
    "LOCAL_OUTPUT_DIR",
    "LOCAL_FACE_SMOOTHING",
]

SECRET_KEYS = {"MUAPI_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"}

_PLACEHOLDER_SECRET_RE = re.compile(
    r"^(your[_-].*[_-]here|changeme|xxx+|replace.?me|<.*>|todo|fix)$",
    re.IGNORECASE,
)


def _is_real_secret(value: Optional[str]) -> bool:
    """True when a secret looks configured (non-empty, not an .env.example placeholder)."""
    val = (value or "").strip().strip("'\"")
    if not val:
        return False
    if _PLACEHOLDER_SECRET_RE.match(val):
        return False
    return True


class ConfigUpdate(BaseModel):
    values: Dict[str, str] = Field(default_factory=dict)


class YoutubeUploadBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    privacy: Optional[str] = None


class LogCapture(io.TextIOBase):
    """Capture print() output into a job's log list."""

    def __init__(self, job_id: str, original):
        self.job_id = job_id
        self.original = original
        self._buf = ""

    def write(self, s: str) -> int:
        if self.original:
            try:
                self.original.write(s)
                self.original.flush()
            except Exception:
                pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                _append_log(self.job_id, line)
        return len(s)

    def flush(self) -> None:
        if self.original:
            try:
                self.original.flush()
            except Exception:
                pass
        if self._buf.strip():
            _append_log(self.job_id, self._buf.rstrip())
            self._buf = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_log(job_id: str, message: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["logs"].append({"ts": _now(), "message": message})
        job["updated_at"] = _now()
        _persist_job(job)


def _persist_job(job: Dict[str, Any]) -> None:
    path = JOBS_DIR / f"{job['id']}.json"
    # Don't dump huge transcript into the lightweight status file twice —
    # full result lives in result.json alongside.
    slim = {k: v for k, v in job.items() if k != "result"}
    if job.get("result") is not None:
        slim["has_result"] = True
        slim["shorts_count"] = len(job["result"].get("shorts", []))
        slim["highlights_count"] = len(job["result"].get("highlights", []))
    path.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
    if job.get("result") is not None:
        (JOBS_DIR / f"{job['id']}_result.json").write_text(
            json.dumps(job["result"], indent=2, default=str), encoding="utf-8"
        )


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return value[:4] + "••••" + value[-4:]


def _read_config() -> Dict[str, Any]:
    from shorts_generator.config import LANGUAGE_OPTIONS

    raw = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    # Fall back to process env for keys present in os.environ
    for key in CONFIG_KEYS:
        if key not in raw or raw[key] is None:
            env_val = os.getenv(key)
            if env_val is not None:
                raw[key] = env_val

    items = []
    for key in CONFIG_KEYS:
        val = (raw.get(key) or "").strip()
        if key == "CONTENT_LANGUAGE" and not val:
            val = "pt"
        if key == "LOCAL_OUTPUT_DIR" and not val:
            val = "output"
        if key == "LOCAL_FACE_SMOOTHING" and not val:
            val = "0.15"
        secret = key in SECRET_KEYS
        is_set = _is_real_secret(val) if secret else bool(val)
        item: Dict[str, Any] = {
            "key": key,
            "value": "" if secret else val,
            "masked": _mask(val) if secret and is_set else None,
            "is_secret": secret,
            "is_set": is_set,
            "input_type": "language" if key == "CONTENT_LANGUAGE" else "text",
        }
        if key == "LOCAL_OUTPUT_DIR":
            item["resolved_path"] = str(Path(val).expanduser().resolve())
        items.append(item)
    muapi = _is_real_secret(raw.get("MUAPI_API_KEY"))
    openai = _is_real_secret(raw.get("OPENAI_API_KEY"))
    gemini = _is_real_secret(raw.get("GEMINI_API_KEY"))
    youtube = (
        _is_real_secret(raw.get("YOUTUBE_CLIENT_ID"))
        and _is_real_secret(raw.get("YOUTUBE_CLIENT_SECRET"))
        and _is_real_secret(raw.get("YOUTUBE_REFRESH_TOKEN"))
    )
    # Local is always available; API only when MuAPI is really configured.
    modes = ["api", "local"] if muapi else ["local"]
    default_mode = "api" if muapi else "local"
    return {
        "items": items,
        "language_options": [
            {"value": code, "label": label} for code, label in LANGUAGE_OPTIONS
        ],
        "status": {
            "muapi": muapi,
            "openai": openai,
            "gemini": gemini,
            "youtube": youtube,
            "llm_provider": (raw.get("LLM_PROVIDER") or "openai").strip().lower(),
            "content_language": (raw.get("CONTENT_LANGUAGE") or "pt").strip().strip("'\"").lower() or "pt",
            "modes": modes,
            "default_mode": default_mode,
        },
    }


def _env_float(key: str, default: float) -> float:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _write_config(updates: Dict[str, str]) -> None:
    if not ENV_PATH.exists():
        ENV_PATH.write_text("", encoding="utf-8")
    for key, value in updates.items():
        if key not in CONFIG_KEYS:
            continue
        # Skip blank updates — keep existing value (avoids wiping optional keys
        # and breaking float() reload with empty strings).
        if not str(value).strip():
            continue
        set_key(str(ENV_PATH), key, str(value).strip())
        os.environ[key] = str(value).strip()
    # Reload config module globals used by the pipeline
    load_dotenv(ENV_PATH, override=True)
    import shorts_generator.config as cfg

    cfg.MUAPI_API_KEY = os.getenv("MUAPI_API_KEY", "").strip()
    cfg.MUAPI_BASE_URL = (
        os.getenv("MUAPI_BASE_URL") or "https://api.muapi.ai/api/v1"
    ).strip().rstrip("/")
    cfg.POLL_INTERVAL_SECONDS = _env_float("MUAPI_POLL_INTERVAL", 5.0)
    cfg.POLL_TIMEOUT_SECONDS = _env_float("MUAPI_POLL_TIMEOUT", 600.0)
    cfg.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    cfg.OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    cfg.GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    cfg.GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
    cfg.LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()
    cfg.LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL") or "base"
    cfg.LOCAL_WHISPER_DEVICE = os.getenv("LOCAL_WHISPER_DEVICE") or "auto"
    cfg.LOCAL_OUTPUT_DIR = os.getenv("LOCAL_OUTPUT_DIR") or "output"
    cfg.LOCAL_WHISPER_VAD_FILTER = (
        os.getenv("LOCAL_WHISPER_VAD_FILTER", "false").strip().lower() == "true"
    )
    try:
        cfg.LOCAL_FACE_SMOOTHING = max(
            0.0, min(1.0, _env_float("LOCAL_FACE_SMOOTHING", 0.15))
        )
    except (TypeError, ValueError):
        cfg.LOCAL_FACE_SMOOTHING = 0.15
    cfg.CONTENT_LANGUAGE = os.getenv("CONTENT_LANGUAGE", "pt").strip().lower() or "pt"


def _public_short(job_id: str, short: Dict[str, Any], index: int) -> Dict[str, Any]:
    out = dict(short)
    clip = short.get("clip_url") or ""
    hid = _short_id(short, index)
    if clip and not clip.startswith("http"):
        # Serve by highlight id so URLs stay stable as shorts arrive mid-render
        out["clip_url"] = f"/api/jobs/{job_id}/clips/{hid}"
        out["local_path"] = clip
    thumb_path = JOBS_DIR / job_id / "short_thumbs" / f"{hid}.jpg"
    if thumb_path.exists():
        out["thumbnail_url"] = f"/api/jobs/{job_id}/short-thumbs/{hid}?v=2"
    elif out.get("thumbnail_url"):
        pass
    return out


def _public_result(job_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    public = dict(result)
    public["shorts"] = [
        _public_short(job_id, s, i) for i, s in enumerate(result.get("shorts", []))
    ]
    # Enrich highlights with thumbnail URLs for the selection UI
    highlights = public.get("highlights") or []
    if isinstance(highlights, list):
        public["highlights"] = [
            _public_highlight(job_id, h, i) for i, h in enumerate(highlights)
        ]
    speakers = public.get("speakers") or []
    if isinstance(speakers, list):
        public["speakers"] = [
            _public_speaker(job_id, sp, i) for i, sp in enumerate(speakers)
        ]
    # Preview URL for the source video (local file or remote HTTP)
    ffmpeg_src, _yt_id = _resolve_job_source(
        result, str(result.get("source_url") or "")
    )
    if ffmpeg_src:
        if str(ffmpeg_src).startswith("http"):
            public["source_preview_url"] = ffmpeg_src
        else:
            public["source_preview_url"] = f"/api/jobs/{job_id}/source"
    duration = None
    transcript = public.get("transcript")
    if isinstance(transcript, dict):
        try:
            duration = float(transcript.get("duration") or 0) or None
        except (TypeError, ValueError):
            duration = None
    if duration:
        public["duration"] = duration
    # Truncate transcript segments in API responses for speed
    if isinstance(transcript, dict) and "segments" in transcript:
        segs = transcript["segments"]
        public["transcript"] = {
            "duration": transcript.get("duration"),
            "language": transcript.get("language"),
            "segment_count": len(segs) if isinstance(segs, list) else 0,
        }
    return public


def _public_speaker(job_id: str, speaker: Any, index: int) -> Dict[str, Any]:
    if not isinstance(speaker, dict):
        return {"id": f"S{index + 1}"}
    out = dict(speaker)
    sid = str(out.get("id") or f"S{index + 1}").strip().upper() or f"S{index + 1}"
    portrait = JOBS_DIR / job_id / "cast" / f"{sid}.jpg"
    if portrait.exists():
        try:
            bust = int(portrait.stat().st_mtime)
        except OSError:
            bust = 0
        out["portrait_url"] = f"/api/jobs/{job_id}/cast/{sid}?v={bust}"
    return out


def _public_highlight(job_id: str, highlight: Dict[str, Any], index: int) -> Dict[str, Any]:
    out = dict(highlight)
    hid = int(out.get("id", index))
    preview = JOBS_DIR / job_id / "preview_thumbs" / f"{hid}.jpg"
    thumb_path = JOBS_DIR / job_id / "thumbs" / f"{hid}.jpg"
    # Prefer hook+style poster for topic cards; fall back to raw frame
    if preview.exists():
        out["thumbnail_url"] = f"/api/jobs/{job_id}/preview-thumbs/{hid}?v=2"
    elif thumb_path.exists():
        out["thumbnail_url"] = f"/api/jobs/{job_id}/thumbs/{hid}"
    elif out.get("thumbnail_url"):
        pass  # already set (e.g. YouTube fallback)
    # Always expose a stable preview URL so the UI can lazy-generate
    out["preview_thumbnail_url"] = f"/api/jobs/{job_id}/preview-thumbs/{hid}?v=2"
    return out


def _youtube_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"(?:v=|/shorts/|/embed/|youtu\.be/)([A-Za-z0-9_-]{6,})", url or "")
    return m.group(1) if m else None


def _extract_frame(source: str, timestamp: float, dest: Path) -> bool:
    """Grab a JPEG frame via ffmpeg. Returns True on success."""
    import subprocess

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{max(0.0, float(timestamp)):.3f}",
        "-i", source,
        "-frames:v", "1",
        "-q:v", "3",
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=60)
        return dest.exists() and dest.stat().st_size > 0
    except (subprocess.SubprocessError, OSError, TimeoutError):
        return False


def _ass_colour_to_rgb(ass: str) -> Tuple[int, int, int]:
    """ASS &HAABBGGRR → (R, G, B)."""
    m = re.search(r"&H([0-9A-Fa-f]{8})", str(ass or ""))
    if not m:
        return (255, 255, 255)
    hex8 = m.group(1)
    bb = int(hex8[2:4], 16)
    gg = int(hex8[4:6], 16)
    rr = int(hex8[6:8], 16)
    return (rr, gg, bb)


def _resolve_thumb_font(font_name: str, size: int):
    """Best-effort TTF for karaoke-style short thumbnails."""
    from PIL import ImageFont

    name = (font_name or "Arial Black").strip().lower()
    bold = "black" in name or "bold" in name or "impact" in name
    candidates = []
    if "impact" in name:
        candidates += [
            "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
    if "arial" in name or "helvetica" in name or "black" in name:
        candidates += [
            "/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf",
            "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
    if "dejavu" in name:
        candidates.append("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=max(12, int(size)))
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap_hook_lines(text: str, max_words: int) -> List[str]:
    tokens = [t for t in re.split(r"\s+", str(text or "").strip()) if t]
    if not tokens:
        return []
    max_w = max(1, int(max_words or 4))
    return [" ".join(tokens[i : i + max_w]) for i in range(0, len(tokens), max_w)]


def _measure_line(draw, text: str, font, outline: int) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=outline)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_hook_to_width(
    draw,
    text: str,
    font,
    outline: int,
    max_width: int,
    max_words: int,
) -> List[str]:
    """Wrap hook by both max-words preference and hard pixel width."""
    tokens = [t for t in re.split(r"\s+", str(text or "").strip()) if t]
    if not tokens:
        return []
    max_w = max(1, int(max_words or 4))
    lines: List[str] = []
    i = 0
    while i < len(tokens):
        # Prefer chunks of up to max_words, but shrink if too wide
        take = min(max_w, len(tokens) - i)
        while take > 0:
            candidate = " ".join(tokens[i : i + take]).upper()
            tw, _ = _measure_line(draw, candidate, font, outline)
            if tw <= max_width or take == 1:
                # Single oversized word: keep it (font shrink handles later)
                lines.append(candidate)
                i += take
                break
            take -= 1
        else:
            break
        if len(lines) >= 8:
            break
    return lines


def _draw_hook_on_frame(
    frame_path: Path,
    dest: Path,
    hook: str,
    style: Optional[Dict[str, Any]] = None,
) -> bool:
    """Compose centered karaoke-styled hook over a JPEG frame.

    Text is constrained to the frame width (side margins) so it never
    spills past the chosen aspect-ratio crop.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        try:
            dest.write_bytes(frame_path.read_bytes())
            return dest.exists()
        except OSError:
            return False

    try:
        img = Image.open(frame_path).convert("RGB")
    except OSError:
        return False

    from shorts_generator.captions import resolve_style

    style = resolve_style(style)
    if style.get("enabled") is False:
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="JPEG", quality=90)
        return True

    w, h = img.size
    side_pad = max(12, int(round(w * 0.10)))
    max_text_w = max(40, w - side_pad * 2)
    max_text_h = max(40, int(h * 0.72))

    # Scale ASS design size relative to frame width (PlayRes ~1080)
    scale = w / 1080.0
    base_font = max(16, int(float(style.get("font_size") or 72) * scale))
    outline_base = max(0, int(round(float(style.get("outline") or 4) * scale)))
    max_words = int(style.get("max_words_per_line") or 4)
    fill = _ass_colour_to_rgb(style.get("primary_colour") or "&H0000FFFF")
    stroke = _ass_colour_to_rgb(style.get("outline_colour") or "&H00000000")
    font_name = str(style.get("font_name") or "Arial Black")

    draw = ImageDraw.Draw(img)
    font_size = base_font
    outline = outline_base
    lines: List[str] = []
    heights: List[int] = []
    widths: List[int] = []
    line_gap = 4

    # Shrink font until wrapped text fits inside the safe box
    for _ in range(18):
        font = _resolve_thumb_font(font_name, font_size)
        outline = max(0, int(round(outline_base * (font_size / max(1, base_font)))))
        lines = _wrap_hook_to_width(
            draw, hook, font, outline, max_text_w, max_words
        )
        if not lines:
            dest.parent.mkdir(parents=True, exist_ok=True)
            img.save(dest, format="JPEG", quality=90)
            return True
        heights = []
        widths = []
        for line in lines:
            tw, th = _measure_line(draw, line, font, outline)
            widths.append(tw)
            heights.append(th)
        line_gap = max(3, int(font_size * 0.16))
        total_h = sum(heights) + line_gap * (len(lines) - 1)
        max_line_w = max(widths) if widths else 0
        if max_line_w <= max_text_w and total_h <= max_text_h:
            break
        font_size = max(14, int(font_size * 0.88))
    else:
        font = _resolve_thumb_font(font_name, font_size)

    total_h = sum(heights) + line_gap * max(0, len(lines) - 1)
    y = (h - total_h) / 2
    for i, line in enumerate(lines):
        tw = widths[i]
        th = heights[i]
        # Clamp X inside padded box even if a single word is still slightly wide
        x = max(side_pad, min((w - tw) / 2, w - side_pad - tw))
        draw.text(
            (x, y),
            line,
            font=font,
            fill=fill,
            stroke_width=outline,
            stroke_fill=stroke,
        )
        y += th + line_gap

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="JPEG", quality=90, optimize=True)
    return dest.exists() and dest.stat().st_size > 0


def _center_crop_to_ratio(img, aspect_ratio: str = "9:16"):
    """Center-crop a PIL image to the target aspect ratio."""
    try:
        rw, rh = [max(1, int(x)) for x in str(aspect_ratio or "9:16").split(":")]
        target = rw / rh
    except (TypeError, ValueError, ZeroDivisionError):
        target = 9 / 16
    w, h = img.size
    if w < 2 or h < 2:
        return img
    current = w / h
    if abs(current - target) < 0.01:
        return img
    if current > target:
        new_w = max(1, int(round(h * target)))
        x0 = max(0, (w - new_w) // 2)
        return img.crop((x0, 0, x0 + new_w, h))
    new_h = max(1, int(round(w / target)))
    y0 = max(0, (h - new_h) // 2)
    return img.crop((0, y0, w, y0 + new_h))


def _generate_topic_preview_thumbnail(
    job_id: str,
    highlight: Dict[str, Any],
    *,
    style: Optional[Dict[str, Any]] = None,
    aspect_ratio: str = "9:16",
    source: str = "",
    force: bool = False,
) -> Optional[str]:
    """Poster for topic cards: aspect-cropped frame + centered hook in karaoke style."""
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        hid = int(highlight.get("id", -1))
    except (TypeError, ValueError):
        return None
    if hid < 0:
        return None

    dest = JOBS_DIR / job_id / "preview_thumbs" / f"{hid}.jpg"
    if dest.exists() and not force:
        return f"/api/jobs/{job_id}/preview-thumbs/{hid}"

    frame = JOBS_DIR / job_id / "thumbs" / f"{hid}.jpg"
    tmp = JOBS_DIR / job_id / "preview_thumbs" / f".{hid}.frame.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        if not frame.exists() and source:
            _extract_frame(source, float(highlight.get("start_time") or 0), frame)
        src_frame = frame if frame.exists() else None
        if src_frame is None and source:
            if _extract_frame(source, float(highlight.get("start_time") or 0), tmp):
                src_frame = tmp
        if src_frame is None:
            return None

        img = Image.open(src_frame).convert("RGB")
        img = _center_crop_to_ratio(img, aspect_ratio)
        # Normalize width for readable type (match vertical short feel)
        target_w = 720
        if img.width != target_w:
            target_h = max(1, int(round(target_w * img.height / img.width)))
            img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
        img.save(tmp, format="JPEG", quality=92)

        hook = str(
            highlight.get("hook_sentence") or highlight.get("title") or ""
        ).strip()
        # Always burn hook on topic posters (even if karaoke burn-in is off)
        style_for_thumb = dict(style or {})
        style_for_thumb["enabled"] = True
        if not _draw_hook_on_frame(tmp, dest, hook, style=style_for_thumb):
            return None
        return f"/api/jobs/{job_id}/preview-thumbs/{hid}"
    except Exception:
        return None
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _invalidate_preview_thumbs(job_id: str) -> None:
    folder = JOBS_DIR / job_id / "preview_thumbs"
    if not folder.exists():
        return
    for p in folder.glob("*.jpg"):
        try:
            p.unlink()
        except OSError:
            pass


def _short_clip_path(short: Dict[str, Any]) -> Optional[Path]:
    clip = short.get("clip_url") or short.get("local_path") or ""
    if not clip or str(clip).startswith("http"):
        return None
    path = Path(str(clip))
    if not path.is_absolute():
        path = ROOT / path
    return path if path.exists() else None


def _generate_short_thumbnail(
    job_id: str,
    short: Dict[str, Any],
    *,
    style: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> Optional[str]:
    """Build a poster for a rendered short: clip frame + centered hook.

    Returns public thumbnail URL path or None.
    """
    hid = _short_id(short)
    if hid < 0:
        return None
    dest = JOBS_DIR / job_id / "short_thumbs" / f"{hid}.jpg"
    if dest.exists() and not force:
        return f"/api/jobs/{job_id}/short-thumbs/{hid}"

    clip_path = _short_clip_path(short)
    if clip_path is None:
        return None

    hook = str(short.get("hook_sentence") or short.get("title") or "").strip()
    tmp = JOBS_DIR / job_id / "short_thumbs" / f".{hid}.frame.jpg"
    try:
        # Prefer a frame ~0.8s into the clip so we avoid fade/black opens
        ok = _extract_frame(str(clip_path), 0.8, tmp)
        if not ok:
            ok = _extract_frame(str(clip_path), 0.0, tmp)
        if not ok:
            return None
        if not _draw_hook_on_frame(tmp, dest, hook, style=style):
            return None
        short["thumbnail_url"] = f"/api/jobs/{job_id}/short-thumbs/{hid}"
        return short["thumbnail_url"]
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _ensure_short_thumbnails(
    job_id: str,
    shorts: List[Dict[str, Any]],
    *,
    style: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> None:
    for s in shorts:
        if not isinstance(s, dict):
            continue
        if s.get("error") or not (s.get("clip_url") or s.get("local_path")):
            continue
        try:
            _generate_short_thumbnail(job_id, s, style=style, force=force)
        except Exception:
            # Best-effort — never break the render publish path
            continue


# Face area vs frame: reject noise / giant LED-screen faces.
_FACE_AREA_MIN = 0.004
_FACE_AREA_MAX = 0.22
_FACE_DEDUP_SIM = 0.86  # cosine on 32x32 grayscale fingerprint


def _load_haar_cascade():
    try:
        import cv2  # type: ignore
    except ImportError:
        return None, None
    if not hasattr(cv2, "CascadeClassifier") or not hasattr(cv2, "data"):
        return cv2, None
    cascade_path = getattr(cv2.data, "haarcascades", "") + "haarcascade_frontalface_default.xml"
    if not cascade_path or not Path(cascade_path).exists():
        return cv2, None
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        return cv2, None
    return cv2, cascade


def _face_area_ratio(fw: int, fh: int, frame_w: int, frame_h: int) -> float:
    denom = max(1, int(frame_w) * int(frame_h))
    return (int(fw) * int(fh)) / float(denom)


def _face_is_plausible(fw: int, fh: int, frame_w: int, frame_h: int) -> bool:
    ratio = _face_area_ratio(fw, fh, frame_w, frame_h)
    return _FACE_AREA_MIN <= ratio <= _FACE_AREA_MAX


def _face_fingerprint(crop_bgr) -> Optional[Any]:
    """Compact appearance vector for dedup (no extra ML deps)."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return None
    if crop_bgr is None or getattr(crop_bgr, "size", 0) == 0:
        return None
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    vec = small.astype("float32").ravel()
    vec -= float(vec.mean())
    norm = float(np.linalg.norm(vec))
    if norm < 1e-6:
        return None
    return vec / norm


def _face_similarity(a: Any, b: Any) -> float:
    if a is None or b is None:
        return 0.0
    try:
        import numpy as np  # type: ignore

        return float(np.dot(a, b))
    except Exception:
        return 0.0


def _faces_too_similar(a: Any, b: Any, threshold: float = _FACE_DEDUP_SIM) -> bool:
    return _face_similarity(a, b) >= threshold


def _padded_face_crop(img, face, size: int = 256):
    """Return (resized_bgr, fingerprint) for a Haar face box on ``img``."""
    try:
        import cv2  # type: ignore
    except ImportError:
        return None, None
    h, w = img.shape[:2]
    fx, fy, fw, fh = [int(v) for v in face]
    pad = int(max(fw, fh) * 0.55)
    cx = fx + fw // 2
    cy = fy + fh // 2
    side = min(w, h, max(fw, fh) + 2 * pad)
    if side < 8:
        return None, None
    x0 = max(0, min(w - side, cx - side // 2))
    y0 = max(0, min(h - side, cy - side // 2))
    crop = img[y0 : y0 + side, x0 : x0 + side]
    if crop.size == 0:
        return None, None
    out = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
    return out, _face_fingerprint(out)


def _center_square_crop(img, size: int = 256):
    try:
        import cv2  # type: ignore
    except ImportError:
        return None, None
    h, w = img.shape[:2]
    side = min(w, h)
    x0 = max(0, (w - side) // 2)
    y0 = max(0, (h - side) // 2)
    crop = img[y0 : y0 + side, x0 : x0 + side]
    if crop.size == 0:
        return None, None
    out = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
    return out, _face_fingerprint(out)


def _detect_plausible_faces(img) -> List[Tuple[int, int, int, int]]:
    """Haar faces with studio-ish size; largest-first among survivors."""
    cv2, cascade = _load_haar_cascade()
    if cv2 is None or cascade is None or img is None:
        return []
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    raw = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=4, minSize=(48, 48)
    )
    faces: List[Tuple[int, int, int, int]] = []
    for fx, fy, fw, fh in raw:
        if _face_is_plausible(int(fw), int(fh), w, h):
            faces.append((int(fx), int(fy), int(fw), int(fh)))
    faces.sort(key=lambda f: f[2] * f[3], reverse=True)
    return faces


def _crop_face_square(
    image_path: Path,
    dest: Path,
    size: int = 256,
    *,
    exclude_fingerprints: Optional[List[Any]] = None,
) -> Tuple[bool, bool, Optional[Any]]:
    """Crop a plausible face to a square JPEG; fall back to center crop.

    Returns ``(wrote_file, found_face, fingerprint)``.
    Skips faces too similar to ``exclude_fingerprints`` (other speakers).
    """
    try:
        import cv2  # type: ignore
    except ImportError:
        if image_path.resolve() != dest.resolve():
            try:
                dest.write_bytes(image_path.read_bytes())
                return True, False, None
            except OSError:
                return False, False, None
        return image_path.exists(), False, None

    img = cv2.imread(str(image_path))
    if img is None:
        return False, False, None
    h, w = img.shape[:2]
    if h < 8 or w < 8:
        return False, False, None

    exclude = exclude_fingerprints or []
    dest.parent.mkdir(parents=True, exist_ok=True)

    for face in _detect_plausible_faces(img):
        out, fp = _padded_face_crop(img, face, size=size)
        if out is None:
            continue
        if any(_faces_too_similar(fp, other) for other in exclude):
            continue
        wrote = bool(cv2.imwrite(str(dest), out, [int(cv2.IMWRITE_JPEG_QUALITY), 88]))
        if wrote:
            return True, True, fp

    # Last resort: center crop (may still be useful for UI placeholder)
    out, fp = _center_square_crop(img, size=size)
    if out is None:
        return False, False, None
    if any(_faces_too_similar(fp, other) for other in exclude):
        return False, False, None
    wrote = bool(cv2.imwrite(str(dest), out, [int(cv2.IMWRITE_JPEG_QUALITY), 88]))
    return wrote, False, (fp if wrote else None)


def _resolve_job_source(result: Dict[str, Any], original_url: str = ""):
    """Return (ffmpeg_source_or_None, youtube_id_or_None)."""
    source = result.get("source_video_url") or ""
    yt_id = _youtube_id_from_url(original_url) or _youtube_id_from_url(source)
    local_source = Path(source)
    if not local_source.is_absolute() and source and not source.startswith("http"):
        local_source = ROOT / source
    if local_source.exists() and local_source.is_file():
        return str(local_source), yt_id
    if source.startswith("http"):
        return source, yt_id
    return None, yt_id


def _normalize_quote_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _portrait_candidate_times(
    sp: Dict[str, Any],
    transcript: Dict[str, Any],
    index: int,
    n_speakers: int,
) -> List[float]:
    """Timestamps when this speaker is likely on camera / speaking."""
    segments = list(transcript.get("segments") or [])
    times: List[float] = []

    def _push(t: object) -> None:
        try:
            val = float(t)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if val < 0 or val != val:
            return
        times.append(round(max(0.0, val), 2))

    # 1) Labeled speech turns (available after cast labeling / refresh)
    sid = str(sp.get("id") or "").strip().upper()
    if sid:
        for seg in segments:
            if str(seg.get("speaker_id") or "").strip().upper() != sid:
                continue
            _push(seg.get("start"))
            try:
                start = float(seg.get("start", 0))
                end = float(seg.get("end", start))
            except (TypeError, ValueError):
                continue
            mid = start + max(0.0, (end - start) * 0.35)
            _push(mid)

    # 2) Quote match across the whole transcript (not only intro)
    quote = _normalize_quote_key(str(sp.get("sample_quote") or ""))
    if quote and len(quote) >= 8:
        for seg in segments:
            text = _normalize_quote_key(str(seg.get("text") or ""))
            if quote in text or (len(quote) > 20 and text in quote):
                _push(seg.get("start"))

    # 3) LLM / refined sample_time + nearby offsets
    try:
        base = float(sp.get("sample_time")) if sp.get("sample_time") is not None else None
    except (TypeError, ValueError):
        base = None
    if base is None:
        base = 5.0 * (index + 1)
    for delta in (0.0, 2.0, 5.0, 9.0, 14.0, 22.0, 35.0, 55.0, 90.0, 140.0):
        _push(base + delta)
        if delta:
            _push(max(0.0, base - delta * 0.4))

    # 4) Spread across early segments so slots diverge before labeling
    intro = []
    for seg in segments:
        try:
            start = float(seg.get("start", 0))
        except (TypeError, ValueError):
            continue
        if start > 300.0:
            break
        intro.append(seg)
    if intro:
        step = max(1, len(intro) // max(1, n_speakers))
        idx = min(index * step, len(intro) - 1)
        for j in range(idx, len(intro), max(1, n_speakers)):
            _push(intro[j].get("start"))
            if len(times) > 80:
                break

    # Dedupe near-duplicates while preserving order
    unique: List[float] = []
    for t in times:
        if any(abs(t - u) < 1.2 for u in unique):
            continue
        unique.append(t)
        if len(unique) >= 48:
            break
    return unique or [float(base)]


def _fingerprint_from_portrait_file(path: Path) -> Optional[Any]:
    try:
        import cv2  # type: ignore
    except ImportError:
        return None
    if not path.exists():
        return None
    img = cv2.imread(str(path))
    if img is None:
        return None
    return _face_fingerprint(img)


def _cast_portraits_missing(job_id: str, result: Dict[str, Any]) -> bool:
    speakers = result.get("speakers") or []
    if not isinstance(speakers, list) or not speakers:
        return False
    cast_dir = JOBS_DIR / job_id / "cast"
    for i, sp in enumerate(speakers):
        if not isinstance(sp, dict):
            continue
        sid = str(sp.get("id") or f"S{i + 1}").strip().upper() or f"S{i + 1}"
        if not (cast_dir / f"{sid}.jpg").exists():
            return True
    return False


def _ensure_cast_portraits(job_id: str, job: Dict[str, Any]) -> None:
    """Generate missing speaker portraits for the cast UI (best-effort).

    Must not be called while holding ``_jobs_lock`` (does disk/ffmpeg I/O).
    """
    result = job.get("result")
    if not isinstance(result, dict):
        return
    if not _cast_portraits_missing(job_id, result):
        return
    url = (job.get("params") or {}).get("url") or ""
    try:
        _generate_cast_portraits(job_id, result, original_url=url)
        with _jobs_lock:
            current = _jobs.get(job_id)
            if current is not None and current.get("result") is result:
                _persist_job(current)
    except Exception as e:
        _append_log(job_id, f"Aviso: retratos dos locutores falharam ({e})")


def _pick_speaker_portrait(
    *,
    ffmpeg_src: str,
    sp: Dict[str, Any],
    dest: Path,
    tmp: Path,
    candidates: List[float],
    exclude_fingerprints: List[Any],
    skip_times: Optional[List[float]] = None,
    require_face: bool = True,
) -> Tuple[bool, Optional[float], Optional[Any]]:
    """Try candidate timestamps until a distinct plausible face is written."""
    skipped = skip_times or []
    fallback: Optional[Tuple[bytes, float, Optional[Any]]] = None

    for ts in candidates:
        if any(abs(ts - s) < 0.8 for s in skipped):
            continue
        if not _extract_frame(ffmpeg_src, ts, tmp):
            continue
        wrote, found_face, fp = _crop_face_square(
            tmp, dest, exclude_fingerprints=exclude_fingerprints
        )
        if not wrote:
            continue
        if found_face:
            return True, ts, fp
        if not require_face and fallback is None:
            try:
                fallback = (dest.read_bytes(), ts, fp)
            except OSError:
                pass

    if fallback is not None:
        data, ts, fp = fallback
        try:
            dest.write_bytes(data)
            return True, ts, fp
        except OSError:
            return False, None, None
    return False, None, None


def _generate_cast_portraits(
    job_id: str,
    result: Dict[str, Any],
    original_url: str = "",
    *,
    force: bool = False,
) -> None:
    """Grab a distinct face still for each speaker (best-effort).

    When ``force`` is True, overwrite existing portraits (e.g. after speech labeling).
    """
    speakers = result.get("speakers") or []
    if not isinstance(speakers, list) or not speakers:
        return
    ffmpeg_src, _yt_id = _resolve_job_source(result, original_url)
    if not ffmpeg_src:
        return

    try:
        from shorts_generator.cast import attach_speaker_sample_times

        attach_speaker_sample_times(speakers, result.get("transcript") or {})
    except Exception:
        pass

    cast_dir = JOBS_DIR / job_id / "cast"
    cast_dir.mkdir(parents=True, exist_ok=True)
    transcript = result.get("transcript") or {}
    n = len(speakers)
    used_fps: List[Any] = []
    made = 0

    for i, sp in enumerate(speakers):
        if not isinstance(sp, dict):
            continue
        sid = str(sp.get("id") or f"S{i + 1}").strip().upper() or f"S{i + 1}"
        dest = cast_dir / f"{sid}.jpg"
        tmp = cast_dir / f".{sid}.full.jpg"
        if not force and dest.exists():
            fp_existing = _fingerprint_from_portrait_file(dest)
            if fp_existing is not None:
                used_fps.append(fp_existing)
            sp["portrait_url"] = f"/api/jobs/{job_id}/cast/{sid}"
            made += 1
            continue
        if force:
            sp.pop("portrait_skip_times", None)
        candidates = _portrait_candidate_times(sp, transcript, i, n)
        skip = (
            []
            if force
            else [float(t) for t in (sp.get("portrait_skip_times") or []) if t is not None]
        )
        ok, ts, fp = _pick_speaker_portrait(
            ffmpeg_src=ffmpeg_src,
            sp=sp,
            dest=dest,
            tmp=tmp,
            candidates=candidates,
            exclude_fingerprints=used_fps,
            skip_times=skip,
            require_face=True,
        )
        if not ok:
            # Relax: allow center-crop if nothing better, still avoid dup faces
            ok, ts, fp = _pick_speaker_portrait(
                ffmpeg_src=ffmpeg_src,
                sp=sp,
                dest=dest,
                tmp=tmp,
                candidates=candidates,
                exclude_fingerprints=used_fps,
                skip_times=skip,
                require_face=False,
            )
        if not ok and used_fps:
            # Absolute last resort: any face/frame so the UI isn't empty
            ok, ts, fp = _pick_speaker_portrait(
                ffmpeg_src=ffmpeg_src,
                sp=sp,
                dest=dest,
                tmp=tmp,
                candidates=candidates,
                exclude_fingerprints=[],
                skip_times=skip,
                require_face=False,
            )
        if ok:
            sp["portrait_url"] = f"/api/jobs/{job_id}/cast/{sid}"
            if ts is not None:
                sp["portrait_time"] = round(float(ts), 2)
            if fp is not None:
                used_fps.append(fp)
            made += 1
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    if made:
        print(f"[cast] portraits ready: {made}/{len(speakers)}", flush=True)


def _advance_cast_portrait(
    job_id: str,
    result: Dict[str, Any],
    speaker_id: str,
    original_url: str = "",
) -> Dict[str, Any]:
    """Cycle to the next distinct face for one speaker (UI 'Trocar foto')."""
    speakers = result.get("speakers") or []
    if not isinstance(speakers, list):
        raise HTTPException(404, "Locutor não encontrado")

    sid = re.sub(r"[^A-Za-z0-9_-]", "", speaker_id or "").upper()
    target: Optional[Dict[str, Any]] = None
    target_index = -1
    for i, sp in enumerate(speakers):
        if not isinstance(sp, dict):
            continue
        cur = str(sp.get("id") or f"S{i + 1}").strip().upper()
        if cur == sid:
            target = sp
            target_index = i
            break
    if target is None:
        raise HTTPException(404, "Locutor não encontrado")

    ffmpeg_src, _yt_id = _resolve_job_source(result, original_url)
    if not ffmpeg_src:
        raise HTTPException(400, "Vídeo fonte indisponível para extrair frame")

    try:
        from shorts_generator.cast import attach_speaker_sample_times

        attach_speaker_sample_times(speakers, result.get("transcript") or {})
    except Exception:
        pass

    cast_dir = JOBS_DIR / job_id / "cast"
    cast_dir.mkdir(parents=True, exist_ok=True)
    dest = cast_dir / f"{sid}.jpg"
    tmp = cast_dir / f".{sid}.full.jpg"

    # Fingerprints of the *other* speakers' current portraits
    exclude: List[Any] = []
    for i, sp in enumerate(speakers):
        if not isinstance(sp, dict) or i == target_index:
            continue
        other_id = str(sp.get("id") or f"S{i + 1}").strip().upper()
        fp = _fingerprint_from_portrait_file(cast_dir / f"{other_id}.jpg")
        if fp is not None:
            exclude.append(fp)

    skip: List[float] = []
    for t in target.get("portrait_skip_times") or []:
        try:
            skip.append(float(t))
        except (TypeError, ValueError):
            pass
    try:
        cur_t = float(target.get("portrait_time")) if target.get("portrait_time") is not None else None
    except (TypeError, ValueError):
        cur_t = None
    if cur_t is not None and not any(abs(cur_t - s) < 0.8 for s in skip):
        skip.append(cur_t)

    candidates = _portrait_candidate_times(
        target, result.get("transcript") or {}, target_index, len(speakers)
    )
    # Extra sweep if user keeps clicking: denser grid over first ~8 min
    for extra in range(0, 480, 8):
        candidates.append(float(extra))
    # Dedupe candidate list
    dense: List[float] = []
    for t in candidates:
        if any(abs(t - u) < 1.0 for u in dense):
            continue
        dense.append(t)

    ok, ts, _fp = _pick_speaker_portrait(
        ffmpeg_src=ffmpeg_src,
        sp=target,
        dest=dest,
        tmp=tmp,
        candidates=dense,
        exclude_fingerprints=exclude,
        skip_times=skip,
        require_face=True,
    )
    if not ok:
        # Wrap around: clear skips except nothing, try again requiring face
        ok, ts, _fp = _pick_speaker_portrait(
            ffmpeg_src=ffmpeg_src,
            sp=target,
            dest=dest,
            tmp=tmp,
            candidates=dense,
            exclude_fingerprints=exclude,
            skip_times=[],
            require_face=True,
        )
        skip = []
    if not ok:
        ok, ts, _fp = _pick_speaker_portrait(
            ffmpeg_src=ffmpeg_src,
            sp=target,
            dest=dest,
            tmp=tmp,
            candidates=dense,
            exclude_fingerprints=exclude,
            skip_times=[],
            require_face=False,
        )
        skip = []

    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass

    if not ok:
        raise HTTPException(500, "Não foi possível extrair outro frame")

    if ts is not None:
        skip.append(float(ts))
        target["portrait_time"] = round(float(ts), 2)
    # Cap skip history so wrap-around stays possible
    target["portrait_skip_times"] = [round(float(t), 2) for t in skip[-40:]]
    try:
        bust = int(dest.stat().st_mtime)
    except OSError:
        bust = 0
    target["portrait_url"] = f"/api/jobs/{job_id}/cast/{sid}?v={bust}"
    return target


def _generate_thumbnails(job_id: str, result: Dict[str, Any], original_url: str = "") -> None:
    """Best-effort frame grabs for each highlight; YouTube poster as fallback."""
    ffmpeg_src, yt_id = _resolve_job_source(result, original_url)
    use_ffmpeg = bool(ffmpeg_src)

    for i, h in enumerate(result.get("highlights") or []):
        hid = int(h.get("id", i))
        dest = JOBS_DIR / job_id / "thumbs" / f"{hid}.jpg"
        ok = False
        if use_ffmpeg and ffmpeg_src:
            ok = _extract_frame(ffmpeg_src, float(h.get("start_time", 0)), dest)
        if not ok and yt_id:
            h["thumbnail_url"] = f"https://i.ytimg.com/vi/{yt_id}/hqdefault.jpg"


def _run_analyze(job_id: str) -> None:
    from shorts_generator import prepare_video

    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "analyzing"
        job["updated_at"] = _now()
        params = dict(job["params"])
        _persist_job(job)

    _append_log(job_id, f"Preparando vídeo (mode={params['mode']})…")
    capture = LogCapture(job_id, sys.stdout)
    err_capture = LogCapture(job_id, sys.stderr)

    try:
        with redirect_stdout(capture), redirect_stderr(err_capture):
            result = prepare_video(
                youtube_url=params["url"],
                download_format=params["download_format"],
                language=params.get("language") or None,
                mode=params["mode"],
            )
        speakers = result.get("speakers") or []
        try:
            _generate_cast_portraits(
                job_id, result, original_url=params.get("url") or ""
            )
        except Exception as e:
            _append_log(job_id, f"Aviso: retratos dos locutores falharam ({e})")
        with _jobs_lock:
            job = _jobs[job_id]
            job["status"] = "awaiting_cast"
            job["result"] = result
            job["error"] = None
            job["updated_at"] = _now()
            _persist_job(job)
        _append_log(
            job_id,
            f"Transcrição pronta — {len(speakers)} locutor(es) detectado(s). "
            "Confirme os nomes para gerar os títulos.",
        )
    except Exception as e:
        tb = traceback.format_exc()
        with _jobs_lock:
            job = _jobs[job_id]
            job["status"] = "failed"
            job["error"] = str(e)
            job["updated_at"] = _now()
            job["finished_at"] = _now()
            _persist_job(job)
        _append_log(job_id, f"ERRO: {e}")
        _append_log(job_id, tb)


def _run_rank_highlights(
    job_id: str,
    speaker_names: Dict[str, str],
    *,
    skip_cast: bool = False,
) -> None:
    from shorts_generator import finalize_analysis

    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "ranking"
        job["updated_at"] = _now()
        params = dict(job["params"])
        prepared = dict(job.get("result") or {})
        _persist_job(job)

    _append_log(job_id, "Rotulando falas e ranqueando tópicos virais…")
    capture = LogCapture(job_id, sys.stdout)
    err_capture = LogCapture(job_id, sys.stderr)

    try:
        with redirect_stdout(capture), redirect_stderr(err_capture):
            result = finalize_analysis(
                prepared,
                speaker_names=speaker_names,
                skip_cast=skip_cast,
                num_clips=None,
                language=params.get("language") or None,
            )
        # Refine portraits using labeled speech turns (speaker_id on segments)
        if not skip_cast and (result.get("speakers") or []):
            try:
                _generate_cast_portraits(
                    job_id,
                    result,
                    original_url=params.get("url") or "",
                    force=True,
                )
            except Exception as e:
                _append_log(job_id, f"Aviso: retratos pós-rótulo falharam ({e})")
        _append_log(job_id, "Gerando miniaturas dos tópicos…")
        _generate_thumbnails(job_id, result, original_url=params.get("url") or "")
        with _jobs_lock:
            job = _jobs[job_id]
            job["status"] = "awaiting_selection"
            job["result"] = result
            job["error"] = None
            job["updated_at"] = _now()
            _persist_job(job)
        _append_log(
            job_id,
            f"Análise pronta: {len(result.get('highlights', []))} tópicos — "
            "selecione quais cortar.",
        )
    except Exception as e:
        tb = traceback.format_exc()
        with _jobs_lock:
            job = _jobs[job_id]
            # Keep prepared transcript/speakers so the user can retry naming
            job["status"] = "awaiting_cast"
            job["error"] = str(e)
            job["updated_at"] = _now()
            _persist_job(job)
        _append_log(job_id, f"ERRO no ranking: {e}")
        _append_log(job_id, tb)
        _append_log(job_id, "Ajuste os nomes dos locutores e tente novamente.")


def _short_id(short: Dict[str, Any], fallback: int = -1) -> int:
    try:
        return int(short.get("id", fallback))
    except (TypeError, ValueError):
        return fallback


def _build_render_result(
    analysis: Dict[str, Any],
    selected_ids: List[int],
    done_by_id: Dict[int, Dict[str, Any]],
    *,
    phase: str,
    current_id: Optional[int] = None,
) -> Dict[str, Any]:
    shorts = [done_by_id[sid] for sid in selected_ids if sid in done_by_id]
    pending_ids = [
        sid for sid in selected_ids if sid not in done_by_id and sid != current_id
    ]
    return {
        "mode": analysis.get("mode"),
        "phase": phase,
        "source_video_url": analysis.get("source_video_url"),
        "source_url": analysis.get("source_url"),
        "metadata": analysis.get("metadata") or {},
        "transcript": analysis.get("transcript"),
        "speakers": analysis.get("speakers") or [],
        "highlights": analysis.get("highlights") or [],
        "selected_ids": list(selected_ids),
        "shorts": shorts,
        "render_progress": {
            "total": len(selected_ids),
            "done": len(done_by_id),
            "current_id": current_id,
            "pending_ids": pending_ids,
            "done_ids": [sid for sid in selected_ids if sid in done_by_id],
        },
    }


def _run_render(
    job_id: str,
    selected_ids: List[int],
    *,
    force_all: bool = False,
) -> None:
    from shorts_generator import render_selected_shorts

    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "rendering"
        job["updated_at"] = _now()
        params = dict(job["params"])
        analysis = dict(job.get("result") or {})
        job["params"]["selected_ids"] = selected_ids
        force = force_all or bool(params.get("force_rerender"))
        job["params"]["force_rerender"] = False
        if force and job.get("result"):
            job["result"] = dict(job["result"])
            job["result"]["shorts"] = []
            analysis = dict(job["result"])
        _persist_job(job)

    existing = analysis.get("shorts") or []
    desired_style = params.get("caption_style")
    existing_by_id = {}
    for i, s in enumerate(existing):
        if not s.get("clip_url") or s.get("error"):
            continue
        sid = _short_id(s, i)
        # Re-render when karaoke style no longer matches
        if desired_style and s.get("caption_style") != desired_style:
            continue
        existing_by_id[sid] = s
    if force:
        reuse_ids: List[int] = []
        render_ids = list(selected_ids)
    else:
        reuse_ids = [sid for sid in selected_ids if sid in existing_by_id]
        render_ids = [sid for sid in selected_ids if sid not in existing_by_id]

    dropped = [s for s in existing if _short_id(s) not in set(selected_ids)]
    if reuse_ids and not render_ids:
        _append_log(
            job_id,
            f"Seleção atualizada: {len(reuse_ids)} shorts reaproveitados, "
            f"{len(dropped)} removidos — nada novo para cortar.",
        )
    elif reuse_ids:
        _append_log(
            job_id,
            f"Cortando {len(render_ids)} novos · "
            f"reaproveitando {len(reuse_ids)} · removendo {len(dropped)}…",
        )
    else:
        _append_log(job_id, f"Cortando {len(render_ids)} tópicos selecionados…")

    done_by_id: Dict[int, Dict[str, Any]] = {
        sid: existing_by_id[sid] for sid in reuse_ids
    }

    def publish(phase: str, current_id: Optional[int] = None) -> None:
        result = _build_render_result(
            analysis, selected_ids, done_by_id, phase=phase, current_id=current_id
        )
        with _jobs_lock:
            job = _jobs[job_id]
            job["result"] = result
            job["updated_at"] = _now()
            if phase == "completed":
                job["status"] = "completed"
                job["error"] = None
                job["finished_at"] = _now()
            else:
                job["status"] = "rendering"
            _persist_job(job)

    first_current = render_ids[0] if render_ids else None
    publish("rendering", current_id=first_current)

    capture = LogCapture(job_id, sys.stdout)
    err_capture = LogCapture(job_id, sys.stderr)

    try:
        new_shorts: List[Dict[str, Any]] = []
        if render_ids:
            remaining = list(render_ids)

            def on_short_done(short: Dict[str, Any], _i: int, _total: int) -> None:
                sid = _short_id(short)
                try:
                    _generate_short_thumbnail(
                        job_id, short, style=desired_style, force=True
                    )
                except Exception:
                    pass
                done_by_id[sid] = short
                new_shorts.append(short)
                if sid in remaining:
                    remaining.remove(sid)
                next_id = remaining[0] if remaining else None
                publish("rendering", current_id=next_id)
                title = short.get("title") or f"#{sid}"
                if short.get("error"):
                    _append_log(job_id, f"Falhou short {sid}: {title} — {short['error']}")
                else:
                    _append_log(
                        job_id,
                        f"Pronto {len(done_by_id)}/{len(selected_ids)}: {title}",
                    )

            with redirect_stdout(capture), redirect_stderr(err_capture):
                partial = render_selected_shorts(
                    analysis,
                    render_ids,
                    aspect_ratio=params.get("aspect_ratio") or "9:16",
                    on_short_done=on_short_done,
                    caption_style=params.get("caption_style"),
                )
            # Ensure any shorts from the batch are present even if callback skipped
            for s in partial.get("shorts") or []:
                done_by_id[_short_id(s)] = s

        # Backfill posters for reused/new shorts (hook + karaoke style)
        _ensure_short_thumbnails(
            job_id,
            list(done_by_id.values()),
            style=desired_style,
            force=False,
        )

        publish("completed", current_id=None)
        _append_log(
            job_id,
            f"Concluído: {len(done_by_id)} shorts "
            f"({len(new_shorts)} novos, {len(reuse_ids)} reaproveitados).",
        )
    except Exception as e:
        tb = traceback.format_exc()
        with _jobs_lock:
            job = _jobs[job_id]
            job["status"] = "failed"
            job["error"] = str(e)
            job["updated_at"] = _now()
            job["finished_at"] = _now()
            # Keep partial shorts so the UI can show what already rendered
            if done_by_id:
                job["result"] = _build_render_result(
                    analysis, selected_ids, done_by_id, phase="failed"
                )
            _persist_job(job)
        _append_log(job_id, f"ERRO: {e}")
        _append_log(job_id, tb)


class SelectHighlights(BaseModel):
    ids: List[int] = Field(default_factory=list)
    force: bool = False
    caption_style: Optional[Dict[str, Any]] = None


class CastSpeakerName(BaseModel):
    id: str
    name: str = ""


class ConfirmCast(BaseModel):
    speakers: List[CastSpeakerName] = Field(default_factory=list)
    skip: bool = False


class UpdateJobParams(BaseModel):
    aspect_ratio: Optional[str] = None
    download_format: Optional[str] = None
    regenerate: bool = True
    caption_style: Optional[Dict[str, Any]] = None
    ui_step: Optional[int] = None
    selected_ids: Optional[List[int]] = None


class UpdateHighlightTimes(BaseModel):
    start_time: float
    end_time: float
    title: Optional[str] = None
    attributed_to: Optional[str] = None


@app.get("/api/health")
def health():
    cfg = _read_config()
    return {"ok": True, "config": cfg["status"]}


@app.get("/api/caption-themes")
def caption_themes():
    from shorts_generator.captions import list_themes, resolve_style

    return {
        "themes": list_themes(),
        "default": resolve_style({"theme": "bold-white"}),
    }

def _fmt_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.0f} {u}" if u == "B" else f"{size:.1f} {u}"
        size /= 1024
    return f"{n} B"


def _read_source_meta(meta_path: Path) -> Optional[str]:
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    title = (data or {}).get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


def _write_source_meta(meta_path: Path, video_id: str, title: str) -> None:
    try:
        meta_path.write_text(
            json.dumps({"id": video_id, "title": title}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _fetch_youtube_title(video_id: str) -> Optional[str]:
    """Resolve title via YouTube oEmbed (no API key)."""
    from urllib.error import URLError, HTTPError
    from urllib.parse import quote
    from urllib.request import Request, urlopen

    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    oembed = f"https://www.youtube.com/oembed?url={quote(watch_url, safe='')}&format=json"
    req = Request(oembed, headers={"User-Agent": "ShortsLab/1.0"})
    try:
        with urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None
    title = data.get("title") if isinstance(data, dict) else None
    return title.strip() if isinstance(title, str) and title.strip() else None


def _resolve_youtube_title(video_id: str, out_dir: Path, youtube_url: Optional[str] = None) -> str:
    meta_path = out_dir / f"source_{video_id}.meta.json"
    cached = _read_source_meta(meta_path)
    if cached:
        return cached
    title = _fetch_youtube_title(video_id)
    if title:
        _write_source_meta(meta_path, video_id, title)
        return title
    return youtube_url or f"YouTube {video_id}"


def _list_recent_sources(limit: int = 12) -> List[Dict[str, Any]]:
    """Cached downloads + recent job URLs so the UI can re-run without re-pasting."""
    from shorts_generator.config import LOCAL_OUTPUT_DIR

    out_dir = Path(LOCAL_OUTPUT_DIR)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    # video_id / url → metadata from jobs (last used, mode)
    job_meta: Dict[str, Dict[str, Any]] = {}
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j.get("created_at") or "", reverse=True)
    for j in jobs:
        params = j.get("params") or {}
        url = (params.get("url") or "").strip()
        if not url:
            continue
        key = url
        m = re.search(r"(?:v=|/shorts/|/embed/|youtu\.be/)([A-Za-z0-9_-]{6,})", url)
        if m:
            key = m.group(1)
        if key not in job_meta:
            job_meta[key] = {
                "url": url,
                "mode": params.get("mode") or "local",
                "last_job_id": j.get("id"),
                "last_used_at": j.get("created_at") or j.get("updated_at"),
                "last_status": j.get("status"),
            }

    items: List[Dict[str, Any]] = []
    seen: set = set()

    if out_dir.exists():
        for path in out_dir.glob("source_*.*"):
            if path.suffix.lower() not in {".mp4", ".mkv", ".webm", ".mov"}:
                continue
            # source_VIDEOID.ext
            stem = path.stem  # source_QGXD7ip4L2I
            video_id = stem[len("source_") :] if stem.startswith("source_") else stem
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            meta = job_meta.get(video_id) or {}
            youtube_url = meta.get("url") or f"https://www.youtube.com/watch?v={video_id}"
            stat = path.stat()
            srt = path.with_suffix(".srt")
            json_cache = path.with_name(path.stem + ".transcript.json")
            items.append(
                {
                    "id": video_id,
                    "title": _resolve_youtube_title(video_id, out_dir, youtube_url),
                    "url": youtube_url,
                    "local_path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                    "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    "size_bytes": stat.st_size,
                    "size_label": _fmt_size(stat.st_size),
                    "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "has_transcript_cache": json_cache.exists() or srt.exists(),
                    "mode": meta.get("mode") or "local",
                    "last_job_id": meta.get("last_job_id"),
                    "last_used_at": meta.get("last_used_at"),
                    "last_status": meta.get("last_status"),
                    "kind": "cache",
                }
            )

    # Job URLs without a local cache (e.g. API mode / remote-only)
    for key, meta in job_meta.items():
        url = meta["url"]
        if key in seen:
            continue
        if url.startswith("/") or url.startswith("file:") or Path(url).exists():
            # local upload path from a past job
            p = Path(url)
            if not p.is_absolute():
                p = ROOT / p
            if not p.exists():
                continue
            seen.add(key)
            stat = p.stat()
            items.append(
                {
                    "id": key[:16],
                    "title": p.name,
                    "url": str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p),
                    "local_path": str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p),
                    "thumbnail": None,
                    "size_bytes": stat.st_size,
                    "size_label": _fmt_size(stat.st_size),
                    "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "has_transcript_cache": False,
                    "mode": "local",
                    "last_job_id": meta.get("last_job_id"),
                    "last_used_at": meta.get("last_used_at"),
                    "last_status": meta.get("last_status"),
                    "kind": "upload",
                }
            )
            continue
        if not url.startswith("http"):
            continue
        seen.add(key)
        is_yt_id = bool(re.fullmatch(r"[A-Za-z0-9_-]{6,}", key))
        title = _resolve_youtube_title(key, out_dir, url) if is_yt_id else url
        items.append(
            {
                "id": key if len(key) <= 16 else key[:16],
                "title": title,
                "url": url,
                "local_path": None,
                "thumbnail": (
                    f"https://i.ytimg.com/vi/{key}/hqdefault.jpg" if is_yt_id else None
                ),
                "size_bytes": None,
                "size_label": None,
                "mtime": meta.get("last_used_at"),
                "has_transcript_cache": False,
                "mode": meta.get("mode") or "api",
                "last_job_id": meta.get("last_job_id"),
                "last_used_at": meta.get("last_used_at"),
                "last_status": meta.get("last_status"),
                "kind": "recent",
            }
        )

    def sort_key(it: Dict[str, Any]):
        return it.get("last_used_at") or it.get("mtime") or ""

    items.sort(key=sort_key, reverse=True)
    return items[:limit]


@app.get("/api/sources")
def list_sources():
    return {"sources": _list_recent_sources()}


@app.get("/api/config")
def get_config():
    return _read_config()


@app.put("/api/config")
def put_config(body: ConfigUpdate):
    _write_config(body.values)
    return _read_config()


@app.get("/api/jobs")
def list_jobs():
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
        return [
            {
                "id": j["id"],
                "status": j["status"],
                "params": j["params"],
                "created_at": j["created_at"],
                "updated_at": j["updated_at"],
                "finished_at": j.get("finished_at"),
                "error": j.get("error"),
                "shorts_count": len(j["result"]["shorts"]) if j.get("result") else 0,
                "highlights_count": len(j["result"]["highlights"]) if j.get("result") else 0,
            }
            for j in jobs
        ]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, include_logs: bool = True):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        needs_portraits = (
            job.get("status") == "awaiting_cast"
            and isinstance(job.get("result"), dict)
            and _cast_portraits_missing(job_id, job["result"])
        )
        job_ref = job if needs_portraits else None

    # Backfill portraits outside the lock (ffmpeg can take a second or two)
    if job_ref is not None:
        _ensure_cast_portraits(job_id, job_ref)

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        payload = {
            "id": job["id"],
            "status": job["status"],
            "params": job["params"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "finished_at": job.get("finished_at"),
            "error": job.get("error"),
            "logs": job["logs"] if include_logs else [],
            "result": _public_result(job_id, job["result"]) if job.get("result") else None,
        }
        return payload


@app.get("/api/jobs/{job_id}/result.json")
def download_result_json(job_id: str):
    path = JOBS_DIR / f"{job_id}_result.json"
    if not path.exists():
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job or not job.get("result"):
                raise HTTPException(404, "Resultado não disponível")
            path.write_text(json.dumps(job["result"], indent=2, default=str), encoding="utf-8")
    return FileResponse(path, media_type="application/json", filename=f"shorts_{job_id}.json")


@app.get("/api/jobs/{job_id}/source")
def get_job_source(job_id: str):
    """Serve the prepared source video for in-browser trim preview."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise HTTPException(404, "Job/resultado não encontrado")
        result = job["result"]
        original = str((job.get("params") or {}).get("url") or result.get("source_url") or "")
    ffmpeg_src, _yt_id = _resolve_job_source(result, original)
    if not ffmpeg_src:
        raise HTTPException(404, "Vídeo fonte não disponível")
    if str(ffmpeg_src).startswith("http"):
        return JSONResponse({"redirect": ffmpeg_src})
    path = Path(ffmpeg_src)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, f"Arquivo não encontrado: {path}")
    suffix = path.suffix.lower()
    media = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
    }.get(suffix, "application/octet-stream")
    return FileResponse(path, media_type=media, filename=path.name)


@app.patch("/api/jobs/{job_id}/highlights/{highlight_id}")
def update_highlight_times(job_id: str, highlight_id: int, body: UpdateHighlightTimes):
    """Adjust start/end, title and/or speaker of a highlight before cutting shorts."""
    start = float(body.start_time)
    end = float(body.end_time)
    if start < 0:
        raise HTTPException(400, "start_time não pode ser negativo")
    if end <= start:
        raise HTTPException(400, "end_time deve ser maior que start_time")
    if end - start < 1.0:
        raise HTTPException(400, "O corte precisa ter pelo menos 1 segundo")

    new_title: Optional[str] = None
    if body.title is not None:
        new_title = str(body.title).strip()
        if not new_title:
            raise HTTPException(400, "title não pode ser vazio")
        if len(new_title) > 200:
            raise HTTPException(400, "title muito longo (máx. 200)")

    new_speaker: Optional[str] = None
    if body.attributed_to is not None:
        new_speaker = str(body.attributed_to).strip()
        if len(new_speaker) > 120:
            raise HTTPException(400, "attributed_to muito longo (máx. 120)")

    thumb_job: Optional[Tuple[str, Dict[str, Any], str]] = None
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        if not _job_allows_selection(job):
            raise HTTPException(
                400,
                f"Não é possível editar tópicos agora (status={job['status']})",
            )
        result = job.get("result")
        if not isinstance(result, dict):
            raise HTTPException(400, "Job ainda não tem análise")
        highlights = result.get("highlights") or []
        target = None
        target_index = -1
        for i, h in enumerate(highlights):
            if int(h.get("id", i)) == int(highlight_id):
                target = h
                target_index = i
                break
        if target is None:
            raise HTTPException(404, f"Tópico #{highlight_id} não encontrado")

        transcript = result.get("transcript") or {}
        try:
            duration = float(transcript.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0 and end > duration + 0.05:
            raise HTTPException(
                400, f"end_time ({end:.1f}s) ultrapassa a duração do vídeo ({duration:.1f}s)"
            )

        prev_start = float(target.get("start_time", 0))
        prev_end = float(target.get("end_time", 0))
        times_changed = abs(prev_start - start) > 0.01 or abs(prev_end - end) > 0.01

        target["start_time"] = round(start, 3)
        target["end_time"] = round(end, 3)
        if new_title is not None:
            target["title"] = new_title
        if new_speaker is not None:
            target["attributed_to"] = new_speaker
        highlights[target_index] = target
        result["highlights"] = highlights

        invalidated = False
        if times_changed:
            shorts = result.get("shorts") or []
            kept = [
                s
                for i, s in enumerate(shorts)
                if _short_id(s, i) != int(highlight_id)
            ]
            if len(kept) != len(shorts):
                result["shorts"] = kept
                invalidated = True
            # Drop stale thumbnail so the UI refreshes after regen
            thumb_path = JOBS_DIR / job_id / "thumbs" / f"{int(highlight_id)}.jpg"
            if thumb_path.exists():
                try:
                    thumb_path.unlink()
                except OSError:
                    pass

        job["result"] = result
        job["updated_at"] = _now()
        if invalidated and job.get("status") == "completed":
            # Selection still valid; user must re-cut this topic
            job["status"] = "awaiting_selection"
            job["finished_at"] = None
        _persist_job(job)
        public_h = _public_highlight(job_id, target, target_index)
        original = str((job.get("params") or {}).get("url") or result.get("source_url") or "")
        if times_changed:
            thumb_job = (job_id, dict(result), original)

    if thumb_job is not None:
        tid, res_copy, original_url = thumb_job
        threading.Thread(
            target=_regen_highlight_thumb,
            args=(tid, int(highlight_id), res_copy, original_url),
            daemon=True,
        ).start()

    return {
        "id": job_id,
        "highlight": public_h,
        "invalidated_short": invalidated,
    }


def _regen_highlight_thumb(
    job_id: str, highlight_id: int, result: Dict[str, Any], original_url: str
) -> None:
    """Best-effort thumbnail refresh after the user trims a highlight."""
    ffmpeg_src, yt_id = _resolve_job_source(result, original_url)
    dest = JOBS_DIR / job_id / "thumbs" / f"{highlight_id}.jpg"
    h = None
    for i, item in enumerate(result.get("highlights") or []):
        if int(item.get("id", i)) == int(highlight_id):
            h = item
            break
    if h is None:
        return
    ok = False
    if ffmpeg_src:
        ok = _extract_frame(ffmpeg_src, float(h.get("start_time", 0)), dest)
    if not ok and yt_id:
        # Leave YouTube poster as fallback via public highlight enrichment
        return
    if ok:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job or not isinstance(job.get("result"), dict):
                return
            for i, item in enumerate(job["result"].get("highlights") or []):
                if int(item.get("id", i)) == int(highlight_id):
                    item["thumbnail_url"] = f"/api/jobs/{job_id}/thumbs/{highlight_id}"
                    break
            job["updated_at"] = _now()
            _persist_job(job)


@app.get("/api/jobs/{job_id}/clips/{clip_ref}")
def get_clip(job_id: str, clip_ref: int):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise HTTPException(404, "Job/resultado não encontrado")
        shorts = job["result"].get("shorts", [])
        clip = ""
        # Prefer highlight id (stable during progressive render)
        for i, s in enumerate(shorts):
            if _short_id(s, i) == clip_ref:
                clip = s.get("clip_url") or ""
                break
        else:
            # Legacy: treat as array index
            if 0 <= clip_ref < len(shorts):
                clip = shorts[clip_ref].get("clip_url") or ""
            else:
                raise HTTPException(404, "Clip não encontrado")
        if not clip:
            raise HTTPException(404, "Clip não encontrado")
    if clip.startswith("http"):
        return JSONResponse({"redirect": clip})
    path = Path(clip)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise HTTPException(404, f"Arquivo não encontrado: {path}")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/jobs/{job_id}/preview-thumbs/{index}")
def get_preview_thumb(job_id: str, index: int):
    """Topic-card poster: aspect crop + hook text in karaoke style (lazy)."""
    path = JOBS_DIR / job_id / "preview_thumbs" / f"{index}.jpg"
    if path.exists():
        return FileResponse(
            path, media_type="image/jpeg", filename=f"preview_thumb_{index}.jpg"
        )

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise HTTPException(404, "Miniatura não encontrada")
        params = dict(job.get("params") or {})
        style = params.get("caption_style")
        aspect = params.get("aspect_ratio") or "9:16"
        result = job["result"]
        highlights = result.get("highlights") or []
        target = None
        for i, h in enumerate(highlights):
            try:
                hid = int(h.get("id", i))
            except (TypeError, ValueError):
                hid = i
            if hid == int(index):
                target = dict(h)
                break
        ffmpeg_src, _yt = _resolve_job_source(
            result, str((params.get("url") or result.get("source_url") or ""))
        )

    if not target:
        raise HTTPException(404, "Tópico não encontrado")

    url = _generate_topic_preview_thumbnail(
        job_id,
        target,
        style=style,
        aspect_ratio=str(aspect),
        source=str(ffmpeg_src or ""),
        force=True,
    )
    if not url or not path.exists():
        # Fall back to raw highlight frame if styled poster fails
        raw = JOBS_DIR / job_id / "thumbs" / f"{index}.jpg"
        if raw.exists():
            return FileResponse(
                raw, media_type="image/jpeg", filename=f"thumb_{index}.jpg"
            )
        raise HTTPException(404, "Não foi possível gerar a miniatura")
    return FileResponse(
        path, media_type="image/jpeg", filename=f"preview_thumb_{index}.jpg"
    )


@app.get("/api/jobs/{job_id}/thumbs/{index}")
def get_thumb(job_id: str, index: int):
    path = JOBS_DIR / job_id / "thumbs" / f"{index}.jpg"
    if not path.exists():
        raise HTTPException(404, "Miniatura não encontrada")
    return FileResponse(path, media_type="image/jpeg", filename=f"thumb_{index}.jpg")


@app.get("/api/jobs/{job_id}/short-thumbs/{index}")
def get_short_thumb(job_id: str, index: int):
    path = JOBS_DIR / job_id / "short_thumbs" / f"{index}.jpg"
    if path.exists():
        return FileResponse(
            path, media_type="image/jpeg", filename=f"short_thumb_{index}.jpg"
        )

    # Lazy generate from an already-rendered clip
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise HTTPException(404, "Miniatura do short não encontrada")
        shorts = job["result"].get("shorts") or []
        style = (job.get("params") or {}).get("caption_style")
        target = None
        for i, s in enumerate(shorts):
            if _short_id(s, i) == int(index):
                target = dict(s)
                break
    if not target:
        raise HTTPException(404, "Short não encontrado")
    url = _generate_short_thumbnail(job_id, target, style=style, force=True)
    if not url or not path.exists():
        raise HTTPException(404, "Não foi possível gerar a miniatura")
    # Persist URL onto the in-memory short for later API responses
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job and job.get("result"):
            for i, s in enumerate(job["result"].get("shorts") or []):
                if _short_id(s, i) == int(index):
                    s["thumbnail_url"] = url
                    break
            _persist_job(job)
    return FileResponse(
        path, media_type="image/jpeg", filename=f"short_thumb_{index}.jpg"
    )


@app.get("/api/jobs/{job_id}/cast/{speaker_id}")
def get_cast_portrait(job_id: str, speaker_id: str):
    sid = re.sub(r"[^A-Za-z0-9_-]", "", speaker_id or "").upper()
    if not sid:
        raise HTTPException(404, "Retrato não encontrado")
    path = JOBS_DIR / job_id / "cast" / f"{sid}.jpg"
    if not path.exists():
        raise HTTPException(404, "Retrato não encontrado")
    return FileResponse(path, media_type="image/jpeg", filename=f"cast_{sid}.jpg")


@app.post("/api/jobs/{job_id}/cast/{speaker_id}/next-portrait")
def next_cast_portrait(job_id: str, speaker_id: str):
    """Advance to another face frame for this speaker (cast UI correction)."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        if job.get("status") != "awaiting_cast":
            raise HTTPException(
                400,
                f"Só é possível trocar foto enquanto identifica locutores (status={job['status']})",
            )
        result = job.get("result")
        if not isinstance(result, dict):
            raise HTTPException(400, "Job sem resultado de análise")
        url = (job.get("params") or {}).get("url") or ""

    updated = _advance_cast_portrait(job_id, result, speaker_id, original_url=url)

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        if job.get("status") != "awaiting_cast":
            raise HTTPException(400, "Job não está mais na etapa de locutores")
        current = job.get("result")
        if current is not result and isinstance(current, dict):
            # Job result object replaced — apply URL onto matching speaker if present
            sid = str(updated.get("id") or "").upper()
            for sp in current.get("speakers") or []:
                if isinstance(sp, dict) and str(sp.get("id") or "").upper() == sid:
                    sp["portrait_url"] = updated.get("portrait_url")
                    sp["portrait_time"] = updated.get("portrait_time")
                    sp["portrait_skip_times"] = updated.get("portrait_skip_times")
                    break
        job["updated_at"] = _now()
        _persist_job(job)

    return {
        "id": job_id,
        "speaker_id": updated.get("id"),
        "portrait_url": updated.get("portrait_url"),
        "portrait_time": updated.get("portrait_time"),
    }


def _sorted_selection_ids(highlights: List[Dict[str, Any]], ids: List[int]) -> List[int]:
    by_id = {int(h.get("id", i)): h for i, h in enumerate(highlights)}
    return sorted(ids, key=lambda i: float(by_id[i].get("start_time", 0)))


def _job_highlights(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list((job.get("result") or {}).get("highlights") or [])


def _job_allows_selection(job: Dict[str, Any]) -> bool:
    """Selection is allowed after analysis, including recoverable failures."""
    status = job.get("status")
    if status in ("awaiting_selection", "completed"):
        return True
    # Render/analyze interrupted or failed after analysis — user can retry cut
    if status == "failed" and _job_highlights(job):
        return True
    return False


@app.post("/api/jobs/{job_id}/cast")
def confirm_cast(job_id: str, body: ConfirmCast):
    """Confirm speaker names (or skip) and continue to highlight ranking."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        if job.get("status") != "awaiting_cast":
            raise HTTPException(
                400,
                f"Job não está aguardando locutores (status={job['status']})",
            )
        result = job.get("result") or {}
        if not result.get("transcript"):
            raise HTTPException(400, "Job ainda não tem transcrição")
        names = {
            str(s.id).strip().upper(): (s.name or "").strip()
            for s in (body.speakers or [])
            if str(s.id).strip()
        }

    thread = threading.Thread(
        target=_run_rank_highlights,
        args=(job_id, names),
        kwargs={"skip_cast": bool(body.skip)},
        daemon=True,
    )
    thread.start()
    return {"id": job_id, "status": "ranking", "skip": bool(body.skip)}


@app.post("/api/jobs/{job_id}/select")
def select_highlights(job_id: str, body: SelectHighlights):
    from shorts_generator.captions import resolve_style

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        if not _job_allows_selection(job):
            raise HTTPException(
                400,
                f"Job não permite nova seleção (status={job['status']})",
            )
        highlights = _job_highlights(job)
        if not highlights:
            raise HTTPException(400, "Job ainda não tem tópicos analisados")
        valid_ids = {int(h.get("id", i)) for i, h in enumerate(highlights)}
        ids = [int(i) for i in (body.ids or [])]
        if not ids:
            raise HTTPException(400, "Selecione ao menos um tópico")
        bad = [i for i in ids if i not in valid_ids]
        if bad:
            raise HTTPException(400, f"IDs inválidos: {bad}")
        ids = _sorted_selection_ids(highlights, ids)
        style = resolve_style(body.caption_style if body.caption_style is not None else job["params"].get("caption_style"))
        prev_style = job["params"].get("caption_style")
        style_changed = prev_style != style
        job["params"]["caption_style"] = style
        force = bool(body.force) or bool(job["params"].get("force_rerender")) or style_changed
        # Clear prior failure so UI reflects a fresh render attempt
        if job["status"] == "failed":
            job["status"] = "awaiting_selection"
            job["error"] = None
        job["updated_at"] = _now()
        _persist_job(job)

    thread = threading.Thread(
        target=_run_render,
        args=(job_id, ids),
        kwargs={"force_all": force},
        daemon=True,
    )
    thread.start()
    return {
        "id": job_id,
        "status": "rendering",
        "selected_ids": ids,
        "force": force,
        "caption_style": style,
    }


@app.get("/api/jobs/{job_id}/caption-words")
def get_caption_words(job_id: str, start: float = 0.0, end: float = 0.0):
    """Word timestamps for karaoke preview over [start, end] (absolute source time)."""
    from shorts_generator.captions import words_from_transcript

    if end <= start:
        raise HTTPException(400, "end deve ser maior que start")
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        result = job.get("result") or {}
        transcript = result.get("transcript")
        if not isinstance(transcript, dict):
            raise HTTPException(404, "Transcrição indisponível")
        # Copy under lock — words extraction can be CPU-light but avoid mutating
        transcript = dict(transcript)

    rel = words_from_transcript(transcript, float(start), float(end))
    words = [
        {
            "word": w["word"],
            "start": float(start) + float(w["start"]),
            "end": float(start) + float(w["end"]),
        }
        for w in rel
    ]
    return {"start": float(start), "end": float(end), "words": words}


@app.patch("/api/jobs/{job_id}/params")
def update_job_params(job_id: str, body: UpdateJobParams):
    """Update render params (e.g. aspect ratio). Changing format forces full re-crop."""
    aspect = (body.aspect_ratio or "").strip()
    if not aspect:
        raise HTTPException(400, "Informe aspect_ratio")
    if aspect not in ("9:16", "1:1", "4:5", "16:9"):
        raise HTTPException(400, "aspect_ratio inválido")

    fmt = (body.download_format or "").strip() or None
    if fmt is not None and fmt not in ("360", "480", "720", "1080"):
        raise HTTPException(400, "download_format inválido")

    if body.ui_step is not None and body.ui_step not in (1, 2, 3, 4, 5):
        raise HTTPException(400, "ui_step inválido")

    log_msg = None
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        if not _job_allows_selection(job):
            raise HTTPException(
                400,
                f"Não é possível alterar params agora (status={job['status']})",
            )
        prev = (job["params"].get("aspect_ratio") or "9:16").strip()
        prev_fmt = (job["params"].get("download_format") or "720").strip()
        changed = prev != aspect
        fmt_changed = bool(fmt and fmt != prev_fmt)
        job["params"]["aspect_ratio"] = aspect
        if fmt:
            job["params"]["download_format"] = fmt
        ui_changed = False
        if body.ui_step is not None:
            step = int(body.ui_step)
            ui_changed = job["params"].get("ui_step") != step
            job["params"]["ui_step"] = step
            job["params"]["flow_version"] = 2
        sel_changed = False
        if body.selected_ids is not None:
            new_sel = [int(i) for i in body.selected_ids]
            prev_sel = job["params"].get("selected_ids")
            if not isinstance(prev_sel, list) or [int(i) for i in prev_sel] != new_sel:
                sel_changed = True
            job["params"]["selected_ids"] = new_sel
        style_changed = False
        if body.caption_style is not None:
            from shorts_generator.captions import resolve_style

            style = resolve_style(body.caption_style)
            if job["params"].get("caption_style") != style:
                style_changed = True
            job["params"]["caption_style"] = style
        selected = [int(i) for i in (job["params"].get("selected_ids") or [])]
        if not selected and job.get("result"):
            selected = [
                _short_id(s, i)
                for i, s in enumerate(job["result"].get("shorts") or [])
            ]
        should_render = bool(body.regenerate and changed and selected)
        if changed or style_changed:
            # Aspect/style change invalidates hook posters on topic cards
            _invalidate_preview_thumbs(job_id)
        if style_changed:
            short_thumb_dir = JOBS_DIR / job_id / "short_thumbs"
            if short_thumb_dir.exists():
                for p in short_thumb_dir.glob("*.jpg"):
                    try:
                        p.unlink()
                    except OSError:
                        pass
        if changed or fmt_changed:
            if changed:
                job["params"]["force_rerender"] = True
                # Drop existing crops so UI doesn't show stale aspect until re-render
                if job.get("result") and not should_render:
                    job["result"] = dict(job["result"])
                    job["result"]["shorts"] = []
                    job["status"] = "awaiting_selection"
            job["updated_at"] = _now()
            _persist_job(job)
            parts = []
            if changed:
                parts.append(f"Proporção: {prev} → {aspect}")
            if fmt_changed:
                parts.append(f"Resolução salva: {fmt} (vale no próximo download)")
            if should_render:
                parts.append("regenerando todos os shorts…")
            log_msg = " · ".join(parts) if parts else None
        elif ui_changed or sel_changed or style_changed:
            job["updated_at"] = _now()
            _persist_job(job)
            return {
                "id": job_id,
                "status": job["status"],
                "params": job["params"],
                "changed": False,
                "ui_step": job["params"].get("ui_step"),
                "selected_ids": job["params"].get("selected_ids"),
            }
        else:
            return {
                "id": job_id,
                "status": job["status"],
                "params": job["params"],
                "changed": False,
            }

    if log_msg:
        _append_log(job_id, log_msg)

    if should_render:
        thread = threading.Thread(
            target=_run_render,
            args=(job_id, selected),
            kwargs={"force_all": True},
            daemon=True,
        )
        thread.start()
        return {
            "id": job_id,
            "status": "rendering",
            "params": {"aspect_ratio": aspect, "download_format": fmt or prev_fmt},
            "changed": True,
            "regenerating": True,
            "selected_ids": selected,
        }

    with _jobs_lock:
        job = _jobs[job_id]
        return {
            "id": job_id,
            "status": job["status"],
            "params": job["params"],
            "changed": True,
            "regenerating": False,
        }


@app.post("/api/jobs")
async def create_job(
    url: Optional[str] = Form(None),
    mode: str = Form("api"),
    aspect_ratio: str = Form("9:16"),
    download_format: str = Form("720"),
    language: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    mode = (mode or "api").lower().strip()
    if mode not in ("api", "local"):
        raise HTTPException(400, "mode deve ser 'api' ou 'local'")
    if download_format not in ("360", "480", "720", "1080"):
        raise HTTPException(400, "format inválido")

    source = (url or "").strip()
    if file and file.filename:
        if mode != "local":
            raise HTTPException(400, "Upload de arquivo só é suportado no modo local")
        safe = re.sub(r"[^\w.\-]", "_", file.filename)
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{safe}"
        content = await file.read()
        dest.write_bytes(content)
        source = str(dest)

    if not source:
        raise HTTPException(400, "Informe uma URL do YouTube ou envie um arquivo")

    # Prefer explicit language if sent; otherwise use CONTENT_LANGUAGE from Config
    # (saved in .env and applied to all future generations).
    lang = (language or "").strip() or None
    if not lang:
        import shorts_generator.config as cfg

        lang = (cfg.CONTENT_LANGUAGE or "pt").strip().lower() or "pt"
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "queued",
        "params": {
            "url": source,
            "mode": mode,
            "aspect_ratio": aspect_ratio.strip() or "9:16",
            "download_format": download_format,
            "language": lang,
            "original_filename": file.filename if file and file.filename else None,
        },
        "logs": [],
        "result": None,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
        "finished_at": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        _persist_job(job)

    thread = threading.Thread(target=_run_analyze, args=(job_id,), daemon=True)
    thread.start()
    return {"id": job_id, "status": "queued"}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    for p in (JOBS_DIR / f"{job_id}.json", JOBS_DIR / f"{job_id}_result.json"):
        if p.exists():
            p.unlink()
    thumb_dir = JOBS_DIR / job_id
    if thumb_dir.exists() and thumb_dir.is_dir():
        import shutil

        shutil.rmtree(thumb_dir, ignore_errors=True)
    return {"ok": True}


def _resolve_short_upload_path(short: Dict[str, Any], job_id: str) -> Path:
    """Return a local mp4 path for upload (download remote clips to a temp file)."""
    import shutil

    import requests

    local = _short_clip_path(short)
    if local is not None:
        return local

    clip = short.get("clip_url") or short.get("local_path") or ""
    if not str(clip).startswith("http"):
        raise HTTPException(400, "Clip local indisponível para upload")

    dest_dir = JOBS_DIR / job_id / "youtube_upload"
    dest_dir.mkdir(parents=True, exist_ok=True)
    hid = _short_id(short)
    dest = dest_dir / f"short_{hid if hid >= 0 else 'x'}.mp4"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    try:
        with requests.get(str(clip), stream=True, timeout=120) as resp:
            resp.raise_for_status()
            tmp = dest.with_suffix(".part")
            with open(tmp, "wb") as fh:
                shutil.copyfileobj(resp.raw, fh)
            tmp.replace(dest)
    except Exception as exc:
        raise HTTPException(502, f"Falha ao baixar clip remoto: {exc}") from exc
    return dest


@app.post("/api/jobs/{job_id}/shorts/{short_id}/youtube")
def upload_short_to_youtube(
    job_id: str, short_id: int, body: Optional[YoutubeUploadBody] = None
):
    """Upload a rendered short to the connected YouTube channel."""
    from shorts_generator import youtube_uploader as yt

    if not yt.credentials_configured():
        raise HTTPException(
            400,
            "YouTube não configurado. Defina YOUTUBE_CLIENT_ID, "
            "YOUTUBE_CLIENT_SECRET e YOUTUBE_REFRESH_TOKEN no .env "
            "(python scripts/youtube_oauth.py).",
        )

    body = body or YoutubeUploadBody()

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise HTTPException(404, "Job/resultado não encontrado")
        shorts = job["result"].get("shorts") or []
        target = None
        target_index = -1
        for i, s in enumerate(shorts):
            if _short_id(s, i) == int(short_id):
                target = s
                target_index = i
                break
        if target is None:
            raise HTTPException(404, "Short não encontrado")
        if target.get("error"):
            raise HTTPException(400, "Este short falhou na renderização")
        if not (target.get("clip_url") or target.get("local_path")):
            raise HTTPException(400, "Clip ainda não está pronto")
        # Re-upload allowed; keep previous URL until success
        short_snapshot = dict(target)

    path = _resolve_short_upload_path(short_snapshot, job_id)
    title = (body.title or short_snapshot.get("title") or f"Short #{short_id + 1}").strip()
    description = (body.description or "").strip() or yt.build_description(
        hook=str(short_snapshot.get("hook_sentence") or ""),
        reason=str(short_snapshot.get("virality_reason") or ""),
    )
    privacy = (body.privacy or "").strip() or None

    _append_log(job_id, f"Upload YouTube iniciado: short #{short_id} → {path.name}")
    try:
        uploaded = yt.upload_video(
            path,
            title=title,
            description=description,
            privacy=privacy,
        )
    except Exception as exc:
        _append_log(job_id, f"Upload YouTube falhou (short #{short_id}): {exc}")
        raise HTTPException(502, f"Falha no upload YouTube: {exc}") from exc

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise HTTPException(404, "Job sumiu durante o upload")
        shorts = job["result"].get("shorts") or []
        updated = None
        for i, s in enumerate(shorts):
            if _short_id(s, i) == int(short_id) or i == target_index:
                s["youtube_video_id"] = uploaded["video_id"]
                s["youtube_url"] = uploaded["url"]
                s["youtube_privacy"] = uploaded["privacy_status"]
                s["youtube_uploaded_at"] = _now()
                updated = s
                break
        if updated is None:
            raise HTTPException(404, "Short não encontrado após upload")
        job["updated_at"] = _now()
        _persist_job(job)
        public = _public_short(job_id, updated, target_index if target_index >= 0 else 0)

    _append_log(
        job_id,
        f"Upload YouTube ok: short #{short_id} → {uploaded['url']} ({uploaded['privacy_status']})",
    )
    return {
        "ok": True,
        "video_id": uploaded["video_id"],
        "url": uploaded["url"],
        "privacy_status": uploaded["privacy_status"],
        "title": uploaded["title"],
        "short": public,
    }


def _load_persisted_jobs() -> None:
    for path in JOBS_DIR.glob("*.json"):
        if path.name.endswith("_result.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            job_id = data.get("id") or path.stem
            result_path = JOBS_DIR / f"{job_id}_result.json"
            if result_path.exists():
                data["result"] = json.loads(result_path.read_text(encoding="utf-8"))
            data.setdefault("logs", [])
            # Interrupted in-flight runs: recover to selection if analysis exists
            if data.get("status") in (
                "queued",
                "analyzing",
                "ranking",
                "rendering",
                "running",
            ):
                if _job_highlights(data):
                    data["status"] = "awaiting_selection"
                    data["error"] = None
                    data["logs"].append(
                        {
                            "ts": _now(),
                            "message": (
                                "Servidor reiniciou durante o processamento — "
                                "análise preservada; selecione os tópicos novamente."
                            ),
                        }
                    )
                    data["updated_at"] = _now()
                    _persist_job(data)
                elif data.get("status") == "ranking" and (data.get("result") or {}).get(
                    "transcript"
                ):
                    data["status"] = "awaiting_cast"
                    data["error"] = None
                    data["logs"].append(
                        {
                            "ts": _now(),
                            "message": (
                                "Servidor reiniciou durante o ranking — "
                                "confirme os locutores novamente."
                            ),
                        }
                    )
                    data["updated_at"] = _now()
                    _persist_job(data)
                else:
                    data["status"] = "failed"
                    data["error"] = (
                        data.get("error") or "Interrompido (servidor reiniciou)"
                    )
                    data["updated_at"] = _now()
                    _persist_job(data)
            # awaiting_cast / awaiting_selection / completed / failed stay as-is
            _jobs[job_id] = data
        except Exception:
            continue


_load_persisted_jobs()


def _spa_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/")
def spa_root():
    return _spa_index()


@app.get("/jobs")
@app.get("/config")
def spa_pages():
    return _spa_index()


@app.get("/jobs/{job_id}")
def spa_job_page(job_id: str):
    # SPA deep-link; API lives under /api/jobs/{job_id}
    return _spa_index()


app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")
