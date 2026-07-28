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

CONFIG_KEYS = [
    "MUAPI_API_KEY",
    "MUAPI_BASE_URL",
    "MUAPI_POLL_INTERVAL",
    "MUAPI_POLL_TIMEOUT",
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "LOCAL_WHISPER_MODEL",
    "LOCAL_WHISPER_DEVICE",
    "LOCAL_OUTPUT_DIR",
    "LOCAL_WHISPER_VAD_FILTER",
]

SECRET_KEYS = {"MUAPI_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"}


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
        secret = key in SECRET_KEYS
        items.append(
            {
                "key": key,
                "value": "" if secret else val,
                "masked": _mask(val) if secret else None,
                "is_secret": secret,
                "is_set": bool(val),
            }
        )
    return {
        "items": items,
        "status": {
            "muapi": bool((raw.get("MUAPI_API_KEY") or "").strip()),
            "openai": bool((raw.get("OPENAI_API_KEY") or "").strip()),
            "gemini": bool((raw.get("GEMINI_API_KEY") or "").strip()),
            "llm_provider": (raw.get("LLM_PROVIDER") or "openai").strip().lower(),
        },
    }


def _write_config(updates: Dict[str, str]) -> None:
    if not ENV_PATH.exists():
        ENV_PATH.write_text("", encoding="utf-8")
    for key, value in updates.items():
        if key not in CONFIG_KEYS:
            continue
        # Skip blank secret updates — keep existing value
        if key in SECRET_KEYS and not str(value).strip():
            continue
        set_key(str(ENV_PATH), key, str(value).strip())
        os.environ[key] = str(value).strip()
    # Reload config module globals used by the pipeline
    load_dotenv(ENV_PATH, override=True)
    import shorts_generator.config as cfg

    cfg.MUAPI_API_KEY = os.getenv("MUAPI_API_KEY", "").strip()
    cfg.MUAPI_BASE_URL = os.getenv("MUAPI_BASE_URL", "https://api.muapi.ai/api/v1").rstrip("/")
    cfg.POLL_INTERVAL_SECONDS = float(os.getenv("MUAPI_POLL_INTERVAL", "5"))
    cfg.POLL_TIMEOUT_SECONDS = float(os.getenv("MUAPI_POLL_TIMEOUT", "600"))
    cfg.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    cfg.OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    cfg.GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    cfg.GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    cfg.LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    cfg.LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "base")
    cfg.LOCAL_WHISPER_DEVICE = os.getenv("LOCAL_WHISPER_DEVICE", "auto")
    cfg.LOCAL_OUTPUT_DIR = os.getenv("LOCAL_OUTPUT_DIR", "output")
    cfg.LOCAL_WHISPER_VAD_FILTER = (
        os.getenv("LOCAL_WHISPER_VAD_FILTER", "false").strip().lower() == "true"
    )


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


def _run_job(job_id: str) -> None:
    from shorts_generator import generate_shorts

    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "running"
        job["updated_at"] = _now()
        params = dict(job["params"])
        _persist_job(job)

    _append_log(job_id, f"Iniciando pipeline (mode={params['mode']})…")
    capture = LogCapture(job_id, sys.stdout)
    err_capture = LogCapture(job_id, sys.stderr)

    try:
        with redirect_stdout(capture), redirect_stderr(err_capture):
            result = generate_shorts(
                youtube_url=params["url"],
                num_clips=params["num_clips"],
                aspect_ratio=params["aspect_ratio"],
                download_format=params["download_format"],
                language=params.get("language") or None,
                mode=params["mode"],
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
            f"Concluído: {len(result.get('shorts', []))} shorts "
            f"de {len(result.get('highlights', []))} candidatos.",
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


@app.get("/api/health")
def health():
    cfg = _read_config()
    return {"ok": True, "config": cfg["status"]}


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


@app.post("/api/jobs")
async def create_job(
    url: Optional[str] = Form(None),
    mode: str = Form("api"),
    num_clips: int = Form(3),
    aspect_ratio: str = Form("9:16"),
    download_format: str = Form("720"),
    language: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    mode = (mode or "api").lower().strip()
    if mode not in ("api", "local"):
        raise HTTPException(400, "mode deve ser 'api' ou 'local'")
    if num_clips < 1 or num_clips > 20:
        raise HTTPException(400, "num_clips deve estar entre 1 e 20")
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

    lang = (language or "").strip() or None
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "queued",
        "params": {
            "url": source,
            "mode": mode,
            "num_clips": num_clips,
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

    thread = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
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
            # Mark interrupted runs as failed
            if data.get("status") in ("queued", "running"):
                data["status"] = "failed"
                data["error"] = data.get("error") or "Interrompido (servidor reiniciou)"
            data.setdefault("logs", [])
            _jobs[job_id] = data
        except Exception:
            continue


_load_persisted_jobs()

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
