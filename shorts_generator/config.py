import os

from dotenv import load_dotenv

load_dotenv()

MUAPI_API_KEY = os.getenv("MUAPI_API_KEY", "").strip()
MUAPI_BASE_URL = (os.getenv("MUAPI_BASE_URL") or "https://api.muapi.ai/api/v1").strip().rstrip("/")


def _env_float(key: str, default: float) -> float:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


POLL_INTERVAL_SECONDS = _env_float("MUAPI_POLL_INTERVAL", 5.0)
POLL_TIMEOUT_SECONDS = _env_float("MUAPI_POLL_TIMEOUT", 600.0)

# Local-mode (--mode local) settings — only consulted when running offline.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# Image thumbs: mini + medium quality is ~10× cheaper than legacy gpt-image-1 high.
OPENAI_IMAGE_MODEL = (
    os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1-mini").strip().strip("'\"")
    or "gpt-image-1-mini"
)
_OPENAI_IMAGE_QUALITY = (
    os.getenv("OPENAI_IMAGE_QUALITY", "medium").strip().strip("'\"").lower() or "medium"
)
OPENAI_IMAGE_QUALITY = (
    _OPENAI_IMAGE_QUALITY if _OPENAI_IMAGE_QUALITY in ("low", "medium", "high") else "medium"
)
_OPENAI_IMAGE_FIDELITY = (
    os.getenv("OPENAI_IMAGE_FIDELITY", "low").strip().strip("'\"").lower() or "low"
)
# low | high | none (omit param — cheapest when editing with face refs)
OPENAI_IMAGE_FIDELITY = (
    _OPENAI_IMAGE_FIDELITY
    if _OPENAI_IMAGE_FIDELITY in ("low", "high", "none", "off")
    else "low"
)
# hybrid legado — tipografia + moldura sempre em PIL no fluxo atual
THUMBNAIL_HYBRID = os.getenv("THUMBNAIL_HYBRID", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# frame/cutout = frame original do locutor + overlay local (sem fundo IA)
# ai = legado; tratado como frame
_THUMBNAIL_MODE = (
    os.getenv("THUMBNAIL_MODE", "frame").strip().strip("'\"").lower() or "frame"
)
THUMBNAIL_MODE = (
    _THUMBNAIL_MODE if _THUMBNAIL_MODE in ("cutout", "ai", "frame") else "frame"
)
# Shared palette for thumbnail border gradient + per-word hook text fills (comma-separated hex).
DEFAULT_THUMBNAIL_PALETTE = "#FF28B4,#FF7828,#FFDC28,#28DCFF,#A03CFF"
THUMBNAIL_PALETTE = (
    os.getenv("THUMBNAIL_PALETTE", DEFAULT_THUMBNAIL_PALETTE).strip().strip("'\"")
    or DEFAULT_THUMBNAIL_PALETTE
)


def parse_thumbnail_palette(raw: str | None = None) -> list[tuple[int, int, int]]:
    """Parse ``#RRGGBB,#RRGGBB,…`` into RGB tuples; falls back to the default palette."""
    import re

    _FALLBACK = [
        (255, 40, 180),
        (255, 120, 40),
        (255, 220, 40),
        (40, 220, 255),
        (160, 60, 255),
    ]
    text = (raw if raw is not None else THUMBNAIL_PALETTE) or ""
    text = str(text).strip().strip("'\"")
    colors: list[tuple[int, int, int]] = []
    for part in re.split(r"[,;\s]+", text):
        token = part.strip().lstrip("#")
        if len(token) == 3 and all(c in "0123456789abcdefABCDEF" for c in token):
            token = "".join(c * 2 for c in token)
        if len(token) != 6:
            continue
        try:
            colors.append(
                (int(token[0:2], 16), int(token[2:4], 16), int(token[4:6], 16))
            )
        except ValueError:
            continue
    if colors:
        return colors
    if raw is not None and str(raw).strip() and str(raw).strip() != DEFAULT_THUMBNAIL_PALETTE:
        return parse_thumbnail_palette(DEFAULT_THUMBNAIL_PALETTE)
    return list(_FALLBACK)


def format_thumbnail_palette(colors: list[tuple[int, int, int]] | None = None) -> str:
    """Serialize RGB tuples (or current config) as ``#RRGGBB,#RRGGBB,…``."""
    if colors is None:
        colors = parse_thumbnail_palette()
    return ",".join(f"#{r:02X}{g:02X}{b:02X}" for r, g, b in colors)
# openai | gemini | auto (tenta OpenAI; se billing/quota → Gemini)
_IMAGE_PROVIDER = (
    os.getenv("IMAGE_PROVIDER", "auto").strip().strip("'\"").lower() or "auto"
)
IMAGE_PROVIDER = (
    _IMAGE_PROVIDER if _IMAGE_PROVIDER in ("openai", "gemini", "auto") else "auto"
)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_IMAGE_MODEL = (
    os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image").strip().strip("'\"")
    or "gemini-2.5-flash-image"
)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "base")
LOCAL_WHISPER_DEVICE = os.getenv("LOCAL_WHISPER_DEVICE", "auto")  # auto / cpu / cuda
LOCAL_OUTPUT_DIR = os.getenv("LOCAL_OUTPUT_DIR", "output")
# Face-tracking crop: 0 = freeze on first face, 1 = snap instantly to new position.
LOCAL_FACE_SMOOTHING = max(0.0, min(1.0, _env_float("LOCAL_FACE_SMOOTHING", 0.15)))

