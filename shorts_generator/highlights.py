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
from .virality import (
    DEFAULT_HOOK_IN_FIRST_SECONDS,
    build_profile_prompt_block,
    build_virality_criteria,
    normalize_virality_profile,
)


LLMFn = Callable[[str], str]


CONTENT_TYPE_PROMPT = """Analyze this video transcript sample and classify the content type.
Choose one: podcast, interview, tutorial, lecture, commentary, debate, vlog, other.
Also estimate content density: low (mostly filler/chit-chat), medium, or high (dense info/stories).
Respond with JSON only: {"content_type": "...", "density": "..."}"""


# Kept for docs / external imports; runtime uses build_virality_criteria(profile).
VIRALITY_CRITERIA = build_virality_criteria(None)


HIGHLIGHT_SYSTEM_PROMPT = """You are {editor_role}

{virality_criteria}

Content type: {content_type} | Density: {density} | Clip format: {clip_length}

{profile_block}

Your task: identify the most viral-worthy highlights from the transcript.

Rules:
- Every highlight must open with a strong HOOK — the critical spoken line is the FIRST thing the viewer hears (within the first ~2–3 seconds)
- Structure is HOOK → claim/peak → payoff. Do NOT put soft setup or throat-clearing before the hook. Any needed context comes AFTER the opening line.
{duration_rules}
- Each clip must be a COMPLETE argument/story that a viewer who never saw the full video still understands.
- Never cut mid-sentence or mid-thought — end on a completed idea
- Clips must not overlap significantly with each other
- Score 0-100 on viral potential (not general quality)
- {num_clips_instruction}
- For each highlight, identify the single best "hook_sentence" — the opening line that would make someone stop scrolling
- Set "hook_start_time" to the transcript timestamp where that hook_sentence begins speaking
- start_time must be ≤ hook_start_time and close to it (small silence lead-in only). end_time covers the payoff after the hook.
- Explain in one sentence why this clip is viral ("virality_reason")
- CRITICAL language rule: write title, hook_sentence, and virality_reason entirely in {output_language}. Do NOT use English unless the output language is English. Prefer hooks taken from / closely paraphrasing the transcript.

{title_specialist}

{cast_block}

Respond ONLY with valid JSON (no markdown, no explanation):
{{"highlights":[{{"title":"string","start_time":float,"end_time":float,"hook_start_time":float,"score":int,"hook_sentence":"string","virality_reason":"string","attributed_to":"string"}}]}}"""


TITLE_SPECIALIST_SHORT = """TITLE SPECIALIST (YouTube Shorts / Reels / TikTok — this is the feed title, not a chapter heading):
You write titles the way a growth editor writes Shorts titles: scroll-stopping, specific, emotional, and easy to read in under 1 second.
- The title must SELL the clip. Lead with the claim, provocation, contradiction, or curiosity gap — never with a neutral topic label.
- Ideal length: ~40–70 characters. Hard max ~80. Cut filler words.
- Prefer concrete language from the clip (a quote-shaped claim) over abstract themes.
- Patterns that WORK: bold claim · open question · "X vs Y" · unexpected confession · "ninguém fala isso" energy · specific number/detail when present.
- Patterns that FAIL (never use): "Nome: tema genérico", "Nome fala sobre X", "o papel de X", "a importância de X", "X e suas consequências", "a verdade sobre X", "a polêmica sobre X", essay/chapter titles, Wikipedia-style labels.
- Do NOT start every title with the speaker name. Put the person in attributed_to. Optionally weave a short famous name INTO the punchline when it adds social proof (e.g. "Cassius: aborto é assassinato") — but the punchline still comes first or owns the title.
- Each title must be UNIQUE across the batch — no near-duplicates that only swap synonyms.
- Title and hook_sentence are different jobs: title = feed bait; hook_sentence = first spoken line / on-screen opener. Do not paste the same string into both.
- When KNOWN SPEAKERS are provided (podcast/interview/debate), fill attributed_to with the main speaker / public figure (prefer guests over hosts). Never invent names outside the list. attributed_to may be empty if none apply.

Bad → Good examples (Portuguese):
- BAD: "Cassius Ogrus: liberdade de expressão e suas consequências" → GOOD: "Liberdade total? Tem coisa que não pode"
- BAD: "Cassius Ogrus: a verdade sobre as vacinas" → GOOD: "Proibir falar de vacina é pior que mentir"
- BAD: "Cassius Ogrus: o papel da comédia na vida das pessoas" → GOOD: "Meu humor fez gente rir de novo na depressão"
- BAD: "Cassius Ogrus: O que é sucesso?" → GOOD: "Sucesso pra mim é ter família — ponto"
"""

