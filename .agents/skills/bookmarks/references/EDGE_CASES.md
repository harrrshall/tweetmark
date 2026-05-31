# EDGE_CASES.md: the tricky cases and how the pipeline handles them

Quick reference for the non-obvious situations. Most are already handled by the
scripts; this doc tells you what the behavior is and what to do when it isn't
automatic.

## Deleted tweets (TweetTombstone)

- A bookmark whose GraphQL `__typename` is `TweetTombstone` is captured as a
  **snapshot**: `ingest.py` sets `deleted=true` and `type=deleted`, keeping
  whatever text was captured at save time. Content snapshotted at capture survives
  the deletion.
- Deleted snapshots are **never merged into a thread** (they stay standalone so
  the `deleted` type and snapshot are preserved verbatim).
- Enrichment: `why_saved` ≈ "snapshot of a since-deleted tweet kept for the
  record". If there's no usable text, it routes to `00_inbox`.
- `doctor.py --dead-links` flags broken links/tweets but the snapshot text stays
  readable, so a dead bookmark is still useful.

## Media-only tweets and OCR

- A tweet that is just an image/video with little or no text is flagged
  `needs_ocr=true` by `ingest.py` (type `media`). Each `media` item carries
  `kind` (`photo|video|animated_gif`), `url`, `alt` (X alt-text if present), and
  `ocr` (filled later).
- Enrichment summarizes from `alt`/`ocr` **first**. If both are empty there is no
  text to summarize; describe only what's available; do **not** hallucinate
  image contents. `why_saved` ≈ "visual reference to keep".
- OCR is optional and not run by the stdlib path; `needs_ocr` simply marks items
  whose searchability would improve with alt/OCR text.

## Quote tweets

- `type=quote`. The quoted tweet projection lives in the normalized record's
  `quoted` dict (`author_handle`, `text`). Both the GraphQL location
  (`result.quoted_status_result.result`) and the normalized field exist.
- Enrichment summarizes **both** the user's comment and the quoted claim,
  labeled. Never invent what the quoted tweet said; use the captured `quoted.text`.

## Very long threads

- A same-author reply chain sharing a `conversation_id` is collapsed by
  `ingest.py` into **one** `type=thread` item with ordered `thread_texts`
  (oldest first). Collapsing triggers only when 2+ same-author items group; a
  lone tweet that merely has its own `conversation_id` is **not** a thread.
- Enrichment: `tldr` is the thread's thesis; `key_points` is up to 4 ordered
  bullets from later tweets (threads are the only type that gets key_points).
  A 50-tweet thread still becomes one note; summarize the arc, don't dump it.
- The note stores `thread_texts` content so retrieval can grep the full chain
  even though only the root is the "item".

## Rate limiting / HTTP 429

- The collector paces ~2.5–3s per page (jittered) and, on a 429, backs off with
  full-jitter exponential delays (`util.backoff_delays`), up to 6 retries per
  page, then raises. `--seed N` makes pacing and backoff deterministic for tests.
- Incremental `sync` stops at the first already-seen `status_id`, so a refresh
  fetches ~1 page and almost never trips a limit. If you hit sustained 429s, wait
  and re-run later; the run is resumable from the last cursor and idempotent.

## queryId / features rotation

- X's Bookmarks `queryId` and `features` object rotate every ~2–4 weeks. The
  collector **discovers them live** off the running app every run; it never
  relies on the hardcoded values except as a last-resort fallback
  (`FALLBACK_QUERY_ID`, `FALLBACK_FEATURES` in `scripts/collect.py`).
- If discovery returns nothing and the fallback also 4xx's, the schema has
  drifted: re-run later (the app may update), or refresh the fallback constants
  from the live request. The collector never forges the anti-bot headers
  (`x-client-transaction-id`, `x-xp-forwarded-for`); X attaches them itself
  because the fetch runs in-page, same-origin.

## Chrome 136+ copy-profile path

- Chrome 136+ **silently ignores** `--remote-debugging` on the *default* profile.
  The CDP collector therefore copies the chosen profile's `Local State` +
  `Cookies` (+ minimal Preferences) into a private `0700` temp `--user-data-dir`
  and launches headless against the copy. On Linux the copied cookies stay valid
  (no App-Bound Encryption).
- The temp copy holds live session cookies, so it is created with private
  permissions and **shredded in a `finally`** regardless of outcome
  (`util.make_private_tempdir` / `util.shred_dir`). It is never committed.

## Profile lock (SingletonLock)

- A live Chrome profile is `SingletonLock`-ed while the user's browser is open,
  so the collector never drives the live profile directly; it operates on the
  **copy** (see above). The user keeps browsing uninterrupted; the headless run
  is isolated.
- If no profile with an x.com/twitter.com cookie is found, the collector exits
  asking for `--profile NAME`. Profile detection is **presence-only** (it checks
  `host_key LIKE '%x.com'` in the read-only Cookies sqlite); it **never decrypts
  cookie values**. The chosen profile is remembered in `.state/config.json`.

## Fallback collector (no agent-driven browser)

- If the user prefers a manual one-click export, the Tampermonkey userscript at
  `assets/collector.user.js` intercepts the Bookmarks GraphQL responses while
  they scroll and downloads `bookmarks_raw.jsonl` in the **same RawBookmark
  shape** as the CDP path. Import it with
  `collect.py --engine userscript --fixture <that file>`; it flows through the
  exact same parse/normalize code.
