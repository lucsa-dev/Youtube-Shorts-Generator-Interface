"""Local transcription via faster-whisper.

Reads a local media file and returns:
  {duration, segments[{start, end, text, words?}], words?}

Word timestamps power karaoke burn-in. Cache is JSON (preferred) with a
legacy .srt fallback for segment-only reads.
"""
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from ..config import LOCAL_OUTPUT_DIR, LOCAL_WHISPER_DEVICE, LOCAL_WHISPER_MODEL


def _transcript_cache_path(media_path: str) -> Path:
    """Return the .transcript.json cache path for a media file."""
    cache_dir = Path(LOCAL_OUTPUT_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / (Path(media_path).stem + ".transcript.json")


def _legacy_srt_cache_path(media_path: str) -> Path:
    return Path(LOCAL_OUTPUT_DIR) / (Path(media_path).stem + ".srt")


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + (millis / 1000.0)


def _write_srt_sidecar(media_path: str, transcript: Dict) -> Path:
    cache_path = _legacy_srt_cache_path(media_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for idx, segment in enumerate(transcript.get("segments", []), start=1):
        start = _format_srt_timestamp(float(segment["start"]))
        end = _format_srt_timestamp(float(segment["end"]))
        text = str(segment.get("text", "")).strip().replace("\r", "").replace("\n", " ")
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
    cache_path.write_text("\n".join(lines), encoding="utf-8")
    return cache_path


def _load_srt_cache(cache_path: Path) -> Dict:
    content = cache_path.read_text(encoding="utf-8-sig").strip()
    if not content:
        return {"duration": 0.0, "segments": [], "words": []}

    segments = []
    for block in re.split(r"\n\s*\n", content):
        lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[0].split("-->", 1)]
        text = "\n".join(lines[1:]).strip()
        segments.append(
            {
                "start": _parse_srt_timestamp(start_raw),
                "end": _parse_srt_timestamp(end_raw),
                "text": text,
            }
        )

    duration = segments[-1]["end"] if segments else 0.0
    return {"duration": duration, "segments": segments, "words": []}


def _write_json_cache(media_path: str, transcript: Dict) -> Path:
    cache_path = _transcript_cache_path(media_path)
    cache_path.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
    return cache_path


def _load_json_cache(cache_path: Path) -> Dict:
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"duration": 0.0, "segments": [], "words": []}
    data.setdefault("words", [])
    data.setdefault("segments", [])
    data.setdefault("duration", 0.0)
    return data


def _resolve_device() -> str:
    if LOCAL_WHISPER_DEVICE != "auto":
        return LOCAL_WHISPER_DEVICE
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            # Test that CUDA actually works (catches missing cuBLAS/cuDNN libs)
            torch.zeros(1, device="cuda")
            return "cuda"
    except (ImportError, OSError, RuntimeError):
        pass
    return "cpu"


def _segment_words(segment) -> List[Dict]:
    words = []
    for w in getattr(segment, "words", None) or []:
        text = (getattr(w, "word", None) or "").strip()
        if not text:
            continue
        words.append({
            "start": float(w.start),
            "end": float(w.end),
            "word": text,
        })
    return words


