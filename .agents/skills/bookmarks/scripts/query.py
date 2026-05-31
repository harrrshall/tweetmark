#!/usr/bin/env python3
"""query.py - two-tier grep retrieval over the bookmarks knowledge base.

Stdlib only. The most-used path and the cheapest by design (see
references/RETRIEVAL.md). The CLI:

    python3 query.py --kb <KB> "QUERY" [--json] [--k 5]

The pipeline, in order:

  1. EXPAND the query in-process (free): turn the natural-language question into
     a small set of keywords / synonyms / likely tags. No LLM, no network: a
     deterministic lexical expansion (stem-ish tokens, a curated synonym map,
     and tag hints). This is the single step that ~10x'd grep accuracy in the
     research, so it runs every time.

  2. GREP INDEX.tsv for rows matching ANY expansion term. ripgrep (`rg`) is used
     when present (streamed, capped with -m); otherwise a pure-Python streamed
     line scan with the same cap. INDEX.tsv is NEVER read wholesale into memory
     -- both paths stream line by line and stop at the cap.

  3. RANK. If grep returns too many candidates (over the rank threshold) OR too
     few (under --k while the index clearly holds more), fall back to the FTS5
     `.state/kb.db` (bm25 ranking) to order/rescue results. The kb.db is
     optional and rebuildable; if it is missing or unusable we degrade to a
     lexical score over the grepped rows. We never block on it.

  4. OPEN only the top-k note files (by Johnny.Decimal id -> path on disk),
     reading each note's body for the why-saved / key-points detail. Only k
     notes are ever opened, so token cost stays flat as the KB grows.

  5. PRINT ranked results WITH source: id, title, tldr, author, url, and the
     note path. `--json` emits a structured JSON array of the same records.

Guarantees: never reads the whole INDEX.tsv into memory for matching; never
cites a non-existent id (every emitted id is a real INDEX.tsv row, and `path` is
"" when no note file backs it rather than a fabricated path).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Tuple

# --- import shim ----------------------------- #
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import schema  # noqa: E402
import frontmatter  # noqa: E402
import jdid  # noqa: E402
import util  # noqa: E402


# A grep that returns more than this many candidate rows is "too many": we hand
# off to FTS5 (or the lexical scorer) to rank rather than trusting grep order.
RANK_THRESHOLD = 40
# Hard cap on rows pulled out of INDEX.tsv during the streamed grep, so a query
# that matches a huge fraction of a 50k-row index still stays bounded.
GREP_CAP = 400


# --------------------------------------------------------------------------- #
# 1. Query expansion (in-process, free, deterministic)
# --------------------------------------------------------------------------- #

# A small, curated synonym / related-term map skewed toward the KB's domain
# (AI / tooling / startups / etc., per the default taxonomy) plus a few generic
# ones. Keys and values are lowercase. Expansion is one hop only (no chains), so
# it stays small and predictable.
_SYNONYMS: Dict[str, List[str]] = {
    "rlhf": ["dpo", "ppo", "reward model", "alignment", "preference"],
    "dpo": ["rlhf", "ppo", "preference optimization", "alignment"],
    "ppo": ["rlhf", "dpo", "policy optimization"],
    "llm": ["language model", "gpt", "transformer", "model"],
    "model": ["llm", "checkpoint", "weights"],
    "finetune": ["fine-tune", "fine-tuning", "finetuning", "training", "lora"],
    "finetuning": ["finetune", "fine-tuning", "training", "lora"],
    "training": ["finetune", "pretrain", "rlhf"],
    "lora": ["finetune", "peft", "adapter", "qlora"],
    "rag": ["retrieval", "vector", "embedding", "retrieval-augmented"],
    "embedding": ["vector", "rag", "semantic"],
    "tts": ["text-to-speech", "speech", "audio", "voice"],
    "speech": ["tts", "audio", "voice", "asr"],
    "agent": ["agentic", "autonomous", "tool-use", "agents"],
    "prompt": ["prompting", "prompt engineering", "system prompt"],
    "prompting": ["prompt", "prompt engineering"],
    "startup": ["startups", "founder", "company", "venture", "yc"],
    "founder": ["startup", "founders", "ceo"],
    "fundraising": ["raise", "seed", "vc", "investor", "funding"],
    "marketing": ["growth", "gtm", "go-to-market", "acquisition"],
    "gtm": ["go-to-market", "marketing", "sales"],
    "investing": ["investment", "stocks", "portfolio", "markets"],
    "crypto": ["bitcoin", "ethereum", "web3", "defi", "blockchain"],
    "productivity": ["workflow", "focus", "habits", "getting-things-done"],
    "notes": ["note-taking", "notetaking", "obsidian", "knowledge"],
    "design": ["ui", "ux", "interface", "typography", "branding"],
    "ui": ["ux", "interface", "design", "frontend"],
    "ux": ["ui", "interface", "design", "usability"],
    "health": ["fitness", "nutrition", "wellness"],
    "fitness": ["training", "workout", "exercise", "gym"],
    "sleep": ["rest", "circadian", "recovery"],
    "tool": ["tools", "app", "utility", "library"],
    "tutorial": ["guide", "how-to", "walkthrough", "explainer"],
    "explainer": ["explanation", "tutorial", "primer", "intro"],
    "paper": ["arxiv", "research", "study"],
    "research": ["paper", "study", "arxiv"],
    "thread": ["threads"],
}

# Words too generic to be worth greping on their own.
_STOPWORDS = frozenset("""
a an and are as at be but by for from has have how i if in into is it its
my no not of on or our so that the their this to up was what when where
which who why with you your about can do does find get give me show tell
some any all most best top good great new old want need looking look saved
bookmark bookmarks tweet tweets thread about please using use used
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9#@_+.\-]*", re.IGNORECASE)

