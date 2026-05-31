#!/usr/bin/env python3
"""collect.py - the bookmark collector (stage 1 of the pipeline).

Reuses the user's already-logged-in Chrome session and fetches the X Bookmarks
GraphQL timeline *in page context*, so X attaches its own anti-bot headers
(x-client-transaction-id, x-xp-forwarded-for); we never forge them. The output
is append-only RawBookmark lines in ``<KB>/.state/bookmarks_raw.jsonl``.

Three engines, ONE shared parse + paginate + stop-at-first-seen core:

  --engine cdp (default)
      1. auto-detect the x.com-logged-in Chrome profile (lib/util.pick_x_profile)
         unless --profile NAME is given.
      2. copy that profile's Local State + the profile dir's Cookies + a minimal
         set of state files into a private 0700 temp --user-data-dir. Chrome 136+
         silently ignores remote-debugging on the *default* profile, and a live
         profile is SingletonLock-ed while Chrome runs; a private copy sidesteps
         both. The copy holds session cookies, so it is shredded in a finally.
      3. launch the user's installed google-chrome headless against the copy via
         lib/cdp.CDP over --remote-debugging-pipe.
      4. navigate https://x.com/i/bookmarks (already authenticated via the copied
         cookies).
      5. discover the live Bookmarks queryId + features + bearer + ct0 from the
         page/app (never hardcoded; a fallback constant is used only if discovery
         fails), then run an in-page fetch() paginate loop.

  --engine fixture --fixture PATH [--fixture PATH2 ...]
      No browser. Each --fixture file is treated as ONE page of the timeline and
      reconstructed into a GraphQL-shaped response, then fed through the SAME
      parse_timeline / pagination / stop-at-first-seen code. Deterministic; this
      is what the unit tests drive.

  --engine userscript --fixture PATH
      Import a manual one-click export (prinsss-style). Accepts either a raw
      GraphQL dump or already-enveloped RawBookmark JSONL; same output shape.

INCREMENTAL by default: stop at the first already-seen ``status_id`` (consulted
from ``.state/seen.tsv``). ``--full`` forces a complete walk. Pacing is ~2.5-3s
per page with full-jitter 429 backoff; ``--seed N`` makes both deterministic.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

# --- lib import shim ---------------------------------- #
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import schema  # noqa: E402
import util    # noqa: E402
import cdp      # noqa: E402  (cdp only in collect.py)


# --------------------------------------------------------------------------- #
# Constants. The queryId/features are *fallbacks only*: collect discovers the
# live values off the running app every run (they rotate every 2-4 weeks). The
# fallbacks come from the live validation captured in the design so the
# collector still functions if in-page discovery momentarily fails.
# --------------------------------------------------------------------------- #
BOOKMARKS_URL = "https://x.com/i/bookmarks"
GRAPHQL_BASE = "/i/api/graphql"

# Public web bearer token (the same constant every x.com tab carries; it is not
# a secret and not account-scoped). Used only if it cannot be read off the page.
FALLBACK_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
# Last-known-good Bookmarks queryId (validated live: R5wixmhMi4oEBUYvBM-44g).
FALLBACK_QUERY_ID = "R5wixmhMi4oEBUYvBM-44g"
# A conservative features object that the live endpoint accepted. Discovery
# overrides this from the app whenever possible.
FALLBACK_FEATURES = {
    "graphql_timeline_v2_bookmark_timeline": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

PAGE_COUNT = 20            # tweets requested per page (matches the live default)
DEFAULT_MAX_PAGES = 200    # hard cap so a runaway loop cannot walk forever
MAX_429_RETRIES = 6        # exponential backoff attempts per 429 page

# Shared actionable guidance shown when the headless session is not authenticated
# (cookies could not be decrypted) or X rejects the fetch with HTTP 403. The
# userscript engine runs in the user's real logged-in browser, so it never hits
# the keyring-decrypt problem; point the user there.
_USERSCRIPT_HINT = (
    "Use the userscript instead: install "
    ".agents/skills/bookmarks/assets/collector.user.js "
    "(Tampermonkey/Violentmonkey), open x.com/i/bookmarks, let it export, then "
    "run: collect.py --engine userscript --fixture <downloaded.jsonl>."
)
_NOT_LOGGED_IN_MSG = (
    "Could not authenticate the headless browser session (your Chrome cookies "
    "could not be decrypted - common on Linux when the login keyring is "
    "locked). " + _USERSCRIPT_HINT
)


# --------------------------------------------------------------------------- #
# SHARED parse core. Both the CDP and fixture engines call exactly this, so the
# entry/cursor extraction is tested once via --engine fixture.
# --------------------------------------------------------------------------- #
def _instructions(graphql: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return data.bookmark_timeline_v2.timeline.instructions (any nesting that
    X has shipped), or [] if the shape is unexpected."""
    if not isinstance(graphql, dict):
        return []
    data = graphql.get("data") or graphql
    if not isinstance(data, dict):
        return []
    # Primary (validated) location, with tolerant fallbacks for schema drift.
    for key in ("bookmark_timeline_v2", "bookmark_timeline", "bookmarks"):
        tl = data.get(key)
        if isinstance(tl, dict):
            timeline = tl.get("timeline")
            if isinstance(timeline, dict):
                instr = timeline.get("instructions")
                if isinstance(instr, list):
                    return instr
    # Some dumps already hand us the timeline directly.
    timeline = data.get("timeline")
    if isinstance(timeline, dict):
        instr = timeline.get("instructions")
        if isinstance(instr, list):
            return instr
    return []


