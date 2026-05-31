#!/usr/bin/env python3
"""ui_template.py — render the self-contained kb.html UI.

Stdlib only. ``render(payload)`` returns a complete single-file HTML document:
inline CSS + JS, no external requests, no build step. The bookmark index is
embedded as inline JSON in a ``<script type="application/json">`` tag, so the
file works by double-clicking it (``file://``) with zero server.

Design: minimal modern, command-palette / list-first.
Monochrome + one accent, dark default with a light toggle, system/Inter type,
a single search field that owns the screen, Cmd/Ctrl-K (or ``/``) to focus,
dense one-line rows grouped by Johnny.Decimal area, full keyboard nav (up/down,
Enter expands a row inline into a detail panel, Esc clears), inline filter tokens
(``#tag``, ``area:``, ``from:@``, ``is:thread``), a persistent footer with live
counts + active filter, an honest zero-result state, sub-50ms client-side fuzzy
filtering over the embedded index.

The payload is the ONLY thing that varies between KBs; everything else is static
markup. We inject it by replacing a unique sentinel so CSS/JS braces never need
escaping.
"""

from __future__ import annotations

import json
from typing import Any, Dict

# Unique sentinel the template replaces with the embedded JSON. Chosen so it can
# never collide with CSS/JS content.
_DATA_SENTINEL = "/*__KB_DATA__*/"