# Structured inline-filter tokens the user may type (same vocabulary the kb.html
# UI honors): #tag, from:@handle, area:slug, is:type. These are AND constraints,
# NOT free-text grep terms -- a `from:@carol fine-tuning` query must return only
# carol's fine-tuning notes, never another author's. We parse them out here and
# apply them as hard post-filters in run_query (see _row_passes_filters), so the
# CLI and the UI agree and an author/type/area/tag scope is actually enforced.
_FILTER_TAG_RE = re.compile(r"(?:^|\s)#([a-z0-9_\-]+)", re.IGNORECASE)
_FILTER_FROM_RE = re.compile(r"(?:^|\s)from:(@?[a-z0-9_]+)", re.IGNORECASE)
_FILTER_AREA_RE = re.compile(r"(?:^|\s)area:([a-z0-9_\-]+)", re.IGNORECASE)
_FILTER_IS_RE = re.compile(r"(?:^|\s)is:([a-z0-9_\-]+)", re.IGNORECASE)
# Substring that strips ALL structured tokens out of the raw text before it is
# tokenized for free-text expansion, so `area`, `is`, `from` never leak in as
# literal grep terms (and the filter VALUES are added back deliberately, below).
_FILTER_ANY_RE = re.compile(
    r"(?:^|\s)(?:#[a-z0-9_\-]+|from:@?[a-z0-9_]+|area:[a-z0-9_\-]+|is:[a-z0-9_\-]+)",
    re.IGNORECASE)


class Filters:
    """The structured AND-constraints parsed from a query (tags / from / area /
    type). Empty lists mean 'no constraint of this kind'. A row must satisfy
    EVERY populated constraint to pass (see _row_passes_filters)."""

    __slots__ = ("tags", "froms", "areas", "types")

    def __init__(self, tags=None, froms=None, areas=None, types=None):
        self.tags = tags or []
        self.froms = froms or []
        self.areas = areas or []
        self.types = types or []

    def any(self) -> bool:
        return bool(self.tags or self.froms or self.areas or self.types)