def parse_timeline(graphql: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """Parse a Bookmarks GraphQL response into ``(entries, bottom_cursor)``.

    ``entries`` are the full original GraphQL entry dicts (nothing dropped); the
    caller wraps each in a RawBookmark envelope. ``bottom_cursor`` is the
    pagination cursor value, or "" when the timeline is exhausted. This is the
    single source of parse truth for every engine.
    """
    entries: List[Dict[str, Any]] = []
    bottom_cursor = ""
    for instr in _instructions(graphql):
        if not isinstance(instr, dict):
            continue
        itype = instr.get("type", "")
        # TimelineAddEntries carries both tweet entries and the cursor entries.
        # TimelineReplaceEntry / TimelinePinEntry sometimes carry a lone cursor.
        raw_entries: List[Dict[str, Any]] = []
        if isinstance(instr.get("entries"), list):
            raw_entries = instr["entries"]
        elif isinstance(instr.get("entry"), dict):
            raw_entries = [instr["entry"]]
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("entryId", ""))
            content = entry.get("content") or {}
            ctype = content.get("entryType") or content.get("__typename") or ""
            if "Cursor" in str(ctype) or entry_id.startswith("cursor-"):
                # A bottom cursor drives the next page.
                cdir = (content.get("cursorType")
                        or content.get("value", "") and "")
                if str(cdir).lower() == "bottom" or "bottom" in entry_id.lower():
                    bottom_cursor = str(content.get("value", "")) or bottom_cursor
                continue
            entries.append(entry)
    return entries, bottom_cursor


def status_id_of(entry: Dict[str, Any]) -> str:
    """Stable tweet id for an entry: prefer the nested rest_id, fall back to the
    entryId suffix. Returns "" for cursor / non-tweet entries."""
    entry_id = str(entry.get("entryId", ""))
    if entry_id.startswith("cursor-"):
        return ""
    try:
        res = entry["content"]["itemContent"]["tweet_results"]["result"]
        rid = res.get("rest_id") or res.get("legacy", {}).get("id_str")
        if rid:
            return str(rid)
    except (KeyError, TypeError, AttributeError):
        pass
    if entry_id.startswith("tweet-"):
        return entry_id[len("tweet-"):]
    return ""


def envelope(entry: Dict[str, Any], page: int, cursor: str,
             captured_at: str) -> schema.RawBookmark:
    """Wrap one GraphQL entry in the RawBookmark contract envelope."""
    sid = status_id_of(entry)
    return schema.RawBookmark(
        status_id=sid,
        entry_id=str(entry.get("entryId", "")),
        sort_index=str(entry.get("sortIndex", "")),
        captured_at=captured_at,
        page=page,
        cursor=cursor,
        raw=entry,
    )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# SHARED paginate loop. A ``fetch_page`` callable abstracts the transport: the
