"""End-to-end orchestrator.

Two modes:
  * mode="api"   (default) — MuAPI does download / transcribe / LLM / autocrop.
                              Fast, no local deps, pay-per-call.
  * mode="local"            — yt-dlp + faster-whisper + OpenAI or Gemini + ffmpeg/opencv.
                              Self-hosted, LLM_PROVIDER selects OpenAI or Gemini.

The web UI runs analysis and clipping as separate steps so the user can pick
which highlights to render. CLI still runs the full pipeline in one shot.
"""
from typing import Callable, Dict, List, Optional, Sequence

from .clipper import crop_highlights
from .config import normalize_language, resolve_content_language
from .downloader import download_youtube
from .highlights import call_muapi_llm, get_highlights, snippet_for_range
from .transcriber import transcribe


def _enrich_highlights(highlights: List[Dict], transcript: Dict) -> List[Dict]:
    """Attach transcript snippets and stable ids for the selection UI."""
    enriched: List[Dict] = []
    # Present chronologically for the picker; score stays on each item.
    ordered = sorted(highlights, key=lambda h: float(h.get("start_time", 0)))
    for i, h in enumerate(ordered):
        item = dict(h)
        item["id"] = i
        item["snippet"] = snippet_for_range(
            transcript,
            float(h.get("start_time", 0)),
            float(h.get("end_time", 0)),
        )
        enriched.append(item)
    return enriched


def analyze_video(
    youtube_url: str,
    download_format: str = "720",
    language: Optional[str] = None,
    mode: str = "api",
) -> Dict:
    """Download + transcribe + rank highlights — stop before cropping.

    The number of highlights is chosen by the model from the content
    (no user-supplied num_clips).
    """
    from .config import CONTENT_LANGUAGE

    mode = (mode or "api").lower()
    if not (language or "").strip():
        language = CONTENT_LANGUAGE or "pt"
    if mode == "local":
        return _analyze_local(youtube_url, download_format, language)
    if mode == "api":
        return _analyze_api(youtube_url, download_format, language)
    raise ValueError(f"Unknown mode: {mode!r}. Use 'api' or 'local'.")


def _analyze_local(youtube_url: str, download_format: str, language: Optional[str]) -> Dict:
    from .local.downloader import download_youtube_local
    from .local.llm import call_local_llm
    from .local.transcriber import transcribe_local

    whisper_lang = normalize_language(language)
    output_lang = resolve_content_language(language)

    source_path = download_youtube_local(youtube_url, fmt=download_format)

    transcript = transcribe_local(source_path, language=whisper_lang)
    if not transcript["segments"]:
        raise RuntimeError(
            "Whisper produced no segments. The video may have no detectable speech."
        )

    highlights_result = get_highlights(
        transcript,
        num_clips=None,
        llm_fn=call_local_llm,
        output_language=output_lang,
    )
    all_highlights: List[Dict] = highlights_result.get("highlights", [])
    if not all_highlights:
        raise RuntimeError("Highlight generator returned zero clips.")

    enriched = _enrich_highlights(all_highlights, transcript)
    print(f"[pipeline/local] analysis done — {len(enriched)} topics", flush=True)

    return {
        "mode": "local",
        "phase": "awaiting_selection",
        "source_video_url": source_path,
        "transcript": transcript,
        "highlights": enriched,
        "shorts": [],
    }


def _analyze_api(youtube_url: str, download_format: str, language: Optional[str]) -> Dict:
    whisper_lang = normalize_language(language)
    output_lang = resolve_content_language(language)

    source_url = download_youtube(youtube_url, fmt=download_format)

    transcript = transcribe(source_url, language=whisper_lang)
    if not transcript["segments"]:
        raise RuntimeError(
            "Whisper produced no segments. The video may have no detectable speech."
        )

    highlights_result = get_highlights(
        transcript,
        num_clips=None,
        llm_fn=call_muapi_llm,
        output_language=output_lang,
    )
    all_highlights: List[Dict] = highlights_result.get("highlights", [])
    if not all_highlights:
        raise RuntimeError("Highlight generator returned zero clips.")

    enriched = _enrich_highlights(all_highlights, transcript)
    print(f"[pipeline] analysis done — {len(enriched)} topics", flush=True)

    return {
        "mode": "api",
        "phase": "awaiting_selection",
        "source_video_url": source_url,
        "transcript": transcript,
        "highlights": enriched,
        "shorts": [],
    }


