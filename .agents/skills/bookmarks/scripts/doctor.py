#!/usr/bin/env python3
"""doctor.py -- maintenance and anti-rot for the bookmarks knowledge base.

Stdlib only. Keeps the KB from becoming a graveyard as it grows (see
the design). The notes under ``<KB>`` are the single source of
truth; every check reads their frontmatter + body, the same surface index.py and
query.py read, so doctor never invents data it cannot point at.

CLI (frozen):

    python3 doctor.py --kb <KB> [--stats|--dedup|--dead-links|--decay|--digest]

With no mode flag, defaults to --stats. Exactly one mode runs per invocation
(argparse mutually-exclusive group); --stats wins the default.

Modes:

  --stats       Counts per area, a tag histogram, the total, and growth over
                time (notes added per month, derived from the ``saved`` date).
                Read-only.

  --dedup       Merge notes that share the same ``status_id`` OR the same
                resolved URL. The lowest Johnny.Decimal id is kept as canonical;
                the duplicate note files are removed (their content already lives
                in the survivor's snapshot). Idempotent: a KB with no duplicates
                is left byte-untouched. Use --dry-run to only report.

  --dead-links  HEAD/GET each note's outbound links (and the tweet permalink),
                flagging anything that does not resolve to a live 2xx/3xx. Honors
                a no-network test mode: pass --offline to skip every network call
                (then every link is reported as "skipped", nothing is flagged
                broken). Read-only -- a dead link still has usable snapshot text,
                so we never delete the note, only report.

  --decay       SUGGEST (never move, never delete) notes untouched past the
                policy window (default 90 days, --days N) for the 90-99_archive
                area. "Untouched" = the note file's modification time. Notes
                already in the archive area are skipped. Read-only.

  --digest      Print the N (--limit, default 10) most aging / least-recently
                surfaced items with their one-line summaries, so the user can
                resurface things they saved and forgot. Read-only.

Common flags:
    --kb <KB>     (required) knowledge-base root.
    --json        emit machine-readable JSON instead of the human report.
    --limit N     digest size / dedup+decay report cap (mode-specific default).
    --days N      decay policy window in days (default 90).
    --offline     dead-links: skip all network, report links as "skipped".
    --dry-run     dedup: report duplicates but do not remove any file.

All file mutations (only --dedup removes files) stay inside ``--kb``. Re-running
any read-only mode changes nothing; re-running --dedup on a deduped KB is a
no-op.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# --- import shim ---------------------------------- #
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import schema        # noqa: E402
import frontmatter   # noqa: E402
import jdid          # noqa: E402
import util          # noqa: E402


# --------------------------------------------------------------------------- #
# Note discovery + loading. Same rules as index.py / build_kb.py: a note is a
# ``CC.NN_*.md`` file under an area folder; generated aids and .state/ are
# skipped. We keep this self-contained (no cross-script import) so doctor.py
# stands alone, but the filename/skip semantics match exactly.
# --------------------------------------------------------------------------- #
_STATE_DIR_NAME = ".state"
_SKIP_FILES = {"00_START-HERE.md", "_README.md"}
_NOTE_NAME_RE = re.compile(r"^(\d{1,2})\.(\d{1,3})_.*\.md$")


def _is_note_filename(name: str) -> bool:
    if name in _SKIP_FILES:
        return False
    return _NOTE_NAME_RE.match(name) is not None


def iter_note_paths(kb_root: str) -> List[str]:
    """All note file paths under ``kb_root`` (sorted, deterministic), excluding
    ``.state``/dotfolders and the generated aids."""
    out: List[str] = []
    for root, dirs, files in os.walk(kb_root):
        dirs[:] = sorted(d for d in dirs
                         if d != _STATE_DIR_NAME and not d.startswith("."))
        for fn in sorted(files):
            if _is_note_filename(fn):
                out.append(os.path.join(root, fn))
    return out


_SECTION_RE = re.compile(r"\*\*(TL;DR|Why saved|Key points|Links):\*\*",
                         re.IGNORECASE)


def _parse_body_sections(body: str) -> Dict[str, Any]:
    """Split a note body into {tldr, why_saved, key_points[list], links[list]}.
    Lenient: matches the bold-label layout build_kb.py emits but tolerates
    hand-edited notes (unknown lines extend the current section)."""
    sec: Dict[str, Any] = {"tldr": "", "why_saved": "", "key_points": [],
                           "links": []}
    cur: Optional[str] = None
    for raw_line in body.split("\n"):
        line = raw_line.strip()
        m = _SECTION_RE.match(line)
        if m:
            label = m.group(1).lower()
            after = line.split(":**", 1)[1].strip() if ":**" in line else ""
            if label == "tl;dr":
                cur = "tldr"
                sec["tldr"] = after
            elif label == "why saved":
                cur = "why_saved"
                sec["why_saved"] = after
            elif label == "key points":
                cur = "key_points"
                if after:
                    sec["key_points"].append(after)
            elif label == "links":
                cur = "links"
                if after:
                    sec["links"].append(after)
            continue
        if cur is None or not line:
            continue
        if cur in ("key_points", "links"):
            item = line.lstrip("-*• ").strip()
            if item:
                sec[cur].append(item)
        else:
            sec[cur] = (sec[cur] + " " + line).strip() if sec[cur] else line
    return sec


def _meta_str(meta: Dict[str, Any], key: str, default: str = "") -> str:
    v = meta.get(key, default)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return "" if v is None else str(v)


def load_note(path: str) -> Optional[Dict[str, Any]]:
    """Parse one note into the flat record every doctor mode shares. Returns
    None for an unreadable / id-less file (then it is not a real note)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    meta, body = frontmatter.parse(text)

    note_id = _meta_str(meta, "id").strip()
    if not note_id:
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

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0

    return {
        "id": note_id,
        "status_id": _meta_str(meta, "status_id"),
        "title": _meta_str(meta, "title") or note_id,
        "url": _meta_str(meta, "url"),
        "author": _meta_str(meta, "author"),
        "date": _meta_str(meta, "saved"),
        "type": _meta_str(meta, "type", "tweet") or "tweet",
        "category": category,
        "tags": tags,
        "tldr": sections["tldr"],
        "why_saved": sections["why_saved"],
        "key_points": sections["key_points"],
        "links": sections["links"],
        "content_hash": _meta_str(meta, "content_hash"),
        "cc": cc,
        "area_folder": jdid.area_folder_name(cc),
        "area_slug": jdid.area_for_code(cc)[0],
        "area_desc": jdid.area_for_code(cc)[1],
        "path": path,
        "mtime": mtime,
    }


