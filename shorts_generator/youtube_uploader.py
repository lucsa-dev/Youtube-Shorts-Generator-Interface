"""Upload vertical shorts to YouTube via Data API v3 (OAuth refresh token)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
MAX_TITLE_LEN = 100

_PLACEHOLDER_RE = re.compile(
    r"^(your[_-].*[_-]here|changeme|xxx+|replace.?me|<.*>|todo|fix)$",
    re.IGNORECASE,
)


def _env(key: str) -> str:
    return (os.getenv(key) or "").strip().strip("'\"")


def _is_real(value: str) -> bool:
    val = (value or "").strip()
    if not val:
        return False
    if _PLACEHOLDER_RE.match(val):
        return False
    return True


def credentials_configured() -> bool:
    return (
        _is_real(_env("YOUTUBE_CLIENT_ID"))
        and _is_real(_env("YOUTUBE_CLIENT_SECRET"))
        and _is_real(_env("YOUTUBE_REFRESH_TOKEN"))
    )


def privacy_status() -> str:
    raw = (_env("YOUTUBE_PRIVACY_STATUS") or "private").lower()
    if raw not in ("private", "unlisted", "public"):
        return "private"
    return raw


def _credentials():
    from google.oauth2.credentials import Credentials

    if not credentials_configured():
        raise RuntimeError(
            "Credenciais YouTube ausentes. Defina YOUTUBE_CLIENT_ID, "
            "YOUTUBE_CLIENT_SECRET e YOUTUBE_REFRESH_TOKEN no .env "
            "(veja scripts/youtube_oauth.py)."
        )
    return Credentials(
        token=None,
        refresh_token=_env("YOUTUBE_REFRESH_TOKEN"),
        token_uri=TOKEN_URI,
        client_id=_env("YOUTUBE_CLIENT_ID"),
        client_secret=_env("YOUTUBE_CLIENT_SECRET"),
        scopes=SCOPES,
    )


def _youtube_service():
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=_credentials(), cache_discovery=False)


def _truncate_title(title: str) -> str:
    title = re.sub(r"\s+", " ", (title or "").strip()) or "Short"
    if len(title) <= MAX_TITLE_LEN:
        return title
    return title[: MAX_TITLE_LEN - 1].rstrip() + "…"


def build_description(
    *,
    hook: str = "",
    reason: str = "",
    extra: str = "",
) -> str:
    parts: List[str] = []
    hook = (hook or "").strip()
    reason = (reason or "").strip()
    extra = (extra or "").strip()
    if hook:
        parts.append(hook)
    if reason:
        parts.append(reason)
    if extra:
        parts.append(extra)
    parts.append("#Shorts")
    return "\n\n".join(parts)


def upload_video(
    file_path: str | Path,
    *,
    title: str,
    description: str = "",
    tags: Optional[List[str]] = None,
    privacy: Optional[str] = None,
    category_id: str = "22",
) -> Dict[str, Any]:
    """Upload an mp4 and return {video_id, url, title, privacy_status}."""
    from googleapiclient.http import MediaFileUpload

    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo de vídeo não encontrado: {path}")

    status = (privacy or privacy_status()).lower()
    if status not in ("private", "unlisted", "public"):
        status = "private"

    tag_list = [t.strip() for t in (tags or ["Shorts"]) if t and str(t).strip()]
    if "Shorts" not in tag_list:
        tag_list.append("Shorts")

    body = {
        "snippet": {
            "title": _truncate_title(title),
            "description": description or "#Shorts",
            "tags": tag_list,
            "categoryId": str(category_id or "22"),
        },
        "status": {
            "privacyStatus": status,
            "selfDeclaredMadeForKids": False,
        },
    }

    youtube = _youtube_service()
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _status, response = request.next_chunk()

    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"Upload YouTube sem video id: {response}")

    return {
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "title": body["snippet"]["title"],
        "privacy_status": status,
    }
