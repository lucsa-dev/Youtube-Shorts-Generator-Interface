"""Thumbnail generation — speaker frame + local PIL overlay (no image-model BG)."""
from __future__ import annotations

import base64
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import (
    require_gemini_key,
    require_openai_key,
)
from . import config as _cfg

CITED_PEOPLE_PROMPT = """From this short-clip transcript snippet, list public figures who are CITED or talked ABOUT (not the people speaking on camera).

Known on-camera speakers (exclude these):
{speakers}

Snippet:
{snippet}

Rules:
- Max 2 people
- Prefer famous / recognizable names
- Add a short disambiguation hint (role/country) when useful
- If nobody notable is cited, return an empty list
- Respond JSON only: {{"people":[{{"name":"string","hint":"string"}}]}}"""

THUMBNAIL_TEXT_PROMPT = """You write the SHORT on-image text for a viral YouTube Shorts / TikTok / Reels thumbnail (the big letters burned onto the image — not the feed title).

Goal: maximize scroll-stop and click probability. Curiosity gap, bold claim, contradiction, or emotional punch — still faithful to the clip. Think growth editor, not chapter heading.

Inputs:
- Feed title: {title}
- Spoken hook: {hook}
- Why this clip is viral: {reason}
- Transcript snippet: {snippet}

Rules:
- Write entirely in {language}. Do NOT use English unless the language is English.
- Ideal length: 3–6 words. Hard max ~40 characters (spaces count).
- Compress — never paste a long title. Extract the click trigger.
- No speaker-name prefix ("Fulano:"), no hashtags, no emoji, no quotation marks, no trailing ellipsis spam.
- Prefer concrete punch from the clip over vague labels ("a verdade", "o papel de", "a importância").
- Patterns that WORK: bold claim · open question · "X vs Y" · unexpected confession · specific number/detail.
- ALL CAPS is applied later — write with normal casing and correct accents.

Respond JSON only: {{"text":"string"}}"""

def _thumbnail_palette() -> List[Tuple[int, int, int]]:
    """Shared viral palette (border gradient + per-word text fills) from config."""
    from .config import parse_thumbnail_palette

    return parse_thumbnail_palette()


def _parse_json_loose(raw: str) -> Dict:
    text = (raw or "").strip()
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


def extract_cited_people(
    snippet: str,
    speakers: Sequence[str],
    *,
    llm_fn=None,
) -> List[Dict[str, str]]:
    """LLM-extract cited public figures from the clip text."""
    text = (snippet or "").strip()
    if len(text) < 12:
        return []

    if llm_fn is None:
        from .local.llm import call_openai_llm

        llm_fn = call_openai_llm

    roster = "\n".join(f"- {n}" for n in speakers if n) or "- (none)"
    prompt = CITED_PEOPLE_PROMPT.format(speakers=roster, snippet=text[:1800])
    try:
        raw = llm_fn(prompt)
        parsed = _parse_json_loose(raw)
    except Exception as e:
        print(f"[thumb-ai] cited people extract failed: {e}", flush=True)
        return []

    people = parsed.get("people") if isinstance(parsed, dict) else None
    if not isinstance(people, list):
        return []

    speaker_keys = {str(s).strip().casefold() for s in speakers if str(s).strip()}
    out: List[Dict[str, str]] = []
    seen = set()
    for item in people:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or len(name) < 2:
            continue
        key = name.casefold()
        if key in seen or key in speaker_keys:
            continue
        if any(key in sk or sk in key for sk in speaker_keys if len(sk) >= 4):
            continue
        seen.add(key)
        out.append(
            {
                "name": name,
                "hint": str(item.get("hint") or "").strip()[:120],
            }
        )
        if len(out) >= 2:
            break
    return out


def _language_label(language: str) -> str:
    lang = (language or "pt").strip().lower()
    if lang.startswith("pt"):
        return "Brazilian Portuguese"
    if lang.startswith("en"):
        return "English"
    if lang.startswith("es"):
        return "Spanish"
    return f"language code '{lang}'"


def generate_thumbnail_overlay_text(
    *,
    title: str = "",
    hook: str = "",
    reason: str = "",
    snippet: str = "",
    language: str = "pt",
    llm_fn=None,
) -> str:
    """LLM: short click-bait on-image text from title/hook/reason/snippet.

    Returns plain text (not forced UPPERCASE). Empty string only when every
    input is empty — callers should fall back to ``_hook_for_thumb``.
    """
    title = (title or "").strip()
    hook = (hook or "").strip()
    reason = (reason or "").strip()
    snippet = (snippet or "").strip()
    if not any((title, hook, reason, snippet)):
        return ""

    if llm_fn is None:
        from .local.llm import call_local_llm

        llm_fn = call_local_llm

    prompt = THUMBNAIL_TEXT_PROMPT.format(
        title=title or "(none)",
        hook=hook or "(none)",
        reason=reason or "(none)",
        snippet=(snippet[:1200] if snippet else "(none)"),
        language=_language_label(language),
    )
    try:
        raw = llm_fn(prompt)
        parsed = _parse_json_loose(raw)
    except Exception as e:
        print(f"[thumb-ai] overlay text LLM failed: {e}", flush=True)
        return ""

    text = ""
    if isinstance(parsed, dict):
        text = str(parsed.get("text") or parsed.get("overlay") or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" \"'`“”‘’")
    # Drop accidental ALL-CAPS so _hook_for_thumb can normalize once
    if len(text) > 40:
        words = text.split()
        trimmed: List[str] = []
        for w in words:
            candidate = (" ".join(trimmed + [w])).strip()
            if len(candidate) > 40:
                break
            trimmed.append(w)
        text = " ".join(trimmed) if trimmed else text[:39].rstrip()
    return text


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


