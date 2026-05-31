#!/usr/bin/env python3
"""build_kb.py -- turn enriched + normalized records into Johnny.Decimal notes.

Reads ``.state/enriched/*.out.jsonl`` (EnrichedBookmark) and
``.state/new_items.jsonl`` (NormalizedBookmark), joins them by ``status_id``,
allocates a Johnny.Decimal ``CC.NN`` id per bookmark, and writes one note per
bookmark to ``<KB>/<area>/<CC_category>/CC.NN_kebab-title.md`` with full
frontmatter and a readable body.

Frontmatter (order = ``frontmatter.FIELD_ORDER``):
    id, status_id, title, url, canonical_url, author, saved, type, lang,
    category, tags, media_count, thumb, media_alt, engagement, media, content_hash

Body sections (always emitted, in this order):
    **TL;DR:**     the one-sentence takeaway
    **Why saved:** the inferred intent
    **Key points:** bullet list (threads; omitted when there are none)
    **Media:**     attached images/video as a short list with alt text (omitted
                   when there is no media)
    **Links:**     outbound links (omitted when there are none)

Idempotency / id stability (the binding contract):
  * Before assigning anything, we scan the KB's existing notes and recover the
    ``status_id -> (id, content_hash, path)`` mapping from their frontmatter.
  * An item already built KEEPS its id. If its ``content_hash`` is unchanged the
    note is left byte-untouched (no needless rewrite). If the same ``status_id``
    re-appears with a new ``content_hash`` (an edited tweet), its existing note
    is REWRITTEN IN PLACE -- same id, same folder slot, no duplicate file.
  * A genuinely new ``status_id`` is assigned the next free ``CC.NN`` in its
    category. Unknown/empty category label -> area 00 (Inbox) via
    ``jdid.code_for_label`` returning 0.

Stdlib only. Writes only inside ``--kb``. Re-running on unchanged input is a
no-op (no files rewritten, no ids reallocated).

CLI (frozen):
    python3 build_kb.py --kb <KB>
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

# --- import shim: add lib/ to sys.path, then import the shared modules -------- #
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
)
import schema        # noqa: E402
import frontmatter   # noqa: E402
import jdid          # noqa: E402
import util          # noqa: E402


# --------------------------------------------------------------------------- #
# Title derivation
# --------------------------------------------------------------------------- #
_WS_RE = re.compile(r"\s+")
_SENT_END_RE = re.compile(r"(?<=[.!?])\s")
_URL_RE = re.compile(r"https?://\S+")
# Bare t.co tokens (the image permalink left in a media-only tweet's text), with
# or without a scheme: "https://t.co/abc", "t.co/abc".
_TCO_RE = re.compile(r"(?:https?://)?t\.co/\S+", re.IGNORECASE)
_LEADING_MENTION_RE = re.compile(r"^@[A-Za-z0-9_]{1,15}[\s,:]+")


def _first_sentence(text: str) -> str:
    """First sentence (or first clause) of a block of text, whitespace-folded."""
    text = _WS_RE.sub(" ", (text or "").strip())
    if not text:
        return ""
    parts = _SENT_END_RE.split(text, maxsplit=1)
    return parts[0].strip()


def _strip_title_noise(text: str) -> str:
    """Strip URLs and bare t.co tokens, then a LEADING run of @handle reply
    addressing, leaving the first real clause. Mirrors enrich._strip_for_tldr so
    titles and TL;DRs treat reply headers the same way."""
    t = _URL_RE.sub(" ", text or "")
    t = _TCO_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    while True:
        m = _LEADING_MENTION_RE.match(t)
        if not m:
            break
        t = t[m.end():]
    return _WS_RE.sub(" ", t).strip()


def _media_title(norm: schema.NormalizedBookmark) -> str:
    """Title for a media-only item: the first media's alt, else its ocr, else a
    synthetic "Image from @handle" (never "media by @x", never a t.co link)."""
    for m in norm.media or []:
        alt = (m.alt or "").strip()
        if alt:
            return alt
    for m in norm.media or []:
        ocr = (m.ocr or "").strip()
        if ocr:
            return ocr
    who = norm.author_handle or norm.author_name or "unknown"
    return "Image from {}".format(who)


def derive_title(norm: schema.NormalizedBookmark,
                 enr: Optional[schema.EnrichedBookmark]) -> str:
    """Human-readable note title.

    Order:
      1. the enriched TL;DR's first sentence (curated takeaway),
      2. the bookmark's own first clause of text with URLs, bare t.co tokens, and
         a leading run of @handle reply addressing stripped,
      3. for a media-only item with no usable text, the first media's
         alt -> ocr -> "Image from @handle",
      4. a synthetic "<type> by @author", then "untitled".
    Trimmed to 80 chars on a word boundary.
    """
    candidate = ""
    if getattr(norm, "article_title", ""):
        # Twitter Article: its own title is the best note title.
        candidate = _strip_title_noise(norm.article_title)
    if not candidate and enr is not None and enr.tldr:
        candidate = _strip_title_noise(_first_sentence(enr.tldr))
    if not candidate:
        # First non-empty line of the snapshot text, noise removed.
        raw = norm.text or (norm.thread_texts[0] if norm.thread_texts else "")
        for line in (raw or "").splitlines():
            cleaned = _strip_title_noise(line)
            if cleaned:
                candidate = _first_sentence(cleaned) or cleaned
                break
    if not candidate and (norm.type == "media" or norm.needs_ocr or norm.media):
        # Media-only (or text was just a t.co link): use alt/ocr/"Image from @x".
        candidate = _media_title(norm)
    if not candidate:
        who = norm.author_handle or norm.author_name or "unknown"
        candidate = "{} by {}".format(norm.type or "tweet", who)
    candidate = _WS_RE.sub(" ", candidate).strip().strip(".").strip()
    return _truncate_words(candidate, 80) or "untitled"


def _truncate_words(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.strip()


def kebab_title(title: str, max_len: int = 60) -> str:
    """Filename-safe kebab-case slug for the note title.

    Lowercase, non-word -> hyphen, collapse repeats, trim. Truncated on a
    hyphen boundary so words stay whole. Empty -> "untitled".
    """
    s = (title or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) > max_len:
        s = s[:max_len]
        if "-" in s:
            s = s[:s.rfind("-")]
        s = s.strip("-")
    return s or "untitled"


# --------------------------------------------------------------------------- #
# Date / saved derivation
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def saved_date(norm: schema.NormalizedBookmark) -> str:
    """YYYY-MM-DD from the bookmark's saved_at (capture time), else created_at,
    else empty. Tolerant of full ISO8601 timestamps."""
    for raw in (norm.saved_at, norm.created_at):
        if not raw:
            continue
        m = _DATE_RE.search(raw)
        if m:
            return m.group(1)
        # bare date already
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            return raw[:10]
    return ""


def posted_date(norm: schema.NormalizedBookmark) -> str:
    """YYYY-MM-DD of when the TWEET was posted (created_at), falling back to the
    saved date. This is the date the UI shows; ``saved`` stays for decay/resurface."""
    for raw in (norm.created_at, norm.saved_at):
        if not raw:
            continue
        m = _DATE_RE.search(raw)
        if m:
            return m.group(1)
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            return raw[:10]
    return ""


# --------------------------------------------------------------------------- #
# Body rendering
# --------------------------------------------------------------------------- #
def _gather_links(norm: schema.NormalizedBookmark) -> List[str]:
    """Outbound links for the Links section: the normalized urls, de-duped,
    order-preserving. The tweet's own permalink (norm.url) is already in the
    frontmatter so it is not repeated here."""
    seen = set()
    out: List[str] = []
    for u in norm.urls or []:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _media_line(m: schema.MediaItem) -> str:
    """One '- <kind>: <alt/ocr> (<url>)' line for the Media section. Falls back
    to a short description when neither alt nor ocr is present, never a bare
    t.co link as the description."""
    kind = (m.kind or "media").strip()
    desc = (m.alt or "").strip() or (m.ocr or "").strip()
    url = (m.url or "").strip()
    if desc:
        desc = _WS_RE.sub(" ", desc)
        if url:
            return "- {}: {} ({})".format(kind, desc, url)
        return "- {}: {}".format(kind, desc)
    if url:
        return "- {} ({})".format(kind, url)
    return "- {}".format(kind)


def render_body(norm: schema.NormalizedBookmark,
                enr: Optional[schema.EnrichedBookmark]) -> str:
    """Readable markdown body: TL;DR, Why saved, Key points, Media, Links.

    TL;DR and Why saved always render (with a sensible fallback line so a note
    is never blank). Key points, Media, and Links render only when they have
    content. The TL;DR fallback for a media-only item is the first media's
    alt/ocr (never the bare t.co link the snapshot text holds).
    """
    tldr = (enr.tldr.strip() if (enr and enr.tldr) else "")
    if not tldr:
        # Fall back to a short snapshot of the text so the note is informative.
        # Strip URLs + bare t.co so a media-only tweet never shows a raw short
        # link; if nothing real remains, summarize from media alt/ocr.
        snap = _strip_title_noise(norm.text or "")
        if not snap and (norm.type == "media" or norm.needs_ocr or norm.media):
            snap = _media_title(norm)
        tldr = _truncate_words(snap, 200) if snap else "(no summary available)"

    why = (enr.why_saved.strip() if (enr and enr.why_saved) else "")
    if not why:
        why = "(intent not inferred)"

    parts: List[str] = []
    parts.append("**TL;DR:** {}".format(tldr))
    parts.append("")
    parts.append("**Why saved:** {}".format(why))

    key_points = list(enr.key_points) if enr else []
    if key_points:
        parts.append("")
        parts.append("**Key points:**")
        for kp in key_points:
            kp = _WS_RE.sub(" ", str(kp).strip())
            if kp:
                parts.append("- {}".format(kp))

    if norm.media:
        parts.append("")
        parts.append("**Media:**")
        for m in norm.media:
            parts.append(_media_line(m))

    links = _gather_links(norm)
    if links:
        parts.append("")
        parts.append("**Links:**")
        for ln in links:
            parts.append("- {}".format(ln))

    # Full text: persist the COMPLETE snapshot so a long-form note tweet or an
    # article preview is never lost to the TL;DR. Rendered last (so any stray
    # "**word:**" inside it cannot swallow a later section) and only when it
    # carries more than the TL;DR already shows. Threads join their parts.
    full = norm.text or ""
    if norm.thread_texts:
        full = "\n\n".join(t for t in norm.thread_texts if t.strip()) or full
    full = full.strip()
    bare_tco = full.startswith("https://t.co/") and " " not in full
    if full and not bare_tco and len(full) > len(tldr) + 40:
        parts.append("")
        parts.append("**Full text:**")
        parts.append("")
        parts.append(full)

    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- #
# Reading existing notes (for id stability / in-place updates)
# --------------------------------------------------------------------------- #
class ExistingNote:
    __slots__ = ("status_id", "id", "content_hash", "path")

    def __init__(self, status_id: str, id_: str, content_hash: str, path: str):
        self.status_id = status_id
        self.id = id_
        self.content_hash = content_hash
        self.path = path


def _iter_note_files(kb_root: str):
    """Yield absolute paths of every CC.NN_*.md note under the KB.

    Notes live in ``<area>/<CC_category>/CC.NN_*.md``. We match on the
    ``CC.NN_`` filename prefix so generated aids (``_README.md``,
    ``00_START-HERE.md``) and anything in ``.state`` are skipped.
    """
    name_re = re.compile(r"^\d{2}\.\d{2,3}_.*\.md$")
    state = util.state_dir(kb_root)
    for dirpath, dirnames, filenames in os.walk(kb_root):
        # never descend into machine state
        if os.path.abspath(dirpath) == os.path.abspath(state):
            dirnames[:] = []
            continue
        if ".state" in dirpath.split(os.sep):
            continue
        for fn in filenames:
            if name_re.match(fn):
                yield os.path.join(dirpath, fn)


def scan_existing(kb_root: str) -> Dict[str, ExistingNote]:
    """Map ``status_id -> ExistingNote`` for every note already on disk.

    This is what makes re-runs stable: an already-built bookmark is recognized
    by its status_id, so it keeps its id and its note slot. If two notes somehow
    share a status_id, the first-seen wins (deterministic by sorted path) and
    the duplicate is reported via stderr by the caller.
    """
    out: Dict[str, ExistingNote] = {}
    for path in sorted(_iter_note_files(kb_root)):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        meta, _body = frontmatter.parse(text)
        sid = str(meta.get("status_id", "")).strip()
        nid = str(meta.get("id", "")).strip()
        chash = str(meta.get("content_hash", "")).strip()
        if not sid or not nid:
            continue
        if sid not in out:
            out[sid] = ExistingNote(sid, nid, chash, path)
    return out


# --------------------------------------------------------------------------- #
# Loading enriched + normalized inputs
# --------------------------------------------------------------------------- #
def load_enriched(kb_root: str) -> Dict[str, schema.EnrichedBookmark]:
    """Latest enrichment per status_id across all ``enriched/*.out.jsonl``.

    Batch files are processed in sorted filename order; a later record for the
    same status_id overrides an earlier one (re-enrichment supersedes)."""
    enriched_dir = os.path.join(util.state_dir(kb_root), "enriched")
    out: Dict[str, schema.EnrichedBookmark] = {}
    pattern = os.path.join(enriched_dir, "*.out.jsonl")
    for fpath in sorted(glob.glob(pattern)):
        for row in schema.iter_jsonl(fpath):
            try:
                eb = schema.EnrichedBookmark.from_dict(row)
            except Exception:
                continue
            if eb.status_id:
                out[eb.status_id] = eb
    return out


def load_normalized(kb_root: str) -> List[schema.NormalizedBookmark]:
    """Normalized items in file order. If a status_id appears more than once
    the last record wins (it is the most recent normalization)."""
    path = os.path.join(util.state_dir(kb_root), "new_items.jsonl")
    by_sid: Dict[str, schema.NormalizedBookmark] = {}
    order: List[str] = []
    for row in schema.iter_jsonl(path):
        try:
            nb = schema.NormalizedBookmark.from_dict(row)
        except Exception:
            continue
        if not nb.status_id:
            continue
        if nb.status_id not in by_sid:
            order.append(nb.status_id)
        by_sid[nb.status_id] = nb
    return [by_sid[s] for s in order]


# --------------------------------------------------------------------------- #
# Note path / write
# --------------------------------------------------------------------------- #
def note_path(kb_root: str, folder: str, note_id: str, title_slug: str) -> str:
    """Absolute path ``<KB>/<folder>/<id>_<slug>.md``."""
    filename = "{}_{}.md".format(note_id, title_slug)
    return os.path.join(kb_root, folder, filename)


def _canonical_status_url(status_id: str) -> str:
    """Handle-INDEPENDENT permalink https://x.com/i/status/<id>. Derived purely
    from status_id so it never churns the content hash. Empty id -> ""."""
    return "https://x.com/i/status/{}".format(status_id) if status_id else ""


def _media_frontmatter(norm: schema.NormalizedBookmark) -> List[Dict[str, str]]:
    """The inline-YAML ``media`` list: a flat ``{kind, url, alt}`` map per item.
    OCR is intentionally DROPPED from frontmatter (kept only in .state to stay
    token-lean); only kind, url, alt are written."""
    out: List[Dict[str, str]] = []
    for m in norm.media or []:
        out.append({
            "kind": (m.kind or "").strip(),
            "url": (m.url or "").strip(),
            "alt": (m.alt or "").strip(),
        })
    return out


def _thumb_url(norm: schema.NormalizedBookmark) -> str:
    """First media item's image/poster URL (or "" when there is no media)."""
    for m in norm.media or []:
        if (m.url or "").strip():
            return m.url.strip()
    return ""


def _media_alt(norm: schema.NormalizedBookmark) -> str:
    """First non-empty alt, else first non-empty ocr, else ""."""
    for m in norm.media or []:
        if (m.alt or "").strip():
            return m.alt.strip()
    for m in norm.media or []:
        if (m.ocr or "").strip():
            return m.ocr.strip()
    return ""


def _engagement_frontmatter(norm: schema.NormalizedBookmark) -> Dict[str, int]:
    """Inline flat ``str->int`` engagement map with SHORT keys.
    Maps the normalized *_count fields onto likes/retweets/replies/quotes/views.
    Insertion order is fixed so the emitted map is deterministic."""
    return {
        "likes": int(norm.like_count or 0),
        "retweets": int(norm.retweet_count or 0),
        "replies": int(norm.reply_count or 0),
        "quotes": int(norm.quote_count or 0),
        "views": int(norm.view_count or 0),
    }


def build_note_text(note_id: str,
                    norm: schema.NormalizedBookmark,
                    enr: Optional[schema.EnrichedBookmark],
                    title: str,
                    thumb_override: str = "") -> str:
    """Render the full note (frontmatter + body) as a string.

    Every key in ``frontmatter.FIELD_ORDER`` is emitted (so the note's key order
    equals FIELD_ORDER exactly): empty media emits ``media: []`` and the
    engagement map is always present. Of the added keys only ``media`` is hashed
    (it was already in ``hashing._MEANINGFUL_FIELDS``); canonical_url / lang /
    media_count / thumb / media_alt / engagement are derived or drift and are NOT
    hashed, so a re-run on unchanged input stays a byte-identical no-op and a
    like-count-only delta is still ``skipped``.

    ``thumb_override`` (set only under ``--cache-media``) replaces the ``thumb``
    scalar with a RELATIVE ``.media/...`` path. The ``media`` list keeps the
    original CDN URLs (those ARE hashed; rewriting them would churn the hash).
    """
    tags: List[str] = list(enr.tags) if (enr and enr.tags) else []
    category = (enr.category if (enr and enr.category) else "")
    # content_hash: prefer the normalized hash; enriched echoes the same value.
    chash = norm.content_hash or (enr.content_hash if enr else "")

    media_list = _media_frontmatter(norm)
    # canonical_url: prefer the value carried on the record (ingest sets it);
    # fall back to deriving it from status_id so hand-written/older notes still
    # get a robust open-original target.
    canonical = (norm.canonical_url or "").strip() or \
        _canonical_status_url(norm.status_id)

    meta = {
        "id": note_id,
        "status_id": norm.status_id,
        "title": title,
        "url": norm.url,
        "canonical_url": canonical,
        "author": norm.author_handle or norm.author_name,
        "posted": posted_date(norm),
        "saved": saved_date(norm),
        "type": norm.type or "tweet",
        "lang": norm.lang or "",
        "category": category,
        "tags": tags,
        "media_count": len(media_list),
        "thumb": thumb_override or _thumb_url(norm),
        "media_alt": _media_alt(norm),
        "engagement": _engagement_frontmatter(norm),
        "media": media_list,
        "content_hash": chash,
    }
    body = render_body(norm, enr)
    return frontmatter.dump(meta, body, order=frontmatter.FIELD_ORDER)


# --------------------------------------------------------------------------- #
# Optional local media cache (--cache-media, default OFF, network behind a flag)
# --------------------------------------------------------------------------- #
_PBS_MEDIA_RE = re.compile(r"https?://pbs\.twimg\.com/media/", re.IGNORECASE)


def _media_cache_dir(kb_root: str) -> str:
    """``<KB>/.media`` — the only place --cache-media writes (still inside --kb)."""
    return os.path.join(kb_root, ".media")


def _safe_cache_key(status_id: str) -> str:
    """A filesystem-safe component for a cache filename. Real X status ids are
    numeric, but ingest accepts whatever ``rest_id`` / a fixture supplies, so we
    NEVER trust it for a path: strip everything but ``[A-Za-z0-9_-]`` so a crafted
    id like ``../../etc`` cannot escape ``<KB>/.media/`` (hard rule: scripts write
    only inside --kb). Empty after stripping -> ``"item"``."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(status_id or ""))
    return safe or "item"


def _small_still_url(url: str) -> str:
    """Request the 680px 'small' still for a pbs.twimg.com/media/ URL when it has
    no existing name=/format= query; otherwise leave the URL untouched. Mirrors
    the UI's rewrite so the cached byte stream matches the UI."""
    if not _PBS_MEDIA_RE.match(url):
        return url
    low = url.lower()
    if "name=" in low or "format=" in low:
        return url
    sep = "&" if "?" in url else "?"
    return "{}{}format=jpg&name=small".format(url, sep)


def _cache_ext(url: str) -> str:
    """A safe file extension for a cached still. pbs media URLs carry the format
    in either the path ('.jpg') or a 'format=' query; default to 'jpg'."""
    base = url.split("?", 1)[0]
    _, dot, ext = base.rpartition(".")
    if dot and 1 <= len(ext) <= 5 and ext.isalnum():
        return ext.lower()
    m = re.search(r"[?&]format=([A-Za-z0-9]{1,5})", url)
    if m:
        return m.group(1).lower()
    return "jpg"


def cache_media_for(kb_root: str, norm: schema.NormalizedBookmark,
                    opener=None) -> str:
    """Download the FIRST media item's small still under ``<KB>/.media/`` and
    return its RELATIVE ``.media/<status_id>_<n>.<ext>`` path (forward slashes),
    or "" when there is nothing to cache / the fetch fails.

    Idempotent: an already-present cache file is reused (no re-download), so a
    second --cache-media run is byte-stable. Writes ONLY under ``<KB>/.media``.
    ``opener`` lets tests inject a fake fetcher so the suite never hits the net.
    """
    url = _thumb_url(norm)
    if not url:
        return ""
    n = 0  # first media item with a URL
    ext = _cache_ext(_small_still_url(url))
    # NEVER interpolate a raw status_id into a path: a crafted id (``../..``)
    # would otherwise escape <KB>/.media/. Sanitize to a safe slug first.
    key = _safe_cache_key(norm.status_id)
    rel = ".media/{}_{}.{}".format(key, n, ext)
    abs_path = os.path.join(kb_root, ".media",
                            "{}_{}.{}".format(key, n, ext))
    if os.path.exists(abs_path):
        return rel  # idempotent reuse
    fetch = opener or _http_get_bytes
    try:
        data = fetch(_small_still_url(url))
    except Exception:
        return ""  # rotted URL / blocked / offline -> fall back to no thumb cache
    if not data:
        return ""
    # Create <KB>/.media only once there is something to write, so a failed /
    # offline fetch leaves no stray empty directory.
    util.ensure_dir(_media_cache_dir(kb_root))
    try:
        util.atomic_write_bytes(abs_path, data)
    except Exception:
        return ""
    return rel


def _http_get_bytes(url: str, timeout: float = 15.0) -> bytes:
    """Fetch ``url`` with stdlib urllib (no referer, matches the CDN's tokenless
    expectations). Used ONLY behind --cache-media."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "tweetmark/1.0", "Referer": ""})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - flagged
        return resp.read()


# --------------------------------------------------------------------------- #
# Main build
# --------------------------------------------------------------------------- #
def build(kb_root: str, log=None, cache_media: bool = False,
          media_opener=None) -> Dict[str, int]:
    """Build/update all notes. Returns counts {created, updated, unchanged}.

    ``cache_media`` (default OFF; mirrors collect.py's explicit-network rule)
    downloads each item's first media still under ``<KB>/.media/`` and rewrites
    that note's ``thumb`` scalar to the relative cache path. The default agent
    path stays network-free. ``media_opener`` lets a test inject a fetcher so the
    cache path can be exercised without touching the network.
    """
    if log is None:
        log = lambda *a, **k: None  # noqa: E731

    util.ensure_dir(kb_root)

    existing = scan_existing(kb_root)
    enriched = load_enriched(kb_root)
    normalized = load_normalized(kb_root)

    # All ids already in use across the KB -> feed jdid.next_id so new ids never
    # collide. We grow this set as we allocate within this run too.
    used_ids: List[str] = [en.id for en in existing.values()]

    counts = {"created": 0, "updated": 0, "unchanged": 0}

    for norm in normalized:
        sid = norm.status_id
        enr = enriched.get(sid)
        chash = norm.content_hash or (enr.content_hash if enr else "")
        title = derive_title(norm, enr)
        slug = kebab_title(title)
        thumb_override = ""
        if cache_media and norm.media:
            thumb_override = cache_media_for(kb_root, norm, opener=media_opener)

        prior = existing.get(sid)
        if prior is not None:
            # Already built. Keep the assigned id; rewrite in place only if the
            # content changed (or the slug/filename changed).
            note_id = prior.id
            cc = jdid.code_for_label(enr.category if enr else "")
            # Honor the category the note was filed under by keeping its id's CC;
            # but the folder still follows the (possibly updated) category label.
            folder = jdid.folder_for_code(
                int(note_id.split(".")[0]),
                enr.category if (enr and enr.category) else "",
            )
            target = note_path(kb_root, folder, note_id, slug)
            text = build_note_text(note_id, norm, enr, title, thumb_override)

            unchanged = (
                prior.content_hash == chash
                and os.path.abspath(prior.path) == os.path.abspath(target)
                and _file_text(prior.path) == text
            )
            if unchanged:
                counts["unchanged"] += 1
                continue

            # Rewrite in place. If the filename/folder changed (edited title or
            # re-categorized), remove the stale file so there is never a dup.
            util.ensure_dir(os.path.dirname(target))
            util.atomic_write(target, text)
            if os.path.abspath(prior.path) != os.path.abspath(target):
                _safe_remove(prior.path)
            prior.path = target
            prior.content_hash = chash
            counts["updated"] += 1
            log("updated", note_id, sid, target)
            continue

        # New bookmark: allocate the next free CC.NN in its category.
        note_id, folder = jdid.assign(enr.category if enr else "", used_ids)
        used_ids.append(note_id)
        target = note_path(kb_root, folder, note_id, slug)
        util.ensure_dir(os.path.dirname(target))
        text = build_note_text(note_id, norm, enr, title, thumb_override)
        util.atomic_write(target, text)
        existing[sid] = ExistingNote(sid, note_id, chash, target)
        counts["created"] += 1
        log("created", note_id, sid, target)

    return counts


def _file_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="build_kb.py",
        description="Build Johnny.Decimal notes from enriched + normalized "
                    "bookmark records.",
    )
    ap.add_argument(
        "--kb", required=True,
        help="Knowledge-base root (all output is written inside it).",
    )
    ap.add_argument(
        "--cache-media", action="store_true",
        help="OPT-IN: download each item's first media still (name=small) into "
             "<KB>/.media/ and point its thumb at the local copy. OFF by default; "
             "this is the ONLY build_kb path that touches the network, and it "
             "writes solely under <KB>/.media/.",
    )
    args = ap.parse_args(argv)

    kb_root = os.path.expanduser(args.kb)

    def _log(action, note_id, sid, path):
        rel = os.path.relpath(path, kb_root)
        sys.stderr.write("{:8s} {:>7s}  status_id={}  {}\n".format(
            action, note_id, sid, rel))

    counts = build(kb_root, log=_log, cache_media=args.cache_media)
    sys.stderr.write(
        "build_kb: {created} created, {updated} updated, {unchanged} "
        "unchanged\n".format(**counts)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
