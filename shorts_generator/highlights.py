"""Find the most viral-worthy highlights in a transcript.

Logic ported from ViralVadoo's transcript_analysis/highlight_generator.py:
  - content-type / density detection
  - chunking for long videos with overlap
  - virality-criteria prompt
  - score-based dedupe with overlap suppression

The LLM call is pluggable via the `llm_fn` argument so the same prompts can
drive either MuAPI (default, --mode api) or a direct local LLM client
(--mode local).
"""
import json
import re
from typing import Callable, Dict, List, Optional

from . import muapi
from .config import (
    CLIP_START_LEAD_IN,
    CLIP_START_LEAD_IN_MAX,
    CLIP_START_LEAD_IN_MIN,
)


LLMFn = Callable[[str], str]


CONTENT_TYPE_PROMPT = """Analyze this video transcript sample and classify the content type.
Choose one: podcast, interview, tutorial, lecture, commentary, debate, vlog, other.
Also estimate content density: low (mostly filler/chit-chat), medium, or high (dense info/stories).
Respond with JSON only: {"content_type": "...", "density": "..."}"""


VIRALITY_CRITERIA = """
Virality signals to prioritize (ranked by impact):
1. HOOK MOMENTS — statements that create immediate curiosity ("The secret is...", "Nobody talks about...", "I was completely wrong about...")
2. EMOTIONAL PEAKS — genuine surprise, laughter, anger, vulnerability, excitement; raw unscripted reactions
3. OPINION BOMBS — strong, polarizing or counter-intuitive statements that trigger agree/disagree
4. REVELATION MOMENTS — surprising facts, stats, or confessions that reframe how the viewer thinks
5. CONFLICT/TENSION — disagreement, pushback, or a problem being confronted head-on
6. QUOTABLE ONE-LINERS — a sentence that works as a standalone quote card
7. STORY PEAKS — the climax or twist of an anecdote; the payoff moment
8. PRACTICAL VALUE — a concrete tip, hack, or insight the viewer can immediately apply
"""


HIGHLIGHT_SYSTEM_PROMPT = """You are an elite short-form video editor who has studied thousands of viral clips on TikTok, Instagram Reels, and YouTube Shorts. You know exactly what makes viewers stop scrolling, watch to the end, and share.

{virality_criteria}

Content type: {content_type} | Density: {density}

Your task: identify the most viral-worthy highlights from the transcript.

Rules:
- Every highlight must open with a strong HOOK — a line that grabs attention within the first 3 seconds
- HARD duration rule: every clip MUST be between 45 and 90 seconds. Prefer ~60s. Never return a hook-only snippet.
- Go longer (91-180s) ONLY when a story arc needs full context to land. Never go under 45 seconds.
- Each clip must be a COMPLETE mini-argument: setup/context → claim or peak → payoff or consequence. A viewer who never saw the full video must still understand the point.
- Never cut mid-sentence or mid-thought — end on a completed idea
- Clips must not overlap significantly with each other
- Score 0-100 on viral potential (not general quality)
- {num_clips_instruction}
- For each highlight, identify the single best "hook_sentence" — the opening line that would make someone stop scrolling
- Explain in one sentence why this clip is viral ("virality_reason")
- start_time / end_time must span the FULL self-contained segment, not just the hook line
- CRITICAL language rule: write title, hook_sentence, and virality_reason entirely in {output_language}. Do NOT use English unless the output language is English. Prefer hooks taken from / closely paraphrasing the transcript.
- When KNOWN SPEAKERS are provided (podcast/interview/debate), titles MUST attribute the main speaker or public figure when known — e.g. "{{Name}} fala sobre {{topic}}" or "{{Name}}: {{claim}}". Prefer guests / public figures over hosts. Never invent names outside the provided list. Fill attributed_to with that person's name (or empty string if none).

{cast_block}

Respond ONLY with valid JSON (no markdown, no explanation):
{{"highlights":[{{"title":"string","start_time":float,"end_time":float,"score":int,"hook_sentence":"string","virality_reason":"string","attributed_to":"string"}}]}}"""


