"""Minimal Chrome DevTools Protocol client over --remote-debugging-pipe.

Stdlib only. No websocket dependency, no Playwright. Chrome's pipe transport
exchanges NUL-delimited JSON over two extra file descriptors:

    fd 3 : we WRITE CDP commands here (Chrome reads)
    fd 4 : we READ  CDP events/responses here (Chrome writes)

We launch Chrome with ``--remote-debugging-pipe`` and ``pass_fds=(3, 4)``,
wiring our pipe ends to fds 3 and 4 in the child. A reader thread drains fd 4,
demultiplexing responses (matched by ``id``) from events (have a ``method``).

Why the pipe and not the HTTP/ws endpoint: Chrome 136+ silently ignores
``--remote-debugging-port`` on a normal profile, and the pipe needs no port, no
websocket handshake, and no third-party library. It is the robust shipped path.

Public surface:
    c = CDP(); c.launch(chrome_path, user_data_dir, extra_args=[...])
    c.navigate(url)                 # navigates the page target and waits for load
    c.evaluate(expr)                # Runtime.evaluate, awaitPromise + returnByValue
    c.send(method, params)          # raw command -> result dict
    c.close()

A page target is attached on launch so evaluate/navigate run against the tab.
Works headless. Run ``python3 cdp.py`` for a self-check (headless about:blank,
evaluate 1+1).
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional


class CDPError(RuntimeError):
    pass


# Candidate Chrome/Chromium binaries to try when none is supplied.
_CHROME_CANDIDATES = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "chrome",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/opt/google/chrome/chrome",
]


def find_chrome(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve a Chrome/Chromium executable path, or None."""
    if explicit:
        if os.path.isabs(explicit) and os.access(explicit, os.X_OK):
            return explicit
        found = shutil.which(explicit)
        if found:
            return found
    for cand in _CHROME_CANDIDATES:
        if os.path.isabs(cand):
            if os.access(cand, os.X_OK):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


