"""AI thumbnail generation — cost-optimized (mini model + hybrid PIL overlay)."""
from __future__ import annotations

import base64
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import (
    OPENAI_IMAGE_FIDELITY,
    OPENAI_IMAGE_MODEL,
    OPENAI_IMAGE_QUALITY,
    THUMBNAIL_HYBRID,
    require_openai_key,
)

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

# Viral palette for PIL overlay (fill cycles per line)
_HOOK_COLORS = [
    (255, 230, 0),  # yellow
    (0, 229, 255),  # electric cyan
    (255, 64, 160),  # hot pink
    (180, 255, 40),  # lime
    (255, 140, 0),  # orange
]
_BORDER_STOPS = [
    (255, 40, 180),
    (255, 120, 40),
    (255, 220, 40),
    (40, 220, 255),
    (160, 60, 255),
    (255, 40, 180),
]


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


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def match_cast_portrait(
    attributed_to: str,
    speakers: Sequence[Dict[str, Any]],
    cast_dir: Path,
) -> Optional[Tuple[Path, str]]:
    """Return (portrait_path, display_name) for the attributed speaker."""
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
        if not name and not target:
            continue
        nkey = _normalize_name(name)
        score = 0.0
        if target and nkey:
            if target == nkey:
                score = 1.0
            elif target in nkey or nkey in target:
                score = 0.85
            else:
                ta = set(target.split())
                tb = set(nkey.split())
                if ta and tb and ta & tb:
                    score = len(ta & tb) / max(len(ta | tb), 1)
        if score >= 0.5:
            candidates.append((path, name or sid, score))

    if not candidates and speakers:
        for sp in speakers:
            if not isinstance(sp, dict):
                continue
            sid = str(sp.get("id") or "").strip().upper()
            path = cast_dir / f"{sid}.jpg"
            if path.is_file():
                name = str(sp.get("name") or sp.get("suggested_name") or sid).strip()
                return path, name

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[2], reverse=True)
    best = candidates[0]
    return best[0], best[1]