CHUNK_SIZE_SECONDS = 1200       # 20-min chunks for long videos
LONG_VIDEO_THRESHOLD = 1800     # chunk videos longer than 30 min
CHUNK_OVERLAP_SECONDS = 60
GPT_CALL_TIMEOUT_SECONDS = 300  # cap LLM polls at 5 min — a wedged call should fail fast
MAX_HIGHLIGHT_API_ATTEMPTS = 3

# Enforce complete-context clips even when the LLM returns a hook-only window.
MIN_CLIP_SECONDS = 45.0
TARGET_CLIP_SECONDS = 65.0
MAX_CLIP_SECONDS = 180.0


def call_muapi_llm(prompt: str) -> str:
    """Default LLM backend: MuAPI gpt-5-mini."""
    result = muapi.run(
        "gpt-5-mini",
        {"prompt": prompt},
        label="gpt-5-mini",
        timeout=GPT_CALL_TIMEOUT_SECONDS,
    )

    outputs = result.get("outputs")
    if isinstance(outputs, list) and outputs and isinstance(outputs[0], str) and outputs[0].strip():
        return outputs[0]

    for key in ("output", "text", "response", "result", "content"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            inner = v.get("text") or v.get("content")
            if isinstance(inner, str) and inner.strip():
                return inner
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0]

    raise RuntimeError(f"Could not extract gpt-5-mini text from response: {result}")


def _parse_json_loose(raw: str) -> Dict:
    """gpt-5-4 sometimes wraps JSON in markdown fences — strip and parse."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
        raise


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _highlight_times(item: Dict) -> tuple:
    """Accept common aliases the models sometimes emit."""
    start = item.get("start_time", item.get("start", item.get("from")))
    end = item.get("end_time", item.get("end", item.get("to")))
    return start, end


def _sanitize_highlights(raw_highlights: object, duration: float) -> List[Dict]:
    """Normalize model output into the expected shape; skip invalid entries."""
    if not isinstance(raw_highlights, list):
        return []

    max_end = duration if duration > 0 else float("inf")
    cleaned: List[Dict] = []
    for item in raw_highlights:
        if not isinstance(item, dict):
            continue

        start = _coerce_float(_highlight_times(item)[0], default=-1.0)
        end = _coerce_float(_highlight_times(item)[1], default=-1.0)
        if start < 0 or end <= start:
            continue

        if max_end != float("inf"):
            # Drop clearly out-of-window times (e.g. absolute stamps on a relative chunk).
            if start > max_end:
                continue
            end = min(end, max_end)
            if end <= start:
                continue

        cleaned.append(
            {
                "title": str(item.get("title") or "Untitled Highlight").strip(),
                "start_time": start,
                "end_time": end,
                "score": max(0, min(100, _coerce_int(item.get("score"), default=0))),
                "hook_sentence": str(
                    item.get("hook_sentence") or item.get("hook") or ""
                ).strip(),
                "virality_reason": str(
                    item.get("virality_reason") or item.get("reason") or ""
                ).strip(),
                "attributed_to": str(
                    item.get("attributed_to") or item.get("speaker") or ""
                ).strip(),
            }
        )

    return cleaned


_SENTENCE_END_RE = re.compile(r'[.!?…]"?$')


def _segment_ends_thought(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_SENTENCE_END_RE.search(t))


def _flatten_words(segments: List[Dict], transcript_words: Optional[List[Dict]] = None) -> List[Dict]:
    """Word list for boundary snapping — prefer per-segment words, else flat transcript.words."""
    words: List[Dict] = []
    for seg in segments:
        for w in seg.get("words") or []:
            try:
                ws, we = float(w["start"]), float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if we <= ws:
                continue
            words.append(
                {
                    "start": ws,
                    "end": we,
                    "word": str(w.get("word") or w.get("text") or ""),
                }
            )
    if words:
        words.sort(key=lambda w: w["start"])
        return words
    for w in transcript_words or []:
        try:
            ws, we = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if we <= ws:
            continue
        words.append(
            {
                "start": ws,
                "end": we,
                "word": str(w.get("word") or w.get("text") or ""),
            }
        )
    words.sort(key=lambda w: w["start"])
    return words


def _snap_start_off_mid_word(t: float, words: List[Dict], speech_start: float) -> float:
    """If t falls inside a word, move to that word's start (never chop a syllable)."""
    for w in words:
        ws = float(w["start"])
        we = float(w["end"])
        if ws < t < we:
            # Include the whole word; never start later than the speech onset we padded from.
            return min(ws, speech_start)
    return t


