#!/usr/bin/env python3
"""index.py — (re)build every read surface from the note frontmatter.

Stdlib only. Reads the Johnny.Decimal markdown notes under ``<KB>`` (the single
source of truth) and regenerates, atomically:

    <KB>/INDEX.tsv            grep target: id title tags tldr author date type url
    <KB>/.state/kb.db         SQLite FTS5 over title/tags/tldr/body (bm25, snippet)
    <KB>/00_START-HERE.md     plain-English guide + area list with counts
    <KB>/<area>/_README.md    per-area table of its notes
    <KB>/kb.html              self-contained command-palette UI (index embedded)

None of these is a source of truth; every one is rebuilt from the notes, so the
UI, the agent (grep), and the raw files never diverge.

CLI (frozen):  python3 index.py --kb <KB>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

# --- import shim ---------------------------------------- #
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import schema        # noqa: E402
import frontmatter   # noqa: E402
import jdid          # noqa: E402
import util          # noqa: E402

import ui_template   # noqa: E402  (sibling module: the kb.html generator)


# --------------------------------------------------------------------------- #
# 1. Collect notes from disk
# --------------------------------------------------------------------------- #
# A "note" is any CC.NN_*.md file living under an area folder. We deliberately
# skip generated aids (00_START-HERE.md, _README.md) and anything in .state/.
_STATE_DIR_NAME = ".state"
_SKIP_FILES = {"00_START-HERE.md", "_README.md"}


def _is_note_filename(name: str) -> bool:
    """True for 'CC.NN_kebab-title.md'. The id prefix is two-digit.two-digit."""
    if not name.endswith(".md"):
        return False
    if name in _SKIP_FILES:
        return False
    stem = name[:-3]
    head = stem.split("_", 1)[0]
    parts = head.split(".")
    if len(parts) != 2:
        return False
    return parts[0].isdigit() and parts[1].isdigit()


def iter_note_paths(kb_root: str) -> List[str]:
    """All note file paths under ``kb_root``, excluding .state/ and generated
    aids, sorted for deterministic output."""
    out: List[str] = []
    for root, dirs, files in os.walk(kb_root):
        # prune .state and any hidden dir so output is stable + machine-state free
        dirs[:] = sorted(d for d in dirs if d != _STATE_DIR_NAME
                         and not d.startswith("."))
        for fn in sorted(files):
            if _is_note_filename(fn):
                out.append(os.path.join(root, fn))
    return out


# --------------------------------------------------------------------------- #
# 2. Parse one note into a record the surfaces share
# --------------------------------------------------------------------------- #
# Body sections are the four labels build_kb.py emits. We extract them so the UI
# detail panel and the FTS body column carry the real prose, not just frontmatter.
_SECTION_LABELS = {
    "tldr": "**TL;DR:**",
    "why_saved": "**Why saved:**",
    "key_points": "**Key points:**",
    "links": "**Links:**",
    "full_text": "**Full text:**",
}


def _meta_str(meta: Dict[str, Any], key: str, default: str = "") -> str:
    v = meta.get(key, default)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return "" if v is None else str(v)


def _meta_int(meta: Dict[str, Any], key: str, default: int = 0) -> int:
    """Read a frontmatter scalar as an int. The frontmatter parser hands back
    strings, so we coerce; a missing / non-numeric value falls back to default."""
    v = meta.get(key, default)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _canonical_status_url(status_id: str) -> str:
    """Handle-independent permalink. X redirects /i/status/<id> to the live
    handle, so renamed / suspended / deleted / fake handles still resolve. Mirrors
    ingest._canonical_status_url so a note missing the field can still derive one."""
    sid = str(status_id or "").strip()
    return "https://x.com/i/status/{}".format(sid) if sid else ""


# A bold-label heading is '**Some words:**' at the start of a line. build_kb
# emits **Media:** between **Key points:** and **Links:**; the UI does not index
# it, but its lines must still close the preceding section so its raw markdown
# (e.g. '**Media:** - photo (https://pbs.twimg.com/...)') never bleeds into the
# why/tldr text. We treat ANY such heading -- known or not -- as a hard boundary.
_BOLD_HEADING_RE = re.compile(r"^\*\*[^*]+:\*\*")


def _parse_body_sections(body: str) -> Dict[str, Any]:
    """Split the note body into {tldr, why_saved, key_points[list], links}.

    The body is the canonical build_kb.py layout: bold-label paragraphs, with
    Key points as a bullet list. Each scalar section (tldr / why_saved / links)
    is a single paragraph: it ends at the next blank line or the next
    ``**Heading:**`` -- including headings we do not index (e.g. ``**Media:**``),
    so their markdown never leaks into the captured text. We still parse
    leniently: a continuation line of a paragraph (no blank line between) is
    appended to the current section."""
    sec = {"tldr": "", "why_saved": "", "key_points": [], "links": "",
           "full_text": ""}
    cur: Optional[str] = None
    for raw_line in body.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        matched = None
        for key, label in _SECTION_LABELS.items():
            if stripped.startswith(label):
                matched = key
                rest = stripped[len(label):].strip()
                cur = key
                if key == "key_points":
                    if rest:
                        sec["key_points"].append(rest)
                else:
                    sec[key] = rest
                break
        if matched is not None:
            continue
        # Full text is free-form and rendered LAST: once inside it, capture every
        # remaining line (blank lines = paragraph breaks; a stray '**x:**' is text).
        if cur == "full_text":
            sec["full_text"] = (sec["full_text"] + "\n" + line) if sec["full_text"] else line
            continue
        # Any other bold-label heading (e.g. '**Media:**') closes the current
        # section; we do not index it, so it must not append to why/tldr/links.
        if _BOLD_HEADING_RE.match(stripped):
            cur = None
            continue
        if cur is None:
            continue
        if cur == "key_points":
            if not stripped:
                # a blank line ends the bullet list
                cur = None
            elif stripped.startswith(("-", "*", "•")):
                item = stripped.lstrip("-*• ").strip()
                if item:
                    sec["key_points"].append(item)
            else:
                # continuation of the previous bullet
                if sec["key_points"]:
                    sec["key_points"][-1] += " " + stripped
        else:
            # A scalar paragraph ends at the first blank line; anything after it
            # belongs to a later section (or stray text), never to this one.
            if not stripped:
                cur = None
            else:
                sec[cur] = (sec[cur] + " " + stripped).strip() if sec[cur] else stripped
    return sec


def load_note(path: str) -> Optional[Dict[str, Any]]:
    """Parse a note file into a flat record used by every surface. Returns None
    if the file has no usable id (then it is not a real note)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    meta, body = frontmatter.parse(text)

    note_id = _meta_str(meta, "id").strip()
    if not note_id:
        # fall back to the filename prefix so a frontmatter-less note still lands
        base = os.path.basename(path)[:-3]
        note_id = base.split("_", 1)[0]
    if not note_id:
        return None

    sections = _parse_body_sections(body)
    tags = meta.get("tags")
    if not isinstance(tags, list):
        tags = [t for t in str(tags or "").split(",") if t.strip()]
    tags = [str(t).strip() for t in tags if str(t).strip()]

    category = _meta_str(meta, "category")
    try:
        cc = jdid.code_for_label(category) if category else jdid.code_for_label(note_id)
    except Exception:
        cc = jdid.code_for_label(note_id)
    area_folder = jdid.area_folder_name(cc)
    area_slug, area_desc = jdid.area_for_code(cc)

    status_id = _meta_str(meta, "status_id")
    # canonical_url is the handle-independent open-original target. Read it from
    # frontmatter; derive https://x.com/i/status/<status_id> for older / hand-
    # written notes that predate the field (or that left it blank).
    canonical_url = _meta_str(meta, "canonical_url")
    if not canonical_url:
        canonical_url = _canonical_status_url(status_id)

    # Derived media scalars (emitted by build_kb; absent on older notes -> empty).
    media_count = _meta_int(meta, "media_count", 0)
    thumb = _meta_str(meta, "thumb")
    media_alt = _meta_str(meta, "media_alt")

    rec = {
        "id": note_id,
        "status_id": status_id,
        "title": _meta_str(meta, "title") or note_id,
        "url": _meta_str(meta, "url"),
        "canonical_url": canonical_url,
        "author": _meta_str(meta, "author"),
        "date": _meta_str(meta, "posted") or _meta_str(meta, "saved"),
        "type": _meta_str(meta, "type", "tweet") or "tweet",
        "lang": _meta_str(meta, "lang"),
        "category": category,
        "tags": tags,
        "media_count": media_count,
        "thumb": thumb,
        "media_alt": media_alt,
        "tldr": sections["tldr"],
        "why_saved": sections["why_saved"],
        "key_points": sections["key_points"],
        "links": sections["links"],
        "full_text": sections["full_text"].strip(),
        "cc": cc,
        "area_folder": area_folder,
        "area_slug": area_slug,
        "area_desc": area_desc,
        "path": path,
        "body": body,
    }
    return rec