_NAME_STOPWORDS = frozenset(
    {
        "de",
        "da",
        "do",
        "das",
        "dos",
        "e",
        "the",
        "of",
        "a",
        "an",
        "van",
        "von",
    }
)


def _name_tokens(value: str) -> set:
    return {t for t in _normalize_name(value).split() if t and t not in _NAME_STOPWORDS}


def _name_match_score(target: str, candidate: str) -> float:
    """Score how well ``candidate`` matches ``target`` (0–1)."""
    if not target or not candidate:
        return 0.0
    if target == candidate:
        return 1.0
    if target in candidate or candidate in target:
        return 0.85
    ta = _name_tokens(target)
    tb = _name_tokens(candidate)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    if not inter:
        return 0.0
    # Require at least one meaningful token overlap (len>=3) to avoid
    # weak matches like single letters / initials.
    if not any(len(t) >= 3 for t in inter):
        return 0.0
    return len(inter) / max(len(ta | tb), 1)


def match_cast_portrait(
    attributed_to: str,
    speakers: Sequence[Dict[str, Any]],
    cast_dir: Path,
) -> Optional[Tuple[Path, str]]:
    """Return (portrait_path, display_name) for the attributed speaker.

    Never falls back to an unrelated cast member (e.g. the podcast host)
    when ``attributed_to`` does not match — that used to put the wrong face
    on AI thumbnails.
    """
    target = _normalize_name(attributed_to)
    if not target or not cast_dir.is_dir():
        return None

    candidates: List[Tuple[Path, str, float]] = []
    for sp in speakers:
        if not isinstance(sp, dict):
            continue
        name = str(sp.get("name") or sp.get("suggested_name") or "").strip()
        sid = str(sp.get("id") or "").strip().upper()
        path = cast_dir / f"{sid}.jpg"
        if not path.is_file():
            continue
        nkey = _normalize_name(name)
        score = _name_match_score(target, nkey)
        if score >= 0.5:
            candidates.append((path, name or sid, score))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[2], reverse=True)
    best = candidates[0]
    return best[0], best[1]


def _hook_for_thumb(hook: str, title: str, *, max_chars: int = 110) -> str:
    """Text painted on the thumbnail — prefer title, fall back to hook (always UPPERCASE)."""
    text = (title or hook or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ""
    if len(text) >= 2 and text[0] in "\"'“”" and text[-1] in "\"'“”":
        text = text[1:-1].strip()

    result = text
    if len(text) > max_chars:
        parts = re.split(r"(?<=[,:;.—–\-])\s+|\s+[—–\-]\s+", text)
        parts = [p.strip(" ,;:—–-") for p in parts if p and p.strip(" ,;:—–-")]
        if len(parts) >= 2:
            tail = parts[-1]
            if 12 <= len(tail) <= max_chars:
                head = parts[0]
                combo = f"{head} {tail}".strip()
                result = combo if len(combo) <= max_chars else tail
            else:
                result = None
        else:
            result = None
        if result is None:
            words = text.split()
            out: List[str] = []
            for w in words:
                candidate = (" ".join(out + [w])).strip()
                if len(candidate) > max_chars:
                    break
                out.append(w)
            result = " ".join(out) if out else text[: max_chars - 1].rstrip() + "…"

    return result.upper() if result else ""


def _size_for_aspect(aspect_ratio: str) -> str:
    """OpenAI GPT Image sizes: 1024x1024 | 1024x1536 | 1536x1024."""
    raw = (aspect_ratio or "9:16").strip()
    if raw in ("16:9", "1.777", "1.78"):
        return "1536x1024"
    if raw in ("1:1", "1.0"):
        return "1024x1024"
    return "1024x1536"


def _clip_context(title: str, hook_line: str, snippet: str, *, limit: int = 420) -> str:
    context = " ".join(
        part.strip()
        for part in (title, hook_line, (snippet or "").strip())
        if part and str(part).strip()
    )
    if len(context) > limit:
        context = context[:limit].rsplit(" ", 1)[0] + "…"
    return context


def _build_background_prompt(
    *,
    hook: str,
    title: str,
    attributed_to: str,
    language: str,
    aspect_ratio: str,
    snippet: str = "",
) -> str:
    """Scene-only prompt: AI must NOT invent people (cutout is composited later)."""
    hook_line = _hook_for_thumb(hook, title)
    context = _clip_context(title, hook_line, snippet)
    who = f" about {attributed_to}" if attributed_to else ""
    if context:
        bg = (
            f"Invent a rich thematic environment{who} that matches: \"{context}\". "
            "Environment, props, colors and mood that clearly echo the topic. "
        )
    else:
        bg = (
            f"Invent a bold cinematic environment{who} with depth and vivid color. "
        )
    return (
        "Create a viral YouTube thumbnail BACKGROUND plate only. "
        "High contrast, bold colors, cinematic lighting, click-worthy. "
        f"Composition for aspect ratio {aspect_ratio}. "
        f"{bg}"
        "CRITICAL: do NOT draw any people, faces, hands, silhouettes, mannequins, "
        "or human figures of any kind. Empty stage for a real photo cutout. "
        "Leave the LOWER third relatively clear and uncluttered (soft bokeh) "
        "so text can be overlaid later. "
        "Keep the UPPER / center area with depth but not busy edges. "
        "Do NOT draw any text, letters, words, captions, logos, watermarks, "
        "UI chrome, frames, borders, stickers, or speech bubbles."
    )


def _build_prompt(
    *,
    hook: str,
    title: str,
    attributed_to: str,
    cited_names: Sequence[str],
    language: str,
    aspect_ratio: str,
    snippet: str = "",
    hybrid: bool = True,
    paint_text: str = "",
) -> str:
    hook_line = _hook_for_thumb(hook, title)
    paint_line = _hook_for_thumb(paint_text, paint_text) if paint_text else hook_line
    people_bits = []
    if attributed_to:
        people_bits.append(f"main on-camera person: {attributed_to}")
    for name in cited_names:
        people_bits.append(f"cited public figure (use reference photo): {name}")
    people = "; ".join(people_bits) if people_bits else "expressive face from references"

    context = _clip_context(title, hook_line, snippet)
    if context:
        bg_note = (
            "BACKGROUND (critical): invent a rich, thematic scene that matches this clip context — "
            f"\"{context}\". Use environment, props, colors and mood that clearly echo the topic. "
            "Do NOT use a flat empty studio backdrop. Soft depth, cinematic lighting, "
            "but keep faces sharp and readable."
        )
    else:
        bg_note = (
            "BACKGROUND: bold cinematic environment with depth and color — not a flat studio wall. "
            "Pick a vibrant mood that supports a click-worthy thumbnail."
        )

    base = (
        "Create a viral YouTube thumbnail base image. High contrast, bold colors, cinematic lighting, "
        "click-worthy but not spammy. Preserve the identity of people from the reference photos "
        "(same face, skin tone, hair). "
        f"Composition for aspect ratio {aspect_ratio}. "
        f"Subjects: {people}. "
        "The FIRST reference photo is the primary on-camera person — that face MUST dominate "
        "the thumbnail. Do not invent a different person or swap in someone else from memory. "
        "Large expressive face(s) in the UPPER half / safe area, shallow depth of field. "
        f"{bg_note} "
    )

    if hybrid:
        # Text + frame are painted locally — keep the model focused on faces/scene.
        return (
            base
            + "COMPOSITION (critical): leave the LOWER third relatively clear and uncluttered "
            "(soft bokeh / simple background there) so text can be overlaid later. "
            "Do NOT draw any text, letters, words, captions, logos, watermarks, UI chrome, "
            "frames, borders, stickers, or speech bubbles. Faces and scene only."
        )

    lang = (language or "pt").strip().lower()
    lang_note = (
        "All on-image text MUST be in Brazilian Portuguese, with correct accents."
        if lang.startswith("pt")
        else f"All on-image text MUST be in language code '{lang}'."
    )
    exact = paint_line or title or "VIRAL"
    return (
        base
        + "FRAME / BORDER (critical): wrap the entire thumbnail content inside a thick rounded "
        "rectangle frame with generous corner radius. The border itself must be a vibrant "
        "multi-stop gradient. Soft outer glow + subtle inner highlight. Leave a small outer "
        "margin so the rounded border is fully visible. "
        "TYPOGRAPHY (critical): paint the TITLE as stylized viral thumbnail lettering — "
        "chunky display font, vivid multi-color fills from a neon palette, thick dark outline "
        "+ soft glow. ALL CAPS / CAIXA ALTA only — never mixed or lowercase. "
        "2–4 lines max, huge readable letters, centered in the mid/lower safe zone. "
        f"Render this COMPLETE title text EXACTLY, word-for-word, do not truncate, do not paraphrase, "
        f"do not drop the ending: \"{exact}\". "
        f"{lang_note} "
        "No watermarks, no logos, no UI chrome, no extra slogans besides that title."
    )


def _openai_client():
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "Pacote openai não instalado. Rode: pip install openai"
        ) from e
    return OpenAI(api_key=require_openai_key())


