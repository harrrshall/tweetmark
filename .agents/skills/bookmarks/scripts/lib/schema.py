"""Data schemas for the bookmarks pipeline.

Stdlib only. Defines the four records that flow through the pipeline plus
JSONL read/write helpers. Every dataclass has ``from_dict`` / ``to_dict`` so
the on-disk JSON shape is explicit and stable across agents.

Record flow:
    RawBookmark        collect.py  -> .state/bookmarks_raw.jsonl
    NormalizedBookmark ingest.py   -> .state/new_items.jsonl
    EnrichedBookmark   enrich.py   -> .state/enriched/NNN.out.jsonl
    IndexRow           index.py    -> INDEX.tsv (one tab-separated line)

The dict forms are the binding contract. Unknown keys are
preserved on Raw records (X rotates fields) but ignored elsewhere.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional


# --------------------------------------------------------------------------- #
# RawBookmark: minimally-shaped capture of one tweet entry from the GraphQL
# Bookmarks timeline. We keep the full original entry under ``raw`` so no field
# is ever lost (X rotates them every few weeks), and lift the few stable
# identifiers we always need for dedup and pagination up to the top level.
# --------------------------------------------------------------------------- #
@dataclass
class RawBookmark:
    status_id: str                       # stable tweet id, e.g. "1730000000000000000"
    entry_id: str                        # the GraphQL entryId, e.g. "tweet-1730..."
    sort_index: str = ""                 # entry sortIndex (timeline ordering)
    captured_at: str = ""                # ISO8601 UTC when collect.py saw it
    page: int = 0                        # which fetch page it came from (0-based)
    cursor: str = ""                     # bottom cursor active when captured
    raw: Dict[str, Any] = field(default_factory=dict)  # full original entry

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status_id": self.status_id,
            "entry_id": self.entry_id,
            "sort_index": self.sort_index,
            "captured_at": self.captured_at,
            "page": self.page,
            "cursor": self.cursor,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RawBookmark":
        return cls(
            status_id=str(d.get("status_id", "")),
            entry_id=str(d.get("entry_id", "")),
            sort_index=str(d.get("sort_index", "")),
            captured_at=str(d.get("captured_at", "")),
            page=int(d.get("page", 0) or 0),
            cursor=str(d.get("cursor", "")),
            raw=d.get("raw", {}) or {},
        )


# --------------------------------------------------------------------------- #
# MediaItem: one image/video/gif attached to a tweet. ``alt`` and ``ocr`` are
# populated downstream; media-only tweets set needs_ocr so they stay searchable.
# --------------------------------------------------------------------------- #
@dataclass
class MediaItem:
    kind: str = ""        # "photo" | "video" | "animated_gif"
    url: str = ""         # media URL (https)
    alt: str = ""         # alt-text from X if present
    ocr: str = ""         # OCR text (filled by enrich, optional)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "url": self.url, "alt": self.alt, "ocr": self.ocr}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MediaItem":
        return cls(
            kind=str(d.get("kind", "")),
            url=str(d.get("url", "")),
            alt=str(d.get("alt", "")),
            ocr=str(d.get("ocr", "")),
        )


# --------------------------------------------------------------------------- #
# NormalizedBookmark: deterministic extraction from a RawBookmark. No LLM, no
# tokens. This is what enrich.py reads. ``thread_texts`` collapses a same-author
# reply chain; ``quoted`` carries a nested NormalizedBookmark-ish dict.
# --------------------------------------------------------------------------- #
@dataclass
class NormalizedBookmark:
    status_id: str
    url: str = ""
    canonical_url: str = ""              # https://x.com/i/status/<id> (handle-free)
    author_handle: str = ""              # "@someone"
    author_name: str = ""
    created_at: str = ""                 # ISO8601
    saved_at: str = ""                   # ISO8601 (capture time)
    text: str = ""                       # full expanded text (snapshot)
    type: str = "tweet"                  # tweet|thread|quote|media|link|deleted
    lang: str = ""
    conversation_id: str = ""
    urls: List[str] = field(default_factory=list)        # expanded outbound links
    mentions: List[str] = field(default_factory=list)    # ["@a", "@b"]
    hashtags: List[str] = field(default_factory=list)    # ["rlhf", ...] (no '#')
    media: List[MediaItem] = field(default_factory=list)
    thread_texts: List[str] = field(default_factory=list)  # ordered tweet texts
    quoted: Optional[Dict[str, Any]] = None              # nested quoted tweet
    needs_ocr: bool = False
    deleted: bool = False                # snapshot of a tweet later removed
    article_title: str = ""              # Twitter Article title (else "")
    article_summary: str = ""            # Twitter Article preview/abstract (else "")
    # Engagement counts (drift constantly; EXCLUDED from the content hash so a
    # like-count tick stays 'skipped' and never re-enriches — see hashing.py).
    like_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    quote_count: int = 0
    view_count: int = 0
    content_hash: str = ""               # filled by hashing.content_hash()
    change: str = "new"                  # "new" | "changed" (set by ingest)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status_id": self.status_id,
            "url": self.url,
            "canonical_url": self.canonical_url,
            "author_handle": self.author_handle,
            "author_name": self.author_name,
            "created_at": self.created_at,
            "saved_at": self.saved_at,
            "text": self.text,
            "type": self.type,
            "lang": self.lang,
            "conversation_id": self.conversation_id,
            "urls": list(self.urls),
            "mentions": list(self.mentions),
            "hashtags": list(self.hashtags),
            "media": [m.to_dict() for m in self.media],
            "thread_texts": list(self.thread_texts),
            "quoted": self.quoted,
            "needs_ocr": self.needs_ocr,
            "deleted": self.deleted,
            "article_title": self.article_title,
            "article_summary": self.article_summary,
            "like_count": self.like_count,
            "retweet_count": self.retweet_count,
            "reply_count": self.reply_count,
            "quote_count": self.quote_count,
            "view_count": self.view_count,
            "content_hash": self.content_hash,
            "change": self.change,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NormalizedBookmark":
        media = [MediaItem.from_dict(m) for m in (d.get("media") or [])]

        def _i(v: Any) -> int:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0

        return cls(
            status_id=str(d.get("status_id", "")),
            url=str(d.get("url", "")),
            canonical_url=str(d.get("canonical_url", "")),
            author_handle=str(d.get("author_handle", "")),
            author_name=str(d.get("author_name", "")),
            created_at=str(d.get("created_at", "")),
            saved_at=str(d.get("saved_at", "")),
            text=str(d.get("text", "")),
            type=str(d.get("type", "tweet")),
            lang=str(d.get("lang", "")),
            conversation_id=str(d.get("conversation_id", "")),
            urls=list(d.get("urls") or []),
            mentions=list(d.get("mentions") or []),
            hashtags=list(d.get("hashtags") or []),
            media=media,
            thread_texts=list(d.get("thread_texts") or []),
            quoted=d.get("quoted"),
            needs_ocr=bool(d.get("needs_ocr", False)),
            deleted=bool(d.get("deleted", False)),
            article_title=str(d.get("article_title", "")),
            article_summary=str(d.get("article_summary", "")),
            like_count=_i(d.get("like_count", 0)),
            retweet_count=_i(d.get("retweet_count", 0)),
            reply_count=_i(d.get("reply_count", 0)),
            quote_count=_i(d.get("quote_count", 0)),
            view_count=_i(d.get("view_count", 0)),
            content_hash=str(d.get("content_hash", "")),
            change=str(d.get("change", "new")),
        )


# --------------------------------------------------------------------------- #
# EnrichedBookmark: the one LLM (or mock) output per item. Joined back to the
# normalized record by status_id in build_kb.py. ``id`` (Johnny.Decimal) is
# assigned at build time, not here, so enrichment stays order-independent.
# --------------------------------------------------------------------------- #
@dataclass
class EnrichedBookmark:
    status_id: str
    category: str = ""                   # "11_llm-training" (label, maps to CC.NN)
    tags: List[str] = field(default_factory=list)        # 2..5, from tags.txt
    tldr: str = ""                       # 1 sentence
    key_points: List[str] = field(default_factory=list)  # 0..4, threads only
    why_saved: str = ""                  # 1 line inferred intent
    entities: List[str] = field(default_factory=list)    # tools/products/@handles
    content_hash: str = ""               # echoes the normalized hash it enriched

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status_id": self.status_id,
            "category": self.category,
            "tags": list(self.tags),
            "tldr": self.tldr,
            "key_points": list(self.key_points),
            "why_saved": self.why_saved,
            "entities": list(self.entities),
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EnrichedBookmark":
        return cls(
            status_id=str(d.get("status_id", "")),
            category=str(d.get("category", "")),
            tags=list(d.get("tags") or []),
            tldr=str(d.get("tldr", "")),
            key_points=list(d.get("key_points") or []),
            why_saved=str(d.get("why_saved", "")),
            entities=list(d.get("entities") or []),
            content_hash=str(d.get("content_hash", "")),
        )


# --------------------------------------------------------------------------- #
# IndexRow: one INDEX.tsv line. Column order is BINDING;
# the first 8 columns are FROZEN, media columns are append-only after `url`:
#   id  title  tags  tldr  author  date  type  url  has_media  thumb  media_alt
# tags are comma-joined; no field may contain a literal tab or newline (they are
# sanitized to spaces on emit). `from_tsv` pads short lines, so an OLD 8-column
# INDEX.tsv still parses (new fields come back empty/False) — backward compatible.
# --------------------------------------------------------------------------- #
INDEX_COLUMNS = [
    "id", "title", "tags", "tldr", "author", "date", "type", "url",
    "has_media", "thumb", "media_alt",
]


@dataclass
class IndexRow:
    id: str = ""          # Johnny.Decimal, e.g. "11.01"
    title: str = ""
    tags: List[str] = field(default_factory=list)
    tldr: str = ""
    author: str = ""      # "@someone"
    date: str = ""        # YYYY-MM-DD
    type: str = "tweet"
    url: str = ""
    has_media: bool = False   # True when the item has >=1 media descriptor
    thumb: str = ""           # first media URL (image/poster) or local cache path
    media_alt: str = ""       # first alt, else first ocr, else ""

    @staticmethod
    def _clean(s: str) -> str:
        # Tabs and newlines would corrupt the TSV; collapse to single spaces.
        return " ".join(str(s).replace("\t", " ").split())

    def to_tsv(self) -> str:
        return "\t".join([
            self._clean(self.id),
            self._clean(self.title),
            self._clean(",".join(self.tags)),
            self._clean(self.tldr),
            self._clean(self.author),
            self._clean(self.date),
            self._clean(self.type),
            self._clean(self.url),
            "1" if self.has_media else "",
            self._clean(self.thumb),
            self._clean(self.media_alt),
        ])

    @classmethod
    def from_tsv(cls, line: str) -> "IndexRow":
        parts = line.rstrip("\n").split("\t")
        # Pad to the expected width so short/partial lines never IndexError.
        while len(parts) < len(INDEX_COLUMNS):
            parts.append("")
        tags = [t for t in parts[2].split(",") if t]
        return cls(
            id=parts[0], title=parts[1], tags=tags, tldr=parts[3],
            author=parts[4], date=parts[5], type=parts[6], url=parts[7],
            has_media=bool(parts[8]), thumb=parts[9], media_alt=parts[10],
        )

    def to_dict(self) -> Dict[str, Any]:
        # Used when embedding the index as JSON in kb.html.
        return {
            "id": self.id, "title": self.title, "tags": list(self.tags),
            "tldr": self.tldr, "author": self.author, "date": self.date,
            "type": self.type, "url": self.url,
            "has_media": self.has_media, "thumb": self.thumb,
            "media_alt": self.media_alt,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IndexRow":
        return cls(
            id=str(d.get("id", "")), title=str(d.get("title", "")),
            tags=list(d.get("tags") or []), tldr=str(d.get("tldr", "")),
            author=str(d.get("author", "")), date=str(d.get("date", "")),
            type=str(d.get("type", "tweet")), url=str(d.get("url", "")),
            has_media=bool(d.get("has_media", False)),
            thumb=str(d.get("thumb", "")),
            media_alt=str(d.get("media_alt", "")),
        )


# --------------------------------------------------------------------------- #
# JSONL helpers. Reading skips blank lines and tolerates a trailing newline.
# Writing is plain append/overwrite; for atomic state writes use
# util.atomic_write to stage a full file then rename.
# --------------------------------------------------------------------------- #
def iter_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    """Yield each JSON object from a .jsonl file. Missing file -> empty."""
    import os
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    return list(iter_jsonl(path))


def write_jsonl(path: str, rows: Iterable[Any]) -> int:
    """Overwrite ``path`` with one JSON object per row. Accepts dataclasses
    (anything with ``to_dict``) or plain dicts. Returns the count written."""
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            obj = row.to_dict() if hasattr(row, "to_dict") else row
            fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            n += 1
    return n


def append_jsonl(path: str, rows: Iterable[Any]) -> int:
    """Append one JSON object per row to ``path`` (created if absent)."""
    n = 0
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            obj = row.to_dict() if hasattr(row, "to_dict") else row
            fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            n += 1
    return n


def as_dict(obj: Any) -> Dict[str, Any]:
    """Coerce a dataclass or dict to a plain dict."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    return dict(obj)