def parse_filters(query: str) -> "Filters":
    """Extract the structured inline filters (#tag / from:@h / area:x / is:t)
    from a query. Lowercased; handle '@' stripped. These mirror the kb.html UI's
    parseQuery so the agent CLI and the UI enforce the same scoping."""
    raw = query or ""
    tags = [m.group(1).lower() for m in _FILTER_TAG_RE.finditer(raw)]
    froms = [m.group(1).lower().lstrip("@") for m in _FILTER_FROM_RE.finditer(raw)]
    areas = [m.group(1).lower() for m in _FILTER_AREA_RE.finditer(raw)]
    types = [m.group(1).lower() for m in _FILTER_IS_RE.finditer(raw)]
    return Filters(tags=tags, froms=froms, areas=areas, types=types)


def _row_area_slug(row: schema.IndexRow) -> str:
    """The Johnny.Decimal area slug for a row, derived from its id prefix (e.g.
    '11.01' -> 'ai-tech'). Used to honor an `area:` filter without needing the
    note body. Empty when the id is malformed."""
    try:
        cc = jdid.code_for_label(row.id or "")
        slug, _desc = jdid.area_for_code(cc)
        return (slug or "").lower()
    except Exception:
        return ""


def _row_passes_filters(row: schema.IndexRow, f: "Filters") -> bool:
    """True iff the INDEX.tsv row satisfies EVERY populated structured filter.
    Matches the kb.html UI semantics: tag/area substring, author substring (sans
    '@'), type substring. A constraint with no value never excludes anything."""
    if not f.any():
        return True
    row_tags = [t.lower() for t in (row.tags or [])]
    for want in f.tags:
        if not any(want in t for t in row_tags):
            return False
    author = (row.author or "").lower().lstrip("@")
    for want in f.froms:
        if want not in author:
            return False
    rtype = (row.type or "").lower()
    for want in f.types:
        if want not in rtype:
            return False
    if f.areas:
        slug = _row_area_slug(row)
        for want in f.areas:
            if want not in slug:
                return False
    return True


def _normalize_token(tok: str) -> str:
    return tok.strip().lower().strip(".-_")


def _light_stem(tok: str) -> Optional[str]:
    """Return a crude singular/base form if it differs, else None. Deliberately
    conservative so we never produce garbage stems (no 'analysi' from
    'analysis')."""
    if len(tok) <= 4:
        return None
    for suf, repl in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""),
                      ("s", "")):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            base = tok[: len(tok) - len(suf)] + repl
            if base != tok:
                return base
    return None


def expand_query(query: str) -> Tuple[List[str], List[str]]:
    """Expand a natural-language query into (terms, tags).

    ``terms`` is the ordered, de-duplicated set of literal substrings to grep
    for (keywords, light stems, synonyms). ``tags`` is the subset that look like
    controlled-vocabulary tags (single tokens / explicit ``#tag``), used to bias
    ranking. Both are lowercase. The query's own raw tokens always come first so
    exact matches rank ahead of synonym matches.
    """
    raw = query or ""
    terms: List[str] = []
    seen = set()

    def add(t: str) -> None:
        t = _normalize_token(t)
        if not t or t in seen:
            return
        seen.add(t)
        terms.append(t)

    # explicit #tag / area: / from:@handle filters in the query
    explicit_tags: List[str] = []
    for m in _FILTER_TAG_RE.finditer(raw):
        explicit_tags.append(m.group(1).lower())
    for m in _FILTER_FROM_RE.finditer(raw):
        add(m.group(1))

    # Strip ALL structured filter tokens (#tag / from: / area: / is:) from the
    # free-text before tokenizing, so the filter KEYWORDS ('area', 'is', 'from')
    # and their slug/type values never leak in as literal grep terms (run_query
    # enforces them as hard filters instead). The #tag VALUE is re-added below as
    # a grep term so it still drives recall; the from-handle was added above.
    free_text = _FILTER_ANY_RE.sub(" ", raw)
    tokens = [_normalize_token(m.group(0)) for m in _TOKEN_RE.finditer(free_text)]
    content_tokens = [t for t in tokens if t and t not in _STOPWORDS]

    # 1) keep meaningful raw tokens first (preserve query phrasing priority)
    for t in content_tokens:
        add(t)

    # 2) light stems of those tokens
    for t in content_tokens:
        st = _light_stem(t)
        if st:
            add(st)

    # 3) one hop of synonyms / related terms
    for t in list(content_tokens):
        for syn in _SYNONYMS.get(t, []):
            add(syn)

    # tags: explicit #tags plus single-word content tokens (likely tag matches)
    tag_set: List[str] = []
    tseen = set()
    for t in explicit_tags + content_tokens:
        t = t.lower()
        if t and t not in tseen and t not in _STOPWORDS:
            tseen.add(t)
            tag_set.append(t)
        add(t)  # ensure explicit tags are grep terms too

    # Fallback: if expansion stripped everything (all stopwords), grep the raw
    # tokens so we never search for nothing.
    if not terms and tokens:
        for t in tokens:
            add(t)

    return terms, tag_set


