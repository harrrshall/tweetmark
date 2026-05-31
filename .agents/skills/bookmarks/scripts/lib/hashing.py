"""Stable content hashing for idempotent dedup.

The ledger (.state/seen.tsv) keys ``status_id -> content_hash``. ingest.py marks
an item new if the status_id is unknown, changed if known with a different hash,
skipped if known and identical. So the hash must be:

  * stable     -> same meaningful content always yields the same hex,
                  independent of dict ordering, capture time, or cursor.
  * sensitive  -> an edited tweet (text/media/links/thread change) yields a new
                  hex, so build_kb regenerates and enrich re-runs for it only.
  * insensitive-> to bookkeeping fields (saved_at, captured_at, content_hash
                  itself, change flag), which would otherwise cause false churn.

We hash a canonical JSON projection of the meaningful fields with sha256 and
truncate to 16 hex chars (64 bits) which is collision-safe at KB scale.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

# Fields that define "the content of a bookmark". Anything not here is ignored
# by the hash (capture timestamps, the assigned id, the hash/flag fields, etc.).
#
# DELIBERATELY EXCLUDED (must NEVER be added here, or idempotency breaks):
#   * canonical_url        — derived purely from status_id; adding it would
#                            re-hash every note for zero content change.
#   * lang                 — metadata, not content; rarely drifts but is bookkeeping.
#   * like_count / retweet_count / reply_count / quote_count / view_count
#                          — engagement DRIFTS constantly; including any of them
#                            would flip a like-tick to `changed` and re-enrich
#                            the whole KB on every sync. A like-count-only delta
#                            MUST classify as `skipped` (same rule as saved_at).
#   * media_count / thumb / media_alt
#                          — DERIVED from `media` (which IS hashed below); hashing
#                            the derivations too would be redundant churn.
# Of the surfaced note fields, only `media` (kind+url+alt, OCR excluded) is hashed.
_MEANINGFUL_FIELDS = [
    "status_id",
    "url",
    "author_handle",
    "created_at",
    "text",
    "type",
    "urls",
    "mentions",
    "hashtags",
    "media",
    "thread_texts",
    "quoted",
    "deleted",
]

_HASH_LEN = 16  # hex chars from the front of the sha256 digest


def _project(norm: Dict[str, Any]) -> Dict[str, Any]:
    """Pull just the meaningful fields, normalizing list order where order is
    not semantically meaningful (mentions/hashtags/urls are sorted; media and
    thread_texts keep order because sequence matters)."""
    out: Dict[str, Any] = {}
    for f in _MEANINGFUL_FIELDS:
        v = norm.get(f)
        if f in ("mentions", "hashtags", "urls") and isinstance(v, list):
            out[f] = sorted(str(x) for x in v)
        elif f == "media" and isinstance(v, list):
            # Only the identity of media matters for change detection, not OCR
            # text (which enrich fills in later and must not retrigger churn).
            out[f] = [
                {"kind": m.get("kind", ""), "url": m.get("url", ""),
                 "alt": m.get("alt", "")}
                for m in v if isinstance(m, dict)
            ]
        else:
            out[f] = v
    return out


def content_hash(normalized: Any) -> str:
    """Return a short stable hex hash over the meaningful fields of a
    normalized bookmark. Accepts a dict or any object with ``to_dict``."""
    if hasattr(normalized, "to_dict"):
        d = normalized.to_dict()
    else:
        d = dict(normalized)
    projection = _project(d)
    canonical = json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:_HASH_LEN]


def short(text: str, n: int = _HASH_LEN) -> str:
    """Generic short sha256 of an arbitrary string (used for misc keys)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]
