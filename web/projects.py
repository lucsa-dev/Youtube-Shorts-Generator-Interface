"""Per-channel project store (JSON files under projects/)."""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_PLACEHOLDER_RE = re.compile(
    r"^(your[_-].*[_-]here|changeme|xxx+|replace.?me|<.*>|todo|fix)$",
    re.IGNORECASE,
)

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_real(value: Optional[str]) -> bool:
    val = (value or "").strip().strip("'\"")
    if not val:
        return False
    if _PLACEHOLDER_RE.match(val):
        return False
    return True


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return value[:4] + "••••" + value[-4:]


def youtube_ready(yt: Optional[Dict[str, Any]]) -> bool:
    yt = yt or {}
    return (
        _is_real(yt.get("client_id"))
        and _is_real(yt.get("client_secret"))
        and _is_real(yt.get("refresh_token"))
    )


class ProjectStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: str) -> Path:
        return self.root / f"{project_id}.json"

    def list(self) -> List[Dict[str, Any]]:
        with _lock:
            items = []
            for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                items.append(self._public(data))
            return items

    def get(self, project_id: str) -> Optional[Dict[str, Any]]:
        with _lock:
            return self._read(project_id)

    def get_public(self, project_id: str) -> Optional[Dict[str, Any]]:
        data = self.get(project_id)
        return self._public(data) if data else None

    def create(self, name: str) -> Dict[str, Any]:
        name = (name or "").strip() or "Novo canal"
        project_id = uuid.uuid4().hex[:12]
        now = _now()
        data = {
            "id": project_id,
            "name": name,
            "created_at": now,
            "updated_at": now,
            "youtube": {
                "client_id": "",
                "client_secret": "",
                "refresh_token": "",
                "privacy_status": "private",
                "channel_title": "",
                "channel_id": "",
            },
        }
        with _lock:
            self._write(data)
        return self._public(data)

    def update(self, project_id: str, *, name: Optional[str] = None, youtube: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with _lock:
            data = self._read(project_id)
            if not data:
                raise KeyError(project_id)
            if name is not None:
                cleaned = name.strip()
                if cleaned:
                    data["name"] = cleaned
            if youtube is not None:
                yt = dict(data.get("youtube") or {})
                for key in (
                    "client_id",
                    "client_secret",
                    "refresh_token",
                    "privacy_status",
                    "channel_title",
                    "channel_id",
                ):
                    if key not in youtube:
                        continue
                    val = youtube[key]
                    if val is None:
                        continue
                    # Blank secret fields keep previous value (avoid wiping on save)
                    if key in ("client_secret", "refresh_token") and not str(val).strip():
                        continue
                    yt[key] = str(val).strip()
                privacy = (yt.get("privacy_status") or "private").lower()
                if privacy not in ("private", "unlisted", "public"):
                    privacy = "private"
                yt["privacy_status"] = privacy
                data["youtube"] = yt
            data["updated_at"] = _now()
            self._write(data)
            return self._public(data)

    def set_youtube_tokens(
        self,
        project_id: str,
        *,
        refresh_token: str,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        channel_title: str = "",
        channel_id: str = "",
    ) -> Dict[str, Any]:
        with _lock:
            data = self._read(project_id)
            if not data:
                raise KeyError(project_id)
            yt = dict(data.get("youtube") or {})
            if client_id:
                yt["client_id"] = client_id.strip()
            if client_secret:
                yt["client_secret"] = client_secret.strip()
            yt["refresh_token"] = refresh_token.strip()
            if channel_title:
                yt["channel_title"] = channel_title.strip()
            if channel_id:
                yt["channel_id"] = channel_id.strip()
            data["youtube"] = yt
            data["updated_at"] = _now()
            self._write(data)
            return self._public(data)

    def delete(self, project_id: str) -> bool:
        with _lock:
            path = self._path(project_id)
            if not path.exists():
                return False
            path.unlink()
            return True

    def youtube_credentials(self, project_id: str) -> Optional[Dict[str, str]]:
        data = self.get(project_id)
        if not data:
            return None
        yt = data.get("youtube") or {}
        if not youtube_ready(yt):
            return None
        return {
            "client_id": str(yt.get("client_id") or "").strip(),
            "client_secret": str(yt.get("client_secret") or "").strip(),
            "refresh_token": str(yt.get("refresh_token") or "").strip(),
            "privacy_status": str(yt.get("privacy_status") or "private").strip().lower(),
        }

    def _read(self, project_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(project_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        data.setdefault("id", project_id)
        data.setdefault("youtube", {})
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        path = self._path(data["id"])
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _public(self, data: Dict[str, Any]) -> Dict[str, Any]:
        yt = dict(data.get("youtube") or {})
        client_id = str(yt.get("client_id") or "")
        client_secret = str(yt.get("client_secret") or "")
        refresh = str(yt.get("refresh_token") or "")
        return {
            "id": data.get("id"),
            "name": data.get("name") or "Sem nome",
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "youtube": {
                "client_id": client_id,
                "client_secret_set": _is_real(client_secret),
                "client_secret_masked": _mask(client_secret) if _is_real(client_secret) else None,
                "refresh_token_set": _is_real(refresh),
                "refresh_token_masked": _mask(refresh) if _is_real(refresh) else None,
                "privacy_status": (yt.get("privacy_status") or "private"),
                "channel_title": yt.get("channel_title") or "",
                "channel_id": yt.get("channel_id") or "",
                "configured": youtube_ready(yt),
            },
        }
