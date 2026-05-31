# TweetMark

**TweetMark** is an **Agent Skill** for Claude Code and Codex. Point your agent at this
folder and paste one instruction; it imports every X (Twitter) bookmark and opens a
local, searchable screen of all your saved tweets. Everything stays on your own disk.

**The promise:** it reuses the Chrome session you are already signed into, so there
is no login step, no pasted cookies, and no API keys. Everything lands on your own
disk as plain markdown plus a fast index. You ask questions like "what did I save
about RLHF reward models?" and the agent answers with the original tweets cited.

- **No login.** The one browser step drives the profile you already use. It never
  signs in for you and never asks for a password.
- **Private and local.** The knowledge base is plain files in a folder you own.
  Nothing is uploaded anywhere.
- **Ask in plain English.** Six plain words (`import`, `sync`, `ask`, `browse`,
  `organize`, `status`) cover everything. You can also just describe what you want.
- **Cheap to run.** The expensive work (categorize, tag, summarize) happens once at
  import and is cached forever. Asking a question stays around 1.5k to 3.5k tokens
  whether you have 100 bookmarks or 10,000.

---

## One paste, and all your tweets are on screen

Open this folder in your agent, then paste the instruction below. The agent imports
every bookmark, builds the knowledge base, and opens `kb.html`, a fast, searchable
screen of all your saved tweets.

```bash
cd tweetmark && claude      # or: codex
```

Then paste this and let the agent do the rest:

```text
Use the bookmarks skill to import ALL my X/Twitter bookmarks. Reuse my already
logged-in Chrome session: do not ask me to log in, and do not ask for cookies or API
keys. Run the full pipeline (collect --full, ingest, enrich, build_kb, index) into the
default knowledge-base folder, then open kb.html so I can see every tweet on one
screen. If the headless browser cannot authenticate, set up the bundled userscript and
import that export instead. When you finish, tell me how many bookmarks you imported.
```

Everything below is detail, alternatives, and the engineering behind it.

---

## What you need

- Google Chrome installed and **already logged into x.com** in a normal profile.
- Python 3 (standard library only; this project installs nothing).
- Either **Claude Code** or **OpenAI Codex CLI**. The same skill works in both.

---

## The one-click launcher

The fastest path needs no agent at all. The `./tweetmark` launcher (bash, standard
library plus coreutils, nothing to install) runs the whole pipeline and opens the UI:

```bash
./tweetmark import          # full export, then open kb.html
./tweetmark sync            # incremental refresh, then open kb.html
./tweetmark ask "dpo vs ppo" --k 8
./tweetmark open            # regenerate the UI and open kb.html
./tweetmark status          # counts per area, tag histogram, growth
./tweetmark install         # symlink the skill + launcher onto your PATH
./tweetmark --kb "/some/path" import   # override the default KB folder
```

Copy-paste to get your bookmarks in and the UI open:

```bash
cd tweetmark
./tweetmark import
```

That collects every bookmark, builds the knowledge base, prints a count, and opens
`kb.html`. The launcher enriches with `--engine mock` (deterministic, zero setup);
for richer, your-own-inference summaries use the agent `import` verb below
(`--engine agent`). Run `./tweetmark install` once to drop the skill into
`~/.claude` and `~/.agents` and link the launcher into `~/.local/bin`.

---

## Copy-paste quickstart (agent-driven)

You do not configure anything. Launch the agent in this folder, then paste one prompt.
The skill is discovered automatically through matching folders:

```
.agents/skills/bookmarks/        the real skill   (Codex reads here)
.claude/skills/bookmarks  ->  ../../.agents/skills/bookmarks   (Claude reads here)
```

### Step 1 — launch the agent in this folder

```bash
cd tweetmark && claude      # Claude Code
# or
cd tweetmark && codex       # OpenAI Codex CLI
```

Optional, run once, to make the skill available in every folder (not just this one):

```bash
mkdir -p ~/.claude/skills ~/.agents/skills
ln -sfn "$PWD/.agents/skills/bookmarks" ~/.claude/skills/bookmarks   # Claude Code
ln -sfn "$PWD/.agents/skills/bookmarks" ~/.agents/skills/bookmarks   # Codex
```