def render_selected_shorts(
    analysis: Dict,
    selected_ids: Sequence[int],
    aspect_ratio: str = "9:16",
    on_short_done: Optional[Callable[[Dict, int, int], None]] = None,
) -> Dict:
    """Crop only the highlights the user selected from an analysis result."""
    highlights = analysis.get("highlights") or []
    by_id = {int(h.get("id", i)): h for i, h in enumerate(highlights)}
    selected: List[Dict] = []
    for sid in selected_ids:
        h = by_id.get(int(sid))
        if h is None:
            raise ValueError(f"Highlight id inválido: {sid}")
        selected.append(h)

    if not selected:
        raise ValueError("Selecione ao menos um tópico para continuar.")

    # Keep chronological order for rendering
    selected = sorted(selected, key=lambda h: float(h.get("start_time", 0)))
    source = analysis["source_video_url"]
    mode = (analysis.get("mode") or "api").lower()

    print(f"[pipeline] cropping {len(selected)} selected highlights", flush=True)

    if mode == "local":
        from .local.clipper import crop_highlights_local

        shorts = crop_highlights_local(
            source, selected, aspect_ratio=aspect_ratio, on_short_done=on_short_done
        )
    else:
        shorts = crop_highlights(
            source, selected, aspect_ratio=aspect_ratio, on_short_done=on_short_done
        )

    return {
        "mode": mode,
        "phase": "completed",
        "source_video_url": source,
        "transcript": analysis.get("transcript"),
        "highlights": highlights,
        "selected_ids": [int(h.get("id", 0)) for h in selected],
        "shorts": shorts,
    }


def _run_local(
    youtube_url: str,
    num_clips: int,
    aspect_ratio: str,
    download_format: str,
    language: Optional[str],
) -> Dict:
    analysis = _analyze_local(youtube_url, download_format, language)
    all_highlights = analysis["highlights"]
    # CLI: keep top-N by score (ids were assigned chronologically)
    top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)), reverse=True)[:num_clips]
    selected_ids = [int(h["id"]) for h in top]
    return render_selected_shorts(analysis, selected_ids, aspect_ratio=aspect_ratio)


def _run_api(
    youtube_url: str,
    num_clips: int,
    aspect_ratio: str,
    download_format: str,
    language: Optional[str],
) -> Dict:
    analysis = _analyze_api(youtube_url, download_format, language)
    all_highlights = analysis["highlights"]
    top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)), reverse=True)[:num_clips]
    selected_ids = [int(h["id"]) for h in top]
    return render_selected_shorts(analysis, selected_ids, aspect_ratio=aspect_ratio)


def generate_shorts(
    youtube_url: str,
    num_clips: int = 3,
    aspect_ratio: str = "9:16",
    download_format: str = "720",
    language: Optional[str] = None,
    mode: str = "api",
) -> Dict:
    """Run the full pipeline and return a structured result.

    Args:
        youtube_url: source URL.
        num_clips: how many shorts to render (CLI top-N after AI ranking).
        aspect_ratio: e.g. "9:16", "1:1".
        download_format: source resolution ("360" / "480" / "720" / "1080").
        language: content language for Whisper + LLM titles/hooks
            (ISO-639-1, e.g. "pt"). Defaults to CONTENT_LANGUAGE (pt).
            Pass "auto" to let Whisper detect; LLM still uses CONTENT_LANGUAGE.
        mode: "api" (default, MuAPI) or "local" (yt-dlp + faster-whisper +
            OpenAI or Gemini + ffmpeg).

    Returns:
        {
          "mode": "api" | "local",
          "source_video_url": str,   # hosted URL (api) or local path (local)
          "transcript": {...},
          "highlights": [...],       # all candidates ranked
          "shorts": [...],           # top `num_clips` with clip_url / local path
        }
    """
    from .config import CONTENT_LANGUAGE

    mode = (mode or "api").lower()
    if not (language or "").strip():
        language = CONTENT_LANGUAGE or "pt"
    if mode == "local":
        return _run_local(youtube_url, num_clips, aspect_ratio, download_format, language)
    if mode == "api":
        return _run_api(youtube_url, num_clips, aspect_ratio, download_format, language)
    raise ValueError(f"Unknown mode: {mode!r}. Use 'api' or 'local'.")
