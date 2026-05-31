#!/usr/bin/env python3
"""ingest.py — deterministic normalize + dedup (zero LLM, zero tokens).

Reads ``.state/bookmarks_raw.jsonl`` (RawBookmark lines from collect.py) plus the
``.state/seen.tsv`` ledger, turns each raw GraphQL tweet entry into a clean
``NormalizedBookmark``, and decides what is new:

  * unknown ``status_id``                 -> new      (emit, ledger gets the hash)
  * known ``status_id`` w/ different hash  -> changed  (emit, ledger updated)
  * known ``status_id`` w/ same hash       -> skip     (not emitted, ledger kept)

It writes ``.state/new_items.jsonl`` (new/changed ONLY) and atomically rewrites
``.state/seen.tsv``. Cursor-only raw lines (empty ``status_id``) are ignored.

Extraction is purely deterministic: author handle/name, created_at (ISO8601),
fully expanded outbound URLs, mentions, hashtags, media descriptors (media-only
tweets are flagged ``needs_ocr``), the quoted/referenced tweet projection,
conversation_id, and THREAD detection (a same-author reply chain sharing a
``conversation_id`` collapses into one item with ordered ``thread_texts``).

Idempotency: a 2nd run on the same raw input emits 0 new items and leaves
``seen.tsv`` byte-identical, because the hash is stable over meaningful content
and the classification is keyed on ``(status_id, content_hash)``.

Stdlib only. CLI (frozen):  python3 ingest.py --kb <KB>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# --- import shim: add scripts/lib to sys.path, then import the shared lib ---- #
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import schema   # noqa: E402
import hashing  # noqa: E402
import util     # noqa: E402


# --------------------------------------------------------------------------- #
# created_at conversion. X serves the legacy Twitter format
#   "Mon May 12 09:00:00 +0000 2026"
# We convert to a stable ISO8601 UTC string ("2026-05-12T09:00:00Z") with no
# external deps. If parsing fails we keep the original string verbatim so the
# hash stays stable and nothing is silently dropped.
# --------------------------------------------------------------------------- #
_MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}


def _to_iso8601(twitter_ts: str) -> str:
    """Convert "Mon May 12 09:00:00 +0000 2026" -> "2026-05-12T09:00:00Z".

    Returns the input unchanged if it is empty, already ISO-ish, or unparseable.
    Pure string work (no datetime/tz deps), deterministic.
    """
    s = (twitter_ts or "").strip()
    if not s:
        return ""
    # Already ISO8601 (e.g. came pre-normalized) -> leave as-is.
    if "T" in s and "-" in s[:8]:
        return s
    parts = s.split()
    # Expected: [DOW, Mon, DD, HH:MM:SS, +0000, YYYY]
    if len(parts) != 6:
        return s
    _dow, mon, day, hms, offset, year = parts
    mm = _MONTHS.get(mon)
    if mm is None or not day.isdigit() or not year.isdigit():
        return s
    day = day.zfill(2)
    # Honor a non-UTC offset by emitting it explicitly; +0000 -> trailing Z.
    if offset in ("+0000", "+00:00", "Z", "-0000"):
        suffix = "Z"
    elif len(offset) == 5 and offset[0] in "+-" and offset[1:].isdigit():
        suffix = "{}{}:{}".format(offset[0], offset[1:3], offset[3:5])
    else:
        suffix = "Z"
    return "{year}-{mm}-{day}T{hms}{suffix}".format(
        year=year, mm=mm, day=day, hms=hms, suffix=suffix)


# --------------------------------------------------------------------------- #
# GraphQL navigation helpers. The validated nested path is
#   raw.content.itemContent.tweet_results.result
# with author at result.core.user_results.result and the body in result.legacy.
# Everything below defends against missing keys so a drifted/partial entry is
# skipped rather than crashing the whole ingest.
# --------------------------------------------------------------------------- #
def _tweet_result(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull the tweet ``result`` object out of a RawBookmark ``raw`` entry."""
    try:
        item = raw["content"]["itemContent"]
    except (KeyError, TypeError):
        return None
    res = (item.get("tweet_results") or {}).get("result")
    if isinstance(res, dict):
        return res
    return None


def _author(result: Dict[str, Any]) -> Tuple[str, str]:
    """Return (handle_with_at, display_name). Prefers core.*, falls back to
    legacy.* (X is mid-migration and populates either)."""
    user = (((result.get("core") or {}).get("user_results") or {}).get("result")
            or {})
    core = user.get("core") or {}
    leg = user.get("legacy") or {}
    handle = core.get("screen_name") or leg.get("screen_name") or ""
    name = core.get("name") or leg.get("name") or ""
    handle = ("@" + handle) if handle else ""
    return handle, name