### Step 2 — paste this to import your bookmarks

Into **Claude Code**, paste:

```text
Use the bookmarks skill to import my X/Twitter bookmarks. Reuse my already-logged-in
Chrome session: do not ask me to log in, and do not ask for cookies or API keys. Run the
full pipeline (collect with --full, then ingest, enrich --engine agent, build_kb, index)
into the default knowledge-base folder. If I have more than one Chrome profile, pick the
one logged into x.com. When you finish, tell me how many bookmarks you imported and where
the knowledge base is, then open kb.html.
```

Into **Codex**, paste:

```text
$bookmarks import my X/Twitter bookmarks using my already-logged-in Chrome session (no
login, no cookies, no API keys). Run collect --full, then ingest, enrich --engine agent,
build_kb, and index into the default knowledge-base folder; if there are several Chrome
profiles, pick the one logged into x.com. Then tell me the count and open kb.html.
```

### Step 3 — paste this to search (any time after import)

```text
Ask my bookmarks: what did I save about RLHF reward models? Answer in plain English and
cite the original tweets.
```

### Step 4 — paste this to refresh later (fetches only what is new)

```text
Use the bookmarks skill to sync my X/Twitter bookmarks (incremental), then tell me what
is new.
```

Under the hood the agent runs the same stdlib Python scripts in both tools. The import
step launches Chrome once (an explicit, user-approved action under Codex's sandbox);
every other step is fully offline.

---

## The six verbs

Each plain-English verb maps to exactly one script. Run them with `python3` from
`.agents/skills/bookmarks/scripts/`. `<KB>` is your knowledge-base folder (defaults
to `~/Documents/Twitter Bookmarks`; override with `$BOOKMARKS_KB` or `--kb`).

| Verb | What it does |
|---|---|
| **import** | First full export: collect every bookmark, then build the whole KB. |
| **sync** | Incremental refresh: fetch only what is new, re-enrich only what changed. |
| **ask** | Natural-language search that answers with the original tweets cited. |
| **browse** | Open the local `kb.html` UI for keyboard-first browsing. |
| **organize** | Dedup, dead-link checks, archive suggestions, resurface digest. |
| **status** | Counts per area, tag histogram, growth over time. |

### import: first full export

```
python3 scripts/collect.py  --kb "<KB>" --full
python3 scripts/ingest.py   --kb "<KB>"
python3 scripts/enrich.py   --kb "<KB>" --engine agent
python3 scripts/build_kb.py --kb "<KB>"
python3 scripts/index.py    --kb "<KB>"
```

`enrich.py` has three engines. `--engine agent` (default for real use) writes batch
files under `.state/batches/` that the agent reads and answers with its own
inference, no API key. `--engine mock` is a deterministic, zero-token first pass
(used by tests). `--engine api` is the cheap-model option for unattended bulk
enrichment.

`build_kb.py --cache-media` is optional. It downloads each item's first image still
into `<KB>/.media/` so thumbnails work fully offline. Left off (the default), the UI
hotlinks thumbnails straight from the X CDN. It is the only `build_kb.py` step that
touches the network, and it writes solely under `<KB>/.media/`.

### sync: incremental refresh

```
python3 scripts/collect.py  --kb "<KB>"          # incremental (stops at first seen)
python3 scripts/ingest.py   --kb "<KB>"
python3 scripts/enrich.py   --kb "<KB>" --engine agent
python3 scripts/build_kb.py --kb "<KB>"
python3 scripts/index.py    --kb "<KB>"
```

`collect.py` stops at the first already-seen tweet, so a refresh fetches about one
page and re-enriches only new or edited items.

### ask: natural-language query

```
python3 scripts/query.py --kb "<KB>" "how does DPO compare to PPO" --k 5
python3 scripts/query.py --kb "<KB>" "rlhf reward model" --json
```

The agent expands your question into keywords, greps the index, opens only the top
few notes, and answers with each source shown (id, title, TL;DR, author, link). If
confidence is low it says so rather than invent an answer.

### browse: open the UI

```
python3 scripts/index.py --kb "<KB>"     # (re)generates kb.html
# then open <KB>/kb.html in a browser (double-click, or: xdg-open "<KB>/kb.html")
```

### organize: maintenance

```
python3 scripts/doctor.py --kb "<KB>" --dedup
python3 scripts/doctor.py --kb "<KB>" --dead-links     # add --offline to skip network
python3 scripts/doctor.py --kb "<KB>" --decay
python3 scripts/doctor.py --kb "<KB>" --digest
```

Every maintenance command is offline except `--dead-links`, which makes outbound
HTTP requests to test saved links. Pass `--offline` under a no-network sandbox.

### status: what is in the KB

```
python3 scripts/doctor.py --kb "<KB>" --stats
```

---

## What the knowledge base looks like on disk

The folder scheme is **Johnny.Decimal**: shallow, numbered, predictable. At most 10
areas, at most 10 categories per area, item ids like `11.01`. It is easy to browse
in any file manager and easy to grep.

```
<KB>/
  INDEX.tsv                one tab-separated row per bookmark (the grep target)
  00_START-HERE.md         generated plain-English guide + category counts
  tags.txt                 controlled tag vocabulary
  kb.html                  self-contained UI (embeds the index as JSON)
  10-19_ai-tech/           an area folder
    _README.md             generated table of what is in this area
    11_llm-training/       a category folder
      11.01_dpo-vs-ppo.md  one note per bookmark: YAML frontmatter + body
  20-29_tools/  30-39_business/  ...  90-99_archive/
  .media/                  local image cache (only after build_kb --cache-media)
  .state/                  machine state (not the source of truth)
    bookmarks_raw.jsonl    append-only raw capture from collect.py
    seen.tsv               status_id -> content_hash ledger (idempotency)
    new_items.jsonl        new/changed normalized items from ingest.py
    enriched/NNN.out.jsonl cached enrichment output
    batches/NNN.txt        agent-engine batch files (enrich --engine agent)
    kb.db                  rebuildable SQLite FTS5 index (optional)
    config.json            remembered profile, etc.
```

The markdown notes and `INDEX.tsv` are the **single source of truth**. The agent,
the `kb.html` UI, and any text editor all read the same files, so the three views
never drift apart.

Each note carries rich frontmatter, in order: id, status_id, title, url,
canonical_url, author, saved date, type, lang, category, tags, media_count, thumb,
media_alt, an inline engagement map (likes, retweets, replies, quotes, views), a
media list, and the content hash. The body follows: a one-line TL;DR, a "why saved"
line, key points for threads, a **Media** section for attached images and video,
and the original links.

Two of those fields matter for robustness. `canonical_url` is the handle-free
`https://x.com/i/status/<id>` permalink that X redirects to the live account, so a
renamed, suspended, deleted, or fabricated handle still resolves "open original".
And the media fields (`media_count`, `thumb`, `media_alt`, the `media` list) carry
images all the way through: into `INDEX.tsv` (three columns: `has_media`, `thumb`,
`media_alt`) and into the `kb.html` detail panel as a thumbnail. The engagement
counts and the derived media scalars are deliberately excluded from the content
hash, so a like-count tick re-runs as a no-op and never re-summarizes the note.

### The kb.html UI

`index.py` writes a single self-contained `kb.html` with the index embedded as
inline JSON. Double-click it and you get instant client-side search with no server
and no build step:

- A command-palette search field owns the screen; Cmd/Ctrl-K focuses it.
- Results are dense one-line rows grouped by area; arrow keys move, Enter opens.
- Inline filter tokens (`#tag`, `area:ai`, `from:@handle`, `is:thread`) replace any
  sidebar.
- Monochrome with a single accent, dark by default with a light toggle.
- Expanding a row shows the note detail with a media thumbnail (lazy, no-referrer,
  and self-healing if a URL has rotted). "Open original" uses the handle-free
  canonical link, and a deleted tweet shows a muted "snapshot only" label instead
  of a dead link.

---

## How it stays fast, cheap, private, and invisible to X

These properties are deliberate:

**Fast and token-efficient.** All expensive LLM work (categorize, tag, summarize)
runs once at import and is cached to files forever. Asking a question is a two-tier
grep: expand the query, grep `INDEX.tsv` (never load it whole), open only the few
notes you actually need. Per-query cost stays flat at roughly 1.5k to 3.5k tokens
whether the KB holds 100 bookmarks or 10,000. The UI search is client-side over the
embedded index and stays under 50ms at 10k entries.

**Private and local.** The knowledge base is plain files in a folder you own.
Scripts write only inside the chosen `--kb`. The whole pipeline is offline except
three explicit, opt-in steps: the single browser collection step, the optional
`doctor.py --dead-links` link check, and the optional `build_kb.py --cache-media`
thumbnail download. By default the UI hotlinks media straight from the X CDN with no
referrer; pass `--cache-media` to keep copies locally and browse fully offline.

**No login, reuses your session.** The collector never signs in and never asks for
cookies or API keys. It reuses the Chrome profile you are already logged into and
runs the bookmark fetch **inside the authenticated x.com page**, so X's own request
machinery attaches every anti-bot header. Profile detection is presence-only; the
skill never decrypts your cookies.

**Invisible to X.** Requests come from your real, logged-in browser, same-origin,
with the genuine TLS stack and the browser's own headers. There is nothing for X to
distinguish from normal use. Incremental sync stops at the first already-seen tweet
(a refresh touches about one page), pacing is jittered around 2.5 to 3 seconds per
page, and rate-limit responses trigger exponential backoff.

**Idempotent re-runs.** Every stage keys on the tweet `status_id` plus a
`content_hash` over the meaningful fields. Re-running the pipeline on the same export
yields zero new notes, zero duplicate index rows, and zero re-enrichment. An edited
tweet gets a new hash, so exactly that one note is rebuilt. State writes are atomic
(temp file + rename), so an interrupted run never corrupts the KB.

---

## Troubleshooting

**Chrome 136+ ignores remote debugging on the default profile.** Newer Chrome
silently refuses `--remote-debugging` on the live default profile. The collector
handles this for you: it copies the chosen profile's `Local State` and `Cookies` into
a private temp directory, launches your installed Chrome headless against the copy
(already logged in through the copied cookies), and shreds the temp profile after the
run. Your live browser keeps working uninterrupted because the collector never
touches it directly.

**Picking the right Chrome profile.** If you have several profiles (`Default`,
`Profile 3`, and so on), the collector picks the one whose cookie store holds an
x.com cookie and remembers the choice in `.state/config.json`. To force a specific
one, pass `--profile`:

```
python3 scripts/collect.py --kb "<KB>" --profile "Profile 3" --full
```

**X rotates its query id every few weeks.** X changes the internal `Bookmarks`
queryId and feature flags roughly every two to four weeks. The collector never
hardcodes them; it discovers the current values from the live app on every run. If X
changes its schema in a way the collector cannot read, the collector reports it
rather than failing silently.

**The userscript fallback.** If you would rather not let an agent drive your browser
at all, use the manual one-click exporter at
`.agents/skills/bookmarks/assets/collector.user.js`. Install it with Tampermonkey or
Violentmonkey, open `x.com/i/bookmarks`, scroll to the bottom, and it downloads a
`bookmarks_raw.jsonl` in the exact same shape as the automated path. Re-import it
with:

```
python3 scripts/collect.py --kb "<KB>" --engine userscript --fixture bookmarks_raw.jsonl
```

It is a passive network interceptor: it only reads the responses X already sends to
the page as you scroll, and forges nothing.

---

## How it is packaged for both agents

One real skill, mounted twice. `SKILL.md` carries only the `name` and `description`
frontmatter plus the six verbs, so only a tiny resident footprint stays in context
until the skill is triggered. Detailed rules live in `references/` (taxonomy,
enrichment, retrieval, edge cases) and load on demand. All logic is stdlib Python
under `scripts/`, identical under Claude Code and under Codex's `workspace-write`
sandbox. `agents/openai.yaml` adds Codex display polish (a friendly name and default
prompt).

The exact CLIs, file schemas, and on-disk layout are documented in `SKILL.md` and the
`references/` files, and are pinned by the test suite.