def _safe_json(payload: Dict[str, Any]) -> str:
    """JSON for embedding inside a <script> tag. We escape '<' so a literal
    '</script>' inside any field can never terminate the tag early, and escape
    line/paragraph separators that are valid JS string breaks."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e")
    text = text.replace(" ", "\\u2028").replace(" ", "\\u2029")
    text = text.replace("&", "\\u0026")
    return text


def render(payload: Dict[str, Any]) -> str:
    data = _safe_json(payload)
    return _TEMPLATE.replace(_DATA_SENTINEL, data)


# --------------------------------------------------------------------------- #
# The template. Single file. The <script id="kb-data"> body is the sentinel.
# --------------------------------------------------------------------------- #
_TEMPLATE = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TweetMark</title>
<style>
:root{
  --bg:#0b0c0e; --panel:#111317; --row:#0e1013; --row-hover:#15181d;
  --text:#e7e9ee; --muted:#9aa1ad; --faint:#6b7280; --line:#1d2026;
  --accent:#7aa2ff; --accent-soft:rgba(122,162,255,.14);
  --pill:#181b21; --shadow:none;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
html[data-theme="light"]{
  --bg:#fbfbfc; --panel:#ffffff; --row:#ffffff; --row-hover:#f3f4f6;
  --text:#14161a; --muted:#5b626d; --faint:#9aa1ad; --line:#e6e8ec;
  --accent:#2f6bff; --accent-soft:rgba(47,107,255,.10);
  --pill:#f1f2f5;
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0; background:var(--bg); color:var(--text);
  font-family:var(--sans); font-size:14px; line-height:1.45;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
.app{max-width:880px; margin:0 auto; min-height:100%; display:flex; flex-direction:column;}
/* ---- header / search ---- */
.bar{
  position:sticky; top:0; z-index:5; background:linear-gradient(var(--bg),var(--bg) 70%,transparent);
  padding:26px 20px 14px;
}
.searchwrap{
  display:flex; align-items:center; gap:10px;
  background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:0 14px; height:52px;
}
.searchwrap:focus-within{border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft);}
.searchwrap .glyph{color:var(--faint); font-size:15px; flex:none;}
#q{
  flex:1; background:transparent; border:0; outline:0; color:var(--text);
  font-family:var(--sans); font-size:16px; height:100%;
}
#q::placeholder{color:var(--faint);}
.kbd{
  font-family:var(--mono); font-size:11px; color:var(--muted);
  border:1px solid var(--line); border-radius:6px; padding:2px 6px; background:var(--row);
  flex:none;
}
.iconbtn{
  flex:none; cursor:pointer; color:var(--muted); background:transparent;
  border:1px solid var(--line); border-radius:8px; height:34px; width:34px;
  font-size:15px; display:grid; place-items:center;
}
.iconbtn:hover{color:var(--text); border-color:var(--accent);}
.hint{
  margin:10px 2px 0; color:var(--faint); font-size:12px; font-family:var(--mono);
  display:flex; flex-wrap:wrap; gap:4px 14px;
}
.hint b{color:var(--muted); font-weight:600;}
/* ---- list ---- */
.list{flex:1; padding:4px 12px 90px;}
.area{margin:18px 8px 6px; color:var(--muted); font-size:11px; font-weight:700;
  letter-spacing:.09em; text-transform:uppercase; display:flex; align-items:baseline; gap:8px;}
.area .acount{color:var(--faint); font-weight:500; letter-spacing:0;}
.row{
  display:flex; align-items:center; gap:12px; padding:9px 12px; border-radius:9px;
  cursor:pointer; border:1px solid transparent;
}
.row:hover{background:var(--row-hover);}
.row.sel{background:var(--accent-soft); border-color:var(--accent);}
.row .marker{color:var(--faint); flex:none; width:10px; text-align:center; font-size:12px;}
.row.sel .marker{color:var(--accent);}
.row .title{flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.row .ty{
  flex:none; font-size:10.5px; font-family:var(--mono); color:var(--muted);
  background:var(--pill); border:1px solid var(--line); border-radius:5px; padding:1px 6px;
}
.row .who{flex:none; color:var(--muted); font-size:12.5px; max-width:140px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.row .when{flex:none; color:var(--faint); font-size:12px; font-variant-numeric:tabular-nums;
  width:82px; text-align:right; white-space:nowrap;}
mark{background:transparent; color:var(--accent); font-weight:600;}
/* ---- detail panel (inline expand) ---- */
.detail{
  margin:2px 4px 10px; padding:16px 18px; background:var(--panel);
  border:1px solid var(--line); border-left:2px solid var(--accent); border-radius:10px;
}
.detail .dtldr{font-size:15px; line-height:1.5; margin:0 0 10px;}
.detail .dwhy{color:var(--muted); margin:0 0 12px;}
.detail .dfull{white-space:pre-wrap; font-size:14px; line-height:1.55; color:var(--text);
  margin:0 0 12px; padding:10px 12px; background:var(--pill); border-radius:8px;
  max-height:360px; overflow-y:auto;}
.detail ul{margin:0 0 12px; padding-left:18px; color:var(--text);}
.detail ul li{margin:2px 0;}
.detail .meta{display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-top:6px;}
.detail .open{
  margin-left:auto; color:var(--accent); text-decoration:none; font-size:13px; font-weight:600;
  white-space:nowrap;
}
.detail .open:hover{text-decoration:underline;}
.detail .byline{color:var(--faint); font-size:12px; margin:0 0 12px;}
/* snapshot-only label (deleted tweets: no open-original link) */
.snaponly{color:var(--faint); font-size:13px; margin-left:auto; white-space:nowrap;}
/* media thumbnail (detail panel only; rows stay dense + media-free). The image
   is hotlinked from the tokenless pbs.twimg.com CDN, lazily + with no referrer,
   and self-heals (onerror removes it) if a blocker or rotted URL fails. */
.detail .thumb{
  display:block; max-width:320px; max-height:240px; width:auto; height:auto;
  margin:0 0 12px; border:1px solid var(--line); border-radius:8px;
  background:var(--row);
}
/* ---- empty state ---- */
.empty{padding:64px 24px; text-align:center; color:var(--muted);}
.empty .big{font-size:16px; color:var(--text); margin-bottom:6px;}
.empty .sub{font-size:13px; color:var(--faint);}
.empty code{font-family:var(--mono); color:var(--accent); background:var(--pill);
  padding:1px 6px; border-radius:5px;}
/* ---- footer ---- */
.foot{
  position:fixed; left:0; right:0; bottom:0; z-index:6;
  background:var(--panel); border-top:1px solid var(--line);
  font-family:var(--mono); font-size:11.5px; color:var(--muted);
  padding:8px 18px; display:flex; align-items:center; gap:16px;
}
.foot .grow{flex:1;}
.foot .filt{color:var(--accent);}
.foot .k{color:var(--faint);}
.foot .k b{color:var(--muted); font-weight:600;}
@media (max-width:560px){
  .row .who{display:none;} .hint{display:none;}
  .bar{padding:18px 12px 10px;}
}
</style>
</head>
<body>
<div class="app">
  <header class="bar">
    <div class="searchwrap">
      <span class="glyph">&#9906;</span>
      <input id="q" type="text" autocomplete="off" autocapitalize="off"
             autocorrect="off" spellcheck="false" aria-label="Search bookmarks"
             placeholder="Search bookmarks…  try #tag  area:ai  from:@handle  is:thread" />
      <span class="kbd" id="kbdhint">/</span>
      <button class="iconbtn" id="theme" title="Toggle theme (t)" aria-label="Toggle theme">&#9681;</button>
    </div>
  </header>
  <main class="list" id="list" role="listbox" aria-label="Bookmarks"></main>
</div>
<footer class="foot">
  <span id="counts"></span>
  <span class="grow"></span>
  <span class="filt" id="activefilter"></span>
  <span class="k"><b>&#8984;K</b>/<b>/</b> search &middot; <b>&#8593;&#8595;</b> move &middot; <b>&#8629;</b> open &middot; <b>esc</b> clear</span>
</footer>

<script type="application/json" id="kb-data">/*__KB_DATA__*/</script>
<script>
(function(){
  "use strict";
  // ---- data ----------------------------------------------------------------
  var DATA = JSON.parse(document.getElementById("kb-data").textContent || "{}");
  var ITEMS = DATA.items || [];
  var AREAS = DATA.areas || [];
  var TOTAL = ITEMS.length;
  var AREA_COUNT = AREAS.length;

  // Precompute a lowercased haystack per item for fast fuzzy/substring scans.
  ITEMS.forEach(function(it, i){
    it._i = i;
    it._hay = (
      (it.title||"") + " " + (it.tldr||"") + " " + (it.why||"") + " " +
      (it.author||"") + " " + (it.tags||[]).join(" ") + " " +
      (it.type||"") + " " + (it.areaLabel||"") + " " + (it.areaSlug||"") + " " +
      (it.points||[]).join(" ") + " " + (it.text||"")
    ).toLowerCase();
    it._title_l = (it.title||"").toLowerCase();
  });

  var listEl = document.getElementById("list");
  var qEl = document.getElementById("q");
  var countsEl = document.getElementById("counts");
  var activeFilterEl = document.getElementById("activefilter");

  var state = { results: [], sel: 0, expanded: null, q: "" };

  // ---- query parsing -------------------------------------------------------
  // Tokens: #tag, area:x, from:@h, is:type. Everything else is free text.
  // A structured token with an EMPTY value (after the prefix, trimmed, with a
  // leading '@' stripped for from:) is a NO-OP: it is dropped, not turned into a
  // filter that matches nothing. This keeps mid-typing (#, area:, from:@, is:)
  // from blanking the list -- only a token WITH a value actually filters.
  function parseQuery(raw){
    var tags=[], areas=[], froms=[], types=[], words=[];
    (raw.trim().split(/\\s+/)).forEach(function(tok){
      if(!tok) return;
      var low = tok.toLowerCase(), v;
      if(low.indexOf("#")===0){ v=low.slice(1); if(v) tags.push(v); }
      else if(low.indexOf("area:")===0){ v=low.slice(5); if(v) areas.push(v); }
      else if(low.indexOf("from:")===0){ v=low.slice(5).replace(/^@/,""); if(v) froms.push(v); }
      else if(low.indexOf("is:")===0){ v=low.slice(3); if(v) types.push(v); }
      else { words.push(low); }
    });
    return {tags:tags, areas:areas, froms:froms, types:types, words:words};
  }

  // is:<type> aliases. Media tweets are stored as type "media", but users type
  // is:photo / is:image / is:video / is:gif -- all of those alias to "media".
  // Any other is:<type> (thread/quote/link/deleted/tweet/...) keeps the plain
  // substring match against the item's type.
  var TYPE_ALIASES = { photo:"media", image:"media", video:"media", gif:"media" };
  function typeMatches(itemType, want){
    itemType = (itemType||"").toLowerCase();
    var alias = TYPE_ALIASES[want];
    if(alias) return itemType.indexOf(alias)!==-1;
    return itemType.indexOf(want)!==-1;
  }

  // subsequence fuzzy match: are all chars of needle present in order in hay?
  function subseq(needle, hay){
    if(!needle) return true;
    var n=0;
    for(var h=0; h<hay.length && n<needle.length; h++){
      if(hay[h] === needle[n]) n++;
    }
    return n === needle.length;
  }

  function matches(it, p){
    var i, ok;
    for(i=0;i<p.tags.length;i++){
      ok=false;
      for(var t=0;t<(it.tags||[]).length;t++){
        if(it.tags[t].toLowerCase().indexOf(p.tags[i])!==-1){ok=true;break;}
      }
      if(!ok) return false;
    }
    for(i=0;i<p.areas.length;i++){
      if((it.areaSlug||"").toLowerCase().indexOf(p.areas[i])===-1 &&
         (it.areaLabel||"").toLowerCase().indexOf(p.areas[i])===-1) return false;
    }
    for(i=0;i<p.froms.length;i++){
      if((it.author||"").toLowerCase().replace(/^@/,"").indexOf(p.froms[i])===-1) return false;
    }
    for(i=0;i<p.types.length;i++){
      if(!typeMatches(it.type, p.types[i])) return false;
    }
    // free words: each word must appear (substring) OR fuzzy-match the title.
    for(i=0;i<p.words.length;i++){
      var w=p.words[i];
      if(it._hay.indexOf(w)!==-1) continue;
      if(w.length>=3 && subseq(w, it._title_l)) continue;
      return false;
    }
    return true;
  }

  // crude relevance score so the best rows float up within an area-less view.
  function score(it, p){
    var s=0, i;
    for(i=0;i<p.words.length;i++){
      var w=p.words[i];
      var ti=it._title_l.indexOf(w);
      if(ti===0) s+=12; else if(ti>0) s+=6;
      else if(it._hay.indexOf(w)!==-1) s+=2;
      else s+=1; // fuzzy
    }
    if(p.words.length===0) s=0;
    return s;
  }

  function activeFilterLabel(p){
    var parts=[];
    p.tags.forEach(function(t){parts.push("#"+t);});
    p.areas.forEach(function(a){parts.push("area:"+a);});
    p.froms.forEach(function(f){parts.push("from:@"+f);});
    p.types.forEach(function(t){parts.push("is:"+t);});
    if(p.words.length) parts.push('"'+p.words.join(" ")+'"');
    return parts.join("  ");
  }

  // ---- render --------------------------------------------------------------
  function esc(s){
    return String(s==null?"":s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;");
  }
  // Build the thumbnail src. For a pbs.twimg.com/media/ URL with no existing
  // size/format query, request the small (680px) JPEG still; otherwise keep the
  // URL as-is (already-sized CDN URLs, or a local .media/ cache path). Returns ""
  // for anything that is not an allowed image source, so no other host is hotlinked.
  function thumbSrc(u){
    u = String(u==null?"":u);
    if(!u) return "";
    var isCdn = (u.indexOf("https://pbs.twimg.com/") === 0
              || u.indexOf("http://pbs.twimg.com/") === 0);
    var isLocal = (u.indexOf(".media/") === 0 || u.indexOf("./.media/") === 0);
    if(!isCdn && !isLocal) return "";   // never hotlink an arbitrary remote host
    if(isCdn && u.indexOf("/media/") !== -1
       && u.indexOf("name=") === -1 && u.indexOf("format=") === -1){
      u += (u.indexOf("?") === -1 ? "?" : "&") + "format=jpg&name=small";
    }
    return u;
  }

  function hl(s, words){
    var out = esc(s);
    if(!words || !words.length) return out;
    // highlight whole-word-ish substrings, longest first to avoid nesting
    var ws = words.slice().filter(function(w){return w.length>=2;})
                  .sort(function(a,b){return b.length-a.length;});
    ws.forEach(function(w){
      var re = new RegExp("("+w.replace(/[.*+?^${}()|[\\]\\\\]/g,"\\\\$&")+")","ig");
      out = out.replace(re, "<mark>$1</mark>");
    });
    return out;
  }

  function compute(){
    var p = parseQuery(state.q);
    var res = [];
    for(var i=0;i<ITEMS.length;i++){
      if(matches(ITEMS[i], p)){ res.push(ITEMS[i]); }
    }
    var searching = (p.words.length>0);
    if(searching){
      res.sort(function(a,b){
        var d = score(b,p)-score(a,p);
        if(d) return d;
        return a._i-b._i;
      });
    }
    state.results = res;
    state.parsed = p;
    state.searching = searching;
    if(state.sel >= res.length) state.sel = res.length? res.length-1 : 0;
    if(state.sel < 0) state.sel = 0;
  }

  function render(){
    var p = state.parsed, res = state.results;
    listEl.innerHTML = "";
    // footer counts
    countsEl.innerHTML = "<b>"+(res.length===TOTAL?TOTAL:res.length)+"</b>"
      + (res.length===TOTAL? " saved" : " of "+TOTAL)
      + " &middot; " + AREA_COUNT + " area"+(AREA_COUNT===1?"":"s");
    var fl = activeFilterLabel(p);
    activeFilterEl.textContent = fl ? ("filter: "+fl) : "";

    if(res.length===0){
      var e = document.createElement("div");
      e.className="empty";
      e.innerHTML = '<div class="big">No bookmarks match.</div>'
        + '<div class="sub">'
        + (state.q.trim()
            ? 'Nothing matched <code>'+esc(state.q.trim())+'</code>. Try fewer words, a broader <code>#tag</code>, or clear with <code>esc</code>.'
            : 'This knowledge base is empty. Run <code>import</code> to fetch your bookmarks.')
        + '</div>';
      listEl.appendChild(e);
      return;
    }

    var words = p.words;
    var frag = document.createDocumentFragment();
    var lastArea = null;
    var groupByArea = !state.searching; // when ranking by relevance, drop headers

    res.forEach(function(it, idx){
      if(groupByArea && it.area !== lastArea){
        lastArea = it.area;
        var ah = document.createElement("div");
        ah.className="area";
        var inArea = res.filter(function(r){return r.area===it.area;}).length;
        ah.innerHTML = esc(it.areaLabel||it.area) + ' <span class="acount">'+inArea+'</span>';
        frag.appendChild(ah);
      }
      var row = document.createElement("div");
      row.className = "row" + (idx===state.sel?" sel":"");
      row.setAttribute("role","option");
      row.dataset.idx = idx;
      row.innerHTML =
        '<span class="marker">'+(idx===state.sel?"\\u203a":"")+'</span>'
        + '<span class="title">'+hl(it.title||it.id, words)+'</span>'
        + (it.type? '<span class="ty">'+esc(it.type)+'</span>':'')
        + '<span class="who">'+esc(it.author||"")+'</span>'
        + '<span class="when">'+esc(it.date||"")+'</span>';
      frag.appendChild(row);
      if(idx===state.expanded){
        frag.appendChild(detailNode(it, words));
      }
    });
    listEl.appendChild(frag);
    scrollSelIntoView();
  }

  function detailNode(it, words){
    var d = document.createElement("div");
    d.className="detail";
    var html = "";
    html += '<div class="byline">'+esc(it.id)+' &middot; '+esc(it.author||"unknown")
          + (it.date? ' &middot; '+esc(it.date):'')+'</div>';
    // media thumbnail (photos + the poster still for video/animated_gif). Lazy,
    // no-referrer, decode-async, self-healing on error. Never an inline <video>.
    var tsrc = thumbSrc(it.thumb);
    if(tsrc){
      html += '<img class="thumb" src="'+esc(tsrc)+'" alt="'+esc(it.mediaAlt||"")+'"'
            + ' loading="lazy" decoding="async" referrerpolicy="no-referrer"'
            + ' onerror="this.remove()">';
    }
    if(it.tldr) html += '<p class="dtldr">'+hl(it.tldr, words)+'</p>';
    if(it.why)  html += '<p class="dwhy">'+hl(it.why, words)+'</p>';
    if(it.points && it.points.length){
      html += '<ul>';
      it.points.forEach(function(pt){ html += '<li>'+hl(pt, words)+'</li>'; });
      html += '</ul>';
    }
    if(it.text) html += '<div class="dfull">'+hl(it.text, words)+'</div>';
    html += '<div class="meta">';
    // open original: a deleted tweet has no live page, so we show a muted
    // snapshot-only label instead of a dead link. Otherwise prefer the
    // handle-independent canonical URL (resolves renamed/suspended handles).
    if(it.type === "deleted"){
      html += '<span class="snaponly">snapshot only \\u00b7 original deleted</span>';
    } else {
      var openHref = it.canonical || it.url;
      if(openHref){
        html += '<a class="open" href="'+esc(openHref)+'" target="_blank" rel="noopener">open original \\u2197</a>';
      }
    }
    html += '</div>';
    d.innerHTML = html;
    return d;
  }

  function addToken(tok){
    var cur = qEl.value.trim();
    if(cur.split(/\\s+/).indexOf(tok)!==-1) return;
    qEl.value = (cur? cur+" ":"") + tok + " ";
    onInput();
    qEl.focus();
  }

  function scrollSelIntoView(){
    var rows = listEl.querySelectorAll(".row");
    var el = rows[state.sel];
    if(el && el.scrollIntoView) el.scrollIntoView({block:"nearest"});
  }

  // ---- interaction ---------------------------------------------------------
  function onInput(){
    state.q = qEl.value;
    state.sel = 0;
    state.expanded = null;
    compute();
    render();
  }

  function move(delta){
    if(!state.results.length) return;
    state.sel = (state.sel + delta + state.results.length) % state.results.length;
    state.expanded = (state.expanded!=null) ? state.sel : null; // keep panel following selection if open
    render();
  }

  function toggleExpand(){
    if(!state.results.length) return;
    state.expanded = (state.expanded===state.sel) ? null : state.sel;
    render();
  }

  function clearAll(){
    if(qEl.value){ qEl.value=""; onInput(); return; }
    if(state.expanded!=null){ state.expanded=null; render(); return; }
    qEl.blur();
  }

  listEl.addEventListener("click", function(ev){
    var row = ev.target.closest(".row");
    if(!row) return;
    var idx = parseInt(row.dataset.idx,10);
    if(isNaN(idx)) return;
    state.sel = idx;
    state.expanded = (state.expanded===idx)? null : idx;
    render();
  });

  qEl.addEventListener("input", onInput);

  document.addEventListener("keydown", function(ev){
    var typing = document.activeElement === qEl;
    // global focus shortcuts
    if((ev.key==="k"||ev.key==="K") && (ev.metaKey||ev.ctrlKey)){
      ev.preventDefault(); qEl.focus(); qEl.select(); return;
    }
    if(ev.key==="/" && !typing){ ev.preventDefault(); qEl.focus(); return; }
    if(ev.key==="t" && !typing){ ev.preventDefault(); toggleTheme(); return; }

    if(ev.key==="ArrowDown"){ ev.preventDefault(); move(1); return; }
    if(ev.key==="ArrowUp"){ ev.preventDefault(); move(-1); return; }
    if(ev.key==="Enter"){ ev.preventDefault(); toggleExpand(); return; }
    if(ev.key==="Escape"){ ev.preventDefault(); clearAll(); return; }
    // type-to-search: a printable key anywhere focuses the box
    if(!typing && ev.key.length===1 && !ev.metaKey && !ev.ctrlKey && !ev.altKey){
      qEl.focus();
    }
  });

  // ---- theme ---------------------------------------------------------------
  function applyTheme(t){
    document.documentElement.setAttribute("data-theme", t);
    try{ localStorage.setItem("kb-theme", t); }catch(e){}
  }
  function toggleTheme(){
    var cur = document.documentElement.getAttribute("data-theme");
    applyTheme(cur==="dark"?"light":"dark");
  }
  document.getElementById("theme").addEventListener("click", toggleTheme);
  try{
    var saved = localStorage.getItem("kb-theme");
    if(saved) applyTheme(saved);
  }catch(e){}

  // mac vs other: show the right focus hint
  try{
    if(/Mac|iPhone|iPad/.test(navigator.platform)) document.getElementById("kbdhint").textContent="\\u2318K";
  }catch(e){}

  // ---- boot ----------------------------------------------------------------
  compute();
  render();
  // focus search on load for instant typing
  setTimeout(function(){ try{ qEl.focus(); }catch(e){} }, 0);
})();
</script>
</body>
</html>
"""
