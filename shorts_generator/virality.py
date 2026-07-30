"""Per-channel virality profile — shapes highlight ranking prompts + hook timing."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# Canonical signal ids (order = default priority when profile is empty).
VIRALITY_SIGNALS: List[Dict[str, str]] = [
    {
        "id": "hook",
        "label": "Hook moments",
        "desc": "statements that create immediate curiosity",
    },
    {
        "id": "emotional",
        "label": "Emotional peaks",
        "desc": "surprise, laughter, anger, vulnerability, excitement",
    },
    {
        "id": "opinion",
        "label": "Opinion bombs",
        "desc": "polarizing or counter-intuitive takes",
    },
    {
        "id": "revelation",
        "label": "Revelation moments",
        "desc": "surprising facts, stats, or confessions",
    },
    {
        "id": "conflict",
        "label": "Conflict / tension",
        "desc": "disagreement, pushback, confrontation",
    },
    {
        "id": "quotable",
        "label": "Quotable one-liners",
        "desc": "standalone quote-card lines",
    },
    {
        "id": "story",
        "label": "Story peaks",
        "desc": "climax or twist of an anecdote",
    },
    {
        "id": "practical",
        "label": "Practical value",
        "desc": "concrete tip or actionable insight",
    },
]

_SIGNAL_BY_ID = {s["id"]: s for s in VIRALITY_SIGNALS}
_SIGNAL_IDS = {s["id"] for s in VIRALITY_SIGNALS}

DEFAULT_HOOK_IN_FIRST_SECONDS = 2.5


def default_virality_profile() -> Dict[str, Any]:
    return {
        "niche": "",
        "hook_in_first_seconds": DEFAULT_HOOK_IN_FIRST_SECONDS,
        "prefer": [],
        "deprioritize": [],
        "forbidden_openings": [],
        "custom_rules": "",
        "few_shot_hooks": [],
    }


def _clean_str_list(value: Any, *, max_items: int = 24, max_len: int = 200) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [ln.strip() for ln in value.splitlines()]
    elif isinstance(value, Sequence):
        items = [str(x).strip() for x in value]
    else:
        return []
    out: List[str] = []
    seen = set()
    for item in items:
        if not item:
            continue
        clipped = item[:max_len]
        key = clipped.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(clipped)
        if len(out) >= max_items:
            break
    return out


def _clean_signal_ids(value: Any) -> List[str]:
    raw = _clean_str_list(value, max_items=len(_SIGNAL_IDS), max_len=40)
    out: List[str] = []
    for item in raw:
        sid = item.strip().lower().replace(" ", "_")
        aliases = {
            "hooks": "hook",
            "hook_moments": "hook",
            "emotional_peaks": "emotional",
            "opinion_bombs": "opinion",
            "revelation_moments": "revelation",
            "conflict_tension": "conflict",
            "quotable_one_liners": "quotable",
            "story_peaks": "story",
            "practical_value": "practical",
        }
        sid = aliases.get(sid, sid)
        if sid in _SIGNAL_IDS and sid not in out:
            out.append(sid)
    return out


def normalize_virality_profile(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce arbitrary JSON into a safe virality profile."""
    base = default_virality_profile()
    if not isinstance(raw, dict):
        return base

    niche = str(raw.get("niche") or "").strip()[:200]
    base["niche"] = niche

    try:
        hook_s = float(raw.get("hook_in_first_seconds", DEFAULT_HOOK_IN_FIRST_SECONDS))
    except (TypeError, ValueError):
        hook_s = DEFAULT_HOOK_IN_FIRST_SECONDS
    base["hook_in_first_seconds"] = max(1.0, min(5.0, hook_s))

    prefer = _clean_signal_ids(raw.get("prefer"))
    deprioritize = _clean_signal_ids(raw.get("deprioritize"))
    # Prefer wins if listed in both.
    deprioritize = [s for s in deprioritize if s not in prefer]
    base["prefer"] = prefer
    base["deprioritize"] = deprioritize

    base["forbidden_openings"] = _clean_str_list(
        raw.get("forbidden_openings"), max_items=20, max_len=80
    )
    base["custom_rules"] = str(raw.get("custom_rules") or "").strip()[:2000]
    base["few_shot_hooks"] = _clean_str_list(
        raw.get("few_shot_hooks"), max_items=12, max_len=160
    )
    return base


