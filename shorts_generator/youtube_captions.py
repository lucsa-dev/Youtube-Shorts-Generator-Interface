"""Fetch YouTube captions (manual or auto) and shape them like Whisper output.

Used as a fast/free first pass before MuAPI Whisper or faster-whisper.
Returns None when captions are missing or too thin — caller falls back to ASR.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests

from .config import LOCAL_OUTPUT_DIR, normalize_language

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_MIN_SEGMENTS = 3
_MIN_CHARS = 40


def _is_youtube_url(source: str) -> bool:
    try:
        from .local.downloader import _extract_youtube_video_id

        return bool(_extract_youtube_video_id(source or ""))
    except Exception:
        parsed = urlparse(source or "")
        host = (parsed.netloc or "").lower()
        return "youtube.com" in host or host.endswith("youtu.be")


def _cache_path(video_id: str, lang: str) -> Path:
    safe_lang = re.sub(r"[^\w\-]+", "_", lang or "auto")[:20]
    return Path(LOCAL_OUTPUT_DIR) / f"source_{video_id}.ytcaptions.{safe_lang}.json"


def _lang_candidates(preferred: Optional[str]) -> List[str]:
    """Ordered language codes to try (exact + common variants)."""
    out: List[str] = []
    seen = set()

    def add(code: str) -> None:
        c = (code or "").strip()
        if not c or c in seen:
            return
        seen.add(c)
        out.append(c)

    norm = normalize_language(preferred)
    if norm:
        add(norm)
        if norm == "pt":
            add("pt-BR")
            add("pt-PT")
        elif norm == "en":
            add("en-US")
            add("en-GB")
            add(f"a.{norm}")
    # Broad fallbacks after the preferred family
    for code in ("pt", "pt-BR", "en", "en-US", "es", "a.en"):
        add(code)
    return out


def _pick_track(
    bag: Dict[str, list],
    lang_prefs: Sequence[str],
) -> Optional[Tuple[str, dict]]:
    """Pick (lang, track_dict) preferring json3, then srv3/vtt/srt."""
    ext_rank = {"json3": 0, "srv3": 1, "vtt": 2, "srt": 3, "srv1": 4}

    for lang in lang_prefs:
        tracks = bag.get(lang) or []
        if not tracks:
            # case-insensitive / prefix match (pt → pt-BR already in prefs)
            for key, val in bag.items():
                if key.lower() == lang.lower() or key.lower().startswith(lang.lower() + "-"):
                    tracks = val
                    lang = key
                    break
        if not tracks:
            continue
        ranked = sorted(
            tracks,
            key=lambda t: ext_rank.get(str(t.get("ext") or ""), 99),
        )
        return lang, ranked[0]

    # Last resort: any language, prefer json3
    best: Optional[Tuple[str, dict, int]] = None
    for lang, tracks in bag.items():
        for t in tracks or []:
            score = ext_rank.get(str(t.get("ext") or ""), 99)
            if best is None or score < best[2]:
                best = (lang, t, score)
    if best:
        return best[0], best[1]
    return None


def _fetch_bytes(url: str) -> bytes:
    resp = requests.get(url, timeout=45, headers={"User-Agent": _UA})
    resp.raise_for_status()
    if not resp.content or resp.content[:1] in (b"<", b""):
        # Rate-limit / HTML error page
        raise RuntimeError(f"Caption fetch returned non-caption payload ({resp.status_code})")
    return resp.content


def _clean_caption_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[\r\n]+", " ", text)
    # YouTube auto-captions mark speaker changes with >>
    text = re.sub(r">{2,}\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Drop pure music / sound markers
    if re.fullmatch(r"[\[\(]?[\♪\♫\.\,\!\?\s]*[\]\)]?", text):
        return ""
    return text


def _spread_words(text: str, start: float, end: float) -> List[Dict]:
    tokens = [t for t in re.split(r"\s+", text) if t]
    if not tokens:
        return []
    dur = max(0.05, end - start)
    step = dur / len(tokens)
    words = []
    for i, tok in enumerate(tokens):
        ws = start + i * step
        we = start + (i + 1) * step
        words.append({"start": ws, "end": we, "word": tok})
    return words


def _parse_json3(raw: bytes, duration_hint: float = 0.0) -> Dict:
    data = json.loads(raw.decode("utf-8"))
    segments: List[Dict] = []
    all_words: List[Dict] = []

    for event in data.get("events") or []:
        segs = event.get("segs") or []
        if not segs:
            continue
        # Skip append-only newline / empty cues
        pieces = []
        for s in segs:
            utf = str(s.get("utf8") or "")
            if utf.strip() in ("", "\n"):
                continue
            pieces.append(s)
        if not pieces:
            continue

        start_ms = float(event.get("tStartMs") or 0)
        dur_ms = float(event.get("dDurationMs") or 0)
        start = start_ms / 1000.0
        end = (start_ms + dur_ms) / 1000.0 if dur_ms > 0 else start + 0.5

        text_parts = []
        words: List[Dict] = []
        has_offsets = any("tOffsetMs" in p for p in pieces)

        for p in pieces:
            token = _clean_caption_text(str(p.get("utf8") or ""))
            if not token:
                continue
            text_parts.append(token)
            if has_offsets:
                off = float(p.get("tOffsetMs") or 0) / 1000.0
                ws = start + off
                # End unknown per-token — fill after loop
                words.append({"start": ws, "end": ws + 0.12, "word": token})

        text = _clean_caption_text(" ".join(text_parts))
        if not text:
            continue

        if words:
            # Close each word at the next word start (or segment end)
            for i, w in enumerate(words):
                if i + 1 < len(words):
                    w["end"] = max(w["start"] + 0.05, words[i + 1]["start"])
                else:
                    w["end"] = max(w["start"] + 0.05, end)
            all_words.extend(words)
            seg = {"start": start, "end": max(end, words[-1]["end"]), "text": text, "words": words}
        else:
            approx = _spread_words(text, start, end)
            all_words.extend(approx)
            seg = {"start": start, "end": end, "text": text}
            if approx:
                seg["words"] = approx
        segments.append(seg)

    duration = duration_hint
    if segments:
        duration = max(duration, float(segments[-1]["end"]))
    return {"duration": duration, "segments": segments, "words": all_words}


def _parse_vtt_timestamp(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(parts[0])


def _parse_vtt_or_srt(raw: bytes, duration_hint: float = 0.0) -> Dict:
    text = raw.decode("utf-8", errors="replace")
    # Strip WEBVTT header / NOTE blocks lightly
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments: List[Dict] = []
    all_words: List[Dict] = []
    i = 0
    ts_re = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})"
        r"\s*-->\s*"
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})"
    )

    while i < len(lines):
        line = lines[i].strip()
        i += 1
        m = ts_re.search(line)
        if not m:
            continue
        start = _parse_vtt_timestamp(m.group(1))
        end = _parse_vtt_timestamp(m.group(2))
        buf: List[str] = []
        while i < len(lines) and lines[i].strip():
            # Drop VTT cue settings / tags
            cue = re.sub(r"<[^>]+>", "", lines[i]).strip()
            if cue:
                buf.append(cue)
            i += 1
        body = _clean_caption_text(" ".join(buf))
        if not body:
            continue
        words = _spread_words(body, start, end)
        all_words.extend(words)
        seg: Dict = {"start": start, "end": max(end, start + 0.05), "text": body}
        if words:
            seg["words"] = words
        segments.append(seg)

    duration = duration_hint
    if segments:
        duration = max(duration, float(segments[-1]["end"]))
    return {"duration": duration, "segments": segments, "words": all_words}


def _parse_caption_payload(raw: bytes, ext: str, duration_hint: float) -> Dict:
    ext = (ext or "").lower()
    if ext == "json3":
        return _parse_json3(raw, duration_hint=duration_hint)
    return _parse_vtt_or_srt(raw, duration_hint=duration_hint)


def _is_usable(transcript: Dict) -> bool:
    segments = transcript.get("segments") or []
    if len(segments) < _MIN_SEGMENTS:
        return False
    chars = sum(len(str(s.get("text") or "")) for s in segments)
    return chars >= _MIN_CHARS


def try_youtube_captions(
    source: str,
    language: Optional[str] = None,
) -> Optional[Dict]:
    """Return Whisper-shaped transcript from YouTube captions, or None.

    Prefers manual subs in the requested language, then auto-captions, then
    other languages. Caches under LOCAL_OUTPUT_DIR.
    """
    if not source or not _is_youtube_url(source):
        return None

    try:
        from .local.downloader import _extract_youtube_video_id, _import_ytdlp
    except Exception as e:
        print(f"[captions/youtube] yt-dlp unavailable: {e}", flush=True)
        return None

    video_id = _extract_youtube_video_id(source)
    lang_prefs = _lang_candidates(language)
    cache_key = normalize_language(language) or "auto"

    if video_id:
        cached = _cache_path(video_id, cache_key)
        if cached.exists():
            try:
                data = json.loads(cached.read_text(encoding="utf-8"))
                if _is_usable(data):
                    print(
                        f"[captions/youtube] reusing cache: {cached.name} "
                        f"({len(data.get('segments') or [])} segments)",
                        flush=True,
                    )
                    return data
            except (OSError, json.JSONDecodeError, TypeError):
                pass

    try:
        yt_dlp = _import_ytdlp()
    except RuntimeError as e:
        print(f"[captions/youtube] {e}", flush=True)
        return None

    print(f"[captions/youtube] probing subtitles for {source}", flush=True)
    try:
        with yt_dlp.YoutubeDL(
            {"quiet": True, "no_warnings": True, "skip_download": True}
        ) as ydl:
            info = ydl.extract_info(source, download=False)
    except Exception as e:
        print(f"[captions/youtube] probe failed: {e}", flush=True)
        return None

    if not isinstance(info, dict):
        return None

    duration_hint = float(info.get("duration") or 0.0)
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}

    # Prefer creator-uploaded captions for text quality; fall back to ASR.
    picked = _pick_track(manual, lang_prefs)
    kind = "manual"
    if not picked:
        picked = _pick_track(automatic, lang_prefs)
        kind = "auto"
    if not picked:
        print("[captions/youtube] no captions available", flush=True)
        return None

    lang, track = picked
    ext = str(track.get("ext") or "json3")
    url = track.get("url")
    if not url:
        print(f"[captions/youtube] track {kind}/{lang} has no url", flush=True)
        return None

    try:
        raw = _fetch_bytes(url)
        transcript = _parse_caption_payload(raw, ext, duration_hint)
    except Exception as e:
        print(f"[captions/youtube] download/parse failed ({kind}/{lang}.{ext}): {e}", flush=True)
        return None

    if not _is_usable(transcript):
        print(
            f"[captions/youtube] too thin ({len(transcript.get('segments') or [])} segs) — skip",
            flush=True,
        )
        return None

    transcript["source"] = "youtube_captions"
    transcript["caption_kind"] = kind
    transcript["caption_lang"] = lang
    if video_id:
        transcript["video_id"] = video_id

    n_words = len(transcript.get("words") or [])
    print(
        f"[captions/youtube] {kind}/{lang}.{ext} → {len(transcript['segments'])} segments, "
        f"{n_words} words, {transcript['duration']:.0f}s",
        flush=True,
    )

    if video_id:
        try:
            os.makedirs(LOCAL_OUTPUT_DIR, exist_ok=True)
            path = _cache_path(video_id, cache_key)
            path.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            print(f"[captions/youtube] cache write skipped: {e}", flush=True)

    return transcript