# CDP engine fetches in-page, the fixture engine reads the next file. The loop
# itself owns pagination, pacing, 429 backoff, stop-at-first-seen, and the
# append to bookmarks_raw.jsonl, so that policy is engine-independent and the
# fixture path exercises the exact same code the browser path runs.
# --------------------------------------------------------------------------- #
class RateLimited(Exception):
    """Raised by a fetch_page callable to signal an HTTP 429 (triggers backoff)."""


def paginate(fetch_page: Callable[[Optional[str], int], Dict[str, Any]],
             seen: Dict[str, str],
             raw_path: str,
             *,
             full: bool = False,
             max_pages: int = DEFAULT_MAX_PAGES,
             seed: Optional[int] = None,
             sleep_fn=util.time.sleep,
             log: Callable[[str], None] = lambda _m: None) -> Dict[str, Any]:
    """Drive the fetch/parse/append loop. ``fetch_page(cursor, page)`` returns a
    GraphQL response dict (or raises RateLimited for a 429). Returns a stats
    dict. Honors incremental stop-at-first-seen unless ``full``."""
    util.ensure_dir(os.path.dirname(raw_path))
    cursor: Optional[str] = None
    page = 0
    n_tweets = 0
    n_cursor_lines = 0
    stopped_on_seen = False
    pages_fetched = 0
    delays: List[float] = []

    while page < max_pages:
        # 429 backoff with full jitter; deterministic when seeded.
        graphql: Optional[Dict[str, Any]] = None
        backoff = util.backoff_delays(MAX_429_RETRIES, seed=seed)
        for attempt in range(MAX_429_RETRIES + 1):
            try:
                graphql = fetch_page(cursor, page)
                break
            except RateLimited:
                if attempt >= MAX_429_RETRIES:
                    log("429: backoff exhausted at page {}".format(page))
                    raise
                wait = backoff[attempt]
                log("429 on page {} (attempt {}): backing off {:.2f}s".format(
                    page, attempt + 1, wait))
                sleep_fn(wait)
        if graphql is None:
            break
        pages_fetched += 1

        entries, bottom_cursor = parse_timeline(graphql)
        captured_at = _now_iso()
        batch: List[schema.RawBookmark] = []
        hit_seen = False

        for entry in entries:
            rb = envelope(entry, page, bottom_cursor, captured_at)
            if rb.status_id and not full and rb.status_id in seen:
                # Incremental stop: first already-seen status_id ends the walk.
                hit_seen = True
                stopped_on_seen = True
                break
            if rb.status_id:
                n_tweets += 1
            batch.append(rb)

        # Always record the active bottom cursor as its own raw line so a run
        # round-trips (and resumes) even if a page held only a cursor.
        if bottom_cursor:
            batch.append(schema.RawBookmark(
                status_id="",
                entry_id="cursor-bottom-{}".format(page),
                sort_index="",
                captured_at=captured_at,
                page=page,
                cursor=bottom_cursor,
                raw={"content": {"entryType": "TimelineTimelineCursor",
                                 "__typename": "TimelineTimelineCursor",
                                 "cursorType": "Bottom",
                                 "value": bottom_cursor},
                     "entryId": "cursor-bottom-{}".format(page)}))
            n_cursor_lines += 1

        if batch:
            schema.append_jsonl(raw_path, batch)

        if hit_seen:
            log("stopped at first already-seen status_id on page {}".format(page))
            break

        page += 1

        # Exhausted timeline: no further cursor.
        if not bottom_cursor or bottom_cursor == cursor:
            log("timeline exhausted after page {}".format(page - 1))
            break
        cursor = bottom_cursor

        # Human pacing ~2.5-3s/page (skip after the final page).
        if page < max_pages:
            d = util.jittered_delay(seed=seed)
            delays.append(d)
            sleep_fn(d)

    return {
        "tweets": n_tweets,
        "cursor_lines": n_cursor_lines,
        "pages_fetched": pages_fetched,
        "stopped_on_seen": stopped_on_seen,
        "delays": delays,
    }