def build_virality_criteria(profile: Optional[Dict[str, Any]] = None) -> str:
    """Ranked signal list for the system prompt (profile can reorder / drop)."""
    p = normalize_virality_profile(profile)
    prefer = p["prefer"]
    deprioritize = set(p["deprioritize"])

    ordered: List[Dict[str, str]] = []
    for sid in prefer:
        if sid in _SIGNAL_BY_ID and sid not in deprioritize:
            ordered.append(_SIGNAL_BY_ID[sid])
    for sig in VIRALITY_SIGNALS:
        if sig["id"] in deprioritize:
            continue
        if any(s["id"] == sig["id"] for s in ordered):
            continue
        ordered.append(sig)

    if not ordered:
        ordered = list(VIRALITY_SIGNALS)

    lines = ["Virality signals to prioritize (ranked by impact for THIS channel):"]
    for i, sig in enumerate(ordered, 1):
        lines.append(f"{i}. {sig['label'].upper()} — {sig['desc']}")
    if deprioritize:
        labels = [
            _SIGNAL_BY_ID[s]["label"] for s in p["deprioritize"] if s in _SIGNAL_BY_ID
        ]
        if labels:
            lines.append(
                "Deprioritize (only use if nothing stronger exists): "
                + ", ".join(labels)
            )
    return "\n".join(lines)


def build_profile_prompt_block(profile: Optional[Dict[str, Any]] = None) -> str:
    """Extra instructions injected into the highlight system prompt."""
    p = normalize_virality_profile(profile)
    hook_s = p["hook_in_first_seconds"]
    lines = [
        "CHANNEL / EDITOR PROFILE (hard constraints — override generic defaults):",
        f"- The spoken hook_sentence MUST begin within the first {hook_s:.1f} seconds "
        "of the clip (hook_start_time - start_time ≤ "
        f"{hook_s:.1f}). Do NOT put setup, throat-clearing, or soft context before the hook.",
        "- Expand context AFTER the hook (payoff), not before. Minimal lead-in silence is OK.",
        "- start_time should land just before the hook; end_time covers claim + payoff.",
        "- Always include hook_start_time: the exact transcript timestamp where hook_sentence begins.",
    ]
    if p["niche"]:
        lines.append(f"- Channel niche / tone: {p['niche']}")
    if p["forbidden_openings"]:
        bad = "; ".join(f'"{x}"' for x in p["forbidden_openings"])
        lines.append(
            f"- NEVER open a clip with these (or close paraphrases): {bad}"
        )
    if p["custom_rules"]:
        lines.append(f"- Extra editor rules:\n{p['custom_rules']}")
    if p["few_shot_hooks"]:
        lines.append("- Hooks that work for this channel (match this energy):")
        for h in p["few_shot_hooks"]:
            lines.append(f'  • "{h}"')
    return "\n".join(lines)


def profile_is_customized(profile: Optional[Dict[str, Any]] = None) -> bool:
    p = normalize_virality_profile(profile)
    d = default_virality_profile()
    return (
        bool(p["niche"])
        or p["prefer"] != d["prefer"]
        or p["deprioritize"] != d["deprioritize"]
        or bool(p["forbidden_openings"])
        or bool(p["custom_rules"])
        or bool(p["few_shot_hooks"])
        or abs(p["hook_in_first_seconds"] - DEFAULT_HOOK_IN_FIRST_SECONDS) > 0.05
    )
