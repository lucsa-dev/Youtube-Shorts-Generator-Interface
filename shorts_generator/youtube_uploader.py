"""Upload vertical shorts to YouTube via Data API v3 (OAuth refresh token)."""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# upload + thumbnails.set (force-ssl). Existing tokens with only .upload still
# upload video; thumbnail needs re-auth with force-ssl.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
MAX_TITLE_LEN = 100
MAX_DESCRIPTION_LEN = 5000
MAX_TAGS_CHARS = 480  # YouTube limit is ~500; leave headroom
MAX_TAG_LEN = 30
MAX_HASHTAGS = 6

_PLACEHOLDER_RE = re.compile(
    r"^(your[_-].*[_-]here|changeme|xxx+|replace.?me|<.*>|todo|fix)$",
    re.IGNORECASE,
)
_YT_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?.*?v=|shorts/|embed/|live/))([A-Za-z0-9_-]{11})"
)
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9''-]{1,}", re.UNICODE)

# YouTube categoryId values used for Shorts niches.
_CATEGORY_BY_CONTENT = {
    "podcast": "24",  # Entertainment
    "interview": "24",
    "debate": "25",  # News & Politics (often topical)
    "commentary": "24",
    "tutorial": "27",  # Education
    "lecture": "27",
    "vlog": "22",  # People & Blogs
    "other": "22",
}

_CTA_BY_LANG = {
    "pt": "👉 Se gostou, deixa o like e se inscreve no canal para mais cortes!",
    "en": "👉 Like & subscribe for more shorts from the full episode!",
    "es": "👉 ¡Dale like y suscríbete para más cortes del episodio completo!",
    "fr": "👉 Likez et abonnez-vous pour plus de clips de l'épisode !",
    "it": "👉 Metti like e iscriviti per altri shorts dall'episodio!",
    "de": "👉 Like & abonnieren für mehr Shorts aus der Folge!",
}

_FULL_EPISODE_BY_LANG = {
    "pt": "Assista o episódio completo",
    "en": "Watch the full episode",
    "es": "Mira el episodio completo",
    "fr": "Voir l'épisode complet",
    "it": "Guarda l'episodio completo",
    "de": "Ganze Folge ansehen",
}

_FEATURING_BY_LANG = {
    "pt": "Com",
    "en": "Featuring",
    "es": "Con",
    "fr": "Avec",
    "it": "Con",
    "de": "Mit",
}

_STOPWORDS = {
    "pt": {
        "a", "o", "os", "as", "um", "uma", "de", "da", "do", "das", "dos", "e",
        "em", "no", "na", "nos", "nas", "por", "para", "com", "sem", "que", "se",
        "ao", "à", "às", "ou", "é", "foi", "ser", "tem", "mais", "muito", "já",
        "não", "sim", "eu", "ele", "ela", "eles", "elas", "você", "vocês", "meu",
        "sua", "seu", "the", "and", "of", "flow", "podcast", "shorts", "corte",
        "cortes", "clip", "vídeo", "video", "entrevista", "ep", "episódio",
        "fala", "falar", "sobre", "isso", "essa", "esse", "aqui", "ali", "ainda",
        "como", "quando", "onde", "porque", "porquê", "também", "só", "entre",
        "depois", "antes", "hoje", "agora", "tudo", "nada", "alguém", "ninguém",
        "gera", "faz", "fazer", "tem", "têm", "está", "estão", "vai", "vão",
    },
    "en": {
        "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
        "from", "by", "is", "are", "was", "were", "be", "this", "that", "it",
        "as", "at", "podcast", "shorts", "clip", "video", "interview", "episode",
        "ep", "full", "about", "talks", "says", "just", "really", "very",
    },
    "es": {
        "el", "la", "los", "las", "un", "una", "de", "del", "y", "en", "con",
        "por", "para", "que", "se", "es", "podcast", "shorts", "clip", "video",
        "entrevista", "episodio", "sobre", "habla", "como",
    },
}


def _env(key: str) -> str:
    return (os.getenv(key) or "").strip().strip("'\"")


def _is_real(value: str) -> bool:
    val = (value or "").strip()
    if not val:
        return False
    if _PLACEHOLDER_RE.match(val):
        return False
    return True