def _sort_key(rec: Dict[str, Any]) -> Tuple:
    """Order records by area, then numeric id, then title. Deterministic so all
    generated files are byte-stable across runs on unchanged input."""
    parts = str(rec["id"]).split(".")
    try:
        cc = int(parts[0])
        nn = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        cc, nn = 999, 999
    return (rec["cc"], cc, nn, rec["title"].lower())


def collect_records(kb_root: str) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    for path in iter_note_paths(kb_root):
        rec = load_note(path)
        if rec is not None:
            recs.append(rec)
    recs.sort(key=_sort_key)
    return recs


# --------------------------------------------------------------------------- #
# 3. INDEX.tsv
# --------------------------------------------------------------------------- #
def build_index_tsv(kb_root: str, recs: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for r in recs:
        row = schema.IndexRow(
            id=r["id"], title=r["title"], tags=r["tags"], tldr=r["tldr"],
            author=r["author"], date=r["date"], type=r["type"], url=r["url"],
            has_media=bool(r.get("media_count", 0)),
            thumb=r.get("thumb", ""), media_alt=r.get("media_alt", ""),
        )
        lines.append(row.to_tsv())
    text = "\n".join(lines) + ("\n" if lines else "")
    path = os.path.join(kb_root, "INDEX.tsv")
    util.atomic_write(path, text)
    return path


# --------------------------------------------------------------------------- #
# 4. SQLite FTS5 index  (.state/kb.db)
# --------------------------------------------------------------------------- #
# Virtual table columns: id (unindexed key), title, tags, tldr, body. Ranking is
# bm25(); snippet() gives bounded excerpts. Rebuilt from scratch every run so it
# is never a source of truth.
def build_fts_db(kb_root: str, recs: List[Dict[str, Any]]) -> str:
    state = util.state_dir(kb_root)
    util.ensure_dir(state)
    db_path = os.path.join(state, "kb.db")
    # Build into a temp db then atomically replace, so an interrupted rebuild
    # never leaves a half-written index (and concurrent readers see the old one).
    tmp_path = db_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    con = sqlite3.connect(tmp_path)
    try:
        con.execute("PRAGMA journal_mode=MEMORY")
        # 'id' carries the note id but is UNINDEXED so it is stored/returned but
        # not tokenized (it should never match a free-text query like '11').
        con.execute(
            "CREATE VIRTUAL TABLE bm USING fts5("
            "id UNINDEXED, title, tags, tldr, body, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        rows = []
        for r in recs:
            # media_alt joins the body blob so a search for words that appear
            # only in a media item's alt/ocr text still finds the note.
            body_blob = "\n".join([
                r["tldr"], r["why_saved"],
                "\n".join(r["key_points"]),
                r["links"], r.get("media_alt", ""), r["body"],
            ]).strip()
            rows.append((
                r["id"], r["title"], " ".join(r["tags"]),
                r["tldr"], body_blob,
            ))
        con.executemany(
            "INSERT INTO bm(id, title, tags, tldr, body) VALUES (?,?,?,?,?)",
            rows,
        )
        con.commit()
        con.execute("PRAGMA optimize")
        con.commit()
    finally:
        con.close()

    os.replace(tmp_path, db_path)
    return db_path


# --------------------------------------------------------------------------- #
# 5. 00_START-HERE.md  (+ per-area counts)
# --------------------------------------------------------------------------- #
def _area_counts(recs: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in recs:
        counts[r["area_folder"]] = counts.get(r["area_folder"], 0) + 1
    return counts


def build_start_here(kb_root: str, recs: List[Dict[str, Any]]) -> str:
    total = len(recs)
    counts = _area_counts(recs)
    n_areas = sum(1 for v in counts.values() if v > 0)

    out: List[str] = []
    out.append("# Your X Bookmarks — Knowledge Base")
    out.append("")
    out.append(
        "This folder is a searchable, human-readable copy of your X (Twitter) "
        "bookmarks. Every bookmark is one plain Markdown note you can open in any "
        "editor. Nothing here needs special software."
    )
    out.append("")
    out.append("**{} bookmarks** across **{} areas**.".format(total, n_areas))
    out.append("")
    out.append("## Three ways to use it")
    out.append("")
    out.append("- **Open `kb.html`** (double-click it) for a fast, keyboard-first "
               "search UI. Press `/` or `Cmd/Ctrl-K` to search; arrow keys move, "
               "`Enter` opens a note inline, `Esc` clears.")
    out.append("- **Ask the agent** in plain English (\"find my RLHF threads\") — "
               "it greps `INDEX.tsv` and opens only the few notes it needs.")
    out.append("- **Browse the folders** below by topic. Each area has a "
               "`_README.md` listing its notes.")
    out.append("")
    out.append("## Areas")
    out.append("")
    out.append("| Area | Count | Folder |")
    out.append("|---|---:|---|")
    for lo, hi, slug, desc in jdid.DEFAULT_TAXONOMY:
        folder = "{:02d}-{:02d}_{}".format(lo, hi, slug)
        c = counts.get(folder, 0)
        # the human label is the part of desc before the parenthetical
        label = desc.split("(")[0].strip()
        out.append("| {} | {} | `{}/` |".format(label, c, folder))
    out.append("")
    out.append("## How it stays current")
    out.append("")
    out.append("Say **sync** to fetch new bookmarks, or **import** for a full "
               "first export. **status** shows counts and a tag histogram; "
               "**organize** dedups and flags dead links. Re-running any step on "
               "unchanged bookmarks changes nothing.")
    out.append("")
    out.append("_Generated by `index.py` from the notes themselves — edits to a "
               "note are picked up on the next rebuild._")
    out.append("")

    text = "\n".join(out)
    path = os.path.join(kb_root, "00_START-HERE.md")
    util.atomic_write(path, text)
    return path


# --------------------------------------------------------------------------- #
# 6. per-area _README.md
# --------------------------------------------------------------------------- #
def _md_cell(s: str) -> str:
    """Escape a value for a Markdown table cell (pipes/newlines)."""
    return str(s).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def build_area_readmes(kb_root: str, recs: List[Dict[str, Any]]) -> List[str]:
    """Write one _README.md per area that actually has notes. We regenerate only
    populated areas; empty areas get no README (and no empty folder churn)."""
    by_area: Dict[str, List[Dict[str, Any]]] = {}
    area_meta: Dict[str, Tuple[str, str]] = {}
    for r in recs:
        by_area.setdefault(r["area_folder"], []).append(r)
        area_meta[r["area_folder"]] = (r["area_slug"], r["area_desc"])

    written: List[str] = []
    for folder, items in sorted(by_area.items()):
        slug, desc = area_meta[folder]
        label = desc.split("(")[0].strip()
        items_sorted = sorted(items, key=_sort_key)

        out: List[str] = []
        out.append("# {}".format(label))
        out.append("")
        out.append("_{}_".format(desc))
        out.append("")
        out.append("**{} note{}** in this area.".format(
            len(items_sorted), "" if len(items_sorted) == 1 else "s"))
        out.append("")
        out.append("| ID | Title | Tags | Author | Date | Type |")
        out.append("|---|---|---|---|---|---|")
        for r in items_sorted:
            rel = os.path.relpath(r["path"], os.path.join(kb_root, folder))
            rel = rel.replace(os.sep, "/")
            title_link = "[{}]({})".format(_md_cell(r["title"]), rel)
            out.append("| {} | {} | {} | {} | {} | {} |".format(
                _md_cell(r["id"]), title_link,
                _md_cell(", ".join(r["tags"])),
                _md_cell(r["author"]), _md_cell(r["date"]),
                _md_cell(r["type"]),
            ))
        out.append("")
        out.append("_Generated by `index.py`._")
        out.append("")

        path = os.path.join(kb_root, folder, "_README.md")
        util.ensure_dir(os.path.dirname(path))
        util.atomic_write(path, "\n".join(out))
        written.append(path)
    return written


# --------------------------------------------------------------------------- #
# 7. kb.html  (self-contained UI; index embedded as inline JSON)
# --------------------------------------------------------------------------- #
def _ui_payload(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Shape the data the UI needs: the index rows plus the few extra fields the
    detail panel shows (why_saved, key_points, category, area grouping)."""
    items = []
    for r in recs:
        items.append({
            "id": r["id"],
            "title": r["title"],
            "tags": r["tags"],
            "tldr": r["tldr"],
            "author": r["author"],
            "date": r["date"],
            "type": r["type"],
            "url": r["url"],
            # canonical is the handle-independent open-original target; the UI
            # prefers it over url so renamed/suspended/deleted handles resolve.
            "canonical": r.get("canonical_url", ""),
            "lang": r.get("lang", ""),
            "thumb": r.get("thumb", ""),
            "mediaAlt": r.get("media_alt", ""),
            "media_count": r.get("media_count", 0),
            "why": r["why_saved"],
            "points": r["key_points"],
            # Full snapshot text (long-form note tweets + article previews). Capped
            # so the embedded index stays light; the note .md holds the untruncated
            # copy. Omitted when it only duplicates the TL;DR.
            "text": (r.get("full_text", "") or "")[:4000],
            "category": r["category"],
            "area": r["area_folder"],
            "areaLabel": r["area_desc"].split("(")[0].strip(),
            "areaSlug": r["area_slug"],
        })
    # Area ordering follows the taxonomy (areas with 0 notes are omitted).
    present = []
    seen = set()
    for r in recs:
        if r["area_folder"] not in seen:
            seen.add(r["area_folder"])
            present.append({
                "folder": r["area_folder"],
                "label": r["area_desc"].split("(")[0].strip(),
                "slug": r["area_slug"],
                "cc": r["cc"],
            })
    present.sort(key=lambda a: a["cc"])
    return {"items": items, "areas": present, "count": len(items)}


def build_html(kb_root: str, recs: List[Dict[str, Any]]) -> str:
    payload = _ui_payload(recs)
    html = ui_template.render(payload)
    path = os.path.join(kb_root, "kb.html")
    util.atomic_write(path, html)
    return path


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def rebuild(kb_root: str) -> Dict[str, Any]:
    """Rebuild every surface. Returns a small summary dict (used by tests/CLI)."""
    util.ensure_dir(kb_root)
    recs = collect_records(kb_root)
    index_path = build_index_tsv(kb_root, recs)
    db_path = build_fts_db(kb_root, recs)
    start_path = build_start_here(kb_root, recs)
    readme_paths = build_area_readmes(kb_root, recs)
    html_path = build_html(kb_root, recs)
    return {
        "count": len(recs),
        "index": index_path,
        "db": db_path,
        "start_here": start_path,
        "readmes": readme_paths,
        "html": html_path,
        "areas": sorted({r["area_folder"] for r in recs}),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="index.py",
        description="Rebuild INDEX.tsv, kb.db (FTS5), kb.html, START-HERE and "
                    "per-area READMEs from the notes.",
    )
    ap.add_argument("--kb", default=util.default_kb_root(),
                    help="knowledge-base root (default: $BOOKMARKS_KB or "
                         "~/Documents/Twitter Bookmarks)")
    args = ap.parse_args(argv)

    kb_root = os.path.abspath(os.path.expanduser(args.kb))
    if not os.path.isdir(kb_root):
        sys.stderr.write("index.py: KB root does not exist: {}\n".format(kb_root))
        return 2

    summary = rebuild(kb_root)
    sys.stdout.write(
        "indexed {} note{} -> INDEX.tsv, kb.db, kb.html, START-HERE, "
        "{} area README{}\n".format(
            summary["count"], "" if summary["count"] == 1 else "s",
            len(summary["readmes"]),
            "" if len(summary["readmes"]) == 1 else "s",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