def _decode_image_result(result) -> bytes:
    data = getattr(result, "data", None) or []
    if not data:
        raise RuntimeError("OpenAI não retornou imagem")
    item = data[0]
    b64 = getattr(item, "b64_json", None)
    if b64:
        return base64.b64decode(b64)
    url = getattr(item, "url", None)
    if url:
        import requests

        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content
    raise RuntimeError("OpenAI image response sem b64_json/url")


def _to_jpeg(raw: bytes, dest: Path, *, max_side: int = 1920) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        scale = min(1.0, float(max_side) / max(w, h))
        if scale < 1.0:
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.LANCZOS,
            )
        img.save(dest, format="JPEG", quality=92, optimize=True)
    except Exception:
        if dest.suffix.lower() not in (".jpg", ".jpeg"):
            dest = dest.with_suffix(".jpg")
        dest.write_bytes(raw)
    return dest


def _resolve_display_font(size: int):
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/lato/Lato-Black.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Heavy.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=max(12, int(size)))
            except OSError:
                continue
    return ImageFont.load_default()


def _ass_to_rgb(ass: str, *, fallback: Tuple[int, int, int] = (255, 255, 0)) -> Tuple[int, int, int]:
    """ASS ``&HAABBGGRR`` → RGB tuple."""
    m = re.match(r"&H([0-9A-Fa-f]{8})\b", str(ass or "").strip())
    if not m:
        return fallback
    hexv = m.group(1)
    try:
        bb = int(hexv[2:4], 16)
        gg = int(hexv[4:6], 16)
        rr = int(hexv[6:8], 16)
    except ValueError:
        return fallback
    return (rr, gg, bb)


