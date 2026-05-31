# RETRIEVAL.md: the query algorithm and citation format

Follow this verbatim whenever the user asks a question about their bookmarks.
Retrieval is the most-used path and the cheapest by design: grep a pre-computed
index, open only a few notes. Target cost ~1.5–3.5k tokens per query, flat as the
KB grows. `scripts/query.py` already implements all of step 1–5; prefer running
it over doing the steps by hand.

## The fast path (one command)

```
python3 scripts/query.py --kb <KB> "the user's question" --k 5
python3 scripts/query.py --kb <KB> "rlhf reward model" --json   # structured
```

`query.py` returns ranked results, each with its source (`id, title, tldr,
author, url, date, type, tags, why_saved, key_points, path`). Then format the
answer per the **Answer format** below. The steps that follow describe exactly
what `query.py` does, for when you need to reason about or reproduce it.

## Step-by-step algorithm

1. **Expand the query in-context (free).** Turn the question into 3–6
   keywords / synonyms + likely tags. Keep the user's own meaningful tokens
   first (exact matches should rank ahead of synonyms), add light stems
   (`finetuning`→`finetune`), and one hop of domain synonyms
   (`dpo`→`rlhf, ppo, preference`). This single step ~10×'d grep accuracy in the
   research, so it runs every time. Honor explicit filters the user types:
   `#tag`, `from:@handle`, `area:ai`, `is:thread`.

2. **Grep `INDEX.tsv`; never read it whole.** Match ANY expansion term with a
   single case-insensitive alternation, streamed and capped:
   ```
   rg -i -N -m 40 'dpo|ppo|rlhf|reward model|alignment' <KB>/INDEX.tsv
   ```
   If `rg` is absent, a pure-Python streamed line scan with the same cap is used.
   Only matching rows enter context; the index is never slurped into memory.
   Each row is `id ⇥ title ⇥ tags ⇥ tldr ⇥ author ⇥ date ⇥ type ⇥ url`.

3. **Rank with FTS5 only if needed.** If grep returns too many candidates (>40)
   or too few (< `--k`), order/rescue via the optional `.state/kb.db`:
   ```
   sqlite3 <KB>/.state/kb.db \
     "SELECT id FROM bm WHERE bm MATCH 'dpo OR ppo OR rlhf' ORDER BY bm25(bm) LIMIT 20"
   ```
   The kb.db is rebuildable and optional; never block on it. If it's missing or
   errors, fall back to a lexical score over the grepped rows (title/tag hits
   weigh highest, then tldr, then author/url; earlier query terms weigh more).

4. **Pick the top 3–5 ids** from the one-line summaries. Do not open notes you
   won't cite.

5. **Open only those notes** by `id` → path on disk
   (`<area>/<CC_cat>/CC.NN_*.md`). Read each note's body for the richer TL;DR,
   `why_saved`, key points, and links. Only `k` notes are ever opened, so token
   cost stays flat as the KB grows.

6. **Answer with the source shown** (see below). If confidence is low, say so
   rather than fabricate. Never cite an id that isn't a real `INDEX.tsv` row.

## Answer format (source attribution is the trust lever)

Lead with a short synthesis (1–3 sentences) that actually answers the question,
then list the result rows. Every claim is backed by a cited bookmark; never a
bare assertion.

```
<one to three sentences synthesizing the answer>

1. [11.01] DPO vs PPO for RLHF: a practical comparison
   DPO drops the separate reward model; simpler pipeline, often matches PPO.
   @someone · 2026-05-12 · thread · #rlhf #fine-tuning
   why: reference for the reward-model-free argument
   https://x.com/someone/status/1730000000000000000
   note: <KB>/10-19_ai-tech/11_llm-training/11.01_dpo-vs-ppo.md

2. [12.03] …
```

Each result row shows: the `[id]` and title, the TL;DR, then a meta line
(author · date · type · tags), the `why:` line, the original URL, and the local
note path. `query.py --json` emits the same records as a JSON array.

## Hard rules

- **Never cite a non-existent id.** Every emitted id must be a real `INDEX.tsv`
  row; if an FTS rescue id has no backing row, drop it rather than cite it. A
  `path` of `""` means no note file backs the row; show the row, omit the path,
  don't invent one.
- **Never load the whole index** for matching; stream and cap.
- **Don't enrich at query time.** Retrieval greps pre-computed summaries; it
  never calls an LLM to summarize. If the notes don't answer the question, say
  so and suggest a `sync` or different keywords.
- **Honest zero-result state.** If nothing matches, say so plainly and suggest
  fewer/different keywords or a sync; do not pad with loosely-related rows.
- **Respect `--k`.** Default 5. Don't over-return.
