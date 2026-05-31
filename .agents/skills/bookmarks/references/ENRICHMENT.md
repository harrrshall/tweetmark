# ENRICHMENT.md: the agent enrichment contract

`enrich.py --engine agent` writes batch prompt files to
`.state/batches/NNN.txt`. Each file inlines the taxonomy and the controlled
vocabulary **once**, then lists ~`--batch-size` items as compact JSON. Your job:
read a batch, write one JSON object per item into the sibling
`.state/batches/NNN.out.jsonl`, then re-run `enrich.py --engine agent` to promote
the answers into `.state/enriched/NNN.out.jsonl`. Only one batch is ever in your
context, so cost stays flat.

## Output schema (one compact JSON object per line, JSONL only; no prose)

```json
{"status_id":"…","category":"11_llm-training","tags":["rlhf","fine-tuning"],
 "tldr":"…","key_points":[],"why_saved":"…","entities":["@x","Tool"],
 "content_hash":"…"}
```

| Key            | Rule                                                                          |
|----------------|-------------------------------------------------------------------------------|
| `status_id`    | Copy verbatim from the item.                                                  |
| `category`     | Exactly ONE `CC_slug` label from the taxonomy (e.g. `11_llm-training`). If nothing fits, `00_inbox`. |
| `tags`         | 2–5 tags from the controlled vocabulary. New tag ONLY if nothing fits.       |
| `tldr`         | One sentence, ≤140 chars, the single claim/takeaway.                          |
| `key_points`   | 0–4 bullets, **threads only**. Empty list `[]` for everything else.          |
| `why_saved`    | One line, the inferred reason it was saved.                                   |
| `entities`     | Tools/products/`@handles` named in the item.                                 |
| `content_hash` | Copy verbatim; it keys the dedup cache. (Recovered from `status_id` if you drop it, but copy it.) |

## Batch prompt template (what each `.txt` already contains)

```
# BOOKMARKS ENRICHMENT BATCH NNN
# Answer into: .state/batches/NNN.out.jsonl

<instructions: the schema above + the rules below>

TAXONOMY (pick ONE category label per item):
  00-09  inbox          …
  10-19  ai-tech        …
  … (all 10 areas) …
Use a category LABEL like "11_llm-training" (CC_slug). If nothing fits, "00_inbox".

CONTROLLED VOCABULARY (prefer these tags):
  llm, rlhf, fine-tuning, prompting, agents, rag, … (the full tags.txt)

ITEMS (N):
{"status_id":"…","content_hash":"…","type":"thread","author":"@x","url":"…","text":"…", …}
{ …one compact JSON item per line… }
```

You don't build this file; `enrich.py` does. You only read it and produce the
`.out.jsonl`. The item JSON carries the fields relevant to its type:
`thread_texts` for threads, `quoted` for quote tweets, `media` (kind/alt/ocr)
for media items, `urls`/`hashtags` when present, `deleted:true` for tombstones.

## Summary templates

- **tldr**: one declarative sentence stating the claim, not a description of the
  tweet. Strip URLs and leading `@`-mentions (reply addressing). Good:
  "DPO drops the separate reward model and often matches PPO at lower compute."
  Bad: "@someone shares thoughts about RLHF in this thread."
- **key_points** (threads only); up to 4 bullets, each the core point of a
  later tweet in the thread, in order. One sentence each, ≤140 chars.
- **why_saved**: one line of inferred intent: "tool to try", "reference for the
  reward-model-free argument", "for project X", "visual reference to keep". This
  is the single most useful retrieval signal after the title; make it concrete.
- **entities**: named tools/products/models/`@handles` (e.g. `EnCodec`,
  `GPT-4`, `VALL-E`, `@karpathy`), plus outbound URLs. Proper nouns only; a
  capitalized sentence-initial common word is grammar, not an entity.

## Per-tweet-type rules

| Type      | tldr                                  | key_points          | Notes |
|-----------|---------------------------------------|---------------------|-------|
| `tweet`   | The single claim/takeaway.            | `[]`                | For quotes/hot-takes, keep the verbatim wording in mind so the tldr is faithful. |
| `thread`  | The thread's overall thesis.          | 3–5 ordered points  | The whole reply chain is ONE item; summarize the arc, not tweet #1 alone. |
| `quote`   | Summarize BOTH the comment and the quoted tweet, labeled.| `[]` | Treat the quoted text as the claim being reacted to; never invent what the quoted tweet said. |
| `media`   | Summarize the image/video via alt/OCR text first.| `[]` | If `media.alt`/`media.ocr` is empty there is no text to summarize; describe only what the alt/ocr provides; do not hallucinate image contents. |
| `link`    | Summarize the linked content; the tweet is a pointer.| `[]` | Keep the resolved URL in `entities`; the value is the destination. |
| `deleted` | Use the snapshot text; note it's a since-deleted tweet.| `[]` | Snapshot only; `why_saved` ≈ "kept for the record". Never merged into a thread. |

Type precedence (set by `ingest.py`, do not override):
`deleted > thread > quote > media > link > tweet`.

## Controlled-vocabulary rules (anti-sprawl)

1. **Pick from `tags.txt` first.** It's inlined in the batch. Prefer an existing
   tag over a near-synonym (`fine-tune` → `fine-tuning`, `notes` →
   `note-taking`).
2. **Propose a new tag only when nothing fits.** Lowercase, kebab-case, singular
   where natural. `enrich.py` maps your free-form tags to the nearest existing
   vocab tag (exact → substring ≥4 chars → small edit distance); a genuinely new
   tag is appended to `tags.txt` only when no match is close. Don't rely on
   creative tags surviving; they get canonicalized.
3. **2–5 tags, deduped.** Lists longer than 5 are clamped. Aim for tags that are
   facets you'd actually search by (topic, modality, intent), not restatements
   of the category.
4. **No `#`, no spaces, no capitals.** `ui-ux`, not `UI/UX` or `#UIUX`.

## Caching and re-runs

An item whose `content_hash` already appears in any `enriched/*.out.jsonl` is
**not** re-enriched; copy `content_hash` faithfully so the cache works. Re-running
`enrich.py` on unchanged input is a no-op; only new/changed items get prompts.
An edited tweet has a new `content_hash`, so exactly that item is re-enriched.
