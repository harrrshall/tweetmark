"""Utility helpers: profile discovery, temp dirs, jittered sleep, atomic write.

Stdlib only. Nothing here decrypts cookies or touches the network; profile
detection is a presence check over the Cookies sqlite (does an x.com/twitter.com
host_key row exist?) so it never reads secret values.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import tempfile
import time
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# KB root resolution
# --------------------------------------------------------------------------- #
def default_kb_root() -> str:
    """BOOKMARKS_KB env var, else ~/Documents/Twitter Bookmarks."""
    env = os.environ.get("BOOKMARKS_KB")
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.expanduser("~"), "Documents", "Twitter Bookmarks")


def state_dir(kb_root: str) -> str:
    return os.path.join(kb_root, ".state")


# --------------------------------------------------------------------------- #
# Canonical, handle-independent tweet permalink. X redirects this form to the
# live handle, so renamed / suspended / deleted / fake handles still resolve.
# This is intentionally DERIVED purely from status_id (no handle), so it can be
# regenerated anywhere from the id alone and never depends on a stored handle.
# It is the robust "open original" target; it is NOT part of the content hash.
# (Distinct from doctor.canonical_url, which is an unrelated dedup normalizer.)
# --------------------------------------------------------------------------- #
def canonical_url(status_id: str) -> str:
    """Return https://x.com/i/status/<status_id> (handle-independent permalink).

    Empty/falsey status_id yields "" so callers never emit a broken URL.
    """
    sid = str(status_id or "").strip()
    return "https://x.com/i/status/{}".format(sid) if sid else ""


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Chrome profile discovery (Linux). We only check for the *presence* of an
# x.com / twitter.com cookie row; we never read or decrypt cookie values.
# --------------------------------------------------------------------------- #
def chrome_config_root() -> str:
    """Default Chrome user-data root on Linux."""
    return os.path.join(os.path.expanduser("~"), ".config", "google-chrome")


def list_profiles(config_root: Optional[str] = None) -> List[str]:
    """List profile directory names under the Chrome config root
    (``Default``, ``Profile 3``, ...). Missing root -> empty list."""
    root = config_root or chrome_config_root()
    if not os.path.isdir(root):
        return []
    out: List[str] = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        if name == "Default" or name.startswith("Profile "):
            # A real profile has a Preferences file.
            if os.path.exists(os.path.join(full, "Preferences")):
                out.append(name)
    return out


def _cookies_db_path(profile_dir: str) -> Optional[str]:
    """Locate the Cookies sqlite for a profile dir (Network/ subdir on modern
    Chrome, legacy top-level fallback)."""
    for rel in (os.path.join("Network", "Cookies"), "Cookies"):
        p = os.path.join(profile_dir, rel)
        if os.path.exists(p):
            return p
    return None


def profile_has_x_cookie(profile_dir: str) -> bool:
    """True if the profile's Cookies db has any host_key for x.com or
    twitter.com. Opens the sqlite read-only via URI immutable mode so it works
    even while Chrome holds the file; never reads cookie *values*."""
    db = _cookies_db_path(profile_dir)
    if not db:
        return False
    uri = "file:{}?immutable=1&mode=ro".format(db.replace("?", "%3f"))
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        # Fall back to a temp copy if the live file cannot be opened.
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".cookies", delete=False)
            tmp.close()
            shutil.copy2(db, tmp.name)
            conn = sqlite3.connect(tmp.name, timeout=1.0)
        except (OSError, sqlite3.Error):
            return False
    try:
        cur = conn.execute(
            "SELECT 1 FROM cookies WHERE host_key LIKE ? OR host_key LIKE ? "
            "LIMIT 1",
            ("%x.com", "%twitter.com"),
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def discover_x_profiles(config_root: Optional[str] = None) -> List[Dict[str, object]]:
    """Return one dict per profile: {name, path, has_x_cookie}. The collector
    picks the first profile with has_x_cookie True (or asks the user)."""
    root = config_root or chrome_config_root()
    results: List[Dict[str, object]] = []
    for name in list_profiles(root):
        full = os.path.join(root, name)
        results.append({
            "name": name,
            "path": full,
            "has_x_cookie": profile_has_x_cookie(full),
        })
    return results


def pick_x_profile(config_root: Optional[str] = None) -> Optional[Dict[str, object]]:
    """Return the first profile whose cookie store holds an x.com/twitter.com
    cookie, or None if none qualify."""
    for prof in discover_x_profiles(config_root):
        if prof["has_x_cookie"]:
            return prof
    return None


# --------------------------------------------------------------------------- #
# Temp profile / temp dir helpers. The collector copies a profile's Local State
# + Cookies into a private temp --user-data-dir, then shreds it after the run.
# --------------------------------------------------------------------------- #
def make_private_tempdir(prefix: str = "bm-profile-") -> str:
    """Create a 0700 temp dir for a copied Chrome profile."""
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def shred_dir(path: str) -> None:
    """Best-effort secure-ish removal of a temp profile dir. Overwrites small
    files then removes the tree. Session cookies must not linger on disk."""
    if not path or not os.path.isdir(path):
        return
    for root, _dirs, files in os.walk(path):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                size = os.path.getsize(fp)
                if 0 < size <= 4 * 1024 * 1024:  # only bother for small files
                    with open(fp, "wb") as fh:
                        fh.write(b"\x00" * size)
                        fh.flush()
                        os.fsync(fh.fileno())
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Jittered sleep. Pacing is ~2.5-3s/page; jitter avoids a robotic cadence. Tests
# pass a fixed seed and capture the computed delays without really sleeping.
# --------------------------------------------------------------------------- #
def jittered_delay(base: float = 2.75, spread: float = 0.5,
                   seed: Optional[int] = None) -> float:
    """Return a delay in [base-spread/2, base+spread/2] using an optional fixed
    seed (deterministic for tests). Does NOT sleep."""
    rng = random.Random(seed) if seed is not None else random
    lo = base - spread / 2.0
    hi = base + spread / 2.0
    return rng.uniform(lo, hi)


def jittered_sleep(base: float = 2.75, spread: float = 0.5,
                   seed: Optional[int] = None, sleep_fn=time.sleep) -> float:
    """Compute a jittered delay and sleep for it. Returns the delay used.
    ``sleep_fn`` is injectable so tests pass a no-op and assert on the value."""
    d = jittered_delay(base=base, spread=spread, seed=seed)
    sleep_fn(d)
    return d


def backoff_delays(retries: int, base: float = 1.0, cap: float = 60.0,
                   seed: Optional[int] = None) -> List[float]:
    """Exponential backoff with full jitter for 429 handling. Deterministic
    when ``seed`` is given. Returns the planned delay for each retry index."""
    rng = random.Random(seed) if seed is not None else random
    out: List[float] = []
    for i in range(retries):
        ceil = min(cap, base * (2 ** i))
        out.append(rng.uniform(0, ceil))
    return out


# --------------------------------------------------------------------------- #
# Atomic write: stage to a temp file in the same dir, fsync, rename. An
# interrupted run never leaves a half-written state file.
# --------------------------------------------------------------------------- #
def atomic_write(path: str, data: str, encoding: str = "utf-8") -> None:
    """Atomically write text to ``path`` (temp + fsync + os.replace)."""
    d = os.path.dirname(os.path.abspath(path))
    ensure_dir(d)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: str, data: bytes) -> None:
    d = os.path.dirname(os.path.abspath(path))
    ensure_dir(d)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# seen.tsv helpers (status_id -> content_hash ledger). Shared by ingest/doctor.
# --------------------------------------------------------------------------- #
def read_seen(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
    return out


def write_seen(path: str, seen: Dict[str, str]) -> None:
    lines = ["{}\t{}".format(k, seen[k]) for k in sorted(seen)]
    atomic_write(path, "\n".join(lines) + ("\n" if lines else ""))


# --------------------------------------------------------------------------- #
# config.json (remembers the chosen profile, etc.)
# --------------------------------------------------------------------------- #
def read_config(kb_root: str) -> Dict[str, object]:
    p = os.path.join(state_dir(kb_root), "config.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_config(kb_root: str, cfg: Dict[str, object]) -> None:
    ensure_dir(state_dir(kb_root))
    p = os.path.join(state_dir(kb_root), "config.json")
    atomic_write(p, json.dumps(cfg, indent=2, sort_keys=True) + "\n")