def _fmt_mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def transcribe_local(media_path: str, language: Optional[str] = None) -> Dict:
    """Run faster-whisper on a local file path, caching the result as JSON."""
    json_cache = _transcript_cache_path(media_path)
    srt_cache = _legacy_srt_cache_path(media_path)
    source_mtime = os.path.getmtime(media_path)

    if json_cache.exists() and json_cache.stat().st_mtime >= source_mtime:
        print(f"[transcribe/local] reusing cached transcript: {json_cache}", flush=True)
        cached = _load_json_cache(json_cache)
        if not cached["segments"] or float(cached.get("duration") or 0) <= 0.0:
            print(f"[transcribe/local] cache is empty/invalid, deleting: {json_cache}", flush=True)
            json_cache.unlink(missing_ok=True)
        elif not (cached.get("words") or []):
            print(
                f"[transcribe/local] cache has no word timestamps, deleting: {json_cache}",
                flush=True,
            )
            json_cache.unlink(missing_ok=True)
        else:
            n_words = len(cached.get("words") or [])
            print(
                f"[transcribe/local] {len(cached['segments'])} cached segments, "
                f"{n_words} words, {cached['duration']:.0f}s of audio",
                flush=True,
            )
            return cached

    # Legacy SRT has no word timestamps — ignore so karaoke gets real timings.
    if srt_cache.exists() and not json_cache.exists():
        print(
            f"[transcribe/local] legacy SRT found without words — re-transcribing: {srt_cache}",
            flush=True,
        )

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    device = _resolve_device()
    compute_type = "float16" if device == "cuda" else "int8"
    print(
        f"[transcribe/local] loading faster-whisper model={LOCAL_WHISPER_MODEL} "
        f"device={device} compute={compute_type}…",
        flush=True,
    )

    from ..config import LOCAL_WHISPER_VAD_FILTER, LOCAL_WHISPER_VAD_PARAMETERS

    model = WhisperModel(LOCAL_WHISPER_MODEL, device=device, compute_type=compute_type)
    print(f"[transcribe/local] model ready — starting transcription of {media_path}", flush=True)

    transcribe_kwargs = {
        "audio": media_path,
        "language": language,
        "beam_size": 5,
        "condition_on_previous_text": False,
        "word_timestamps": True,
    }
    if LOCAL_WHISPER_VAD_FILTER:
        transcribe_kwargs["vad_filter"] = True
        transcribe_kwargs["vad_parameters"] = LOCAL_WHISPER_VAD_PARAMETERS
    else:
        transcribe_kwargs["vad_filter"] = False

    segments_iter, info = model.transcribe(**transcribe_kwargs)
    total_duration = float(getattr(info, "duration", 0.0) or 0.0)
    detected_lang = getattr(info, "language", None) or language or "auto"
    lang_prob = getattr(info, "language_probability", None)
    lang_extra = f" (p={lang_prob:.0%})" if isinstance(lang_prob, float) else ""
    print(
        f"[transcribe/local] audio={_fmt_mmss(total_duration)} "
        f"({total_duration:.0f}s) language={detected_lang}{lang_extra}",
        flush=True,
    )

    segments = []
    all_words: List[Dict] = []
    last_logged_pct = -1
    for s in segments_iter:
        words = _segment_words(s)
        all_words.extend(words)
        text = (s.text or "").strip()
        seg = {
            "start": float(s.start),
            "end": float(s.end),
            "text": text,
        }
        if words:
            seg["words"] = words
        segments.append(seg)

        end = float(s.end)
        pct = int((end / total_duration) * 100) if total_duration > 0 else 0
        pct = min(pct, 100)
        # Log on first segment and whenever progress advances by ≥1%.
        if last_logged_pct < 0 or pct >= last_logged_pct + 1:
            preview = text.replace("\n", " ")
            if len(preview) > 90:
                preview = preview[:89] + "…"
            print(
                f"[transcribe/local] {pct:3d}% "
                f"({_fmt_mmss(end)}/{_fmt_mmss(total_duration)}) "
                f"seg#{len(segments)} | {preview or '(silêncio)'}",
                flush=True,
            )
            last_logged_pct = pct

    duration = total_duration or (segments[-1]["end"] if segments else 0.0)
    print(
        f"[transcribe/local] done — {len(segments)} segments, {len(all_words)} words, "
        f"{duration:.0f}s of audio",
        flush=True,
    )
    transcript = {"duration": duration, "segments": segments, "words": all_words}
    cache_path = _write_json_cache(media_path, transcript)
    _write_srt_sidecar(media_path, transcript)
    print(f"[transcribe/local] wrote cache: {cache_path}", flush=True)
    return transcript
