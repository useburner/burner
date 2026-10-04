#!/usr/bin/env python3
"""u2mux - persistent uiautomator2 connection multiplexer.

Holds ONE uiautomator2 device connection (amortizing the ~2s connect +
healthcheck) and serves fast UI reads/writes over a Unix socket:

    u2mux.py daemon            # start (also done automatically by u2ctl)
    u2ctl dump [fresh]         # hierarchy XML (cached <2s unless "fresh")
    u2ctl exists "Send"        # 1/0
    u2ctl set_text '{"text": "..."}'
    u2ctl click_text "Login"   # {"clicked":true} or {"x","y",...} for adb tap
    u2ctl wait_for '{"text": "Send", "timeout": 30, "absent": false}'
    u2ctl invalidate           # screen changed; drop the dump cache

Speed stack: one persistent keep-alive HTTP stream to the device (kept
warm), a 2s dump cache invalidated on mutations, thread-per-connection
with one RLock around the device.

Protocol: one line command, first line of reply is "ok <nbytes>" or "err <msg>",
followed by <nbytes> of payload for ok.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time

# Paths derive from this file's real location (lib/u2/u2mux.py), so a copy
# of the tree runs its own daemon. BURNER_WORKSPACE overrides where the shared
# venv/adb live (see WORKSPACE below).
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
# Tools (adb, older venvs) live in BURNER_WORKSPACE if set, else in this
# tree (where install.sh puts them), else in the tree's parent (older layout).
WORKSPACE = os.environ.get("BURNER_WORKSPACE") or (
    ROOT if os.path.isdir(os.path.join(ROOT, ".android-tools"))
    else os.path.dirname(ROOT))
# install.sh makes ROOT/.venv.
VENV_PY = next((p for p in (
    os.path.join(ROOT, ".venv", "bin", "python"),)
    if os.path.exists(p)), sys.executable)
SOCK_PATH = os.path.join(ROOT, "run", "u2-mux.sock")
PID_PATH = os.path.join(ROOT, "run", "u2-mux.pid")
TARGET = "127.0.0.1:15555"


def log(*a):
    print("[u2mux]", *a, file=sys.stderr, flush=True)


class U2NotFound(Exception):
    """Expected miss — no such element. Not a transport failure."""
    pass


import contextlib
import time as _time

TRACE_FILE = os.path.join(ROOT, "run", "trace.jsonl")
TRACE_WINDOW_S = 60


@contextlib.contextmanager
def _t(label):
    t0 = _time.monotonic()
    try:
        yield
    finally:
        ms = (_time.monotonic() - t0) * 1000
        log("%-24s %6.0fms" % (label, ms))
        # `BURNER_TRACE=json burner ...` touches the trace file as it starts;
        # for a minute after that, RPC spans go there too, so
        # tools/tracesum.py can place them inside the command that asked
        # for them. Untraced use later doesn't keep growing the file.
        try:
            tracing = _time.time() - os.path.getmtime(TRACE_FILE) < TRACE_WINDOW_S
        except OSError:
            tracing = False
        if tracing:
            try:
                with open(TRACE_FILE, "a") as f:
                    f.write(json.dumps({"src": "u2mux", "step": label,
                                        "ms": round(ms, 1),
                                        "t": round(_time.time(), 3)}) + "\n")
            except OSError:
                pass


class KeepAliveHTTP:
    """Persistent HTTP/1.1 connection to the on-device u2 server.

    Stock uiautomator2 (3.7) opens a fresh adb `tcp:9008` stream for every
    RPC and closes it after — ~150-270ms of setup per call over Tailscale.
    This keeps one stream open and reuses it. NanoHTTPD drops idle
    connections after ~5s, so we reopen proactively past IDLE_S and retry
    once if a *reused* connection turns out to have been closed by the
    server (broken pipe on send / closed with no response — the idle-close
    signature, same rule urllib3 uses). Resets mid-response are NOT retried,
    so a processed click can't be replayed."""
    IDLE_S = 4.0
    # Keep-warm: for WARM_S after the last real request, swap in a freshly
    # opened stream whenever the current one is REFRESH_S old, so the next
    # command never pays the reopen (~150-400ms). The new stream is opened
    # outside the lock, so this never delays a real request.
    WARM_S = 60.0
    REFRESH_S = 3.0

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._conns = {}   # (serial, port) -> [conn, last_used]
        self._last_real = 0.0
        self._target = None  # (dev, port) of the most recent request
        self._warm_thread = None

    def start_warmer(self):
        import threading
        if self._warm_thread is None:
            self._warm_thread = threading.Thread(target=self._warm_loop, daemon=True)
            self._warm_thread.start()

    def _warm_loop(self):
        while True:
            _time.sleep(0.5)
            tgt = self._target
            now = _time.monotonic()
            if tgt is None or now - self._last_real > self.WARM_S:
                continue
            ent = self._conns.get((tgt[0].serial, tgt[1]))
            if ent is not None and now - ent[1] < self.REFRESH_S:
                continue
            try:
                fresh = self._open(*tgt)
            except Exception:
                continue
            key = (tgt[0].serial, tgt[1])
            if not self._lock.acquire(blocking=False):
                fresh.close()  # a real request is running; it keeps the conn warm
                continue
            try:
                old = self._conns.get(key)
                if old is not None:
                    old[0].close()
                self._conns[key] = [fresh, _time.monotonic()]
            finally:
                self._lock.release()

    def _open(self, dev, port):
        from uiautomator2.core import AdbHTTPConnection
        c = AdbHTTPConnection(dev, port=port)
        c.connect()
        return c

    def close(self):
        with self._lock:
            for c, _ in self._conns.values():
                c.close()
            self._conns.clear()

    def request(self, dev, port, method, path, data=None, timeout=10.0,
                print_request=False):
        import http.client
        from uiautomator2.core import HTTPResponse, HTTPError
        # The on-phone server gzips its JSON when asked: a screen read's
        # 50-150KB crosses the link as 5-15KB (it matters on a slow link).
        # Not for a screenshot: its base64 JPEG barely shrinks, and the
        # phone pays for compressing it (shot went from 0.37s to 0.6s).
        shot = isinstance(data, dict) and data.get("method") == "takeScreenshot"
        headers = {'User-Agent': 'uiautomator2',
                   'Accept-Encoding': '' if shot else 'gzip',
                   'Content-Type': 'application/json'}
        body = json.dumps(data) if data else None
        key = (dev.serial, port)
        self._target = (dev, port)
        self._last_real = _time.monotonic()
        with self._lock:
            for attempt in range(2):
                ent = self._conns.get(key)
                reused = ent is not None and _time.monotonic() - ent[1] < self.IDLE_S
                if not reused:
                    if ent:
                        ent[0].close()
                    ent = self._conns[key] = [self._open(dev, port), 0]
                conn = ent[0]
                conn.sock.settimeout(timeout)
                try:
                    conn.request(method, path, body, headers=headers)
                    resp = conn.getresponse()
                    content = resp.read()
                except (http.client.RemoteDisconnected, BrokenPipeError,
                        http.client.CannotSendRequest) as e:
                    conn.close()
                    del self._conns[key]
                    if reused and attempt == 0:
                        log("keepalive conn went stale (%s), reopening" % type(e).__name__)
                        continue
                    raise
                except Exception:
                    conn.close()
                    del self._conns[key]
                    raise
                if resp.will_close:
                    conn.close()
                    del self._conns[key]
                else:
                    ent[1] = _time.monotonic()
                if resp.status != 200:
                    raise HTTPError("HTTP request failed: %s %s" % (resp.status, resp.reason))
                if (resp.getheader("Content-Encoding") or "").lower() == "gzip":
                    import gzip
                    import zlib
                    try:
                        content = gzip.decompress(content)
                    except (OSError, EOFError, zlib.error) as e:
                        log("gzip reply couldn't be decoded (%s); using it as is" % e)
                return HTTPResponse(content)