TITLE_SPECIALIST_LONG = """TITLE SPECIALIST (YouTube mid-form 3–10 min — upload title, not a chapter label):
You write titles that sell a self-contained segment: curiosity + substance, readable in under 2 seconds.
- Lead with the claim, conflict, or curiosity gap — never a neutral topic label.
- Ideal length: ~45–85 characters. Hard max ~100.
- Prefer concrete language from the clip over abstract themes.
- Patterns that FAIL (never use): "Nome: tema genérico", "Nome fala sobre X", "o papel de X", "a importância de X", essay/chapter titles.
- Do NOT start every title with the speaker name. Put the person in attributed_to.
- Each title must be UNIQUE across the batch.
- Title and hook_sentence are different jobs: title = thumbnail/feed bait; hook_sentence = first spoken line.
- When KNOWN SPEAKERS are provided, fill attributed_to (prefer guests over hosts). Never invent names outside the list.

Bad → Good examples (Portuguese):
- BAD: "Kim Kataguiri: emendas parlamentares" → GOOD: "Orçamento secreto: por que o Centrão não larga o osso"
- BAD: "Debate sobre letalidade policial" → GOOD: "Bandido negro atirando: ele responde sem filtro"
"""

DURATION_RULES_SHORT = """- HARD duration rule: every clip MUST be between 45 and 90 seconds. Prefer ~60s. Never return a hook-only snippet.
- Go longer (91-180s) ONLY when a story arc needs full context to land. Never go under 45 seconds."""

DURATION_RULES_LONG = """- HARD duration rule: every clip MUST be between 180 and 600 seconds (3–10 minutes). Prefer ~5–6 minutes (~300–360s).
- Never return a short hook snippet or a sub-3-minute tease — each clip is a full mid-form segment with setup, development, and payoff.
- Go toward 8–10 minutes ONLY when the topic genuinely needs that arc. Never go under 180 seconds."""


CHUNK_SIZE_SECONDS = 1200       # 20-min chunks for long source videos
LONG_VIDEO_THRESHOLD = 1800     # chunk sources longer than 30 min
CHUNK_OVERLAP_SECONDS = 60
GPT_CALL_TIMEOUT_SECONDS = 300  # cap LLM polls at 5 min — a wedged call should fail fast
MAX_HIGHLIGHT_API_ATTEMPTS = 3

# Default = short-form (back-compat aliases)
MIN_CLIP_SECONDS = 45.0
TARGET_CLIP_SECONDS = 65.0
MAX_CLIP_SECONDS = 180.0

CLIP_LENGTH_SHORT = "short"
CLIP_LENGTH_LONG = "long"
DEFAULT_CLIP_LENGTH = CLIP_LENGTH_SHORT

CLIP_LENGTH_PRESETS: Dict[str, Dict] = {
    CLIP_LENGTH_SHORT: {
        "id": CLIP_LENGTH_SHORT,
        "min_seconds": 45.0,
        "target_seconds": 65.0,
        "max_seconds": 180.0,
        "prefer_hi": 90.0,
        "seconds_per_clip_hint": 90.0,
        "max_natural": 8,
        "max_natural_chunk": 5,
        "editor_role": (
            "an elite short-form video editor who has studied thousands of viral "
            "clips on TikTok, Instagram Reels, and YouTube Shorts. You know exactly "
            "what makes viewers stop scrolling, watch to the end, and share."
        ),
        "duration_rules": DURATION_RULES_SHORT,
        "title_specialist": TITLE_SPECIALIST_SHORT,
        "retry_duration_label": "45–90 seconds (up to 180s only if the arc needs it)",
    },
    CLIP_LENGTH_LONG: {
        "id": CLIP_LENGTH_LONG,
        "min_seconds": 180.0,
        "target_seconds": 360.0,
        "max_seconds": 600.0,
        "prefer_hi": 480.0,
        "seconds_per_clip_hint": 360.0,
        "max_natural": 6,
        "max_natural_chunk": 3,
        "editor_role": (
            "an elite YouTube editor who cuts long interviews/podcasts into "
            "self-contained 3–10 minute mid-form videos that retain viewers "
            "and stand alone without the full episode."
        ),
        "duration_rules": DURATION_RULES_LONG,
        "title_specialist": TITLE_SPECIALIST_LONG,
        "retry_duration_label": "180–600 seconds (3–10 minutes; prefer ~5–6 min)",
    },
}