# --------------------------------------------------------------------------- #
# 2. Grep INDEX.tsv (streamed, capped, ripgrep-or-stdlib)
# --------------------------------------------------------------------------- #

def _regex_for_terms(terms: Iterable[str]) -> str:
    """Build a single case-insensitive alternation regex from terms. Each term
    is escaped so '.' / '+' / '#' in tags are literal."""
    parts = [re.escape(t) for t in terms if t]
    return "|".join(parts) if parts else r"(?!x)x"  # never-match sentinel


def grep_index(index_path: str, terms: List[str],
               cap: int = GREP_CAP) -> List[schema.IndexRow]:
    """Return INDEX.tsv rows matching ANY term, capped at ``cap``. Tries
    ripgrep first (streamed, ``-m`` capped), then a pure-Python streamed scan.

    INDEX.tsv is never slurped whole: ripgrep streams it and we read its stdout
    line by line; the Python fallback iterates the file handle line by line and
    stops at the cap.
    """
    if not os.path.exists(index_path) or not terms:
        return []
    pattern = _regex_for_terms(terms)

    rg = shutil.which("rg")
    if rg:
        rows = _grep_with_ripgrep(rg, index_path, pattern, cap)
        if rows is not None:
            return rows
    return _grep_with_python(index_path, pattern, cap)


def _grep_with_ripgrep(rg: str, index_path: str, pattern: str,
                       cap: int) -> Optional[List[schema.IndexRow]]:
    """Run ripgrep streamed and parse matching lines. Returns None on failure so
    the caller falls back to the Python scanner."""
    cmd = [rg, "--no-config", "-i", "-N", "-m", str(cap), "-e", pattern,
           index_path]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    rows: List[schema.IndexRow] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:  # streamed, one line at a time
            line = line.rstrip("\n")
            if not line:
                continue
            rows.append(schema.IndexRow.from_tsv(line))
            if len(rows) >= cap:
                break
    finally:
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except OSError:
            pass
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass
    # ripgrep exit 1 == "no matches" (rows == []), which is a valid result.
    return rows


