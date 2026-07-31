"""Face-frame helpers for thumbnails (candidate ranking; cutout utils kept unused)."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Soft upper-body crop relative to Haar face box (x, y, w, h)
_FACE_PAD_X = 1.35
_FACE_PAD_UP = 0.85
_FACE_PAD_DOWN = 2.6


def _load_cv2():
    try:
        import cv2  # type: ignore
    except ImportError:
        return None
    return cv2


def _load_haar(cv2):
    if cv2 is None or not hasattr(cv2, "CascadeClassifier"):
        return None
    path = getattr(getattr(cv2, "data", None), "haarcascades", "") + "haarcascade_frontalface_default.xml"
    if not path or not Path(path).exists():
        return None
    cascade = cv2.CascadeClassifier(path)
    return None if cascade.empty() else cascade


def detect_faces_bgr(img) -> List[Tuple[int, int, int, int]]:
    cv2 = _load_cv2()
    cascade = _load_haar(cv2)
    if cv2 is None or cascade is None or img is None:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    raw = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=4, minSize=(40, 40)
    )
    faces = [(int(x), int(y), int(w), int(h)) for x, y, w, h in raw]
    faces.sort(key=lambda f: f[2] * f[3], reverse=True)
    return faces


def laplacian_sharpness(img_bgr) -> float:
    cv2 = _load_cv2()
    if cv2 is None or img_bgr is None:
        return 0.0
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def score_face_frame(img_bgr) -> Tuple[float, Optional[Tuple[int, int, int, int]]]:
    """Higher is better. Returns (score, best_face_box)."""
    if img_bgr is None:
        return 0.0, None
    h, w = img_bgr.shape[:2]
    faces = detect_faces_bgr(img_bgr)
    if not faces:
        return 0.0, None
    fx, fy, fw, fh = faces[0]
    area_ratio = (fw * fh) / float(max(1, w * h))
    # Prefer mid-sized faces (not tiny, not filling the whole frame)
    area_score = 1.0 - abs(area_ratio - 0.08) / 0.08
    area_score = max(0.0, min(1.0, area_score))
    # Prefer faces in upper 60%
    cy = fy + fh / 2.0
    pos_score = 1.0 if cy < h * 0.62 else max(0.0, 1.0 - (cy - h * 0.62) / (h * 0.4))
    sharp = laplacian_sharpness(img_bgr)
    sharp_score = min(1.0, sharp / 350.0)
    score = area_score * 0.45 + pos_score * 0.25 + sharp_score * 0.30
    # Boost absolute face size a bit
    score += min(0.15, area_ratio * 1.5)
    return float(score), (fx, fy, fw, fh)


def candidate_timestamps(
    start: float,
    end: float,
    *,
    transcript: Optional[Dict[str, Any]] = None,
    speaker_id: Optional[str] = None,
    attributed_to: str = "",
    speakers: Optional[Sequence[Dict[str, Any]]] = None,
    max_samples: int = 18,
) -> List[float]:
    """Prefer labeled speech turns for the attributed speaker, else even samples."""
    start = max(0.0, float(start))
    end = max(start + 0.5, float(end))
    times: List[float] = []

    def _push(t: object) -> None:
        try:
            val = float(t)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if val != val or val < start - 0.2 or val > end + 0.2:
            return
        times.append(round(min(end, max(start, val)), 2))

    sid = (speaker_id or "").strip().upper()
    if not sid and attributed_to and speakers:
        target = " ".join(str(attributed_to).lower().split())
        for sp in speakers:
            if not isinstance(sp, dict):
                continue
            name = str(sp.get("name") or sp.get("suggested_name") or "").strip().lower()
            if name and (name in target or target in name):
                sid = str(sp.get("id") or "").strip().upper()
                break

    segments = list((transcript or {}).get("segments") or []) if transcript else []
    if sid and segments:
        for seg in segments:
            if str(seg.get("speaker_id") or "").strip().upper() != sid:
                continue
            try:
                s = float(seg.get("start", 0))
                e = float(seg.get("end", s))
            except (TypeError, ValueError):
                continue
            if e < start or s > end:
                continue
            _push(s + min(0.4, max(0.0, (e - s) * 0.3)))
            _push((s + e) / 2.0)

    # Even grid across the clip
    span = end - start
    n = max(6, min(max_samples, int(span / 2.5) + 4))
    for i in range(n):
        _push(start + (span * (i + 0.5) / n))

    # Always include early hook zone
    _push(start + min(1.2, span * 0.12))
    _push(start + min(2.5, span * 0.25))

    unique: List[float] = []
    for t in times:
        if any(abs(t - u) < 0.55 for u in unique):
            continue
        unique.append(t)
        if len(unique) >= max_samples:
            break
    return unique or [start]


def upper_body_crop_box(
    face: Tuple[int, int, int, int],
    frame_w: int,
    frame_h: int,
) -> Tuple[int, int, int, int]:
    fx, fy, fw, fh = face
    cx = fx + fw / 2.0
    pad_x = fw * _FACE_PAD_X
    top = fy - fh * _FACE_PAD_UP
    bottom = fy + fh * _FACE_PAD_DOWN
    left = cx - pad_x
    right = cx + pad_x
    x0 = max(0, int(math.floor(left)))
    y0 = max(0, int(math.floor(top)))
    x1 = min(frame_w, int(math.ceil(right)))
    y1 = min(frame_h, int(math.ceil(bottom)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 0, 0, frame_w, frame_h
    return x0, y0, x1, y1


def _soft_alpha_mask(w: int, h: int, face_in_crop: Tuple[int, int, int, int]):
    """RGBA alpha: keep head/shoulders, feather edges."""
    from PIL import Image, ImageDraw, ImageFilter

    fx, fy, fw, fh = face_in_crop
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    # Head ellipse
    head = [
        fx - fw * 0.25,
        fy - fh * 0.35,
        fx + fw * 1.25,
        fy + fh * 1.15,
    ]
    draw.ellipse(head, fill=255)
    # Torso / shoulders (rounded rectangle-ish ellipse)
    torso = [
        fx - fw * 0.9,
        fy + fh * 0.55,
        fx + fw * 1.9,
        h + fh * 0.2,
    ]
    draw.ellipse(torso, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(4, int(min(w, h) * 0.035))))
    return mask


def _rembg_model_ready() -> bool:
    """True when u2net weights are already on disk (skip multi‑MB first download)."""
    import os

    home = Path.home()
    candidates = [
        home / ".u2net" / "u2net.onnx",
        home / ".u2net" / "u2netp.onnx",
    ]
    u2_home = (os.environ.get("U2NET_HOME") or "").strip()
    if u2_home:
        candidates.append(Path(u2_home) / "u2net.onnx")
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 1_000_000:
                return True
        except OSError:
            continue
    return False


def remove_background_rgba(src: Path, dest: Path) -> Path:
    """Write an RGBA PNG cutout of the person.

    Uses rembg only when the ONNX model is already cached — otherwise Haar
    soft-mask (avoids a 176 MB download that hung the thumbnail request).
    """
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(src).convert("RGB")

    if _rembg_model_ready():
        try:
            from rembg import remove  # type: ignore

            cut = remove(img)
            if cut.mode != "RGBA":
                cut = cut.convert("RGBA")
            cut.save(dest, format="PNG")
            return dest
        except Exception as exc:
            print(f"[cutout] rembg falhou ({exc}); usando máscara Haar", flush=True)
    else:
        print(
            "[cutout] rembg pulado (modelo u2net ainda não baixado) — máscara Haar",
            flush=True,
        )

    # Fallback: Haar face → soft upper-body alpha
    cv2 = _load_cv2()
    if cv2 is None:
        rgba = img.convert("RGBA")
        rgba.save(dest, format="PNG")
        return dest

    bgr = cv2.imread(str(src))
    score, face = score_face_frame(bgr)
    if face is None or score <= 0:
        rgba = img.convert("RGBA")
        # Mild vignette so flat paste looks less boxy
        w, h = rgba.size
        mask = Image.new("L", (w, h), 0)
        from PIL import ImageDraw, ImageFilter

        draw = ImageDraw.Draw(mask)
        draw.ellipse((-w * 0.05, -h * 0.1, w * 1.05, h * 1.05), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(radius=max(8, w // 20)))
        rgba.putalpha(mask)
        rgba.save(dest, format="PNG")
        return dest

    x0, y0, x1, y1 = upper_body_crop_box(face, bgr.shape[1], bgr.shape[0])
    crop = img.crop((x0, y0, x1, y1))
    fx, fy, fw, fh = face
    face_in = (fx - x0, fy - y0, fw, fh)
    alpha = _soft_alpha_mask(crop.width, crop.height, face_in)
    rgba = crop.convert("RGBA")
    rgba.putalpha(alpha)
    rgba.save(dest, format="PNG")
    return dest


def _trim_transparent(rgba, *, pad: int = 2):
    """Crop to the opaque bbox so transparent padding does not kill zoom."""
    if rgba.mode != "RGBA":
        rgba = rgba.convert("RGBA")
    alpha = rgba.split()[3]
    bbox = alpha.getbbox()
    if not bbox:
        return rgba
    l, t, r, b = bbox
    l = max(0, l - pad)
    t = max(0, t - pad)
    r = min(rgba.width, r + pad)
    b = min(rgba.height, b + pad)
    return rgba.crop((l, t, r, b))


def _boost_person(rgba):
    """Lift contrast/brightness so the cutout does not look washed out."""
    from PIL import ImageEnhance

    rgb = rgba.convert("RGB")
    alpha = rgba.split()[3]
    rgb = ImageEnhance.Contrast(rgb).enhance(1.18)
    rgb = ImageEnhance.Brightness(rgb).enhance(1.08)
    rgb = ImageEnhance.Color(rgb).enhance(1.12)
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def _dilate_alpha(alpha, radius: int):
    """Expand an L-mode alpha mask by ``radius`` px (cv2 preferred, else MaxFilter)."""
    from PIL import Image, ImageFilter

    radius = max(1, int(radius))
    cv2 = _load_cv2()
    if cv2 is not None:
        import numpy as np

        arr = np.array(alpha)
        k = radius * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        dilated = cv2.dilate(arr, kernel, iterations=1)
        return Image.fromarray(dilated, mode="L")

    # Fallback: iterative MaxFilter (Pillow caps useful neighborhood size)
    out = alpha
    remaining = radius
    while remaining > 0:
        step = min(24, remaining)
        k = step * 2 + 1
        if k % 2 == 0:
            k += 1
        out = out.filter(ImageFilter.MaxFilter(size=k))
        remaining -= step
    return out


def _outline_cutout(
    rgba,
    *,
    thickness: int = 10,
    colors: Optional[Sequence[Tuple[int, int, int]]] = None,
):
    """Draw a thick multi-color stroke around the silhouette (viral border)."""
    from PIL import Image, ImageFilter

    if rgba.mode != "RGBA":
        rgba = rgba.convert("RGBA")
    w, h = rgba.size
    alpha = rgba.split()[3]
    # Room for the stroke outside the silhouette — dilate on padded canvas
    # so the border is not clipped at the person bbox.
    pad = max(thickness + 4, 10)
    canvas_w, canvas_h = w + pad * 2, h + pad * 2
    alpha_pad = Image.new("L", (canvas_w, canvas_h), 0)
    alpha_pad.paste(alpha, (pad, pad))

    palette = list(colors) if colors else [
        (255, 255, 255),
        (255, 220, 40),
        (255, 40, 180),
    ]
    # Outer ring → inner ring (color → bright white edge)
    rings: List[Tuple[int, Tuple[int, int, int]]] = []
    if thickness >= 8:
        rings.append((thickness, palette[min(2, len(palette) - 1)]))
        rings.append((max(3, thickness * 2 // 3), palette[min(1, len(palette) - 1)]))
        rings.append((max(2, thickness // 3), (255, 255, 255)))
    else:
        rings.append((thickness, (255, 255, 255)))

    base = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    for rad, (cr, cg, cb) in rings:
        dilated = _dilate_alpha(alpha_pad, rad)
        dilated = dilated.filter(
            ImageFilter.GaussianBlur(radius=max(0.5, rad * 0.08))
        )
        ring = Image.new("RGBA", (canvas_w, canvas_h), (cr, cg, cb, 0))
        ring.putalpha(dilated)
        base = Image.alpha_composite(base, ring)

    # Soft drop shadow behind everything
    shadow_alpha = alpha_pad.point(lambda a: int(a * 0.45))
    shadow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    shadow.putalpha(shadow_alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(8, w // 22)))
    base.alpha_composite(
        shadow, (max(2, w // 70), max(4, h // 35))
    )

    # Person on top
    base.paste(rgba, (pad, pad), rgba)
    return base


def compose_person_on_background(
    background: Path,
    cutout: Path,
    dest: Path,
    *,
    person_height_ratio: float = 0.98,
    anchor_y_ratio: float = 0.0,
    face_zoom: float = 1.22,
    outline_thickness: Optional[int] = None,
) -> Path:
    """Paste RGBA cutout onto RGB/JPEG background (upper half), write JPEG.

    Zooms the real person large, trims transparent padding, and adds a
    thick silhouette border so they pop against the AI scene.
    """
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    bg = Image.open(background).convert("RGBA")
    person = Image.open(cutout).convert("RGBA")
    bw, bh = bg.size

    person = _trim_transparent(person)
    person = _boost_person(person)

    # Extra face-centric zoom: crop a bit of lower torso / sides if tall
    if face_zoom and face_zoom > 1.0:
        zw, zh = person.size
        keep_h = max(32, int(zh / face_zoom))
        keep_w = max(32, int(zw / max(1.0, face_zoom * 0.92)))
        # Bias crop toward the top (head)
        left = max(0, (zw - keep_w) // 2)
        top = max(0, int((zh - keep_h) * 0.08))
        person = person.crop((left, top, left + keep_w, top + keep_h))
        person = _trim_transparent(person)

    # Target size: nearly full thumbnail height (text overlays lower band)
    target_h = max(32, int(bh * person_height_ratio))
    scale = target_h / float(max(1, person.height))
    target_w = max(32, int(person.width * scale))
    max_w = int(bw * 0.98)
    if target_w > max_w:
        scale = max_w / float(person.width)
        target_w = max_w
        target_h = max(32, int(person.height * scale))
    person = person.resize((target_w, target_h), Image.Resampling.LANCZOS)

    thick = outline_thickness
    if thick is None:
        thick = max(10, int(min(target_w, target_h) * 0.028))
    try:
        from .config import parse_thumbnail_palette

        border_colors = parse_thumbnail_palette()
    except Exception:
        border_colors = None
    person = _outline_cutout(person, thickness=thick, colors=border_colors)
    target_w, target_h = person.size

    x = (bw - target_w) // 2
    y = max(0, int(bh * anchor_y_ratio))
    # Allow slight overflow at top for aggressive zoom; keep feet above text
    max_y = int(bh * 0.04)
    y = min(y, max_y)
    # If the cutout is taller than the canvas, pin to top (crop bottom into text)
    if target_h > bh:
        y = 0

    layer = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    layer.paste(person, (x, y), person)
    out = Image.alpha_composite(bg, layer).convert("RGB")
    out.save(dest, format="JPEG", quality=92, optimize=True)
    return dest


def build_face_candidates(
    *,
    ffmpeg_src: str,
    extract_frame_fn,
    out_dir: Path,
    start: float,
    end: float,
    transcript: Optional[Dict[str, Any]] = None,
    attributed_to: str = "",
    speakers: Optional[Sequence[Dict[str, Any]]] = None,
    cast_portrait: Optional[Path] = None,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    """Extract and rank face frames; write JPEGs under ``out_dir``.

    ``extract_frame_fn(source, timestamp, dest) -> bool``
    """
    cv2 = _load_cv2()
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clear previous candidates for this highlight
    for old in out_dir.glob("*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass
    for old in out_dir.glob("*.json"):
        try:
            old.unlink()
        except OSError:
            pass

    scored: List[Dict[str, Any]] = []
    times = candidate_timestamps(
        start,
        end,
        transcript=transcript,
        attributed_to=attributed_to,
        speakers=speakers,
    )

    for i, ts in enumerate(times):
        dest = out_dir / f"raw_{i:02d}.jpg"
        ok = False
        try:
            ok = bool(extract_frame_fn(ffmpeg_src, float(ts), dest))
        except Exception:
            ok = False
        if not ok or not dest.exists():
            continue
        face_box = None
        score = 0.0
        if cv2 is not None:
            img = cv2.imread(str(dest))
            score, face_box = score_face_frame(img)
        if score < 0.12:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        scored.append(
            {
                "time": round(float(ts), 2),
                "score": round(float(score), 4),
                "path": dest,
                "face": face_box,
                "source": "video",
            }
        )

    scored.sort(key=lambda c: c["score"], reverse=True)

    # Dedupe visually similar timestamps (keep higher score)
    picked: List[Dict[str, Any]] = []
    for item in scored:
        if any(abs(item["time"] - p["time"]) < 1.4 for p in picked):
            continue
        picked.append(item)
        if len(picked) >= max(1, limit - (1 if cast_portrait and cast_portrait.exists() else 0)):
            break

    results: List[Dict[str, Any]] = []
    # Optional cast portrait first (user-approved face)
    if cast_portrait is not None and cast_portrait.exists():
        cast_dest = out_dir / "cand_00_cast.jpg"
        try:
            from PIL import Image

            Image.open(cast_portrait).convert("RGB").save(
                cast_dest, format="JPEG", quality=90
            )
            results.append(
                {
                    "id": "cast",
                    "time": None,
                    "score": 1.0,
                    "source": "cast",
                    "label": "Foto do locutor",
                    "path": cast_dest,
                }
            )
        except Exception:
            pass

    for i, item in enumerate(picked):
        cid = f"t{i}"
        final = out_dir / f"cand_{len(results):02d}_{cid}.jpg"
        try:
            item["path"].replace(final)
        except Exception:
            try:
                from shutil import copy2

                copy2(item["path"], final)
                item["path"].unlink(missing_ok=True)
            except Exception:
                continue
        results.append(
            {
                "id": cid,
                "time": item["time"],
                "score": item["score"],
                "source": "video",
                "label": f"{item['time']:.1f}s",
                "path": final,
            }
        )

    for raw in out_dir.glob("raw_*.jpg"):
        try:
            raw.unlink(missing_ok=True)
        except OSError:
            pass

    # Manifest for stable id → path lookup
    try:
        import json

        manifest = [
            {
                "id": r["id"],
                "file": Path(r["path"]).name,
                "time": r.get("time"),
                "source": r.get("source"),
                "label": r.get("label"),
                "score": r.get("score"),
            }
            for r in results
        ]
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

    return results