def _resolve_caption_font(font_name: str, size: int, *, bold: bool = True):
    """Map caption ``font_name`` to a truetype face (size in px)."""
    from PIL import ImageFont

    name = (font_name or "Arial Black").strip().casefold()
    size = max(12, int(size))
    by_name: Dict[str, List[str]] = {
        "arial black": [
            "/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ],
        "impact": [
            "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
            "/usr/share/fonts/truetype/lato/Lato-Black.ttf",
        ],
        "arial": [
            "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
            "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ],
        "dejavu sans": [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ],
        "helvetica": [
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ],
    }
    candidates = list(by_name.get(name) or [])
    candidates.extend(
        [
            "/usr/share/fonts/truetype/lato/Lato-Black.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    )
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _is_shorts_aspect(aspect_ratio: str) -> bool:
    raw = (aspect_ratio or "9:16").strip()
    return raw not in ("16:9", "1.777", "1.78", "1:1", "1.0")

def _lerp_rgb(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _gradient_color(t: float) -> Tuple[int, int, int]:
    palette = _thumbnail_palette()
    # Loop seamlessly for the border ring.
    stops = list(palette) + ([palette[0]] if palette else [(255, 40, 180)])
    if t <= 0:
        return stops[0]
    if t >= 1:
        return stops[-1]
    scaled = t * (len(stops) - 1)
    i = int(math.floor(scaled))
    frac = scaled - i
    return _lerp_rgb(stops[i], stops[min(i + 1, len(stops) - 1)], frac)


def _draw_gradient_rounded_border(
    img,
    *,
    thickness: int,
    radius: int,
) -> None:
    """Paint a full-bleed gradient frame with a rounded inner cutout.

    Outer edge is flush to the canvas (no scene peeking outside the border);
    only the inner window is rounded.
    """
    from PIL import Image, ImageDraw, ImageFilter

    w, h = img.size
    thickness = max(12, int(thickness))
    radius = max(16, int(radius))

    # Fast diagonal multi-stop gradient (blur softens banding on the ring)
    strip_h = 64
    strip = Image.new("RGB", (1, strip_h))
    spx = strip.load()
    for y in range(strip_h):
        spx[0, y] = _gradient_color(y / max(1, strip_h - 1))
    # Stretch + slight horizontal shear feel via resize to full canvas
    grad = strip.resize((w, h), Image.Resampling.BILINEAR)
    # Second pass: blend a horizontal gradient so corners get more variety
    hstrip = Image.new("RGB", (strip_h, 1))
    hpx = hstrip.load()
    for x in range(strip_h):
        hpx[x, 0] = _gradient_color((x / max(1, strip_h - 1) + 0.35) % 1.0)
    grad2 = hstrip.resize((w, h), Image.Resampling.BILINEAR)
    grad = Image.blend(grad, grad2, 0.55)

    # Full canvas outer fill; punch a rounded hole for the scene.
    mask = Image.new("L", (w, h), 255)
    md = ImageDraw.Draw(mask)
    inner_m = thickness
    inner_r = max(6, radius)
    md.rounded_rectangle(
        [inner_m, inner_m, w - 1 - inner_m, h - 1 - inner_m],
        radius=inner_r,
        fill=0,
    )

    ring = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ring.paste(grad.convert("RGBA"), (0, 0), mask)

    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gld = ImageDraw.Draw(glow)
    gld.rounded_rectangle(
        [inner_m + 1, inner_m + 1, w - 2 - inner_m, h - 2 - inner_m],
        radius=max(4, inner_r - 1),
        outline=(255, 255, 255, 60),
        width=2,
    )

    base = img.convert("RGBA")
    composed = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(1)))
    composed = Image.alpha_composite(composed, ring)
    img.paste(composed.convert("RGB"))


def _measure_line(draw, text: str, font, outline: int) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=outline)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _space_width(draw, font, outline: int) -> int:
    """Width of one space between stroked words (stroke inflates bare ``' '``)."""
    w_gap, _ = _measure_line(draw, "A A", font, outline)
    w_tight, _ = _measure_line(draw, "AA", font, outline)
    return max(1, int(w_gap - w_tight))


def _draw_line_words(
    draw,
    *,
    x: float,
    y: float,
    line: str,
    font,
    outline: int,
    stroke_rgb: Tuple[int, int, int],
    fill_rgb: Optional[Tuple[int, int, int]],
    palette: List[Tuple[int, int, int]],
    word_index: int,
) -> int:
    """Draw a line; in palette mode each word cycles a different fill.

    Returns the next global word index after this line.
    """
    words = [t for t in re.split(r"\s+", str(line or "").strip()) if t]
    if not words:
        return word_index
    if fill_rgb is not None:
        draw.text(
            (x, y),
            line,
            font=font,
            fill=fill_rgb,
            stroke_width=outline,
            stroke_fill=stroke_rgb,
        )
        return word_index + len(words)

    gap = _space_width(draw, font, outline)
    cursor = float(x)
    wi = word_index
    for j, word in enumerate(words):
        fill = palette[wi % len(palette)]
        draw.text(
            (cursor, y),
            word,
            font=font,
            fill=fill,
            stroke_width=outline,
            stroke_fill=stroke_rgb,
        )
        tw, _ = _measure_line(draw, word, font, outline)
        cursor += tw
        if j < len(words) - 1:
            cursor += gap
        wi += 1
    return wi


def _wrap_hook_to_width(
    draw,
    text: str,
    font,
    outline: int,
    max_width: int,
    max_words: int = 4,
) -> List[str]:
    tokens = [t for t in re.split(r"\s+", str(text or "").strip()) if t]
    if not tokens:
        return []
    max_w = max(1, int(max_words or 4))
    lines: List[str] = []
    i = 0
    while i < len(tokens):
        take = min(max_w, len(tokens) - i)
        while take > 0:
            candidate = " ".join(tokens[i : i + take])
            tw, _ = _measure_line(draw, candidate, font, outline)
            if tw <= max_width or take == 1:
                lines.append(candidate)
                i += take
                break
            take -= 1
        else:
            break
        if len(lines) >= 6:
            # Pack remaining into last line
            if i < len(tokens):
                lines[-1] = (lines[-1] + " " + " ".join(tokens[i:])).strip()
            break
    return lines


def apply_viral_thumbnail_overlay(
    src: Path,
    dest: Path,
    hook: str,
    *,
    title: str = "",
    caption_style: Optional[Dict[str, Any]] = None,
    aspect_ratio: str = "9:16",
    text_color_mode: str = "caption",
    margin_v: Optional[int] = None,
    font_size: Optional[int] = None,
    border_pct: Optional[float] = None,
) -> Path:
    """Burn gradient frame + title onto the speaker frame.

    When ``caption_style`` is set, the title uses the same graphic rules as
    karaoke captions (font, size, outline, words/line) for any aspect ratio.
    ``text_color_mode`` chooses caption primary colour vs border palette
    (palette mode: each word cycles a different colour).
    ``margin_v`` / ``font_size`` / ``border_pct`` override style defaults.
    """
    from PIL import Image, ImageDraw, ImageFilter

    use_caption = bool(caption_style)
    color_mode = (text_color_mode or "caption").strip().lower()
    use_palette_fill = color_mode == "palette"
    if use_caption:
        from .captions import resolve_style

        style = resolve_style(caption_style)
        raw = re.sub(r"\s+", " ", (hook or title or "").strip())
        if len(raw) >= 2 and raw[0] in "\"'“”" and raw[-1] in "\"'“”":
            raw = raw[1:-1].strip()
        hook_line = raw.upper() if style.get("uppercase", True) else raw
    else:
        style = None
        hook_line = _hook_for_thumb(hook, title)

    dest.parent.mkdir(parents=True, exist_ok=True)

    img = Image.open(src).convert("RGB")
    w, h = img.size

    pct = 3.8
    if border_pct is not None:
        try:
            pct = max(1.0, min(8.0, float(border_pct)))
        except (TypeError, ValueError):
            pct = 3.8
    thickness = max(4, int(round(min(w, h) * (pct / 100.0))))
    radius = max(16, int(round(min(w, h) * max(0.04, pct / 100.0 * 2.1))))
    _draw_gradient_rounded_border(img, thickness=thickness, radius=radius)

    if not hook_line:
        img.save(dest, format="JPEG", quality=92, optimize=True)
        return dest

    pad = thickness + max(8, int(w * 0.04))
    max_text_w = max(40, w - pad * 2)

    # Match ASS DESIGN_PLAY_RES (1080×1920) used by caption preview for both aspects.
    play_w = 1080.0
    play_h = 1920.0

    if use_caption and style is not None:
        scale = w / play_w
        scale_y = h / play_h
        style_font = float(style["font_size"])
        if font_size is not None:
            try:
                style_font = float(max(40, min(140, int(font_size))))
            except (TypeError, ValueError):
                pass
        base_font = max(16, int(round(style_font * scale)))
        outline = max(0, int(round(float(style["outline"]) * scale)))
        style_margin = float(style["margin_v"])
        if margin_v is not None:
            try:
                style_margin = float(max(40, min(720, int(margin_v))))
            except (TypeError, ValueError):
                pass
        margin_scaled = max(0, int(round(style_margin * scale_y)))
        # Keep text above the decorative border ring.
        text_bottom = max(thickness + 8, h - margin_scaled)
        text_top = thickness + max(8, int(h * 0.08))
        max_text_h = max(40, text_bottom - text_top)
        max_words = max(1, int(style.get("max_words_per_line") or 4))
        fill_rgb = (
            None
            if use_palette_fill
            else _ass_to_rgb(str(style.get("primary_colour") or ""), fallback=(255, 255, 0))
        )
        stroke_rgb = _ass_to_rgb(
            str(style.get("outline_colour") or ""), fallback=(10, 10, 18)
        )
        bold = bool(style.get("bold", True))
        font_name = str(style.get("font_name") or "Arial Black")

        def _font_at(sz: int):
            return _resolve_caption_font(font_name, sz, bold=bold)

    else:
        text_top = int(h * 0.52)
        text_bottom = h - (thickness + max(12, int(h * 0.04)))
        max_text_h = max(40, text_bottom - text_top)
        style_font = 100.0
        if font_size is not None:
            try:
                style_font = float(max(40, min(140, int(font_size))))
            except (TypeError, ValueError):
                pass
        base_font = max(28, int(round(style_font * (w / play_w))))
        if margin_v is not None:
            try:
                margin_scaled = max(
                    0,
                    int(round(float(max(40, min(720, int(margin_v)))) * (h / play_h))),
                )
                text_bottom = max(thickness + 8, h - margin_scaled)
                max_text_h = max(40, text_bottom - text_top)
            except (TypeError, ValueError):
                pass
        outline = max(4, int(base_font * 0.14))
        max_words = 4
        fill_rgb = None if use_palette_fill else (255, 230, 0)
        stroke_rgb = (10, 10, 18)
        _font_at = _resolve_display_font  # type: ignore[assignment]

    draw = ImageDraw.Draw(img)
    font_size = base_font
    outline_base = outline
    lines: List[str] = []
    heights: List[int] = []
    widths: List[int] = []
    line_gap = 6
    font = _font_at(font_size)

    for _ in range(20):
        font = _font_at(font_size)
        if use_caption:
            # Keep outline proportional to caption style (don't grow with retries).
            outline = outline_base
        else:
            outline = max(3, int(round(outline_base * (font_size / max(1, base_font)))))
        lines = _wrap_hook_to_width(
            draw, hook_line, font, outline, max_text_w, max_words=max_words
        )
        if not lines:
            break
        heights = []
        widths = []
        for line in lines:
            tw, th = _measure_line(draw, line, font, outline)
            widths.append(tw)
            heights.append(th)
        line_gap = max(4, int(font_size * 0.12))
        total_h = sum(heights) + line_gap * (len(lines) - 1)
        max_line_w = max(widths) if widths else 0
        if max_line_w <= max_text_w and total_h <= max_text_h:
            break
        font_size = max(16, int(font_size * 0.88))

    if lines:
        total_h = sum(heights) + line_gap * max(0, len(lines) - 1)
        y = text_bottom - total_h
        if y < text_top:
            y = text_top

        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sy = y + max(2, int(font_size * 0.06))
        for i, line in enumerate(lines):
            tw = widths[i]
            th = heights[i]
            x = max(pad, min((w - tw) / 2, w - pad - tw))
            sd.text(
                (x + 2, sy + 2),
                line,
                font=font,
                fill=(0, 0, 0, 140),
                stroke_width=outline + 2 if outline else 2,
                stroke_fill=(0, 0, 0, 160),
            )
            sy += th + line_gap
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(1, font_size // 28)))
        base = img.convert("RGBA")
        img = Image.alpha_composite(base, shadow).convert("RGB")
        draw = ImageDraw.Draw(img)

        palette = _thumbnail_palette() or [(255, 230, 0)]
        word_i = 0
        for i, line in enumerate(lines):
            tw = widths[i]
            th = heights[i]
            x = max(pad, min((w - tw) / 2, w - pad - tw))
            word_i = _draw_line_words(
                draw,
                x=x,
                y=y,
                line=line,
                font=font,
                outline=outline,
                stroke_rgb=stroke_rgb,
                fill_rgb=fill_rgb,
                palette=palette,
                word_index=word_i,
            )
            y += th + line_gap

    img.save(dest, format="JPEG", quality=92, optimize=True)
    return dest


def _is_billing_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    needles = (
        "billing_hard_limit",
        "billing_limit",
        "insufficient_quota",
        "credit_balance_exhausted",
        "you have no credits",
        "exceeded your current quota",
        "quota exceeded",
    )
    if any(n in msg for n in needles):
        return True
    if "429" in msg and ("credit" in msg or "quota" in msg or "billing" in msg):
        return True
    return False


class ImageBillingError(RuntimeError):
    """Provider account has no credits or hit a hard billing limit."""


def _friendly_billing_message(provider: str = "openai") -> str:
    if provider.lower().startswith("gemini") or provider.lower() == "google":
        return (
            "Créditos Gemini esgotados ou billing bloqueado. "
            "Confira https://aistudio.google.com/apikey e o billing do projeto Google Cloud."
        )
    return (
        "Créditos OpenAI esgotados (billing hard limit). "
        "Adicione créditos em https://platform.openai.com/settings/organization/billing/ "
        "ou use IMAGE_PROVIDER=gemini com GEMINI_API_KEY."
    )


def _gemini_aspect(aspect_ratio: str) -> str:
    raw = (aspect_ratio or "9:16").strip()
    allowed = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
    if raw in allowed:
        return raw
    if raw in ("1.0",):
        return "1:1"
    if raw in ("1.777", "1.78"):
        return "16:9"
    return "9:16"


def _call_gemini_image(
    *,
    prompt: str,
    aspect_ratio: str,
    refs: Sequence[Path],
    model: Optional[str] = None,
) -> bytes:
    """Generate/edit via Gemini Flash Image; returns raw image bytes."""
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "google-genai/Pillow necessários para IMAGE_PROVIDER=gemini. "
            "Rode: pip install -r requirements-local.txt"
        ) from e

    image_model = (model or _cfg.GEMINI_IMAGE_MODEL or "gemini-2.5-flash-image").strip()
    client = genai.Client(api_key=require_gemini_key())

    parts: List[Any] = [prompt]
    for p in list(refs)[:3]:
        try:
            parts.append(Image.open(p).convert("RGB"))
        except OSError as e:
            print(f"[thumb-ai] skip gemini ref {p}: {e}", flush=True)

    aspect = _gemini_aspect(aspect_ratio)
    print(
        f"[thumb-ai] gemini model={image_model} aspect={aspect} refs={len(parts) - 1}",
        flush=True,
    )

    try:
        config = types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect),
        )
        response = client.models.generate_content(
            model=image_model,
            contents=parts,
            config=config,
        )
    except Exception as exc:
        if _is_billing_error(exc):
            raise ImageBillingError(_friendly_billing_message("gemini")) from exc
        try:
            response = client.models.generate_content(
                model=image_model,
                contents=parts,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                ),
            )
        except Exception as exc2:
            if _is_billing_error(exc2):
                raise ImageBillingError(_friendly_billing_message("gemini")) from exc2
            raise

    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        parts_out = getattr(content, "parts", None) or []
        for part in parts_out:
            inline = getattr(part, "inline_data", None)
            if inline is None:
                continue
            data = getattr(inline, "data", None)
            if data:
                if isinstance(data, str):
                    return base64.b64decode(data)
                return bytes(data)
    raise RuntimeError("Gemini não retornou imagem")


