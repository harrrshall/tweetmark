#!/usr/bin/env python3
"""enrich.py -- the one (LLM-or-mock) enrichment stage of the bookmarks pipeline.

Reads ``.state/new_items.jsonl`` (NormalizedBookmark) and writes batch-indexed
``.state/enriched/NNN.out.jsonl`` (EnrichedBookmark). It is the only stage that
assigns a category, picks controlled-vocabulary tags, and writes summaries.

    python3 enrich.py --kb <KB> --engine mock|agent|api [--batch-size 20]

Three engines, ONE output format (so build_kb.py stays engine-agnostic):

  mock   DETERMINISTIC, rule-based, no network, no randomness. A keyword map
         chooses one default-taxonomy category; 2..5 tags are chosen from the
         controlled vocabulary (tags.txt, seeded from assets/tags.seed.txt);
         the TL;DR is the cleaned first sentence truncated to ~140 chars;
         key_points are extracted only for threads; why_saved is a small
         heuristic; entities are the @handles + outbound urls named. Stable
         across runs -- golden/idempotency tests run on this engine.

  agent  Prepares ``.state/batches/NNN.txt`` (~--batch-size items each, with the
         taxonomy and tags.txt inlined ONCE per batch) for the host agent
         (Claude Code / Codex) to read and answer into a sibling
         ``.state/batches/NNN.out.jsonl``. On a later invocation enrich reads
         those answers back, normalizes their tags against the vocabulary, and
         promotes them into ``.state/enriched/NNN.out.jsonl``. Resumable: only
         one batch is ever in the agent's context.

  api    A clearly-documented STUB for unattended bulk enrichment via a cheap
         model. It explains the env var + endpoint it WOULD call but never
         touches the network here (so tests stay hermetic). With no key it falls
         back to the deterministic mock path so the pipeline still completes.

Caching: an item whose ``content_hash`` already appears in any existing
``enriched/*.out.jsonl`` is NOT re-enriched. Re-running on unchanged input is a
no-op. State files are written atomically (util.atomic_write).

Controlled vocabulary (anti-sprawl): tags must come from ``tags.txt`` (seeded
from ``assets/tags.seed.txt`` on first run). A free-form candidate is mapped to
its nearest existing tag; a genuinely new tag is appended to ``tags.txt`` only
when nothing in the vocabulary fits.

Stdlib only. Writes only inside the chosen --kb.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# --- import shim --------------------------------- #
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import schema  # noqa: E402
import jdid    # noqa: E402
import util    # noqa: E402

# assets/tags.seed.txt lives next to scripts/ under the skill root.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPTS_DIR)
SEED_TAGS_PATH = os.path.join(_SKILL_DIR, "assets", "tags.seed.txt")


# =========================================================================== #
# Controlled vocabulary (tags.txt)
# =========================================================================== #
def _slug_tag(raw: str) -> str:
    """Normalize a free-form tag candidate to lowercase kebab-case."""
    s = str(raw).strip().lower().lstrip("#")
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def load_seed_tags() -> list:
    """Read the shipped seed vocabulary (assets/tags.seed.txt). Comments (#) and
    blank lines are skipped. Order is preserved, deduped."""
    out: list = []
    seen = set()
    if not os.path.exists(SEED_TAGS_PATH):
        return out
    with open(SEED_TAGS_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            t = _slug_tag(line)
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _tags_txt_path(kb: str) -> str:
    return os.path.join(kb, "tags.txt")


def load_vocab(kb: str) -> list:
    """Load the KB's controlled vocabulary from ``tags.txt``, seeding it from
    assets/tags.seed.txt on first run. Returns an ordered, deduped list."""
    path = _tags_txt_path(kb)
    if not os.path.exists(path):
        seed = load_seed_tags()
        write_vocab(kb, seed)
        return seed
    out: list = []
    seen = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            t = _slug_tag(line)
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    if not out:
        # An empty/comment-only tags.txt re-seeds rather than stranding enrich.
        out = load_seed_tags()
        write_vocab(kb, out)
    return out


def write_vocab(kb: str, tags: list) -> None:
    """Persist the controlled vocabulary to ``tags.txt`` atomically. Deduped,
    order preserved (append-only growth keeps diffs small and stable)."""
    util.ensure_dir(kb)
    out: list = []
    seen = set()
    for t in tags:
        t = _slug_tag(t)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    header = (
        "# Controlled vocabulary for bookmark tags (kebab-case, one per line).\n"
        "# Seeded from assets/tags.seed.txt; grown by enrich.py only when nothing\n"
        "# in the list fits. doctor.py tracks drift against this file.\n"
    )
    util.atomic_write(_tags_txt_path(kb), header + "\n".join(out) + ("\n" if out else ""))


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (stdlib, small strings) for nearest-tag matching."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def nearest_vocab_tag(candidate: str, vocab: list):
    """Map a candidate tag to the nearest existing vocabulary tag, or None if
    nothing is close enough. Match order (deterministic):
      1. exact slug match,
      2. one is a substring of the other (>=4 chars, to avoid noise),
      3. small edit distance relative to length.
    Returns the matched vocab tag or None (caller may then append it as new).
    """
    cand = _slug_tag(candidate)
    if not cand:
        return None
    if cand in vocab:
        return cand
    # substring containment (e.g. "fine-tune" -> "fine-tuning")
    best_sub = None
    for v in vocab:
        if len(cand) >= 4 and len(v) >= 4 and (cand in v or v in cand):
            # prefer the shortest containing/contained vocab tag for stability
            if best_sub is None or (len(v), v) < (len(best_sub), best_sub):
                best_sub = v
    if best_sub is not None:
        return best_sub
    # near edit distance (allow ~1 edit per 4 chars, deterministic tie-break)
    best = None
    best_d = 10 ** 9
    for v in vocab:
        d = _edit_distance(cand, v)
        thresh = max(1, min(len(cand), len(v)) // 4)
        if d <= thresh and (d < best_d or (d == best_d and v < best)):
            best, best_d = v, d
    return best


def canonicalize_tags(raw_tags, vocab: list, kb: str, allow_grow: bool):
    """Map a list of free-form tag candidates to controlled-vocabulary tags.

    Each candidate is matched to its nearest existing vocab tag. A candidate with
    no acceptable match is appended to the vocabulary (and persisted) ONLY when
    ``allow_grow`` is True; otherwise it is dropped. Returns (tags, grew?) where
    tags is deduped, order-stable, and clamped to <=5.
    """
    out: list = []
    seen = set()
    grew = False
    for raw in raw_tags or []:
        cand = _slug_tag(raw)
        if not cand:
            continue
        match = nearest_vocab_tag(cand, vocab)
        if match is None:
            if allow_grow:
                vocab.append(cand)
                match = cand
                grew = True
            else:
                continue
        if match not in seen:
            seen.add(match)
            out.append(match)
        if len(out) >= 5:
            break
    if grew:
        write_vocab(kb, vocab)
    return out, grew


# =========================================================================== #
# Deterministic ("mock") enrichment
# =========================================================================== #
# Keyword -> default-taxonomy category LABEL. Order matters: the FIRST matching
# group (scanned in this list order) wins, so the table is hand-ordered from most
# specific to most general. Labels are the "CC_slug" form jdid maps to CC.NN.
# Each entry: (category_label, [keywords...], [preferred_tags...]).
_CATEGORY_RULES = [
    ("11_llm-training",
     ["rlhf", "dpo", "ppo", "fine-tune", "fine-tuning", "finetune", "lora",
      "pretrain", "pre-train", "reward model", "alignment", "sft", "training run"],
     ["rlhf", "fine-tuning", "llm"]),
    ("12_llm-models",
     ["llm", "gpt", "claude", "gemini", "llama", "mistral", "qwen", "model card",
      "transformer", "language model", "context window", "tokenizer"],
     ["llm", "open-source", "benchmarks"]),
    ("13_agents",
     ["agent", "agents", "agentic", "tool use", "tool-use", "function calling",
      "mcp", "autonomous", "orchestration", "react loop"],
     ["agents", "llm"]),
    ("14_rag",
     ["rag", "retrieval", "vector db", "vector database", "embedding",
      "embeddings", "semantic search", "reranker", "chunking"],
     ["rag", "inference"]),
    ("15_audio-ml",
     ["tts", "text-to-speech", "speech", "voice", "audio", "encodec",
      "vall-e", "whisper", "asr", "vocoder"],
     ["tts", "inference"]),
    ("16_diffusion",
     ["diffusion", "stable diffusion", "image generation", "text-to-image",
      "img2img", "controlnet", "sora", "video generation"],
     ["diffusion", "open-source"]),
    ("17_inference",
     ["inference", "quantization", "quantize", "gguf", "vllm", "kv cache",
      "throughput", "latency", "serving", "tensorrt", "flash attention"],
     ["inference", "benchmarks"]),
    ("18_coding",
     ["code", "coding", "programming", "python", "rust", "typescript",
      "compiler", "refactor", "debugging", "git ", "pull request"],
     ["coding", "open-source"]),
    ("21_tools",
     ["tool", "app ", " app", "extension", "plugin", "library", "framework",
      "sdk", "open-sourced", "released", "launch"],
     ["tool", "open-source"]),
    ("22_datasets",
     ["dataset", "datasets", "corpus", "benchmark dataset", "training data",
      "data set", "huggingface dataset"],
     ["dataset", "open-source"]),
    ("23_apis-cli",
     ["api", "endpoint", "cli", "command line", "command-line", "terminal",
      "rest api", "webhook"],
     ["api", "cli"]),
    ("31_strategy",
     ["startup", "founder", "strategy", "moat", "wedge", "product-market",
      "pmf", "business model", "b2b", "saas"],
     ["startup", "growth"]),
    ("32_gtm",
     ["gtm", "go-to-market", "marketing", "growth", "acquisition", "funnel",
      "positioning", "launch strategy", "distribution"],
     ["gtm", "marketing", "growth"]),
    ("33_fundraising",
     ["fundraising", "raise", "seed round", "series a", "vc ", "venture",
      "valuation", "cap table", "investor"],
     ["fundraising", "startup"]),
    ("34_hiring",
     ["hiring", "recruiting", "interview", "candidate", "offer letter",
      "headcount", "team building"],
     ["hiring", "startup"]),
    ("41_investing",
     ["investing", "stocks", "equities", "portfolio", "etf", "index fund",
      "dividend", "valuation multiple"],
     ["investing", "markets"]),
    ("42_crypto",
     ["crypto", "bitcoin", "ethereum", "defi", "onchain", "on-chain", "token",
      "blockchain", "stablecoin"],
     ["crypto", "markets"]),
    ("43_personal-finance",
     ["personal finance", "budgeting", "savings", "retirement", "taxes",
      "mortgage", "net worth"],
     ["personal-finance"]),
    ("51_productivity",
     ["productivity", "workflow", "getting things done", "gtd", "time block",
      "time-block", "second brain", "deep work"],
     ["productivity", "focus"]),
    ("52_note-taking",
     ["note-taking", "notes", "obsidian", "notion", "zettelkasten", "pkm",
      "knowledge base", "knowledge management"],
     ["note-taking", "productivity"]),
    ("53_habits",
     ["habit", "habits", "routine", "discipline", "focus", "motivation",
      "procrastination"],
     ["habits", "focus"]),
    ("61_fitness",
     ["fitness", "workout", "lifting", "strength", "cardio", "running",
      "hypertrophy", "gym"],
     ["fitness"]),
    ("62_nutrition",
     ["nutrition", "diet", "protein", "calories", "fasting", "macros",
      "supplement"],
     ["nutrition", "fitness"]),
    ("63_health",
     ["sleep", "mental health", "anxiety", "meditation", "longevity",
      "recovery", "stress"],
     ["sleep", "mental-health"]),
    ("71_design",
     ["design", "ui ", "ux", "ui/ux", "figma", "wireframe", "color palette",
      "spacing", "layout"],
     ["ui-ux", "inspiration"]),
    ("72_typography",
     ["typography", "typeface", "font", "kerning", "leading", "type scale"],
     ["typography", "ui-ux"]),
    ("73_branding",
     ["branding", "brand", "logo", "identity", "visual identity", "style guide"],
     ["branding", "inspiration"]),
    ("81_tutorial",
     ["tutorial", "how to", "how-to", "step by step", "step-by-step",
      "walkthrough", "guide", "build a"],
     ["tutorial", "reference"]),
    ("82_explainer",
     ["explainer", "explained", "intuition", "understand", "deep dive",
      "deep-dive", "primer", "from scratch"],
     ["explainer", "reference"]),
    ("83_reference",
     ["reference", "cheatsheet", "cheat sheet", "documentation", "docs",
      "spec ", "specification", "career"],
     ["reference", "career"]),
]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WS_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+")
# Bare t.co tokens (a media-only tweet's full_text is typically just this short
# link). Stripped from the TL;DR source so it is never a lone t.co URL.
_TCO_RE = re.compile(r"(?:https?://)?t\.co/\S+", re.IGNORECASE)
_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{1,15}")
# Capitalized product/tool names (>=2 chars), incl. internal caps/digits like
# "EnCodec", "GPT-4", "VALL-E". Common sentence-initial words are filtered out.
_PRODUCT_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*)\b")
_STOP_PRODUCTS = {
    "The", "This", "That", "These", "Those", "A", "An", "And", "But", "Or",
    "If", "When", "While", "For", "With", "Without", "From", "Into", "Onto",
    "It", "Its", "We", "You", "They", "He", "She", "I", "My", "Our", "Your",
    "Here", "There", "Now", "Then", "How", "Why", "What", "Who", "Where",
    "Just", "New", "Most", "More", "Some", "Many", "All", "Every", "Each",
    "One", "Two", "Three", "Today", "Yesterday", "Thread", "TL", "DR", "RT",
}

TLDR_MAX = 140


def _clean_ws(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def _strip_for_tldr(text: str) -> str:
    """Remove urls (incl. bare t.co tokens) and leading mentions so the TL;DR
    reads as a claim, not a reply header or a lone short link. Keeps inline
    @handles that are part of a sentence."""
    t = _URL_RE.sub("", str(text or ""))
    t = _TCO_RE.sub("", t)   # scheme-less t.co/... left after the URL strip
    t = _clean_ws(t)
    # drop a leading run of @mentions (reply addressing)
    while True:
        m = re.match(r"^@[A-Za-z0-9_]{1,15}[\s,:]+", t)
        if not m:
            break
        t = t[m.end():]
    return _clean_ws(t)


def first_sentence(text: str) -> str:
    t = _strip_for_tldr(text)
    if not t:
        return ""
    parts = _SENTENCE_SPLIT_RE.split(t)
    return parts[0].strip() if parts else t


def make_tldr(text: str) -> str:
    """Cleaned first sentence, truncated to ~TLDR_MAX chars on a word boundary
    with an ellipsis. Deterministic."""
    s = first_sentence(text)
    if not s:
        return ""
    if len(s) <= TLDR_MAX:
        return s
    cut = s[:TLDR_MAX]
    sp = cut.rfind(" ")
    if sp >= 40:  # keep at least a meaningful prefix
        cut = cut[:sp]
    return cut.rstrip(" ,;:-") + "…"


def _distinctive_caps(tok: str) -> bool:
    """A token whose casing/shape marks it as a product/proper noun regardless of
    position: internal capital (EnCodec), a digit (GPT-4), or a hyphen (VALL-E)."""
    return bool(re.search(r"[A-Za-z][A-Z0-9]", tok)) or "-" in tok or any(c.isdigit() for c in tok)


def extract_entities(nb: schema.NormalizedBookmark) -> list:
    """Tools/products/@handles + outbound urls named in the item. Deterministic,
    order-stable, deduped. Mentions come from the normalized field first, then
    inline @handles, then capitalized product-like tokens, then urls.

    A plain Capitalized word that merely starts a sentence is NOT an entity (it
    is grammar, not a name); we only keep it when it has distinctive casing
    (internal caps / digit / hyphen) or recurs later in lowercase-free position.
    """
    out: list = []
    seen = set()

    def add(x):
        x = str(x).strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    for h in nb.mentions or []:
        add(h)

    body_text = " ".join([nb.text or ""] + list(nb.thread_texts or []))
    for m in _HANDLE_RE.findall(body_text):
        add(m)

    # Walk sentence by sentence so the first word of each sentence is treated as
    # grammar unless its shape is distinctive. A distinctive-caps token counts
    # anywhere. This keeps "EnCodec"/"VALL-E"/"GPT-4" and drops "First"/"Worth".
    cleaned = _URL_RE.sub(" ", body_text)
    for sentence in _SENTENCE_SPLIT_RE.split(cleaned):
        toks = _PRODUCT_RE.findall(sentence)
        for pos, tok in enumerate(toks):
            if tok in _STOP_PRODUCTS:
                continue
            if _distinctive_caps(tok):
                add(tok)
            elif pos > 0 and len(tok) >= 4:
                # not sentence-initial, a multi-char proper-noun-like word
                add(tok)

    for u in nb.urls or []:
        add(u)
    return out[:12]


def _haystack(nb: schema.NormalizedBookmark) -> str:
    bits = [nb.text or ""]
    bits.extend(nb.thread_texts or [])
    bits.extend(nb.hashtags or [])
    if nb.quoted and isinstance(nb.quoted, dict):
        bits.append(str(nb.quoted.get("text", "")))
    return " " + _clean_ws(" ".join(bits)).lower() + " "


def classify(nb: schema.NormalizedBookmark):
    """Return (category_label, preferred_tags) for a normalized item using the
    deterministic keyword rules. First matching rule (by table order) wins.
    A deleted snapshot with no usable text falls to the Inbox area."""
    hay = _haystack(nb)
    for label, keywords, pref in _CATEGORY_RULES:
        for kw in keywords:
            if kw in hay:
                return label, list(pref)
    # No keyword matched: route by hashtags via the jdid alias table if possible.
    for ht in nb.hashtags or []:
        cc = jdid.code_for_label(ht)
        if cc:
            slug = jdid.area_for_code(cc)[0]
            return "{:02d}_{}".format(cc, slug), []
    return "00_inbox", []


def pick_tags(nb: schema.NormalizedBookmark, preferred: list, vocab: list, kb: str):
    """Choose 2..5 controlled-vocabulary tags. Sources, in priority order:
      1. the category's preferred tags,
      2. the item's own hashtags (mapped to vocab),
      3. type-derived tags (thread/media/link),
      4. keyword hits in the text that ARE vocab tags.
    Tags are canonicalized against the vocabulary; a new tag is appended only
    when a hashtag genuinely fits nothing. Returns the final tag list.
    """
    candidates: list = []
    candidates.extend(preferred)
    candidates.extend(nb.hashtags or [])
    # type signal
    if nb.type == "thread":
        candidates.append("explainer")
    if nb.type in ("media",) or nb.needs_ocr:
        candidates.append("inspiration")
    if nb.type == "link":
        candidates.append("reference")
    # direct vocabulary keyword hits in the body
    hay = _haystack(nb)
    for v in vocab:
        # match the vocab word as a token (handle kebab tags too)
        token = v.replace("-", " ")
        if " " + token + " " in hay or " " + v + " " in hay:
            candidates.append(v)

    # canonicalize: preferred/derived tags are already in-vocab; hashtags MAY
    # grow the vocab (they are user-authored signal worth keeping).
    chosen, _ = canonicalize_tags(candidates, vocab, kb, allow_grow=True)

    # guarantee a minimum of 2 tags using the category's area as a backstop.
    if len(chosen) < 2:
        cc = jdid.code_for_label(classify(nb)[0])
        area_slug = jdid.area_for_code(cc)[0]
        backstop = nearest_vocab_tag(area_slug, vocab) or area_slug
        for b in (backstop, "reference"):
            bb = nearest_vocab_tag(b, vocab)
            if bb and bb not in chosen:
                chosen.append(bb)
            if len(chosen) >= 2:
                break
    return chosen[:5]


def make_key_points(nb: schema.NormalizedBookmark) -> list:
    """0..4 bullets, threads ONLY (per the contract). Each bullet is the cleaned
    first sentence of a thread tweet after the first, truncated. Deterministic."""
    if nb.type != "thread":
        return []
    points: list = []
    for txt in (nb.thread_texts or [])[1:]:
        s = first_sentence(txt)
        if not s:
            continue
        if len(s) > TLDR_MAX:
            s = s[:TLDR_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
        points.append(s)
        if len(points) >= 4:
            break
    return points


def make_why_saved(nb: schema.NormalizedBookmark, category: str) -> str:
    """One-line inferred intent. Heuristic over type + category + content."""
    if nb.deleted:
        return "snapshot of a since-deleted tweet kept for the record"
    hay = _haystack(nb)
    if nb.type == "link" or nb.urls:
        if any(k in hay for k in ("github.com", "released", "launch", "open-source", "open sourced")):
            return "tool/repo to try"
        return "linked resource to read later"
    if nb.type == "thread":
        return "explainer/reference thread to revisit"
    if nb.type == "media" or nb.needs_ocr:
        return "visual reference to keep"
    if nb.type == "quote":
        return "commentary on another take worth remembering"
    cc = jdid.code_for_label(category)
    if cc in (10, 20):
        return "technical reference to revisit"
    if cc == 30:
        return "business/startup idea to apply"
    if any(k in hay for k in ("how to", "how-to", "guide", "tutorial", "step")):
        return "how-to to follow later"
    return "insight worth remembering"


def _media_text(nb: schema.NormalizedBookmark) -> str:
    """Alt/ocr summary for a media-only item: first alt, else first ocr, else a
    synthetic "Image from @handle". Never a bare t.co link."""
    for m in nb.media or []:
        if (m.alt or "").strip():
            return m.alt.strip()
    for m in nb.media or []:
        if (m.ocr or "").strip():
            return m.ocr.strip()
    who = nb.author_handle or "the author"
    return "Image from {}".format(who)


def _tldr_source(nb: schema.NormalizedBookmark) -> str:
    """The text the TL;DR is built from. For a media-only item (type 'media' or
    needs_ocr) whose text is empty or just a t.co short link, use the media
    alt/ocr summary instead of the raw t.co URL. Otherwise use the tweet text
    (then the thread root)."""
    if getattr(nb, "article_summary", ""):
        # Twitter Article: the preview/abstract is the right TL;DR source.
        return nb.article_summary
    base = nb.text if nb.text else (nb.thread_texts[0] if nb.thread_texts else "")
    if nb.type == "media" or nb.needs_ocr:
        # Real words after stripping urls/t.co? keep them; else summarize media.
        if not _strip_for_tldr(base):
            return _media_text(nb)
    return base


def enrich_one_mock(nb: schema.NormalizedBookmark, vocab: list, kb: str) -> schema.EnrichedBookmark:
    """Deterministic enrichment of a single normalized item."""
    category, preferred = classify(nb)
    tags = pick_tags(nb, preferred, vocab, kb)
    return schema.EnrichedBookmark(
        status_id=nb.status_id,
        category=category,
        tags=tags,
        tldr=make_tldr(_tldr_source(nb)),
        key_points=make_key_points(nb),
        why_saved=make_why_saved(nb, category),
        entities=extract_entities(nb),
        content_hash=nb.content_hash,
    )


# =========================================================================== #
# Caching + batching helpers
# =========================================================================== #
def _enriched_dir(kb: str) -> str:
    return os.path.join(util.state_dir(kb), "enriched")


def _batches_dir(kb: str) -> str:
    return os.path.join(util.state_dir(kb), "batches")


def load_enriched_cache(kb: str):
    """Return a dict content_hash -> EnrichedBookmark.to_dict() for every item
    already enriched (scanning all enriched/*.out.jsonl). Empty content_hash is
    not cacheable (cannot dedup), so it is skipped."""
    cache = {}
    d = _enriched_dir(kb)
    if not os.path.isdir(d):
        return cache
    for name in sorted(os.listdir(d)):
        if not name.endswith(".out.jsonl"):
            continue
        for obj in schema.iter_jsonl(os.path.join(d, name)):
            ch = str(obj.get("content_hash", ""))
            if ch:
                cache[ch] = obj
    return cache


def _next_batch_index(kb: str) -> int:
    """Lowest unused 3-digit batch index across enriched/ and batches/."""
    used = set()
    for d in (_enriched_dir(kb), _batches_dir(kb)):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            m = re.match(r"^(\d{3})\.", name)
            if m:
                used.add(int(m.group(1)))
    i = 0
    while i in used:
        i += 1
    return i


def _batch_name(idx: int) -> str:
    return "{:03d}".format(idx)


def read_new_items(kb: str):
    """Load NormalizedBookmark records from .state/new_items.jsonl."""
    path = os.path.join(util.state_dir(kb), "new_items.jsonl")
    items = []
    for obj in schema.iter_jsonl(path):
        items.append(schema.NormalizedBookmark.from_dict(obj))
    return items


def write_enriched_batch(kb: str, idx: int, enriched_rows):
    """Write one enriched batch atomically to enriched/NNN.out.jsonl."""
    util.ensure_dir(_enriched_dir(kb))
    path = os.path.join(_enriched_dir(kb), _batch_name(idx) + ".out.jsonl")
    lines = [json.dumps(r.to_dict() if hasattr(r, "to_dict") else r,
                        ensure_ascii=False, sort_keys=True)
             for r in enriched_rows]
    util.atomic_write(path, ("\n".join(lines) + "\n") if lines else "")
    return path


def filter_uncached(items, cache):
    """Split items into (to_do, reused_dicts). An item whose content_hash is in
    the cache is reused verbatim (status_id refreshed to the current record).
    Items with an empty content_hash are always processed (cannot be cached)."""
    to_do = []
    reused = []
    for nb in items:
        ch = nb.content_hash
        if ch and ch in cache:
            cached = dict(cache[ch])
            cached["status_id"] = nb.status_id  # follow id moves, keep enrichment
            reused.append(cached)
        else:
            to_do.append(nb)
    return to_do, reused


# =========================================================================== #
# Engine: mock
# =========================================================================== #
def run_mock(kb: str, items, cache, batch_size: int) -> dict:
    vocab = load_vocab(kb)
    to_do, reused = filter_uncached(items, cache)
    written_files = []
    # Only items NOT already in the cache produce new enriched rows. Cache hits
    # already live in an existing enriched/*.out.jsonl, so re-emitting them would
    # duplicate content_hashes on disk and break the "re-run is a no-op"
    # guarantee. We therefore write ONLY the freshly computed rows; unchanged
    # input yields zero new files.
    new_rows = [enrich_one_mock(nb, vocab, kb) for nb in to_do]
    if not new_rows:
        return {"engine": "mock", "new": 0, "reused": len(reused), "files": []}
    bs = max(1, batch_size)
    start = _next_batch_index(kb)
    for i in range(0, len(new_rows), bs):
        idx = start + (i // bs)
        chunk = new_rows[i:i + bs]
        written_files.append(write_enriched_batch(kb, idx, chunk))
    return {"engine": "mock", "new": len(new_rows), "reused": len(reused),
            "files": written_files}


# =========================================================================== #
# Engine: agent (prepare batch prompts; read back answers)
# =========================================================================== #
def _taxonomy_block() -> str:
    lines = ["TAXONOMY (pick ONE category label per item; the numeric prefix is "
             "its Johnny.Decimal code):"]
    for lo, hi, slug, desc in jdid.DEFAULT_TAXONOMY:
        lines.append("  {:02d}-{:02d}  {:<14} {}".format(lo, hi, slug, desc))
    lines.append("")
    lines.append("Use a category LABEL like \"11_llm-training\" (CC_slug). If "
                 "nothing fits, use \"00_inbox\".")
    return "\n".join(lines)


def _agent_instructions(vocab: list) -> str:
    return "\n".join([
        "You are enriching saved X/Twitter bookmarks. For EACH item below, emit",
        "exactly one compact JSON object (one per line) into the sibling file",
        "named <thisfile-without-.txt>.out.jsonl, with these keys:",
        "  status_id    (copy from the item)",
        "  category     ONE label from the taxonomy below (e.g. 11_llm-training)",
        "  tags         2..5 tags, chosen from the CONTROLLED VOCABULARY below;",
        "               propose a NEW tag ONLY if nothing in the list fits",
        "  tldr         one sentence, the single claim/takeaway (<=140 chars)",
        "  key_points   0..4 bullets, THREADS ONLY (else [])",
        "  why_saved    one line, the inferred reason it was saved",
        "  entities     tools/products/@handles named in the item",
        "  content_hash (copy from the item, verbatim -- it keys the cache)",
        "",
        "Rules: no invented facts; summarize quoted tweets as the quoted claim;",
        "for media-only items summarize the alt/ocr text; keep tags lowercase",
        "kebab-case. Output JSONL only, one object per line, no prose.",
        "",
        "CONTROLLED VOCABULARY (prefer these tags):",
        "  " + ", ".join(vocab),
    ])


def _item_block(nb: schema.NormalizedBookmark) -> str:
    """A compact, token-lean rendering of one normalized item for the agent."""
    d = {
        "status_id": nb.status_id,
        "content_hash": nb.content_hash,
        "type": nb.type,
        "author": nb.author_handle,
        "url": nb.url,
        "text": nb.text,
    }
    if nb.thread_texts:
        d["thread_texts"] = list(nb.thread_texts)
    if nb.hashtags:
        d["hashtags"] = list(nb.hashtags)
    if nb.urls:
        d["urls"] = list(nb.urls)
    if nb.quoted:
        qt = nb.quoted if isinstance(nb.quoted, dict) else {}
        d["quoted"] = {"author": qt.get("author_handle", ""),
                       "text": qt.get("text", "")}
    if nb.media:
        d["media"] = [{"kind": m.kind, "alt": m.alt, "ocr": m.ocr}
                      for m in nb.media]
    if nb.deleted:
        d["deleted"] = True
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def write_batch_prompt(kb: str, idx: int, chunk, vocab: list) -> str:
    util.ensure_dir(_batches_dir(kb))
    name = _batch_name(idx)
    path = os.path.join(_batches_dir(kb), name + ".txt")
    out_name = name + ".out.jsonl"
    body = []
    body.append("# BOOKMARKS ENRICHMENT BATCH {}".format(name))
    body.append("# Answer into: .state/batches/{}".format(out_name))
    body.append("")
    body.append(_agent_instructions(vocab))
    body.append("")
    body.append(_taxonomy_block())
    body.append("")
    body.append("ITEMS ({}):".format(len(chunk)))
    for nb in chunk:
        body.append(_item_block(nb))
    util.atomic_write(path, "\n".join(body) + "\n")
    return path


def read_batch_answers(kb: str, idx: int, by_status, by_hash, vocab: list, kb_for_grow: str):
    """Read a batches/NNN.out.jsonl the host agent filled in. Each line is
    coerced to an EnrichedBookmark, its tags canonicalized against the vocab,
    and content_hash recovered from the matching normalized item when missing.
    Returns a list of EnrichedBookmark, or None if the answer file is absent."""
    name = _batch_name(idx)
    out_path = os.path.join(_batches_dir(kb), name + ".out.jsonl")
    if not os.path.exists(out_path):
        return None
    rows = []
    for obj in schema.iter_jsonl(out_path):
        eb = schema.EnrichedBookmark.from_dict(obj)
        # recover content_hash from the source normalized item if the agent
        # dropped it (cache keys on it).
        if not eb.content_hash and eb.status_id in by_status:
            eb.content_hash = by_status[eb.status_id].content_hash
        eb.tags, _ = canonicalize_tags(eb.tags, vocab, kb_for_grow, allow_grow=True)
        # sanity-clamp category to a real label form; unknown -> inbox.
        if not eb.category:
            eb.category = "00_inbox"
        rows.append(eb)
    return rows


def run_agent(kb: str, items, cache, batch_size: int) -> dict:
    """Two-phase. Phase A: write batch prompts for items that have no answer yet.
    Phase B: read back any answered batches and promote them into enriched/.
    Both phases run every invocation, so the loop is: run -> agent answers ->
    run again. Cache hits are promoted immediately without a prompt."""
    vocab = load_vocab(kb)
    to_do, reused = filter_uncached(items, cache)
    by_status = {nb.status_id: nb for nb in items}
    by_hash = {nb.content_hash: nb for nb in items if nb.content_hash}

    util.ensure_dir(_batches_dir(kb))
    util.ensure_dir(_enriched_dir(kb))

    # ---- Phase B: harvest any already-answered batches first ---------------- #
    promoted_files = []
    answered_hashes = set()
    existing_batches = []
    if os.path.isdir(_batches_dir(kb)):
        for name in sorted(os.listdir(_batches_dir(kb))):
            m = re.match(r"^(\d{3})\.txt$", name)
            if m:
                existing_batches.append(int(m.group(1)))
    for idx in existing_batches:
        # skip if already promoted to enriched/
        enr_path = os.path.join(_enriched_dir(kb), _batch_name(idx) + ".out.jsonl")
        if os.path.exists(enr_path):
            for obj in schema.iter_jsonl(enr_path):
                if obj.get("content_hash"):
                    answered_hashes.add(obj["content_hash"])
            continue
        answers = read_batch_answers(kb, idx, by_status, by_hash, vocab, kb)
        if answers is None:
            continue
        write_enriched_batch(kb, idx, answers)
        promoted_files.append(enr_path)
        for eb in answers:
            if eb.content_hash:
                answered_hashes.add(eb.content_hash)

    # Cache hits already live in an existing enriched/*.out.jsonl, so they are
    # neither re-prompted nor re-written (that would duplicate content_hashes and
    # break the "re-run is a no-op" guarantee).
    # ---- Phase A: write prompts for items still without an answer ----------- #
    pending = [nb for nb in to_do if nb.content_hash not in answered_hashes]
    prompt_files = []
    bs = max(1, batch_size)
    if pending:
        start = _next_batch_index(kb)
        for i in range(0, len(pending), bs):
            idx = start + (i // bs)
            chunk = pending[i:i + bs]
            prompt_files.append(write_batch_prompt(kb, idx, chunk, vocab))

    return {
        "engine": "agent",
        "pending": len(pending),
        "prompts_written": prompt_files,
        "promoted": promoted_files,
        "reused": len(reused),
        "note": ("Batch prompts written. The host agent should read each "
                 ".state/batches/NNN.txt and write answers to the sibling "
                 ".state/batches/NNN.out.jsonl, then re-run this command to "
                 "promote them.") if prompt_files else
                "All items already enriched or answered.",
    }


# =========================================================================== #
# Engine: api (documented stub)
# =========================================================================== #
API_KEY_ENV = "BOOKMARKS_LLM_API_KEY"
API_BASE_ENV = "BOOKMARKS_LLM_API_BASE"
API_MODEL_ENV = "BOOKMARKS_LLM_MODEL"
DEFAULT_API_BASE = "https://api.anthropic.com/v1/messages"
DEFAULT_API_MODEL = "claude-haiku (cheap-model class)"


def run_api(kb: str, items, cache, batch_size: int) -> dict:
    """Documented stub for unattended bulk enrichment via a cheap model.

    Configuration (NONE of which is required for tests; no network here):
        {API_KEY_ENV}   the API key. ABSENT -> this stub does NO network call
                        and falls back to the deterministic mock engine so the
                        pipeline still produces enriched/*.out.jsonl.
        {API_BASE_ENV}  endpoint override (default: {DEFAULT_API_BASE}).
        {API_MODEL_ENV} model id (default: {DEFAULT_API_MODEL}).

    When a key IS present, a real implementation would, per batch of
    --batch-size items: build the same prompt run_agent writes (taxonomy + the
    controlled vocabulary + the compact item blocks), POST it to the endpoint
    with the key, parse the JSONL reply, canonicalize tags against tags.txt, and
    write enriched/NNN.out.jsonl -- identical output to the other two engines.
    That POST is intentionally NOT performed in this stub to keep the agent path
    network-free and tests hermetic; wire it in behind the key check below.
    """
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        result = run_mock(kb, items, cache, batch_size)
        result["engine"] = "api"
        result["api"] = "no key (${} unset); used deterministic fallback".format(API_KEY_ENV)
        return result
    # Key present but the network call is intentionally not made in this stub.
    base = os.environ.get(API_BASE_ENV, DEFAULT_API_BASE)
    model = os.environ.get(API_MODEL_ENV, DEFAULT_API_MODEL)
    result = run_mock(kb, items, cache, batch_size)
    result["engine"] = "api"
    result["api"] = ("key detected -> would POST {} batches to {} (model {}); "
                     "network disabled in this stub, used deterministic fallback"
                     .format((len(items) // max(1, batch_size)) + 1, base, model))
    return result


# Fill the docstring placeholders for `run_api` once at import time.
run_api.__doc__ = (run_api.__doc__ or "").format(
    API_KEY_ENV=API_KEY_ENV, API_BASE_ENV=API_BASE_ENV,
    API_MODEL_ENV=API_MODEL_ENV, DEFAULT_API_BASE=DEFAULT_API_BASE,
    DEFAULT_API_MODEL=DEFAULT_API_MODEL,
)


# =========================================================================== #
# CLI
# =========================================================================== #
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="enrich.py",
        description="Categorize, tag, and summarize new bookmarks "
                    "(mock|agent|api engines; output is engine-agnostic).")
    p.add_argument("--kb", required=True, help="KB root directory")
    p.add_argument("--engine", required=True, choices=["mock", "agent", "api"],
                   help="enrichment engine")
    p.add_argument("--batch-size", type=int, default=20,
                   help="items per batch (default 20)")
    args = p.parse_args(argv)

    kb = os.path.abspath(os.path.expanduser(args.kb))
    util.ensure_dir(util.state_dir(kb))

    items = read_new_items(kb)
    cache = load_enriched_cache(kb)

    if args.engine == "mock":
        result = run_mock(kb, items, cache, args.batch_size)
    elif args.engine == "agent":
        result = run_agent(kb, items, cache, args.batch_size)
    else:
        result = run_api(kb, items, cache, args.batch_size)

    result["items"] = len(items)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
