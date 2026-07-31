"""End-to-end orchestrator.

Two modes:
  * mode="api"   (default) — MuAPI does download / transcribe / LLM / autocrop.
                              Fast, no local deps, pay-per-call.
  * mode="local"            — yt-dlp + faster-whisper + OpenAI or Gemini + ffmpeg/opencv.
                              Self-hosted, LLM_PROVIDER selects OpenAI or Gemini.

Transcription prefers free YouTube captions (manual/auto) when
PREFER_YOUTUBE_CAPTIONS is on, then falls back to Whisper (MuAPI or local).

The web UI runs analysis in stages so the user can name speakers, then pick
which highlights to render. CLI still runs the full pipeline in one shot
(auto-accepting suggested speaker names).
"""
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .cast import (
    apply_speaker_names,
    extract_speakers,
    fetch_source_metadata,
    label_transcript_speakers,
)
from .clipper import crop_highlights
from .config import (
    LOCAL_OUTPUT_DIR,
    PREFER_YOUTUBE_CAPTIONS,
    normalize_language,
    resolve_content_language,
)
from .downloader import download_youtube
from .highlights import call_muapi_llm, get_highlights, snippet_for_range
from .transcriber import transcribe
from .youtube_captions import try_youtube_captions


def _source_folder_slug(analysis: Dict) -> str:
    """Folder name for rendered shorts — YouTube id from the step-1 source."""
    from .local.downloader import _extract_youtube_video_id

    source_url = str(analysis.get("source_url") or "")
    source_video = str(analysis.get("source_video_url") or "")
    meta = analysis.get("metadata") or {}

    video_id = (
        _extract_youtube_video_id(source_url)
        or _extract_youtube_video_id(source_video)
        or (str(meta.get("id") or "").strip() or None)
    )

    if not video_id and source_video:
        stem = Path(source_video).stem
        if stem.startswith("source_"):
            video_id = stem[len("source_") :]
        elif stem:
            video_id = stem

    slug = re.sub(r"[^\w\-]+", "_", video_id or "unknown").strip("_") or "unknown"
    return slug[:80]


def _shorts_output_dir(analysis: Dict) -> str:
    """LOCAL_OUTPUT_DIR/<youtube_id>/ for per-source rendered clips."""
    return os.path.join(LOCAL_OUTPUT_DIR, _source_folder_slug(analysis))


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


def _llm_for_mode(mode: str):
    if mode == "local":
        from .local.llm import call_local_llm

        return call_local_llm
    return call_muapi_llm


def prepare_video(
    youtube_url: str,
    download_format: str = "720",
    language: Optional[str] = None,
    mode: str = "api",
) -> Dict:
    """Download + transcribe + extract speaker candidates — stop before highlights.

    Web UI uses this so the user can name SPEAKER_1 / SPEAKER_2 before ranking.
    """
    from .config import CONTENT_LANGUAGE

    mode = (mode or "api").lower()
    if not (language or "").strip():
        language = CONTENT_LANGUAGE or "pt"
    if mode not in ("api", "local"):
        raise ValueError(f"Unknown mode: {mode!r}. Use 'api' or 'local'.")

    whisper_lang = normalize_language(language)
    llm_fn = _llm_for_mode(mode)

    transcript = None
    if PREFER_YOUTUBE_CAPTIONS:
        transcript = try_youtube_captions(youtube_url, language=whisper_lang)

    if mode == "local":
        from .local.downloader import download_youtube_local
        from .local.transcriber import transcribe_local

        source = download_youtube_local(youtube_url, fmt=download_format)
        if transcript is None:
            transcript = transcribe_local(source, language=whisper_lang)
    else:
        source = download_youtube(youtube_url, fmt=download_format)
        if transcript is None:
            transcript = transcribe(source, language=whisper_lang)

    if not transcript or not transcript.get("segments"):
        raise RuntimeError(
            "No transcript segments (YouTube captions + Whisper both empty). "
            "The video may have no captions and no detectable speech."
        )

    metadata = fetch_source_metadata(youtube_url)
    if not metadata.get("title") and mode == "local":
        # Fall back to probing the downloaded file's sibling meta / URL again
        metadata = fetch_source_metadata(source)

    speakers = extract_speakers(transcript, metadata=metadata, llm_fn=llm_fn)
    print(
        f"[pipeline] prepared — awaiting cast ({len(speakers)} speaker slot(s))",
        flush=True,
    )

    return {
        "mode": mode,
        "phase": "awaiting_cast",
        "source_video_url": source,
        "source_url": youtube_url,
        "metadata": metadata,
        "transcript": transcript,
        "speakers": speakers,
        "highlights": [],
        "shorts": [],
    }


