"""Johnny.Decimal id assignment and the default taxonomy.

Johnny.Decimal: at most 10 areas (00-09, 10-19, ... 90-99), at most 10
categories per area, item ids ``CC.NN`` where CC is the two-digit category and
NN is a zero-padded item counter within that category.

The default taxonomy is the 10 areas from the design. Each area
spans a decade; its representative *category code* (CC) is the decade's first
slot (00, 10, 20, ...). A category *label* like ``11_llm-training`` already
carries its CC ("11"); for plain area labels we fall back to the area's base CC.

Public API:
    DEFAULT_TAXONOMY                 list of (decade_lo, decade_hi, slug, desc)
    area_for_code(cc)                -> (slug, desc) for a category code
    code_for_label(label)            -> "CC" for a category/area label
    folder_for_code(cc)              -> "10-19_ai-tech/11_llm-training"-style path
    next_id(cc, existing_ids)        -> next free "CC.NN"
    assign(label, existing_ids)      -> ("CC.NN", folder_path)
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# (decade_low, decade_high, area_slug, human_description)
DEFAULT_TAXONOMY: List[Tuple[int, int, str, str]] = [
    (0, 9, "inbox", "Inbox / Read-Later / Meta (unsorted, START-HERE)"),
    (10, 19, "ai-tech", "AI & Tech (models, tools, prompting, coding)"),
    (20, 29, "tools", "Tools & Resources (apps, templates, datasets, extensions)"),
    (30, 39, "business", "Business & Startups (strategy, fundraising, marketing, hiring)"),
    (40, 49, "money", "Money & Finance (investing, personal finance, crypto, markets)"),
    (50, 59, "productivity", "Productivity & Self-Improvement (workflow, habits, focus, notes)"),
    (60, 69, "health", "Health & Fitness (nutrition, training, sleep, mental health)"),
    (70, 79, "design", "Design & Creative (ui/ux, branding, typography, inspiration)"),
    (80, 89, "learning", "Learning & How-To (tutorials, explainers, courses, references)"),
    (90, 99, "archive", "Archive (decayed/done; keeps the active set clean)"),
]

# Keyword hints that map a free-text area name to a decade base CC. Used by the
# mock enricher and by code_for_label when only an area word is given.
_AREA_ALIASES = {
    "inbox": 0, "meta": 0, "unsorted": 0, "read-later": 0,
    "ai": 10, "ml": 10, "tech": 10, "llm": 10, "model": 10, "prompt": 10,
    "coding": 10, "code": 10, "ai-tech": 10, "ai-ml": 10,
    "tool": 20, "tools": 20, "app": 20, "resource": 20, "dataset": 20,
    "extension": 20, "template": 20,
    "business": 30, "startup": 30, "startups": 30, "marketing": 30,
    "fundraising": 30, "hiring": 30, "gtm": 30,
    "money": 40, "finance": 40, "investing": 40, "crypto": 40, "markets": 40,
    "productivity": 50, "habits": 50, "focus": 50, "workflow": 50,
    "note-taking": 50, "self-improvement": 50,
    "health": 60, "fitness": 60, "nutrition": 60, "sleep": 60, "training": 60,
    "design": 70, "ux": 70, "ui": 70, "branding": 70, "typography": 70,
    "creative": 70,
    "learning": 80, "tutorial": 80, "how-to": 80, "course": 80, "explainer": 80,
    "reference": 80,
    "archive": 90, "archived": 90, "done": 90, "decay": 90,
}

_CC_PREFIX_RE = re.compile(r"^\s*(\d{1,2})")


def _decade_base(decade_lo: int) -> int:
    return decade_lo


def area_for_code(cc: int) -> Tuple[str, str]:
    """Return (area_slug, description) for a two-digit category code."""
    for lo, hi, slug, desc in DEFAULT_TAXONOMY:
        if lo <= cc <= hi:
            return slug, desc
    return "inbox", DEFAULT_TAXONOMY[0][3]


def area_folder_name(cc: int) -> str:
    """e.g. cc=11 -> '10-19_ai-tech'."""
    for lo, hi, slug, _desc in DEFAULT_TAXONOMY:
        if lo <= cc <= hi:
            return "{:02d}-{:02d}_{}".format(lo, hi, slug)
    return "00-09_inbox"


def code_for_label(label: str) -> int:
    """Map a category/area label to its two-digit category code (CC).

    Accepts:
      * "11_llm-training"  -> 11 (explicit numeric prefix wins)
      * "11"               -> 11
      * "ai-tech" / "ai"   -> 10 (alias to the area base code)
      * unknown            -> 0  (Inbox)
    """
    if label is None:
        return 0
    label = str(label).strip()
    m = _CC_PREFIX_RE.match(label)
    if m:
        cc = int(m.group(1))
        if 0 <= cc <= 99:
            return cc
    key = label.lower().lstrip("#").strip()
    # try whole label, then its first token
    if key in _AREA_ALIASES:
        return _AREA_ALIASES[key]
    first = re.split(r"[\s/_\-]+", key)[0] if key else ""
    if first in _AREA_ALIASES:
        return _AREA_ALIASES[first]
    return 0


def _category_slug(label: str) -> str:
    """Extract the category slug portion of a label like '11_llm-training'
    -> 'llm-training'. If no slug, fall back to the area slug."""
    label = str(label).strip()
    m = re.match(r"^\d{1,2}[_\-](.+)$", label)
    if m:
        return _slugify(m.group(1))
    s = _slugify(label)
    if s:
        return s
    cc = code_for_label(label)
    return area_for_code(cc)[0]


def _slugify(text: str) -> str:
    text = str(text).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def folder_for_code(cc: int, label: str = "") -> str:
    """Return the relative folder path 'AREAFOLDER/CC_categoryslug'.

    e.g. cc=11, label='11_llm-training' -> '10-19_ai-tech/11_llm-training'.
    With no label the category slug defaults to the area slug.
    """
    area = area_folder_name(cc)
    slug = _category_slug(label) if label else area_for_code(cc)[0]
    return "{}/{:02d}_{}".format(area, cc, slug)


def _parse_ids(existing_ids) -> Dict[int, List[int]]:
    """Group existing 'CC.NN' ids by CC -> sorted list of NN ints."""
    by_cc: Dict[int, List[int]] = {}
    for raw in existing_ids or []:
        s = str(raw).strip()
        m = re.match(r"^(\d{1,2})\.(\d{1,3})$", s)
        if not m:
            continue
        cc = int(m.group(1))
        nn = int(m.group(2))
        by_cc.setdefault(cc, []).append(nn)
    for cc in by_cc:
        by_cc[cc].sort()
    return by_cc


def next_id(cc: int, existing_ids) -> str:
    """Allocate the next free 'CC.NN' for category ``cc``, given the ids already
    in use. NN starts at 01 and fills the first gap (so a deleted note's slot is
    reused only after compaction; by default we just take max+1 to stay stable).
    """
    by_cc = _parse_ids(existing_ids)
    used = by_cc.get(cc, [])
    nn = (used[-1] + 1) if used else 1
    return "{:02d}.{:02d}".format(cc, nn)


def assign(label: str, existing_ids) -> Tuple[str, str]:
    """Given a category label and the ids already in use, return
    (new_id 'CC.NN', folder_path). Convenience wrapper used by build_kb.py."""
    cc = code_for_label(label)
    new_id = next_id(cc, existing_ids)
    folder = folder_for_code(cc, label)
    return new_id, folder


def all_area_folders() -> List[str]:
    """List the ten area folder names (for scaffolding / READMEs)."""
    return ["{:02d}-{:02d}_{}".format(lo, hi, slug)
            for lo, hi, slug, _ in DEFAULT_TAXONOMY]


if __name__ == "__main__":
    # tiny smoke check
    assert code_for_label("11_llm-training") == 11
    assert code_for_label("ai") == 10
    assert code_for_label("archive") == 90
    assert code_for_label("nonsense-xyz") == 0
    assert next_id(11, ["11.01", "11.02", "12.01"]) == "11.03"
    assert next_id(11, []) == "11.01"
    nid, folder = assign("11_llm-training", ["11.01"])
    assert nid == "11.02", nid
    assert folder == "10-19_ai-tech/11_llm-training", folder
    print("jdid self-check OK:", nid, folder)