_KEEPALIVE = KeepAliveHTTP()


def install_keepalive():
    """Route every uiautomator2 RPC through the persistent connection.
    u2.core._jsonrpc_call looks up _http_request as a module global, so
    patching it covers d.info, dump_hierarchy, click, UiObject calls, etc."""
    import uiautomator2.core as core
    core._http_request = _KEEPALIVE.request
    _KEEPALIVE.start_warmer()


def ensure_server():
    """Make sure the on-device uiautomator2 server is listening."""
    import uiautomator2 as u2
    try:
        d = u2.connect(TARGET)
        d.info  # cheap deviceInfo RPC — raises if server is dead
        return
    except Exception as e:
        log("server not responding (%s), (re)initing" % e)
        # The old server process can linger half-dead (this is the
        # "ApplicationSharedMemory not initialized" error). Stop it before
        # pushing a new one so the restart is clean and happens once.
        try:
            u2.connect(TARGET).stop_uiautomator()
        except Exception as e2:
            log("stop_uiautomator failed (%s)" % e2)
    # Pass the serial: with one, init only pushes u2.jar. Without it, init
    # also installs the ATX keyboard app, which Android 14+ blocks as an
    # "unsafe app" (it targets an old Android). burner doesn't need it.
    r = subprocess.run(
        [VENV_PY, "-m", "uiautomator2", "init", "--serial", TARGET],
        capture_output=True, text=True, timeout=120)
    d = u2.connect(TARGET)
    d.info
    log("server (re)started")


