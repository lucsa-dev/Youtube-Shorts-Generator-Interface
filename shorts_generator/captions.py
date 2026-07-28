"""Karaoke burn-in captions via ASS + ffmpeg libass.

Builds word-timed ASS from a Whisper transcript (with optional word timestamps)
and burns them into a local mp4. Themes map to ASS Style fields.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# ASS colour: &HAABBGGRR (alpha, blue, green, red) — alpha 00 = opaque.
THEMES: Dict[str, Dict[str, Any]] = {
    "bold-white": {
        "label": "Branco bold",
        "font_name": "Arial Black",
        "font_size": 72,
        "primary_colour": "&H0000FFFF",  # yellow highlight (karaoke fill)
        "secondary_colour": "&H00FFFFFF",  # white waiting
        "outline_colour": "&H00000000",
        "back_colour": "&H80000000",
        "bold": True,
        "outline": 4,
        "shadow": 0,
        "margin_v": 160,
        "max_words_per_line": 4,
    },
    "yellow-pop": {
        "label": "Amarelo pop",
        "font_name": "Impact",
        "font_size": 78,
        "primary_colour": "&H0000FFFF",
        "secondary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
        "back_colour": "&H80000000",
        "bold": True,
        "outline": 5,
        "shadow": 2,
        "margin_v": 180,
        "max_words_per_line": 3,
    },
    "neon-mint": {
        "label": "Verde neon",
        "font_name": "Arial Black",
        "font_size": 70,
        "primary_colour": "&H00B0FF60",
        "secondary_colour": "&H00FFFFFF",
        "outline_colour": "&H00202020",
        "back_colour": "&H90000000",
        "bold": True,
        "outline": 4,
        "shadow": 0,
        "margin_v": 160,
        "max_words_per_line": 4,
    },
    "minimal": {
        "label": "Minimal",
        "font_name": "Arial",
        "font_size": 58,
        "primary_colour": "&H00FFFFFF",
        "secondary_colour": "&H00CCCCCC",
        "outline_colour": "&H00000000",
        "back_colour": "&H60000000",
        "bold": False,
        "outline": 2,
        "shadow": 0,
        "margin_v": 140,
        "max_words_per_line": 5,
    },
}

_STYLE_KEYS = (
    "font_name",
    "font_size",
    "primary_colour",
    "secondary_colour",
    "outline_colour",
    "back_colour",
    "bold",
    "outline",
    "shadow",
    "margin_v",
    "max_words_per_line",
    "enabled",
    "uppercase",
)

# Design canvas used by the web preview (frameW / 1080). Keep ASS PlayRes here
# so libass scales font/outline/margin to the actual clip resolution.
DESIGN_PLAY_RES = (1080, 1920)


def list_themes() -> List[Dict[str, Any]]:
    return [
        {"id": tid, "label": t.get("label", tid), **{k: t[k] for k in t if k != "label"}}
        for tid, t in THEMES.items()
    ]


def resolve_style(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge theme preset + user overrides into a concrete style dict."""
    raw = dict(raw or {})
    theme_id = str(raw.get("theme") or "bold-white")
    base = dict(THEMES.get(theme_id) or THEMES["bold-white"])
    base.pop("label", None)
    style: Dict[str, Any] = {
        "theme": theme_id,
        "enabled": True,
        "uppercase": True,  # match web preview text-transform: uppercase
        **base,
    }
    for key in _STYLE_KEYS:
        if key in raw and raw[key] is not None and raw[key] != "":
            style[key] = raw[key]
    style["font_size"] = int(style["font_size"])
    style["outline"] = int(style["outline"])
    style["shadow"] = int(style["shadow"])
    style["margin_v"] = int(style["margin_v"])
    style["max_words_per_line"] = max(1, int(style["max_words_per_line"]))
    style["bold"] = bool(style["bold"])
    style["enabled"] = bool(style.get("enabled", True))
    style["uppercase"] = bool(style.get("uppercase", True))
    return style


def _ass_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    if s >= 60:
        m += 1
        s = 0
    if m >= 60:
        h += 1
        m = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", " ")
    )


