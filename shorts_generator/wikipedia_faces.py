"""Fetch portrait images for people cited in a clip (Wikipedia / Wikidata)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

_UA = "AI-Youtube-Shorts-Generator/1.0 (thumbnail; contact=local)"
_TIMEOUT = 20

_SKIP_TITLE_RE = re.compile(
    r"(?i)\b("
    r"lista\b|list of\b|filme\b|film\b|álbum\b|album\b|canção\b|song\b|"
    r"série\b|series\b|episódio\b|episode\b|partido\b|party\b|"
    r"desambiguação\b|disambiguation\b|categoria\b|category\b"
    r")\b"
)


def _slug(name: str) -> str:
    raw = re.sub(r"[^\w\-]+", "_", (name or "").strip(), flags=re.UNICODE)
    return (raw.strip("_") or "person")[:80]


def _api_get(lang: str, params: Dict) -> Dict:
    url = f"https://{lang}.wikipedia.org/w/api.php"
    resp = requests.get(
        url,
        params=params,
        headers={"User-Agent": _UA},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _wikidata_get(params: Dict) -> Dict:
    resp = requests.get(
        "https://www.wikidata.org/w/api.php",
        params=params,
        headers={"User-Agent": _UA},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _title_looks_like_person_page(title: str) -> bool:
    return not bool(_SKIP_TITLE_RE.search(title or ""))


def _search_titles(name: str, lang: str, limit: int = 8) -> List[str]:
    data = _api_get(
        lang,
        {
            "action": "query",
            "list": "search",
            "srsearch": name,
            "srlimit": limit,
            "srprop": "snippet",
            "format": "json",
        },
    )
    hits = (data.get("query") or {}).get("search") or []
    out: List[str] = []
    for hit in hits:
        title = str(hit.get("title") or "").strip()
        if title and _title_looks_like_person_page(title):
            out.append(title)
    return out


def _page_image_and_wikibase(title: str, lang: str) -> Tuple[Optional[str], Optional[str]]:
    data = _api_get(
        lang,
        {
            "action": "query",
            "titles": title,
            "prop": "pageimages|pageprops",
            "piprop": "original|thumbnail",
            "pithumbsize": 800,
            "format": "json",
            "redirects": 1,
        },
    )
    pages = (data.get("query") or {}).get("pages") or {}
    for page in pages.values():
        if not isinstance(page, dict) or page.get("missing") is not None:
            continue
        original = page.get("original") or {}
        thumb = page.get("thumbnail") or {}
        url = (original.get("source") or thumb.get("source") or "").strip() or None
        props = page.get("pageprops") or {}
        qid = (props.get("wikibase_item") or "").strip() or None
        return url, qid
    return None, None


def _wikidata_is_human(qid: str) -> Optional[bool]:
    """True if P31 includes human (Q5). None if unknown."""
    if not qid:
        return None
    try:
        data = _wikidata_get(
            {
                "action": "wbgetentities",
                "ids": qid,
                "props": "claims",
                "format": "json",
            }
        )
        entity = ((data.get("entities") or {}).get(qid)) or {}
        claims = entity.get("claims") or {}
        instances = claims.get("P31") or []
        for claim in instances:
            mainsnak = (claim or {}).get("mainsnak") or {}
            datavalue = mainsnak.get("datavalue") or {}
            value = datavalue.get("value") or {}
            if value.get("id") == "Q5":
                return True
        if instances:
            return False
    except Exception as e:
        print(f"[wiki] wikidata P31 check failed for {qid}: {e}", flush=True)
    return None


def _wikidata_image_url(qid: str) -> Optional[str]:
    """Commons file from Wikidata P18."""
    if not qid:
        return None
    try:
        data = _wikidata_get(
            {
                "action": "wbgetentities",
                "ids": qid,
                "props": "claims",
                "format": "json",
            }
        )
        entity = ((data.get("entities") or {}).get(qid)) or {}
        claims = (entity.get("claims") or {}).get("P18") or []
        if not claims:
            return None
        mainsnak = (claims[0] or {}).get("mainsnak") or {}
        filename = ((mainsnak.get("datavalue") or {}).get("value") or "").strip()
        if not filename:
            return None
        # Resolve commons file URL
        commons = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "titles": f"File:{filename}",
                "prop": "imageinfo",
                "iiprop": "url",
                "format": "json",
            },
            headers={"User-Agent": _UA},
            timeout=_TIMEOUT,
        )
        commons.raise_for_status()
        pages = (commons.json().get("query") or {}).get("pages") or {}
        for page in pages.values():
            infos = page.get("imageinfo") or []
            if infos and infos[0].get("url"):
                return str(infos[0]["url"])
    except Exception as e:
        print(f"[wiki] wikidata P18 failed for {qid}: {e}", flush=True)
    return None


def _wikidata_search_human(name: str, language: str) -> Optional[Dict[str, str]]:
    """Find a human entity via Wikidata search, then grab P18 image."""
    try:
        data = _wikidata_get(
            {
                "action": "wbsearchentities",
                "search": name,
                "language": language[:2] or "pt",
                "uselang": language[:2] or "pt",
                "type": "item",
                "limit": 6,
                "format": "json",
            }
        )
    except Exception as e:
        print(f"[wiki] wikidata search failed: {e}", flush=True)
        return None

    for hit in data.get("search") or []:
        qid = str(hit.get("id") or "").strip()
        if not qid:
            continue
        if _wikidata_is_human(qid) is False:
            continue
        # Prefer likely humans; if P31 unknown, still try image
        image_url = _wikidata_image_url(qid)
        if not image_url:
            continue
        label = str(hit.get("label") or name).strip()
        return {
            "name": name,
            "title": label,
            "page_url": f"https://www.wikidata.org/wiki/{qid}",
            "image_url": image_url,
            "lang": "wikidata",
            "qid": qid,
        }
    return None


def resolve_wikipedia_image_url(
    name: str,
    *,
    language: str = "pt",
    hint: str = "",
) -> Optional[Dict[str, str]]:
    """Return {name, title, page_url, image_url, lang} or None."""
    base_name = (name or "").strip()
    if len(base_name) < 2:
        return None

    langs = []
    primary = (language or "pt").strip().lower()[:2] or "pt"
    for lang in (primary, "en", "pt"):
        if lang and lang not in langs:
            langs.append(lang)

    queries = [base_name]
    hint = (hint or "").strip()
    if hint:
        queries.append(f"{base_name} {hint}")

    for lang in langs:
        for query in queries:
            try:
                for title in _search_titles(query, lang):
                    image_url, qid = _page_image_and_wikibase(title, lang)
                    human = _wikidata_is_human(qid) if qid else None
                    if human is False:
                        continue
                    if not image_url and qid:
                        image_url = _wikidata_image_url(qid)
                    if not image_url:
                        continue
                    page_url = (
                        f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
                    )
                    return {
                        "name": base_name,
                        "title": title,
                        "page_url": page_url,
                        "image_url": image_url,
                        "lang": lang,
                        "qid": qid or "",
                    }
            except Exception as e:
                print(f"[wiki] lookup failed for {base_name!r} ({lang}): {e}", flush=True)
                continue

    # Last resort: Wikidata human search
    for lang in langs:
        hit = _wikidata_search_human(
            f"{base_name} {hint}".strip() if hint else base_name,
            lang,
        )
        if hit:
            return hit
        if hint:
            hit = _wikidata_search_human(base_name, lang)
            if hit:
                return hit
    return None


def download_image(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        resp.raise_for_status()
        if not resp.content or len(resp.content) < 200:
            return False
        # Normalize to JPEG so OpenAI image edit always gets a supported still
        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            # Prefer a portrait crop around center if the wiki image is very wide
            w, h = img.size
            if w > h * 1.35:
                side = h
                x0 = max(0, (w - side) // 2)
                img = img.crop((x0, 0, x0 + side, h))
            max_side = 1024
            scale = min(1.0, float(max_side) / max(img.size))
            if scale < 1.0:
                img = img.resize(
                    (max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale))),
                    Image.Resampling.LANCZOS,
                )
            dest = dest.with_suffix(".jpg")
            img.save(dest, format="JPEG", quality=92)
        except Exception:
            dest.write_bytes(resp.content)
        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        print(f"[wiki] download failed: {e}", flush=True)
        return False


def fetch_cited_portraits(
    names: List[Dict[str, str]],
    dest_dir: Path,
    *,
    language: str = "pt",
) -> List[Dict[str, str]]:
    """Download Wikipedia portraits for cited people.

    ``names`` items: {name, hint?}
    Returns list of {name, path, page_url, title}.
    """
    out: List[Dict[str, str]] = []
    seen = set()
    for item in names:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        meta = resolve_wikipedia_image_url(
            name,
            language=language,
            hint=str(item.get("hint") or "").strip(),
        )
        if not meta:
            print(f"[wiki] sem página/imagem para {name!r}", flush=True)
            continue
        dest = dest_dir / f"{_slug(name)}.jpg"
        if dest.exists() and dest.stat().st_size > 0:
            print(
                f"[wiki] cache hit: {name!r} → {dest.name} ({meta.get('page_url')})",
                flush=True,
            )
            out.append(
                {
                    "name": name,
                    "path": str(dest),
                    "page_url": meta["page_url"],
                    "title": meta["title"],
                }
            )
            continue
        print(
            f"[wiki] download: {name!r} ← {meta.get('image_url')}",
            flush=True,
        )
        if download_image(meta["image_url"], dest):
            out.append(
                {
                    "name": name,
                    "path": str(dest),
                    "page_url": meta["page_url"],
                    "title": meta["title"],
                }
            )
            print(
                f"[wiki] salvo: {name!r} → {dest.name} ({meta.get('page_url')})",
                flush=True,
            )
        else:
            print(f"[wiki] download falhou: {name!r}", flush=True)
    return out