# Pull clip start earlier so the first word isn't clipped (Whisper lands on phoneme onset).
# Final start prefers end-of-previous-sentence or a short silence inside [min, max].
CLIP_START_LEAD_IN = max(0.0, _env_float("CLIP_START_LEAD_IN", 1.0))
CLIP_START_LEAD_IN_MIN = max(0.0, _env_float("CLIP_START_LEAD_IN_MIN", 0.8))
CLIP_START_LEAD_IN_MAX = max(
    CLIP_START_LEAD_IN_MIN,
    _env_float("CLIP_START_LEAD_IN_MAX", 1.2),
)

# Content language: Whisper recognition + titles/hooks/descriptions from the LLM.
# ISO-639-1 code. Default Brazilian Portuguese.
CONTENT_LANGUAGE = os.getenv("CONTENT_LANGUAGE", "pt").strip().lower() or "pt"

# Prefer free YouTube captions (manual/auto) before Whisper when the source is a
# YouTube URL. Falls back to MuAPI Whisper / faster-whisper if missing or thin.
PREFER_YOUTUBE_CAPTIONS = os.getenv("PREFER_YOUTUBE_CAPTIONS", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

LANGUAGE_OPTIONS = [
    ("pt", "Português (Brasil)"),
    ("en", "English"),
    ("es", "Español"),
    ("fr", "Français"),
    ("it", "Italiano"),
    ("de", "Deutsch"),
    ("auto", "Auto-detect (Whisper)"),
]

LANGUAGE_LABELS = {
    "pt": "Brazilian Portuguese (pt-BR)",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "de": "German",
}


def normalize_language(code: str | None) -> str | None:
    """Normalize UI/config language to Whisper ISO-639-1, or None for auto."""
    if code is None:
        return None
    raw = str(code).strip().lower().replace("_", "-")
    if not raw or raw in ("auto", "detect", "none"):
        return None
    aliases = {
        "pt-br": "pt",
        "pt-pt": "pt",
        "portuguese": "pt",
        "portugues": "pt",
        "português": "pt",
        "english": "en",
        "spanish": "es",
        "español": "es",
        "french": "fr",
        "italian": "it",
        "german": "de",
    }
    if raw in aliases:
        return aliases[raw]
    # pt-BR → pt
    if "-" in raw:
        raw = raw.split("-", 1)[0]
    return raw or None


def language_label(code: str | None) -> str:
    norm = normalize_language(code) or CONTENT_LANGUAGE or "pt"
    return LANGUAGE_LABELS.get(norm, LANGUAGE_LABELS.get("pt", "Brazilian Portuguese (pt-BR)"))


def resolve_content_language(override: str | None = None) -> str:
    """Language for LLM titles/hooks. Never 'auto' — falls back to CONTENT_LANGUAGE/pt."""
    norm = normalize_language(override)
    if norm:
        return norm
    fallback = normalize_language(CONTENT_LANGUAGE) or "pt"
    return fallback

# VAD (Voice Activity Detection) settings for faster-whisper
# Default threshold is 0.5; lower = more sensitive, higher = less sensitive
# Default min_speech_duration_ms is 250ms; increase to avoid tiny false positives
# Default min_silence_duration_ms is 2000ms; increase to avoid splitting mid-sentence
# DISABLED by default because VAD is too aggressive on mixed speech/music content
LOCAL_WHISPER_VAD_FILTER = os.getenv("LOCAL_WHISPER_VAD_FILTER", "false").strip().lower() == "true"
_vad_params_env = os.getenv("LOCAL_WHISPER_VAD_PARAMETERS", "")
if _vad_params_env:
    import json
    LOCAL_WHISPER_VAD_PARAMETERS = json.loads(_vad_params_env)
else:
    # Match faster-whisper defaults when VAD is enabled
    LOCAL_WHISPER_VAD_PARAMETERS = {
        "threshold": 0.5,
        "min_speech_duration_ms": 250,
        "max_speech_duration_s": float("inf"),
        "min_silence_duration_ms": 2000,
        "speech_pad_ms": 400,
    }


def require_api_key() -> str:
    if not MUAPI_API_KEY:
        raise RuntimeError(
            "MUAPI_API_KEY is not set. Add it to your .env file or export it as an env var."
        )
    return MUAPI_API_KEY


def require_openai_key() -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Local mode needs an OpenAI key for highlight ranking. "
            "Add it to your .env or export it, or switch back to --mode api."
        )
    return OPENAI_API_KEY


def require_gemini_key() -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Local mode needs a Gemini key when LLM_PROVIDER=gemini. "
            "Add it to your .env or export it, or switch LLM_PROVIDER back to openai."
        )
    return GEMINI_API_KEY