def _id_sort_tuple(note_id: str) -> Tuple[int, int]:
    """(CC, NN) integer tuple for a 'CC.NN' id; junk -> very large so it sorts
    last. Used to pick the lowest id as the canonical survivor in --dedup."""
    parts = str(note_id).split(".")
    try:
        cc = int(parts[0])
        nn = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return (10 ** 6, 10 ** 6)
    return (cc, nn)


def collect_notes(kb_root: str) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    for path in iter_note_paths(kb_root):
        rec = load_note(path)
        if rec is not None:
            recs.append(rec)
    recs.sort(key=lambda r: (r["cc"], _id_sort_tuple(r["id"]),
                             r["title"].lower()))
    return recs


# --------------------------------------------------------------------------- #
# URL canonicalization (for --dedup "same resolved url"). We resolve the obvious
# equivalences (scheme/host case, default port, trailing slash, common tracking
# params, x.com<->twitter.com host, leading 'www.') without any network. Two
# bookmarks that point at the same canonical URL are duplicates.
# --------------------------------------------------------------------------- #
_TRACKING_PARAMS = frozenset((
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "s", "t", "ref_src", "ref_url", "cxt", "fbclid", "gclid", "mc_cid",
    "mc_eid", "igshid",
))


def canonical_url(url: str) -> str:
    """Best-effort, network-free canonical form of a URL for equality. Empty in
    -> empty out (an empty url never makes two notes 'the same url')."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    raw = (url or "").strip()
    if not raw:
        return ""
    # Treat a bare host as https for splitting purposes.
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    # twitter.com and x.com serve the same content; fold to x.com.
    if host in ("twitter.com", "mobile.twitter.com"):
        host = "x.com"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = "/"

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS]
    kept.sort()
    query = urlencode(kept)

    # Drop scheme (http vs https should not split a dup) and fragment.
    return urlunsplit(("", host, path, query, ""))


# --------------------------------------------------------------------------- #
# --stats
# --------------------------------------------------------------------------- #
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})")


def compute_stats(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts per area, a tag histogram (desc by count then tag), the total, and
    growth (notes saved per YYYY-MM, ascending). Pure over the loaded notes."""
    total = len(recs)

    # per-area counts, in taxonomy order, only areas that have notes.
    area_counts: Dict[str, int] = {}
    area_label: Dict[str, str] = {}
    for r in recs:
        folder = r["area_folder"]
        area_counts[folder] = area_counts.get(folder, 0) + 1
        area_label[folder] = r["area_desc"].split("(")[0].strip()
    areas: List[Dict[str, Any]] = []
    for lo, hi, slug, desc in jdid.DEFAULT_TAXONOMY:
        folder = "{:02d}-{:02d}_{}".format(lo, hi, slug)
        c = area_counts.get(folder, 0)
        if c:
            areas.append({"folder": folder,
                          "label": desc.split("(")[0].strip(),
                          "count": c})

    # tag histogram
    tag_counts: Dict[str, int] = {}
    for r in recs:
        for t in r["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    tags = sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    tag_hist = [{"tag": t, "count": c} for t, c in tags]

    # growth: notes per YYYY-MM (from the 'saved' date), ascending by month.
    month_counts: Dict[str, int] = {}
    undated = 0
    for r in recs:
        m = _MONTH_RE.match(r["date"] or "")
        if m:
            ym = "{}-{}".format(m.group(1), m.group(2))
            month_counts[ym] = month_counts.get(ym, 0) + 1
        else:
            undated += 1
    growth = [{"month": ym, "count": month_counts[ym]}
              for ym in sorted(month_counts)]

    # type breakdown is a cheap extra the digest/decay also benefit from.
    type_counts: Dict[str, int] = {}
    for r in recs:
        type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1
    types = sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    return {
        "total": total,
        "areas": areas,
        "tags": tag_hist,
        "distinct_tags": len(tag_hist),
        "growth": growth,
        "undated": undated,
        "types": [{"type": t, "count": c} for t, c in types],
    }


def format_stats(stats: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append("Knowledge base: {} note{} across {} area{}.".format(
        stats["total"], "" if stats["total"] == 1 else "s",
        len(stats["areas"]), "" if len(stats["areas"]) == 1 else "s"))
    out.append("")

    if stats["areas"]:
        out.append("Per area:")
        width = max(len(a["label"]) for a in stats["areas"])
        for a in stats["areas"]:
            out.append("  {:<{w}}  {:>4}".format(a["label"], a["count"],
                                                 w=width))
        out.append("")

    if stats["tags"]:
        out.append("Tag histogram ({} distinct):".format(stats["distinct_tags"]))
        shown = stats["tags"][:30]
        width = max(len(t["tag"]) for t in shown)
        for t in shown:
            bar = "#" * min(t["count"], 40)
            out.append("  {:<{w}}  {:>4}  {}".format(t["tag"], t["count"], bar,
                                                     w=width))
        if len(stats["tags"]) > len(shown):
            out.append("  ... and {} more tags".format(
                len(stats["tags"]) - len(shown)))
        out.append("")

    if stats["growth"]:
        out.append("Growth (notes saved per month):")
        for g in stats["growth"]:
            bar = "#" * min(g["count"], 40)
            out.append("  {}  {:>4}  {}".format(g["month"], g["count"], bar))
        if stats["undated"]:
            out.append("  (undated)  {:>4}".format(stats["undated"]))
        out.append("")

    if stats["types"]:
        out.append("Types: " + ", ".join(
            "{} {}".format(t["count"], t["type"]) for t in stats["types"]))

    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# --dedup
# --------------------------------------------------------------------------- #
def find_duplicate_groups(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group notes that are the same bookmark by either a shared non-empty
    ``status_id`` or a shared canonical URL. Returns one group per duplicate set
    (size >= 2), each as {key, basis, survivor, duplicates[]} with the lowest id
    chosen as the survivor. Notes with neither a status_id nor a url are never
    merged (nothing ties them together)."""
    # Union-find over note indices so a chain (A==B by status_id, B==C by url)
    # collapses into one group.
    parent = list(range(len(recs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    by_sid: Dict[str, int] = {}
    by_url: Dict[str, int] = {}
    for idx, r in enumerate(recs):
        sid = (r["status_id"] or "").strip()
        if sid:
            if sid in by_sid:
                union(by_sid[sid], idx)
            else:
                by_sid[sid] = idx
        cu = canonical_url(r["url"])
        if cu:
            if cu in by_url:
                union(by_url[cu], idx)
            else:
                by_url[cu] = idx

    groups_idx: Dict[int, List[int]] = {}
    for idx in range(len(recs)):
        groups_idx.setdefault(find(idx), []).append(idx)

    groups: List[Dict[str, Any]] = []
    for members in groups_idx.values():
        if len(members) < 2:
            continue
        members_recs = [recs[i] for i in members]
        members_recs.sort(key=lambda r: (_id_sort_tuple(r["id"]), r["path"]))
        survivor = members_recs[0]
        dups = members_recs[1:]
        sids = {(r["status_id"] or "").strip() for r in members_recs
                if (r["status_id"] or "").strip()}
        urls = {canonical_url(r["url"]) for r in members_recs
                if canonical_url(r["url"])}
        if len(sids) == 1 and any((r["status_id"] or "").strip()
                                  for r in members_recs):
            basis = "status_id"
            key = next(iter(sids))
        else:
            basis = "resolved-url"
            key = next(iter(urls)) if urls else (next(iter(sids)) if sids else "")
        groups.append({
            "basis": basis,
            "key": key,
            "survivor": {"id": survivor["id"], "title": survivor["title"],
                         "path": survivor["path"]},
            "duplicates": [{"id": d["id"], "title": d["title"],
                            "path": d["path"]} for d in dups],
        })
    groups.sort(key=lambda g: _id_sort_tuple(g["survivor"]["id"]))
    return groups


def run_dedup(kb_root: str, recs: List[Dict[str, Any]],
              dry_run: bool = False) -> Dict[str, Any]:
    """Merge duplicate notes. The lowest-id note in each group is kept; the rest
    are removed (their snapshot content is already preserved in the survivor).
    ``dry_run`` reports without removing. Returns a summary."""
    groups = find_duplicate_groups(recs)
    removed: List[str] = []
    for g in groups:
        for d in g["duplicates"]:
            path = d["path"]
            if dry_run:
                continue
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    return {
        "groups": groups,
        "duplicate_count": sum(len(g["duplicates"]) for g in groups),
        "removed": removed,
        "dry_run": dry_run,
    }


def format_dedup(result: Dict[str, Any]) -> str:
    groups = result["groups"]
    if not groups:
        return "No duplicates: every note has a distinct status_id and URL.\n"
    out: List[str] = []
    verb = "would merge" if result["dry_run"] else "merged"
    out.append("{} {} duplicate group{} ({} duplicate note{}):".format(
        verb.capitalize(), len(groups), "" if len(groups) == 1 else "s",
        result["duplicate_count"],
        "" if result["duplicate_count"] == 1 else "s"))
    out.append("")
    for g in groups:
        out.append("  [{}] keep {} — {}".format(
            g["basis"], g["survivor"]["id"], g["survivor"]["title"]))
        for d in g["duplicates"]:
            mark = "drop" if not result["dry_run"] else "would drop"
            out.append("        {} {} — {}".format(
                mark, d["id"], d["title"]))
            out.append("              {}".format(d["path"]))
    out.append("")
    out.append("Run `index.py --kb <KB>` to rebuild INDEX.tsv / kb.html after a "
               "merge." if not result["dry_run"]
               else "Re-run without --dry-run to apply, then rebuild the index.")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# --dead-links
# --------------------------------------------------------------------------- #
_LINK_TIMEOUT = 8.0  # seconds per request


def _collect_links(rec: Dict[str, Any]) -> List[str]:
    """Outbound links to check for one note: its body Links plus the tweet
    permalink (url). De-duped, order-preserving, http(s) only."""
    out: List[str] = []
    seen = set()
    for u in [rec["url"]] + list(rec["links"]):
        u = (u or "").strip()
        if not u:
            continue
        if not (u.startswith("http://") or u.startswith("https://")):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def check_link(url: str, timeout: float = _LINK_TIMEOUT) -> Tuple[str, int, str]:
    """Check one URL over the network. Tries a HEAD then falls back to a ranged
    GET (some hosts reject HEAD). Returns (state, status_code, detail):
      state in {'ok','broken','error'}; status_code is the HTTP code (0 if none).
    Never raises. Caller must NOT invoke this in --offline mode."""
    import urllib.request
    import urllib.error

    headers = {
        "User-Agent": ("Mozilla/5.0 (compatible; bookmarks-doctor/1.0; "
                       "+local-maintenance)"),
        "Accept": "*/*",
    }

    def _attempt(method: str) -> Tuple[Optional[int], Optional[str]]:
        req = urllib.request.Request(url, method=method, headers=dict(headers))
        if method == "GET":
            req.add_header("Range", "bytes=0-0")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return getattr(resp, "status", resp.getcode()), None
        except urllib.error.HTTPError as e:
            # An HTTP status came back (e.g. 405 to HEAD, 404 broken). Return it.
            return e.code, None
        except urllib.error.URLError as e:
            return None, str(getattr(e, "reason", e))
        except (ValueError, OSError) as e:
            return None, str(e)
        except Exception as e:  # noqa: BLE001 - network is hostile; never crash
            return None, str(e)

    code, err = _attempt("HEAD")
    # Fall back to GET when HEAD is rejected/unsupported or yielded no status.
    if code is None or code in (403, 405, 501):
        code2, err2 = _attempt("GET")
        if code2 is not None:
            code, err = code2, None
        elif code is None:
            err = err2 or err

    if code is None:
        return "error", 0, (err or "no response")
    if 200 <= code < 400:
        return "ok", code, ""
    return "broken", code, "HTTP {}".format(code)


def run_dead_links(recs: List[Dict[str, Any]], offline: bool = False,
                   timeout: float = _LINK_TIMEOUT) -> Dict[str, Any]:
    """Check every note's links. In ``offline`` mode no network call is made and
    every link is reported as 'skipped' (the no-network test path). Otherwise
    each unique URL is checked once (cached across notes). Read-only."""
    # Gather unique urls -> the notes that reference them.
    url_to_notes: Dict[str, List[Dict[str, str]]] = {}
    ordered_urls: List[str] = []
    for r in recs:
        for u in _collect_links(r):
            if u not in url_to_notes:
                url_to_notes[u] = []
                ordered_urls.append(u)
            url_to_notes[u].append({"id": r["id"], "title": r["title"],
                                    "path": r["path"]})

    results: List[Dict[str, Any]] = []
    broken = 0
    errored = 0
    checked = 0
    for u in ordered_urls:
        if offline:
            state, code, detail = "skipped", 0, "offline"
        else:
            state, code, detail = check_link(u, timeout=timeout)
            checked += 1
        if state == "broken":
            broken += 1
        elif state == "error":
            errored += 1
        results.append({
            "url": u,
            "state": state,
            "status_code": code,
            "detail": detail,
            "notes": url_to_notes[u],
        })

    return {
        "offline": offline,
        "total_links": len(ordered_urls),
        "checked": checked,
        "broken": broken,
        "errored": errored,
        "results": results,
    }


def format_dead_links(result: Dict[str, Any]) -> str:
    out: List[str] = []
    if result["offline"]:
        out.append("Dead-link check (offline): {} link{} found, none checked "
                   "(no network).".format(
                       result["total_links"],
                       "" if result["total_links"] == 1 else "s"))
        out.append("Snapshot text stays readable regardless; re-run without "
                   "--offline to verify links.")
        return "\n".join(out) + "\n"

    out.append("Dead-link check: {} link{} checked, {} broken, {} "
               "unreachable.".format(
                   result["total_links"],
                   "" if result["total_links"] == 1 else "s",
                   result["broken"], result["errored"]))
    flagged = [r for r in result["results"] if r["state"] in ("broken", "error")]
    if not flagged:
        out.append("All links resolve. Nothing to fix.")
        return "\n".join(out) + "\n"
    out.append("")
    for r in flagged:
        label = "BROKEN" if r["state"] == "broken" else "UNREACHABLE"
        out.append("  {} {}  {}".format(label, r["detail"] or "", r["url"]))
        for n in r["notes"]:
            out.append("        in [{}] {}".format(n["id"], n["title"]))
    out.append("")
    out.append("Links are only flagged, never removed — each note still "
               "holds the snapshot text captured when you saved it.")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# --decay  (suggest only, never move/delete)
# --------------------------------------------------------------------------- #
_ARCHIVE_CC_LO, _ARCHIVE_CC_HI = 90, 99
_SECONDS_PER_DAY = 86400.0


def run_decay(recs: List[Dict[str, Any]], days: int = 90,
              now: Optional[float] = None) -> Dict[str, Any]:
    """Suggest (never move) notes untouched longer than ``days`` for the archive
    area. "Untouched" = the note file mtime. Notes already in 90-99_archive are
    skipped. Read-only -- output is a suggestion list the user acts on, or not."""
    import time
    if now is None:
        now = time.time()
    cutoff = now - days * _SECONDS_PER_DAY

    suggestions: List[Dict[str, Any]] = []
    for r in recs:
        if _ARCHIVE_CC_LO <= r["cc"] <= _ARCHIVE_CC_HI:
            continue  # already archived
        if r["mtime"] and r["mtime"] < cutoff:
            age_days = int((now - r["mtime"]) / _SECONDS_PER_DAY)
            suggestions.append({
                "id": r["id"],
                "title": r["title"],
                "area": r["area_folder"],
                "age_days": age_days,
                "saved": r["date"],
                "path": r["path"],
            })
    suggestions.sort(key=lambda s: (-s["age_days"], _id_sort_tuple(s["id"])))
    return {
        "days": days,
        "archive_area": "90-99_archive",
        "candidates": suggestions,
        "count": len(suggestions),
    }


def format_decay(result: Dict[str, Any]) -> str:
    cands = result["candidates"]
    if not cands:
        return ("Nothing to archive: every active note has been touched within "
                "the last {} days.\n".format(result["days"]))
    out: List[str] = []
    out.append("Decay suggestions ({} note{} untouched > {} days). These are "
               "SUGGESTIONS only — nothing was moved or deleted.".format(
                   result["count"], "" if result["count"] == 1 else "s",
                   result["days"]))
    out.append("Suggested home: {}/".format(result["archive_area"]))
    out.append("")
    for c in cands:
        out.append("  {:>7}  {:>4}d  {}".format(c["id"], c["age_days"],
                                                c["title"]))
    out.append("")
    out.append("To archive one, move its file into {}/ and re-run `index.py`."
               .format(result["archive_area"]))
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# --digest  (resurface N aging / unread items)
# --------------------------------------------------------------------------- #
def run_digest(recs: List[Dict[str, Any]], limit: int = 10) -> Dict[str, Any]:
    """Pick the N most aging items to resurface, with their summaries. Aging =
    least-recently-touched first (oldest mtime), tie-broken by oldest saved date
    then id. Notes already in the archive area are skipped (they were already
    set aside). Read-only."""
    active = [r for r in recs
              if not (_ARCHIVE_CC_LO <= r["cc"] <= _ARCHIVE_CC_HI)]

    def _key(r: Dict[str, Any]) -> Tuple:
        # oldest first: smaller mtime ranks earlier; missing mtime sorts oldest.
        mt = r["mtime"] if r["mtime"] else 0.0
        return (mt, r["date"] or "", _id_sort_tuple(r["id"]))

    active.sort(key=_key)
    chosen = active[:max(0, limit)]
    items: List[Dict[str, Any]] = []
    for r in chosen:
        summary = r["tldr"] or (r["why_saved"] or "")
        items.append({
            "id": r["id"],
            "title": r["title"],
            "author": r["author"],
            "saved": r["date"],
            "type": r["type"],
            "tags": r["tags"],
            "summary": summary,
            "why_saved": r["why_saved"],
            "url": r["url"],
            "path": r["path"],
        })
    return {"limit": limit, "count": len(items), "total_active": len(active),
            "items": items}


def format_digest(result: Dict[str, Any]) -> str:
    items = result["items"]
    if not items:
        return "Nothing to resurface: the active set is empty.\n"
    out: List[str] = []
    out.append("Resurface digest — {} aging item{} (of {} active):".format(
        result["count"], "" if result["count"] == 1 else "s",
        result["total_active"]))
    out.append("")
    for n, it in enumerate(items, 1):
        out.append("{}. [{}] {}".format(n, it["id"], it["title"]
                                        or "(untitled)"))
        if it["summary"]:
            out.append("   {}".format(it["summary"]))
        meta_bits = []
        if it["author"]:
            meta_bits.append(it["author"])
        if it["saved"]:
            meta_bits.append(it["saved"])
        if it["type"]:
            meta_bits.append(it["type"])
        if it["tags"]:
            meta_bits.append("#" + " #".join(it["tags"]))
        if meta_bits:
            out.append("   " + "  ".join(meta_bits))
        if it["why_saved"]:
            out.append("   why: {}".format(it["why_saved"]))
        if it["url"]:
            out.append("   {}".format(it["url"]))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def run(kb_root: str, mode: str, *, json_out: bool = False,
        limit: Optional[int] = None, days: int = 90,
        offline: bool = False, dry_run: bool = False) -> Tuple[str, Any]:
    """Execute one doctor mode. Returns (text_report, json_payload). The json
    payload is what --json emits; the text report is the human view."""
    recs = collect_notes(kb_root)

    if mode == "stats":
        payload = compute_stats(recs)
        return format_stats(payload), payload

    if mode == "dedup":
        payload = run_dedup(kb_root, recs, dry_run=dry_run)
        return format_dedup(payload), payload

    if mode == "dead-links":
        payload = run_dead_links(recs, offline=offline)
        return format_dead_links(payload), payload

    if mode == "decay":
        payload = run_decay(recs, days=days)
        return format_decay(payload), payload

    if mode == "digest":
        payload = run_digest(recs, limit=(limit if limit is not None else 10))
        return format_digest(payload), payload

    raise ValueError("unknown mode: {}".format(mode))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="doctor.py",
        description="Maintenance + anti-rot for the bookmarks KB: stats, dedup, "
                    "dead-links, decay suggestions, and a resurface digest.")
    ap.add_argument("--kb", required=True, help="knowledge-base root")

    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--stats", action="store_true",
                     help="counts per area, tag histogram, total, growth "
                          "(default)")
    grp.add_argument("--dedup", action="store_true",
                     help="merge notes sharing a status_id or resolved URL")
    grp.add_argument("--dead-links", dest="dead_links", action="store_true",
                     help="flag broken outbound links (use --offline to skip "
                          "the network)")
    grp.add_argument("--decay", action="store_true",
                     help="suggest (never move) notes untouched past --days for "
                          "90-99_archive")
    grp.add_argument("--digest", action="store_true",
                     help="print --limit aging/unread items with summaries")

    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON instead of the text report")
    ap.add_argument("--limit", type=int, default=None,
                    help="digest size (default 10)")
    ap.add_argument("--days", type=int, default=90,
                    help="decay policy window in days (default 90)")
    ap.add_argument("--offline", action="store_true",
                    help="dead-links: skip all network (no-network test mode)")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="dedup: report duplicates without removing any file")
    args = ap.parse_args(argv)

    kb_root = os.path.abspath(os.path.expanduser(args.kb))
    if not os.path.isdir(kb_root):
        sys.stderr.write("doctor.py: KB root does not exist: {}\n".format(
            kb_root))
        return 2

    # Resolve the single mode (default --stats).
    if args.dedup:
        mode = "dedup"
    elif args.dead_links:
        mode = "dead-links"
    elif args.decay:
        mode = "decay"
    elif args.digest:
        mode = "digest"
    else:
        mode = "stats"

    text, payload = run(
        kb_root, mode,
        json_out=args.json, limit=args.limit, days=args.days,
        offline=args.offline, dry_run=args.dry_run,
    )

    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False,
                                    indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