def words_from_transcript(
    transcript: Optional[Dict[str, Any]],
    start_time: float,
    end_time: float,
) -> List[Dict[str, float | str]]:
    """Extract words overlapping [start_time, end_time], rebased to clip t=0."""
    if not transcript:
        return []

    words: List[Dict[str, float | str]] = []
    # Prefer flat word list if present
    flat = transcript.get("words")
    if isinstance(flat, list) and flat:
        for w in flat:
            try:
                ws = float(w["start"])
                we = float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            text = str(w.get("word") or w.get("text") or "").strip()
            if not text or we <= start_time or ws >= end_time:
                continue
            words.append(
                {
                    "start": max(0.0, ws - start_time),
                    "end": max(0.0, min(we, end_time) - start_time),
                    "word": text,
                }
            )
        return _sanitize_words(words)

    for seg in transcript.get("segments") or []:
        seg_words = seg.get("words")
        if seg_words:
            for w in seg_words:
                try:
                    ws = float(w["start"])
                    we = float(w["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                text = str(w.get("word") or w.get("text") or "").strip()
                if not text or we <= start_time or ws >= end_time:
                    continue
                words.append(
                    {
                        "start": max(0.0, ws - start_time),
                        "end": max(0.0, min(we, end_time) - start_time),
                        "word": text,
                    }
                )
            continue

        # Fallback: spread segment text evenly across its duration
        try:
            ss = float(seg["start"])
            se = float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if se <= start_time or ss >= end_time:
            continue
        text = str(seg.get("text") or "").strip()
        tokens = [t for t in re.split(r"\s+", text) if t]
        if not tokens:
            continue
        clip_s = max(ss, start_time)
        clip_e = min(se, end_time)
        dur = max(0.05, clip_e - clip_s)
        step = dur / len(tokens)
        for i, tok in enumerate(tokens):
            ws = clip_s + i * step
            we = clip_s + (i + 1) * step
            words.append(
                {
                    "start": max(0.0, ws - start_time),
                    "end": max(0.0, we - start_time),
                    "word": tok,
                }
            )

    return _sanitize_words(words)


def _sanitize_words(words: Sequence[Dict[str, float | str]]) -> List[Dict[str, float | str]]:
    cleaned: List[Dict[str, float | str]] = []
    for w in words:
        start = float(w["start"])
        end = float(w["end"])
        if end <= start:
            end = start + 0.08
        cleaned.append({"start": start, "end": end, "word": str(w["word"])})
    cleaned.sort(key=lambda x: float(x["start"]))
    return cleaned


def _chunk_words(
    words: Sequence[Dict[str, float | str]], max_per_line: int
) -> List[List[Dict[str, float | str]]]:
    if not words:
        return []
    chunks: List[List[Dict[str, float | str]]] = []
    cur: List[Dict[str, float | str]] = []
    for w in words:
        cur.append(w)
        # Break on punctuation or max length
        token = str(w["word"])
        if len(cur) >= max_per_line or token.endswith((".", "!", "?", "…")):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(
    words: Sequence[Dict[str, float | str]],
    style: Dict[str, Any],
    *,
    play_res_x: int = DESIGN_PLAY_RES[0],
    play_res_y: int = DESIGN_PLAY_RES[1],
) -> str:
    """Build karaoke ASS: each line uses \\k tags (centiseconds).

    Style numeric fields are authored for DESIGN_PLAY_RES (1080×1920), matching
    the web preview. Pass that PlayRes so libass scales to the real clip size.
    """
    style = resolve_style(style)
    bold = -1 if style["bold"] else 0
    font = str(style["font_name"])
    max_w = int(style["max_words_per_line"])
    uppercase = bool(style.get("uppercase", True))
    # Preview sits ~10–12% from the bottom; keep margin in that band on design canvas.
    margin_v = int(style["margin_v"])
    if margin_v <= 0:
        margin_v = int(round(play_res_y * 0.12))

    header = f"""[Script Info]
Title: Shorts Lab Karaoke
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: {play_res_x}
PlayResY: {play_res_y}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{font},{int(style['font_size'])},{style['primary_colour']},{style['secondary_colour']},{style['outline_colour']},{style['back_colour']},{bold},0,0,0,100,100,0,0,1,{int(style['outline'])},{int(style['shadow'])},2,40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for chunk in _chunk_words(words, max_w):
        line_start = float(chunk[0]["start"])
        line_end = float(chunk[-1]["end"])
        if line_end <= line_start:
            line_end = line_start + 0.2
        parts: List[str] = []
        for w in chunk:
            dur_cs = max(1, int(round((float(w["end"]) - float(w["start"])) * 100)))
            token = str(w["word"])
            if uppercase:
                token = token.upper()
            parts.append(f"{{\\k{dur_cs}}}{_escape_ass_text(token)}")
        text = " ".join(parts)
        lines.append(
            f"Dialogue: 0,{_ass_ts(line_start)},{_ass_ts(line_end)},Karaoke,,0,0,0,,{text}\n"
        )
    return "".join(lines)


def burn_ass(video_path: str, ass_path: str, out_path: str) -> str:
    """Burn ASS into video with ffmpeg libass (re-encode video, copy audio)."""
    # Escape path for ffmpeg filtergraph (Windows-hostile chars)
    ass_esc = (
        Path(ass_path).resolve().as_posix()
        .replace("\\", "/")
        .replace(":", "\\:")
        .replace("'", "\\'")
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video_path,
        "-vf", f"ass='{ass_esc}'",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "copy",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def apply_karaoke_captions(
    video_path: str,
    transcript: Optional[Dict[str, Any]],
    start_time: float,
    end_time: float,
    style: Optional[Dict[str, Any]] = None,
    *,
    out_path: Optional[str] = None,
) -> str:
    """Burn karaoke captions onto a local clip. Returns final mp4 path."""
    style = resolve_style(style)
    if not style.get("enabled", True):
        return video_path

    words = words_from_transcript(transcript, start_time, end_time)
    if not words:
        print("[captions] no words in range — skipping burn-in", flush=True)
        return video_path

    # Always author ASS against the preview design canvas. Using the raw clip
    # resolution made font_size/outline look huge on 404×720 (etc.) crops.
    play_x, play_y = DESIGN_PLAY_RES
    ass_body = build_ass(words, style, play_res_x=play_x, play_res_y=play_y)

    final = out_path or video_path
    tmp_out = final + ".captioned.mp4"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ass", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(ass_body)
        ass_path = tf.name
    try:
        print(
            f"[captions] karaoke burn-in ({len(words)} words, theme={style.get('theme')})",
            flush=True,
        )
        burn_ass(video_path, ass_path, tmp_out)
        os.replace(tmp_out, final)
    finally:
        try:
            os.unlink(ass_path)
        except OSError:
            pass
        if os.path.exists(tmp_out):
            try:
                os.unlink(tmp_out)
            except OSError:
                pass
    return final