def _call_openai_image(
    *,
    client,
    model: str,
    prompt: str,
    size: str,
    quality: str,
    fidelity: str,
    refs: Sequence[Path],
):
    """images.edit when refs exist; otherwise images.generate. Cost-aware defaults."""
    q = (quality or "medium").strip().lower()
    if q not in ("low", "medium", "high"):
        q = "medium"
    fid = (fidelity or "low").strip().lower()

    handles = []
    try:
        if refs:
            for p in refs:
                handles.append(open(p, "rb"))
            kwargs: Dict[str, Any] = {
                "model": model,
                "image": handles if len(handles) > 1 else handles[0],
                "prompt": prompt,
                "size": size,
                "n": 1,
                "quality": q,
            }
            use_fidelity = fid in ("low", "high")
            try:
                if use_fidelity:
                    return client.images.edit(**kwargs, input_fidelity=fid)
                return client.images.edit(**kwargs)
            except TypeError:
                kwargs.pop("quality", None)
                try:
                    if use_fidelity:
                        return client.images.edit(**kwargs, input_fidelity=fid)
                    return client.images.edit(**kwargs)
                except TypeError:
                    return client.images.edit(
                        model=model,
                        image=kwargs["image"],
                        prompt=prompt,
                        size=size,
                        n=1,
                    )
            except Exception as first:
                if _is_billing_error(first):
                    raise ImageBillingError(_friendly_billing_message("openai")) from first
                msg = str(first).lower()
                if "input_fidelity" in msg or "quality" in msg:
                    retry = {
                        "model": model,
                        "image": kwargs["image"],
                        "prompt": prompt,
                        "size": size,
                        "n": 1,
                    }
                    if "quality" not in msg:
                        retry["quality"] = q
                    try:
                        return client.images.edit(**retry)
                    except Exception as second:
                        if _is_billing_error(second):
                            raise ImageBillingError(
                                _friendly_billing_message("openai")
                            ) from second
                        print(
                            f"[thumb-ai] edit failed ({second}); falling back to generate",
                            flush=True,
                        )
                else:
                    print(
                        f"[thumb-ai] edit failed ({first}); falling back to generate",
                        flush=True,
                    )
                try:
                    return client.images.generate(
                        model=model,
                        prompt=prompt,
                        size=size,
                        n=1,
                        quality=q,
                    )
                except Exception as gen_exc:
                    if _is_billing_error(gen_exc):
                        raise ImageBillingError(
                            _friendly_billing_message("openai")
                        ) from gen_exc
                    raise

        try:
            return client.images.generate(
                model=model,
                prompt=prompt,
                size=size,
                n=1,
                quality=q,
            )
        except TypeError:
            return client.images.generate(
                model=model,
                prompt=prompt,
                size=size,
                n=1,
            )
        except Exception as gen_exc:
            if _is_billing_error(gen_exc):
                raise ImageBillingError(_friendly_billing_message("openai")) from gen_exc
            raise
    finally:
        for fh in handles:
            try:
                fh.close()
            except Exception:
                pass