class CDP:
    """A single-connection CDP client driving one Chrome instance + page."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._write_fd: Optional[int] = None   # parent end we write commands to
        self._read_fd: Optional[int] = None    # parent end we read replies from
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._cond = threading.Condition(self._pending_lock)
        self._events: List[Dict[str, Any]] = []
        self._event_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._closed = False
        self._session_id: Optional[str] = None  # flat-session id for the page

    # ----- lifecycle ---------------------------------------------------- #
    def launch(self, chrome_path: Optional[str] = None,
               user_data_dir: Optional[str] = None,
               extra_args: Optional[List[str]] = None,
               headless: bool = True,
               timeout: float = 30.0) -> None:
        """Launch Chrome with the remote-debugging pipe and attach a page."""
        binary = find_chrome(chrome_path)
        if not binary:
            raise CDPError("no Chrome/Chromium binary found")

        # Pipe A: parent writes commands -> child fd 3 reads.
        a_r, a_w = os.pipe()   # a_r given to child as fd 3; a_w kept by parent
        # Pipe B: child fd 4 writes -> parent reads.
        b_r, b_w = os.pipe()   # b_w given to child as fd 4; b_r kept by parent

        args = [
            binary,
            "--remote-debugging-pipe",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-gpu",
            "--no-sandbox",
        ]
        if headless:
            # New headless mode; falls back gracefully on older builds.
            args.append("--headless=new")
        if user_data_dir:
            args.append("--user-data-dir=" + user_data_dir)
        if extra_args:
            args.extend(extra_args)
        # A start URL keeps a page target alive immediately.
        args.append("about:blank")

        def _preexec_remap():
            # In the child: make a_r -> fd 3, b_w -> fd 4.
            os.dup2(a_r, 3)
            os.dup2(b_w, 4)

        self._proc = subprocess.Popen(
            args,
            close_fds=True,
            pass_fds=(3, 4),
            preexec_fn=_preexec_remap,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Parent keeps a_w (write) and b_r (read); close the child ends.
        os.close(a_r)
        os.close(b_w)
        self._write_fd = a_w
        self._read_fd = b_r

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        self._attach_page(timeout=timeout)

    def _attach_page(self, timeout: float) -> None:
        """Find the page target and attach a flat session to it."""
        deadline = time.time() + timeout
        target_id = None
        while time.time() < deadline:
            try:
                res = self.send("Target.getTargets", {}, timeout=5.0)
            except CDPError:
                time.sleep(0.1)
                continue
            for info in res.get("targetInfos", []):
                if info.get("type") == "page":
                    target_id = info.get("targetId")
                    break
            if target_id:
                break
            time.sleep(0.1)
        if not target_id:
            raise CDPError("no page target appeared")

        res = self.send("Target.attachToTarget",
                        {"targetId": target_id, "flatten": True},
                        timeout=10.0)
        self._session_id = res.get("sessionId")
        if not self._session_id:
            raise CDPError("attachToTarget returned no sessionId")
        # Enable the domains we use.
        self.send("Page.enable", {}, session=True)
        self.send("Runtime.enable", {}, session=True)

    # ----- low-level IO ------------------------------------------------- #
    def _read_loop(self) -> None:
        buf = b""
        while not self._closed:
            try:
                r, _, _ = select.select([self._read_fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if not r:
                if self._proc and self._proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(self._read_fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\x00" in buf:
                msg, buf = buf.split(b"\x00", 1)
                if not msg:
                    continue
                try:
                    obj = json.loads(msg.decode("utf-8"))
                except ValueError:
                    continue
                self._dispatch(obj)

    def _dispatch(self, obj: Dict[str, Any]) -> None:
        mid = obj.get("id")
        if mid is not None:
            with self._cond:
                self._pending[mid] = obj
                self._cond.notify_all()
        else:
            with self._event_lock:
                self._events.append(obj)

    def _write(self, obj: Dict[str, Any]) -> None:
        if self._write_fd is None:
            raise CDPError("not launched")
        data = json.dumps(obj).encode("utf-8") + b"\x00"
        try:
            os.write(self._write_fd, data)
        except OSError as e:
            raise CDPError("write to pipe failed: {}".format(e))

    def send(self, method: str, params: Optional[Dict[str, Any]] = None,
             session: bool = False, timeout: float = 30.0) -> Dict[str, Any]:
        """Send a CDP command and block for its result. If ``session`` is True
        the command is routed to the attached page session."""
        with self._id_lock:
            self._next_id += 1
            mid = self._next_id
        msg: Dict[str, Any] = {"id": mid, "method": method,
                               "params": params or {}}
        if session and self._session_id:
            msg["sessionId"] = self._session_id
        self._write(msg)

        deadline = time.time() + timeout
        with self._cond:
            while mid not in self._pending:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise CDPError("timeout waiting for {}".format(method))
                self._cond.wait(timeout=min(remaining, 1.0))
                if self._proc and self._proc.poll() is not None \
                        and mid not in self._pending:
                    raise CDPError("chrome exited before replying to "
                                   + method)
            reply = self._pending.pop(mid)

        if "error" in reply:
            err = reply["error"]
            raise CDPError("{}: {}".format(method, err.get("message", err)))
        return reply.get("result", {})

    # ----- high-level helpers ------------------------------------------ #
    def evaluate(self, expression: str, await_promise: bool = True,
                 timeout: float = 30.0) -> Any:
        """Runtime.evaluate with awaitPromise + returnByValue. Returns the
        decoded JS value (objects come back as plain dict/list)."""
        res = self.send("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
            "userGesture": True,
        }, session=True, timeout=timeout)
        if res.get("exceptionDetails"):
            ex = res["exceptionDetails"]
            desc = ex.get("exception", {}).get("description") \
                or ex.get("text", "evaluate exception")
            raise CDPError("Runtime.evaluate: {}".format(desc))
        return res.get("result", {}).get("value")

    def navigate(self, url: str, timeout: float = 45.0) -> None:
        """Navigate the page and wait for the load event (best-effort)."""
        # Clear stale load events.
        with self._event_lock:
            self._events = [e for e in self._events
                            if e.get("method") != "Page.loadEventFired"]
        self.send("Page.navigate", {"url": url}, session=True, timeout=timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._event_lock:
                for e in self._events:
                    if e.get("method") == "Page.loadEventFired":
                        return
            time.sleep(0.05)
        # Load event can be missed for some pages; not fatal.

    def wait_for_event(self, method: str, timeout: float = 30.0) \
            -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._event_lock:
                for e in self._events:
                    if e.get("method") == method:
                        return e
            time.sleep(0.05)
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc and self._proc.poll() is None:
                try:
                    self.send("Browser.close", {}, timeout=3.0)
                except CDPError:
                    pass
        except Exception:
            pass
        for fd in (self._write_fd, self._read_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._write_fd = None
        self._read_fd = None
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=2)

    def __enter__(self) -> "CDP":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _self_check() -> int:
    """Launch headless Chrome on about:blank and evaluate 1+1 == 2."""
    binary = find_chrome()
    if not binary:
        print("SELF-CHECK SKIP: no Chrome/Chromium binary found on PATH")
        return 0
    import tempfile
    udd = tempfile.mkdtemp(prefix="cdp-selfcheck-")
    c = CDP()
    try:
        c.launch(chrome_path=binary, user_data_dir=udd, headless=True)
        val = c.evaluate("1 + 1")
        ok = (val == 2)
        print("SELF-CHECK {}: 1+1 -> {!r} (chrome={})".format(
            "OK" if ok else "FAIL", val, binary))
        # also exercise navigate + DOM eval
        c.navigate("about:blank")
        title = c.evaluate("document.title")
        print("  navigate ok, document.title -> {!r}".format(title))
        return 0 if ok else 1
    except CDPError as e:
        print("SELF-CHECK ERROR:", e)
        return 1
    finally:
        c.close()
        shutil.rmtree(udd, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(_self_check())
