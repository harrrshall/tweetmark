// ==UserScript==
// @name         X Bookmarks Collector (bookmarks skill fallback)
// @namespace    https://github.com/bookmarks-skill
// @version      1.0.0
// @description  Fallback one-click X/Twitter bookmarks exporter. Intercepts the Bookmarks GraphQL responses while you scroll and downloads bookmarks_raw.jsonl in the SAME RawBookmark shape as the CDP collector path, re-importable via: collect.py --engine userscript --fixture bookmarks_raw.jsonl
// @author       bookmarks skill
// @match        https://x.com/*
// @match        https://twitter.com/*
// @run-at       document-start
// @grant        none
// @noframes
// ==/UserScript==

// WHY THIS EXISTS
// ---------------
// The shipped collector drives the user's Chrome session over CDP and fetches
// the Bookmarks GraphQL timeline in-page. This userscript is the manual
// fallback for users who would rather not let an agent drive their browser at
// all: it is a prinsss-style passive network interceptor. It does NOT forge any
// request or header -- it only reads the responses X already sends to the page
// as you scroll the Bookmarks tab, then hands you a download.
//
// OUTPUT CONTRACT (must match scripts/lib/schema.py RawBookmark exactly):
//   one compact JSON object per line, UTF-8, keys SORTED, ensure_ascii=False.
//   Fields, in sorted-key emit order:
//     captured_at  ISO8601 UTC capture time
//     cursor       bottom cursor active when this entry was captured
//     entry_id     GraphQL entryId ("tweet-<id>" or "cursor-bottom-<page>")
//     page         0-based page index (one GraphQL response == one page)
//     raw          the FULL original GraphQL entry (nothing dropped)
//     sort_index   entry sortIndex
//     status_id    stable tweet id ("" for a cursor-only line)
//   Like the CDP path, each page also emits one cursor-bottom-<page> line whose
//   `raw` carries a TimelineTimelineCursor entry, so collect.py's fixture loader
//   round-trips it. Tweet entries keep their original entry under `raw` so the
//   ingest parser finds tweet_results.result / legacy / core untouched.
//
// USAGE
//   1. Install Tampermonkey (or Violentmonkey) and add this script.
//   2. Open https://x.com/i/bookmarks and scroll to the bottom (all the way, so
//      every page loads). A small counter overlay shows captured tweets.
//   3. Click "Download bookmarks_raw.jsonl" in the overlay (or it auto-downloads
//      when you stop scrolling at the end).
//   4. Import: python3 scripts/collect.py --kb <KB> --engine userscript \
//                 --fixture /path/to/bookmarks_raw.jsonl