def apply_start_lead_in(
    start: float,
    end: float,
    segments: List[Dict],
    words: List[Dict],
    video_duration: float,
    max_seconds: float = MAX_CLIP_SECONDS,
    lead_in: float = CLIP_START_LEAD_IN,
    lead_min: float = CLIP_START_LEAD_IN_MIN,
    lead_max: float = CLIP_START_LEAD_IN_MAX,
) -> float:
    """Pull ``start`` earlier so the first word has breathing room.

    Prefers landing on the end of the previous sentence or inside a short
    silence; falls back to ``start - lead_in``. Never cuts mid-word.
    """
    speech_start = float(start)
    if lead_in <= 0 or speech_start <= 0:
        return speech_start

    lead_min = max(0.0, min(lead_min, lead_in))
    lead_max = max(lead_in, lead_max)
    # Don't blow past max clip length just for padding.
    room = max(0.0, max_seconds - (float(end) - speech_start))
    if room < lead_min:
        return speech_start
    lead_max = min(lead_max, room)
    lead_in = min(lead_in, lead_max)
    lead_min = min(lead_min, lead_in)

    window_lo = max(0.0, speech_start - lead_max)
    ideal = max(0.0, speech_start - lead_in)
    # Candidates: (priority, distance_to_ideal, time) — lower priority wins.
    candidates: List[tuple] = []

    for seg in segments:
        se = float(seg.get("end") or 0)
        if not (window_lo <= se <= speech_start):
            continue
        if not _segment_ends_thought(str(seg.get("text") or "")):
            continue
        # Small breath after the previous sentence when silence allows.
        next_starts = [
            float(s["start"])
            for s in segments
            if float(s.get("start") or -1) > se + 1e-6
        ]
        gap_to_next = (min(next_starts) - se) if next_starts else (speech_start - se)
        breath = min(0.2, max(0.0, gap_to_next * 0.5))
        cand = min(se + breath, speech_start)
        if cand >= window_lo and speech_start - cand >= lead_min * 0.85:
            candidates.append((0, abs(cand - ideal), cand))

    if words:
        for i in range(len(words) - 1):
            we = float(words[i]["end"])
            ns = float(words[i + 1]["start"])
            gap = ns - we
            if gap < 0.15:
                continue
            if not (window_lo <= we < speech_start):
                continue
            cand = min(we + min(0.25, gap * 0.5), speech_start)
            if cand < window_lo or cand >= speech_start:
                continue
            if speech_start - cand < lead_min * 0.85:
                continue
            candidates.append((1, abs(cand - ideal), cand))

    candidates.append((2, abs(ideal - ideal), ideal))
    candidates.sort()
    new_start = float(candidates[0][2])
    new_start = _snap_start_off_mid_word(new_start, words, speech_start)
    new_start = max(0.0, min(new_start, speech_start))

    # Word-start snap can overshoot lead_max — clamp, then escape mid-word forward.
    if new_start < window_lo:
        new_start = window_lo
        for w in words:
            ws = float(w["start"])
            we = float(w["end"])
            if ws < new_start < we:
                new_start = min(we, speech_start)
                break

    new_start = min(new_start, float(video_duration))

    # If snapping erased most of the pad, keep the original speech onset.
    if speech_start - new_start < 0.15:
        return speech_start
    return new_start


