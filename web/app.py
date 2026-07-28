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
from typing import Any, Dict, List, Optional

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
    if clip and not clip.startswith("http"):
        # Local path → serve via our API
        out["clip_url"] = f"/api/jobs/{job_id}/clips/{index}"
        out["local_path"] = clip
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
    # Truncate transcript segments in API responses for speed
    transcript = public.get("transcript")
    if isinstance(transcript, dict) and "segments" in transcript:
        segs = transcript["segments"]
        public["transcript"] = {
            "duration": transcript.get("duration"),
            "language": transcript.get("language"),
            "segment_count": len(segs) if isinstance(segs, list) else 0,
        }
    return public


def _public_highlight(job_id: str, highlight: Dict[str, Any], index: int) -> Dict[str, Any]:
    out = dict(highlight)
    hid = int(out.get("id", index))
    thumb_path = JOBS_DIR / job_id / "thumbs" / f"{hid}.jpg"
    if thumb_path.exists():
        out["thumbnail_url"] = f"/api/jobs/{job_id}/thumbs/{hid}"
    elif out.get("thumbnail_url"):
        pass  # already set (e.g. YouTube fallback)
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


def _generate_thumbnails(job_id: str, result: Dict[str, Any], original_url: str = "") -> None:
    """Best-effort frame grabs for each highlight; YouTube poster as fallback."""
    source = result.get("source_video_url") or ""
    yt_id = _youtube_id_from_url(original_url) or _youtube_id_from_url(source)
    local_source = Path(source)
    if not local_source.is_absolute() and source and not source.startswith("http"):
        local_source = ROOT / source
    use_ffmpeg = False
    ffmpeg_src = source
    if local_source.exists() and local_source.is_file():
        use_ffmpeg = True
        ffmpeg_src = str(local_source)
    elif source.startswith("http"):
        use_ffmpeg = True
        ffmpeg_src = source

    for i, h in enumerate(result.get("highlights") or []):
        hid = int(h.get("id", i))
        dest = JOBS_DIR / job_id / "thumbs" / f"{hid}.jpg"
        ok = False
        if use_ffmpeg:
            ok = _extract_frame(ffmpeg_src, float(h.get("start_time", 0)), dest)
        if not ok and yt_id:
            h["thumbnail_url"] = f"https://i.ytimg.com/vi/{yt_id}/hqdefault.jpg"


def _run_analyze(job_id: str) -> None:
    from shorts_generator import analyze_video

    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "analyzing"
        job["updated_at"] = _now()
        params = dict(job["params"])
        _persist_job(job)

    _append_log(job_id, f"Analisando vídeo (mode={params['mode']})…")
    capture = LogCapture(job_id, sys.stdout)
    err_capture = LogCapture(job_id, sys.stderr)

    try:
        with redirect_stdout(capture), redirect_stderr(err_capture):
            result = analyze_video(
                youtube_url=params["url"],
                download_format=params["download_format"],
                language=params.get("language") or None,
                mode=params["mode"],
            )
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
            job["status"] = "failed"
            job["error"] = str(e)
            job["updated_at"] = _now()
            job["finished_at"] = _now()
            _persist_job(job)
        _append_log(job_id, f"ERRO: {e}")
        _append_log(job_id, tb)


def _run_render(job_id: str, selected_ids: List[int]) -> None:
    from shorts_generator import render_selected_shorts

    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "rendering"
        job["updated_at"] = _now()
        params = dict(job["params"])
        analysis = dict(job.get("result") or {})
        job["params"]["selected_ids"] = selected_ids
        _persist_job(job)

    _append_log(job_id, f"Cortando {len(selected_ids)} tópicos selecionados…")
    capture = LogCapture(job_id, sys.stdout)
    err_capture = LogCapture(job_id, sys.stderr)

    try:
        with redirect_stdout(capture), redirect_stderr(err_capture):
            result = render_selected_shorts(
                analysis,
                selected_ids,
                aspect_ratio=params.get("aspect_ratio") or "9:16",
            )
        with _jobs_lock:
            job = _jobs[job_id]
            job["status"] = "completed"
            job["result"] = result
            job["error"] = None
            job["updated_at"] = _now()
            job["finished_at"] = _now()
            _persist_job(job)
        _append_log(
            job_id,
            f"Concluído: {len(result.get('shorts', []))} shorts gerados.",
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


class SelectHighlights(BaseModel):
    ids: List[int] = Field(default_factory=list)


@app.get("/api/health")
def health():
    cfg = _read_config()
    return {"ok": True, "config": cfg["status"]}


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
                    "has_transcript_cache": srt.exists(),
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


@app.get("/api/jobs/{job_id}/clips/{index}")
def get_clip(job_id: str, index: int):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job.get("result"):
            raise HTTPException(404, "Job/resultado não encontrado")
        shorts = job["result"].get("shorts", [])
        if index < 0 or index >= len(shorts):
            raise HTTPException(404, "Clip não encontrado")
        clip = shorts[index].get("clip_url") or ""
    if clip.startswith("http"):
        return JSONResponse({"redirect": clip})
    path = Path(clip)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise HTTPException(404, f"Arquivo não encontrado: {path}")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/jobs/{job_id}/thumbs/{index}")
def get_thumb(job_id: str, index: int):
    path = JOBS_DIR / job_id / "thumbs" / f"{index}.jpg"
    if not path.exists():
        raise HTTPException(404, "Miniatura não encontrada")
    return FileResponse(path, media_type="image/jpeg", filename=f"thumb_{index}.jpg")


@app.post("/api/jobs/{job_id}/select")
def select_highlights(job_id: str, body: SelectHighlights):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado")
        if job["status"] != "awaiting_selection":
            raise HTTPException(
                400,
                f"Job não está aguardando seleção (status={job['status']})",
            )
        result = job.get("result") or {}
        highlights = result.get("highlights") or []
        valid_ids = {int(h.get("id", i)) for i, h in enumerate(highlights)}
        ids = [int(i) for i in (body.ids or [])]
        if not ids:
            raise HTTPException(400, "Selecione ao menos um tópico")
        bad = [i for i in ids if i not in valid_ids]
        if bad:
            raise HTTPException(400, f"IDs inválidos: {bad}")
        # Keep chronological order of selection by highlight start time
        by_id = {int(h.get("id", i)): h for i, h in enumerate(highlights)}
        ids = sorted(ids, key=lambda i: float(by_id[i].get("start_time", 0)))

    thread = threading.Thread(target=_run_render, args=(job_id, ids), daemon=True)
    thread.start()
    return {"id": job_id, "status": "rendering", "selected_ids": ids}


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
            # Mark interrupted in-flight runs as failed; keep selection pause
            if data.get("status") in ("queued", "analyzing", "rendering", "running"):
                data["status"] = "failed"
                data["error"] = data.get("error") or "Interrompido (servidor reiniciou)"
            # awaiting_selection + completed/failed stay as-is
            data.setdefault("logs", [])
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