def _normalize_creds(credentials: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Resolve upload credentials from an explicit dict or legacy .env vars."""
    if credentials:
        return {
            "client_id": str(credentials.get("client_id") or "").strip(),
            "client_secret": str(credentials.get("client_secret") or "").strip(),
            "refresh_token": str(credentials.get("refresh_token") or "").strip(),
            "privacy_status": str(
                credentials.get("privacy_status") or "private"
            ).strip().lower(),
        }
    return {
        "client_id": _env("YOUTUBE_CLIENT_ID"),
        "client_secret": _env("YOUTUBE_CLIENT_SECRET"),
        "refresh_token": _env("YOUTUBE_REFRESH_TOKEN"),
        "privacy_status": (_env("YOUTUBE_PRIVACY_STATUS") or "private").lower(),
    }


def credentials_configured(credentials: Optional[Dict[str, Any]] = None) -> bool:
    creds = _normalize_creds(credentials)
    return (
        _is_real(creds["client_id"])
        and _is_real(creds["client_secret"])
        and _is_real(creds["refresh_token"])
    )


def privacy_status(credentials: Optional[Dict[str, Any]] = None) -> str:
    raw = _normalize_creds(credentials).get("privacy_status") or "private"
    if raw not in ("private", "unlisted", "public"):
        return "private"
    return raw


def _credentials(credentials: Optional[Dict[str, Any]] = None):
    from google.oauth2.credentials import Credentials

    creds = _normalize_creds(credentials)
    if not credentials_configured(creds):
        raise RuntimeError(
            "Credenciais YouTube ausentes. Configure o canal no projeto "
            "(Client ID, Client Secret e Refresh Token)."
        )
    return Credentials(
        token=None,
        refresh_token=creds["refresh_token"],
        token_uri=TOKEN_URI,
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        scopes=SCOPES,
    )


def _youtube_service(credentials: Optional[Dict[str, Any]] = None):
    from googleapiclient.discovery import build

    return build(
        "youtube", "v3", credentials=_credentials(credentials), cache_discovery=False
    )


def _truncate_title(title: str) -> str:
    title = re.sub(r"\s+", " ", (title or "").strip()) or "Short"
    if len(title) <= MAX_TITLE_LEN:
        return title
    return title[: MAX_TITLE_LEN - 1].rstrip() + "…"


def _truncate_description(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_DESCRIPTION_LEN:
        return text
    return text[: MAX_DESCRIPTION_LEN - 1].rstrip() + "…"


def youtube_id_from_url(url: str) -> Optional[str]:
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m else None


def source_watch_url(url: str = "", *, start_time: Optional[float] = None) -> str:
    """Build a clean watch URL, optionally with &t= for the clip start."""
    raw = (url or "").strip()
    vid = youtube_id_from_url(raw)
    if not vid:
        return raw
    out = f"https://www.youtube.com/watch?v={vid}"
    if start_time is not None:
        try:
            secs = max(0, int(float(start_time)))
        except (TypeError, ValueError):
            secs = 0
        if secs > 0:
            out = f"{out}&t={secs}s"
    return out


def _lang_code(language: Optional[str]) -> str:
    raw = (language or "pt").strip().lower().replace("_", "-")
    if not raw or raw in ("auto", "detect", "none"):
        return "pt"
    return raw.split("-", 1)[0][:8]


def _yt_language_tag(language: Optional[str]) -> str:
    """BCP-47-ish tag for defaultLanguage / defaultAudioLanguage."""
    code = _lang_code(language)
    if code == "pt":
        return "pt-BR"
    return code


def category_for_content(content_type: Optional[str] = None) -> str:
    key = (content_type or "other").strip().lower()
    return _CATEGORY_BY_CONTENT.get(key, "22")


def _fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _hashtag_slug(text: str) -> str:
    """Turn a phrase into a PascalCase-ish hashtag body (no #)."""
    words = _WORD_RE.findall(text or "")
    if not words:
        return ""
    body = "".join(w[:1].upper() + w[1:] for w in words[:6])
    body = re.sub(r"[^A-Za-zÀ-ÿ0-9]", "", body)
    return body[:40]


def _unique_preserve(items: Sequence[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        key = _fold(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _keyword_candidates(
    *,
    title: str = "",
    hook: str = "",
    reason: str = "",
    attributed_to: str = "",
    source_title: str = "",
    channel: str = "",
    speakers: Optional[Sequence[str]] = None,
    language: str = "pt",
) -> List[str]:
    stop = _STOPWORDS.get(_lang_code(language), _STOPWORDS["en"])
    bag: List[str] = []

    for name in [attributed_to, channel, *(speakers or [])]:
        name = re.sub(r"\s+", " ", (name or "").strip())
        if len(name) >= 2:
            bag.append(name)

    for blob in (title, hook, reason, source_title):
        for word in _WORD_RE.findall(blob or ""):
            if _fold(word) in stop:
                continue
            if len(word) < 3:
                continue
            bag.append(word)

    # Prefer multi-word names already collected; then single keywords.
    return _unique_preserve(bag)


def build_tags(
    *,
    title: str = "",
    hook: str = "",
    reason: str = "",
    attributed_to: str = "",
    source_title: str = "",
    channel: str = "",
    speakers: Optional[Sequence[str]] = None,
    content_type: Optional[str] = None,
    language: str = "pt",
    extra: Optional[Sequence[str]] = None,
) -> List[str]:
    """Build YouTube snippet.tags (≤ ~500 chars total)."""
    seeds: List[str] = ["Shorts", "YouTube Shorts", "corte", "viral"]
    ctype = (content_type or "").strip().lower()
    if ctype and ctype != "other":
        seeds.append(ctype)
    if ctype in ("podcast", "interview", "debate"):
        seeds.extend(["podcast", "entrevista", "corte de podcast"])

    keywords = _keyword_candidates(
        title=title,
        hook=hook,
        reason=reason,
        attributed_to=attributed_to,
        source_title=source_title,
        channel=channel,
        speakers=speakers,
        language=language,
    )
    seeds.extend(keywords[:12])
    if extra:
        seeds.extend(str(x) for x in extra)

    cleaned: List[str] = []
    total = 0
    for raw in _unique_preserve(seeds):
        tag = re.sub(r"\s+", " ", str(raw).strip())
        if not tag or len(tag) > MAX_TAG_LEN:
            # Keep long proper names truncated rather than drop.
            tag = tag[:MAX_TAG_LEN].rstrip()
        if not tag:
            continue
        # Commas break YouTube tag parsing.
        tag = tag.replace(",", " ")
        cost = len(tag) + (1 if cleaned else 0)
        if total + cost > MAX_TAGS_CHARS:
            break
        cleaned.append(tag)
        total += cost
        if len(cleaned) >= 15:
            break

    if "Shorts" not in cleaned:
        cleaned.append("Shorts")
    return cleaned


def build_hashtags(
    *,
    attributed_to: str = "",
    channel: str = "",
    content_type: Optional[str] = None,
    keywords: Optional[Sequence[str]] = None,
    language: str = "pt",
) -> List[str]:
    """Return hashtag strings including leading # (Shorts last)."""
    tags: List[str] = []
    for name in (attributed_to, channel):
        slug = _hashtag_slug(name)
        if slug and len(slug) >= 3:
            tags.append(f"#{slug}")

    ctype = (content_type or "").strip().lower()
    type_map = {
        "podcast": "#Podcast",
        "interview": "#Entrevista" if _lang_code(language) == "pt" else "#Interview",
        "debate": "#Debate",
        "tutorial": "#Tutorial",
        "lecture": "#Educacao" if _lang_code(language) == "pt" else "#Education",
        "commentary": "#Comentario" if _lang_code(language) == "pt" else "#Commentary",
        "vlog": "#Vlog",
    }
    if ctype in type_map:
        tags.append(type_map[ctype])

    for kw in keywords or []:
        slug = _hashtag_slug(str(kw))
        if slug and len(slug) >= 4:
            tags.append(f"#{slug}")
        if len(tags) >= MAX_HASHTAGS - 1:
            break

    tags = _unique_preserve(tags)[: MAX_HASHTAGS - 1]
    tags.append("#Shorts")
    return tags


def enhance_title(
    title: str,
    *,
    attributed_to: str = "",
    primary_keyword: str = "",
) -> str:
    """Keep viral title; lightly inject searchable name/keyword when missing."""
    base = re.sub(r"\s+", " ", (title or "").strip()) or "Short"
    person = re.sub(r"\s+", " ", (attributed_to or "").strip())
    keyword = re.sub(r"\s+", " ", (primary_keyword or "").strip())

    # If a public figure is attributed and not already in the title, append
    # a short "— Name" when it still fits (searchable without killing the hook).
    if person and _fold(person) not in _fold(base):
        candidate = f"{base} — {person}"
        if len(candidate) <= MAX_TITLE_LEN:
            base = candidate
        elif len(base) + 3 + len(person.split()[0]) <= MAX_TITLE_LEN:
            base = f"{base} — {person.split()[0]}"

    if keyword and _fold(keyword) not in _fold(base):
        # Only weave short keywords that fit.
        if len(keyword) <= 24:
            candidate = f"{base} | {keyword}"
            if len(candidate) <= MAX_TITLE_LEN:
                base = candidate

    return _truncate_title(base)


def build_description(
    *,
    hook: str = "",
    reason: str = "",
    extra: str = "",
    attributed_to: str = "",
    source_title: str = "",
    source_url: str = "",
    channel: str = "",
    start_time: Optional[float] = None,
    hashtags: Optional[Sequence[str]] = None,
    language: str = "pt",
    cta: str = "",
) -> str:
    """SEO-oriented Shorts description: hook, context, credit, CTA, hashtags."""
    lang = _lang_code(language)
    parts: List[str] = []

    hook = (hook or "").strip()
    reason = (reason or "").strip()
    extra = (extra or "").strip()
    if hook:
        parts.append(hook)
    if reason and _fold(reason) != _fold(hook):
        parts.append(reason)
    if extra:
        parts.append(extra)

    credit_bits: List[str] = []
    person = (attributed_to or "").strip()
    src_title = (source_title or "").strip()
    ch = (channel or "").strip()
    if person:
        label = _FEATURING_BY_LANG.get(lang, _FEATURING_BY_LANG["en"])
        credit_bits.append(f"{label}: {person}")
    if src_title:
        credit_bits.append(src_title)
    elif ch:
        credit_bits.append(ch)
    if credit_bits:
        parts.append(" · ".join(credit_bits))

    watch = source_watch_url(source_url, start_time=start_time)
    if watch:
        label = _FULL_EPISODE_BY_LANG.get(lang, _FULL_EPISODE_BY_LANG["en"])
        parts.append(f"{label}:\n{watch}")

    cta_line = (cta or "").strip() or _CTA_BY_LANG.get(lang, _CTA_BY_LANG["en"])
    if cta_line:
        parts.append(cta_line)

    tag_line = " ".join(
        t if str(t).startswith("#") else f"#{t}"
        for t in (hashtags or ["#Shorts"])
        if t
    ).strip()
    if tag_line:
        parts.append(tag_line)
    elif not any("#shorts" in p.lower() for p in parts):
        parts.append("#Shorts")

    return _truncate_description("\n\n".join(parts))


def build_seo_metadata(
    *,
    title: str = "",
    hook: str = "",
    reason: str = "",
    attributed_to: str = "",
    source_title: str = "",
    source_url: str = "",
    channel: str = "",
    speakers: Optional[Sequence[str]] = None,
    content_type: Optional[str] = None,
    start_time: Optional[float] = None,
    language: str = "pt",
    extra_description: str = "",
    extra_tags: Optional[Sequence[str]] = None,
    category_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble title/description/tags/hashtags/language/category for upload."""
    lang = _lang_code(language)
    keywords = _keyword_candidates(
        title=title,
        hook=hook,
        reason=reason,
        attributed_to=attributed_to,
        source_title=source_title,
        channel=channel,
        speakers=speakers,
        language=lang,
    )
    primary = ""
    # Prefer attributed person, else first non-channel keyword.
    if (attributed_to or "").strip():
        primary = attributed_to.strip()
    elif keywords:
        primary = keywords[0]

    seo_title = enhance_title(
        title, attributed_to=attributed_to, primary_keyword=""
    )
    tags = build_tags(
        title=title,
        hook=hook,
        reason=reason,
        attributed_to=attributed_to,
        source_title=source_title,
        channel=channel,
        speakers=speakers,
        content_type=content_type,
        language=lang,
        extra=extra_tags,
    )
    hashtags = build_hashtags(
        attributed_to=attributed_to,
        channel=channel,
        content_type=content_type,
        keywords=keywords,
        language=lang,
    )
    description = build_description(
        hook=hook,
        reason=reason,
        extra=extra_description,
        attributed_to=attributed_to,
        source_title=source_title,
        source_url=source_url,
        channel=channel,
        start_time=start_time,
        hashtags=hashtags,
        language=lang,
    )
    cat = str(category_id or category_for_content(content_type) or "22")
    yt_lang = _yt_language_tag(lang)

    return {
        "title": seo_title,
        "description": description,
        "tags": tags,
        "hashtags": hashtags,
        "category_id": cat,
        "default_language": yt_lang,
        "default_audio_language": yt_lang,
        "primary_keyword": primary,
        "source_watch_url": source_watch_url(source_url, start_time=start_time),
    }


def fetch_channel_info(credentials: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Return {channel_id, channel_title} for the authenticated account."""
    youtube = _youtube_service(credentials)
    resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        return {"channel_id": "", "channel_title": ""}
    item = items[0]
    return {
        "channel_id": str(item.get("id") or ""),
        "channel_title": str((item.get("snippet") or {}).get("title") or ""),
    }


def run_oauth_flow(client_id: str, client_secret: str) -> Dict[str, str]:
    """Desktop/local OAuth; returns refresh_token (+ channel info when possible)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_id = (client_id or "").strip().strip("'\"")
    client_secret = (client_secret or "").strip().strip("'\"")
    if not client_id or not client_secret:
        raise ValueError("Client ID e Client Secret são obrigatórios")

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        raise RuntimeError(
            "Nenhum refresh token retornado. Revogue o acesso do app em "
            "https://myaccount.google.com/permissions e tente de novo."
        )

    out = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": creds.refresh_token,
        "channel_id": "",
        "channel_title": "",
    }
    try:
        info = fetch_channel_info(out)
        out["channel_id"] = info.get("channel_id") or ""
        out["channel_title"] = info.get("channel_title") or ""
    except Exception:
        pass
    return out


def set_thumbnail(
    video_id: str,
    thumbnail_path: str | Path,
    *,
    credentials: Optional[Dict[str, Any]] = None,
) -> bool:
    """Upload a custom thumbnail. Returns False if skipped/failed (e.g. scope)."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    path = Path(thumbnail_path)
    if not video_id or not path.is_file():
        return False

    mime = "image/jpeg"
    suffix = path.suffix.lower()
    if suffix == ".png":
        mime = "image/png"
    elif suffix in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    else:
        return False

    youtube = _youtube_service(credentials)
    media = MediaFileUpload(str(path), mimetype=mime, resumable=False)
    try:
        youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
        return True
    except HttpError as exc:
        # Common when refresh token was minted with youtube.upload only.
        print(f"[youtube] thumbnail upload skipped: {exc}", flush=True)
        return False
    except Exception as exc:
        print(f"[youtube] thumbnail upload skipped: {exc}", flush=True)
        return False


def upload_video(
    file_path: str | Path,
    *,
    title: str,
    description: str = "",
    tags: Optional[List[str]] = None,
    privacy: Optional[str] = None,
    category_id: str = "22",
    default_language: Optional[str] = None,
    default_audio_language: Optional[str] = None,
    thumbnail_path: Optional[str | Path] = None,
    credentials: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Upload an mp4 and return {video_id, url, title, privacy_status, ...}."""
    from googleapiclient.http import MediaFileUpload

    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo de vídeo não encontrado: {path}")

    status = (privacy or privacy_status(credentials)).lower()
    if status not in ("private", "unlisted", "public"):
        status = "private"

    tag_list = [t.strip() for t in (tags or ["Shorts"]) if t and str(t).strip()]
    if "Shorts" not in tag_list:
        tag_list.append("Shorts")

    snippet: Dict[str, Any] = {
        "title": _truncate_title(title),
        "description": _truncate_description(description or "#Shorts"),
        "tags": tag_list,
        "categoryId": str(category_id or "22"),
    }
    lang = (default_language or "").strip()
    audio_lang = (default_audio_language or "").strip() or lang
    if lang:
        snippet["defaultLanguage"] = lang
    if audio_lang:
        snippet["defaultAudioLanguage"] = audio_lang

    body = {
        "snippet": snippet,
        "status": {
            "privacyStatus": status,
            "selfDeclaredMadeForKids": False,
        },
    }

    youtube = _youtube_service(credentials)
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _status, response = request.next_chunk()

    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"Upload YouTube sem video id: {response}")

    thumb_ok = False
    if thumbnail_path:
        thumb_ok = set_thumbnail(video_id, thumbnail_path, credentials=credentials)

    return {
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "title": body["snippet"]["title"],
        "description": body["snippet"]["description"],
        "tags": tag_list,
        "category_id": body["snippet"]["categoryId"],
        "default_language": lang or "",
        "privacy_status": status,
        "thumbnail_set": thumb_ok,
    }