def expand_highlight_to_context(
    highlight: Dict,
    segments: List[Dict],
    video_duration: float,
    min_seconds: float = MIN_CLIP_SECONDS,
    target_seconds: float = TARGET_CLIP_SECONDS,
    max_seconds: float = MAX_CLIP_SECONDS,
    words: Optional[List[Dict]] = None,
) -> Dict:
    """Grow a hook-sized window into a self-contained clip using transcript segments.

    Strategy:
      1. Snap start/end onto covering Whisper segments.
      2. Expand backward (setup) and forward (payoff) until >= min_seconds,
         preferring target_seconds and ending on a completed thought.
      3. Cap at max_seconds.
      4. Pull start back with a short lead-in (sentence end / silence / pad).
    """
    if not segments:
        return highlight

    start = float(highlight["start_time"])
    end = float(highlight["end_time"])
    video_duration = float(video_duration or segments[-1]["end"])
    flat_words = words if words is not None else _flatten_words(segments)

    # Indices of segments that overlap the proposed window (or nearest).
    idxs = [
        i
        for i, s in enumerate(segments)
        if float(s["end"]) > start and float(s["start"]) < end
    ]
    if not idxs:
        idxs = [
            min(
                range(len(segments)),
                key=lambda i: abs(float(segments[i]["start"]) - start),
            )
        ]

    lo, hi = idxs[0], idxs[-1]
    start = float(segments[lo]["start"])
    end = float(segments[hi]["end"])

    def dur() -> float:
        return end - start

    # Expand until we hit the minimum (and keep going toward target when cheap).
    guard = 0
    while dur() < min_seconds and guard < len(segments) * 2:
        guard += 1
        can_back = lo > 0
        can_fwd = hi < len(segments) - 1
        if not can_back and not can_fwd:
            break

        # Prefer padding setup first (context before the hook), then payoff.
        need = min_seconds - dur()
        grow_back = can_back and (not can_fwd or need > 0 and (dur() < target_seconds * 0.55 or guard % 2 == 1))
        if grow_back:
            lo -= 1
            start = float(segments[lo]["start"])
        elif can_fwd:
            hi += 1
            end = float(segments[hi]["end"])

    # Keep extending forward a bit to land on a finished sentence / target length.
    while hi < len(segments) - 1 and dur() < target_seconds:
        next_end = float(segments[hi + 1]["end"])
        if next_end - start > max_seconds:
            break
        hi += 1
        end = next_end
        if _segment_ends_thought(str(segments[hi].get("text") or "")) and dur() >= min_seconds:
            break

    # Trim leading fragments that clearly continue a previous sentence
    # (Whisper often starts a cue mid-thought with a lowercase word).
    while lo < hi and dur() - (float(segments[lo]["end"]) - start) >= min_seconds:
        text = str(segments[lo].get("text") or "").lstrip()
        if text and text[0].islower() and not text[0].isdigit():
            lo += 1
            start = float(segments[lo]["start"])
        else:
            break

    # Trim if we overshot the max by pulling the nearer edge inward.
    if dur() > max_seconds:
        # Keep the original hook roughly centered in the window.
        hook_mid = (float(highlight["start_time"]) + float(highlight["end_time"])) / 2.0
        half = max_seconds / 2.0
        start = max(0.0, hook_mid - half)
        end = min(video_duration, start + max_seconds)
        # Re-snap to segments.
        covering = [
            i
            for i, s in enumerate(segments)
            if float(s["end"]) > start and float(s["start"]) < end
        ]
        if covering:
            start = float(segments[covering[0]]["start"])
            end = float(segments[covering[-1]]["end"])
            if end - start > max_seconds:
                end = start + max_seconds

    speech_onset = start
    start = apply_start_lead_in(
        start,
        end,
        segments,
        flat_words,
        video_duration,
        max_seconds=max_seconds,
    )
    lead = speech_onset - start
    if lead > 0.15:
        print(
            f"[highlights] start lead-in {lead:.2f}s "
            f"({speech_onset:.2f}s → {start:.2f}s)",
            flush=True,
        )

    start = max(0.0, min(start, video_duration))
    end = max(start + 0.5, min(end, video_duration))

    out = dict(highlight)
    out["start_time"] = round(start, 3)
    out["end_time"] = round(end, 3)
    return out