def _grep_with_python(index_path: str, pattern: str,
                      cap: int) -> List[schema.IndexRow]:
    """Pure-Python streamed line scan. Reads the file handle line by line and
    stops at the cap -- the whole index is never held in memory."""
    rx = re.compile(pattern, re.IGNORECASE)
    rows: List[schema.IndexRow] = []
    with open(index_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:  # iterator: one line resident at a time
            line = line.rstrip("\n")
            if not line:
                continue
            if rx.search(line):
                rows.append(schema.IndexRow.from_tsv(line))
                if len(rows) >= cap:
                    break
    return rows


def _index_row_count_exceeds(index_path: str, n: int) -> bool:
    """Stream INDEX.tsv counting lines, stopping as soon as the count exceeds
    ``n``. Never loads the file; used to decide whether the index is 'big enough'
    to bother with an FTS5 rescue when grep under-returned."""
    if not os.path.exists(index_path):
        return False
    c = 0
    with open(index_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.strip():
                c += 1
                if c > n:
                    return True
    return False


# --------------------------------------------------------------------------- #
# 3. Ranking (FTS5 bm25 when warranted, else lexical over grepped rows)
# --------------------------------------------------------------------------- #

def _fts_schema(conn: sqlite3.Connection) -> Optional[Tuple[str, List[str]]]:
    """Introspect the kb.db for an FTS5 table and its columns. index.py owns the
    exact name; we discover it rather than hardcode, so we stay compatible with
    whatever it built. Returns (table_name, columns) or None."""
    try:
        cur = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND sql LIKE '%fts5%'")
        cand = cur.fetchall()
    except sqlite3.Error:
        return None
    for name, sql in cand:
        cols = _columns_for(conn, name)
        if cols and "id" in cols:
            return name, cols
    # No table advertised id; take the first FTS5 table we can read columns for.
    for name, sql in cand:
        cols = _columns_for(conn, name)
        if cols:
            return name, cols
    return None


def _columns_for(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        cur = conn.execute("PRAGMA table_info({})".format(_quote_ident(table)))
        return [r[1] for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _fts_match_expr(terms: List[str]) -> str:
    """Build a safe FTS5 MATCH expression: OR of quoted terms. Each term is
    wrapped in double quotes (FTS5 string literal) so punctuation in tags cannot
    inject syntax. Empty -> a token that simply won't match."""
    quoted = []
    for t in terms:
        t = t.replace('"', "")  # FTS5 string literal: drop embedded quotes
        t = t.strip()
        if t:
            quoted.append('"{}"'.format(t))
    if not quoted:
        return '"zzzznomatchzzzz"'
    return " OR ".join(quoted)


def fts_rank_ids(kb_root: str, terms: List[str],
                 limit: int) -> Optional[List[str]]:
    """Return ids ordered by FTS5 bm25 rank (best first), or None if the kb.db
    is missing / has no usable FTS5 table / errors. Best-effort: retrieval must
    never block on the optional index."""
    db_path = os.path.join(util.state_dir(kb_root), "kb.db")
    if not os.path.exists(db_path):
        return None
    uri = "file:{}?mode=ro&immutable=1".format(db_path.replace("?", "%3f"))
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(db_path, timeout=2.0)
        except sqlite3.Error:
            return None
    try:
        sch = _fts_schema(conn)
        if not sch:
            return None
        table, cols = sch
        if "id" not in cols:
            return None
        match = _fts_match_expr(terms)
        sql = ("SELECT id FROM {t} WHERE {t} MATCH ? "
               "ORDER BY bm25({t}) LIMIT ?").format(t=_quote_ident(table))
        try:
            cur = conn.execute(sql, (match, int(limit)))
            ids = [str(r[0]) for r in cur.fetchall()]
        except sqlite3.Error:
            # Some builds reject bm25() on a contentless table; retry on rank.
            try:
                sql2 = ("SELECT id FROM {t} WHERE {t} MATCH ? "
                        "ORDER BY rank LIMIT ?").format(t=_quote_ident(table))
                cur = conn.execute(sql2, (match, int(limit)))
                ids = [str(r[0]) for r in cur.fetchall()]
            except sqlite3.Error:
                return None
        return ids
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def lexical_score(row: schema.IndexRow, terms: List[str],
                  tags: List[str]) -> float:
    """A cheap relevance score for ranking grepped rows without FTS5. Weights
    title and tag hits highest, then tldr, then author/url. Term order in the
    query is rewarded slightly (earlier terms weigh more)."""
    title = (row.title or "").lower()
    tldr = (row.tldr or "").lower()
    author = (row.author or "").lower()
    url = (row.url or "").lower()
    row_tags = [t.lower() for t in (row.tags or [])]
    rid = (row.id or "").lower()

    score = 0.0
    nterms = max(1, len(terms))
    for i, t in enumerate(terms):
        if not t:
            continue
        w = 1.0 + (nterms - i) / float(nterms)  # 2.0 down to ~1.0 by position
        if t in title:
            score += 6.0 * w
        if any(t == rt or t in rt for rt in row_tags):
            score += 5.0 * w
        if t in tldr:
            score += 3.0 * w
        if t in author:
            score += 2.0 * w
        if t in url or t in rid:
            score += 1.0 * w
    # exact controlled-tag hits get an extra bump
    for tg in tags:
        if tg in row_tags:
            score += 2.0
    return score


# --------------------------------------------------------------------------- #
# 4. Open the top-k note files (id -> path on disk)
# --------------------------------------------------------------------------- #

def build_id_to_path(kb_root: str) -> Dict[str, str]:
    """Map Johnny.Decimal id -> note path by scanning the KB tree once. Note
    filenames are 'CC.NN_kebab-title.md'; we key on the 'CC.NN' prefix. Only the
    area folders are walked (``.state`` and dotfiles are skipped)."""
    mapping: Dict[str, str] = {}
    id_re = re.compile(r"^(\d{1,2}\.\d{1,3})_")
    try:
        entries = sorted(os.listdir(kb_root))
    except OSError:
        return mapping
    for area in entries:
        if area.startswith(".") or area.startswith("_"):
            continue
        area_path = os.path.join(kb_root, area)
        if not os.path.isdir(area_path):
            continue
        for root, dirs, files in os.walk(area_path):
            # never descend into a stray .state or hidden dir
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                m = id_re.match(fn)
                if m:
                    mapping.setdefault(m.group(1), os.path.join(root, fn))
    return mapping


_SECTION_RE = re.compile(r"\*\*(TL;DR|Why saved|Key points|Links):\*\*",
                         re.IGNORECASE)


def read_note_detail(path: str) -> Dict[str, object]:
    """Read a note file and pull out the body detail the answer shows: tldr,
    why_saved, key_points, links, plus the parsed frontmatter meta. Missing /
    unreadable file -> empty detail (never raises)."""
    detail: Dict[str, object] = {
        "tldr": "", "why_saved": "", "key_points": [], "links": [], "meta": {},
    }
    if not path or not os.path.exists(path):
        return detail
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return detail
    meta, body = frontmatter.parse(text)
    detail["meta"] = meta

    # Split the body on the bold section headers (TL;DR / Why saved / ...).
    sections: Dict[str, str] = {}
    cur_key = None
    buf: List[str] = []
    for line in body.split("\n"):
        m = _SECTION_RE.match(line.strip())
        if m:
            if cur_key is not None:
                sections[cur_key] = "\n".join(buf).strip()
            cur_key = m.group(1).lower()
            # capture any inline text after the header on the same line
            after = line.split(":**", 1)[1] if ":**" in line else ""
            buf = [after]
        else:
            if cur_key is not None:
                buf.append(line)
    if cur_key is not None:
        sections[cur_key] = "\n".join(buf).strip()

    detail["tldr"] = sections.get("tl;dr", "").strip()
    detail["why_saved"] = sections.get("why saved", "").strip()
    kp_raw = sections.get("key points", "")
    detail["key_points"] = [
        ln.strip().lstrip("-* ").strip()
        for ln in kp_raw.split("\n")
        if ln.strip().lstrip("-* ").strip()
    ]
    links_raw = sections.get("links", "")
    detail["links"] = [
        ln.strip() for ln in re.split(r"[\s,]+", links_raw) if ln.strip()
    ]
    return detail


# --------------------------------------------------------------------------- #
# 5. Drive the pipeline + emit results
# --------------------------------------------------------------------------- #

def _result_record(row: schema.IndexRow, path: str,
                   detail: Dict[str, object], score: float) -> Dict[str, object]:
    """Assemble one ranked result with its source. ``tldr`` prefers the note
    body's TL;DR (richer) but falls back to the INDEX.tsv tldr."""
    tldr = (str(detail.get("tldr") or "").strip()) or (row.tldr or "")
    return {
        "id": row.id,
        "title": row.title,
        "tldr": tldr,
        "author": row.author,
        "url": row.url,
        "path": path or "",
        "date": row.date,
        "type": row.type,
        "tags": list(row.tags or []),
        "why_saved": str(detail.get("why_saved") or ""),
        "key_points": list(detail.get("key_points") or []),
        "score": round(float(score), 3),
    }


def run_query(kb_root: str, query: str, k: int = 5) -> List[Dict[str, object]]:
    """Execute the full two-tier retrieval and return up to ``k`` ranked result
    records (each carrying its source). Pure function over the KB on disk."""
    index_path = os.path.join(kb_root, "INDEX.tsv")
    terms, tags = expand_query(query)
    filters = parse_filters(query)

    # --- tier 1: grep the index (streamed, capped) ---
    rows = grep_index(index_path, terms, cap=GREP_CAP)
    # A filter-only query (e.g. "from:@carol", "is:thread", "area:ai") may leave
    # no free-text grep terms; grep then returns nothing even though matching
    # rows exist. Stream the index applying just the filters so the CLI honors a
    # pure-filter scope exactly like the UI does (still streamed + capped, never
    # slurped whole).
    if filters.any() and not rows:
        rows = _scan_index_filtered(index_path, filters, cap=GREP_CAP)

    by_id: Dict[str, schema.IndexRow] = {}
    order: List[str] = []
    for r in rows:
        # HARD structured filters (#tag / from: / area: / is:) are AND-applied
        # here so an author/type/area/tag scope is actually enforced -- a topic
        # word in a `from:@carol` query can never leak another author's rows.
        if not _row_passes_filters(r, filters):
            continue
        if r.id and r.id not in by_id:
            by_id[r.id] = r
            order.append(r.id)

    # --- tier 2: decide whether to rank via FTS5 ---
    too_many = len(order) > RANK_THRESHOLD
    too_few = len(order) < k
    ranked_ids: List[str] = []

    if too_many or too_few:
        fts_ids = fts_rank_ids(kb_root, terms, limit=max(k * 4, RANK_THRESHOLD))
        if fts_ids:
            # keep FTS order; intersect with grep hits first (precision), then
            # append any FTS-only ids the grep missed (recall rescue).
            grep_set = set(order)
            for fid in fts_ids:
                if fid in grep_set:
                    ranked_ids.append(fid)
            if too_few:
                for fid in fts_ids:
                    if fid not in by_id:
                        ranked_ids.append(fid)
            # any grep hits FTS didn't rank go to the tail, lexical-scored
            tail = [i for i in order if i not in set(ranked_ids)]
            tail.sort(key=lambda i: lexical_score(by_id[i], terms, tags),
                      reverse=True)
            ranked_ids.extend(tail)

    if not ranked_ids:
        # lexical ranking over the grepped rows (stable: score desc, then id)
        ranked_ids = sorted(
            order,
            key=lambda i: (-lexical_score(by_id[i], terms, tags), i))

    # --- tier 3: open only the top-k notes (resolve id -> path) ---
    id_to_path = build_id_to_path(kb_root)
    results: List[Dict[str, object]] = []
    for rid in ranked_ids:
        if len(results) >= k:
            break
        row = by_id.get(rid)
        path = id_to_path.get(rid, "")
        if row is None:
            # FTS-only rescue id with no grepped row: pull its INDEX.tsv row by
            # exact id so we still show real source (never fabricate).
            row = _row_by_id(index_path, rid)
            if row is None:
                continue  # would be a non-existent id; skip rather than cite it
            # an FTS rescue row never passed the grep-time filter gate, so it
            # must satisfy the structured filters here too (no scope leak).
            if not _row_passes_filters(row, filters):
                continue
        detail = read_note_detail(path) if path else {
            "tldr": "", "why_saved": "", "key_points": [], "links": [],
            "meta": {}}
        score = lexical_score(row, terms, tags)
        results.append(_result_record(row, path, detail, score))
    return results


def _scan_index_filtered(index_path: str, f: "Filters",
                         cap: int = GREP_CAP) -> List[schema.IndexRow]:
    """Stream INDEX.tsv returning rows that satisfy the structured filters, for a
    filter-only query (no free-text grep terms). Reads the file handle line by
    line and stops at the cap -- the whole index is never held in memory."""
    if not os.path.exists(index_path) or not f.any():
        return []
    rows: List[schema.IndexRow] = []
    with open(index_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:  # iterator: one line resident at a time
            line = line.rstrip("\n")
            if not line:
                continue
            row = schema.IndexRow.from_tsv(line)
            if _row_passes_filters(row, f):
                rows.append(row)
                if len(rows) >= cap:
                    break
    return rows


def _row_by_id(index_path: str, target_id: str) -> Optional[schema.IndexRow]:
    """Stream INDEX.tsv to find the single row whose id == target_id. Used only
    to back an FTS-rescue id with its real source row; bounded to one lookup,
    never loads the file."""
    if not os.path.exists(index_path) or not target_id:
        return None
    prefix = target_id + "\t"
    with open(index_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(prefix):
                return schema.IndexRow.from_tsv(line.rstrip("\n"))
    return None


# --------------------------------------------------------------------------- #
# Output formatting
# --------------------------------------------------------------------------- #

def format_human(results: List[Dict[str, object]], query: str) -> str:
    if not results:
        return ('No matching bookmarks for: "{}"\n'
                "Try fewer or different keywords, or run a sync to add more."
                .format(query))
    out: List[str] = []
    out.append('{} result{} for "{}":'.format(
        len(results), "" if len(results) == 1 else "s", query))
    out.append("")
    for n, r in enumerate(results, 1):
        out.append("{}. [{}] {}".format(n, r["id"], r["title"] or "(untitled)"))
        if r.get("tldr"):
            out.append("   {}".format(r["tldr"]))
        meta_bits = []
        if r.get("author"):
            meta_bits.append(str(r["author"]))
        if r.get("date"):
            meta_bits.append(str(r["date"]))
        if r.get("type"):
            meta_bits.append(str(r["type"]))
        if r.get("tags"):
            meta_bits.append("#" + " #".join(r["tags"]))
        if meta_bits:
            out.append("   " + "  ".join(meta_bits))
        if r.get("why_saved"):
            out.append("   why: {}".format(r["why_saved"]))
        if r.get("url"):
            out.append("   {}".format(r["url"]))
        if r.get("path"):
            out.append("   note: {}".format(r["path"]))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="query.py",
        description="Two-tier grep retrieval over the bookmarks KB.")
    ap.add_argument("--kb", required=True, help="KB root directory")
    ap.add_argument("query", help="natural-language query")
    ap.add_argument("--json", action="store_true",
                    help="emit a JSON array of structured results")
    ap.add_argument("--k", type=int, default=5,
                    help="max results to return (default 5)")
    args = ap.parse_args(argv)

    kb_root = os.path.expanduser(args.kb)
    if not os.path.isdir(kb_root):
        sys.stderr.write("error: --kb path is not a directory: {}\n".format(
            kb_root))
        return 2
    k = args.k if args.k and args.k > 0 else 5

    results = run_query(kb_root, args.query, k=k)

    if args.json:
        sys.stdout.write(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(format_human(results, args.query))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
