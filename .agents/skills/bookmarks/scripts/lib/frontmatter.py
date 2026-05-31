"""Minimal YAML-ish frontmatter for note .md files. Stdlib only, NO pyyaml.

This supports exactly the field shapes the KB notes use and nothing more:

    ---
    id: 11.01
    status_id: "1730000000000000000"
    title: "DPO vs PPO for RLHF: a practical comparison"
    url: https://x.com/someone/status/1730000000000000000
    author: "@someone"
    saved: 2026-05-12
    type: thread
    category: 11_llm-training
    tags: [rlhf, training, alignment]
    content_hash: "9f2c..."
    ---
    <markdown body>

Value rules (deliberately small and round-trip safe):
  * Scalars are strings. Quote with double quotes when the value contains a
    character that would otherwise be ambiguous (leading digit that must stay a
    string like status_id, a leading '@', a ':' , '#', '[', quotes, or
    leading/trailing space). Plain scalars are emitted unquoted.
  * Lists are inline flow style: ``[a, b, c]``. Items are scalars, each quoted
    by the same rule. An empty list is ``[]``.
  * ONE level of nesting (added for media + engagement):
      - an inline FLAT MAP:        ``{likes: 0, retweets: 0, views: 1234}``
        inner values are scalars (``str`` or ``int``); emitted unquoted for
        ints and via the scalar rule for strings.
      - a LIST OF FLAT MAPS:       ``[{kind: photo, url: ..., alt: "chart"}]``
        each item is a flat ``str->str`` map. An empty list is ``[]``.
    Inner maps are flat (no nested-in-nested); that is all the notes need.
  * No block lists, no multiline scalars. The body holds prose.

parse() returns (meta: dict[str, str|list|dict], body: str).
dump() / emit() takes an ordered meta dict + body and returns the full file
text. Field order is preserved as given (use FIELD_ORDER for canonical notes).
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple, Union

Scalar = str
Value = Union[Scalar, List[Any], Dict[str, Any]]

# Canonical field order for KB notes (build_kb.py emits in this order).
# Append-only: new keys slot in after the existing keys; content_hash stays LAST
# so old notes still parse. Of the new keys only `media` is
# hashed (it was already in hashing._MEANINGFUL_FIELDS via kind+url+alt); the
# rest (canonical_url, lang, media_count, thumb, media_alt, engagement) are
# derived / drift / bookkeeping and are NOT hashed.
FIELD_ORDER = [
    "id", "status_id", "title", "url", "canonical_url", "author", "posted",
    "saved", "type", "lang", "category", "tags", "media_count", "thumb",
    "media_alt", "engagement", "media", "content_hash",
]

_FENCE = "---"

# Characters that force quoting of a plain scalar.
_NEEDS_QUOTE_CHARS = set(":#[]{},\"'@&*!|>%`")


def _scalar_needs_quote(s: str) -> bool:
    if s == "":
        return True
    if s != s.strip():  # leading/trailing whitespace
        return True
    if s[0].isdigit():  # keep numeric-looking ids as strings (e.g. status_id)
        return True
    if s[0] in "-?:":
        return True
    if s.lower() in ("true", "false", "null", "yes", "no", "~"):
        return True
    for ch in s:
        if ch in _NEEDS_QUOTE_CHARS:
            return True
    return False


def _emit_scalar(s: str) -> str:
    s = str(s)
    if _scalar_needs_quote(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _emit_inner_scalar(x: Any) -> str:
    """Scalar inside a nested map: ints stay bare (engagement counts), strings
    follow the normal quoting rule. bool is treated as its lowercase literal."""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    return _emit_scalar(str(x))


def _emit_flat_map(d: Dict[str, Any]) -> str:
    """``{k: v, k: v}`` — a flat one-level map (keys are bare slugs). Insertion
    order is preserved so engagement/media maps emit deterministically."""
    parts = []
    for k, val in d.items():
        parts.append("{}: {}".format(str(k), _emit_inner_scalar(val)))
    return "{" + ", ".join(parts) + "}"


def _emit_value(v: Value) -> str:
    if isinstance(v, dict):
        return _emit_flat_map(v)
    if isinstance(v, (list, tuple)):
        seq = list(v)
        # list of flat maps: ``[{...}, {...}]`` (media). An empty list is ``[]``.
        if seq and all(isinstance(x, dict) for x in seq):
            return "[" + ", ".join(_emit_flat_map(x) for x in seq) + "]"
        return "[" + ", ".join(_emit_scalar(str(x)) for x in seq) + "]"
    # A real top-level integer (media_count) emits bare so the note reads
    # ``media_count: 1`` (booleans excluded; they are not used as scalars here).
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return _emit_scalar(str(v))


def _parse_scalar(tok: str) -> str:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        inner = tok[1:-1]
        if tok[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return tok


def _split_top_level(body: str) -> List[str]:
    """Split ``body`` on top-level commas, respecting quotes AND ``{}`` braces.

    Used for both flow lists and flat maps. A comma inside a quoted string or
    inside a nested ``{...}`` map does NOT split (so ``[{a: 1, b: 2}, {c: 3}]``
    yields the two map chunks, not four)."""
    items: List[str] = []
    cur = []
    quote = ""
    depth = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(body):
                cur.append(body[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == "{":
            depth += 1
            cur.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    tail = "".join(cur)
    if tail.strip() or items:
        items.append(tail)
    return items


def _split_flow_list(body: str) -> List[str]:
    """Split the inside of ``[ ... ]`` of SCALARS on commas, respecting quotes."""
    return [_parse_scalar(x) for x in _split_top_level(body) if x.strip() != ""]


def _coerce_inner(tok: str) -> Any:
    """Parse an inner-map value: a bare integer stays an int (engagement
    counts), everything else is a (possibly quoted) string scalar."""
    t = tok.strip()
    if t and (t.lstrip("-").isdigit()):
        try:
            return int(t)
        except ValueError:
            pass
    return _parse_scalar(t)


def _parse_flat_map(body: str) -> Dict[str, Any]:
    """Parse the inside of ``{ ... }`` into a flat ``str -> (str|int)`` map.
    Keys are bare slugs; the first ``:`` separates key from value."""
    out: Dict[str, Any] = {}
    for chunk in _split_top_level(body):
        if chunk.strip() == "":
            continue
        if ":" not in chunk:
            continue
        k, _, v = chunk.partition(":")
        out[k.strip()] = _coerce_inner(v)
    return out


def _parse_value(raw: str) -> Value:
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        inner = raw[1:-1].strip()
        return _parse_flat_map(inner) if inner else {}
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if inner == "":
            return []
        # A list whose first non-space item starts with '{' is a list of flat
        # maps (media); otherwise a flow list of scalars.
        if inner[0] == "{":
            out: List[Dict[str, Any]] = []
            for chunk in _split_top_level(inner):
                c = chunk.strip()
                if c.startswith("{") and c.endswith("}"):
                    out.append(_parse_flat_map(c[1:-1].strip()))
            return out
        return _split_flow_list(inner)
    return _parse_scalar(raw)


def parse(text: str) -> Tuple[Dict[str, Value], str]:
    """Parse a frontmatter document. If there is no leading ``---`` fence the
    whole text is treated as body with empty meta."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return {}, text

    meta: Dict[str, Value] = {}
    i = 1
    end = None
    while i < len(lines):
        if lines[i].strip() == _FENCE:
            end = i
            break
        line = lines[i]
        if line.strip() == "" or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        meta[key.strip()] = _parse_value(val)
        i += 1

    if end is None:
        # Malformed (no closing fence): treat everything after first fence as body.
        body = "\n".join(lines[1:])
        return meta, body

    body = "\n".join(lines[end + 1:])
    # A single leading blank line after the fence is conventional; strip one.
    if body.startswith("\n"):
        body = body[1:]
    return meta, body


def dump(meta: Dict[str, Value], body: str = "", order: List[str] = None) -> str:
    """Emit a full frontmatter document. Keys are emitted in ``order`` first
    (those present in meta), then any remaining keys in insertion order."""
    keys: List[str] = []
    if order:
        for k in order:
            if k in meta:
                keys.append(k)
    for k in meta:
        if k not in keys:
            keys.append(k)

    out = [_FENCE]
    for k in keys:
        out.append("{}: {}".format(k, _emit_value(meta[k])))
    out.append(_FENCE)
    text = "\n".join(out) + "\n"
    if body:
        text += "\n" + body
        if not text.endswith("\n"):
            text += "\n"
    return text


# Alias used by some callers/tests.
emit = dump


def loads(text: str) -> Tuple[Dict[str, Value], str]:
    return parse(text)


def dumps(meta: Dict[str, Value], body: str = "", order: List[str] = None) -> str:
    return dump(meta, body, order)
