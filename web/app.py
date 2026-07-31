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
PROJECTS_DIR = ROOT / "projects"
STATIC_DIR = Path(__file__).resolve().parent / "static"

sys.path.insert(0, str(ROOT))
load_dotenv(ENV_PATH)

from .projects import ProjectStore  # noqa: E402

UPLOAD_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)
PROJECTS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="AI YouTube Shorts Generator", version="1.0.0")

# In-memory job store (also persisted as JSON under jobs/)
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_projects = ProjectStore(PROJECTS_DIR)

# Async AI thumbnail tasks: key = f"{job_id}:{highlight_id}"
_thumb_tasks: Dict[str, Dict[str, Any]] = {}
_thumb_tasks_lock = threading.Lock()

# Editable from the web Config UI. API keys, Whisper, LLM providers stay in .env only.
# YouTube channel credentials live per-project (see /api/projects).
CONFIG_KEYS = [
    "CONTENT_LANGUAGE",
    "LOCAL_OUTPUT_DIR",
    "LOCAL_FACE_SMOOTHING",
    "THUMBNAIL_PALETTE",
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


class ProjectCreate(BaseModel):
    name: str = Field(default="Novo canal", min_length=1, max_length=120)


class ProjectYoutubeUpdate(BaseModel):
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    refresh_token: Optional[str] = None
    privacy_status: Optional[str] = None
    channel_title: Optional[str] = None
    channel_id: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    youtube: Optional[ProjectYoutubeUpdate] = None
    virality_profile: Optional[Dict[str, Any]] = None


class YoutubeUploadBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    privacy: Optional[str] = None
    tags: Optional[List[str]] = None
    category_id: Optional[str] = None


class GenerateThumbnailBody(BaseModel):
    """Optional face plate + overlay text for frame-based thumbnails."""

    face_candidate_id: Optional[str] = Field(default=None, max_length=64)
    mode: Optional[str] = Field(default=None, max_length=16)
    overlay_text: Optional[str] = Field(default=None, max_length=120)
    text_color_mode: Optional[str] = Field(default=None, max_length=16)
    margin_v: Optional[int] = Field(default=None, ge=0, le=900)
    font_size: Optional[int] = Field(default=None, ge=40, le=140)
    border_pct: Optional[float] = Field(default=None, ge=1, le=8)
    # sync=True keeps the old blocking behaviour (tests / debugging)
    sync: bool = False


def _normalize_thumb_color_mode(raw: Optional[str]) -> str:
    mode = (raw or "caption").strip().lower()
    return "palette" if mode == "palette" else "caption"


def _normalize_thumb_margin_v(raw: Optional[int]) -> Optional[int]:
    if raw is None:
        return None
    try:
        return max(40, min(720, int(raw)))
    except (TypeError, ValueError):
        return None


def _normalize_thumb_font_size(raw: Optional[int]) -> Optional[int]:
    if raw is None:
        return None
    try:
        return max(40, min(140, int(raw)))
    except (TypeError, ValueError):
        return None


def _normalize_thumb_border_pct(raw: Optional[float]) -> Optional[float]:
    if raw is None:
        return None
    try:
        return max(1.0, min(8.0, float(raw)))
    except (TypeError, ValueError):
        return None


def _thumb_task_key(job_id: str, highlight_id: int) -> str:
    return f"{job_id}:{int(highlight_id)}"


def _public_thumb_task(task: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "ok": True,
        "task_id": task.get("task_id"),
        "job_id": task.get("job_id"),
        "highlight_id": task.get("highlight_id"),
        "short_id": task.get("highlight_id"),
        "status": task.get("status"),
        "error": task.get("error"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "face_candidate_id": task.get("face_candidate_id"),
        "mode": task.get("mode"),
        "overlay_text": task.get("overlay_text"),
        "text_color_mode": task.get("text_color_mode"),
        "margin_v": task.get("margin_v"),
        "font_size": task.get("font_size"),
        "border_pct": task.get("border_pct"),
    }
    result = task.get("result")
    if isinstance(result, dict):
        out.update(
            {
                "thumbnail_url": result.get("thumbnail_url"),
                "thumbnail_ai": result.get("thumbnail_ai"),
                "has_custom_thumbnail": result.get("has_custom_thumbnail"),
                "faces": result.get("faces") or [],
                "cited_people": result.get("cited_people") or [],
                "wiki_hits": result.get("wiki_hits") or [],
                "wiki_skip_reason": result.get("wiki_skip_reason"),
                "refs_sent_to_ai": result.get("refs_sent_to_ai") or [],
                "refs_sent_count": result.get("refs_sent_count") or 0,
                "short": result.get("short"),
                "highlight": result.get("highlight"),
                "mode": result.get("mode") or out.get("mode"),
                "overlay_text": result.get("overlay_text"),
                "text_color_mode": result.get("text_color_mode"),
                "margin_v": result.get("margin_v"),
                "font_size": result.get("font_size"),
                "border_pct": result.get("border_pct"),
            }
        )
    return out


def _run_thumb_task(task_key: str) -> None:
    """Background worker for AI / cutout thumbnail generation."""
    from shorts_generator.thumbnails_ai import ImageBillingError

    with _thumb_tasks_lock:
        task = _thumb_tasks.get(task_key)
        if not task:
            return
        task["status"] = "running"
        job_id = task["job_id"]
        highlight_id = int(task["highlight_id"])
        face_candidate_id = task.get("face_candidate_id")
        mode = task.get("mode")
        overlay_text = task.get("overlay_text")
        text_color_mode = task.get("text_color_mode")
        margin_v = task.get("margin_v")
        font_size = task.get("font_size")
        border_pct = task.get("border_pct")

    try:
        result = _generate_ai_short_thumbnail(
            job_id,
            highlight_id,
            face_candidate_id=face_candidate_id,
            mode=mode,
            overlay_text=overlay_text,
            text_color_mode=text_color_mode,
            margin_v=margin_v,
            font_size=font_size,
            border_pct=border_pct,
        )
        with _thumb_tasks_lock:
            task = _thumb_tasks.get(task_key)
            if not task:
                return
            task["status"] = "ready"
            task["result"] = result
            task["error"] = None
            task["finished_at"] = _now()
    except ImageBillingError as exc:
        _append_log(job_id, f"Thumbnail IA billing (tópico #{highlight_id}): {exc}")
        with _thumb_tasks_lock:
            task = _thumb_tasks.get(task_key)
            if task:
                task["status"] = "failed"
                task["error"] = str(exc)
                task["http_status"] = 402
                task["finished_at"] = _now()
    except Exception as exc:
        tb = traceback.format_exc()
        _append_log(job_id, f"Thumbnail IA falhou (tópico #{highlight_id}): {exc}")
        _append_log(job_id, tb)
        with _thumb_tasks_lock:
            task = _thumb_tasks.get(task_key)
            if task:
                task["status"] = "failed"
                task["error"] = str(exc)
                task["http_status"] = 500
                task["finished_at"] = _now()


def _enqueue_ai_thumbnail(
    job_id: str,
    highlight_id: int,
    *,
    face_candidate_id: Optional[str] = None,
    mode: Optional[str] = None,
    overlay_text: Optional[str] = None,
    text_color_mode: Optional[str] = None,
    margin_v: Optional[int] = None,
    font_size: Optional[int] = None,
    border_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Start thumbnail generation in a daemon thread; return immediately."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise ValueError("Job/resultado não encontrado")
        highlight, _ = _find_job_highlight(job, highlight_id)
        short, _ = _find_job_short(job, highlight_id)
        if highlight is None and short is None:
            raise ValueError("Tópico não encontrado")

    color_mode = _normalize_thumb_color_mode(text_color_mode)
    margin = _normalize_thumb_margin_v(margin_v)
    size = _normalize_thumb_font_size(font_size)
    border = _normalize_thumb_border_pct(border_pct)

    task_key = _thumb_task_key(job_id, highlight_id)
    with _thumb_tasks_lock:
        existing = _thumb_tasks.get(task_key)
        if existing and existing.get("status") in ("queued", "running"):
            return _public_thumb_task(existing)

        task = {
            "task_id": task_key,
            "job_id": job_id,
            "highlight_id": int(highlight_id),
            "status": "queued",
            "error": None,
            "result": None,
            "face_candidate_id": face_candidate_id,
            "mode": mode,
            "overlay_text": (overlay_text or "").strip() or None,
            "text_color_mode": color_mode,
            "margin_v": margin,
            "font_size": size,
            "border_pct": border,
            "started_at": _now(),
            "finished_at": None,
            "http_status": None,
        }
        _thumb_tasks[task_key] = task

    _append_log(
        job_id,
        f"Thumbnail enfileirada: tópico #{int(highlight_id)}"
        + (f" · frame={face_candidate_id}" if face_candidate_id else "")
        + (f" · texto={task.get('overlay_text')!r}" if task.get("overlay_text") else "")
        + f" · cor={color_mode}"
        + (f" · margin_v={margin}" if margin is not None else "")
        + (f" · font_size={size}" if size is not None else "")
        + (f" · border_pct={border}" if border is not None else ""),
    )
    threading.Thread(
        target=_run_thumb_task,
        args=(task_key,),
        daemon=True,
        name=f"thumb-{task_key}",
    ).start()
    return _public_thumb_task(task)

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
    from shorts_generator.config import (
        DEFAULT_THUMBNAIL_PALETTE,
        LANGUAGE_OPTIONS,
        format_thumbnail_palette,
        parse_thumbnail_palette,
    )

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
        if key == "THUMBNAIL_PALETTE":
            val = format_thumbnail_palette(parse_thumbnail_palette(val or None))
            if not val:
                val = DEFAULT_THUMBNAIL_PALETTE
        secret = key in SECRET_KEYS
        is_set = _is_real_secret(val) if secret else bool(val)
        item: Dict[str, Any] = {
            "key": key,
            "value": "" if secret else val,
            "masked": _mask(val) if secret and is_set else None,
            "is_secret": secret,
            "is_set": is_set,
            "input_type": (
                "language"
                if key == "CONTENT_LANGUAGE"
                else "palette"
                if key == "THUMBNAIL_PALETTE"
                else "text"
            ),
        }
        if key == "LOCAL_OUTPUT_DIR":
            item["resolved_path"] = str(Path(val).expanduser().resolve())
        if key == "THUMBNAIL_PALETTE":
            item["colors"] = [
                f"#{r:02X}{g:02X}{b:02X}" for r, g, b in parse_thumbnail_palette(val)
            ]
        items.append(item)
    muapi = _is_real_secret(raw.get("MUAPI_API_KEY"))
    openai = _is_real_secret(raw.get("OPENAI_API_KEY"))
    gemini = _is_real_secret(raw.get("GEMINI_API_KEY"))
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
            "llm_provider": (raw.get("LLM_PROVIDER") or "openai").strip().lower(),
            "content_language": (raw.get("CONTENT_LANGUAGE") or "pt").strip().strip("'\"").lower() or "pt",
            "modes": modes,
            "default_mode": default_mode,
        },
    }


def _require_project(project_id: str) -> Dict[str, Any]:
    project = _projects.get_public(project_id)
    if not project:
        raise HTTPException(404, "Projeto não encontrado")
    return project


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
    from shorts_generator.config import format_thumbnail_palette, parse_thumbnail_palette

    for key, value in updates.items():
        if key not in CONFIG_KEYS:
            continue
        # Skip blank updates — keep existing value (avoids wiping optional keys
        # and breaking float() reload with empty strings).
        if not str(value).strip():
            continue
        cleaned = str(value).strip()
        if key == "THUMBNAIL_PALETTE":
            cleaned = format_thumbnail_palette(parse_thumbnail_palette(cleaned))
        set_key(str(ENV_PATH), key, cleaned)
        os.environ[key] = cleaned
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
    cfg.OPENAI_IMAGE_MODEL = (
        os.getenv("OPENAI_IMAGE_MODEL") or "gpt-image-1-mini"
    ).strip().strip("'\"") or "gpt-image-1-mini"
    _q = (
        os.getenv("OPENAI_IMAGE_QUALITY") or "medium"
    ).strip().strip("'\"").lower() or "medium"
    cfg.OPENAI_IMAGE_QUALITY = _q if _q in ("low", "medium", "high") else "medium"
    _f = (
        os.getenv("OPENAI_IMAGE_FIDELITY") or "low"
    ).strip().strip("'\"").lower() or "low"
    cfg.OPENAI_IMAGE_FIDELITY = (
        _f if _f in ("low", "high", "none", "off") else "low"
    )
    cfg.THUMBNAIL_HYBRID = os.getenv("THUMBNAIL_HYBRID", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    _tm = (
        os.getenv("THUMBNAIL_MODE") or "frame"
    ).strip().strip("'\"").lower() or "frame"
    cfg.THUMBNAIL_MODE = _tm if _tm in ("cutout", "ai", "frame") else "frame"
    _ip = (
        os.getenv("IMAGE_PROVIDER") or "auto"
    ).strip().strip("'\"").lower() or "auto"
    cfg.IMAGE_PROVIDER = _ip if _ip in ("openai", "gemini", "auto") else "auto"
    cfg.GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip().strip("'\"")
    cfg.GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
    cfg.GEMINI_IMAGE_MODEL = (
        os.getenv("GEMINI_IMAGE_MODEL") or "gemini-2.5-flash-image"
    ).strip().strip("'\"") or "gemini-2.5-flash-image"
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
    from shorts_generator.config import DEFAULT_THUMBNAIL_PALETTE

    cfg.THUMBNAIL_PALETTE = format_thumbnail_palette(
        parse_thumbnail_palette(
            os.getenv("THUMBNAIL_PALETTE") or DEFAULT_THUMBNAIL_PALETTE
        )
    )


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
        ver = out.get("thumbnail_version")
        if not ver:
            try:
                ver = int(thumb_path.stat().st_mtime)
            except OSError:
                ver = 2
        out["thumbnail_url"] = f"/api/jobs/{job_id}/short-thumbs/{hid}?v={ver}"
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
    ai_thumb = JOBS_DIR / job_id / "short_thumbs" / f"{hid}.jpg"
    # Prefer AI / custom short poster when present (even before render)
    if out.get("thumbnail_ai") and ai_thumb.exists():
        ver = out.get("thumbnail_version")
        if not ver:
            try:
                ver = int(ai_thumb.stat().st_mtime)
            except OSError:
                ver = 2
        out["thumbnail_url"] = f"/api/jobs/{job_id}/short-thumbs/{hid}?v={ver}"
    elif preview.exists():
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


def _heal_injected_short_thumbnail(job_id: str, short: Dict[str, Any]) -> bool:
    """Restore ``short_thumbs/{id}.jpg`` from frame 0 when a custom poster was
    burned into the clip but the JPG was overwritten right after render.

    Heuristic: only rewrite when the JPG mtime is within ~3 minutes of the
    clip (the old attach bug). Later regenerations keep the newer JPG.
    """
    if not short.get("thumbnail_frame_injected"):
        return False
    if not (short.get("thumbnail_ai") or short.get("thumbnail_ai_meta")):
        return False
    sid = _short_id(short)
    if sid < 0:
        return False
    dest = JOBS_DIR / job_id / "short_thumbs" / f"{sid}.jpg"
    clip_path = _short_clip_path(short)
    if clip_path is None:
        return False
    try:
        if dest.is_file() and dest.stat().st_size > 0:
            delta = dest.stat().st_mtime - clip_path.stat().st_mtime
            if delta < 0 or delta > 180:
                return False
    except OSError:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not _extract_frame(str(clip_path), 0.0, dest):
        return False
    try:
        version = int(dest.stat().st_mtime)
    except OSError:
        version = int(datetime.now(timezone.utc).timestamp())
    short["thumbnail_url"] = f"/api/jobs/{job_id}/short-thumbs/{sid}?v={version}"
    short["thumbnail_version"] = version
    return True


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

    # Custom poster was burned into frame 0 — restore it instead of inventing
    # a new frame+hook composite on top of the video.
    if short.get("thumbnail_frame_injected"):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if _extract_frame(str(clip_path), 0.0, dest):
            try:
                version = int(dest.stat().st_mtime)
            except OSError:
                version = int(datetime.now(timezone.utc).timestamp())
            short["thumbnail_url"] = (
                f"/api/jobs/{job_id}/short-thumbs/{hid}?v={version}"
            )
            short["thumbnail_version"] = version
            return short["thumbnail_url"]

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
    highlights: Optional[List[Dict[str, Any]]] = None,
) -> None:
    for s in shorts:
        if not isinstance(s, dict):
            continue
        if s.get("error") or not (s.get("clip_url") or s.get("local_path")):
            continue
        try:
            if highlights is not None and not force:
                _attach_or_generate_short_thumbnail(
                    job_id, s, highlights=highlights, style=style
                )
            else:
                _generate_short_thumbnail(job_id, s, style=style, force=force)
        except Exception:
            # Best-effort — never break the render publish path
            continue


def _highlight_by_id(
    highlights: List[Dict[str, Any]], highlight_id: int
) -> Optional[Dict[str, Any]]:
    for i, h in enumerate(highlights or []):
        if not isinstance(h, dict):
            continue
        try:
            hid = int(h.get("id", i))
        except (TypeError, ValueError):
            hid = i
        if hid == int(highlight_id):
            return h
    return None


def _attach_or_generate_short_thumbnail(
    job_id: str,
    short: Dict[str, Any],
    *,
    highlights: List[Dict[str, Any]],
    style: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Reuse a pre-generated custom/AI thumbnail when present; otherwise build from clip.

    Any non-empty ``short_thumbs/{id}.jpg`` is treated as authoritative — including
    frame/cutout posters from step 4. Never overwrite those with a lazy
    frame+hook composite (that used to wipe the thumb burned into the MP4).
    """
    sid = _short_id(short)
    if sid < 0:
        return None
    dest = JOBS_DIR / job_id / "short_thumbs" / f"{sid}.jpg"
    hl = _highlight_by_id(highlights, sid)
    ai_meta = None
    if short.get("thumbnail_ai"):
        ai_meta = short.get("thumbnail_ai_meta")
    elif hl and hl.get("thumbnail_ai"):
        ai_meta = hl.get("thumbnail_ai_meta")
        short["thumbnail_ai"] = True
        if ai_meta:
            short["thumbnail_ai_meta"] = ai_meta

    try:
        has_custom = dest.is_file() and dest.stat().st_size > 0
    except OSError:
        has_custom = False

    if has_custom:
        _heal_injected_short_thumbnail(job_id, short)
        try:
            version = int(dest.stat().st_mtime)
        except OSError:
            version = int(datetime.now(timezone.utc).timestamp())
        short["thumbnail_url"] = f"/api/jobs/{job_id}/short-thumbs/{sid}?v={version}"
        short["thumbnail_version"] = version
        # File on disk is the custom poster (AI, cutout, or frame+overlay).
        if short.get("thumbnail_ai") or (hl and hl.get("thumbnail_ai")) or ai_meta:
            short["thumbnail_ai"] = True
        if ai_meta and not short.get("thumbnail_ai_meta"):
            short["thumbnail_ai_meta"] = ai_meta
        elif hl and hl.get("thumbnail_ai_meta") and not short.get("thumbnail_ai_meta"):
            short["thumbnail_ai_meta"] = hl.get("thumbnail_ai_meta")
        return short["thumbnail_url"]

    return _generate_short_thumbnail(job_id, short, style=style, force=True)


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

    project_id = job.get("project_id")
    virality_profile = None
    if project_id:
        try:
            virality_profile = _projects.virality_profile(str(project_id))
        except Exception:
            virality_profile = None

    try:
        with redirect_stdout(capture), redirect_stderr(err_capture):
            result = finalize_analysis(
                prepared,
                speaker_names=speaker_names,
                skip_cast=skip_cast,
                num_clips=None,
                language=params.get("language") or None,
                virality_profile=virality_profile,
                clip_length=params.get("clip_length") or "short",
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
        "rendered_aspect_ratio": analysis.get("rendered_aspect_ratio"),
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
    force_ids: Optional[List[int]] = None,
    preserve_others: bool = False,
) -> None:
    from shorts_generator import render_selected_shorts

    force_set = {int(x) for x in (force_ids or [])}
    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "rendering"
        job["updated_at"] = _now()
        params = dict(job["params"])
        # Reattach mp4s wiped from JSON so resume can reuse them
        if not force_all:
            _ensure_shorts_recovered(job)
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
    if not force and not existing:
        # Disk recovery outside the wipe path (analysis may still be empty)
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                disk = _discover_shorts_on_disk(job)
                if disk:
                    existing = list(disk.values())
    from shorts_generator.captions import resolve_style_for_aspect

    desired_style = resolve_style_for_aspect(params)
    existing_by_id = {}
    for i, s in enumerate(existing):
        if not s.get("clip_url") or s.get("error"):
            continue
        sid = _short_id(s, i)
        if sid in force_set:
            continue
        # Bulk: karaoke style drift invalidates the clip.
        # Append/single (preserve_others): keep siblings even if style differs.
        if (
            desired_style
            and s.get("caption_style") != desired_style
            and not preserve_others
        ):
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
        result["rendered_aspect_ratio"] = params.get("aspect_ratio") or "9:16"
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
                    _attach_or_generate_short_thumbnail(
                        job_id,
                        short,
                        highlights=analysis.get("highlights") or [],
                        style=desired_style,
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
                        f"Pronto {len(done_by_id)}/{len(selected_ids)}: {title}"
                        + (
                            " · thumbnail no 1º frame"
                            if short.get("thumbnail_frame_injected")
                            else ""
                        ),
                    )

            with redirect_stdout(capture), redirect_stderr(err_capture):
                # Stamp pre-existing short_thumbs onto highlights so the local
                # clipper can burn them into frame 0 of the vertical mp4.
                for i, h in enumerate(analysis.get("highlights") or []):
                    if not isinstance(h, dict):
                        continue
                    try:
                        hid = int(h.get("id", i))
                    except (TypeError, ValueError):
                        hid = i
                    if hid not in set(render_ids):
                        continue
                    thumb = JOBS_DIR / job_id / "short_thumbs" / f"{hid}.jpg"
                    if thumb.is_file() and thumb.stat().st_size > 0:
                        h["thumbnail_frame"] = str(thumb)
                    else:
                        h.pop("thumbnail_frame", None)

                partial = render_selected_shorts(
                    analysis,
                    render_ids,
                    aspect_ratio=params.get("aspect_ratio") or "9:16",
                    on_short_done=on_short_done,
                    caption_style=desired_style,
                )
            for s in partial.get("shorts") or []:
                done_by_id[_short_id(s)] = s

        # Backfill posters for reused/new shorts (hook + karaoke style)
        _ensure_short_thumbnails(
            job_id,
            list(done_by_id.values()),
            style=desired_style,
            force=False,
            highlights=analysis.get("highlights") or [],
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
    # Per-aspect karaoke styles: {"9:16": {...}, "16:9": {...}}
    caption_styles: Optional[Dict[str, Any]] = None
    # Render only `ids` but keep other already-done shorts in the result set.
    append: bool = False
    # Continue an interrupted batch — reuse disk clips; ignore stale force_rerender.
    resume: bool = False


class CastSpeakerName(BaseModel):
    id: str
    name: str = ""


class ConfirmCast(BaseModel):
    speakers: List[CastSpeakerName] = Field(default_factory=list)
    skip: bool = False


class UpdateCastRoster(BaseModel):
    speakers: List[CastSpeakerName] = Field(default_factory=list)


class UpdateJobParams(BaseModel):
    aspect_ratio: Optional[str] = None
    download_format: Optional[str] = None
    regenerate: bool = True
    caption_style: Optional[Dict[str, Any]] = None
    # Per-aspect karaoke styles: {"9:16": {...}, "16:9": {...}}
    caption_styles: Optional[Dict[str, Any]] = None
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


@app.get("/api/projects")
def list_projects():
    return _projects.list()


@app.post("/api/projects")
def create_project(body: ProjectCreate):
    return _projects.create(body.name)


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    return _require_project(project_id)


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, body: ProjectUpdate):
    _require_project(project_id)
    youtube = body.youtube.model_dump(exclude_none=True) if body.youtube else None
    try:
        return _projects.update(
            project_id,
            name=body.name,
            youtube=youtube,
            virality_profile=body.virality_profile,
        )
    except KeyError:
        raise HTTPException(404, "Projeto não encontrado") from None


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    if not _projects.delete(project_id):
        raise HTTPException(404, "Projeto não encontrado")
    # Detach jobs from deleted project (keep history)
    with _jobs_lock:
        for job in _jobs.values():
            if job.get("project_id") == project_id:
                job["project_id"] = None
                job["updated_at"] = _now()
                _persist_job(job)
    return {"ok": True}


@app.post("/api/projects/{project_id}/youtube/oauth")
def project_youtube_oauth(project_id: str):
    """Run local desktop OAuth and store the refresh token on the project."""
    project = _projects.get(project_id)
    if not project:
        raise HTTPException(404, "Projeto não encontrado")
    yt = project.get("youtube") or {}
    client_id = str(yt.get("client_id") or "").strip()
    client_secret = str(yt.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(
            400,
            "Salve Client ID e Client Secret do projeto antes de autenticar.",
        )
    from shorts_generator import youtube_uploader as yt_up

    try:
        tokens = yt_up.run_oauth_flow(client_id, client_secret)
    except Exception as exc:
        raise HTTPException(502, f"Falha no OAuth YouTube: {exc}") from exc

    return _projects.set_youtube_tokens(
        project_id,
        refresh_token=tokens["refresh_token"],
        client_id=tokens.get("client_id"),
        client_secret=tokens.get("client_secret"),
        channel_title=tokens.get("channel_title") or "",
        channel_id=tokens.get("channel_id") or "",
    )


@app.get("/api/projects/{project_id}/library")
def project_library(project_id: str):
    """Rendered shorts + selected-but-not-rendered clips for a project."""
    _require_project(project_id)
    rendered: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []

    with _jobs_lock:
        jobs = [
            j
            for j in _jobs.values()
            if j.get("project_id") == project_id
        ]
        jobs = sorted(jobs, key=lambda j: j.get("updated_at") or j.get("created_at") or "", reverse=True)

        for job in jobs:
            job_id = job["id"]
            result = job.get("result") or {}
            params = job.get("params") or {}
            highlights = result.get("highlights") or []
            highlights_by_id: Dict[int, Dict[str, Any]] = {}
            for i, h in enumerate(highlights):
                if not isinstance(h, dict):
                    continue
                try:
                    hid = int(h.get("id", i))
                except (TypeError, ValueError):
                    hid = i
                highlights_by_id[hid] = h

            shorts = result.get("shorts") or []
            ready_ids: set = set()
            for i, s in enumerate(shorts):
                if not isinstance(s, dict):
                    continue
                sid = _short_id(s, i)
                public = _public_short(job_id, s, i)
                has_clip = bool(public.get("clip_url") or public.get("local_path"))
                if has_clip and not s.get("error"):
                    ready_ids.add(sid)
                    hl = highlights_by_id.get(sid) or {}
                    rendered.append(
                        {
                            "job_id": job_id,
                            "short_id": sid,
                            "title": public.get("title") or hl.get("title") or f"Short #{sid}",
                            "score": public.get("score") if public.get("score") is not None else hl.get("score"),
                            "hook_sentence": public.get("hook_sentence") or hl.get("hook_sentence") or "",
                            "start_time": public.get("start_time", hl.get("start_time")),
                            "end_time": public.get("end_time", hl.get("end_time")),
                            "clip_url": public.get("clip_url"),
                            "thumbnail_url": public.get("thumbnail_url")
                            or (hl.get("thumbnail_url") if isinstance(hl, dict) else None)
                            or (
                                f"/api/jobs/{job_id}/preview-thumbs/{sid}?v=2"
                                if (JOBS_DIR / job_id / "preview_thumbs" / f"{sid}.jpg").exists()
                                else None
                            ),
                            "youtube_url": public.get("youtube_url") or "",
                            "youtube_upload_status": public.get("youtube_upload_status") or "",
                            "job_status": job.get("status"),
                            "source_url": params.get("url") or result.get("source_url") or "",
                            "updated_at": job.get("updated_at") or job.get("created_at"),
                        }
                    )
                elif s.get("error"):
                    # failed render still counts as "selected not successfully rendered"
                    pass

            selected_raw = params.get("selected_ids")
            if selected_raw is None:
                selected_raw = result.get("selected_ids") or []
            try:
                selected_ids = [int(x) for x in selected_raw]
            except (TypeError, ValueError):
                selected_ids = []

            # Also treat in-progress render_progress pending as selected
            progress = result.get("render_progress") or {}
            for key in ("pending_ids", "done_ids"):
                for x in progress.get(key) or []:
                    try:
                        n = int(x)
                    except (TypeError, ValueError):
                        continue
                    if n not in selected_ids:
                        selected_ids.append(n)
            cur = progress.get("current_id")
            if cur is not None:
                try:
                    n = int(cur)
                    if n not in selected_ids:
                        selected_ids.append(n)
                except (TypeError, ValueError):
                    pass

            for sid in selected_ids:
                if sid in ready_ids:
                    continue
                hl = highlights_by_id.get(sid) or {}
                short_match = next(
                    (
                        s
                        for i, s in enumerate(shorts)
                        if isinstance(s, dict) and _short_id(s, i) == sid
                    ),
                    None,
                )
                err = (short_match or {}).get("error") if short_match else None
                status = job.get("status") or ""
                if err:
                    pending_status = "failed"
                elif status == "rendering" and progress.get("current_id") == sid:
                    pending_status = "rendering"
                elif status == "rendering":
                    pending_status = "queued"
                elif status == "interrupted":
                    pending_status = "interrupted"
                elif status == "awaiting_selection":
                    pending_status = "selected"
                else:
                    pending_status = status or "pending"

                thumb = None
                if isinstance(hl, dict) and hl.get("thumbnail_url"):
                    thumb = hl.get("thumbnail_url")
                elif (JOBS_DIR / job_id / "preview_thumbs" / f"{sid}.jpg").exists():
                    thumb = f"/api/jobs/{job_id}/preview-thumbs/{sid}?v=2"
                elif (JOBS_DIR / job_id / "thumbs" / f"{sid}.jpg").exists():
                    thumb = f"/api/jobs/{job_id}/thumbs/{sid}"

                pending.append(
                    {
                        "job_id": job_id,
                        "short_id": sid,
                        "title": hl.get("title") or f"Tópico #{sid}",
                        "score": hl.get("score"),
                        "hook_sentence": hl.get("hook_sentence") or "",
                        "start_time": hl.get("start_time"),
                        "end_time": hl.get("end_time"),
                        "thumbnail_url": thumb,
                        "pending_status": pending_status,
                        "error": err,
                        "job_status": status,
                        "source_url": params.get("url") or result.get("source_url") or "",
                        "updated_at": job.get("updated_at") or job.get("created_at"),
                    }
                )

    return {
        "project_id": project_id,
        "rendered": rendered,
        "pending": pending,
        "counts": {"rendered": len(rendered), "pending": len(pending)},
    }


@app.get("/api/jobs")
def list_jobs(project_id: Optional[str] = None):
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
        if project_id:
            jobs = [j for j in jobs if j.get("project_id") == project_id]
        return [
            {
                "id": j["id"],
                "project_id": j.get("project_id"),
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
        if isinstance(job.get("result"), dict) and _ensure_shorts_recovered(job):
            _persist_job(job)
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
            "project_id": job.get("project_id"),
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
        from shorts_generator.captions import resolve_style_for_aspect

        style = resolve_style_for_aspect(params)
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
        from shorts_generator.captions import resolve_style_for_aspect

        style = resolve_style_for_aspect(job.get("params") or {})
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
    allowed = {
        "awaiting_cast",
        "awaiting_selection",
        "completed",
        "interrupted",
        "failed",
        "rendering",
    }
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        status = job.get("status")
        if status not in allowed:
            raise HTTPException(
                400,
                f"Só é possível trocar foto com análise pronta (status={status})",
            )
        result = job.get("result")
        if not isinstance(result, dict):
            raise HTTPException(400, "Job sem resultado de análise")
        speakers = result.get("speakers") or []
        if not isinstance(speakers, list) or not speakers:
            raise HTTPException(400, "Job sem locutores para trocar foto")
        url = (job.get("params") or {}).get("url") or ""

    updated = _advance_cast_portrait(job_id, result, speaker_id, original_url=url)

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        if job.get("status") not in allowed:
            raise HTTPException(400, "Job mudou de status durante a troca de foto")
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


def _job_selected_ids(job: Dict[str, Any]) -> List[int]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    raw = (result or {}).get("selected_ids")
    if not raw:
        raw = (job.get("params") or {}).get("selected_ids") or []
    out: List[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _done_shorts_by_id(result: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    done: Dict[int, Dict[str, Any]] = {}
    for i, s in enumerate(result.get("shorts") or []):
        if not isinstance(s, dict):
            continue
        if s.get("error") or not s.get("clip_url"):
            continue
        done[_short_id(s, i)] = s
    return done


def _job_clips_dir(job: Dict[str, Any]) -> Optional[Path]:
    """Directory where local mp4 shorts for this job are written."""
    from shorts_generator.config import LOCAL_OUTPUT_DIR

    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    for s in (result or {}).get("shorts") or []:
        if not isinstance(s, dict):
            continue
        clip = str(s.get("clip_url") or s.get("local_path") or "").strip()
        if not clip or clip.startswith("http"):
            continue
        parent = Path(clip).expanduser()
        if not parent.is_absolute():
            parent = (ROOT / parent).resolve()
        parent = parent.parent
        if parent.is_dir():
            return parent

    url = str(
        (job.get("params") or {}).get("url")
        or (result or {}).get("source_url")
        or ((result or {}).get("metadata") or {}).get("url")
        or ""
    )
    yt_id = _youtube_id_from_url(url)
    if yt_id:
        candidate = Path(LOCAL_OUTPUT_DIR).expanduser() / yt_id
        if candidate.is_dir():
            return candidate
    return None


def _discover_shorts_on_disk(job: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Find rendered mp4s on disk for this job's highlights (even if result.shorts was wiped)."""
    from shorts_generator.local.clipper import _short_out_path

    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if not result:
        return {}
    clips_dir = _job_clips_dir(job)
    if not clips_dir:
        return {}

    style = None
    params = job.get("params") or {}
    if isinstance(params, dict):
        from shorts_generator.captions import resolve_style_for_aspect

        style = resolve_style_for_aspect(params)
    found: Dict[int, Dict[str, Any]] = {}
    for i, h in enumerate(result.get("highlights") or []):
        if not isinstance(h, dict):
            continue
        sid = _short_id(h, i)
        out_path = Path(_short_out_path(str(clips_dir), h, max(sid, 1)))
        try:
            if not out_path.is_file() or out_path.stat().st_size < 10_000:
                continue
        except OSError:
            continue
        short = {**h, "clip_url": str(out_path)}
        if style:
            short["caption_style"] = style
        found[sid] = short
    return found


def _youtube_urls_from_logs(job: Dict[str, Any]) -> Dict[int, str]:
    """Parse prior YouTube upload success lines so recovery can restore links."""
    out: Dict[int, str] = {}
    for entry in job.get("logs") or []:
        msg = str((entry or {}).get("message") or "")
        m = re.search(
            r"Upload YouTube ok:\s*short\s*#(\d+)\s*→\s*(https://youtu\.be/[A-Za-z0-9_-]+)",
            msg,
        )
        if not m:
            continue
        try:
            out[int(m.group(1))] = m.group(2)
        except (TypeError, ValueError):
            continue
    return out


def _ensure_shorts_recovered(job: Dict[str, Any]) -> bool:
    """Reattach on-disk clips when result.shorts was cleared (aspect toggle, crash, etc.)."""
    result = job.get("result")
    if not isinstance(result, dict):
        return False

    existing = _done_shorts_by_id(result)
    discovered = _discover_shorts_on_disk(job)
    if not discovered:
        return False

    yt_urls = _youtube_urls_from_logs(job)
    merged: Dict[int, Dict[str, Any]] = dict(existing)
    changed = False
    for sid, short in discovered.items():
        prev = merged.get(sid)
        if prev and prev.get("clip_url"):
            # Keep richer metadata (youtube upload) but ensure path still exists
            clip = str(prev.get("clip_url") or "")
            path = Path(clip) if clip and not clip.startswith("http") else None
            if path and path.exists():
                if sid in yt_urls and not prev.get("youtube_url"):
                    prev = dict(prev)
                    prev["youtube_url"] = yt_urls[sid]
                    prev["youtube_video_id"] = yt_urls[sid].rsplit("/", 1)[-1]
                    merged[sid] = prev
                    changed = True
                continue
        short = dict(short)
        if sid in yt_urls:
            short["youtube_url"] = yt_urls[sid]
            short["youtube_video_id"] = yt_urls[sid].rsplit("/", 1)[-1]
        merged[sid] = short
        changed = True

    if not changed:
        return False

    selected = _job_selected_ids(job)
    selected_done = {sid: merged[sid] for sid in selected if sid in merged}
    extras = [merged[sid] for sid in sorted(merged.keys()) if sid not in selected_done]

    status = job.get("status")
    phase = result.get("phase")
    if selected:
        done_n = len(selected_done)
        total_n = len(selected)
        if done_n >= total_n and done_n > 0:
            if status in ("awaiting_selection", "interrupted", "failed", "rendering"):
                job["status"] = "completed"
            phase = "completed"
            current_id = None
        else:
            if status in ("awaiting_selection", "failed", "rendering"):
                job["status"] = "interrupted"
            phase = "interrupted"
            pending = [sid for sid in selected if sid not in selected_done]
            current_id = pending[0] if pending else None
        job.setdefault("params", {})["ui_step"] = 5
        rebuilt = _build_render_result(
            result,
            selected,
            selected_done,
            phase=phase or "interrupted",
            current_id=current_id,
        )
        rebuilt["shorts"] = list(rebuilt.get("shorts") or []) + extras
        job["result"] = rebuilt
    else:
        job["result"] = dict(result)
        job["result"]["shorts"] = [merged[sid] for sid in sorted(merged.keys())]
        job["result"]["phase"] = phase or "completed"
        job.setdefault("params", {})["ui_step"] = 5
        if status in ("awaiting_selection", "interrupted", "failed"):
            job["status"] = "completed"

    job["updated_at"] = _now()
    # Clips on disk match the aspect they were rendered with (unknown → current)
    if not job["result"].get("rendered_aspect_ratio"):
        job["result"]["rendered_aspect_ratio"] = (
            (job.get("params") or {}).get("aspect_ratio") or "9:16"
        )
    return True


def _incomplete_render_snapshot(
    job: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return progress snapshot when a render was cut short (server restart etc.)."""
    result = job.get("result")
    if not isinstance(result, dict):
        return None
    selected_ids = _job_selected_ids(job)
    done_by_id = _done_shorts_by_id(result)
    # Prefer JSON shorts; fall back to mp4s still on disk
    if selected_ids:
        disk = _discover_shorts_on_disk(job)
        for sid, short in disk.items():
            if sid in selected_ids and sid not in done_by_id:
                done_by_id[sid] = short
    progress = result.get("render_progress") or {}
    total = len(selected_ids) or int(progress.get("total") or 0)
    done = len(done_by_id) if not selected_ids else len(
        {sid for sid in selected_ids if sid in done_by_id}
    )
    if total <= 0:
        return None
    if done >= total:
        return None
    # Mid-render phase, or leftover progress from a previous interrupted run
    phase = result.get("phase")
    if phase not in ("rendering", "failed", "interrupted") and done <= 0:
        return None
    pending = [sid for sid in selected_ids if sid not in done_by_id]
    current_id = pending[0] if pending else None
    return {
        "selected_ids": selected_ids,
        "done_by_id": {sid: done_by_id[sid] for sid in selected_ids if sid in done_by_id},
        "done": done,
        "total": total,
        "current_id": current_id,
        "pending_ids": pending[1:] if pending else [],
        "done_ids": [sid for sid in selected_ids if sid in done_by_id],
    }


def _mark_render_interrupted(job: Dict[str, Any], snap: Optional[Dict[str, Any]]) -> None:
    """Freeze an in-flight render so the UI can show progress and resume."""
    job["status"] = "interrupted"
    job["error"] = None
    job.setdefault("params", {})
    job["params"]["ui_step"] = 5
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if snap:
        result = _build_render_result(
            result,
            snap["selected_ids"],
            snap["done_by_id"],
            phase="interrupted",
            current_id=snap["current_id"],
        )
        job["result"] = result
        msg = (
            f"Servidor reiniciou durante a renderização — "
            f"{snap['done']} de {snap['total']} shorts prontos. "
            "Abra a etapa Resultados e clique em Retomar renderização."
        )
    else:
        if isinstance(job.get("result"), dict):
            job["result"] = dict(job["result"])
            job["result"]["phase"] = "interrupted"
        selected = _job_selected_ids(job)
        msg = (
            "Servidor reiniciou durante a renderização — "
            "nenhum short novo ficou pronto. "
            "Abra Resultados e clique em Retomar renderização."
            if selected
            else (
                "Servidor reiniciou durante a renderização — "
                "análise preservada; retome quando quiser."
            )
        )
    job.setdefault("logs", []).append({"ts": _now(), "message": msg})
    job["updated_at"] = _now()


def _job_allows_selection(job: Dict[str, Any]) -> bool:
    """Selection is allowed after analysis, including recoverable failures."""
    status = job.get("status")
    if status in ("awaiting_selection", "completed", "interrupted"):
        return True
    # Render/analyze interrupted or failed after analysis — user can retry cut
    if status == "failed" and _job_highlights(job):
        return True
    return False


def _sync_cast_roster(
    result: Dict[str, Any],
    speakers_payload: List[CastSpeakerName],
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Rebuild result['speakers'] from the UI roster (add/remove/reorder)."""
    from shorts_generator.cast import MAX_SPEAKERS

    existing = {
        str(sp.get("id") or "").strip().upper(): dict(sp)
        for sp in (result.get("speakers") or [])
        if isinstance(sp, dict) and str(sp.get("id") or "").strip()
    }
    synced: List[Dict[str, Any]] = []
    names: Dict[str, str] = {}
    seen: set = set()
    for i, s in enumerate(speakers_payload or []):
        raw_id = str(s.id or "").strip().upper()
        sid = raw_id if re.fullmatch(r"S\d+", raw_id) else f"S{i + 1}"
        if sid in seen:
            continue
        seen.add(sid)
        name = (s.name or "").strip()
        base = existing.get(sid)
        if base:
            item = dict(base)
        else:
            item = {
                "id": sid,
                "suggested_name": name,
                "name": "",
                "role": "unknown",
                "sample_quote": "",
                "sample_time": None,
                "evidence": "Adicionado manualmente",
            }
        item["id"] = sid
        if name and not str(item.get("suggested_name") or "").strip():
            item["suggested_name"] = name
        if name:
            item["name"] = name
        synced.append(item)
        names[sid] = name
        if len(synced) >= MAX_SPEAKERS:
            break
    result["speakers"] = synced
    return synced, names


@app.post("/api/jobs/{job_id}/cast")
def confirm_cast(job_id: str, body: ConfirmCast):
    """Confirm speaker names (or skip) and continue to highlight ranking.

    The request body is the source of truth for the cast roster: speakers the
    user removed are dropped, and speakers they added are inserted so labeling
    / titles use only that list.
    """
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

        names: Dict[str, str] = {}
        if not body.skip:
            _synced, names = _sync_cast_roster(result, body.speakers or [])
            job["result"] = result
            job["updated_at"] = _now()
            _persist_job(job)

    thread = threading.Thread(
        target=_run_rank_highlights,
        args=(job_id, names),
        kwargs={"skip_cast": bool(body.skip)},
        daemon=True,
    )
    thread.start()
    return {"id": job_id, "status": "ranking", "skip": bool(body.skip)}


@app.put("/api/jobs/{job_id}/cast/roster")
def update_cast_roster(job_id: str, body: UpdateCastRoster):
    """Persist add/remove of speakers while the user is still naming the cast."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        if job.get("status") != "awaiting_cast":
            raise HTTPException(
                400,
                f"Só é possível editar locutores na etapa de identificação (status={job['status']})",
            )
        result = job.get("result") or {}
        if not isinstance(result, dict):
            raise HTTPException(400, "Job sem resultado de análise")
        synced, _names = _sync_cast_roster(result, body.speakers or [])
        job["result"] = result
        job["updated_at"] = _now()
        _persist_job(job)
        speakers = [
            _public_speaker(job_id, sp, i) for i, sp in enumerate(synced)
        ]
    return {"id": job_id, "speakers": speakers}


@app.post("/api/jobs/{job_id}/select")
def select_highlights(job_id: str, body: SelectHighlights):
    from shorts_generator.captions import (
        merge_caption_styles,
        normalize_caption_aspect,
        resolve_style_for_aspect,
    )

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
        aspect = normalize_caption_aspect(job["params"].get("aspect_ratio"))
        prev_style = resolve_style_for_aspect(job["params"], aspect)
        if body.caption_styles is not None or body.caption_style is not None:
            merged = merge_caption_styles(
                job["params"].get("caption_styles"),
                body.caption_styles,
                active_aspect=aspect,
                active_style=body.caption_style,
            )
            if merged:
                job["params"]["caption_styles"] = merged
        style = resolve_style_for_aspect(job["params"], aspect)
        style_changed = prev_style != style
        job["params"]["caption_style"] = style
        append = bool(body.append)
        resume = bool(body.resume)
        if resume:
            # Continue remaining clips; do not invalidate already-rendered ones
            # because the user toggled aspect/style while interrupted.
            force = bool(body.force)
            job["params"]["force_rerender"] = False
        else:
            force = bool(body.force) or bool(job["params"].get("force_rerender")) or (
                style_changed and not append
            )
        force_ids: List[int] = []
        if append:
            existing_done = set(_done_shorts_by_id(job.get("result") or {}).keys())
            # Re-cut requested ids when forced or caption style changed
            if bool(body.force) or style_changed:
                force_ids = list(ids)
            final_ids = _sorted_selection_ids(
                highlights, list(existing_done | set(ids))
            )
        else:
            final_ids = ids
        # Clear prior failure / interrupt so UI reflects a fresh render attempt
        if job["status"] in ("failed", "interrupted"):
            job["status"] = "awaiting_selection"
            job["error"] = None
        job["updated_at"] = _now()
        _persist_job(job)

    thread = threading.Thread(
        target=_run_render,
        args=(job_id, final_ids),
        kwargs={
            "force_all": force and not append and not resume,
            "force_ids": force_ids,
            "preserve_others": append or resume,
        },
        daemon=True,
    )
    thread.start()
    return {
        "id": job_id,
        "status": "rendering",
        "selected_ids": final_ids,
        "force": force and not append and not resume,
        "force_ids": force_ids,
        "append": append,
        "resume": resume,
        "caption_style": style,
        "caption_styles": (job.get("params") or {}).get("caption_styles"),
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
        # Keep singular caption_style aligned with the active aspect bucket.
        if changed and isinstance(job["params"].get("caption_styles"), dict):
            from shorts_generator.captions import resolve_style_for_aspect

            job["params"]["caption_style"] = resolve_style_for_aspect(
                job["params"], aspect
            )
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
        if body.caption_styles is not None or body.caption_style is not None:
            from shorts_generator.captions import (
                merge_caption_styles,
                normalize_caption_aspect,
                resolve_style_for_aspect,
            )

            aspect_for_style = normalize_caption_aspect(
                job["params"].get("aspect_ratio")
            )
            prev_style = resolve_style_for_aspect(job["params"], aspect_for_style)
            merged = merge_caption_styles(
                job["params"].get("caption_styles"),
                body.caption_styles,
                active_aspect=aspect_for_style,
                active_style=body.caption_style,
            )
            if merged:
                job["params"]["caption_styles"] = merged
            style = resolve_style_for_aspect(job["params"], aspect_for_style)
            if prev_style != style:
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
            # Drop only auto frame+hook posters so they can refresh with the new
            # karaoke palette. Keep custom/AI/cutout short_thumbs intact.
            short_thumb_dir = JOBS_DIR / job_id / "short_thumbs"
            if short_thumb_dir.exists():
                protected: set[int] = set()
                result = (
                    job.get("result") if isinstance(job.get("result"), dict) else {}
                )
                for collection in (
                    result.get("highlights") or [],
                    result.get("shorts") or [],
                ):
                    for i, item in enumerate(collection):
                        if not isinstance(item, dict):
                            continue
                        if (
                            item.get("thumbnail_ai")
                            or item.get("has_custom_thumbnail")
                            or item.get("thumbnail_frame_injected")
                        ):
                            protected.add(_short_id(item, i))
                for p in short_thumb_dir.glob("*.jpg"):
                    if p.name.startswith("."):
                        continue
                    try:
                        sid = int(p.stem)
                    except ValueError:
                        continue
                    if sid in protected:
                        continue
                    try:
                        p.unlink()
                    except OSError:
                        pass
        if changed or fmt_changed:
            if changed:
                # Next full cut re-renders only if aspect differs from on-disk clips.
                # Keep existing shorts + status so Results stays reachable.
                result = job.get("result") if isinstance(job.get("result"), dict) else {}
                rendered = (result or {}).get("rendered_aspect_ratio")
                if not rendered:
                    rendered = prev
                    if result and _done_shorts_by_id(result):
                        job["result"] = dict(result)
                        job["result"]["rendered_aspect_ratio"] = prev
                        rendered = prev
                job["params"]["force_rerender"] = aspect != str(rendered)
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
    clip_length: str = Form("short"),
    project_id: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    mode = (mode or "api").lower().strip()
    if mode not in ("api", "local"):
        raise HTTPException(400, "mode deve ser 'api' ou 'local'")
    if download_format not in ("360", "480", "720", "1080"):
        raise HTTPException(400, "format inválido")

    from shorts_generator.highlights import normalize_clip_length

    clip_len = normalize_clip_length(clip_length)
    # Aspect locked to clip format from step 1 (short → 9:16, long → 16:9)
    aspect_ratio = "16:9" if clip_len == "long" else "9:16"

    pid = (project_id or "").strip() or None
    if not pid:
        raise HTTPException(400, "project_id é obrigatório")
    _require_project(pid)

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
        "project_id": pid,
        "status": "queued",
        "params": {
            "url": source,
            "mode": mode,
            "aspect_ratio": aspect_ratio.strip() or "9:16",
            "download_format": download_format,
            "language": lang,
            "clip_length": clip_len,
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
        raise FileNotFoundError("Clip local indisponível para upload")

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
        raise RuntimeError(f"Falha ao baixar clip remoto: {exc}") from exc
    return dest


def _find_job_short(
    job: Dict[str, Any], short_id: int
) -> Tuple[Optional[Dict[str, Any]], int]:
    shorts = (job.get("result") or {}).get("shorts") or []
    for i, s in enumerate(shorts):
        if _short_id(s, i) == int(short_id):
            return s, i
    return None, -1


def _find_job_highlight(
    job: Dict[str, Any], highlight_id: int
) -> Tuple[Optional[Dict[str, Any]], int]:
    highlights = (job.get("result") or {}).get("highlights") or []
    for i, h in enumerate(highlights):
        if not isinstance(h, dict):
            continue
        try:
            hid = int(h.get("id", i))
        except (TypeError, ValueError):
            hid = i
        if hid == int(highlight_id):
            return h, i
    return None, -1


def _short_thumbnail_path(job_id: str, short: Dict[str, Any]) -> Optional[Path]:
    """Local custom thumbnail jpg for YouTube thumbnails.set, if present."""
    hid = _short_id(short)
    if hid < 0:
        return None
    path = JOBS_DIR / job_id / "short_thumbs" / f"{hid}.jpg"
    if path.is_file() and path.stat().st_size > 0:
        return path
    return None


def _infer_content_type(result: Dict[str, Any]) -> str:
    """Best-effort niche for category/tags when pipeline didn't store content_type."""
    explicit = (result.get("content_type") or "").strip().lower()
    if explicit:
        return explicit
    meta = result.get("metadata") or {}
    blob = " ".join(
        str(meta.get(k) or "") for k in ("title", "channel", "description")
    ).lower()
    if any(w in blob for w in ("podcast", "flow ", "cortes")):
        return "podcast"
    if "entrevista" in blob or "interview" in blob:
        return "interview"
    if "debate" in blob:
        return "debate"
    if any(w in blob for w in ("tutorial", "como fazer", "how to")):
        return "tutorial"
    return "other"


def _build_youtube_seo_for_short(
    job: Dict[str, Any],
    short: Dict[str, Any],
    short_id: int,
    *,
    title: Optional[str] = None,
    tags: Optional[List[str]] = None,
    category_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the SEO payload that would be sent on YouTube upload."""
    from shorts_generator import youtube_uploader as yt

    result = job.get("result") or {}
    params = job.get("params") or {}
    meta = result.get("metadata") or {}
    speakers = [
        str(s.get("name") or "").strip()
        for s in (result.get("speakers") or [])
        if isinstance(s, dict) and (s.get("name") or "").strip()
    ]
    language = (
        (params.get("language") or "").strip()
        or (result.get("transcript") or {}).get("language")
        or "pt"
    )
    content_type = _infer_content_type(result)
    source_url = (
        str(meta.get("url") or "").strip()
        or str(result.get("source_url") or "").strip()
        or str(params.get("url") or "").strip()
    )
    base_title = (title or short.get("title") or f"Short #{short_id + 1}").strip()
    return yt.build_seo_metadata(
        title=base_title,
        hook=str(short.get("hook_sentence") or ""),
        reason=str(short.get("virality_reason") or ""),
        attributed_to=str(short.get("attributed_to") or ""),
        source_title=str(meta.get("title") or ""),
        source_url=source_url,
        channel=str(meta.get("channel") or ""),
        speakers=speakers,
        content_type=content_type,
        start_time=short.get("start_time"),
        language=str(language or "pt"),
        extra_tags=tags,
        category_id=category_id,
    )


def _youtube_upload_preview(job_id: str, short_id: int) -> Dict[str, Any]:
    """Return title/description/hashtags/tags/thumbnail for the upload modal."""
    from shorts_generator import youtube_uploader as yt

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise ValueError("Job/resultado não encontrado")
        project_id = job.get("project_id")
        target, target_index = _find_job_short(job, short_id)
        if target is None:
            raise ValueError("Short não encontrado")
        if target.get("error"):
            raise ValueError("Este short falhou na renderização")
        if not (target.get("clip_url") or target.get("local_path")):
            raise ValueError("Clip ainda não está pronto")
        short_snapshot = dict(target)
        job_snapshot = {
            "result": dict(job.get("result") or {}),
            "params": dict(job.get("params") or {}),
            "project_id": project_id,
        }

    if not project_id:
        raise ValueError(
            "Este job não pertence a um projeto. Abra um canal e gere o short de novo."
        )

    project = _projects.get_public(project_id)
    yt_cfg = (project or {}).get("youtube") or {}
    configured = bool(yt_cfg.get("configured"))
    if not configured:
        creds = _projects.youtube_credentials(project_id)
        configured = bool(creds and yt.credentials_configured(creds))

    seo = _build_youtube_seo_for_short(job_snapshot, short_snapshot, short_id)
    # Heal JPG overwritten by the old frame+hook attach bug (frame 0 is source of truth).
    if _heal_injected_short_thumbnail(job_id, short_snapshot):
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job and job.get("result"):
                live, _ = _find_job_short(job, short_id)
                if live is not None:
                    if short_snapshot.get("thumbnail_url"):
                        live["thumbnail_url"] = short_snapshot["thumbnail_url"]
                    if short_snapshot.get("thumbnail_version") is not None:
                        live["thumbnail_version"] = short_snapshot["thumbnail_version"]
                    job["updated_at"] = _now()
                    _persist_job(job)
    public = _public_short(job_id, short_snapshot, target_index if target_index >= 0 else 0)
    thumb_path = _short_thumbnail_path(job_id, short_snapshot)
    thumbnail_url = public.get("thumbnail_url") or ""
    if not thumbnail_url and thumb_path:
        hid = _short_id(short_snapshot)
        try:
            ver = int(thumb_path.stat().st_mtime)
        except OSError:
            ver = 2
        thumbnail_url = f"/api/jobs/{job_id}/short-thumbs/{hid}?v={ver}"
    elif thumb_path:
        try:
            ver = int(thumb_path.stat().st_mtime)
        except OSError:
            ver = None
        if ver is not None:
            hid = _short_id(short_snapshot)
            thumbnail_url = f"/api/jobs/{job_id}/short-thumbs/{hid}?v={ver}"
            short_snapshot["thumbnail_version"] = ver
            public["thumbnail_url"] = thumbnail_url

    already_uploaded = bool(
        short_snapshot.get("youtube_video_id") or short_snapshot.get("youtube_url")
    )
    saved_privacy = str(short_snapshot.get("youtube_privacy") or "").strip().lower()
    cfg_privacy = str(yt_cfg.get("privacy_status") or "public").strip().lower() or "public"
    privacy = saved_privacy if saved_privacy in ("private", "unlisted", "public") else cfg_privacy
    if privacy not in ("private", "unlisted", "public"):
        privacy = "public"

    title = (
        str(short_snapshot.get("youtube_title") or "").strip()
        or seo["title"]
    )
    description = (
        str(short_snapshot.get("youtube_description") or "").strip()
        or seo["description"]
    )
    tags = short_snapshot.get("youtube_tags") if already_uploaded else None
    if not isinstance(tags, list) or not tags:
        tags = seo.get("tags") or []
    category_id = (
        str(short_snapshot.get("youtube_category_id") or "").strip()
        or seo.get("category_id")
        or "22"
    )

    return {
        "ok": True,
        "short_id": short_id,
        "configured": configured,
        "channel_title": yt_cfg.get("channel_title") or "",
        "already_uploaded": already_uploaded,
        "youtube_video_id": short_snapshot.get("youtube_video_id") or "",
        "youtube_url": short_snapshot.get("youtube_url") or "",
        "title": title,
        "description": description,
        "hashtags": seo.get("hashtags") or [],
        "tags": tags,
        "category_id": category_id,
        "default_language": (
            str(short_snapshot.get("youtube_language") or "").strip()
            or seo.get("default_language")
            or "pt"
        ),
        "privacy": privacy,
        "thumbnail_url": thumbnail_url,
        "has_custom_thumbnail": bool(thumb_path),
        "thumbnail_ai": bool(short_snapshot.get("thumbnail_ai")),
        "openai_configured": _is_real_secret(os.getenv("OPENAI_API_KEY")),
        "clip_url": public.get("clip_url") or "",
        "hook_sentence": short_snapshot.get("hook_sentence") or "",
        "attributed_to": short_snapshot.get("attributed_to") or "",
    }


def _perform_youtube_upload(
    job_id: str,
    short_id: int,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    privacy: Optional[str] = None,
    tags: Optional[List[str]] = None,
    category_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload a rendered short to the project's YouTube channel.

    If the short already has a youtube_video_id, updates metadata on that video
    instead of uploading a duplicate.

    Mutates the short dict in the live job. Raises ValueError/RuntimeError on failure.
    Fills SEO fields: title, description, tags, hashtags, language, category, thumbnail.
    """
    from shorts_generator import youtube_uploader as yt

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise ValueError("Job/resultado não encontrado")
        project_id = job.get("project_id")
        target, target_index = _find_job_short(job, short_id)
        if target is None:
            raise ValueError("Short não encontrado")
        if target.get("error"):
            raise ValueError("Este short falhou na renderização")
        existing_id = str(target.get("youtube_video_id") or "").strip()
        if not existing_id:
            existing_id = yt.youtube_id_from_url(
                str(target.get("youtube_url") or "")
            ) or ""
        if not existing_id and not (target.get("clip_url") or target.get("local_path")):
            raise ValueError("Clip ainda não está pronto")
        short_snapshot = dict(target)
        result_snapshot = dict(job.get("result") or {})
        params_snapshot = dict(job.get("params") or {})
        target["youtube_upload_status"] = "uploading"
        target.pop("youtube_upload_error", None)
        job["updated_at"] = _now()
        _persist_job(job)

    if not project_id:
        raise ValueError(
            "Este job não pertence a um projeto. Abra um canal e gere o short de novo."
        )
    creds = _projects.youtube_credentials(project_id)
    if not creds or not yt.credentials_configured(creds):
        raise ValueError(
            "YouTube não configurado neste projeto. Abra Config do canal e "
            "informe Client ID, Secret e Refresh Token (ou use Conectar YouTube)."
        )

    existing_video_id = existing_id
    is_update = bool(existing_video_id)

    seo = _build_youtube_seo_for_short(
        {"result": result_snapshot, "params": params_snapshot},
        short_snapshot,
        short_id,
        title=title,
        tags=tags,
        category_id=category_id,
    )

    # Explicit body overrides win over auto SEO (except empty).
    upload_title = (title or "").strip() or seo["title"]
    upload_description = (description or "").strip() or seo["description"]
    upload_tags = [t for t in (tags or []) if t and str(t).strip()] or seo["tags"]
    upload_category = (category_id or "").strip() or seo["category_id"]
    upload_privacy = (privacy or "").strip() or None
    thumb_path = _short_thumbnail_path(job_id, short_snapshot)

    if is_update:
        _append_log(
            job_id,
            f"Atualização YouTube iniciada: short #{short_id} → {existing_video_id} "
            f"(SEO: {len(upload_tags)} tags, lang={seo['default_language']}, "
            f"cat={upload_category}"
            f"{', thumb' if thumb_path else ''})",
        )
        try:
            uploaded = yt.update_video_metadata(
                existing_video_id,
                title=upload_title,
                description=upload_description,
                tags=upload_tags,
                privacy=upload_privacy,
                category_id=upload_category,
                default_language=seo["default_language"],
                default_audio_language=seo["default_audio_language"],
                thumbnail_path=thumb_path,
                credentials=creds,
            )
        except Exception as exc:
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job and job.get("result"):
                    target, _ = _find_job_short(job, short_id)
                    if target is not None:
                        target["youtube_upload_status"] = "failed"
                        target["youtube_upload_error"] = str(exc)
                        job["updated_at"] = _now()
                        _persist_job(job)
            _append_log(job_id, f"Atualização YouTube falhou (short #{short_id}): {exc}")
            raise
    else:
        path = _resolve_short_upload_path(short_snapshot, job_id)
        _append_log(
            job_id,
            f"Upload YouTube iniciado: short #{short_id} → {path.name} "
            f"(SEO: {len(upload_tags)} tags, lang={seo['default_language']}, "
            f"cat={upload_category}"
            f"{', thumb' if thumb_path else ''})",
        )
        try:
            uploaded = yt.upload_video(
                path,
                title=upload_title,
                description=upload_description,
                tags=upload_tags,
                privacy=upload_privacy,
                category_id=upload_category,
                default_language=seo["default_language"],
                default_audio_language=seo["default_audio_language"],
                thumbnail_path=thumb_path,
                credentials=creds,
            )
        except Exception as exc:
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job and job.get("result"):
                    target, _ = _find_job_short(job, short_id)
                    if target is not None:
                        target["youtube_upload_status"] = "failed"
                        target["youtube_upload_error"] = str(exc)
                        job["updated_at"] = _now()
                        _persist_job(job)
            _append_log(job_id, f"Upload YouTube falhou (short #{short_id}): {exc}")
            raise

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise ValueError("Job sumiu durante o upload")
        updated, idx = _find_job_short(job, short_id)
        if updated is None and target_index >= 0:
            shorts = job["result"].get("shorts") or []
            if target_index < len(shorts):
                updated = shorts[target_index]
                idx = target_index
        if updated is None:
            raise ValueError("Short não encontrado após upload")
        updated["youtube_video_id"] = uploaded["video_id"]
        updated["youtube_url"] = uploaded["url"]
        updated["youtube_privacy"] = uploaded["privacy_status"]
        if not is_update:
            updated["youtube_uploaded_at"] = _now()
        updated["youtube_updated_at"] = _now()
        updated["youtube_upload_status"] = "uploaded"
        updated["youtube_title"] = uploaded.get("title") or upload_title
        updated["youtube_description"] = uploaded.get("description") or upload_description
        updated["youtube_tags"] = uploaded.get("tags") or upload_tags
        updated["youtube_category_id"] = uploaded.get("category_id") or upload_category
        updated["youtube_language"] = uploaded.get("default_language") or seo["default_language"]
        updated["youtube_thumbnail_set"] = bool(uploaded.get("thumbnail_set"))
        updated.pop("youtube_upload_error", None)
        job["updated_at"] = _now()
        _persist_job(job)
        public = _public_short(job_id, updated, idx if idx >= 0 else 0)

    thumb_note = (
        " + thumbnail"
        if uploaded.get("thumbnail_set")
        else (" (thumbnail não aplicada — reconecte o YouTube)" if thumb_path else "")
    )
    action_label = "Atualização YouTube ok" if is_update else "Upload YouTube ok"
    _append_log(
        job_id,
        f"{action_label}: short #{short_id} → {uploaded['url']} "
        f"({uploaded['privacy_status']}){thumb_note}",
    )
    return {
        "ok": True,
        "updated": is_update,
        "video_id": uploaded["video_id"],
        "url": uploaded["url"],
        "privacy_status": uploaded["privacy_status"],
        "title": uploaded["title"],
        "description": uploaded.get("description") or upload_description,
        "tags": uploaded.get("tags") or upload_tags,
        "category_id": uploaded.get("category_id") or upload_category,
        "default_language": uploaded.get("default_language") or seo["default_language"],
        "thumbnail_set": bool(uploaded.get("thumbnail_set")),
        "hashtags": seo.get("hashtags") or [],
        "short": public,
    }


@app.get("/api/jobs/{job_id}/shorts/{short_id}/youtube/preview")
def youtube_upload_preview(job_id: str, short_id: int):
    """Preview title, description, hashtags and thumbnail before YouTube upload."""
    try:
        return _youtube_upload_preview(job_id, short_id)
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "não encontrado" in msg.lower() or "sumiu" in msg.lower() else 400
        raise HTTPException(code, msg) from exc


def _reload_image_config() -> None:
    """Refresh image-provider settings from .env before each AI thumbnail call."""
    load_dotenv(ENV_PATH, override=True)
    import shorts_generator.config as cfg

    cfg.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    cfg.OPENAI_IMAGE_MODEL = (
        os.getenv("OPENAI_IMAGE_MODEL") or "gpt-image-1-mini"
    ).strip().strip("'\"") or "gpt-image-1-mini"
    _q = (
        os.getenv("OPENAI_IMAGE_QUALITY") or "medium"
    ).strip().strip("'\"").lower() or "medium"
    cfg.OPENAI_IMAGE_QUALITY = _q if _q in ("low", "medium", "high") else "medium"
    _f = (
        os.getenv("OPENAI_IMAGE_FIDELITY") or "low"
    ).strip().strip("'\"").lower() or "low"
    cfg.OPENAI_IMAGE_FIDELITY = (
        _f if _f in ("low", "high", "none", "off") else "low"
    )
    cfg.THUMBNAIL_HYBRID = os.getenv("THUMBNAIL_HYBRID", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    _tm = (
        os.getenv("THUMBNAIL_MODE") or "frame"
    ).strip().strip("'\"").lower() or "frame"
    cfg.THUMBNAIL_MODE = _tm if _tm in ("cutout", "ai", "frame") else "frame"
    _ip = (
        os.getenv("IMAGE_PROVIDER") or "auto"
    ).strip().strip("'\"").lower() or "auto"
    cfg.IMAGE_PROVIDER = _ip if _ip in ("openai", "gemini", "auto") else "auto"
    cfg.GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip().strip("'\"")
    cfg.GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
    cfg.GEMINI_IMAGE_MODEL = (
        os.getenv("GEMINI_IMAGE_MODEL") or "gemini-2.5-flash-image"
    ).strip().strip("'\"") or "gemini-2.5-flash-image"
    cfg.LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()
    from shorts_generator.config import DEFAULT_THUMBNAIL_PALETTE, format_thumbnail_palette, parse_thumbnail_palette

    cfg.THUMBNAIL_PALETTE = format_thumbnail_palette(
        parse_thumbnail_palette(
            os.getenv("THUMBNAIL_PALETTE") or DEFAULT_THUMBNAIL_PALETTE
        )
    )


def _face_cand_dir(job_id: str, highlight_id: int) -> Path:
    return JOBS_DIR / job_id / "face_cands" / str(int(highlight_id))


def _resolve_cast_portrait_for_name(
    job_id: str,
    attributed_to: str,
    speakers: List[Any],
) -> Optional[Path]:
    from shorts_generator.thumbnails_ai import match_cast_portrait

    cast_dir = JOBS_DIR / job_id / "cast"
    if not cast_dir.is_dir():
        return None
    matched = match_cast_portrait(attributed_to, speakers, cast_dir)
    return matched[0] if matched else None


def _build_face_candidates_for_highlight(
    job_id: str, highlight_id: int, *, limit: int = 6
) -> Dict[str, Any]:
    """Sample video frames in the topic range and return ranked face plates."""
    from shorts_generator.thumbnail_cutout import build_face_candidates

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise ValueError("Job/resultado não encontrado")
        highlight, _ = _find_job_highlight(job, highlight_id)
        short, _ = _find_job_short(job, highlight_id)
        if highlight is None and short is None:
            raise ValueError("Tópico não encontrado")
        base = dict(highlight or {})
        if short:
            for key in ("attributed_to", "start_time", "end_time", "title"):
                if not base.get(key) and short.get(key) is not None:
                    base[key] = short.get(key)
        result = job.get("result") or {}
        speakers = list(result.get("speakers") or [])
        transcript = (
            result.get("transcript") if isinstance(result.get("transcript"), dict) else {}
        )
        params = dict(job.get("params") or {})
        original_url = str(params.get("url") or result.get("source_url") or "")
        ffmpeg_src, _yt = _resolve_job_source(result, original_url)

    if not (transcript.get("segments") if isinstance(transcript, dict) else None):
        result_path = JOBS_DIR / f"{job_id}_result.json"
        if result_path.exists():
            try:
                full = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(full.get("transcript"), dict):
                    transcript = full["transcript"]
                if not speakers and isinstance(full.get("speakers"), list):
                    speakers = full["speakers"]
            except Exception:
                pass

    if not ffmpeg_src:
        raise ValueError("Vídeo fonte indisponível para extrair frames")

    try:
        start = float(base.get("start_time") or 0)
        end = float(base.get("end_time") or start + 60)
    except (TypeError, ValueError):
        start, end = 0.0, 60.0

    attributed = str(base.get("attributed_to") or "").strip()
    cast_path = _resolve_cast_portrait_for_name(job_id, attributed, speakers)
    out_dir = _face_cand_dir(job_id, highlight_id)
    built = build_face_candidates(
        ffmpeg_src=str(ffmpeg_src),
        extract_frame_fn=_extract_frame,
        out_dir=out_dir,
        start=start,
        end=end,
        transcript=transcript if isinstance(transcript, dict) else None,
        attributed_to=attributed,
        speakers=speakers,
        cast_portrait=cast_path,
        limit=max(3, min(8, int(limit))),
    )
    if not built:
        raise RuntimeError(
            "Não achei frames com rosto neste trecho — tente outro corte ou use a foto do locutor"
        )

    candidates = []
    for item in built:
        cid = str(item["id"])
        candidates.append(
            {
                "id": cid,
                "time": item.get("time"),
                "score": item.get("score"),
                "source": item.get("source"),
                "label": item.get("label") or cid,
                "url": f"/api/jobs/{job_id}/highlights/{int(highlight_id)}/face-candidates/{cid}",
            }
        )
    return {
        "ok": True,
        "highlight_id": int(highlight_id),
        "attributed_to": attributed,
        "candidates": candidates,
    }


def _face_candidate_path(job_id: str, highlight_id: int, candidate_id: str) -> Path:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", candidate_id or "")
    if not sid:
        raise ValueError("Candidato inválido")
    folder = _face_cand_dir(job_id, highlight_id)
    manifest_path = folder / "manifest.json"
    if manifest_path.exists():
        try:
            items = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in items or []:
                if str(item.get("id") or "") != sid:
                    continue
                path = folder / str(item.get("file") or "")
                if path.exists():
                    return path
        except Exception:
            pass
    # Legacy / fallback lookups
    if sid == "cast":
        for path in folder.glob("*cast*.jpg"):
            if path.exists():
                return path
    for path in folder.glob(f"*_{sid}.jpg"):
        if path.exists():
            return path
    for path in folder.glob(f"*{sid}*.jpg"):
        if path.exists():
            return path
    raise ValueError("Candidato de rosto não encontrado — abra o seletor de novo")


def _generate_ai_short_thumbnail(
    job_id: str,
    short_id: int,
    *,
    face_candidate_id: Optional[str] = None,
    mode: Optional[str] = None,
    overlay_text: Optional[str] = None,
    text_color_mode: Optional[str] = None,
    margin_v: Optional[int] = None,
    font_size: Optional[int] = None,
    border_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a viral thumbnail for a topic/short id (clip optional).

    Works before render by sampling a frame from the source video. Writes
    ``short_thumbs/{id}.jpg`` and stamps ``thumbnail_ai`` on the highlight
    (and short, if already rendered).
    """
    from shorts_generator.highlights import snippet_for_range
    from shorts_generator.thumbnails_ai import generate_ai_thumbnail

    _reload_image_config()
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise ValueError("Job/resultado não encontrado")
        highlight, _hl_index = _find_job_highlight(job, short_id)
        short, short_index = _find_job_short(job, short_id)
        if highlight is None and short is None:
            raise ValueError("Tópico não encontrado")
        if short is not None and short.get("error") and not highlight:
            raise ValueError("Este short falhou na renderização")

        # Prefer highlight fields; fall back to rendered short
        base = dict(highlight or {})
        if short:
            for key in (
                "title",
                "hook_sentence",
                "virality_reason",
                "attributed_to",
                "snippet",
                "start_time",
                "end_time",
                "clip_url",
                "local_path",
            ):
                if not base.get(key) and short.get(key) is not None:
                    base[key] = short.get(key)

        result = job.get("result") or {}
        speakers = list(result.get("speakers") or [])
        transcript = result.get("transcript") if isinstance(result.get("transcript"), dict) else {}
        params = dict(job.get("params") or {})
        aspect = str(params.get("aspect_ratio") or "9:16")
        language = (
            (params.get("language") or "").strip()
            or (transcript.get("language") if isinstance(transcript, dict) else None)
            or "pt"
        )
        original_url = str(params.get("url") or result.get("source_url") or "")
        ffmpeg_src, _yt = _resolve_job_source(result, original_url)
        short_snapshot = dict(base)

    # Prefer full transcript from disk if API response truncated segments
    if not (transcript.get("segments") if isinstance(transcript, dict) else None):
        result_path = JOBS_DIR / f"{job_id}_result.json"
        if result_path.exists():
            try:
                full = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(full.get("transcript"), dict):
                    transcript = full["transcript"]
                if not speakers and isinstance(full.get("speakers"), list):
                    speakers = full["speakers"]
            except Exception:
                pass

    hid = int(short_id)
    dest = JOBS_DIR / job_id / "short_thumbs" / f"{hid}.jpg"
    cast_dir = JOBS_DIR / job_id / "cast"
    wiki_dir = JOBS_DIR / job_id / "wiki"

    try:
        start = float(short_snapshot.get("start_time") or 0)
        end = float(short_snapshot.get("end_time") or start + 60)
    except (TypeError, ValueError):
        start, end = 0.0, 60.0

    snippet = str(short_snapshot.get("snippet") or "").strip()
    if not snippet and isinstance(transcript, dict):
        snippet = snippet_for_range(transcript, start, end, max_chars=900)

    # Selected face plate for cutout mode
    person_frame: Optional[Path] = None
    if face_candidate_id:
        person_frame = _face_candidate_path(job_id, hid, face_candidate_id)

    # Reference frame: rendered clip → existing AI/raw thumbs → source video
    ref_frame: Optional[Path] = None
    tmp_frame = JOBS_DIR / job_id / "short_thumbs" / f".{hid}.ai_ref.jpg"
    clip_path = _short_clip_path(short_snapshot) if short_snapshot.get("clip_url") or short_snapshot.get("local_path") else None
    if clip_path is None and short is not None:
        clip_path = _short_clip_path(short)
    if clip_path is not None:
        if _extract_frame(str(clip_path), 0.8, tmp_frame):
            ref_frame = tmp_frame
    if ref_frame is None:
        for candidate in (
            JOBS_DIR / job_id / "thumbs" / f"{hid}.jpg",
            JOBS_DIR / job_id / "preview_thumbs" / f"{hid}.jpg",
            dest if dest.exists() else None,
        ):
            if candidate is not None and candidate.exists() and candidate.stat().st_size > 0:
                ref_frame = candidate
                break
    if ref_frame is None and ffmpeg_src:
        ts = start + min(0.8, max(0.0, (end - start) * 0.15))
        if _extract_frame(str(ffmpeg_src), ts, tmp_frame):
            ref_frame = tmp_frame

    if person_frame is None:
        person_frame = ref_frame

    _append_log(
        job_id,
        f"Gerando thumbnail (frame original) para tópico #{hid}"
        + (f" · frame={face_candidate_id}" if face_candidate_id else "")
        + "…",
    )
    _append_log(
        job_id,
        f"[thumb] person_frame="
        + (person_frame.name if person_frame else "None")
        + f" · ref_frame="
        + (ref_frame.name if ref_frame else "None")
        + f" · wiki_dir={wiki_dir.name if wiki_dir else 'None'}"
        + f" · attributed_to={short_snapshot.get('attributed_to') or '—'}",
    )
    full_hook = str(
        short_snapshot.get("hook_sentence") or short_snapshot.get("title") or ""
    ).strip()
    from shorts_generator.captions import resolve_style_for_aspect

    caption_style = resolve_style_for_aspect(params, aspect)
    color_mode = _normalize_thumb_color_mode(text_color_mode)
    margin_override = _normalize_thumb_margin_v(margin_v)
    font_override = _normalize_thumb_font_size(font_size)
    border_override = _normalize_thumb_border_pct(border_pct)
    capture = LogCapture(job_id, sys.stdout)
    err_capture = LogCapture(job_id, sys.stderr)
    try:
        with redirect_stdout(capture), redirect_stderr(err_capture):
            meta = generate_ai_thumbnail(
                dest=dest,
                hook=full_hook,
                title=str(short_snapshot.get("title") or ""),
                virality_reason=str(short_snapshot.get("virality_reason") or ""),
                attributed_to=str(short_snapshot.get("attributed_to") or ""),
                snippet=snippet,
                speakers=speakers,
                cast_dir=cast_dir if cast_dir.is_dir() else None,
                wiki_dir=wiki_dir,
                reference_frame=ref_frame,
                person_frame=person_frame,
                aspect_ratio=aspect,
                language=str(language or "pt"),
                mode=mode,
                overlay_text=overlay_text,
                caption_style=caption_style,
                text_color_mode=color_mode,
                margin_v=margin_override,
                font_size=font_override,
                border_pct=border_override,
            )
        # Hybrid: PIL burns hook + frame. Full mode: model paints typography.
        if full_hook and not meta.get("hook"):
            meta["hook"] = full_hook
        meta["hook_burned"] = bool(meta.get("hook_burned"))
    finally:
        try:
            capture.flush()
            err_capture.flush()
        except Exception:
            pass
        try:
            if tmp_frame.exists() and tmp_frame != dest and tmp_frame != ref_frame:
                tmp_frame.unlink(missing_ok=True)
        except OSError:
            pass

    if not dest.exists() or dest.stat().st_size <= 0:
        raise RuntimeError("Thumbnail IA não foi gravada")

    try:
        version = int(dest.stat().st_mtime)
    except OSError:
        version = int(datetime.now(timezone.utc).timestamp())

    thumb_url = f"/api/jobs/{job_id}/short-thumbs/{hid}?v={version}"
    ai_meta = {
        "provider": meta.get("provider"),
        "model": meta.get("model"),
        "size": meta.get("size"),
        "quality": meta.get("quality"),
        "fidelity": meta.get("fidelity"),
        "hybrid": meta.get("hybrid"),
        "mode": meta.get("mode"),
        "mode_requested": meta.get("mode_requested"),
        "hook": meta.get("hook"),
        "hook_burned": bool(meta.get("hook_burned")),
        "overlay_text": meta.get("overlay_text") or meta.get("hook"),
        "overlay_from_llm": bool(meta.get("overlay_from_llm")),
        "text_color_mode": meta.get("text_color_mode") or color_mode,
        "margin_v": meta.get("margin_v") if meta.get("margin_v") is not None else margin_override,
        "font_size": meta.get("font_size") if meta.get("font_size") is not None else font_override,
        "border_pct": meta.get("border_pct") if meta.get("border_pct") is not None else border_override,
        "faces": meta.get("faces") or [],
        "cited_people": meta.get("cited_people") or [],
        "wiki_hits": meta.get("wiki_hits") or [],
        "wiki_skip_reason": meta.get("wiki_skip_reason"),
        "refs_sent_to_ai": meta.get("refs_sent_to_ai") or [],
        "refs_sent_count": meta.get("refs_sent_count") or 0,
        "person_src": meta.get("person_src"),
        "person_src_origin": meta.get("person_src_origin"),
        "face_candidate_id": face_candidate_id,
    }

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise ValueError("Job sumiu durante a geração")

        public_short = None
        updated_short, idx = _find_job_short(job, short_id)
        if updated_short is None and short_index >= 0:
            shorts = job["result"].get("shorts") or []
            if short_index < len(shorts):
                updated_short = shorts[short_index]
                idx = short_index
        if updated_short is not None:
            updated_short["thumbnail_url"] = thumb_url
            updated_short["thumbnail_version"] = version
            updated_short["thumbnail_ai"] = True
            updated_short["thumbnail_ai_meta"] = ai_meta
            public_short = _public_short(job_id, updated_short, idx if idx >= 0 else 0)

        updated_hl, _ = _find_job_highlight(job, short_id)
        if updated_hl is not None:
            updated_hl["thumbnail_url"] = thumb_url
            updated_hl["thumbnail_version"] = version
            updated_hl["thumbnail_ai"] = True
            updated_hl["thumbnail_ai_meta"] = ai_meta

        job["updated_at"] = _now()
        _persist_job(job)
        public_hl = (
            _public_highlight(job_id, updated_hl, short_id)
            if updated_hl is not None
            else None
        )

    face_note = ", ".join(
        f"{f.get('kind')}:{f.get('name')}" for f in (meta.get("faces") or []) if f.get("name")
    )
    cited_note = ", ".join(
        str(c.get("name") or "")
        for c in (meta.get("cited_people") or [])
        if isinstance(c, dict) and c.get("name")
    )
    wiki_note = ", ".join(
        str(h.get("name") or "")
        for h in (meta.get("wiki_hits") or [])
        if isinstance(h, dict) and h.get("name")
    )
    refs_n = int(meta.get("refs_sent_count") or 0)
    refs_names = ", ".join(
        str(r.get("name") or "")
        for r in (meta.get("refs_sent_to_ai") or [])
        if isinstance(r, dict) and r.get("name")
    )
    _append_log(
        job_id,
        f"Thumbnail pronta: tópico #{hid} mode={meta.get('mode')}"
        f" provider={meta.get('provider')}/{meta.get('model')}"
        + (f" ({face_note})" if face_note else ""),
    )
    _append_log(
        job_id,
        f"[thumb] citados={cited_note or '—'} · wiki="
        + (wiki_note or (f"pulado ({meta.get('wiki_skip_reason')})" if meta.get("wiki_skip_reason") else "—"))
        + f" · refs→IA={refs_n}"
        + (f" [{refs_names}]" if refs_names else "")
        + (
            f" · frame={Path(meta['person_src']).name} ({meta.get('person_src_origin')})"
            if meta.get("person_src")
            else ""
        ),
    )
    return {
        "ok": True,
        "short_id": short_id,
        "highlight_id": short_id,
        "thumbnail_url": (
            (public_short or {}).get("thumbnail_url")
            or (public_hl or {}).get("thumbnail_url")
            or thumb_url
        ),
        "has_custom_thumbnail": True,
        "thumbnail_ai": True,
        "mode": meta.get("mode"),
        "overlay_text": meta.get("overlay_text") or meta.get("hook"),
        "text_color_mode": meta.get("text_color_mode") or color_mode,
        "margin_v": meta.get("margin_v") if meta.get("margin_v") is not None else margin_override,
        "font_size": meta.get("font_size") if meta.get("font_size") is not None else font_override,
        "border_pct": meta.get("border_pct") if meta.get("border_pct") is not None else border_override,
        "faces": meta.get("faces") or [],
        "cited_people": meta.get("cited_people") or [],
        "wiki_hits": meta.get("wiki_hits") or [],
        "wiki_skip_reason": meta.get("wiki_skip_reason"),
        "refs_sent_to_ai": meta.get("refs_sent_to_ai") or [],
        "refs_sent_count": refs_n,
        "short": public_short,
        "highlight": public_hl,
        "openai_configured": True,
    }


@app.post("/api/jobs/{job_id}/shorts/{short_id}/generate-thumbnail")
def generate_short_ai_thumbnail(
    job_id: str, short_id: int, body: Optional[GenerateThumbnailBody] = None
):
    """Enqueue (default) or sync-generate a viral AI thumbnail for a short."""
    from shorts_generator.thumbnails_ai import ImageBillingError

    body = body or GenerateThumbnailBody()
    if body.sync:
        try:
            return _generate_ai_short_thumbnail(
                job_id,
                short_id,
                face_candidate_id=body.face_candidate_id,
                mode=body.mode,
                overlay_text=body.overlay_text,
                text_color_mode=body.text_color_mode,
                margin_v=body.margin_v,
                font_size=body.font_size,
                border_pct=body.border_pct,
            )
        except ImageBillingError as exc:
            _append_log(job_id, f"Thumbnail IA billing (short #{short_id}): {exc}")
            raise HTTPException(402, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ValueError as exc:
            msg = str(exc)
            code = 404 if "não encontrado" in msg.lower() or "sumiu" in msg.lower() else 400
            raise HTTPException(code, msg) from exc
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            tb = traceback.format_exc()
            _append_log(job_id, f"Thumbnail IA falhou (short #{short_id}): {exc}")
            _append_log(job_id, tb)
            raise HTTPException(500, f"Falha ao gerar thumbnail: {exc}") from exc
    try:
        return _enqueue_ai_thumbnail(
            job_id,
            short_id,
            face_candidate_id=body.face_candidate_id,
            mode=body.mode,
            overlay_text=body.overlay_text,
            text_color_mode=body.text_color_mode,
            margin_v=body.margin_v,
            font_size=body.font_size,
            border_pct=body.border_pct,
        )
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "não encontrado" in msg.lower() else 400
        raise HTTPException(code, msg) from exc


@app.post("/api/jobs/{job_id}/highlights/{highlight_id}/generate-thumbnail")
def generate_highlight_ai_thumbnail(
    job_id: str, highlight_id: int, body: Optional[GenerateThumbnailBody] = None
):
    """Enqueue (default) or sync-generate a viral AI thumbnail for a topic."""
    from shorts_generator.thumbnails_ai import ImageBillingError

    body = body or GenerateThumbnailBody()
    if body.sync:
        try:
            return _generate_ai_short_thumbnail(
                job_id,
                highlight_id,
                face_candidate_id=body.face_candidate_id,
                mode=body.mode,
                overlay_text=body.overlay_text,
                text_color_mode=body.text_color_mode,
                margin_v=body.margin_v,
                font_size=body.font_size,
                border_pct=body.border_pct,
            )
        except ImageBillingError as exc:
            _append_log(job_id, f"Thumbnail IA billing (tópico #{highlight_id}): {exc}")
            raise HTTPException(402, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ValueError as exc:
            msg = str(exc)
            code = 404 if "não encontrado" in msg.lower() or "sumiu" in msg.lower() else 400
            raise HTTPException(code, msg) from exc
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            tb = traceback.format_exc()
            _append_log(job_id, f"Thumbnail IA falhou (tópico #{highlight_id}): {exc}")
            _append_log(job_id, tb)
            raise HTTPException(500, f"Falha ao gerar thumbnail: {exc}") from exc
    try:
        return _enqueue_ai_thumbnail(
            job_id,
            highlight_id,
            face_candidate_id=body.face_candidate_id,
            mode=body.mode,
            overlay_text=body.overlay_text,
            text_color_mode=body.text_color_mode,
            margin_v=body.margin_v,
            font_size=body.font_size,
            border_pct=body.border_pct,
        )
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "não encontrado" in msg.lower() else 400
        raise HTTPException(code, msg) from exc


@app.get("/api/jobs/{job_id}/highlights/{highlight_id}/thumbnail-status")
def get_highlight_thumbnail_status(job_id: str, highlight_id: int):
    """Poll async thumbnail generation status for a topic/short id."""
    task_key = _thumb_task_key(job_id, highlight_id)
    with _thumb_tasks_lock:
        task = _thumb_tasks.get(task_key)
        if not task:
            # No in-flight task — report current thumb on disk if any
            dest = JOBS_DIR / job_id / "short_thumbs" / f"{int(highlight_id)}.jpg"
            if dest.exists() and dest.stat().st_size > 0:
                try:
                    version = int(dest.stat().st_mtime)
                except OSError:
                    version = 0
                return {
                    "ok": True,
                    "status": "ready",
                    "job_id": job_id,
                    "highlight_id": int(highlight_id),
                    "short_id": int(highlight_id),
                    "thumbnail_url": f"/api/jobs/{job_id}/short-thumbs/{int(highlight_id)}?v={version}",
                    "thumbnail_ai": True,
                    "has_custom_thumbnail": True,
                }
            return {
                "ok": True,
                "status": "idle",
                "job_id": job_id,
                "highlight_id": int(highlight_id),
                "short_id": int(highlight_id),
            }
        public = _public_thumb_task(task)
        http_status = task.get("http_status")
    if public.get("status") == "failed" and http_status == 402:
        # Keep polling contract (200 + status=failed); UI reads error field.
        pass
    return public


@app.get("/api/jobs/{job_id}/highlights/{highlight_id}/face-candidates")
def list_highlight_face_candidates(job_id: str, highlight_id: int, limit: int = 6):
    """Ranked face frames from the topic range for thumbnail picker."""
    try:
        return _build_face_candidates_for_highlight(
            job_id, highlight_id, limit=limit
        )
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "não encontrado" in msg.lower() else 400
        raise HTTPException(code, msg) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        tb = traceback.format_exc()
        _append_log(job_id, f"Face candidates falhou (#{highlight_id}): {exc}")
        _append_log(job_id, tb)
        raise HTTPException(500, f"Falha ao buscar frames: {exc}") from exc


@app.get("/api/jobs/{job_id}/highlights/{highlight_id}/face-candidates/{candidate_id}")
def get_highlight_face_candidate(job_id: str, highlight_id: int, candidate_id: str):
    try:
        path = _face_candidate_path(job_id, highlight_id, candidate_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(
        path, media_type="image/jpeg", filename=f"face_{highlight_id}_{candidate_id}.jpg"
    )


@app.post("/api/jobs/{job_id}/shorts/{short_id}/youtube")
def upload_short_to_youtube(
    job_id: str, short_id: int, body: Optional[YoutubeUploadBody] = None
):
    """Upload a rendered short to the project's YouTube channel."""
    body = body or YoutubeUploadBody()
    try:
        return _perform_youtube_upload(
            job_id,
            short_id,
            title=body.title,
            description=body.description,
            privacy=body.privacy,
            tags=body.tags,
            category_id=body.category_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "não encontrado" in msg.lower() or "sumiu" in msg.lower() else 400
        raise HTTPException(code, msg) from exc
    except Exception as exc:
        raise HTTPException(502, f"Falha no upload YouTube: {exc}") from exc


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
            status = data.get("status")
            # Mid-render survives restart: keep clips and offer resume on step 5
            if status == "rendering":
                snap = _incomplete_render_snapshot(data)
                _mark_render_interrupted(data, snap)
                _persist_job(data)
            elif status in ("queued", "analyzing", "ranking", "running"):
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
                elif status == "ranking" and (data.get("result") or {}).get(
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
            elif status in ("awaiting_selection", "failed"):
                # Legacy recovery wiped "rendering" → selection; restore if clips remain
                snap = _incomplete_render_snapshot(data)
                phase = ((data.get("result") or {}) if isinstance(data.get("result"), dict) else {}).get(
                    "phase"
                )
                if snap and phase in ("rendering", "failed", "interrupted"):
                    _mark_render_interrupted(data, snap)
                    _persist_job(data)
            # awaiting_cast / completed / interrupted stay as-is
            if isinstance(data.get("result"), dict) and _ensure_shorts_recovered(data):
                _persist_job(data)
            _jobs[job_id] = data
        except Exception:
            continue


_load_persisted_jobs()


def _migrate_youtube_env_to_project() -> None:
    """One-shot: if no projects exist, import YOUTUBE_* from .env into a default project."""
    if _projects.list():
        return
    client_id = (os.getenv("YOUTUBE_CLIENT_ID") or "").strip().strip("'\"")
    client_secret = (os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip().strip("'\"")
    refresh = (os.getenv("YOUTUBE_REFRESH_TOKEN") or "").strip().strip("'\"")
    privacy = (os.getenv("YOUTUBE_PRIVACY_STATUS") or "public").strip().lower()
    if not (
        _is_real_secret(client_id)
        and _is_real_secret(client_secret)
        and _is_real_secret(refresh)
    ):
        return
    if privacy not in ("private", "unlisted", "public"):
        privacy = "public"
    created = _projects.create("Canal padrão")
    _projects.update(
        created["id"],
        youtube={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "privacy_status": privacy,
        },
    )


def _attach_orphan_jobs_to_sole_project() -> None:
    """If there is exactly one project, bind jobs that still lack project_id."""
    projects = _projects.list()
    if len(projects) != 1:
        return
    pid = projects[0]["id"]
    with _jobs_lock:
        for job in _jobs.values():
            if not job.get("project_id"):
                job["project_id"] = pid
                job["updated_at"] = _now()
                _persist_job(job)


_migrate_youtube_env_to_project()
_attach_orphan_jobs_to_sole_project()


def _spa_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/")
def spa_root():
    return _spa_index()


@app.get("/projects/{project_id}")
@app.get("/projects/{project_id}/jobs")
@app.get("/projects/{project_id}/config")
@app.get("/projects/{project_id}/jobs/{job_id}")
def spa_project_pages(project_id: str, job_id: Optional[str] = None):
    return _spa_index()


# Legacy deep-links → SPA (JS redirects into a project when possible)
@app.get("/jobs")
@app.get("/config")
@app.get("/jobs/{job_id}")
def spa_legacy_pages(job_id: Optional[str] = None):
    return _spa_index()


app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")