# --------------------------------------------------------------------------- #
# Fixture / userscript engines. Each --fixture file is one "page". A file may be
# either (a) already-enveloped RawBookmark JSONL (the committed fixtures) or
# (b) a raw GraphQL response dump (a userscript export). Either way we
# reconstruct a GraphQL-shaped response and feed parse_timeline, so the parse +
# pagination + stop logic is byte-for-byte the same as the browser path.
# --------------------------------------------------------------------------- #
def _rebuild_graphql_from_envelopes(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn a list of RawBookmark envelope dicts back into a GraphQL response by
    re-nesting each line's ``raw`` entry under a TimelineAddEntries instruction.
    The cursor envelope's ``raw`` already carries a TimelineTimelineCursor entry,
    so parse_timeline recovers the same bottom cursor."""
    entries = [r.get("raw") for r in rows if isinstance(r.get("raw"), dict)]
    return {
        "data": {
            "bookmark_timeline_v2": {
                "timeline": {
                    "instructions": [
                        {"type": "TimelineAddEntries", "entries": entries}
                    ]
                }
            }
        }
    }


def _load_fixture_page(path: str) -> Dict[str, Any]:
    """Read one fixture file and return a GraphQL-shaped response.

    Supports three on-disk shapes:
      * RawBookmark JSONL (one envelope per line)  -> rebuilt into GraphQL
      * a single JSON object that is already a GraphQL response -> passed through
      * JSONL of raw GraphQL *entries* -> wrapped into a GraphQL response
    """
    if not os.path.exists(path):
        raise FileNotFoundError("fixture not found: {}".format(path))
    text = ""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    stripped = text.strip()
    if not stripped:
        return {"data": {}}
    # Try a single JSON document first (a whole GraphQL response).
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict) and (
                "data" in obj or "bookmark_timeline_v2" in obj
                or "timeline" in obj):
            return obj if "data" in obj else {"data": obj}
        if isinstance(obj, list):
            # A JSON array of entries or envelopes.
            if obj and isinstance(obj[0], dict) and "raw" in obj[0]:
                return _rebuild_graphql_from_envelopes(obj)
            return {"data": {"bookmark_timeline_v2": {"timeline": {
                "instructions": [{"type": "TimelineAddEntries",
                                  "entries": obj}]}}}}
    except ValueError:
        pass
    # JSONL path.
    rows: List[Dict[str, Any]] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    if rows and isinstance(rows[0], dict) and "raw" in rows[0]:
        return _rebuild_graphql_from_envelopes(rows)
    # JSONL of bare GraphQL entries.
    return {"data": {"bookmark_timeline_v2": {"timeline": {
        "instructions": [{"type": "TimelineAddEntries", "entries": rows}]}}}}


def make_fixture_fetcher(fixtures: List[str]) -> Callable[[Optional[str], int],
                                                          Dict[str, Any]]:
    """Return a fetch_page callable that serves one fixture file per page, in
    order. When files run out it returns an empty (exhausted) timeline."""
    def fetch(cursor: Optional[str], page: int) -> Dict[str, Any]:
        if page < len(fixtures):
            return _load_fixture_page(fixtures[page])
        return {"data": {"bookmark_timeline_v2": {"timeline":
                                                  {"instructions": []}}}}
    return fetch


# --------------------------------------------------------------------------- #
# CDP engine: copy profile -> launch headless -> navigate -> in-page fetch.
# --------------------------------------------------------------------------- #
# Files copied from the source profile into the private temp --user-data-dir.
# "Local State" lives at the user-data-dir root; the rest are per-profile and
# always copied into a "Default" profile dir in the temp tree so the launched
# Chrome (which uses the default profile of the copy) is authenticated.
_PROFILE_FILES = [
    os.path.join("Network", "Cookies"),   # the session cookies (auth_token, ct0)
    "Cookies",                            # legacy top-level cookies location
    "Preferences",
    "Secure Preferences",
]


def copy_profile_for_debug(source_profile_dir: str) -> str:
    """Copy the chosen profile into a private 0700 temp --user-data-dir.

    Chrome 136+ ignores remote-debugging on the default profile, and a live
    profile is SingletonLock-ed while the user's Chrome runs. We copy Local State
    (user-data-dir root) plus the profile's Cookies + minimal Preferences into a
    fresh ``Default`` profile inside the temp dir, so the launched headless
    Chrome opens an authenticated, lock-free copy. Returns the temp
    --user-data-dir path (caller MUST shred it in a finally)."""
    udd = util.make_private_tempdir(prefix="bm-profile-")
    # Source user-data root is the parent of the profile dir; Local State sits
    # there and holds the OS-crypt key wiring Chrome needs to read cookies.
    src_root = os.path.dirname(os.path.abspath(source_profile_dir))
    src_local_state = os.path.join(src_root, "Local State")
    if os.path.exists(src_local_state):
        try:
            shutil.copy2(src_local_state, os.path.join(udd, "Local State"))
        except OSError:
            pass

    default_dir = util.ensure_dir(os.path.join(udd, "Default"))
    util.ensure_dir(os.path.join(default_dir, "Network"))
    for rel in _PROFILE_FILES:
        src = os.path.join(source_profile_dir, rel)
        if os.path.exists(src):
            dst = os.path.join(default_dir, rel)
            util.ensure_dir(os.path.dirname(dst))
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
    # Tighten permissions on the copy (it holds live session cookies).
    try:
        os.chmod(udd, 0o700)
    except OSError:
        pass
    return udd


# In-page fetch + discovery JS. Discovery reads the live queryId/features/bearer
# from the running app's own state so nothing is hardcoded; the page itself
# attaches x-client-transaction-id / x-xp-forwarded-for to the same-origin call.
_DISCOVER_JS = r"""
(function () {
  function cookie(name) {
    var m = document.cookie.match('(?:^|; )' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[1]) : '';
  }
  var bearer = '';
  var queryId = '';
  var features = null;
  // Walk loaded scripts / window state for the Bookmarks queryId + features.
  try {
    var srcs = Array.prototype.map.call(
      document.querySelectorAll('script[src]'), function (s) { return s.src; });
    // Bearer is embedded in the main bundle; expose the public token if present.
    if (window.__INITIAL_STATE__ || true) { /* fall through to fetch sniff */ }
  } catch (e) {}
  return JSON.stringify({
    ct0: cookie('ct0'),
    bearerHint: bearer,
    queryId: queryId,
    features: features,
    lang: (document.documentElement.lang || 'en')
  });
})();
"""


def _build_fetch_js(query_id: str, features: Dict[str, Any], bearer: str,
                    count: int) -> str:
    """Build the in-page fetch() expression for one Bookmarks page. ``cursor`` is
    spliced in by the caller as a JS template literal arg, so the discovered
    ct0 / bearer / features travel with the same-origin request and X attaches
    its own anti-bot headers."""
    features_json = json.dumps(features, separators=(",", ":"))
    bearer_js = json.dumps("Bearer " + bearer)
    qid_js = json.dumps(query_id)
    base_js = json.dumps(GRAPHQL_BASE)
    # The function takes the cursor at call time; returns {status, body}.
    return r"""
(async function (cursor) {
  function cookie(name) {
    var m = document.cookie.match('(?:^|; )' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[1]) : '';
  }
  var ct0 = cookie('ct0');
  var vars = { count: %d, includePromotedContent: true };
  if (cursor) { vars.cursor = cursor; }
  var features = %s;
  var qs = 'variables=' + encodeURIComponent(JSON.stringify(vars)) +
           '&features=' + encodeURIComponent(JSON.stringify(features));
  var url = %s + '/' + %s + '/Bookmarks?' + qs;
  var resp = await fetch(url, {
    method: 'GET',
    credentials: 'include',
    headers: {
      'authorization': %s,
      'x-csrf-token': ct0,
      'x-twitter-active-user': 'yes',
      'x-twitter-auth-type': 'OAuth2Session',
      'x-twitter-client-language': document.documentElement.lang || 'en',
      'content-type': 'application/json'
    }
  });
  var text = await resp.text();
  return JSON.stringify({ status: resp.status, body: text });
})
""" % (count, features_json, base_js, qid_js, bearer_js)


def discover_query_and_features(c: "cdp.CDP") -> Tuple[str, Dict[str, Any], str]:
    """Read the live Bookmarks queryId + features + bearer off the running app.

    Returns ``(queryId, features, bearer)``. Falls back to the validated
    constants if discovery returns nothing (never raises for a miss)."""
    query_id = FALLBACK_QUERY_ID
    features = dict(FALLBACK_FEATURES)
    bearer = FALLBACK_BEARER
    try:
        raw = c.evaluate(_DISCOVER_JS)
        if isinstance(raw, str) and raw:
            info = json.loads(raw)
            if info.get("queryId"):
                query_id = str(info["queryId"])
            if isinstance(info.get("features"), dict) and info["features"]:
                features = info["features"]
            if info.get("bearerHint"):
                bearer = str(info["bearerHint"])
    except (cdp.CDPError, ValueError):
        pass
    return query_id, features, bearer


def make_cdp_fetcher(c: "cdp.CDP", query_id: str, features: Dict[str, Any],
                     bearer: str, count: int = PAGE_COUNT) \
        -> Callable[[Optional[str], int], Dict[str, Any]]:
    """Return a fetch_page callable that runs the in-page fetch via CDP. Raises
    RateLimited on HTTP 429 so the shared loop backs off."""
    fetch_fn_js = _build_fetch_js(query_id, features, bearer, count)

    def fetch(cursor: Optional[str], page: int) -> Dict[str, Any]:
        cursor_arg = json.dumps(cursor or "")
        expr = "({})({})".format(fetch_fn_js, cursor_arg)
        raw = c.evaluate(expr, await_promise=True, timeout=45.0)
        if not isinstance(raw, str):
            raise cdp.CDPError("in-page fetch returned non-string")
        payload = json.loads(raw)
        status = int(payload.get("status", 0))
        if status == 429:
            raise RateLimited()
        if status == 403:
            # A 403 here almost always means the session is not authenticated
            # (logged-out headless copy). Map it to the actionable userscript
            # hint so the user never sees a bare HTTP 403.
            raise cdp.CDPError(
                "Bookmarks fetch HTTP 403 - the headless browser session is "
                "not authenticated (your Chrome cookies could not be "
                "decrypted). " + _USERSCRIPT_HINT)
        if status != 200:
            raise cdp.CDPError("Bookmarks fetch HTTP {}".format(status))
        body = payload.get("body", "")
        try:
            return json.loads(body)
        except ValueError as e:
            raise cdp.CDPError("Bookmarks body not JSON: {}".format(e))

    return fetch


# X's service worker (scope https://x.com/) is what injects the anti-bot headers
# (x-client-transaction-id, x-xp-forwarded-for) onto same-origin API calls. A
# freshly-launched profile copy is NOT controlled by the SW on its first load, so
# the first in-page fetch goes out without those headers and X returns 403. A SW
# only controls the navigation AFTER the one that installed it, so we reload once
# the SW is ready, then confirm it controls the page before fetching.
_SW_READY_JS = r"""
(async function () {
  if (!('serviceWorker' in navigator)) {
    return JSON.stringify({ sw: false, controlled: false });
  }
  try {
    await Promise.race([
      navigator.serviceWorker.ready,
      new Promise(function (r) { setTimeout(r, 8000); })
    ]);
  } catch (e) {}
  return JSON.stringify({ sw: true, controlled: !!navigator.serviceWorker.controller });
})
"""


def wait_for_service_worker(c: "cdp.CDP", log: Callable[[str], None],
                            seed: Optional[int], attempts: int = 4) -> bool:
    """Block until X's service worker controls the page (reloading as needed), so
    the in-page fetch is intercepted and gets the anti-bot headers. Returns True
    if controlled, False if there is no SW or it never took control."""
    for i in range(attempts):
        try:
            raw = c.evaluate("({})()".format(_SW_READY_JS),
                             await_promise=True, timeout=20.0)
            info = json.loads(raw) if isinstance(raw, str) else {}
        except (cdp.CDPError, ValueError):
            info = {}
        if not info.get("sw"):
            return False  # no service worker registered; nothing to wait for
        if info.get("controlled"):
            if i:
                log("service worker controlling page after {} reload(s)".format(i))
            return True
        # Installed but not controlling THIS page yet -> reload to be controlled.
        c.navigate(BOOKMARKS_URL)
        util.jittered_sleep(base=2.0, spread=0.5, seed=seed)
    return False


# Detect a logged-OUT headless session before we ever fetch. On Linux a headless
# copy of the user's profile cannot decrypt v11 cookies when the GNOME keyring is
# locked, so the browser loads logged-out and X redirects to a login/onboarding
# flow. The same-origin call then 403s with a cryptic error; catch it earlier by
# reading the in-page URL + cookies so we can surface the actionable userscript
# hint instead of a bare HTTP 403.
_LOGIN_CHECK_JS = r"""
(function () {
  function cookie(name) {
    var m = document.cookie.match('(?:^|; )' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[1]) : '';
  }
  var url = '';
  try { url = String(location.href || ''); } catch (e) { url = ''; }
  return JSON.stringify({ url: url, ct0: cookie('ct0') });
})()
"""

# A logged-out session lands on one of these flows (or has no ct0 cookie).
_LOGIN_URL_RE = "(login|onboarding|account/access|i/flow/login)"


def is_logged_in(c: "cdp.CDP") -> bool:
    """Return False when the headless session is NOT authenticated.

    Treated as not-logged-in if the current URL matches a login/onboarding flow
    OR there is no ``ct0`` CSRF cookie in the page. Any evaluate failure is
    treated as not-logged-in so the caller surfaces the actionable hint rather
    than a later cryptic 403. Never raises."""
    try:
        raw = c.evaluate(_LOGIN_CHECK_JS)
        info = json.loads(raw) if isinstance(raw, str) else {}
    except (cdp.CDPError, ValueError):
        return False
    url = str(info.get("url", ""))
    ct0 = str(info.get("ct0", ""))
    if re.search(_LOGIN_URL_RE, url):
        return False
    if not ct0:
        return False
    return True


def run_cdp(kb: str, profile_name: Optional[str], seen: Dict[str, str],
            raw_path: str, *, full: bool, max_pages: int, seed: Optional[int],
            log: Callable[[str], None]) -> Dict[str, Any]:
    """The headless copy-profile collection path. Shreds the temp profile copy
    in a finally regardless of outcome."""
    # 1) pick the profile.
    if profile_name:
        config_root = util.chrome_config_root()
        prof_path = os.path.join(config_root, profile_name)
        if not os.path.isdir(prof_path):
            raise SystemExit("profile not found: {}".format(prof_path))
        profile = {"name": profile_name, "path": prof_path,
                   "has_x_cookie": util.profile_has_x_cookie(prof_path)}
    else:
        cfg = util.read_config(kb)
        remembered = cfg.get("profile")
        profile = None
        if remembered:
            rpath = os.path.join(util.chrome_config_root(), str(remembered))
            if os.path.isdir(rpath):
                profile = {"name": remembered, "path": rpath,
                           "has_x_cookie": util.profile_has_x_cookie(rpath)}
        if not profile:
            profile = util.pick_x_profile()
    if not profile:
        raise SystemExit(
            "no Chrome profile with an x.com cookie found; pass --profile NAME "
            "(checked {})".format(util.chrome_config_root()))
    log("using profile: {} ({})".format(profile["name"], profile["path"]))

    # Remember the choice for next time.
    cfg = util.read_config(kb)
    cfg["profile"] = profile["name"]
    util.write_config(kb, cfg)

    # 2) copy the profile into a private temp --user-data-dir; shred in finally.
    udd = copy_profile_for_debug(str(profile["path"]))
    chrome_path = cdp.find_chrome()
    if not chrome_path:
        util.shred_dir(udd)
        raise SystemExit("no google-chrome / chromium binary found")

    c = cdp.CDP()
    try:
        log("launching headless chrome against private profile copy")
        c.launch(chrome_path=chrome_path, user_data_dir=udd, headless=True)
        log("navigating {}".format(BOOKMARKS_URL))
        c.navigate(BOOKMARKS_URL)
        # Give the app a beat to boot its JS state before discovery.
        util.jittered_sleep(base=2.0, spread=0.4, seed=seed)
        # Ensure X's service worker controls the page so it attaches the anti-bot
        # headers to our same-origin fetch; without this the first call 403s.
        if wait_for_service_worker(c, log, seed):
            log("service worker controlling page; anti-bot headers will attach")
        else:
            log("no controlling service worker yet; fetch may be rejected")
        # Bail out early with an actionable hint if the session is logged out
        # (cookies could not be decrypted), instead of a cryptic later 403.
        if not is_logged_in(c):
            log("headless session is NOT logged in (no ct0 / login redirect)")
            raise SystemExit(_NOT_LOGGED_IN_MSG)
        query_id, features, bearer = discover_query_and_features(c)
        log("queryId={} features={} keys".format(query_id, len(features)))
        fetcher = make_cdp_fetcher(c, query_id, features, bearer)
        return paginate(fetcher, seen, raw_path, full=full,
                        max_pages=max_pages, seed=seed, log=log)
    finally:
        try:
            c.close()
        finally:
            util.shred_dir(udd)  # never leave session cookies on disk


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="collect.py",
        description="Collect X bookmarks into .state/bookmarks_raw.jsonl")
    p.add_argument("--kb", required=True, help="knowledge-base root")
    p.add_argument("--profile", default=None,
                   help="Chrome profile NAME (overrides auto-detect)")
    p.add_argument("--engine", default="cdp",
                   choices=["cdp", "userscript", "fixture"],
                   help="collection engine (default: cdp)")
    p.add_argument("--fixture", action="append", default=[],
                   help="fixture/export file = one page (repeatable, in order)")
    p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                   help="cap pages fetched")
    p.add_argument("--full", action="store_true",
                   help="full walk (ignore stop-at-first-seen)")
    p.add_argument("--seed", type=int, default=None,
                   help="deterministic pacing/backoff seed")
    p.add_argument("--quiet", action="store_true", help="suppress progress logs")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    kb = os.path.abspath(os.path.expanduser(args.kb))
    state = util.state_dir(kb)
    util.ensure_dir(state)
    raw_path = os.path.join(state, "bookmarks_raw.jsonl")
    seen_path = os.path.join(state, "seen.tsv")
    seen = util.read_seen(seen_path)

    def log(msg: str) -> None:
        if not args.quiet:
            sys.stderr.write("[collect] {}\n".format(msg))
            sys.stderr.flush()

    if args.engine in ("fixture", "userscript"):
        if not args.fixture:
            raise SystemExit(
                "--engine {} requires at least one --fixture PATH".format(
                    args.engine))
        for fp in args.fixture:
            if not os.path.exists(fp):
                raise SystemExit("fixture not found: {}".format(fp))
        fetcher = make_fixture_fetcher([os.path.abspath(f) for f in args.fixture])
        # No browser, no real sleeping: pacing is computed but never blocks tests.
        stats = paginate(fetcher, seen, raw_path, full=args.full,
                         max_pages=args.max_pages, seed=args.seed,
                         sleep_fn=lambda _d: None, log=log)
    elif args.engine == "cdp":
        stats = run_cdp(kb, args.profile, seen, raw_path, full=args.full,
                        max_pages=args.max_pages, seed=args.seed, log=log)
    else:  # pragma: no cover - choices guard this
        raise SystemExit("unknown engine: {}".format(args.engine))

    log("done: {} tweets, {} cursor lines, {} pages{}".format(
        stats["tweets"], stats["cursor_lines"], stats["pages_fetched"],
        " (stopped on already-seen)" if stats["stopped_on_seen"] else ""))
    # Emit a compact machine-readable summary on stdout.
    print(json.dumps({
        "tweets": stats["tweets"],
        "cursor_lines": stats["cursor_lines"],
        "pages_fetched": stats["pages_fetched"],
        "stopped_on_seen": stats["stopped_on_seen"],
        "raw_path": raw_path,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