def expand_highlights_to_context(
    highlights: List[Dict],
    transcript: Dict,
) -> List[Dict]:
    segments = transcript.get("segments") or []
    duration = float(transcript.get("duration") or (segments[-1]["end"] if segments else 0))
    words = _flatten_words(segments, transcript.get("words") or [])
    expanded: List[Dict] = []
    for h in highlights:
        before = float(h["end_time"]) - float(h["start_time"])
        e = expand_highlight_to_context(h, segments, duration, words=words)
        after = float(e["end_time"]) - float(e["start_time"])
        if after < MIN_CLIP_SECONDS - 0.5:
            print(
                f"[highlights] drop too-short after expand: {before:.1f}s → {after:.1f}s "
                f"({e.get('title', '')!r})",
                flush=True,
            )
            continue
        if after - before > 1.0:
            print(
                f"[highlights] expanded clip {before:.1f}s → {after:.1f}s "
                f"({e.get('title', '')!r})",
                flush=True,
            )
        expanded.append(e)
    return expanded


def detect_content_type(transcript: Dict, llm_fn: LLMFn = call_muapi_llm) -> Dict[str, str]:
    segments = transcript.get("segments", [])
    sample = " ".join(s["text"] for s in segments[:25])[:3000]
    prompt = f"{CONTENT_TYPE_PROMPT}\n\nTranscript sample:\n{sample}"
    try:
        raw = llm_fn(prompt)
        return _parse_json_loose(raw)
    except Exception:
        return {"content_type": "other", "density": "medium"}


def build_transcript_text(transcript: Dict, offset: float = 0.0) -> str:
    """Render transcript lines for the LLM.

    When processing a long-video chunk, pass ``offset`` so timestamps are
    relative to the chunk start (0 … duration). The model otherwise copies
    absolute wall-clock times (e.g. 1500s) which then fail sanitization
    against the chunk's relative duration (~1200s).

    If segments carry ``speaker`` (from cast labeling), prefix each line.
    """
    from .cast import build_named_transcript_text

    segments = transcript.get("segments") or []
    if any(str(s.get("speaker") or "").strip() for s in segments):
        return build_named_transcript_text(transcript, offset=offset)
    return "\n".join(
        f"[{max(0.0, float(s['start']) - offset):.1f}s] {str(s.get('text') or '').strip()}"
        for s in segments
    )


def chunk_transcript(transcript: Dict) -> List[Dict]:
    segments = transcript.get("segments", [])
    duration = transcript.get("duration", segments[-1]["end"] if segments else 0)
    chunks = []
    start = 0
    while start < duration:
        end = min(start + CHUNK_SIZE_SECONDS, duration)
        chunk_segs = [
            s for s in segments
            if s["start"] >= start and s["end"] <= end + CHUNK_OVERLAP_SECONDS
        ]
        if chunk_segs:
            chunk = dict(transcript)
            chunk["segments"] = chunk_segs
            chunk["duration"] = end - start
            chunk["_offset"] = start
            chunks.append(chunk)
        start += CHUNK_SIZE_SECONDS - CHUNK_OVERLAP_SECONDS
    return chunks


def _num_clips_instruction(num_clips: Optional[int], duration: float, is_chunk: bool) -> str:
    """How many highlights to ask the model for.

    When ``num_clips`` is None the model decides from content density/length
    (used by the interactive web picker). Otherwise ask for ~2× the CLI target.
    """
    natural = max(2 if is_chunk else 3, int(float(duration or 0) / 90))
    if num_clips is None:
        aim = min(natural, 8 if not is_chunk else 5)
        return (
            f"Identify EVERY distinct viral-worthy topic in this transcript. "
            f"Return as many as the content genuinely supports "
            f"(typically around {aim}; more if dense). "
            "Do not invent weak filler clips — only real standout moments."
        )
    target = max(int(num_clips) * 2, 5)
    min_clips = min(target, natural, 8)
    return f"Generate at least {min_clips} highlights"


