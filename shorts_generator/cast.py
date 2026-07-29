"""Speaker / cast detection for podcasts and interviews.

Effort-first path (no face recognition, no pyannote):
  1. Extract speaker candidates from YouTube metadata + transcript intro (LLM).
  2. User (or CLI) assigns display names to SPEAKER_1 / SPEAKER_2 / …
  3. Label Whisper segments with those speaker ids (chunked LLM pass).
  4. Feed labeled transcript + cast roster into highlight ranking for
     name-attributed titles ("Rodrigo Pimentel fala sobre …").
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional

LLMFn = Callable[[str], str]


def _parse_json_loose(raw: str) -> Dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start : end + 1])
        raise


def _default_llm(prompt: str) -> str:
    from .highlights import call_muapi_llm

    return call_muapi_llm(prompt)

INTRO_SECONDS = 240.0  # first ~4 minutes usually contain host/guest intros
LABEL_BATCH_SEGMENTS = 60
MAX_SPEAKERS = 6

CAST_EXTRACT_PROMPT = """You identify who is speaking on a podcast/interview/debate video.

From the VIDEO METADATA and TRANSCRIPT INTRO, list the distinct people who SPEAK on the show
(hosts and guests). Do NOT list people who are only mentioned as topics of conversation
unless they are clearly present as speakers.

Rules:
- Prefer real full names when the intro or metadata gives them
- If only a first name is known, keep it (e.g. "Renan")
- id must be S1, S2, S3… in order of likely importance (guests first if famous, else host first)
- suggested_name may be empty if unknown — still create a slot
- sample_quote: a short line that helps the user recognize that person
- sample_time: approximate timestamp in seconds when that person is visible / speaking that line (number)
- role: host | guest | unknown
- Max {max_speakers} speakers. Skip silent producers / audience.

Respond with JSON only:
{{"speakers":[{{"id":"S1","suggested_name":"string","role":"host|guest|unknown","sample_quote":"string","sample_time":0,"evidence":"string"}}]}}"""


LABEL_SYSTEM_PROMPT = """You assign each transcript line to one of the named speakers.

Speakers (use these exact ids):
{speaker_roster}

