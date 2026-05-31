---
name: bookmarks
description: Imports, syncs, searches, and organizes the user's X/Twitter bookmarks into a local, human-readable, searchable knowledge base. Use when the user wants to import or sync their X (Twitter) bookmarks, ask questions about or search their saved tweets, browse them in a UI, or organize and clean up the collection.
---

# Bookmarks: X/Twitter bookmarks to a searchable knowledge base

This skill turns the user's X (Twitter) bookmarks into a local knowledge base of
plain markdown notes plus a grep-able `INDEX.tsv` and a self-contained `kb.html`
UI. The expensive work (categorize, tag, summarize) happens once at ingest and is
cached forever; retrieval is a cheap grep, flat in cost as the KB grows.

All logic lives in stdlib-only Python under `scripts/`. Nothing here installs
packages. The whole ingest/enrich/build/index/query pipeline is fully offline;
only two steps touch the network, both explicit and opt-in: `collect.py` (the
single browser step, which reuses the user's already-logged-in Chrome session)
and `doctor.py --dead-links` (plain HTTP link checks; pass `--offline` to skip).

## The knowledge base

Default location: `~/Documents/Twitter Bookmarks` (override with `$BOOKMARKS_KB`
or `--kb`). Layout:

```
<KB>/
  INDEX.tsv              one tab-separated row per bookmark (the grep target)
  00_START-HERE.md       generated plain-English guide + category counts
  10-19_ai-tech/         Johnny.Decimal area folders, each with _README.md
    11_llm-training/
      11.01_dpo-vs-ppo.md   one note per bookmark, YAML frontmatter + body
  tags.txt               controlled tag vocabulary
  kb.html                self-contained UI (embeds the index as JSON)
  .media/                local image cache (only when build_kb --cache-media ran)
  .state/
    bookmarks_raw.jsonl  append-only raw capture (collect.py)
    seen.tsv             status_id -> content_hash ledger (idempotency)
    new_items.jsonl      new/changed normalized items (ingest.py)
    enriched/NNN.out.jsonl   cached enrichment output
    batches/NNN.txt      agent-engine batch files (enrich --engine agent)
    kb.db                rebuildable SQLite FTS5 index (optional)
    config.json          remembered profile, etc.
```

The markdown notes + `INDEX.tsv` are the single source of truth. The agent, the
`kb.html` UI, and any editor all read the same files, so the three surfaces never
diverge.

## The six verbs

Each plain-English verb maps to exactly one script. Run scripts with `python3`
from this skill's `scripts/` directory. `<KB>` is the knowledge-base path.

### import  — first full export
Collect every bookmark, then build the whole KB.
```
python3 scripts/collect.py  --kb <KB> --full
python3 scripts/ingest.py   --kb <KB>
python3 scripts/enrich.py   --kb <KB> --engine mock
python3 scripts/build_kb.py --kb <KB>
python3 scripts/index.py    --kb <KB>
```
Use `--engine agent` on `enrich.py` to enrich with your own inference (it writes
batch files under `.state/batches/` for you to read and answer); use
`--engine mock` for a deterministic, zero-token first pass.

Add `--cache-media` to `build_kb.py` to download each item's first image still
into `<KB>/.media/` for fully offline thumbnails. It is opt-in and off by default
(the only `build_kb.py` step that touches the network); leave it off and the UI
hotlinks thumbnails straight from the X CDN.

To finish: print `Imported N bookmarks into <KB>` (N = the notes `build_kb.py`
created+updated, or `wc -l <KB>/INDEX.tsv`) and open `<KB>/kb.html` via
`xdg-open <KB>/kb.html` (macOS: `open`; WSL: `wslview`), so one pasted prompt ends
with the UI open and a count reported.

### One-click launcher (`./tweetmark`)

For a zero-setup path with no agent in the loop, the repo ships a `./tweetmark`
bash launcher (stdlib + coreutils, no pip):

```
./tweetmark import          # full pipeline, then open kb.html
./tweetmark sync            # incremental refresh, then open kb.html
./tweetmark ask "QUERY"     # query.py (extra args pass through, e.g. --k 8 --json)
./tweetmark open            # regenerate the UI and open kb.html
./tweetmark status          # counts per area, tag histogram, growth
./tweetmark install         # symlink the skill into ~/.claude and ~/.agents
./tweetmark --kb PATH ...   # override the default KB (before the subcommand)
```

The launcher runs `--engine mock` (deterministic, zero setup). It converges on the
same finish as this agent `import` verb, which instead uses `--engine agent` for
richer, your-own-inference enrichment.

### sync  — incremental refresh
Same pipeline, but `collect.py` stops at the first already-seen tweet, so a
refresh fetches about one page and re-enriches only new/changed items.
```
python3 scripts/collect.py  --kb <KB>          # incremental (default)
python3 scripts/ingest.py   --kb <KB>
python3 scripts/enrich.py   --kb <KB> --engine mock
python3 scripts/build_kb.py --kb <KB>
python3 scripts/index.py    --kb <KB>
```

### ask  — natural-language query
Two-tier retrieval: expand the query, grep `INDEX.tsv`, optionally rank with
FTS5, open only the top few notes, answer with sources.
```
python3 scripts/query.py --kb <KB> "how does DPO compare to PPO" --k 5
python3 scripts/query.py --kb <KB> "rlhf reward model" --json
```
Follow `references/RETRIEVAL.md` verbatim when answering: always show the source
(id, title, tldr, author, url) and never cite an id that does not exist.

### browse  — open the UI
The UI is the regenerated `kb.html`; just open it.
```
python3 scripts/index.py --kb <KB>     # (re)generates kb.html
# then open <KB>/kb.html in a browser (double-click, or xdg-open)
```

### organize  — maintenance
Dedup, dead-link check, decay-to-archive suggestions, stats, resurface digest.
```
python3 scripts/doctor.py --kb <KB> --stats
python3 scripts/doctor.py --kb <KB> --dedup
python3 scripts/doctor.py --kb <KB> --dead-links
python3 scripts/doctor.py --kb <KB> --decay
python3 scripts/doctor.py --kb <KB> --digest
```
Network note: every command here is offline except `--dead-links`, which makes
outbound HTTP HEAD/GET requests to test saved links. It is the only maintenance
step that touches the network. Under a no-network sandbox (e.g. Codex
`workspace-write` with network off), pass `--offline` to skip the checks:
`python3 scripts/doctor.py --kb <KB> --dead-links --offline`.

### status  — what is in the KB
A quick health/summary view (counts per area, tag histogram, growth).
```
python3 scripts/doctor.py --kb <KB> --stats
```

## Collection (the only browser step)

`collect.py` reuses the user's existing Chrome session and runs the bookmark
fetch *inside* the authenticated `x.com` page, so X attaches its own anti-bot
headers. It never logs in, pastes cookies, or forges headers.

```
python3 scripts/collect.py --kb <KB> [--profile NAME] [--engine cdp|userscript|fixture] \
    [--fixture PATH] [--max-pages N] [--full] [--seed N]
```
- `--engine cdp` (default): copy the chosen profile, launch the user's Chrome
  headless over `--remote-debugging-pipe`, navigate to `x.com/i/bookmarks`,
  in-page fetch + paginate.
- `--engine userscript`: emit/ingest a manual one-click export instead.
- `--engine fixture --fixture PATH`: read fixture JSONL instead of a browser
  (deterministic; used by tests). Multiple `--fixture` flags page in order.
- `--full` forces a complete walk; default is incremental (stop at first
  already-seen `status_id`). Pacing is jittered ~2.5-3s/page with 429 backoff;
  `--seed N` makes pacing deterministic for tests.

Profile selection: on first run the collector picks the Chrome profile whose
cookie store holds an x.com/twitter.com cookie (presence check only, no
decryption) and remembers it in `.state/config.json`.

## Retrieval algorithm (summary)

1. Expand the question into 3-6 keywords/synonyms and likely tags (free, in-context).
2. Grep `INDEX.tsv` (`rg`/`grep`), never read it whole — only matching rows enter context.
3. Rank with SQLite FTS5 (`.state/kb.db`) only if the grep returns too many candidates.
4. Pick the top 3-5 ids from the one-line summaries.
5. Open only those notes by id/path.
6. Answer as a short synthesis plus result rows with full source attribution; if
   confidence is low, say so rather than fabricate.

Cost stays ~1.5-3.5k tokens per query, flat as the KB grows, because the index is
grepped (not loaded) and only a few notes are opened.

## Idempotency

Every stage keys on `status_id` plus a `content_hash` over the meaningful fields.
Re-running the pipeline on the same export yields zero new notes, zero duplicate
index rows, and zero re-enrichment. Edited tweets get a new hash, so exactly the
changed note is rebuilt and re-enriched. State writes are atomic (temp + rename).

## Media, links, and the richer note

Each note now carries its media and a robust open-original link:

- **Media** is extracted at ingest into `media` ( `{kind, url, alt, ocr}` ), then
  surfaced as note frontmatter (`media_count`, `thumb`, `media_alt`, a `media`
  list), a `**Media:**` body section, three `INDEX.tsv` columns (`has_media`,
  `thumb`, `media_alt`), and a thumbnail in the `kb.html` detail panel (lazy,
  no-referrer, self-healing on a rotted URL). Thumbnails hotlink the X CDN by
  default; `build_kb.py --cache-media` makes them local.
- **canonical_url** (`https://x.com/i/status/<id>`) is the handle-free permalink
  the UI uses for "open original", so renamed, suspended, deleted, or fabricated
  handles still resolve. A deleted tweet shows a muted "snapshot only" label
  instead of a dead link.
- Frontmatter also stores `lang` and an inline `engagement` map
  (`likes/retweets/replies/quotes/views`). Engagement and the derived media
  scalars are excluded from the content hash, so a like-count tick re-runs as a
  no-op and never re-enriches.

## References (load on demand)

- `references/TAXONOMY.md`   the Johnny.Decimal areas/categories and tag policy.
- `references/ENRICHMENT.md` per-type enrichment rules and the batch format.
- `references/RETRIEVAL.md`  the exact retrieval + answer-with-sources procedure.
- `references/EDGE_CASES.md` threads, quotes, media-only, links, deleted snapshots.

(These reference files are authored by later pipeline stages; consult them when a
step needs the detailed rules rather than the summary above.)