(function () {
  "use strict";

  // Only act on the Bookmarks GraphQL endpoint. The operation name is
  // "Bookmarks"; the queryId rotates, so we match on the path segment, not a
  // hardcoded id.
  var BOOKMARKS_RE = /\/i\/api\/graphql\/[^/]+\/Bookmarks\b/;

  // Captured RawBookmark envelope objects, in capture order. De-duped on
  // status_id so re-scrolling the same page does not double-count.
  var captured = [];
  var seenStatus = Object.create(null);
  var seenCursorLine = Object.create(null);
  var pageCounter = 0; // increments once per parsed Bookmarks response

  function nowIso() {
    // ISO8601 UTC, second precision, trailing Z -- matches collect._now_iso().
    return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  }

  // --- parse a Bookmarks GraphQL response, mirroring collect.parse_timeline -- //
  function instructionsOf(graphql) {
    if (!graphql || typeof graphql !== "object") return [];
    var data = graphql.data || graphql;
    if (!data || typeof data !== "object") return [];
    var keys = ["bookmark_timeline_v2", "bookmark_timeline", "bookmarks"];
    for (var i = 0; i < keys.length; i++) {
      var tl = data[keys[i]];
      if (tl && typeof tl === "object" && tl.timeline &&
          Array.isArray(tl.timeline.instructions)) {
        return tl.timeline.instructions;
      }
    }
    if (data.timeline && Array.isArray(data.timeline.instructions)) {
      return data.timeline.instructions;
    }
    return [];
  }

  // Stable tweet id for an entry: nested rest_id first, then entryId suffix.
  // Mirrors collect.status_id_of. Returns "" for cursor / non-tweet entries.
  function statusIdOf(entry) {
    var entryId = String((entry && entry.entryId) || "");
    if (entryId.indexOf("cursor-") === 0) return "";
    try {
      var res = entry.content.itemContent.tweet_results.result;
      var rid = res.rest_id || (res.legacy && res.legacy.id_str);
      if (rid) return String(rid);
    } catch (e) { /* fall through to entryId suffix */ }
    if (entryId.indexOf("tweet-") === 0) return entryId.slice("tweet-".length);
    return "";
  }

  // Returns { entries: [rawEntry...], cursor: "<bottom cursor>" }.
  function parseTimeline(graphql) {
    var entries = [];
    var bottomCursor = "";
    var instr = instructionsOf(graphql);
    for (var i = 0; i < instr.length; i++) {
      var ins = instr[i];
      if (!ins || typeof ins !== "object") continue;
      var rawEntries = [];
      if (Array.isArray(ins.entries)) rawEntries = ins.entries;
      else if (ins.entry && typeof ins.entry === "object") rawEntries = [ins.entry];
      for (var j = 0; j < rawEntries.length; j++) {
        var entry = rawEntries[j];
        if (!entry || typeof entry !== "object") continue;
        var entryId = String(entry.entryId || "");
        var content = entry.content || {};
        var ctype = String(content.entryType || content.__typename || "");
        if (ctype.indexOf("Cursor") !== -1 || entryId.indexOf("cursor-") === 0) {
          var cdir = String(content.cursorType || "").toLowerCase();
          if (cdir === "bottom" || entryId.toLowerCase().indexOf("bottom") !== -1) {
            bottomCursor = String(content.value || "") || bottomCursor;
          }
          continue;
        }
        entries.push(entry);
      }
    }
    return { entries: entries, cursor: bottomCursor };
  }

  // Wrap one GraphQL entry in the RawBookmark envelope (top-level fields lifted,
  // full entry kept under `raw`). Mirrors collect.envelope.
  function envelope(entry, page, cursor, capturedAt) {
    return {
      status_id: statusIdOf(entry),
      entry_id: String(entry.entryId || ""),
      sort_index: String(entry.sortIndex || ""),
      captured_at: capturedAt,
      page: page,
      cursor: cursor,
      raw: entry
    };
  }

  // The synthetic cursor-bottom line, identical in shape to the one collect.py
  // appends per page so the file round-trips through the fixture loader.
  function cursorLine(page, cursor, capturedAt) {
    return {
      status_id: "",
      entry_id: "cursor-bottom-" + page,
      sort_index: "",
      captured_at: capturedAt,
      page: page,
      cursor: cursor,
      raw: {
        content: {
          entryType: "TimelineTimelineCursor",
          __typename: "TimelineTimelineCursor",
          cursorType: "Bottom",
          value: cursor
        },
        entryId: "cursor-bottom-" + page
      }
    };
  }

  function ingestResponse(graphql) {
    var parsed = parseTimeline(graphql);
    if (!parsed.entries.length && !parsed.cursor) return;
    var page = pageCounter++;
    var capturedAt = nowIso();
    var added = 0;
    for (var i = 0; i < parsed.entries.length; i++) {
      var env = envelope(parsed.entries[i], page, parsed.cursor, capturedAt);
      if (env.status_id) {
        if (seenStatus[env.status_id]) continue; // de-dupe across rescrolls
        seenStatus[env.status_id] = true;
      }
      captured.push(env);
      added++;
    }
    if (parsed.cursor && !seenCursorLine[parsed.cursor]) {
      seenCursorLine[parsed.cursor] = true;
      captured.push(cursorLine(page, parsed.cursor, capturedAt));
    }
    if (added) updateOverlay();
  }

  function tweetCount() {
    var n = 0;
    for (var i = 0; i < captured.length; i++) if (captured[i].status_id) n++;
    return n;
  }

  // --- serialize to JSONL with SORTED keys (matches schema.append_jsonl) ----- //
  function sortedStringify(obj) {
    // JSON.stringify with a key-sorting replacer applied recursively, so the
    // top-level RawBookmark keys AND nested `raw` keys emit in sorted order,
    // byte-compatible with Python's json.dumps(sort_keys=True). ensure_ascii is
    // false by default in JS (UTF-8 preserved).
    return JSON.stringify(obj, function (key, value) {
      if (value && typeof value === "object" && !Array.isArray(value)) {
        var out = {};
        Object.keys(value).sort().forEach(function (k) { out[k] = value[k]; });
        return out;
      }
      return value;
    });
  }

  function toJsonl() {
    var lines = [];
    for (var i = 0; i < captured.length; i++) {
      lines.push(sortedStringify(captured[i]));
    }
    return lines.join("\n") + (lines.length ? "\n" : "");
  }

  function download() {
    if (!captured.length) {
      alert("No bookmarks captured yet. Open x.com/i/bookmarks and scroll.");
      return;
    }
    var blob = new Blob([toJsonl()], { type: "application/x-ndjson;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = "bookmarks_raw.jsonl";
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(url);
      a.remove();
    }, 1000);
  }

  // --- network interception: wrap fetch and XHR, READ ONLY -------------------- //
  // We never change the request. We clone the response and parse it after the
  // page has already consumed it, so X's own machinery (and its anti-bot
  // headers) are untouched.
  var origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function () {
      var args = arguments;
      var reqUrl = "";
      try {
        reqUrl = (typeof args[0] === "string") ? args[0]
               : (args[0] && args[0].url) || "";
      } catch (e) { reqUrl = ""; }
      var p = origFetch.apply(this, args);
      if (reqUrl && BOOKMARKS_RE.test(reqUrl)) {
        p.then(function (resp) {
          try {
            resp.clone().json().then(function (data) {
              try { ingestResponse(data); } catch (e) { /* ignore parse errors */ }
            }).catch(function () {});
          } catch (e) { /* clone unsupported: ignore */ }
        }).catch(function () {});
      }
      return p;
    };
  }

  var OrigXHR = window.XMLHttpRequest;
  if (OrigXHR) {
    var origOpen = OrigXHR.prototype.open;
    var origSend = OrigXHR.prototype.send;
    OrigXHR.prototype.open = function (method, url) {
      try { this.__bm_url = url || ""; } catch (e) {}
      return origOpen.apply(this, arguments);
    };
    OrigXHR.prototype.send = function () {
      var xhr = this;
      try {
        if (xhr.__bm_url && BOOKMARKS_RE.test(String(xhr.__bm_url))) {
          xhr.addEventListener("load", function () {
            try {
              if (xhr.responseType === "" || xhr.responseType === "text") {
                var data = JSON.parse(xhr.responseText);
                ingestResponse(data);
              }
            } catch (e) { /* ignore non-JSON / parse errors */ }
          });
        }
      } catch (e) {}
      return origSend.apply(this, arguments);
    };
  }

  // --- minimal overlay (counter + download button) ---------------------------- //
  var overlay = null;
  var counterEl = null;

  function buildOverlay() {
    if (overlay) return;
    overlay = document.createElement("div");
    overlay.style.cssText = [
      "position:fixed", "z-index:2147483647", "bottom:16px", "right:16px",
      "background:#15202b", "color:#e7e9ea", "font:13px/1.4 -apple-system,Segoe UI,Inter,sans-serif",
      "padding:10px 12px", "border:1px solid #38444d", "border-radius:10px",
      "box-shadow:0 4px 16px rgba(0,0,0,.4)", "min-width:180px"
    ].join(";");
    counterEl = document.createElement("div");
    counterEl.style.cssText = "margin-bottom:8px;font-weight:600;";
    var btn = document.createElement("button");
    btn.textContent = "Download bookmarks_raw.jsonl";
    btn.style.cssText = [
      "cursor:pointer", "width:100%", "padding:6px 10px", "border:0",
      "border-radius:8px", "background:#1d9bf0", "color:#fff", "font-weight:600",
      "font-size:12px"
    ].join(";");
    btn.addEventListener("click", download);
    overlay.appendChild(counterEl);
    overlay.appendChild(btn);
    (document.body || document.documentElement).appendChild(overlay);
    updateOverlay();
  }

  function updateOverlay() {
    if (!counterEl) return;
    counterEl.textContent = "Captured " + tweetCount() + " bookmarks";
  }

  function init() {
    // Show the overlay only on the bookmarks page to stay out of the way.
    if (location.pathname.indexOf("/i/bookmarks") === 0 ||
        location.pathname.indexOf("/bookmarks") !== -1) {
      buildOverlay();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
  // X is a SPA; re-check on navigation so the overlay appears when the user
  // routes into bookmarks without a full reload.
  var lastPath = location.pathname;
  setInterval(function () {
    if (location.pathname !== lastPath) {
      lastPath = location.pathname;
      init();
    }
  }, 1500);
})();