DUMP_TTL = 2.0  # seconds a cached hierarchy dump stays valid


# Commands that change the screen: never replayed after an error.
NO_RETRY = {"tap", "click_text", "set_text"}


class U2Daemon:
    def __init__(self):
        import threading
        # One re-entrant lock serializes every self.d call (and reconnects).
        self._lock = threading.RLock()
        # Cache state has its own lock so `invalidate` never waits behind
        # an in-flight dump holding self._lock.
        self._cache_lock = threading.Lock()
        self._cache = None   # (monotonic ts at RPC start, xml)
        self._gen = 0        # bumped on invalidate; stale in-flight dumps aren't cached
        self._last_xml = ""  # the newest read, however old: a hint for `screen`
        self.d = None
        self.connect()

    def connect(self):
        import uiautomator2 as u2
        with self._lock:
            self.invalidate()
            install_keepalive()
            _KEEPALIVE.close()  # drop streams to a possibly-dead server
            ensure_server()
            self.d = u2.connect(TARGET)
            # Kill the idle wait: Chrome never idles, so every UiObject call
            # was paying ~1s waitForIdle(). Zero it; burner does its own polling.
            # Re-applied on every connect since the server loses it on restart.
            try:
                cfg = self.d.jsonrpc.getConfigurator()
                cfg.update({"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0})
                self.d.jsonrpc.setConfigurator(cfg)
                log("fast config applied:", self.d.jsonrpc.getConfigurator())
            except Exception as e:
                log("configurator tweak failed:", e)
            # warm up: one dump so later calls are fast
            self.d.dump_hierarchy()
            log("connected to", TARGET)

    def invalidate(self):
        """Screen may have changed (tap/type/key) — drop the cached dump."""
        with self._cache_lock:
            self._gen += 1
            self._cache = None

    def _fresh_cache(self):
        """Cached XML if younger than DUMP_TTL, else None."""
        c = self._cache
        if c is not None and _time.monotonic() - c[0] < DUMP_TTL:
            return c[1]
        return None

    def _dump(self, fresh=False):
        """Hierarchy XML; served from cache if < DUMP_TTL old unless fresh."""
        xml = None if fresh else self._fresh_cache()
        if xml is not None:
            log("dump cache hit")
            return xml
        with self._lock:
            gen, t0 = self._gen, _time.monotonic()
            with _t("dump rpc"):
                xml = self.d.dump_hierarchy()
            with self._cache_lock:
                if gen == self._gen:
                    self._cache = (t0, xml)
            self._last_xml = xml
        return xml

    def _reconnect(self):
        try:
            self.d.info  # cheap RPC — raises if the server died
            return True
        except Exception:
            log("server check failed, reconnecting")
            try:
                self.connect()
                return True
            except Exception as e:
                log("reconnect failed:", e)
                return False

    def cmd_dump(self, arg):
        # "dump fresh" bypasses the cache.
        return self._dump(fresh=(arg.strip() == "fresh")).encode()

    def cmd_invalidate(self, _):
        self.invalidate()
        return b""

    def cmd_exists(self, arg):
        # One dump, local substring check on text + content-desc.
        # Single roundtrip for both hit and miss (~1s); no 3.6s tail.
        # Match against XML-escaped form (&, <, " are escaped in dumps).
        import xml.sax.saxutils as saxutils
        h = self._dump()
        esc = saxutils.escape(arg, {'"': '&quot;'})
        return b"1" if esc in h else b"0"

    def cmd_find(self, arg):
        """arg: JSON selector like {"text": "Send"} -> JSON list of bounds."""
        sel = json.loads(arg)
        out = []
        with self._lock:
            for e in self.d(**sel):
                try:
                    i = e.info
                    b = i.get("bounds", {})
                    out.append({"text": i.get("text", ""), "desc": i.get("contentDescription", ""),
                                "bounds": [b.get("left"), b.get("top"), b.get("right"), b.get("bottom")],
                                "clickable": i.get("clickable"), "enabled": i.get("enabled")})
                except Exception:
                    pass
        return json.dumps(out).encode()

    def cmd_click_text(self, arg):
        # Three tiers, cheapest first (median over Tailscale, 2026-09-30):
        #  1. fresh cached dump -> return coords; burner taps via adb (~0.45s total)
        #  2. stale cache -> ONE device-side click(selector) RPC on the exact
        #     (case-insensitive) text (~0.43s). Beats dump+adb (~1.3s) cold.
        #  3. miss -> dump, match exact text/desc then substring, return
        #     coords for adb. Rotation-safe: bounds and taps are both in
        #     current-rotation screen coords.
        # Returns JSON {"clicked": true} (tier 2) or {"x","y",...} (1/3).
        # Invalidates the cache (the screen is about to change). U2NotFound on miss.
        if self._fresh_cache() is None:
            from uiautomator2._selector import Selector
            from uiautomator2.exceptions import UiObjectNotFoundError
            pat = "(?is)\\Q" + arg.replace("\\E", "\\E\\\\E\\Q") + "\\E"
            with self._lock:
                try:
                    with _t("click_text selector"):
                        ok = self.d.jsonrpc.click(Selector(textMatches=pat))
                except UiObjectNotFoundError:
                    ok = False
                if ok:
                    self.invalidate()
                    return json.dumps({"clicked": True, "text": arg}).encode()
        n = find_node(self._dump(), arg)
        if n is None:
            raise U2NotFound("no such text: %r" % arg)
        self.invalidate()
        x, y = n["center"]
        return json.dumps({"x": x, "y": y, "text": n["text"], "desc": n["desc"],
                           "bounds": n["bounds"]}).encode()

    def cmd_wait_for(self, arg):
        """arg: JSON {"text", "timeout" (s), "absent" (bool)}. Polls in the
        daemon every POLL_S, taking self._lock only for each dump RPC so
        other clients interleave. Match = exact label (text/content-desc,
        case-insensitive), else substring. Returns JSON with the matched
        node (or {"gone": true}); U2NotFound on timeout. The last dump stays
        cached, so an immediate `burner tap` needs no further lookup RPC."""
        POLL_S = 0.25
        p = json.loads(arg)
        text, absent = p["text"], bool(p.get("absent"))
        t0 = _time.monotonic()
        deadline = t0 + float(p.get("timeout", 30))
        polls, healed, fresh = 0, False, False
        while True:
            polls += 1
            try:
                xml = self._dump(fresh=fresh)  # first poll may use a fresh cache
            except Exception as e:
                # transport hiccup mid-wait: reconnect once, keep polling
                if healed:
                    raise
                healed = True
                log("wait_for poll failed (%s), reconnecting" % e)
                with self._lock:
                    self.connect()
                continue
            fresh = True
            n = find_node(xml, text)
            waited = int((_time.monotonic() - t0) * 1000)
            if absent and n is None:
                return json.dumps({"gone": True, "waited_ms": waited, "polls": polls}).encode()
            if not absent and n is not None:
                n = dict(n, found=True, waited_ms=waited, polls=polls)
                return json.dumps(n).encode()
            if _time.monotonic() + POLL_S > deadline:
                raise U2NotFound("timeout waiting for %r" % text)
            _time.sleep(POLL_S)

    def cmd_tap(self, arg):
        """arg: "x y". One click RPC on the open stream instead of spawning
        `adb shell input tap`. Coordinates are current-rotation screen
        pixels, the same space as dump bounds."""
        x, y = (int(v) for v in arg.split())
        with self._lock:
            with _t("tap rpc"):
                self.d.jsonrpc.click(x, y)
        self.invalidate()
        return b""

    def cmd_idle(self, arg):
        """arg: timeout in ms. Waits on the phone until the UI has had no
        accessibility events for about 0.5s (or the timeout), then drops the
        dump cache. One RPC instead of re-reading the screen over and over
        to see whether it stopped changing. Returns the ms it waited."""
        timeout = int(arg.strip() or "1500")
        t0 = _time.monotonic()
        with self._lock:
            with _t("idle rpc"):
                self.d.jsonrpc.waitForIdle(timeout)
        self.invalidate()
        return str(int((_time.monotonic() - t0) * 1000)).encode()

    IME_HINTS = ("inputmethod", "keyboard", "honeyboard", "swiftkey", ".ime.", "latinime")

    def cmd_screen(self, _):
        """"w h pkg [kbd]": the screen at its current rotation and the app
        in front, from one small deviceInfo RPC (a full dump costs ~0.8s),
        plus "kbd" when the newest read showed a keyboard (a swipe must
        then start above it: on the keys, Gboard glide-types)."""
        with self._lock:
            i = self.d.jsonrpc.deviceInfo()
        out = "{} {} {}".format(i["displayWidth"], i["displayHeight"],
                                i.get("currentPackageName") or "").strip()
        if keyboard_in(self._last_xml):
            out += " kbd"
        return out.encode()

    def cmd_shot(self, arg):
        """Screenshot as base64. One RPC returning a JPEG (a few hundred KB)
        instead of a 1.5MB PNG over adb. arg "png" re-encodes it as PNG.
        The JPEG is 70% size: vision models shrink a 2400px-tall image to
        about 1568px anyway, so full size only costs transfer time."""
        import base64
        png = arg.strip() == "png"
        # 0.6 scale, quality 75: about half the bytes of 0.7/80, and the
        # vision models that read it shrink it further anyway.
        with self._lock:
            with _t("shot rpc"):
                data = self.d.jsonrpc.takeScreenshot(1 if png else 0.6,
                                                     100 if png else 75)
        if not data:
            raise RuntimeError("takeScreenshot returned nothing")
        if png:
            import io
            from PIL import Image
            buf = io.BytesIO()
            Image.open(io.BytesIO(base64.b64decode(data))).save(buf, "PNG")
            data = base64.b64encode(buf.getvalue()).decode()
        return data.encode()

    def cmd_set_text(self, arg):
        """arg: JSON {"text": "...", "selector": {...} (optional)}.
        Atomic ACTION_SET_TEXT on the focused EditText (or selector match)."""
        p = json.loads(arg) if arg.strip().startswith("{") else {"text": arg}
        with self._lock:
            el = None
            if "selector" in p:
                el = self.d(**p["selector"])
                if not el.exists:
                    el = None
            if el is None:
                # The focused EditText; with none focused, only a screen
                # with exactly one field is unambiguous (a login form).
                # Never "the first one" (it was Chrome's address bar once).
                el = self.d(focused=True, className="android.widget.EditText")
                if not el.exists:
                    fields = self.d(className="android.widget.EditText", enabled=True)
                    if fields.count != 1:
                        raise RuntimeError("no editable field found (no field has "
                                           "the focus)")
                    el = fields
            if not el.exists:
                raise RuntimeError("no editable field found")
            el.set_text(p["text"])
            self.invalidate()
        return b""

    def cmd_health(self, _):
        with self._lock:
            return b"alive" if self._reconnect() else b"dead"

    def handle(self, line):
        parts = line.strip().split(" ", 1)
        cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        fn = getattr(self, "cmd_" + cmd, None)
        if not fn:
            raise RuntimeError("unknown command: %s" % cmd)
        try:
            return fn(arg)
        except U2NotFound:
            # Expected miss (no such element) — not a transport failure.
            # Return a sentinel; the caller decides what it means.
            return b"__NOT_FOUND__"
        except Exception as e:
            if cmd in NO_RETRY:
                # The phone may already have acted (the reply was lost, not
                # the request). Replaying would tap or type twice.
                raise
            # Maybe the on-device server died — reconnect once and retry.
            # The RLock makes concurrent handlers queue behind one reconnect.
            with self._lock:
                try:
                    self.d.info
                except Exception:
                    log("command %s failed (%s), reconnecting" % (cmd, e))
                    self.connect()
            try:
                return fn(arg)
            except U2NotFound:
                return b"__NOT_FOUND__"


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def iter_nodes(xml):
    """Yield {text, desc, bounds, center, enabled, clickable} for every
    node with non-empty on-screen bounds, in document order."""
    import xml.etree.ElementTree as ET
    for n in ET.fromstring(xml).iter("node"):
        m = _BOUNDS_RE.match(n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        yield {"text": n.get("text") or "", "desc": n.get("content-desc") or "",
               "bounds": n.get("bounds"), "center": [(x1 + x2) // 2, (y1 + y2) // 2],
               "enabled": n.get("enabled") != "false",
               "clickable": n.get("clickable") == "true"}


def keyboard_in(xml):
    """True if a dump holds a keyboard app's window. Pure."""
    for part in (xml or "").lower().split('package="')[1:]:
        pkg = part.split('"', 1)[0]
        if any(h in pkg for h in U2Daemon.IME_HINTS):
            return True
    return False


def find_node(xml, needle, fuzzy=True):
    """First node whose text or content-desc matches needle: exact
    (case-insensitive) match wins, then substring if fuzzy. "A || B"
    matches any of the labels (the first found, in that order). None on
    miss."""
    needles = [p.strip().lower() for p in needle.split("||") if p.strip()]
    first_sub = None
    nodes = list(iter_nodes(xml))
    for nl in needles:
        for n in nodes:
            labels = (n["text"].lower(), n["desc"].lower())
            if nl in labels:
                return n
            if fuzzy and first_sub is None and any(nl in l for l in labels):
                first_sub = n
        if first_sub is not None:
            return first_sub
    return None


def run_daemon():
    # A fresh install has no run/ folder yet; without it bind() fails and
    # the socket never appears.
    os.makedirs(os.path.dirname(SOCK_PATH), exist_ok=True)
    # single instance
    if os.path.exists(SOCK_PATH):
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(2)
            s.connect(SOCK_PATH)
            s.sendall(b"health\n")
            if s.recv(16).startswith(b"ok"):
                log("already running")
                return
        except Exception:
            pass
        os.unlink(SOCK_PATH)
    daemon = U2Daemon()
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(SOCK_PATH)
    srv.listen(16)
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))
    log("listening on", SOCK_PATH)
    import threading
    # Thread per connection: `invalidate`/cache hits answer instantly even
    # while another client's dump or wait_for is in flight. Every self.d
    # call is serialized by U2Daemon._lock.
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve_conn, args=(daemon, conn), daemon=True).start()


def serve_conn(daemon, conn):
    try:
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        try:
            payload = daemon.handle(data.decode())
            conn.sendall(b"ok %d\n" % len(payload) + payload)
        except Exception as e:
            msg = str(e).encode()
            conn.sendall(b"err %d\n" % len(msg) + msg)
    except OSError:
        pass  # client went away
    finally:
        conn.close()


def client(argv):
    """Send a command to the daemon, auto-starting it if needed."""
    def start_daemon():
        os.makedirs(os.path.dirname(SOCK_PATH), exist_ok=True)
        # Log to run/u2-mux.log so a daemon that dies on start can be seen.
        logf = open(os.path.join(os.path.dirname(SOCK_PATH), "u2-mux.log"), "a")
        subprocess.Popen([VENV_PY, os.path.abspath(__file__), "daemon"],
                         stdout=logf, stderr=logf, start_new_session=True)
        for _ in range(100):
            if os.path.exists(SOCK_PATH):
                break
            time.sleep(0.3)
    if not os.path.exists(SOCK_PATH):
        start_daemon()
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(60)
    try:
        s.connect(SOCK_PATH)
    except (ConnectionRefusedError, FileNotFoundError):
        # Stale socket file from a dead daemon — remove and respawn.
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass
        s.close()
        start_daemon()
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(60)
        s.connect(SOCK_PATH)
    s.sendall((" ".join(argv) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(64)
        if not chunk:
            raise ConnectionError("u2 mux closed the connection")
        buf += chunk
    head, rest = buf.split(b"\n", 1)
    status, n = head.decode().strip().split(" ")
    n = int(n)
    payload = rest
    while len(payload) < n:
        chunk = s.recv(min(65536, n - len(payload)))
        if not chunk:
            raise ConnectionError("u2 mux closed mid-payload")
        payload += chunk
    if status != "ok":
        print("u2 error: " + payload.decode(), file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(payload.decode())
    sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "daemon":
        run_daemon()
    else:
        client(sys.argv[1:])