def normalize_clip_length(value: Optional[str]) -> str:
    raw = (value or DEFAULT_CLIP_LENGTH).strip().lower()
    if raw in ("long", "longo", "mid", "midform", "mid-form", "3-10", "3–10"):
        return CLIP_LENGTH_LONG
    return CLIP_LENGTH_SHORT


def clip_length_preset(clip_length: Optional[str] = None) -> Dict:
    return CLIP_LENGTH_PRESETS[normalize_clip_length(clip_length)]


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

        hook_start_raw = item.get("hook_start_time", item.get("hook_time"))
        hook_start = _coerce_float(hook_start_raw, default=-1.0)
        if hook_start < 0:
            hook_start = start
        # Clamp into the proposed window when the model drifts.
        hook_start = max(start, min(hook_start, end))

        cleaned.append(
            {
                "title": str(item.get("title") or "Untitled Highlight").strip(),
                "start_time": start,
                "end_time": end,
                "hook_start_time": hook_start,
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


def _norm_match_text(text: str) -> str:
    t = (text or "").casefold()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def resolve_hook_start(
    highlight: Dict,
    segments: List[Dict],
    *,
    search_pad: float = 45.0,
) -> float:
    """Locate when the hook line begins speaking in the transcript."""
    start = float(highlight.get("start_time") or 0)
    end = float(highlight.get("end_time") or start)
    declared = float(highlight.get("hook_start_time") or -1)
    if start <= declared <= end:
        return declared

    hook = _norm_match_text(str(highlight.get("hook_sentence") or ""))
    if not hook or not segments:
        return start if declared < 0 else max(start, min(declared, end))

    # Prefer longer prefix matches of the hook sentence against segments near the window.
    window_lo = max(0.0, start - search_pad)
    window_hi = end + search_pad
    hook_tokens = hook.split()
    best: Optional[tuple] = None  # (score, start)

    for seg in segments:
        try:
            ss = float(seg.get("start") or 0)
            se = float(seg.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if se < window_lo or ss > window_hi:
            continue
        text = _norm_match_text(str(seg.get("text") or ""))
        if not text:
            continue
        score = 0
        if hook in text or text in hook:
            score = 100 + min(len(text), len(hook))
        else:
            # Overlap of leading hook tokens.
            seg_tokens = text.split()
            matched = 0
            for a, b in zip(hook_tokens, seg_tokens):
                if a == b:
                    matched += 1
                else:
                    break
            if matched >= 3 or (matched >= 2 and matched >= len(hook_tokens) * 0.5):
                score = matched * 10
            else:
                # Any significant shared span
                for n in (8, 6, 5, 4):
                    if len(hook_tokens) < n:
                        continue
                    needle = " ".join(hook_tokens[:n])
                    if needle and needle in text:
                        score = n * 8
                        break
        if score <= 0:
            continue
        # Prefer segments closest to the model's proposed start.
        dist = abs(ss - start)
        candidate = (score, -dist, ss)
        if best is None or candidate > best:
            best = candidate

    if best is not None:
        return float(best[2])
    if declared >= 0:
        return max(0.0, declared)
    return start


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
    hook_in_first_seconds: float = DEFAULT_HOOK_IN_FIRST_SECONDS,
) -> Dict:
    """Grow a hook window into a self-contained clip — hook stays early.

    Strategy (hook-first):
      1. Resolve when the hook line begins speaking.
      2. Snap onto covering Whisper segments starting at the hook.
      3. Expand FORWARD (payoff) until >= min / toward target; only tiny
         backward pad for silence lead-in — never bury the hook behind setup.
      4. Cap at max_seconds by trimming the END (keep hook at the front).
      5. Enforce hook_start - clip_start ≤ hook_in_first_seconds.
    """
    if not segments:
        return highlight

    video_duration = float(video_duration or segments[-1]["end"])
    flat_words = words if words is not None else _flatten_words(segments)
    hook_t = resolve_hook_start(highlight, segments)
    hook_t = max(0.0, min(hook_t, video_duration))
    max_hook_delay = max(1.0, min(5.0, float(hook_in_first_seconds)))

    # Segment covering the hook onset (or nearest).
    hook_idx = None
    for i, s in enumerate(segments):
        ss, se = float(s["start"]), float(s["end"])
        if ss <= hook_t < se or (ss <= hook_t <= se):
            hook_idx = i
            break
    if hook_idx is None:
        hook_idx = min(
            range(len(segments)),
            key=lambda i: abs(float(segments[i]["start"]) - hook_t),
        )

    lo = hi = hook_idx
    # Prefer starting exactly at the hook segment (critical line first).
    start = float(segments[lo]["start"])
    # If Whisper segment starts well before the matched hook word, snap later
    # when we have word timings that land near hook_t.
    if flat_words:
        for w in flat_words:
            ws = float(w["start"])
            we = float(w["end"])
            if ws <= hook_t <= we + 0.05:
                # Don't start mid-word earlier than needed.
                if ws >= start - 0.01 and hook_t - ws <= max_hook_delay:
                    start = ws
                break
    end = float(segments[hi]["end"])
    # Ensure end is after hook.
    if end <= start:
        end = min(video_duration, start + min_seconds)

    def dur() -> float:
        return end - start

    # Expand FORWARD first until minimum / toward target.
    guard = 0
    while dur() < min_seconds and guard < len(segments) * 2:
        guard += 1
        can_fwd = hi < len(segments) - 1
        can_back = lo > 0
        if not can_fwd and not can_back:
            break
        if can_fwd:
            hi += 1
            end = float(segments[hi]["end"])
            continue
        # Only grow back if still under min AND hook would stay early.
        prev_start = float(segments[lo - 1]["start"])
        if hook_t - prev_start <= max_hook_delay:
            lo -= 1
            start = prev_start
        else:
            break

    # Keep extending forward to land on a finished sentence / target length.
    while hi < len(segments) - 1 and dur() < target_seconds:
        next_end = float(segments[hi + 1]["end"])
        if next_end - start > max_seconds:
            break
        hi += 1
        end = next_end
        if _segment_ends_thought(str(segments[hi].get("text") or "")) and dur() >= min_seconds:
            break

    # Trim leading fragments that are clearly pre-hook setup shoved in by
    # segment snap (lowercase continuation, or speech before hook_t).
    while lo < hi and float(segments[lo]["end"]) <= hook_t + 0.05:
        next_start = float(segments[lo + 1]["start"])
        # Keep a segment that contains the hook.
        if float(segments[lo]["start"]) <= hook_t < float(segments[lo]["end"]):
            break
        if hook_t - next_start > max_hook_delay:
            break
        # Drop pure pre-hook segments when we still have room after.
        if next_start <= hook_t and dur() - (next_start - start) >= min_seconds * 0.85:
            lo += 1
            start = next_start
        else:
            break

    while lo < hi and dur() - (float(segments[lo]["end"]) - start) >= min_seconds:
        text = str(segments[lo].get("text") or "").lstrip()
        if text and text[0].islower() and not text[0].isdigit():
            # Don't drop the segment that holds the hook.
            if float(segments[lo]["start"]) <= hook_t < float(segments[lo]["end"]):
                break
            lo += 1
            start = float(segments[lo]["start"])
        else:
            break

    # Cap max length by trimming the END — keep the hook at the front.
    if dur() > max_seconds:
        end = min(video_duration, start + max_seconds)
        covering = [
            i
            for i, s in enumerate(segments)
            if float(s["end"]) > start and float(s["start"]) < end
        ]
        if covering:
            # Keep lo (hook side); pull hi inward.
            hi = covering[-1]
            end = float(segments[hi]["end"])
            if end - start > max_seconds:
                end = start + max_seconds
            # Prefer ending on a completed thought inside the cap.
            while hi > lo:
                if _segment_ends_thought(str(segments[hi].get("text") or "")):
                    end = float(segments[hi]["end"])
                    if end - start <= max_seconds and end - start >= min_seconds * 0.9:
                        break
                hi -= 1
                end = float(segments[hi]["end"])

    speech_onset = start
    start = apply_start_lead_in(
        start,
        end,
        segments,
        flat_words,
        video_duration,
        max_seconds=max_seconds,
    )
    # Lead-in must not push the hook past the hard delay.
    if hook_t - start > max_hook_delay:
        start = max(0.0, hook_t - max_hook_delay)
        start = _snap_start_off_mid_word(start, flat_words, speech_onset)
        # If snap pulled us too early again, land at speech onset.
        if hook_t - start > max_hook_delay:
            start = max(speech_onset, hook_t - max_hook_delay)

    lead = speech_onset - start
    if lead > 0.15:
        print(
            f"[highlights] start lead-in {lead:.2f}s "
            f"({speech_onset:.2f}s → {start:.2f}s)",
            flush=True,
        )

    # Final hard clamp: hook must stay in the opening window.
    if hook_t - start > max_hook_delay:
        old = start
        start = max(0.0, hook_t - max_hook_delay)
        print(
            f"[highlights] hook-first realign {old:.2f}s → {start:.2f}s "
            f"(hook at {hook_t:.2f}s, max delay {max_hook_delay:.1f}s)",
            flush=True,
        )

    start = max(0.0, min(start, video_duration))
    end = max(start + 0.5, min(end, video_duration))
    # If clamping start ate duration, try to extend end.
    if end - start < min_seconds and end < video_duration:
        end = min(video_duration, start + max(min_seconds, target_seconds * 0.9))

    out = dict(highlight)
    out["start_time"] = round(start, 3)
    out["end_time"] = round(end, 3)
    out["hook_start_time"] = round(hook_t, 3)
    return out


def expand_highlights_to_context(
    highlights: List[Dict],
    transcript: Dict,
    hook_in_first_seconds: float = DEFAULT_HOOK_IN_FIRST_SECONDS,
    clip_length: Optional[str] = None,
) -> List[Dict]:
    preset = clip_length_preset(clip_length)
    min_seconds = float(preset["min_seconds"])
    target_seconds = float(preset["target_seconds"])
    max_seconds = float(preset["max_seconds"])
    segments = transcript.get("segments") or []
    duration = float(transcript.get("duration") or (segments[-1]["end"] if segments else 0))
    words = _flatten_words(segments, transcript.get("words") or [])
    expanded: List[Dict] = []
    for h in highlights:
        before = float(h["end_time"]) - float(h["start_time"])
        e = expand_highlight_to_context(
            h,
            segments,
            duration,
            min_seconds=min_seconds,
            target_seconds=target_seconds,
            max_seconds=max_seconds,
            words=words,
            hook_in_first_seconds=hook_in_first_seconds,
        )
        after = float(e["end_time"]) - float(e["start_time"])
        hook_delay = float(e.get("hook_start_time", e["start_time"])) - float(e["start_time"])
        if after < min_seconds - 0.5:
            print(
                f"[highlights] drop too-short after expand: {before:.1f}s → {after:.1f}s "
                f"(need ≥{min_seconds:.0f}s) ({e.get('title', '')!r})",
                flush=True,
            )
            continue
        if after - before > 1.0:
            print(
                f"[highlights] expanded clip {before:.1f}s → {after:.1f}s "
                f"hook+{hook_delay:.1f}s ({e.get('title', '')!r})",
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


def chunk_transcript(
    transcript: Dict,
    *,
    chunk_size: float = CHUNK_SIZE_SECONDS,
    overlap: float = CHUNK_OVERLAP_SECONDS,
) -> List[Dict]:
    segments = transcript.get("segments", [])
    duration = transcript.get("duration", segments[-1]["end"] if segments else 0)
    chunk_size = max(60.0, float(chunk_size))
    overlap = max(0.0, min(float(overlap), chunk_size * 0.5))
    step = max(30.0, chunk_size - overlap)
    chunks = []
    start = 0.0
    while start < duration:
        end = min(start + chunk_size, duration)
        chunk_segs = [
            s for s in segments
            if s["start"] >= start and s["end"] <= end + overlap
        ]
        if chunk_segs:
            chunk = dict(transcript)
            chunk["segments"] = chunk_segs
            chunk["duration"] = end - start
            chunk["_offset"] = start
            chunks.append(chunk)
        if end >= duration:
            break
        start += step
    return chunks


def _chunk_params_for_clip_length(clip_length: Optional[str]) -> tuple:
    """Wider windows + more overlap so 3–10 min arcs aren't split across chunks."""
    if normalize_clip_length(clip_length) == CLIP_LENGTH_LONG:
        return 1800.0, 360.0  # 30-min windows, 6-min overlap
    return float(CHUNK_SIZE_SECONDS), float(CHUNK_OVERLAP_SECONDS)


def _num_clips_instruction(
    num_clips: Optional[int],
    duration: float,
    is_chunk: bool,
    clip_length: Optional[str] = None,
) -> str:
    """How many highlights to ask the model for.

    When ``num_clips`` is None the model decides from content density/length
    (used by the interactive web picker). Otherwise ask for ~2× the CLI target.
    """
    preset = clip_length_preset(clip_length)
    hint = float(preset["seconds_per_clip_hint"])
    max_natural = int(preset["max_natural_chunk"] if is_chunk else preset["max_natural"])
    natural = max(2 if is_chunk else 3, int(float(duration or 0) / hint))
    if num_clips is None:
        aim = min(natural, max_natural)
        kind = (
            "self-contained mid-form segments (3–10 min)"
            if preset["id"] == CLIP_LENGTH_LONG
            else "viral short-form topics"
        )
        return (
            f"Identify EVERY distinct {kind} in this transcript. "
            f"Return as many as the content genuinely supports "
            f"(typically around {aim}; more if dense). "
            "Do not invent weak filler clips — only real standout moments."
        )
    target = max(int(num_clips) * 2, 5)
    min_clips = min(target, natural, max_natural)
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
    virality_profile: Optional[Dict] = None,
    clip_length: Optional[str] = None,
) -> Dict:
    profile = normalize_virality_profile(virality_profile)
    preset = clip_length_preset(clip_length)
    system = HIGHLIGHT_SYSTEM_PROMPT.format(
        editor_role=preset["editor_role"],
        virality_criteria=build_virality_criteria(profile),
        content_type=content_info.get("content_type", "other"),
        density=content_info.get("density", "medium"),
        clip_length=preset["id"],
        duration_rules=preset["duration_rules"],
        title_specialist=preset["title_specialist"],
        num_clips_instruction=_num_clips_instruction(
            num_clips, duration, is_chunk, clip_length=preset["id"]
        ),
        output_language=output_language,
        cast_block=cast_block or "",
        profile_block=build_profile_prompt_block(profile),
    )
    base_prompt = f"{system}\n\nTranscript:\n{transcript_text}"
    prompt = base_prompt
    last_error = "unknown"
    hook_s = profile["hook_in_first_seconds"]
    min_s = int(preset["min_seconds"])
    prefer_hi = int(preset["prefer_hi"])

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
                + " Each item must include: title, start_time, end_time, hook_start_time, score, hook_sentence, virality_reason, attributed_to."
                + f" Timestamps must be relative to THIS transcript window (0 to {duration:.1f} seconds)."
                + f" EVERY clip duration (end_time - start_time) MUST be {preset['retry_duration_label']}"
                + f" (hard range {min_s}–{int(preset['max_seconds'])}s; prefer up to ~{prefer_hi}s)"
                + " and cover hook + claim + payoff — never a single hook sentence."
                + f" hook_start_time - start_time MUST be ≤ {hook_s:.1f} seconds (hook opens the clip)."
                + f" title, hook_sentence, and virality_reason MUST be written in {output_language}."
                + " Titles must sell the clip (claim/curiosity), NEVER 'Name: generic topic'."
                + " Put the speaker in attributed_to, not as a boring title prefix."
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
    virality_profile: Optional[Dict] = None,
    clip_length: Optional[str] = None,
) -> Dict:
    """Main entry point — returns {highlights: [...]} sorted by score.

    `llm_fn` swaps the underlying LLM. Defaults to MuAPI gpt-5-mini; local
    mode passes in a local LLM-backed callable.
    `output_language` controls the language of title / hook / virality_reason.
    Pass ``num_clips=None`` to let the model decide how many topics to return.
    `speakers` — named cast roster for title attribution (from cast.py).
    `virality_profile` — per-channel editor taste (see virality.py).
    `clip_length` — ``short`` (45–90s Shorts) or ``long`` (3–10 min mid-form).
    """
    from .cast import cast_context_block
    from .config import language_label, resolve_content_language

    llm_fn = llm_fn or call_muapi_llm
    lang_code = resolve_content_language(output_language)
    lang_label = language_label(lang_code)
    duration = transcript.get("duration", 0)
    cast_speakers = speakers or transcript.get("speakers") or []
    cast_block = cast_context_block(cast_speakers) if cast_speakers else ""
    profile = normalize_virality_profile(virality_profile)
    hook_s = float(profile["hook_in_first_seconds"])
    preset = clip_length_preset(clip_length)
    clip_len = preset["id"]
    content_info = detect_content_type(transcript, llm_fn=llm_fn)
    if float(duration or 0) < float(preset["min_seconds"]):
        raise RuntimeError(
            f"Fonte tem {float(duration or 0):.0f}s — insuficiente para clip_length={clip_len} "
            f"(mínimo {preset['min_seconds']:.0f}s por corte)."
        )
    print(
        f"[highlights] content={content_info.get('content_type')} "
        f"density={content_info.get('density')} duration={duration:.0f}s "
        f"clip_length={clip_len} "
        f"({preset['min_seconds']:.0f}–{preset['max_seconds']:.0f}s) "
        f"output_lang={lang_code} speakers={len(cast_speakers)} "
        f"hook_first≤{hook_s:.1f}s",
        flush=True,
    )

    api_kwargs = dict(
        content_info=content_info,
        num_clips=num_clips,
        llm_fn=llm_fn,
        output_language=lang_label,
        cast_block=cast_block,
        virality_profile=profile,
        clip_length=clip_len,
    )

    if duration >= LONG_VIDEO_THRESHOLD:
        chunk_size, overlap = _chunk_params_for_clip_length(clip_len)
        chunks = chunk_transcript(transcript, chunk_size=chunk_size, overlap=overlap)
        print(
            f"[highlights] long source — splitting into {len(chunks)} chunks "
            f"(size={chunk_size:.0f}s overlap={overlap:.0f}s)",
            flush=True,
        )
        all_highlights: List[Dict] = []
        for i, chunk in enumerate(chunks):
            offset = float(chunk.get("_offset", 0) or 0)
            # Relative timestamps so sanitize(duration=chunk length) accepts them.
            text = build_transcript_text(chunk, offset=offset)
            print(f"[highlights] chunk {i + 1}/{len(chunks)} (offset {offset:.0f}s)", flush=True)
            result = call_highlight_api(
                text,
                duration=chunk["duration"],
                is_chunk=True,
                **api_kwargs,
            )
            for h in result.get("highlights", []):
                h["start_time"] = float(h["start_time"]) + offset
                h["end_time"] = float(h["end_time"]) + offset
                if "hook_start_time" in h:
                    h["hook_start_time"] = float(h["hook_start_time"]) + offset
                all_highlights.append(h)
        highlights = expand_highlights_to_context(
            all_highlights,
            transcript,
            hook_in_first_seconds=hook_s,
            clip_length=clip_len,
        )
        highlights = dedupe_highlights(highlights)
    else:
        text = build_transcript_text(transcript)
        result = call_highlight_api(
            text,
            duration=duration,
            **api_kwargs,
        )
        highlights = expand_highlights_to_context(
            result.get("highlights", []),
            transcript,
            hook_in_first_seconds=hook_s,
            clip_length=clip_len,
        )
        highlights = dedupe_highlights(highlights)

    if not highlights:
        raise RuntimeError(
            f"No highlights left after enforcing min duration "
            f"({preset['min_seconds']:.0f}s) and complete-context expansion."
        )

    return {
        "highlights": highlights,
        "content_type": content_info.get("content_type") or "other",
        "density": content_info.get("density") or "medium",
        "clip_length": clip_len,
    }