def _expanded_urls(entities: Dict[str, Any]) -> List[str]:
    """Fully expanded outbound links from entities.urls[].expanded_url
    (falling back to the t.co url if no expansion is present)."""
    out: List[str] = []
    for u in (entities.get("urls") or []):
        if not isinstance(u, dict):
            continue
        link = u.get("expanded_url") or u.get("url") or ""
        if link and link not in out:
            out.append(link)
    return out


def _mentions(entities: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for m in (entities.get("user_mentions") or []):
        if not isinstance(m, dict):
            continue
        sn = m.get("screen_name") or ""
        if sn:
            handle = "@" + sn
            if handle not in out:
                out.append(handle)
    return out


def _hashtags(entities: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for h in (entities.get("hashtags") or []):
        if not isinstance(h, dict):
            continue
        t = h.get("text") or ""
        if t and t not in out:
            out.append(t)
    return out


def _media(legacy: Dict[str, Any]) -> List[schema.MediaItem]:
    """Media descriptors from extended_entities.media (preferred, carries all
    items) with a fallback to entities.media. OCR is left empty (enrich fills
    it later); alt-text from X is captured if present."""
    ext = legacy.get("extended_entities") or {}
    media_list = ext.get("media")
    if not media_list:
        media_list = (legacy.get("entities") or {}).get("media")
    out: List[schema.MediaItem] = []
    for m in (media_list or []):
        if not isinstance(m, dict):
            continue
        kind = m.get("type") or ""
        # video / animated_gif carry the real media at media_url_https too.
        url = m.get("media_url_https") or m.get("media_url") or m.get("url") or ""
        alt = m.get("ext_alt_text") or ""
        out.append(schema.MediaItem(kind=kind, url=url, alt=alt, ocr=""))
    return out


def _media_only(text: str, media: List[schema.MediaItem],
                urls: List[str]) -> bool:
    """A tweet is 'media-only' (needs OCR/alt-text to stay searchable) when it
    has media but the visible text is empty or is just the media's t.co link.
    The raw full_text for an image tweet is typically the bare t.co URL."""
    if not media:
        return False
    stripped = (text or "").strip()
    if not stripped:
        return True
    # full_text == only a t.co short link (the image permalink) -> media-only.
    tokens = stripped.split()
    if len(tokens) == 1 and tokens[0].startswith("https://t.co/"):
        return True
    return False


# --------------------------------------------------------------------------- #
# Quoted-tweet projection. We store a compact nested dict (not a full
# NormalizedBookmark) so build_kb/enrich can summarize "both the comment and the
# quoted tweet". The quoted tweet lives at result.quoted_status_result.result.
# --------------------------------------------------------------------------- #
def _project_quoted(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    qres = (result.get("quoted_status_result") or {}).get("result")
    if not isinstance(qres, dict):
        return None
    leg = qres.get("legacy") or {}
    handle, name = _author(qres)
    qid = qres.get("rest_id") or leg.get("id_str") or ""
    entities = leg.get("entities") or {}
    return {
        "status_id": str(qid),
        "author_handle": handle,
        "author_name": name,
        "created_at": _to_iso8601(leg.get("created_at", "")),
        "text": leg.get("full_text", "") or "",
        "url": _canonical_url(handle, str(qid)),
        "urls": _expanded_urls(entities),
        "mentions": _mentions(entities),
        "hashtags": _hashtags(entities),
        "lang": leg.get("lang", "") or "",
    }


def _canonical_url(handle: str, status_id: str) -> str:
    """https://x.com/<handle>/status/<id>. Handle is stored with a leading @,
    which the URL drops. Falls back to the i/web form if the handle is unknown.

    NOTE: this is the HASHED, human-readable permalink (it is in
    hashing._MEANINGFUL_FIELDS as ``url``); it is deliberately left unchanged so
    no note re-hashes. The robust open-original target is the separate, un-hashed
    ``canonical_url`` produced by _canonical_status_url below."""
    if not status_id:
        return ""
    h = handle[1:] if handle.startswith("@") else handle
    if h:
        return "https://x.com/{}/status/{}".format(h, status_id)
    return "https://x.com/i/web/status/{}".format(status_id)


def _canonical_status_url(status_id: str) -> str:
    """https://x.com/i/status/<status_id> — handle-independent permalink.

    X redirects this form to the live handle, so renamed / suspended / deleted /
    fake handles still resolve. Derived purely from status_id, so it never churns
    the content hash (it is NOT in hashing._MEANINGFUL_FIELDS). Delegates to the
    shared lib helper so there is one source of truth for the i/status form."""
    return util.canonical_url(status_id)


def _engagement(legacy: Dict[str, Any]) -> Dict[str, int]:
    """Extract the five engagement counts from a tweet's legacy block.

    Counts that are absent in fixtures (reply_count / quote_count) default to 0;
    view_count comes from legacy.views.count (a string on the wire). These are
    EXCLUDED from the content hash on purpose: engagement drifts constantly and
    must not retrigger re-enrichment (a like-count tick stays 'skipped')."""
    def _i(v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    views = (legacy.get("views") or {}).get("count")
    return {
        "like_count": _i(legacy.get("favorite_count")),
        "retweet_count": _i(legacy.get("retweet_count")),
        "reply_count": _i(legacy.get("reply_count")),
        "quote_count": _i(legacy.get("quote_count")),
        "view_count": _i(views),
    }


# --------------------------------------------------------------------------- #
# Per-entry normalization (one RawBookmark -> one NormalizedBookmark, no type
# precedence applied yet; thread collapsing + final typing happen afterwards).
# --------------------------------------------------------------------------- #
def _article(result: Dict[str, Any]):
    """Twitter Article content lives in result.article.article_results.result
    (title + preview_text + cover_media), NOT in legacy.full_text (which is just
    the t.co permalink). Returns (title, summary, cover_MediaItem|None) or None."""
    art = ((result.get("article") or {}).get("article_results") or {}).get("result")
    if not isinstance(art, dict):
        return None
    title = (art.get("title") or "").strip()
    summary = (art.get("preview_text") or "").strip()
    if not title and not summary:
        return None
    cm = art.get("cover_media") or {}
    info = cm.get("media_info") or {}
    img = (info.get("original_img_url") or cm.get("media_url_https")
           or (info.get("preview_image") or {}).get("url") or "")
    cover = schema.MediaItem(kind="photo", url=img, alt=title) if img else None
    return title, summary, cover


def _normalize_one(rb: schema.RawBookmark) -> Optional[schema.NormalizedBookmark]:
    result = _tweet_result(rb.raw)
    if result is None:
        return None
    typename = result.get("__typename", "")
    deleted = (typename == "TweetTombstone")

    legacy = result.get("legacy") or {}
    handle, name = _author(result)
    status_id = rb.status_id or str(result.get("rest_id") or "")
    if not status_id:
        return None

    # Long-form ("note") tweets keep their FULL text + entities under note_tweet;
    # legacy.full_text is TRUNCATED (~277 chars) for them. Prefer note_tweet.
    note = (((result.get("note_tweet") or {}).get("note_tweet_results")
             or {}).get("result") or {})
    note_text = (note.get("text") or "").strip()
    text = note_text or legacy.get("full_text", "") or ""
    entities = (note.get("entity_set") if note_text else None) \
        or legacy.get("entities") or {}
    urls = _expanded_urls(entities)
    mentions = _mentions(entities)
    hashtags = _hashtags(entities)
    media = _media(legacy)
    quoted = _project_quoted(result)
    conversation_id = str(legacy.get("conversation_id_str") or status_id)
    needs_ocr = _media_only(text, media, urls)
    eng = _engagement(legacy)

    nb = schema.NormalizedBookmark(
        status_id=status_id,
        url=_canonical_url(handle, status_id),
        canonical_url=_canonical_status_url(status_id),
        author_handle=handle,
        author_name=name,
        created_at=_to_iso8601(legacy.get("created_at", "")),
        saved_at=rb.captured_at,
        text=text,
        type="tweet",  # provisional; set by _apply_type after thread collapse
        lang=legacy.get("lang", "") or "",
        conversation_id=conversation_id,
        urls=urls,
        mentions=mentions,
        hashtags=hashtags,
        media=media,
        thread_texts=[],
        quoted=quoted,
        needs_ocr=needs_ocr,
        deleted=deleted,
        like_count=eng["like_count"],
        retweet_count=eng["retweet_count"],
        reply_count=eng["reply_count"],
        quote_count=eng["quote_count"],
        view_count=eng["view_count"],
    )
    art = _article(result)
    if art:
        nb.article_title, nb.article_summary, _cover = art
        if _cover and not nb.media:
            nb.media.append(_cover)
            nb.needs_ocr = _media_only(nb.text, nb.media, nb.urls)
        _t = nb.text.strip()
        if (not _t) or (_t.startswith("https://t.co/") and " " not in _t):
            # full_text was just the article permalink; use the article content
            # so the note body, title and TL;DR have real words.
            nb.text = (nb.article_title
                       + (". " + nb.article_summary if nb.article_summary else "")).strip()
    # Carry the in-reply-to + sort hints on the side for thread grouping. These
    # are not schema fields, so they live in a transient attribute (dropped
    # before emit) — the emitted record only ever holds contract fields.
    nb._reply_to = str(legacy.get("in_reply_to_status_id_str") or "")  # type: ignore[attr-defined]
    nb._sort_index = rb.sort_index or ""  # type: ignore[attr-defined]
    return nb


# --------------------------------------------------------------------------- #
# Type precedence:
#   deleted > thread > quote > media > link > tweet
# A deleted snapshot stays 'deleted'; a thread that also has media is 'thread'.
# --------------------------------------------------------------------------- #
def _apply_type(nb: schema.NormalizedBookmark) -> None:
    if nb.deleted:
        nb.type = "deleted"
    elif nb.thread_texts:
        nb.type = "thread"
    elif nb.quoted:
        nb.type = "quote"
    elif nb.media:
        nb.type = "media"
    elif nb.urls:
        nb.type = "link"
    else:
        nb.type = "tweet"


# --------------------------------------------------------------------------- #
# Thread detection. A "thread" here is a same-author reply chain captured in the
# bookmarks: two or more bookmarked tweets that share a conversation_id AND the
# same author, where the later parts reply within that conversation. We collapse
# them into ONE NormalizedBookmark (the root) carrying ordered ``thread_texts``.
#
# Ordering: by sort_index when available (timeline order, higher = newer at the
# top), else by created_at, else by status_id — all string-stable so the result
# is deterministic. The collapsed item keeps the root tweet's identity (lowest
# status_id in the chain == the conversation root) for a stable canonical URL.
#
# A single bookmarked tweet that merely *has* a conversation_id (its own id) is
# NOT a thread; collapsing only triggers when 2+ same-author items group.
# --------------------------------------------------------------------------- #
def _thread_sort_key(nb: schema.NormalizedBookmark) -> Tuple[int, str, str]:
    """Order thread parts oldest-first. sortIndex descends down the timeline
    (root highest), so ascending created_at / ascending id gives reading order.
    We sort by (created_at, status_id) which is the tweet's true chronology;
    sort_index is a tie-breaker only."""
    return (0, nb.created_at or "", nb.status_id)


def _collapse_threads(items: List[schema.NormalizedBookmark]
                      ) -> List[schema.NormalizedBookmark]:
    """Group same-author shared-conversation chains into single threaded items.

    Returns a new list where each multi-part chain is represented once (the
    conversation root), order otherwise preserved by first appearance.
    Deleted snapshots are never merged into a thread (they stay standalone so
    the 'deleted' type and snapshot are preserved verbatim).
    """
    # Build groups keyed on (conversation_id, author_handle). Only non-deleted
    # items with a real conversation_id participate.
    groups: Dict[Tuple[str, str], List[schema.NormalizedBookmark]] = {}
    order: List[Any] = []          # preserves first-appearance order of outputs
    placed: Dict[int, bool] = {}   # id(nb) -> already represented in `order`

    for nb in items:
        key = (nb.conversation_id, nb.author_handle)
        if nb.deleted or not nb.conversation_id or not nb.author_handle:
            # Standalone: emit as-is, do not group.
            order.append(("solo", nb))
            placed[id(nb)] = True
            continue
        if key not in groups:
            groups[key] = []
            order.append(("group", key))
        groups[key].append(nb)

    out: List[schema.NormalizedBookmark] = []
    for kind, payload in order:
        if kind == "solo":
            out.append(payload)
            continue
        members = groups[payload]
        if len(members) == 1:
            # A lone bookmarked tweet in its own conversation: not a thread.
            out.append(members[0])
            continue
        # Real thread: order parts chronologically and collapse onto the root.
        members_sorted = sorted(members, key=_thread_sort_key)
        root = members_sorted[0]
        root.thread_texts = [m.text for m in members_sorted]
        # The root's snapshot text is the first part; full text stays on `text`.
        # Merge entity sets across parts (deterministic, de-duplicated, order
        # of first appearance) so the thread stays searchable as one unit.
        for m in members_sorted[1:]:
            for u in m.urls:
                if u not in root.urls:
                    root.urls.append(u)
            for men in m.mentions:
                if men not in root.mentions:
                    root.mentions.append(men)
            for h in m.hashtags:
                if h not in root.hashtags:
                    root.hashtags.append(h)
            for med in m.media:
                root.media.append(med)
            # A quoted tweet anywhere in the chain promotes to the root if it
            # has none yet.
            if root.quoted is None and m.quoted is not None:
                root.quoted = m.quoted
        # Re-evaluate needs_ocr across the merged media/text.
        if root.media and not (root.text or "").strip() and not root.thread_texts:
            root.needs_ocr = True
        out.append(root)
    return out


# --------------------------------------------------------------------------- #
# Pipeline.
# --------------------------------------------------------------------------- #
def ingest(kb_root: str) -> Dict[str, int]:
    """Run the ingest stage. Returns a small stats dict for the CLI/tests."""
    sdir = util.state_dir(kb_root)
    util.ensure_dir(sdir)
    raw_path = os.path.join(sdir, "bookmarks_raw.jsonl")
    seen_path = os.path.join(sdir, "seen.tsv")
    out_path = os.path.join(sdir, "new_items.jsonl")

    seen = util.read_seen(seen_path)

    # 1) Normalize every tweet entry (skip cursor-only + undecodable lines).
    normalized: List[schema.NormalizedBookmark] = []
    seen_status_ids = set()
    for row in schema.iter_jsonl(raw_path):
        rb = schema.RawBookmark.from_dict(row)
        if not rb.status_id:           # cursor-only line
            continue
        nb = _normalize_one(rb)
        if nb is None:
            continue
        # If the same status_id appears twice in the raw log (re-capture), keep
        # the first occurrence so the run is order-stable; raw is append-only so
        # the earliest line is the original capture.
        if nb.status_id in seen_status_ids:
            continue
        seen_status_ids.add(nb.status_id)
        normalized.append(nb)

    # 2) Collapse same-author reply chains into single threaded items.
    collapsed = _collapse_threads(normalized)

    # 3) Final type, content hash, new/changed/skip classification.
    new_seen: Dict[str, str] = dict(seen)
    emitted: List[schema.NormalizedBookmark] = []
    n_new = n_changed = n_skip = 0

    for nb in collapsed:
        _apply_type(nb)
        # Drop transient grouping attrs before hashing/emitting (defensive — the
        # hash only reads contract fields anyway, but the emitted dict must be
        # exactly the schema shape).
        for attr in ("_reply_to", "_sort_index"):
            if hasattr(nb, attr):
                delattr(nb, attr)
        h = hashing.content_hash(nb)
        nb.content_hash = h
        prev = seen.get(nb.status_id)
        if prev is None:
            nb.change = "new"
            n_new += 1
            emitted.append(nb)
        elif prev != h:
            nb.change = "changed"
            n_changed += 1
            emitted.append(nb)
        else:
            n_skip += 1
        new_seen[nb.status_id] = h

    # 4) Atomic writes. new_items.jsonl holds new/changed only; seen.tsv is the
    # full ledger. Both staged to a temp file then renamed, so an interrupted
    # run never corrupts state.
    payload = "".join(
        json.dumps(nb.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        for nb in emitted
    )
    util.atomic_write(out_path, payload)
    util.write_seen(seen_path, new_seen)

    return {
        "raw_tweets": len(normalized),
        "items": len(collapsed),
        "new": n_new,
        "changed": n_changed,
        "skipped": n_skip,
        "emitted": len(emitted),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ingest.py",
        description="Normalize raw bookmarks and emit new/changed items.")
    ap.add_argument("--kb", required=True,
                    help="knowledge-base root (the .state/ dir lives under it)")
    args = ap.parse_args(argv)

    kb_root = os.path.expanduser(args.kb)
    raw_path = os.path.join(util.state_dir(kb_root), "bookmarks_raw.jsonl")
    if not os.path.exists(raw_path):
        sys.stderr.write(
            "ingest: no raw capture at {} (run collect.py first)\n".format(raw_path))
        return 1

    stats = ingest(kb_root)
    sys.stdout.write(
        "ingest: {new} new, {changed} changed, {skipped} unchanged "
        "({items} items from {raw_tweets} tweets)\n".format(**stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