def _has_gemini_key() -> bool:
    key = (_cfg.GEMINI_API_KEY or "").strip().strip("'\"")
    if not key:
        return False
    placeholders = {
        "your_gemini_key_here",
        "changeme",
        "xxx",
        "your_api_key_here",
    }
    return key.casefold() not in placeholders


def _resolve_image_providers() -> List[str]:
    pref = (_cfg.IMAGE_PROVIDER or "auto").strip().lower()
    if pref == "openai":
        return ["openai"]
    if pref == "gemini":
        return ["gemini"]
    order = ["openai"]
    if _has_gemini_key():
        order.append("gemini")
    return order


def _cited_people_llm():
    """Prefer Gemini for cited-people extract when image provider is gemini/auto+gemini."""
    from .local.llm import call_gemini_llm, call_openai_llm, call_local_llm

    pref = (_cfg.IMAGE_PROVIDER or "auto").strip().lower()
    if pref == "gemini" and _has_gemini_key():
        return call_gemini_llm
    if pref == "auto" and _has_gemini_key():
        def _try(prompt: str) -> str:
            try:
                return call_gemini_llm(prompt)
            except Exception:
                return call_openai_llm(prompt)

        return _try
    return call_local_llm


def _fit_frame_to_aspect(
    src: Path,
    dest: Path,
    *,
    aspect_ratio: str = "9:16",
) -> Path:
    """Cover-crop ``src`` into the thumbnail canvas size; keep original pixels (no BG remove)."""
    from PIL import Image

    size = _size_for_aspect(aspect_ratio)
    tw, th = (int(x) for x in size.split("x", 1))
    dest.parent.mkdir(parents=True, exist_ok=True)

    img = Image.open(src).convert("RGB")
    sw, sh = img.size
    if sw < 1 or sh < 1:
        raise RuntimeError(f"Frame inválido: {src}")

    target_ar = tw / float(th)
    src_ar = sw / float(sh)
    if src_ar > target_ar:
        new_w = max(1, int(round(sh * target_ar)))
        left = max(0, (sw - new_w) // 2)
        img = img.crop((left, 0, left + new_w, sh))
    elif src_ar < target_ar:
        new_h = max(1, int(round(sw / target_ar)))
        # Bias toward the top so faces stay in frame
        top = max(0, int((sh - new_h) * 0.12))
        if top + new_h > sh:
            top = max(0, sh - new_h)
        img = img.crop((0, top, sw, top + new_h))

    img = img.resize((tw, th), Image.Resampling.LANCZOS)
    img.save(dest, format="JPEG", quality=92, optimize=True)
    return dest


def generate_ai_thumbnail(
    *,
    dest: Path,
    hook: str = "",
    title: str = "",
    virality_reason: str = "",
    attributed_to: str = "",
    snippet: str = "",
    speakers: Optional[Sequence[Dict[str, Any]]] = None,
    cast_dir: Optional[Path] = None,
    wiki_dir: Optional[Path] = None,
    reference_frame: Optional[Path] = None,
    person_frame: Optional[Path] = None,
    aspect_ratio: str = "9:16",
    language: str = "pt",
    model: Optional[str] = None,
    quality: Optional[str] = None,
    fidelity: Optional[str] = None,
    hybrid: Optional[bool] = None,
    mode: Optional[str] = None,
    overlay_text: Optional[str] = None,
    caption_style: Optional[Dict[str, Any]] = None,
    text_color_mode: Optional[str] = None,
    margin_v: Optional[int] = None,
    font_size: Optional[int] = None,
    border_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Generate a viral thumbnail and write JPEG to ``dest``.

    Uses the speaker frame as-is (original background kept) + local PIL
    overlay (gradient border + short text). No image-model background and
    no background removal.

    If ``overlay_text`` is provided (user edit), it is burned as-is and the
    overlay LLM is skipped. When ``caption_style`` is set, it drives the
    title look (font/size/outline) to match karaoke captions. Colour mode,
    vertical margin, font size and border thickness can be overridden.
    """
    del model, quality, fidelity, hybrid, wiki_dir  # unused — local frame path only

    speakers = list(speakers or [])
    use_caption_look = bool(caption_style)
    color_mode = (
        "palette"
        if (text_color_mode or "").strip().lower() == "palette"
        else "caption"
    )
    margin_override = None
    if margin_v is not None:
        try:
            margin_override = max(40, min(720, int(margin_v)))
        except (TypeError, ValueError):
            margin_override = None
    font_override = None
    if font_size is not None:
        try:
            font_override = max(40, min(140, int(font_size)))
        except (TypeError, ValueError):
            font_override = None
    border_override = None
    if border_pct is not None:
        try:
            border_override = max(1.0, min(8.0, float(border_pct)))
        except (TypeError, ValueError):
            border_override = None

    user_overlay = re.sub(r"\s+", " ", (overlay_text or "").strip())
    overlay_from_user = bool(user_overlay)
    if overlay_from_user:
        # Keep casing; apply_viral_thumbnail_overlay applies uppercase from caption style.
        hook_line = user_overlay if use_caption_look else _hook_for_thumb(user_overlay, user_overlay)
        overlay_raw = ""
        print(f"[thumb-ai] texto overlay (usuário): {hook_line!r}", flush=True)
    else:
        print("[thumb-ai] gerando texto curto da thumbnail (LLM)…", flush=True)
        overlay_raw = generate_thumbnail_overlay_text(
            title=title,
            hook=hook,
            reason=virality_reason,
            snippet=snippet,
            language=language,
            llm_fn=_cited_people_llm(),
        )
        if overlay_raw:
            hook_line = (
                re.sub(r"\s+", " ", overlay_raw.strip())
                if use_caption_look
                else _hook_for_thumb(overlay_raw, overlay_raw)
            )
            print(f"[thumb-ai] texto overlay (IA): {hook_line!r}", flush=True)
        else:
            hook_line = (
                re.sub(r"\s+", " ", (title or hook or "").strip())
                if use_caption_look
                else _hook_for_thumb(hook, title)
            )
            print(
                f"[thumb-ai] texto overlay (fallback título/hook): {hook_line!r}",
                flush=True,
            )

    thumb_mode = (mode or getattr(_cfg, "THUMBNAIL_MODE", "cutout") or "cutout").strip().lower()
    if thumb_mode not in ("cutout", "ai", "frame"):
        thumb_mode = "cutout"

    person_src: Optional[Path] = None
    person_src_origin = ""
    if person_frame and Path(person_frame).is_file():
        person_src = Path(person_frame)
        person_src_origin = "person_frame"
    elif cast_dir:
        matched = match_cast_portrait(attributed_to, speakers, cast_dir)
        if matched:
            person_src = matched[0]
            person_src_origin = "cast_portrait"
    if person_src is None and reference_frame and Path(reference_frame).is_file():
        person_src = Path(reference_frame)
        person_src_origin = "reference_frame"

    if person_src is None:
        raise RuntimeError(
            "Sem frame do locutor para montar a thumbnail "
            "(selecione um frame ou garanta referência do vídeo)."
        )

    face_meta: List[Dict[str, str]] = [
        {
            "kind": "frame",
            "name": attributed_to or "person",
            "path": str(person_src),
        }
    ]
    size = _size_for_aspect(aspect_ratio)
    print(
        f"[thumb-ai] mode=frame (pedido={thumb_mode}) "
        f"person_src={person_src.name} ({person_src_origin}) "
        f"size={size} — fundo original, sem IA de imagem"
        + (" · texto=estilo legenda" if use_caption_look else ""),
        flush=True,
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas = dest.with_suffix(".frame.jpg")
    try:
        _fit_frame_to_aspect(person_src, canvas, aspect_ratio=aspect_ratio)
        print(
            f"[thumb-ai] overlay local (borda + texto) sobre frame {person_src.name}",
            flush=True,
        )
        apply_viral_thumbnail_overlay(
            canvas,
            dest,
            hook_line,
            title=hook_line,
            caption_style=caption_style if use_caption_look else None,
            aspect_ratio=aspect_ratio,
            text_color_mode=color_mode,
            margin_v=margin_override,
            font_size=font_override,
            border_pct=border_override,
        )
    finally:
        try:
            if canvas.exists() and canvas != dest:
                canvas.unlink(missing_ok=True)
        except OSError:
            pass

    print(
        f"[thumb-ai] pronto mode=frame person={person_src.name} "
        f"overlay={hook_line!r}",
        flush=True,
    )

    return {
        "path": str(dest),
        "provider": "local",
        "model": "frame+overlay",
        "size": size,
        "quality": None,
        "fidelity": None,
        "hybrid": True,
        "mode": "frame",
        "mode_requested": thumb_mode,
        "hook": hook_line,
        "hook_burned": bool(hook_line),
        "overlay_text": hook_line,
        "overlay_from_llm": bool(overlay_raw) and not overlay_from_user,
        "overlay_from_user": overlay_from_user,
        "caption_style_applied": use_caption_look,
        "text_color_mode": color_mode,
        "margin_v": margin_override,
        "font_size": font_override,
        "border_pct": border_override,
        "faces": face_meta,
        "cited_people": [],
        "wiki_hits": [],
        "wiki_skip_reason": "frame mode — sem Wikipedia / sem modelo de imagem",
        "refs_sent_to_ai": [],
        "refs_sent_count": 0,
        "person_src": str(person_src),
        "person_src_origin": person_src_origin or None,
    }