def call_highlight_api(
    transcript_text: str,
    content_info: Dict,
    duration: float,
    num_clips: Optional[int] = None,
    is_chunk: bool = False,
    llm_fn: LLMFn = call_muapi_llm,
    output_language: str = "Brazilian Portuguese (pt-BR)",
    cast_block: str = "",
) -> Dict:
    system = HIGHLIGHT_SYSTEM_PROMPT.format(
        virality_criteria=VIRALITY_CRITERIA,
        content_type=content_info.get("content_type", "other"),
        density=content_info.get("density", "medium"),
        num_clips_instruction=_num_clips_instruction(num_clips, duration, is_chunk),
        output_language=output_language,
        cast_block=cast_block or "",
    )
    base_prompt = f"{system}\n\nTranscript:\n{transcript_text}"
    prompt = base_prompt
    last_error = "unknown"

    for attempt in range(1, MAX_HIGHLIGHT_API_ATTEMPTS + 1):
        raw = llm_fn(prompt)
        try:
            parsed = _parse_json_loose(raw)
            if isinstance(parsed, list):
                raw_highlights = parsed
            elif isinstance(parsed, dict):
                raw_highlights = parsed.get("highlights")
                if raw_highlights is None and isinstance(parsed.get("data"), dict):
                    raw_highlights = parsed["data"].get("highlights")
            else:
                raw_highlights = None
            highlights = _sanitize_highlights(raw_highlights, duration=duration)
            if highlights:
                return {"highlights": highlights}
            n_raw = len(raw_highlights) if isinstance(raw_highlights, list) else 0
            preview = (raw or "").strip().replace("\n", " ")[:240]
            last_error = (
                f"no valid highlights in response "
                f"(parsed={n_raw} items, duration={duration:.1f}s, preview={preview!r})"
            )
        except Exception as e:
            preview = (raw or "").strip().replace("\n", " ")[:240]
            last_error = f"{e} (preview={preview!r})"

        if attempt < MAX_HIGHLIGHT_API_ATTEMPTS:
            print(
                f"[highlights] invalid model output on attempt {attempt}/{MAX_HIGHLIGHT_API_ATTEMPTS}; retrying",
                flush=True,
            )
            prompt = (
                base_prompt
                + "\n\nIMPORTANT: Return ONLY valid JSON with a top-level 'highlights' array."
                + " Each item must include: title, start_time, end_time, score, hook_sentence, virality_reason, attributed_to."
                + f" Timestamps must be relative to THIS transcript window (0 to {duration:.1f} seconds)."
                + f" EVERY clip duration (end_time - start_time) MUST be {int(MIN_CLIP_SECONDS)}–90 seconds"
                + " and cover setup + claim + payoff — never a single hook sentence."
                + f" title, hook_sentence, and virality_reason MUST be written in {output_language}."
                + " When speakers are known, put the person name in the title and attributed_to."
                + " No markdown fences, no commentary."
            )

    raise RuntimeError(
        f"Highlight generator produced invalid output after {MAX_HIGHLIGHT_API_ATTEMPTS} attempts: {last_error}"
    )


def dedupe_highlights(highlights: List[Dict]) -> List[Dict]:
    """Drop a highlight if it overlaps >50% with a higher-scoring one already kept."""
    highlights = sorted(highlights, key=lambda x: int(x.get("score", 0)), reverse=True)
    kept: List[Dict] = []
    for h in highlights:
        h_start = float(h["start_time"])
        h_end = float(h["end_time"])
        h_dur = h_end - h_start
        overlapping = False
        for k in kept:
            latest_start = max(h_start, float(k["start_time"]))
            earliest_end = min(h_end, float(k["end_time"]))
            overlap = earliest_end - latest_start
            if overlap > 0 and overlap > 0.5 * h_dur:
                overlapping = True
                break
        if not overlapping:
            kept.append(h)
    return kept