Rules:
- Every line index must appear exactly once in the output
- Prefer the speaker who is most likely saying that line
- Use conversational cues (questions → host, long answers → guest, names addressed, style)
- If unsure, pick the most likely speaker — never invent new ids
- Respond with JSON only: {{"labels":[{{"i":0,"speaker_id":"S1"}}]}}"""


def fetch_source_metadata(source: str) -> Dict[str, str]:
    """Best-effort YouTube (or local cache) metadata for cast extraction."""
    meta: Dict[str, str] = {
        "title": "",
        "description": "",
        "channel": "",
        "url": source or "",
    }
    if not source:
        return meta

    # Local cache written by downloader / web layer
    try:
        from .local.downloader import _extract_youtube_video_id
        from .config import LOCAL_OUTPUT_DIR
        import os

        video_id = _extract_youtube_video_id(source)
        if video_id:
            cache = os.path.join(LOCAL_OUTPUT_DIR, f"source_{video_id}.meta.json")
            if os.path.exists(cache):
                with open(cache, encoding="utf-8") as f:
                    data = json.load(f)
                for key in ("title", "description", "channel"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        meta[key] = val.strip()
                if meta["title"] or meta["description"]:
                    return meta
    except Exception:
        pass

    # Live yt-dlp probe (no download)
    try:
        from .local.downloader import _import_ytdlp, _extract_youtube_video_id

        if not _extract_youtube_video_id(source) and not source.startswith("http"):
            return meta
        yt_dlp = _import_ytdlp()
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(source, download=False)
        if not isinstance(info, dict):
            return meta
        meta["title"] = str(info.get("title") or info.get("fulltitle") or "").strip()
        meta["description"] = str(info.get("description") or "").strip()[:4000]
        meta["channel"] = str(
            info.get("channel") or info.get("uploader") or info.get("creator") or ""
        ).strip()
    except Exception as e:
        print(f"[cast] metadata probe skipped: {e}", flush=True)

    return meta


def _intro_text(transcript: Dict, max_seconds: float = INTRO_SECONDS) -> str:
    lines: List[str] = []
    for seg in transcript.get("segments") or []:
        try:
            start = float(seg.get("start", 0))
        except (TypeError, ValueError):
            continue
        if start > max_seconds:
            break
        text = str(seg.get("text") or "").strip()
        if text:
            lines.append(f"[{start:.1f}s] {text}")
    return "\n".join(lines)[:8000]


def _coerce_time(value: object) -> Optional[float]:
    try:
        t = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if t < 0 or t != t:  # NaN
        return None
    return t


def _normalize_quote(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def attach_speaker_sample_times(
    speakers: List[Dict],
    transcript: Dict,
    *,
    intro_seconds: float = INTRO_SECONDS,
) -> List[Dict]:
    """Fill/refine ``sample_time`` so the UI can grab a face frame per speaker."""
    segments = list(transcript.get("segments") or [])
    intro_segs = []
    for seg in segments:
        try:
            start = float(seg.get("start", 0))
        except (TypeError, ValueError):
            continue
        if start > intro_seconds:
            break
        intro_segs.append(seg)

    used_times: List[float] = []
    for i, sp in enumerate(speakers):
        quote = _normalize_quote(str(sp.get("sample_quote") or ""))
        t = _coerce_time(sp.get("sample_time"))

        if quote and len(quote) >= 8:
            for seg in intro_segs or segments:
                text = _normalize_quote(str(seg.get("text") or ""))
                if quote in text or (len(quote) > 20 and text in quote):
                    try:
                        t = float(seg.get("start", 0))
                    except (TypeError, ValueError):
                        pass
                    break

        if t is None:
            # Spread fallbacks across the intro so different people get different frames
            if intro_segs:
                idx = min(i * max(1, len(intro_segs) // max(1, len(speakers))), len(intro_segs) - 1)
                try:
                    t = float(intro_segs[idx].get("start", 5.0 * (i + 1)))
                except (TypeError, ValueError):
                    t = 5.0 * (i + 1)
            else:
                t = 5.0 * (i + 1)

        # Nudge away from an already-used timestamp (±3s) so faces differ
        for used in used_times:
            if abs(t - used) < 3.0:
                t = used + 4.0
        used_times.append(t)
        sp["sample_time"] = round(max(0.0, t), 2)

    return speakers


def _sanitize_speakers(raw: object) -> List[Dict]:
    if not isinstance(raw, list):
        return []
    cleaned: List[Dict] = []
    seen = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or f"S{i + 1}").strip().upper()
        if not re.fullmatch(r"S\d+", sid):
            sid = f"S{i + 1}"
        if sid in seen:
            continue
        seen.add(sid)
        role = str(item.get("role") or "unknown").strip().lower()
        if role not in ("host", "guest", "unknown"):
            role = "unknown"
        cleaned.append(
            {
                "id": sid,
                "suggested_name": str(item.get("suggested_name") or "").strip(),
                "name": "",  # filled after user/CLI confirmation
                "role": role,
                "sample_quote": str(item.get("sample_quote") or "").strip()[:240],
                "sample_time": _coerce_time(item.get("sample_time")),
                "evidence": str(item.get("evidence") or "").strip()[:240],
            }
        )
        if len(cleaned) >= MAX_SPEAKERS:
            break
    # Normalize ids to S1..Sn in list order
    for i, sp in enumerate(cleaned, 1):
        sp["id"] = f"S{i}"
    return cleaned


def extract_speakers(
    transcript: Dict,
    metadata: Optional[Dict[str, str]] = None,
    llm_fn: Optional[LLMFn] = None,
) -> List[Dict]:
    """Return speaker candidates for the naming UI / CLI auto-accept."""
    llm_fn = llm_fn or _default_llm
    metadata = metadata or {}
    intro = _intro_text(transcript)
    meta_block = (
        f"Title: {metadata.get('title') or '(unknown)'}\n"
        f"Channel: {metadata.get('channel') or '(unknown)'}\n"
        f"Description:\n{(metadata.get('description') or '')[:2500] or '(none)'}"
    )
    prompt = (
        CAST_EXTRACT_PROMPT.format(max_speakers=MAX_SPEAKERS)
        + f"\n\nVIDEO METADATA:\n{meta_block}"
        + f"\n\nTRANSCRIPT INTRO:\n{intro or '(empty)'}"
    )
    try:
        raw = llm_fn(prompt)
        parsed = _parse_json_loose(raw)
        speakers = _sanitize_speakers(
            parsed.get("speakers") if isinstance(parsed, dict) else parsed
        )
    except Exception as e:
        print(f"[cast] extract failed: {e}", flush=True)
        speakers = []

    if not speakers:
        # Fallback: at least one anonymous slot so the UI can still name someone
        speakers = [
            {
                "id": "S1",
                "suggested_name": "",
                "name": "",
                "role": "unknown",
                "sample_quote": "",
                "sample_time": 5.0,
                "evidence": "fallback — could not detect speakers automatically",
            }
        ]
    speakers = attach_speaker_sample_times(speakers, transcript)
    print(
        f"[cast] {len(speakers)} speaker candidate(s): "
        + ", ".join(
            (s.get("suggested_name") or s["id"]) + f"/{s.get('role')}" for s in speakers
        ),
        flush=True,
    )
    return speakers


def apply_speaker_names(
    speakers: List[Dict],
    names: Dict[str, str],
    *,
    use_suggested_if_empty: bool = True,
) -> List[Dict]:
    """Merge user-provided names onto speaker slots. Drop slots left blank."""
    named: List[Dict] = []
    for sp in speakers:
        sid = sp["id"]
        raw = (names.get(sid) or "").strip()
        if not raw and use_suggested_if_empty:
            raw = str(sp.get("suggested_name") or "").strip()
        if not raw:
            continue
        item = dict(sp)
        item["name"] = raw
        named.append(item)
    return named


def _speaker_roster_text(speakers: List[Dict]) -> str:
    lines = []
    for sp in speakers:
        name = sp.get("name") or sp.get("suggested_name") or sp["id"]
        lines.append(f"- {sp['id']}: {name} ({sp.get('role') or 'unknown'})")
    return "\n".join(lines)


def label_transcript_speakers(
    transcript: Dict,
    speakers: List[Dict],
    llm_fn: Optional[LLMFn] = None,
) -> Dict:
    """Attach speaker_id / speaker to each segment via chunked LLM labeling."""
    llm_fn = llm_fn or _default_llm
    segments = list(transcript.get("segments") or [])
    if not segments or not speakers:
        return transcript

    roster = _speaker_roster_text(speakers)
    id_set = {sp["id"] for sp in speakers}
    default_id = speakers[0]["id"]
    name_by_id = {
        sp["id"]: (sp.get("name") or sp.get("suggested_name") or sp["id"]) for sp in speakers
    }

    labels: Dict[int, str] = {}
    total = len(segments)
    for start in range(0, total, LABEL_BATCH_SEGMENTS):
        batch = segments[start : start + LABEL_BATCH_SEGMENTS]
        lines = "\n".join(
            f'[{i}] {str(seg.get("text") or "").strip()}'
            for i, seg in enumerate(batch)
        )
        prompt = (
            LABEL_SYSTEM_PROMPT.format(speaker_roster=roster)
            + f"\n\nLines (local indices 0..{len(batch) - 1}):\n{lines}"
        )
        batch_labels: Dict[int, str] = {}
        try:
            raw = llm_fn(prompt)
            parsed = _parse_json_loose(raw)
            items = parsed.get("labels") if isinstance(parsed, dict) else None
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        li = int(item.get("i"))
                    except (TypeError, ValueError):
                        continue
                    sid = str(item.get("speaker_id") or "").strip().upper()
                    if 0 <= li < len(batch) and sid in id_set:
                        batch_labels[li] = sid
        except Exception as e:
            print(f"[cast] label batch @{start} failed: {e}", flush=True)

        for li in range(len(batch)):
            labels[start + li] = batch_labels.get(li, default_id)

        print(
            f"[cast] labeled segments {start + 1}–{min(start + len(batch), total)}/{total}",
            flush=True,
        )

    out_segs: List[Dict] = []
    for i, seg in enumerate(segments):
        item = dict(seg)
        sid = labels.get(i, default_id)
        item["speaker_id"] = sid
        item["speaker"] = name_by_id.get(sid, sid)
        out_segs.append(item)

    out = dict(transcript)
    out["segments"] = out_segs
    out["speakers"] = [
        {"id": sp["id"], "name": name_by_id[sp["id"]], "role": sp.get("role")}
        for sp in speakers
    ]
    return out


def cast_context_block(speakers: List[Dict]) -> str:
    """Prompt block injected into highlight ranking."""
    if not speakers:
        return ""
    lines = ["KNOWN SPEAKERS ON THIS VIDEO (use these names in titles when relevant):"]
    for sp in speakers:
        name = sp.get("name") or sp.get("suggested_name") or sp["id"]
        role = sp.get("role") or "unknown"
        lines.append(f"- {name} ({role}, id={sp['id']})")
    lines.append(
        "Speaker attribution (podcast/interview/debate):\n"
        "- Set attributed_to to the speaker the clip is mainly about / from\n"
        "- Prefer the guest / public figure over the host when both speak\n"
        "- Never invent names outside this list\n"
        "- Do NOT force \"{Name}: topic\" titles — write a viral Shorts title; keep the name in attributed_to"
    )
    return "\n".join(lines)


def build_named_transcript_text(transcript: Dict, offset: float = 0.0) -> str:
    """Like build_transcript_text but prefixes [SpeakerName] when labeled."""
    segments = transcript.get("segments") or []
    lines: List[str] = []
    for s in segments:
        try:
            t = max(0.0, float(s["start"]) - offset)
        except (TypeError, ValueError, KeyError):
            continue
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        speaker = str(s.get("speaker") or "").strip()
        if speaker:
            lines.append(f"[{t:.1f}s][{speaker}] {text}")
        else:
            lines.append(f"[{t:.1f}s] {text}")
    return "\n".join(lines)