def finalize_analysis(
    prepared: Dict,
    speaker_names: Optional[Dict[str, str]] = None,
    *,
    skip_cast: bool = False,
    num_clips: Optional[int] = None,
    language: Optional[str] = None,
    virality_profile: Optional[Dict] = None,
    clip_length: Optional[str] = None,
) -> Dict:
    """Label transcript with confirmed names (optional) and rank highlights."""
    mode = (prepared.get("mode") or "api").lower()
    llm_fn = _llm_for_mode(mode)
    output_lang = resolve_content_language(language)
    transcript = dict(prepared.get("transcript") or {})
    candidates = list(prepared.get("speakers") or [])

    named: List[Dict] = []
    if not skip_cast and candidates:
        named = apply_speaker_names(
            candidates,
            speaker_names or {},
            use_suggested_if_empty=True,
        )
        if named:
            if len(named) == 1:
                only = named[0]
                name = only.get("name") or only["id"]
                print(f"[pipeline] single speaker — tagging all segments as {name}", flush=True)
                segs = []
                for seg in transcript.get("segments") or []:
                    item = dict(seg)
                    item["speaker_id"] = only["id"]
                    item["speaker"] = name
                    segs.append(item)
                transcript = dict(transcript)
                transcript["segments"] = segs
                transcript["speakers"] = [
                    {"id": only["id"], "name": name, "role": only.get("role")}
                ]
            else:
                print(f"[pipeline] labeling transcript for {len(named)} speaker(s)…", flush=True)
                transcript = label_transcript_speakers(transcript, named, llm_fn=llm_fn)
        else:
            print("[pipeline] no speaker names provided — ranking without cast", flush=True)
    else:
        print("[pipeline] cast skipped — ranking without speaker labels", flush=True)

    highlights_result = get_highlights(
        transcript,
        num_clips=num_clips,
        llm_fn=llm_fn,
        output_language=output_lang,
        speakers=named,
        virality_profile=virality_profile,
        clip_length=clip_length or prepared.get("clip_length"),
    )
    all_highlights: List[Dict] = highlights_result.get("highlights", [])
    if not all_highlights:
        raise RuntimeError("Highlight generator returned zero clips.")

    enriched = _enrich_highlights(all_highlights, transcript)
    clip_len = highlights_result.get("clip_length") or "short"
    print(
        f"[pipeline] analysis done — {len(enriched)} topics (clip_length={clip_len})",
        flush=True,
    )

    return {
        "mode": mode,
        "phase": "awaiting_selection",
        "source_video_url": prepared.get("source_video_url"),
        "source_url": prepared.get("source_url"),
        "metadata": prepared.get("metadata") or {},
        "transcript": transcript,
        "speakers": named or candidates,
        "content_type": highlights_result.get("content_type") or "other",
        "density": highlights_result.get("density") or "medium",
        "clip_length": clip_len,
        "highlights": enriched,
        "shorts": [],
    }


def analyze_video(
    youtube_url: str,
    download_format: str = "720",
    language: Optional[str] = None,
    mode: str = "api",
    *,
    speaker_names: Optional[Dict[str, str]] = None,
    skip_cast: bool = False,
    interactive_cast: bool = False,
) -> Dict:
    """Download + transcribe + (optional cast) + rank highlights — stop before cropping.

    By default (CLI), suggested speaker names are auto-accepted and highlights
    run immediately. Pass ``interactive_cast=True`` to stop at awaiting_cast
    for the web naming UI.
    """
    prepared = prepare_video(
        youtube_url,
        download_format=download_format,
        language=language,
        mode=mode,
    )
    if interactive_cast:
        return prepared
    return finalize_analysis(
        prepared,
        speaker_names=speaker_names,
        skip_cast=skip_cast,
        num_clips=None,
        language=language,
    )


def render_selected_shorts(
    analysis: Dict,
    selected_ids: Sequence[int],
    aspect_ratio: str = "9:16",
    on_short_done: Optional[Callable[[Dict, int, int], None]] = None,
    caption_style: Optional[Dict] = None,
) -> Dict:
    """Crop only the highlights the user selected from an analysis result."""
    from .captions import resolve_style

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
    transcript = analysis.get("transcript")
    style = resolve_style(caption_style)

    out_dir = _shorts_output_dir(analysis)
    print(
        f"[pipeline] cropping {len(selected)} selected highlights → {out_dir}/",
        flush=True,
    )

    if mode == "local":
        from .local.clipper import crop_highlights_local

        shorts = crop_highlights_local(
            source,
            selected,
            aspect_ratio=aspect_ratio,
            out_dir=out_dir,
            on_short_done=on_short_done,
            transcript=transcript,
            caption_style=style,
        )
    else:
        shorts = crop_highlights(
            source, selected, aspect_ratio=aspect_ratio, on_short_done=on_short_done
        )
        if style.get("enabled"):
            print(
                "[pipeline] karaoke burn-in is only supported in --mode local "
                "(API clips are remote URLs)",
                flush=True,
            )

    return {
        "mode": mode,
        "phase": "completed",
        "source_video_url": source,
        "source_url": analysis.get("source_url"),
        "metadata": analysis.get("metadata") or {},
        "transcript": transcript,
        "speakers": analysis.get("speakers") or [],
        "highlights": highlights,
        "selected_ids": [int(h.get("id", 0)) for h in selected],
        "caption_style": style,
        "shorts": shorts,
    }


def _run_full(
    youtube_url: str,
    num_clips: int,
    aspect_ratio: str,
    download_format: str,
    language: Optional[str],
    mode: str,
    clip_length: Optional[str] = None,
) -> Dict:
    prepared = prepare_video(
        youtube_url,
        download_format=download_format,
        language=language,
        mode=mode,
    )
    analysis = finalize_analysis(
        prepared,
        speaker_names=None,  # auto-accept suggested names
        skip_cast=False,
        num_clips=None,
        language=language,
        clip_length=clip_length,
    )
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
    clip_length: Optional[str] = None,
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
        clip_length: "short" (45–90s Shorts) or "long" (3–10 min mid-form).

    Returns:
        {
          "mode": "api" | "local",
          "source_video_url": str,   # hosted URL (api) or local path (local)
          "transcript": {...},
          "speakers": [...],
          "highlights": [...],       # all candidates ranked
          "shorts": [...],           # top `num_clips` with clip_url / local path
        }
    """
    from .config import CONTENT_LANGUAGE

    mode = (mode or "api").lower()
    if not (language or "").strip():
        language = CONTENT_LANGUAGE or "pt"
    if mode not in ("api", "local"):
        raise ValueError(f"Unknown mode: {mode!r}. Use 'api' or 'local'.")
    return _run_full(
        youtube_url,
        num_clips,
        aspect_ratio,
        download_format,
        language,
        mode,
        clip_length=clip_length,
    )