def _hook_for_thumb(hook: str, title: str, *, max_chars: int = 110) -> str:
    """Keep the full hook when it fits; never chop mid-phrase at a fixed word count."""
    text = (hook or title or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ""
    if len(text) >= 2 and text[0] in "\"'“”" and text[-1] in "\"'“”":
        text = text[1:-1].strip()
    if len(text) <= max_chars:
        return text

    parts = re.split(r"(?<=[,:;.—–\-])\s+|\s+[—–\-]\s+", text)
    parts = [p.strip(" ,;:—–-") for p in parts if p and p.strip(" ,;:—–-")]
    if len(parts) >= 2:
        tail = parts[-1]
        if 12 <= len(tail) <= max_chars:
            head = parts[0]
            combo = f"{head} {tail}".strip()
            if len(combo) <= max_chars:
                return combo
            return tail

    words = text.split()
    out: List[str] = []
    for w in words:
        candidate = (" ".join(out + [w])).strip()
        if len(candidate) > max_chars:
            break
        out.append(w)
    return " ".join(out) if out else text[: max_chars - 1].rstrip() + "…"


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
) -> str:
    hook_line = _hook_for_thumb(hook, title)
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
    exact = hook_line or title or "VIRAL"
    return (
        base
        + "FRAME / BORDER (critical): wrap the entire thumbnail content inside a thick rounded "
        "rectangle frame with generous corner radius. The border itself must be a vibrant "
        "multi-stop gradient. Soft outer glow + subtle inner highlight. Leave a small outer "
        "margin so the rounded border is fully visible. "
        "TYPOGRAPHY (critical): paint the hook as stylized viral thumbnail lettering — "
        "chunky display font, vivid multi-color fills, thick dark outline + soft glow. "
        "2–4 lines max, huge readable letters, centered in the mid/lower safe zone. "
        f"Render this COMPLETE hook text EXACTLY, word-for-word, do not truncate, do not paraphrase, "
        f"do not drop the ending: \"{exact}\". "
        f"{lang_note} "
        "No watermarks, no logos, no UI chrome, no extra slogans besides that hook."
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


def _lerp_rgb(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _gradient_color(t: float) -> Tuple[int, int, int]:
    stops = _BORDER_STOPS
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
    margin: int,
    thickness: int,
    radius: int,
) -> None:
    """Paint a thick multi-stop gradient ring as a rounded rectangle border."""
    from PIL import Image, ImageDraw, ImageFilter

    w, h = img.size
    margin = max(4, int(margin))
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

    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle(
        [margin, margin, w - 1 - margin, h - 1 - margin],
        radius=radius,
        fill=255,
    )
    inner_m = margin + thickness
    inner_r = max(6, radius - thickness // 2)
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
        [
            max(0, margin - 3),
            max(0, margin - 3),
            w - 1 - max(0, margin - 3),
            h - 1 - max(0, margin - 3),
        ],
        radius=radius + 3,
        outline=(255, 90, 200, 80),
        width=max(4, thickness // 2),
    )
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
) -> Path:
    """Burn gradient frame + stylized hook onto an AI scene (hybrid path)."""
    from PIL import Image, ImageDraw, ImageFilter

    hook_line = _hook_for_thumb(hook, title)
    dest.parent.mkdir(parents=True, exist_ok=True)

    img = Image.open(src).convert("RGB")
    w, h = img.size

    margin = max(10, int(round(min(w, h) * 0.028)))
    thickness = max(10, int(round(min(w, h) * 0.038)))
    radius = max(28, int(round(min(w, h) * 0.08)))
    _draw_gradient_rounded_border(
        img, margin=margin, thickness=thickness, radius=radius
    )

    if not hook_line:
        img.save(dest, format="JPEG", quality=92, optimize=True)
        return dest

    # Text lives inside the frame, mid/lower zone
    pad = margin + thickness + max(8, int(w * 0.04))
    max_text_w = max(40, w - pad * 2)
    # Keep faces clear: text starts around 52% height
    text_top = int(h * 0.52)
    text_bottom = h - (margin + thickness + max(12, int(h * 0.04)))
    max_text_h = max(40, text_bottom - text_top)

    draw = ImageDraw.Draw(img)
    base_font = max(28, int(w * 0.095))
    outline_base = max(4, int(base_font * 0.14))
    font_size = base_font
    outline = outline_base
    lines: List[str] = []
    heights: List[int] = []
    widths: List[int] = []
    line_gap = 6
    font = _resolve_display_font(font_size)

    for _ in range(20):
        font = _resolve_display_font(font_size)
        outline = max(3, int(round(outline_base * (font_size / max(1, base_font)))))
        lines = _wrap_hook_to_width(draw, hook_line, font, outline, max_text_w, max_words=4)
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
        # Prefer lower block but stay inside text_top..text_bottom
        y = text_bottom - total_h
        if y < text_top:
            y = text_top

        # Soft shadow layer
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
                stroke_width=outline + 2,
                stroke_fill=(0, 0, 0, 160),
            )
            sy += th + line_gap
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(1, font_size // 28)))
        base = img.convert("RGBA")
        img = Image.alpha_composite(base, shadow).convert("RGB")
        draw = ImageDraw.Draw(img)

        for i, line in enumerate(lines):
            tw = widths[i]
            th = heights[i]
            x = max(pad, min((w - tw) / 2, w - pad - tw))
            fill = _HOOK_COLORS[i % len(_HOOK_COLORS)]
            draw.text(
                (x, y),
                line,
                font=font,
                fill=fill,
                stroke_width=outline,
                stroke_fill=(10, 10, 18),
            )
            y += th + line_gap

    img.save(dest, format="JPEG", quality=92, optimize=True)
    return dest


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
                msg = str(first).lower()
                if "input_fidelity" in msg or "quality" in msg:
                    # Retry without the rejected param
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
                        print(
                            f"[thumb-ai] edit failed ({second}); falling back to generate",
                            flush=True,
                        )
                else:
                    print(
                        f"[thumb-ai] edit failed ({first}); falling back to generate",
                        flush=True,
                    )
                return client.images.generate(
                    model=model,
                    prompt=prompt,
                    size=size,
                    n=1,
                    quality=q,
                )

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
    finally:
        for fh in handles:
            try:
                fh.close()
            except Exception:
                pass


def generate_ai_thumbnail(
    *,
    dest: Path,
    hook: str = "",
    title: str = "",
    attributed_to: str = "",
    snippet: str = "",
    speakers: Optional[Sequence[Dict[str, Any]]] = None,
    cast_dir: Optional[Path] = None,
    wiki_dir: Optional[Path] = None,
    reference_frame: Optional[Path] = None,
    aspect_ratio: str = "9:16",
    language: str = "pt",
    model: Optional[str] = None,
    quality: Optional[str] = None,
    fidelity: Optional[str] = None,
    hybrid: Optional[bool] = None,
) -> Dict[str, Any]:
    """Generate a viral thumbnail and write JPEG to ``dest``.

    Default path is cost-optimized: ``gpt-image-1-mini`` + medium quality +
    hybrid mode (scene from the model, typography/frame via PIL).
    """
    speakers = list(speakers or [])
    speaker_names = [
        str(s.get("name") or s.get("suggested_name") or "").strip()
        for s in speakers
        if isinstance(s, dict)
    ]
    speaker_names = [n for n in speaker_names if n]

    face_paths: List[Path] = []
    face_meta: List[Dict[str, str]] = []

    if cast_dir:
        matched = match_cast_portrait(attributed_to, speakers, cast_dir)
        if matched:
            path, name = matched
            face_paths.append(path)
            face_meta.append({"kind": "on_camera", "name": name, "path": str(path)})

    cited = extract_cited_people(snippet, speaker_names)
    cited_names = [c["name"] for c in cited]
    wiki_hits: List[Dict[str, str]] = []
    if cited and wiki_dir is not None:
        from .wikipedia_faces import fetch_cited_portraits

        wiki_hits = fetch_cited_portraits(cited, wiki_dir, language=language)
        for hit in wiki_hits:
            p = Path(hit["path"])
            if p.is_file():
                face_paths.append(p)
                face_meta.append(
                    {
                        "kind": "cited",
                        "name": hit["name"],
                        "path": str(p),
                        "page_url": hit.get("page_url") or "",
                    }
                )

    refs: List[Path] = list(face_paths)
    if reference_frame and Path(reference_frame).is_file():
        refs.append(Path(reference_frame))

    uniq: List[Path] = []
    seen_paths = set()
    for p in refs:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        uniq.append(p)
    refs = uniq[:4]

    use_hybrid = THUMBNAIL_HYBRID if hybrid is None else bool(hybrid)
    prompt = _build_prompt(
        hook=hook,
        title=title,
        attributed_to=attributed_to,
        cited_names=cited_names or [h["name"] for h in wiki_hits],
        language=language,
        aspect_ratio=aspect_ratio,
        snippet=snippet,
        hybrid=use_hybrid,
    )
    size = _size_for_aspect(aspect_ratio)
    image_model = (model or OPENAI_IMAGE_MODEL or "gpt-image-1-mini").strip().strip("'\"")
    image_quality = (quality or OPENAI_IMAGE_QUALITY or "medium").strip().lower()
    image_fidelity = (fidelity or OPENAI_IMAGE_FIDELITY or "low").strip().lower()

    client = _openai_client()
    print(
        f"[thumb-ai] generating model={image_model} quality={image_quality} "
        f"fidelity={image_fidelity} hybrid={use_hybrid} size={size} refs={len(refs)} "
        f"cited={cited_names}",
        flush=True,
    )

    result = _call_openai_image(
        client=client,
        model=image_model,
        prompt=prompt,
        size=size,
        quality=image_quality,
        fidelity=image_fidelity,
        refs=refs,
    )

    raw = _decode_image_result(result)
    hook_line = _hook_for_thumb(hook, title)
    hook_burned = False

    if use_hybrid:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".scene.jpg")
        try:
            _to_jpeg(raw, tmp)
            apply_viral_thumbnail_overlay(tmp, dest, hook_line, title=title)
            hook_burned = bool(hook_line)
        finally:
            try:
                if tmp.exists() and tmp != dest:
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        _to_jpeg(raw, dest)

    return {
        "path": str(dest),
        "model": image_model,
        "size": size,
        "quality": image_quality,
        "fidelity": image_fidelity,
        "hybrid": use_hybrid,
        "hook": hook_line,
        "hook_burned": hook_burned,
        "faces": face_meta,
        "cited_people": cited,
    }