def snippet_for_range(transcript: Dict, start: float, end: float, max_chars: int = 280) -> str:
    """Join Whisper segments overlapping [start, end] into a short preview text."""
    parts: List[str] = []
    for seg in transcript.get("segments") or []:
        try:
            s = float(seg.get("start", 0))
            e = float(seg.get("end", 0))
        except (TypeError, ValueError):
            continue
        if e <= start or s >= end:
            continue
        text = str(seg.get("text") or "").strip()
        if text:
            parts.append(text)
    joined = " ".join(parts).strip()
    if len(joined) <= max_chars:
        return joined
    return joined[: max_chars - 1].rstrip() + "…"


def get_highlights(
    transcript: Dict,
    num_clips: Optional[int] = 3,
    llm_fn: Optional[LLMFn] = None,
    output_language: Optional[str] = None,
    speakers: Optional[List[Dict]] = None,
) -> Dict:
    """Main entry point — returns {highlights: [...]} sorted by score.

    `llm_fn` swaps the underlying LLM. Defaults to MuAPI gpt-5-mini; local
    mode passes in a local LLM-backed callable.
    `output_language` controls the language of title / hook / virality_reason.
    Pass ``num_clips=None`` to let the model decide how many topics to return.
    `speakers` — named cast roster for title attribution (from cast.py).
    """
    from .cast import cast_context_block
    from .config import language_label, resolve_content_language

    llm_fn = llm_fn or call_muapi_llm
    lang_code = resolve_content_language(output_language)
    lang_label = language_label(lang_code)
    duration = transcript.get("duration", 0)
    cast_speakers = speakers or transcript.get("speakers") or []
    cast_block = cast_context_block(cast_speakers) if cast_speakers else ""
    content_info = detect_content_type(transcript, llm_fn=llm_fn)
    print(
        f"[highlights] content={content_info.get('content_type')} "
        f"density={content_info.get('density')} duration={duration:.0f}s "
        f"output_lang={lang_code} speakers={len(cast_speakers)}",
        flush=True,
    )

    if duration >= LONG_VIDEO_THRESHOLD:
        chunks = chunk_transcript(transcript)
        print(f"[highlights] long video — splitting into {len(chunks)} chunks", flush=True)
        all_highlights: List[Dict] = []
        for i, chunk in enumerate(chunks):
            offset = float(chunk.get("_offset", 0) or 0)
            # Relative timestamps so sanitize(duration=chunk length) accepts them.
            text = build_transcript_text(chunk, offset=offset)
            print(f"[highlights] chunk {i + 1}/{len(chunks)} (offset {offset:.0f}s)", flush=True)
            result = call_highlight_api(
                text,
                content_info,
                chunk["duration"],
                num_clips=num_clips,
                is_chunk=True,
                llm_fn=llm_fn,
                output_language=lang_label,
                cast_block=cast_block,
            )
            for h in result.get("highlights", []):
                h["start_time"] = float(h["start_time"]) + offset
                h["end_time"] = float(h["end_time"]) + offset
                all_highlights.append(h)
        highlights = expand_highlights_to_context(all_highlights, transcript)
        highlights = dedupe_highlights(highlights)
    else:
        text = build_transcript_text(transcript)
        result = call_highlight_api(
            text,
            content_info,
            duration,
            num_clips=num_clips,
            llm_fn=llm_fn,
            output_language=lang_label,
            cast_block=cast_block,
        )
        highlights = expand_highlights_to_context(result.get("highlights", []), transcript)
        highlights = dedupe_highlights(highlights)

    if not highlights:
        raise RuntimeError(
            f"No highlights left after enforcing min duration ({MIN_CLIP_SECONDS:.0f}s) "
            "and complete-context expansion."
        )

    return {"highlights": highlights}
