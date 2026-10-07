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
import http.client
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
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


LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__)))), "run", "u2mux.log")
LOG_CAP = 1 << 20  # the log starts over past this size


def own_log(path, mode="a"):
    """`path` opened to write, readable by its owner alone: it holds what
    the screens said (review, Oct 7). An older file is made so too."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC), 0o600)
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass
    return os.fdopen(fd, mode, encoding="utf-8")


def log(*a):
    """A line to stderr and to run/u2mux.log (the helper's stderr goes
    nowhere once it runs in the background; the file says what it did,
    with the time of day)."""
    line = " ".join(str(x) for x in a)
    print("[u2mux]", line, file=sys.stderr, flush=True)
    try:
        mode = "a"
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_CAP:
            mode = "w"
        with own_log(LOG_FILE, mode) as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), line))
    except OSError:
        pass


class U2NotFound(Exception):
    """Expected miss — no such element. Not a transport failure."""
    pass


class _NotThere(Exception):
    """A tap by words found no control on the phone: nothing was tapped."""


try:
    from uiautomator2.exceptions import HTTPError as _U2HTTPError
except ImportError:  # the offline tests: no uiautomator2 here
    _U2HTTPError = ConnectionError


class StreamUnavailable(*((_U2HTTPError, ConnectionError) if _U2HTTPError is not ConnectionError
                          else (ConnectionError,))):
    """No stream to the phone's UI server could be opened: nothing was
    sent on it. An HTTPError and a ConnectionError too, which is what
    uiautomator2 takes as "the server isn't up" and launches it on (an
    OSError alone left it unable to restart the server, Oct 5, 03:20)."""


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


def phone_config():
    """config.env next to bin/burner, as a dict (what tunnel.sh reads):
    PHONE_TAILSCALE_IP names the phone on the tailnet."""
    cfg = {}
    path = os.path.join(os.path.dirname(LOG_FILE), "..", "config.env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return cfg


def tailnet_proxy():
    """(host, auth) of the HTTP CONNECT proxy a hosted assistant reaches
    the tailnet through (HTTPS_PROXY, port 3130, exactly as tunnel.sh),
    or None when this computer is on the tailnet itself."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return None
    rest = proxy.split("://", 1)[-1].rstrip("/")
    auth, host = rest.rsplit("@", 1) if "@" in rest else ("", rest)
    return host.split(":")[0], auth


def connect_direct(ip, port, timeout):
    """A socket to ip:port on the tailnet: direct, or through the HTTP
    CONNECT proxy (see tailnet_proxy), with the handshake tools/direct.py
    used (http.client's tunnel was closed by the proxy without a reply,
    Oct 5). Raises OSError when the proxy or the phone refuses."""
    import base64
    proxy = tailnet_proxy()
    if proxy is None:
        return socket.create_connection((ip, int(port)), timeout)
    host, auth = proxy
    s = socket.create_connection((host, 3130), timeout)
    try:
        req = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (ip, int(port), ip, int(port))
        if auth:
            req += "Proxy-Authorization: Basic %s\r\n" % base64.b64encode(auth.encode()).decode()
        s.sendall((req + "\r\n").encode())
        s.settimeout(timeout)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("the proxy closed the connection")
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 200" not in status:
            raise ConnectionRefusedError("proxy: " + status[:80])
    except BaseException:
        s.close()
        raise
    return s


class DirectHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection to the phone's UI server over the tailnet: its
    socket comes from connect_direct."""

    def connect(self):
        self.sock = connect_direct(self.host, self.port, self.timeout)


def direct_http(ip, port, timeout):
    """An HTTP connection to ip:port on the tailnet (see connect_direct).
    Not connected yet."""
    return DirectHTTPConnection(ip, int(port), timeout=timeout)


STREAM_OPEN_S = 6.0  # opening a stream to the server through adb: 0.15-0.4s when the link is up
PROBE_STREAM_S = 3.0  # the same, as a check of the link while an answer is awaited


class LinkDead(Exception):
    """adb didn't answer a stream open: the link to the phone is dead,
    whatever adb's table says. Not an HTTPError: uiautomator2 takes one
    of those as "the server isn't up" and launches a server, over shells
    with no bound, on a link that answers nothing. `sent`: a request had
    gone out on the link before it was found dead (it may have acted)."""

    def __init__(self, msg, sent=False):
        super().__init__(msg)
        self.sent = sent


class ActNotSent(RuntimeError):
    """"act not sent: ...": the action never went out because the phone's
    server couldn't be reached or the read before it failed. Safe to send
    again once the link is back (see U2Daemon.handle)."""


def open_stream(dev, port, timeout=STREAM_OPEN_S):
    """An HTTP connection to the server's port through adb's streams,
    opened within `timeout`. adbutils gives the open 600s; a link through
    the tunnel that died silently while idle (no close from the far end,
    after hours; Oct 6) never answers it, and adb only dropped the
    transport after 75s: every command hung on it, and three in a row
    ran into the CLI's 60s watchdog."""
    from uiautomator2.core import AdbHTTPConnection
    conn = AdbHTTPConnection(dev, port=port)
    t = dev.open_transport(timeout=timeout)
    try:
        t.send_command("tcp:%d" % int(port))
        t.check_okay()
    except Exception as e:
        t.close()
        if isinstance(e, (socket.timeout, TimeoutError)) or type(e).__name__ == "AdbTimeout":
            raise LinkDead("no answer from the phone's adb in %.0fs (stream open)" % timeout)
        raise
    t.conn.settimeout(None)
    conn.sock = t.conn
    conn.via_adb = True  # its link is checked while an answer is awaited (see _wait_answer)
    return conn


def bounded_shell(dev, cmd, timeout):
    """The output of `cmd` in the phone's shell, the whole of it within
    `timeout`. adbutils bounds only the reading of a shell's output: the
    stream's open waits for the phone with its own 600s, and a link that
    passes nothing never answers it (see open_stream). Raises LinkDead
    when the phone doesn't answer in time."""
    c = dev.open_transport(timeout=timeout)
    try:
        c.send_command("shell:" + cmd)
        c.check_okay()
        out = c.read_until_close()
    except Exception as e:
        if isinstance(e, (socket.timeout, TimeoutError)) or type(e).__name__ == "AdbTimeout":
            raise LinkDead("no answer from the phone's adb in %.0fs (shell)" % timeout)
        raise
    finally:
        c.close()
    return out.rstrip() if isinstance(out, str) else out


DIRECT_RETRY_S = 60.0  # a direct route that failed twice is tried again after this


class KeepAliveHTTP:
    """Persistent HTTP/1.1 connection to the on-device u2 server.

    Straight over the tailnet when the phone's address is known and the
    server answers there (its port, through the proxy a hosted assistant
    reaches the tailnet with): a request then crosses the link once. A
    bare call through adb's streams took 0.4-0.75s from Muse's box (Oct
    5): each request goes as adb packets with their acknowledgements.
    A direct route that fails is left alone for DIRECT_RETRY_S, and
    adb's streams carry the requests, as before.

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
    # An answer awaited on an adb stream: no byte this long, and the link
    # is checked (a second stream opened); again this often after.
    ANSWER_PROBE_S = 3.0
    ANSWER_REPROBE_S = 5.0

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._conns = {}   # (serial, port) -> [conn, last_used]
        self._last_real = 0.0
        self._target = None  # (dev, port) of the most recent request
        self._warm_thread = None
        self._direct_retry_at = 0.0  # when a failed direct route is tried again
        self.direct = None  # True once the direct route answered, False after it failed

    def _direct_target(self, port):
        """(ip, port) of the server straight over the tailnet, or None
        when this computer reaches the tailnet through a proxy (its
        CONNECTs were refused one in two and the route flapped between
        direct and adb, eight times in two minutes, Oct 5, 04:11), when
        the phone's address isn't known, or when the route failed lately."""
        ip = phone_config().get("PHONE_TAILSCALE_IP", "")
        if (tailnet_proxy() is not None or not ip or ip.startswith("YOUR_")
                or _time.monotonic() < self._direct_retry_at):
            return None
        return ip, int(port)

    def _open_direct(self, ip, port):
        conn = direct_http(ip, port, 8.0)
        conn.connect()
        return conn

    def start_warmer(self):
        import threading
        if self._warm_thread is None:
            self._warm_thread = threading.Thread(target=self._warm_loop, daemon=True)
            self._warm_thread.start()

    def _warm_loop(self):
        while True:
            _time.sleep(0.5)
            try:
                self._warm_once()
            except Exception:
                pass

    def _ping(self, conn, timeout=4.0):
        """GET /ping on an open stream: True when the server answered and
        keeps the stream (its idle clock starts over)."""
        conn.sock.settimeout(timeout)
        conn.request("GET", "/ping", headers={"User-Agent": "uiautomator2", "Accept-Encoding": "",
                                              "Connection": "keep-alive"})
        resp = conn.getresponse()
        resp.read()
        return resp.status == 200 and not resp.will_close

    def _warm_once(self):
        """One round of keeping the stream warm (see the class): nothing
        to do, a ping on a direct stream, or a fresh stream swapped in."""
        tgt = self._target
        now = _time.monotonic()
        if tgt is None or now - self._last_real > self.WARM_S:
            return
        key = (tgt[0].serial, tgt[1])
        ent = self._conns.get(key)
        if ent is not None and now - ent[1] < self.REFRESH_S:
            return
        if ent is not None and self.direct:
            # a direct stream is kept with a ping on it: a fresh one costs
            # a CONNECT through the proxy, which refused one in two while
            # a stream was being swapped (Oct 5, 03:46)
            if not self._lock.acquire(blocking=False):
                return  # a real request is running; it keeps the stream warm
            try:
                ent = self._conns.get(key)
                if ent is not None and self._ping(ent[0]):
                    ent[1] = _time.monotonic()
                    return
            except Exception:
                pass
            finally:
                self._lock.release()
            # the ping failed: the stream is gone, a fresh one below
        try:
            fresh = self._open(*tgt)
        except Exception:
            return
        if not self._lock.acquire(blocking=False):
            fresh.close()  # a real request is running; it keeps the conn warm
            return
        try:
            old = self._conns.get(key)
            if old is not None:
                old[0].close()
            self._conns[key] = [fresh, _time.monotonic()]
        finally:
            self._lock.release()

    def _open(self, dev, port):
        direct = self._direct_target(port)
        if direct is not None:
            # twice: the proxy closed one CONNECT in two without a reply
            # while a stream was being replaced (Oct 5, 03:46), and the
            # next one went through
            for attempt in range(2):
                try:
                    c = self._open_direct(*direct)
                    if not self.direct:
                        log("the phone's UI server answers straight over the tailnet (%s:%d)" % direct)
                    self.direct = True
                    return c
                except (OSError, ValueError) as e:
                    if attempt == 0:
                        _time.sleep(0.3)
                        continue
                    self.direct = False
                    self._direct_retry_at = _time.monotonic() + DIRECT_RETRY_S
                    log("no direct route to the UI server (%s:%d: %s); through adb for %.0fs"
                        % (direct[0], direct[1], err_text(e, 80), DIRECT_RETRY_S))
        return open_stream(dev, port)

    def close(self):
        with self._lock:
            for c, _ in self._conns.values():
                c.close()
            self._conns.clear()

    def _wait_answer(self, conn, dev, port, timeout):
        """Wait for the first bytes of the answer to a request sent on an
        adb stream, checking the link while none come: a link through the
        tunnel that froze (adb's table says "device", nothing gets
        through) kept a read waiting its whole 25s before the link was
        looked at (Oct 6, a stopped tunnel leg: 34s for one `state`). The
        check opens a second stream to the server, PROBE_STREAM_S at
        most. Raises LinkDead (sent: the request went out) when the link
        doesn't answer, and the read's own timeout when no answer came in
        `timeout` (the read after it would have waited a second `timeout`:
        a hung server's 25s read took 50s); returns when the answer
        starts, or at once when the socket can't be watched (the read
        then keeps its own timeout). A link check that fails another way
        is logged, and the wait goes on without checks."""
        deadline = _time.monotonic() + timeout
        wait, checking = self.ANSWER_PROBE_S, True
        while True:
            left = deadline - _time.monotonic()
            if left <= 0:
                raise socket.timeout("no answer in %.0fs" % timeout)
            try:
                ready, _, _ = select.select([conn.sock], [], [], min(wait, left) if checking else left)
            except (OSError, ValueError, TypeError) as e:
                if not isinstance(e, TypeError):
                    log("the answer can't be watched (%s); the read waits for it" % err_text(e, 80))
                return
            if ready:
                return
            if not checking or deadline - _time.monotonic() <= 0:
                continue
            try:
                open_stream(dev, port, timeout=PROBE_STREAM_S).close()
            except LinkDead:
                log("no answer in %.0fs and the link answers nothing: it is dead"
                    % (timeout - (deadline - _time.monotonic())))
                conn.close()
                raise LinkDead("no answer from the phone's adb: the link died with the request out",
                               sent=True)
            except Exception as e:
                log("the link check failed (%s); waiting for the answer without it" % err_text(e, 80))
                checking = False
            wait = self.ANSWER_REPROBE_S

    def request(self, dev, port, method, path, data=None, timeout=10.0,
                print_request=False, replay=True):
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
        # replay: a request on a reused stream that turns out closed is
        # sent again on a fresh one (the idle-close signature) when it is
        # a read; never when it carries an action, since a server that
        # closed after reading it has acted (a tap twice).
        sent, stale = False, None
        with self._lock:
            for attempt in range(2):
                ent = self._conns.get(key)
                reused = ent is not None and _time.monotonic() - ent[1] < self.IDLE_S
                if not reused:
                    if ent:
                        ent[0].close()
                    try:
                        fresh = self._open(dev, port)
                    except LinkDead as e:
                        e.sent = e.sent or sent  # the request went out once: it may have acted
                        raise  # the link is dead: not "no stream" (see LinkDead)
                    except Exception as e:
                        if sent:
                            raise stale  # the request went out once: not "not sent"
                        raise StreamUnavailable(err_text(e, 100))
                    ent = self._conns[key] = [fresh, 0]
                conn = ent[0]
                conn.sock.settimeout(timeout)
                try:
                    conn.request(method, path, body, headers=headers)
                    sent = True
                    if getattr(conn, "via_adb", False):
                        self._wait_answer(conn, dev, port, timeout)
                    resp = conn.getresponse()
                    content = resp.read()
                except (http.client.RemoteDisconnected, BrokenPipeError,
                        http.client.CannotSendRequest) as e:
                    conn.close()
                    del self._conns[key]
                    if reused and attempt == 0 and replay:
                        log("keepalive conn went stale (%s), reopening" % type(e).__name__)
                        stale = e
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


DUMP_RPC_TIMEOUT = 25.0   # a screen read never takes this long; a hung server does
# The read's depth. Never None: the phone's server takes a null depth as
# 0 and answers with the window roots alone (two nodes, not one word;
# Oct 4, after an update, for hours). uiautomator2 itself sends 50.
DUMP_DEPTH = 50
PROBE_TIMEOUT = 12.0      # the server check's read


def read_screen(d, timeout=DUMP_RPC_TIMEOUT):
    """One screen read with a bound. (A server that still answers
    deviceInfo but hangs on reads is what a bad batch left behind on
    Oct 4; without a bound, every command sat on it.)"""
    return d.jsonrpc_call("dumpWindowHierarchy", [False, DUMP_DEPTH], timeout=timeout)


def real_screen(xml):
    """True for a read that holds at least one node. A half-started
    server answers with an empty hierarchy for every read (Oct 4: text
    taps kept missing until the server was restarted)."""
    return bool(xml) and "<hierarchy" in xml and "<node" in xml


SYSTEM_UI = "com.android.systemui"


def blank_screen(xml):
    """True for a read with no app on it: no node at all, or nothing but
    the system UI's windows with no word on them. An off screen reads
    like that: the shade's bare window holds the focus (Oct 4, a Pixel 7
    off its charger, whose screen turns off after 10s: label taps missed
    and `wait` timed out while screenshots, which wake the phone first,
    looked fine). An off screen read as the status and navigation bars
    alone too, 15 nodes and no word (Oct 6: after a launch with the screen
    off, taken for a server that reads no words, the server was replaced:
    10s; Muse's screenshots, which wake the phone, showed the app). A
    pulled-down shade or a lock screen has words. Pure."""
    if not real_screen(xml):
        return True
    if not set(re.findall(r'package="([^"]*)"', xml)) <= {SYSTEM_UI}:
        return False
    return xml.count("<node") <= 2 or re.search(r'\s(?:text|content-desc)="[^"]+"', xml) is None


def mute_read(xml):
    """True for a read with an app's nodes but not one word on them: no
    text and no content-desc anywhere. The server launched on the Pixel
    7 at 15:35 (Oct 4) read Settings pages like this for two hours
    (containers, no labels) while screenshots showed the labels; a fresh
    server read them in full. A screen mid-draw reads like this for a
    moment too, so one such read proves nothing (see MUTE_SPELL_S). Pure."""
    if blank_screen(xml):
        return False
    return re.search(r'\s(?:text|content-desc)="[^"]+"', xml) is None


def has_words(xml):
    """True for a read with an app and at least one word on it. Pure."""
    return not blank_screen(xml) and not mute_read(xml)


MUTE_SPELL_S = 8.0  # wordless this long across reads: the server, not a loader


def screen_on(d):
    """deviceInfo's screenOn: True or False, None when the server doesn't
    say. One cheap RPC. Raises when the server doesn't answer (a dead
    server must not pass for "screen on")."""
    info = d.info
    return info.get("screenOn") if isinstance(info, dict) else None


WAKE_SETTLE_S = 3.0  # after waking the screen, before the first read or a launch


def server_works(d, words=False):
    """True if the on-device server answers and can read the screen. An
    off screen reads blank whatever the server's state, so it is woken
    first; a just-started server's first read can be empty, so it gets
    two reads. words: a read with an app and no word on it fails too
    (the server a helper found at 15:35 and at 18:41 on Oct 4 read that
    way for hours, until it was replaced)."""
    woke = screen_on(d) is False  # raises if the server is dead
    if woke:
        try:
            d.jsonrpc.wakeUp()
        except Exception as e:
            log("wakeUp failed (%s)" % str(e)[:80])
        # The app's words come back a few seconds after a long sleep:
        # every wordless server on Oct 4 was judged or launched within
        # seconds of the screen waking (18:41, 19:22, 20:02).
        _time.sleep(WAKE_SETTLE_S)
    xml = read_screen(d, timeout=PROBE_TIMEOUT)
    if not real_screen(xml) or (words and mute_read(xml)):
        _time.sleep(2.0 if woke else 0.5)  # a just-started server's first read is flaky
        xml = read_screen(d, timeout=PROBE_TIMEOUT)
        if not real_screen(xml):
            return False
    if mute_read(xml):
        packages = sorted(set(re.findall(r'package="([^"]*)"', xml)))
        log("the server reads no words (%d nodes in %s)"
            % (xml.count("<node"), ", ".join(packages)))
        return not words
    return True


# The server's process on the phone: app_process running the u2 jar, and
# the shell wrapper adb started it through. A stray `uiautomator` shell
# command holds the same accessibility connection, so it counts too.
SERVER_MARKS = ("com.wetest.uia2", "com.android.commands.uiautomator")


def err_text(e, n=160):
    """An exception for a log line: its type and the end of its message
    (the telling part of uiautomator2's launch errors sits at the end)."""
    msg = " ".join(str(e).split())
    return "%s: %s" % (type(e).__name__, msg[-n:])


def server_pids(ps_output):
    """PIDs of the UI server's processes in a `ps -A -o PID,ARGS` listing.
    Pure."""
    pids = []
    for line in (ps_output or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        if any(m in line for m in SERVER_MARKS):
            pids.append(parts[0])
    return pids


PS_SERVER = "ps -A -o PID,ARGS"
SHELL_TIMEOUT = 8  # a phone shell over a stalled tunnel must not hold the lock for long


def kill_server_on_phone(dev):
    """Kill the UI server's processes on the phone (they run as the shell
    user, so adb's shell may) and check they are gone. Returns the PIDs
    found. uiautomator2 can't do this: it only stops a server it launched
    itself, and never relaunches one that still answers /ping."""
    pids = server_pids(bounded_shell(dev, PS_SERVER, SHELL_TIMEOUT))
    if not pids:
        log("no UI server process on the phone")
        return pids
    bounded_shell(dev, "kill -9 " + " ".join(pids), SHELL_TIMEOUT)
    _time.sleep(0.6)  # the accessibility connection takes a moment to free
    left = server_pids(bounded_shell(dev, PS_SERVER, SHELL_TIMEOUT))
    if left:
        raise RuntimeError("could not kill the UI server on the phone (pid %s)" % " ".join(left))
    log("killed the server on the phone (pid %s)" % " ".join(pids))
    return pids


def apply_fast_config(d):
    """No idle wait on the server: Chrome never idles, so every UiObject
    call was paying ~1s waitForIdle(); burner does its own polling. The
    server loses this on every restart, so it is applied to each one."""
    try:
        cfg = d.jsonrpc.getConfigurator()
        # a tap by words (click by selector) waits for the UI to answer
        # it, 3s by default when nothing on screen changes: 500ms here,
        # the batch's own wait and read follow
        cfg.update({"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0,
                    "actionAcknowledgmentTimeout": 500})
        d.jsonrpc.setConfigurator(cfg)
    except Exception as e:
        log("configurator tweak failed:", err_text(e))


GONE_WORDS = ("not online", "not found", "offline", "no devices", "no device",
              "no answer from the phone")


def device_gone(text):
    """Whether an error from adb, or from the UI server's connect, says the
    phone itself is gone from adb (the link through the tunnel dropped),
    not that the server is sick. Pure."""
    t = (text or "").lower()
    return any(w in t for w in GONE_WORDS)


def phone_gone(e):
    """Whether `e`, from a command on the phone's server, says the phone
    is gone from adb (see device_gone): a transport's error (adb's, a
    stream's, the link's), never the server's own answer, which says
    "not found" of a missing element too."""
    transport = (isinstance(e, (LinkDead, OSError))  # StreamUnavailable is a ConnectionError
                 or type(e).__name__ in ("AdbError", "AdbTimeout", "AdbConnectionError", "ConnectError"))
    return transport and device_gone(err_text(e))


LINK_PROBE_S = 5.0    # one shell over the link; a dead link answers nothing for minutes
ADB_CONNECT_S = 10.0  # `adb connect` through the tunnel: a CONNECT via the proxy and adb's handshake
ONLINE_WAIT_S = 5.0   # after a connect, for adb to call the phone a device (1-2s through the proxy)


LINK_WHY = [""]  # why the phone last didn't answer over adb, for the messages


def link_alive():
    """Whether the phone answers over adb: one shell, LINK_PROBE_S at
    most in all (see bounded_shell). adb's table still says "device" for
    a link through the tunnel that died silently while idle (Oct 6), and
    anything sent on it waits for an answer that never comes;
    uiautomator2's connect sends shells with no bound, so the phone is
    asked this way first. False too when adb has lost the phone (not
    found, offline, unauthorized); LINK_WHY[0] then says which."""
    import adbutils
    try:
        out = bounded_shell(adbutils.adb.device(TARGET), "echo ok", LINK_PROBE_S)
    except Exception as e:
        LINK_WHY[0] = err_text(e, 100)
        log("the phone doesn't answer over adb (%s)" % LINK_WHY[0])
        return False
    if "ok" not in (out or ""):
        LINK_WHY[0] = "its shell said %r" % (out or "")[:40]
        log("the phone doesn't answer over adb (%s)" % LINK_WHY[0])
        return False
    return True


def reconnect_device():
    """The phone back on adb, the first steps of `burner ensure` (what put
    it back on Oct 6, after the helper's own tries had run three
    commands into the 60s watchdog): the tunnel started (nothing when it
    runs), and the phone disconnected and connected again (adb keeps a
    dead link as a device, and says "already connected" to a connect).
    True when the phone answers a shell. adb's server isn't restarted
    here: the CLI's `ensure` does that, with its checks of the phone's
    port, when the helper says the phone is gone (the scrcpy helper's
    streams use that server too). uiautomator2 reconnects a lost phone
    on its own too, but with a second for each connect, which a
    handshake through the proxy often outlasts: 8-10s of tries, or "not
    online" (Oct 6)."""
    import adbutils
    # each step timed: one "adb reconnect 7475ms" left no word which one
    # took the time (ESPN, Oct 7)
    tunnel = os.path.join(ROOT, "tunnel.sh")
    if os.path.exists(tunnel):
        try:
            with _t("tunnel start"):
                subprocess.run(["bash", tunnel, "start"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20)
        except Exception as e:
            log("the tunnel couldn't be started (%s)" % err_text(e, 80))
    try:
        with _t("adb disconnect"):
            adbutils.adb.disconnect(TARGET)
    except Exception:
        pass
    try:
        with _t("adb connect"):
            out = " ".join(str(adbutils.adb.connect(TARGET, timeout=ADB_CONNECT_S)).split())
    except Exception as e:
        out = "failed (%s)" % err_text(e, 100)
    log("adb connect %s: %s" % (TARGET, out[:80]))
    if "connected" not in out:
        LINK_WHY[0] = "adb connect: " + out[:80]
        return False
    try:
        adbutils.adb.wait_for(TARGET, state="device", timeout=ONLINE_WAIT_S)
    except Exception:
        pass  # the shell below says whether it answers
    if link_alive():
        log("the phone is back on adb")
        return True
    return False


def ensure_server(force=False):
    """The on-device uiautomator2 server, listening and able to read the
    screen; restarted (killed on the phone, started fresh) when it
    can't. The server a helper finds must read words (the one found at
    15:35 and at 18:41 on Oct 4 read none, for hours). A fresh server is
    taken as soon as it reads a real screen, words or not: a fresh one
    that reads no words is the spell rule's job (see _fix_wordless_read),
    mid-session, where relaunches read every label. Two tries, the
    second for a launch that failed; the screen is woken before each
    launch and, when it was off, given a few seconds first. force (the
    spell rule): replace the running server without checking it.
    Returns the connected device."""
    import adbutils
    import uiautomator2 as u2
    first = "it reads empty or wordless screens"
    if force:
        first = "its reads held no words"
        log("replacing the server: its reads held no words")
    else:
        # the link first, with a bound: uiautomator2's connect runs shells
        # with no bound, and a link that died silently (Oct 6) answers none
        if not link_alive() and not reconnect_device():
            raise RuntimeError("the phone is gone from adb (%s) and did not come back "
                               "on a reconnect (adb connect %s)" % (LINK_WHY[0], TARGET))
        try:
            d = u2.connect(TARGET)  # starts a dead server (and pushes a new jar)
            apply_fast_config(d)
            if server_works(d, words=True):
                return d
            log("server answers but reads empty or wordless screens; restarting it")
        except Exception as e:
            first = err_text(e)
            if phone_gone(e) and reconnect_device():
                # the phone was gone from adb, not the server from the
                # phone: back on adb, the server is asked again first
                try:
                    d = u2.connect(TARGET)
                    apply_fast_config(d)
                    if server_works(d, words=True):
                        log("the phone is back on adb (%s); the server answers" % first)
                        return d
                except Exception as e2:
                    first = err_text(e2)
            log("server not responding (%s); restarting it" % first)
    why = "no read"
    for attempt, pause in ((1, 1.0), (2, 2.0)):
        try:
            dev = adbutils.adb.device(TARGET)
            killed = kill_server_on_phone(dev)
            _KEEPALIVE.close()
            awake = screen_awake(dev)
            wake_phone(dev)
            # A server launched seconds after the screen woke read no
            # words (Oct 4): a woken screen gets WAKE_SETTLE_S first.
            _time.sleep(pause if awake else WAKE_SETTLE_S)
            d = u2.connect(TARGET)  # /ping fails now, so this launches one
            apply_fast_config(d)
            if server_works(d):
                log("server (re)started")
                return d
            why = ("a fresh server reads empty screens" if killed else
                   "no server process was running, and a fresh one reads empty screens")
        except Exception as e:
            why = err_text(e)
        log("restart %d: %s" % (attempt, why))
    raise RuntimeError("the phone's UI server couldn't be restarted: %s (at first: %s)"
                       % (why, first))


def screen_awake(dev):
    """Whether the phone's screen is on, asked over adb (there may be no
    server to ask): True, False, or None when adb can't say."""
    try:
        out = bounded_shell(dev, "dumpsys power | grep -m1 mWakefulness=", SHELL_TIMEOUT)
    except Exception as e:
        log("couldn't read the screen state (%s)" % err_text(e, 80))
        return None
    m = re.search(r"mWakefulness=(\w+)", out or "")
    return m.group(1) == "Awake" if m else None


def wake_phone(dev):
    """Turn the screen on over adb before a server starts (no server to
    ask yet). KEYCODE_WAKEUP does nothing to a screen that is on."""
    try:
        bounded_shell(dev, "input keyevent 224", SHELL_TIMEOUT)
    except Exception as e:
        log("wake over adb failed (%s)" % err_text(e, 80))


RESTART_COOLDOWN_S = 20.0  # at most one server restart per this


def restart_allowed(since_restart, cooldown=RESTART_COOLDOWN_S):
    """Whether the server may be restarted: not within the cooldown of the
    last restart. Then an empty read is taken as what the phone shows and
    a failed command is raised as it is, so a server that stays broken
    isn't killed and relaunched on every read and command. Pure."""
    return since_restart >= cooldown


DUMP_TTL = 2.0  # seconds a cached hierarchy dump stays valid


# Commands that change the screen: never replayed after an error.
NO_RETRY = {"tap", "click_text", "set_text", "act"}

_CDP = None
WEB_RETRY_S = 20.0  # after the page in Chrome was out of reach: the screen reader this long
WEB_BUSY_RETRY_S = 4.0  # after Chrome's page didn't answer while Chrome is in front


def _cdp():
    """The DevTools client (cdp.py next to this file), loaded once."""
    global _CDP
    if _CDP is None:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "cdp.py")
        spec = importlib.util.spec_from_file_location("burner_cdp", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CDP = mod
    return _CDP


def webview_top(xml):
    """The top edge (screen pixels) of the first WebView on a read, or 0:
    the page's rows are placed below it. Pure."""
    m = re.search(r'class="[^"]*WebView"[^>]*bounds="\[(-?\d+),(-?\d+)\]', xml or "")
    return int(m.group(2)) if m else 0


def url_bar_of(xml):
    """What Chrome's address bar shows on a read of the screen (the text
    of its url_bar field), or "": the address of the tab on screen, for
    the page scan to find that tab wherever Chrome lists it. Pure."""
    import xml.sax.saxutils as saxutils
    for m in re.finditer(r"<node\b[^>]*>", xml or ""):
        tag = m.group(0)
        if ':id/url_bar"' not in tag:
            continue
        t = re.search(r'\btext="([^"]*)"', tag)
        return saxutils.unescape(t.group(1), {"&quot;": '"'}).strip() if t else ""
    return ""


def webview_rect(xml):
    """The rectangle (x1, y1, x2, y2) of the first WebView on a read, or
    None. Pure."""
    m = re.search(r'class="[^"]*WebView"[^>]*bounds="\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', xml or "")
    return tuple(int(v) for v in m.groups()) if m else None


WINDOWS_DEPTH = 2       # a look at the phone's windows: each window's root and its first rows
LOOK_WAIT_S = 2.0       # the look is waited for this long once the page has answered
WINDOW_OVER_PART = 0.2  # a window holding this much of the page's box is over it


def window_over(windows_xml, rect, front_pkg):
    """The window drawn over `rect` (the page's WebView, else the screen)
    on a read of the phone's windows: (its package, its first words) or
    None. A window of another app than the one in front (the
    notification shade, a permission dialog, an app chooser), not a
    keyboard, holding WINDOW_OVER_PART of the rectangle or more; the bars
    at the edges hold too little. The page can't see such a window: the
    shade read as empty from the page while it held nine notifications
    (Oct 5). Pure."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(windows_xml or "")
    except ET.ParseError:
        return None
    x1, y1, x2, y2 = rect
    area = max(1, (x2 - x1) * (y2 - y1))
    fronts = {front_pkg} if isinstance(front_pkg, str) else set(front_pkg or ())
    for w in root:  # the windows, each a root under the hierarchy
        pkg = w.get("package") or ""
        if not pkg or pkg in fronts or any(k in pkg.lower() for k in U2Daemon.IME_HINTS):
            continue
        m = _BOUNDS_RE.match(w.get("bounds", ""))
        if not m:
            continue
        wx1, wy1, wx2, wy2 = map(int, m.groups())
        held = max(0, min(x2, wx2) - max(x1, wx1)) * max(0, min(y2, wy2) - max(y1, wy1))
        if held >= WINDOW_OVER_PART * area:
            words = next((t for n in w.iter("node") for t in (n.get("text"), n.get("content-desc")) if t), "")
            return pkg, words[:40]
    return None


def _log_handshake(page):
    """Say in the log, once per session, that the page's session took a
    second handshake (Chrome dropped the first: see cdp.Page.DROPPED)."""
    if getattr(page, "handshake_again", False):
        page.handshake_again = False  # said: the session is reused for many reads
        log("the page's session took a second handshake (Chrome dropped the first, busy)")


def _log_busy(screen):
    """Say in the log that a page read waited on its rows that said
    "Loading" (see cdp.read_loaded): how many, how long, how many left."""
    busy = (screen or {}).get("busy")
    if busy:
        log("web read: %d rows said Loading; read again for %.2fs, %d left" % (busy[0], busy[2], busy[1]))


class _Look(threading.Thread):
    """A read of the phone's windows alone (WINDOWS_DEPTH deep: small and
    quick on the phone), taken while the page is asked, so that a window
    over the page is seen without a round trip of its own. Started at
    once; `over` has the answer."""

    def __init__(self, dm, front=None):
        super().__init__(daemon=True)
        self.dm, self.xml, self.error = dm, None, None
        self.front = front  # the page's app, when the newest read doesn't show it (just launched)
        self.start()

    def run(self):
        try:
            self.xml = self.dm.d.jsonrpc_call("dumpWindowHierarchy", [False, WINDOWS_DEPTH],
                                              timeout=DUMP_RPC_TIMEOUT)
        except Exception as e:
            self.error = e

    def over(self):
        """The window over the page (see window_over), or None, once the
        look is in (LOOK_WAIT_S at most past the page's answer; a look
        that is late or failed leaves the page's read standing)."""
        self.join(LOOK_WAIT_S)
        if not self.xml:
            log("the look at the windows %s; the page's read stands"
                % ("failed (%s)" % err_text(self.error, 80) if self.error else "is late"))
            return None
        try:
            last = getattr(self.dm, "_last_xml", "")
            info = screen_of(last) if last else None
            rect = webview_rect(last) or ((0, 0, info[0], info[1]) if info else None)
            if rect is None:
                rect = (0, 0, 1080, 2400) if self.front else None
            if rect is None:
                return None
            return window_over(self.xml, rect, self.front or (info[2] if info else ""))
        except Exception as e:
            log("the look at the windows couldn't be read (%s); the page's read stands" % err_text(e, 80))
            return None


def batch_results(replies, n):
    """One entry per call from a JSON-RPC batch reply: the result, or an
    Exception for a call that failed. Pure."""
    if not isinstance(replies, list):
        raise RuntimeError("no batch support: %s" % str(replies)[:80])
    by_id = {r.get("id"): r for r in replies if isinstance(r, dict)}
    out = []
    for i in range(1, n + 1):
        r = by_id.get(i, {})
        if "error" in r:
            out.append(RuntimeError(str(r["error"])[:200]))
        else:
            out.append(r.get("result"))
    return out


SLIVER_PX = 16  # words shorter or narrower than this are cut off at an edge


def same_control(a, b):
    """True when two rows read with the same label are one control drawn
    twice (Play Store's Open: a View described "Open" over a TextView
    reading "Open"; a web link's area over its words): one's centre lies
    inside the other's rectangle. Pure."""
    (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) = a["rect"], b["rect"]
    (acx, acy), (bcx, bcy) = a["center"], b["center"]
    return ((bx1 <= acx <= bx2 and by1 <= acy <= by2)
            or (ax1 <= bcx <= ax2 and ay1 <= bcy <= ay2))


def cut_off(n):
    """True for a row whose words are a sliver on the read, under
    SLIVER_PX tall or wide: the row is cut off at the screen's edge or
    under a bar. (espn.com, Oct 4: a "Box Score" link scrolled to the top
    edge read 2px tall; the tap at its centre landed on the link below.)
    Pure."""
    x1, y1, x2, y2 = n["rect"]
    return (y2 - y1) < SLIVER_PX or (x2 - x1) < SLIVER_PX


def union_rect(rows):
    """The smallest rectangle around these rows' rectangles. Pure."""
    return (min(r["rect"][0] for r in rows), min(r["rect"][1] for r in rows),
            max(r["rect"][2] for r in rows), max(r["rect"][3] for r in rows))


def contains(outer, inner):
    """True when rectangle `outer` holds all of rectangle `inner`. Pure."""
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


OVERLAY_CLASS_HINTS = ("dialog", "bottomsheet", "popup", "dropdown", "spinner", "menu")


def takes_tap(n, target, size):
    """True when `n`, drawn over the tap point of `target` and after it,
    would take the tap (the CLI's own rule, so a tap by words refuses
    what a planned tap refuses): a dialog, sheet, popup or menu by its
    class; a row with words of its own, unless they are the target's
    (the same control drawn twice); or a big clickable unlabelled scrim,
    two fifths of the screen (`size`, (w, h)) or more. Most covers are
    harmless (an unlabelled button drawn over its words, a Compose
    app's views) and pass. Pure."""
    if any(k in n.get("cls", "") for k in OVERLAY_CLASS_HINTS):
        return True
    words = (n["text"] or n["desc"]).strip().lower()
    if words:
        return words != (target["text"] or target["desc"]).strip().lower()
    if not n["clickable"] or not size:
        return False
    x1, y1, x2, y2 = n["rect"]
    return (x2 - x1) * (y2 - y1) >= 0.4 * size[0] * size[1]


def can_tap(nodes, n):
    """True for a row that takes a tap: clickable or selected itself (a
    tab of a navigation bar reads as selected and not clickable: the
    Clock app's "Alarms" tab beside the title "Alarms", Oct 5), or
    inside such a row (see iter_nodes: parent). Pure."""
    if n["clickable"] or n.get("selected"):
        return True
    p = n.get("parent")
    while p is not None:
        if nodes[p]["clickable"] or nodes[p].get("selected"):
            return True
        p = nodes[p].get("parent")
    return False


def descends(nodes, i, ids):
    """True when nodes[i] is inside one of the nodes whose id() is in
    `ids` (see iter_nodes: parent). Pure."""
    p = nodes[i].get("parent")
    while p is not None:
        if id(nodes[p]) in ids:
            return True
        p = nodes[p].get("parent")
    return False


def screen_size(nodes):
    """(w, h) from a read's nodes: the far corner of those touching the
    left or top edge (see screen_of), or None when none does. Pure."""
    w = h = 0
    for n in nodes:
        x1, y1, x2, y2 = n["rect"]
        if x1 == 0 or y1 == 0:
            w, h = max(w, x2), max(h, y2)
    return (w, h) if w >= 300 and h >= 300 else None


def row_over(nodes, control, center, size=None):
    """What is drawn over `center`, the tap point of `control` (its rows),
    or None. On a web page document order says nothing about what is on
    top, and a sticky bar sits over the content it precedes (espn.com,
    Oct 4: the tap meant for Box Score opened Standings): there a row of
    the page with words of its own counts, not one of the control's
    rows, whose rectangle holds the point but neither holds the whole
    control (a card described with its game's names holds its own Box
    Score link and is no cover) nor sits inside it (a button's own
    words). Anything native drawn after the control (later is on top; a
    dialog is a later window) counts when it would take the tap (see
    takes_tap): a dialog's button over a row of the app behind it, or
    over a link in a WebView (a tap by words pressed the dialog's button
    where the CLI's planned tap refused, found in review Oct 5). The
    control's own parts don't count. Pure."""
    cx, cy = center
    whole = union_rect(control)
    web = bool(control[0].get("web"))
    ids = {id(c) for c in control}
    after, cover = False, None
    for i, n in enumerate(nodes):
        if id(n) in ids:
            after = True
            continue
        x1, y1, x2, y2 = n["rect"]
        if not (x1 <= cx <= x2 and y1 <= cy <= y2):
            continue
        if n.get("web"):
            if (web and (n["text"] or n["desc"])
                    and not (contains(n["rect"], whole) or contains(whole, n["rect"]))):
                return n
            continue
        if after and not descends(nodes, i, ids) and takes_tap(n, control[0], size):
            cover = n  # the last one drawn is the one on top
    return cover


def label_target(xml, label):
    """The centre (x, y) of the one control on this read whose text or
    description is `label` (see label_node)."""
    node, _ = label_node(xml, label)
    return tuple(node["center"])


def selector_for(node, label):
    """The phone's own selector for this control: its exact text, else
    its exact description (whichever carries `label`); the phone finds
    it at tap time, so a read need not come first. The shape is
    uiautomator2's Selector (text: mask 1, description: mask 64), built
    here so that no import is needed. Pure."""
    if plain_words(node["text"]).lower() == plain_words(label).lower():
        key, value, mask = "text", node["text"], 1  # its own words: the phone matches them exactly
    else:
        key, value, mask = "description", node["desc"], 64
    return {"mask": mask, "childOrSibling": [], "childOrSiblingSelector": [], key: value}



# Words are compared in their plain form. Every kind of space is one
# plain space: Android formats "6:30 AM" with a narrow no-break space
# before AM (U+202F), the assistant types a plain one, and the exact match
# missed the row while the fuzzy one found the two descriptions around it
# (the Clock app, Oct 5). Typographic punctuation is its plain form: the
# permission dialog's "Don\u2019t allow" has a curly apostrophe, the
# assistant typed "Don't allow", and the tap failed twice (YouTube, Oct
# 6); Settings writes "Wi\u2011Fi" with a non-breaking hyphen.
_PLAIN = dict.fromkeys(map(ord, "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
                                "\u2008\u2009\u200a\u202f\u205f\u3000\t\r\n"), " ")
_PLAIN.update(dict.fromkeys(map(ord, "\u2018\u2019\u201b\u2032\u02bc\uff07"), "'"))
_PLAIN.update(dict.fromkeys(map(ord, "\u201c\u201d\u201e\u201f\u2033\uff02"), '"'))
_PLAIN.update(dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d"), "-"))
_PLAIN[ord("\u2026")] = "..."


def plain_words(s):
    """`s` in its plain form, for comparing words: every kind of space as
    one plain space (runs collapsed, the ends trimmed), curly quotes and
    apostrophes, dashes and hyphens, and the ellipsis as their plain
    forms. Pure."""
    return " ".join((s or "").translate(_PLAIN).split())


def selector_matches(xml, sel):
    """How many rows of a read the phone's selector `sel` (see
    selector_for: a row's exact text, or its exact description) would
    find; 0 for a read that can't be parsed. Pure."""
    import xml.etree.ElementTree as ET
    attr, want = (("text", sel.get("text")) if "text" in sel
                  else ("content-desc", sel.get("description")))
    try:
        return sum(1 for n in ET.fromstring(xml or "").iter("node") if (n.get(attr) or "") == want)
    except ET.ParseError:
        return 0


def label_node(xml, label):
    """The one control on this read whose text or description is `label`
    (case-insensitive exact; "A || B" tries each): (its first row, the
    alternative that matched). A control drawn twice (see same_control)
    is one control, its first row's centre the tap point. A tap by
    coordinates injects a touch and looks nothing up: the server's click
    by selector threw a NullPointerException on a web node in Chrome
    (espn.com, Oct 4). Raises RuntimeError when there is no such control,
    more than one, its words are cut off at an edge (see cut_off), or
    another row sits over it (see row_over), or the read can't be
    parsed (empty, or cut)."""
    try:
        nodes = list(iter_nodes(xml or ""))
    except Exception as e:
        raise RuntimeError("the read can't be parsed (%s)" % err_text(e, 60))
    for alt in [p.strip() for p in label.split("||") if p.strip()]:
        low = plain_words(alt).lower()
        hits = [n for n in nodes
                if low in (plain_words(n["text"]).lower(), plain_words(n["desc"]).lower())]
        if not hits:
            continue
        controls = []  # the hits, one list per control
        for n in hits:
            for c in controls:
                if same_control(c[0], n):
                    c.append(n)
                    break
            else:
                controls.append([n])
        if len(controls) > 1:
            # the words typed into a field are not its label when another
            # row carries them (a search box holding "Pixel 7" beside that
            # suggestion)
            typed = [c for c in controls if c[0].get("field") and plain_words(c[0]["text"]).lower() == low]
            if typed and len(typed) < len(controls):
                controls = [c for c in controls if c not in typed]
        if len(controls) > 1:
            # the words on a button beside the same words as plain text
            # (Play's "Open" button and the word "Open" in the listing,
            # Oct 5): the one that can be tapped is the control
            tappable = [c for c in controls if any(can_tap(nodes, n) for n in c)]
            if len(tappable) == 1:
                controls = tappable
        if len(controls) > 1:
            raise RuntimeError("%d rows read %r" % (len(controls), alt))
        if any(cut_off(n) for n in controls[0]):
            raise RuntimeError("%r is cut off at the screen's edge" % alt)
        center = tuple(controls[0][0]["center"])
        over = row_over(nodes, controls[0], center, screen_size(nodes))
        if over is not None:
            raise RuntimeError("%r is under %r" % (alt, (over["text"] or over["desc"])[:40]))
        return controls[0][0], alt
    raise RuntimeError("not on the last read")


BY_WORDS_S = 20.0  # a read this young still says what the screen is: a tap
                   # by words on it needs no read first


def act_calls(spec):
    """The JSON-RPC calls for an `act` spec: the action (tap, key or
    set_text, if any), then a read that serves as the on-device pause
    while the action's first events arrive (waitForIdle returns at once
    when the last event is older than its quiet window), the wait for
    the UI to go quiet, and the read that is returned. No
    waitForWindowUpdate: inside a batch it crashed the phone's server
    (Oct 4). An action starts with wakeUp: a tap or a key on an off
    screen is dropped (a phone off its charger turns its screen off after
    10s), and wakeUp is a no-op with the screen on and sleeps 500ms only
    when it woke the phone. Not before POWER or SLEEP, whose job is the
    opposite. A read with no action wakes nothing (it may follow a POWER
    sent another way), unless it is the read after a launch (`wake`). Pure."""
    calls = []
    if "tap_selector" in spec:
        # how many controls carry the words now (the check after the tap),
        # then the click, by its words at tap time
        calls.append(("count", [spec["tap_selector"]]))
        calls.append(("click", [spec["tap_selector"]]))
    elif "tap" in spec:
        x, y = spec["tap"]
        calls.append(("click", [int(x), int(y)]))
    elif "key" in spec:
        calls.append(("pressKeyCode", [int(spec["key"])]))
    elif "set_text" in spec:
        if "tap_first" in spec:
            # the field tapped, the screen given its wait, then the text
            # into the field that took the focus: one trip for both
            x, y = spec["tap_first"]
            calls.append(("click", [int(x), int(y)]))
            calls.append(("dumpWindowHierarchy", [False, DUMP_DEPTH]))
            calls.append(("waitForIdle", [int(spec.get("idle", 2000))]))
        sel = spec.get("field_selector") or focused_field_selector()
        calls.append(("setText", [sel, str(spec["set_text"])]))
    if calls:
        if not sleeps_the_screen(spec):
            calls.insert(0, ("wakeUp", []))
        if "tap_selector" not in spec:
            # a tap by words is acknowledged by the UI before the phone
            # answers it (the click waits for the first event it causes),
            # so no read stands in for the pause there
            calls.append(("dumpWindowHierarchy", [False, DUMP_DEPTH]))
    elif spec.get("wake"):
        # the read after a launch: an app started with the screen off read
        # as the system UI's bars alone, and the look at the screen, the
        # wake and the read again came after (about 2s of the Calculator's
        # 3.7s start, Oct 7)
        calls.append(("wakeUp", []))
    calls.append(("waitForIdle", [int(spec.get("idle", 2000))]))
    # the read that is returned; a chained (quiet) step takes it too, on
    # the phone's time alone, as the newest read known: its next step
    # finds its control by words on the screen as it is after this one
    # (a calculator's display reads "1" once the 1 key is tapped; on the
    # read from before, the one "1" was the key, found in review Oct 5)
    calls.append(("dumpWindowHierarchy", [False, DUMP_DEPTH]))
    return calls


ACTION_METHODS = ("click", "pressKeyCode", "setText")


RELINK_RETRY_S = 20.0  # a reconnect that failed isn't tried again sooner (the CLI's `ensure` then)
RELOOK_PAUSE_S = 0.4   # an action that left the screen as it was: this long, then
RELOOK_IDLE_MS = 1200  # a wait for the UI to go quiet, this long at most, and a read


def screen_sig(xml):
    """What a read shows, for telling whether an action changed the
    screen: each row's words, place and state (checked, selected,
    focused), the system UI's rows aside (its clock and icons change on
    their own). None for a read that can't be parsed. Pure."""
    try:
        return [(n["text"], n["desc"], tuple(n["center"]), n["checked"], n["selected"], n["focused"])
                for n in iter_nodes(xml) if n["pkg"] != SYSTEM_UI]
    except Exception:
        return None


def screen_words(xml):
    """What a read says, places and focus aside: each worded row's words
    and its check, the system UI's aside. A tap that opens another app's
    screen moves the row tapped, or gives it the focus, before the new
    screen is drawn: such a read differs from the one before in places
    alone (Settings' search, Oct 7). None for a read that can't be parsed.
    Pure."""
    try:
        return sorted((n["text"], n["desc"], n["checked"]) for n in iter_nodes(xml)
                      if n["pkg"] != SYSTEM_UI and (n["text"] or n["desc"]))
    except Exception:
        return None


def nothing_new(xml, before, words=None):
    """Whether a read after an action shows no row the read before didn't
    (see screen_words: the same rows, or fewer, places and focus aside):
    a screen still fading out for the next one. words: the two reads'
    screen_words, when worked out already. Pure."""
    a, b = words if words is not None else (screen_words(xml), screen_words(before))
    if not a or b is None:
        return False
    pool = list(b)
    for w in a:
        if w not in pool:
            return False
        pool.remove(w)
    return True


def words_changed(before, after):
    """The rows' words in one read and not the other, as ("-" or "+", the
    words cut to 40 characters), switch states aside and text fields too:
    what is typed in one can be a password its eye button shows, never
    logged (review, Oct 7). None for a read that can't be parsed. Pure."""
    def rows(xml):
        try:
            return [n["text"] or n["desc"] for n in iter_nodes(xml)
                    if n["pkg"] != SYSTEM_UI and (n["text"] or n["desc"]) and not n["field"]]
        except Exception:
            return None
    ta, tb = rows(before), rows(after)
    if ta is None or tb is None:
        return None
    gone = [w for w in ta if w not in tb]
    came = [w for w in tb if w not in ta]
    return [("-", w[:40]) for w in gone] + [("+", w[:40]) for w in came]


def focused_field(xml):
    """The text field with the focus on a read, as (resource id, centre),
    or None. Pure."""
    try:
        for n in iter_nodes(xml):
            if n["field"] and n["focused"]:
                return n["rid"], tuple(n["center"])
    except Exception:
        pass
    return None


def worded_rows(xml):
    """How many rows of a read carry words (text or a description), the
    system UI's aside. Pure."""
    try:
        return sum(1 for n in iter_nodes(xml) if n["pkg"] != SYSTEM_UI and (n["text"] or n["desc"]))
    except Exception:
        return 0


def half_drawn_after(xml, before):
    """Whether the read after an action looks taken mid-transition: a row
    or two with words where the read before had a dozen (Settings' "About
    phone" read as its search bar alone after the tap, Oct 6). Pure."""
    return worded_rows(xml) <= 2 and worded_rows(before) >= 12


def chrome_in(xml):
    """Whether a read shows Chrome (a page there is read by the page
    itself after an action). Pure."""
    pkgs = set(re.findall(r'package="([^"]*)"', xml or ""))
    return bool(pkgs & set(_cdp().CHROME_PACKAGES))


def focused_field_selector(rid=""):
    """The phone's selector for the text field that has the focus: that
    very field when its resource id is known (a field that lost the focus
    is then not found, and nothing is typed), else any EditText with the
    focus. uiautomator2's Selector shape (focused: mask 0x20000, class
    name: 0x10, resource id: 0x200000), built here so that no import is
    needed. Pure."""
    sel = {"childOrSibling": [], "childOrSiblingSelector": [], "focused": True}
    if rid:
        sel.update(mask=0x20000 | 0x200000, resourceId=rid)
    else:
        sel.update(mask=0x20000 | 0x10, className="android.widget.EditText")
    return sel


def field_selector(rid=""):
    """The phone's selector for a text field, focused or not: by its
    resource id when known, else any EditText (the caller knows there is
    one). uiautomator2's Selector shape (class name: 0x10, resource id:
    0x200000). Pure."""
    sel = {"childOrSibling": [], "childOrSiblingSelector": [], "className": "android.widget.EditText",
           "mask": 0x10}
    if rid:
        sel.update(mask=0x10 | 0x200000, resourceId=rid)
    return sel


TAP_OPENED_S = 30.0  # a control tapped this recently may have opened the box a `type --field` names
FIELD_AGAIN_S = 0.4  # a field not (or not yet) where the typing wants it is looked for again this much later
WAKE_RPC_S = 3.0     # the wake before a launch: a no-op, or 500ms on the phone


def lone_focused_field(xml, label):
    """The read's one text field, when it has the focus and no row on the
    read carries `label` (a button's words, say, that opened the box: the
    Play Store's "Search or ask Play" opens a box reading "Search apps &
    games", Oct 6); else None. Pure."""
    try:
        nodes = list(iter_nodes(xml or ""))
    except Exception:
        return None
    alts = [plain_words(p).lower() for p in label.split("||") if p.strip()]
    # a keyboard's key reading the label ("Search") is no row of the screen
    # that still carries it (review of Oct 7)
    if any(a in (plain_words(n["text"]).lower(), plain_words(n["desc"]).lower())
           for n in nodes for a in alts
           if n["pkg"] != SYSTEM_UI and not any(k in n["pkg"] for k in U2Daemon.IME_HINTS)):
        return None
    fields = [n for n in nodes if n["field"]]
    return fields[0] if len(fields) == 1 and fields[0]["focused"] else None


def hint_field(xml, label):
    """The text field whose hint reads `label` when a view of its own
    draws the hint: words that can't be tapped, inside the field (the Play
    Store's search box is an empty field under the words "Search apps &
    games"; a type by them took a tap and two more trips, 4.7s, Oct 6).
    None unless one field holds such words, whole, with nothing over it
    but its hint (see row_over). Pure."""
    try:
        nodes = list(iter_nodes(xml or ""))
    except Exception:
        return None
    alts = [plain_words(p).lower() for p in label.split("||") if p.strip()]
    hints = [n for n in nodes if not n["field"] and not n["clickable"]
             and any(a in (plain_words(n["text"]).lower(), plain_words(n["desc"]).lower()) for a in alts)]
    holders = []
    for f in nodes:
        if f["field"]:
            x1, y1, x2, y2 = f["rect"]
            mine = [h for h in hints if x1 <= h["center"][0] <= x2 and y1 <= h["center"][1] <= y2]
            if mine:
                holders.append([f] + mine)
    if len(holders) != 1:
        return None
    field = holders[0][0]
    if cut_off(field) or row_over(nodes, holders[0], tuple(field["center"]), screen_size(nodes)) is not None:
        return None
    return field


# A label for a secret names only a password field loosely: a search box
# holding "password manager" took a password for `--field Password`
# (review of Oct 7). Not "code" or "otp": a verification code's, a zip
# code's or a promo code's field is a plain text field.
SECRET_WORDS = ("password", "passcode", "passphrase", "pin", "cvv", "cvc")


def field_named(label, n, focused_ok=False):
    """Whether text field `n` is named loosely by `label` (any "a || b"
    alternative): the head of its words, a leading symbol aside, is the
    label's as whole words ("Email or phone" for `Email`: an empty field
    reads its hint); without the focus, unless `focused_ok` (a focused
    field's words may be what is being typed: it is named only when no
    other field is); a secret's label names only a password field. Pure."""
    if not n.get("field") or (n.get("focused") and not focused_ok):
        return False
    for a in [plain_words(p).lower() for p in label.split("||") if p.strip()]:
        if any(w in SECRET_WORDS for w in re.findall(r"[a-z]+", a)) and not n.get("password"):
            continue
        for s in (n.get("text"), n.get("desc")):
            head = re.sub(r"^[^\w]+", "", plain_words(s).lower())
            if re.match(re.escape(a) + r"(?![\w-])", head):
                return True
    return False


def loose_field(xml, label):
    """The one text field a label names loosely (see field_named), whole
    on the screen with nothing drawn over it (a sheet's "Continue as Zach"
    over an "Email or phone" field took the tap, review of Oct 7); None
    when none or several are. A field only takes the focus when tapped,
    so a loose match is safe here where a button's isn't (`type --field
    Email` failed on "Email or phone" once taps matched strictly). Pure."""
    try:
        nodes = list(iter_nodes(xml or ""))
    except Exception:
        return None
    size = screen_size(nodes)
    # a field with the focus only when no other is named (an auto-focused
    # "Email or phone" reads its hint, review of Oct 7)
    for focused_ok in (False, True):
        hits = [n for n in nodes if field_named(label, n, focused_ok) and not cut_off(n)
                and row_over(nodes, [n], tuple(n["center"]), size) is None]
        if hits:
            return hits[0] if len(hits) == 1 else None
    return None


def field_node(xml, label):
    """The text field with this label on a read (see label_node: once,
    whole, nothing over it), or the one whose hint reads it (see
    hint_field), or the one field whose words hold it (see loose_field);
    None otherwise. Pure."""
    if not xml:
        return None
    try:
        node, _ = label_node(xml, label)
        refused = False
    except RuntimeError as e:
        # a row with the label refused (under a cover, cut off, several):
        # no loose match stands in for it (review of Oct 7)
        node, refused = None, str(e) != "not on the last read"
    if node is not None and node.get("field"):
        return node
    hint = hint_field(xml, label)
    if hint is not None or refused:
        return hint
    return loose_field(xml, label)
SLEEP_KEYS = (26, 223, 276)  # POWER, SLEEP, SOFT_SLEEP


def sleeps_the_screen(spec):
    """True for an act spec whose key turns the screen off. Pure."""
    try:
        return "key" in spec and int(spec["key"]) in SLEEP_KEYS
    except (TypeError, ValueError):
        return False


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
        self._last_xml_t = 0.0  # when it was read (monotonic)
        self._last_restart = -1e9  # monotonic time of the last connect()
        self._restart_error = None  # why the last connect() failed, until one works
        self._mute_since = None  # monotonic time of the first wordless read of a spell
        self._wordless_seen = False  # a fresh server read no words either this spell
        self.d = None
        self.connect()

    def connect(self, force=False):
        """(Re)connect to the phone's server, restarting it when it can't
        read the screen; force: replace it whatever its checks say."""
        with self._lock:
            self.invalidate()
            install_keepalive()
            _KEEPALIVE.close()  # drop streams to a possibly-dead server
            try:
                self.d = ensure_server(force)  # applies the fast configurator
                self._restart_error = None
            except Exception as e:
                self._restart_error = err_text(e)
                raise
            finally:
                # Stamped even when the restart failed: the cooldown
                # keeps a broken server from being restarted on every read.
                self._last_restart = _time.monotonic()
            # warm up: one dump so later calls are fast; it is the first
            # read, so a helper just (re)started knows the app in front
            # (a page in Chrome is read as a page from the first command)
            xml = read_screen(self.d)
            if has_words(xml):
                self._last_xml, self._last_xml_t = xml, _time.monotonic()
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

    LAUNCH_CONTACT_TRIES = 10  # a Chrome just launched: its DevTools side is asked this often, 0.4s apart
    FRONT_STALE_S = 10.0  # no page in hand: a read of Chrome older than this is checked first
    OPENED_FOR_S = 60.0        # a link the CLI launched is the tab scan's hint this long

    def _address_hint(self, last, hint=None):
        """Where the tab on screen is, for the tab scan: `hint` (the link
        just opened), else what Chrome's bar shows on the newest read,
        else the link launched OPENED_FOR_S ago at most."""
        if hint:
            return hint
        bar = url_bar_of(last)
        if bar:
            return bar
        if _time.monotonic() - getattr(self, "_opened_at", 0.0) < self.OPENED_FOR_S:
            return getattr(self, "_opened", "")
        return ""

    def _page(self, assume_chrome=False, hint=None):
        """The visible page in Chrome, when Chrome is the app in front on
        the newest read and the page can be reached (cdp.front_page);
        else None: a native screen, or Chrome out of reach, and the
        screen reader's path applies. assume_chrome: Chrome was just
        launched with a link (no read shows it yet): the page is
        contacted at once, its current tab given the longer probe from
        the start, and a Chrome still starting is asked again for a
        few seconds, and only once its window is in front (one read of
        the screen first: asking the tabs of a Chrome still coming up
        kept it from coming up, 16-33s cold opens, Oct 5). hint: the
        link's address, for the scan (see _address_hint)."""
        last = getattr(self, "_last_xml", "")
        cached = getattr(self, "_front_cache", None)
        if cached is not None and cached[0] is last:
            info = cached[1]  # the same read as last time: no second parse
        else:
            info = screen_of(last) if last else None
            self._front_cache = (last, info)
        if not assume_chrome:
            if not info or not _cdp().is_chrome(info[2]):
                return None
            if _time.monotonic() < getattr(self, "_web_retry_at", 0.0):
                return None  # out of reach a moment ago: the screen reader, for now
            if (getattr(self, "_web", None) is None
                    and _time.monotonic() - getattr(self, "_last_xml_t", 0.0) > self.FRONT_STALE_S):
                # no page in hand, and the read that says Chrome is in
                # front is old: one look at the screen first (Chrome may
                # have left the front, or the screen gone off: a scan of
                # its tabs then answered "hidden" after 4s, Oct 6)
                xml = self._front_read()
                info = screen_of(xml) if xml else None
                if not info or not _cdp().is_chrome(info[2]):
                    self._front_look = xml or None  # what is in front, for _dump
                    return None
                last = xml
        current = getattr(self, "_web", None)
        chrome_in_front = True
        hint = self._address_hint(last, hint)
        # (an older cdp module takes no hint: none passed when there is none)
        hinted = {"hint": hint} if hint else {}
        if assume_chrome:
            woke = False
            for i in range(self.LAUNCH_CONTACT_TRIES):
                try:
                    # Chrome's window in front first (its address bar, when
                    # it is, is the hint): the tabs are asked only then
                    with _t("dump rpc (launch check)"):
                        xml = read_screen(self.d)
                    if not woke and blank_screen(xml) and screen_on(self.d) is False:
                        # the screen went off between commands (this phone
                        # turns it off soon after the last touch): nothing
                        # on it reads, and Chrome's pages say "hidden" (a
                        # 66s open, Oct 6)
                        woke = True
                        log("the screen is off; waking it to look for the page")
                        self._wake()
                        with _t("dump rpc (launch check)"):
                            xml = read_screen(self.d)
                    if has_words(xml):
                        self._remember(xml)
                        self._front_cache = (xml, screen_of(xml))
                    front = (screen_of(xml) or (0, 0, ""))[2]
                    if not _cdp().is_chrome(front):
                        raise RuntimeError("%s in front, not Chrome yet" % (front or "nothing readable"))
                    bar = url_bar_of(xml)
                    with _t("web page (after a launch)"):
                        self._web = _cdp().front_page(getattr(self.d, "_dev", None), current,
                                                      hint=bar or hint or None, quick=True)
                    _log_handshake(self._web)
                    return self._web
                except Exception as e:
                    current = None
                    if i == self.LAUNCH_CONTACT_TRIES - 1:
                        self._web = None
                        self._web_retry_at = _time.monotonic() + WEB_BUSY_RETRY_S
                        log("the page in Chrome is out of reach after the launch (%s); the screen reader"
                            % err_text(e, 300))
                        return None
                    # each try is logged with what the tabs answered: a cold
                    # open took five tries and 15s with no word why (Oct 5)
                    log("the page isn't reachable yet, try %d (%s)" % (i + 1, err_text(e, 300)))
                    _time.sleep(0.4)  # Chrome still starting, or its tab not yet visible
        if current is not None and not _cdp().visible(current, _cdp().QUICK_PROBE_S):
            # The page in hand didn't answer at once: it left the front,
            # or it is busy (right after a navigation). One look at the
            # screen says which: another app in front means the screen
            # reader's path at once, with no scan of Chrome's tabs (a
            # background tab answers a script only on its timeout); Chrome
            # still in front means the page gets a longer question.
            try:
                with self._lock:
                    with _t("dump rpc (front check)"):
                        xml = read_screen(self.d)
            except Exception:
                xml = ""
            if has_words(xml):
                self._remember(xml)
                self._front_cache = (xml, screen_of(xml))
                chrome_in_front = _cdp().is_chrome((self._front_cache[1] or (0, 0, ""))[2])
                if not chrome_in_front:
                    self._front_look = xml  # a read of what is in front, for _dump
            if not chrome_in_front or not _cdp().visible(current, _cdp().PROBE_S):
                current.close()
                self._web = current = None
            if not chrome_in_front:
                return None
        try:
            with _t("web page"):
                self._web = _cdp().front_page(getattr(self.d, "_dev", None), current, **hinted)
            _log_handshake(self._web)
        except Exception as e:
            self._web = None
            # Chrome in front but no page answering (busy, or a native
            # screen of Chrome's): the next command asks again soon.
            self._web_retry_at = _time.monotonic() + WEB_BUSY_RETRY_S
            log("the page in Chrome is out of reach (%s); the screen reader for %.0fs"
                % (err_text(e, 300), WEB_BUSY_RETRY_S))
            return None
        return self._web

    def _page_xml(self, screen):
        """A page read as a screen read (cdp.page_xml), placed under the
        newest read's WebView and above its navigation bar; None for a
        read that says the document is hidden (a tap opened another tab
        or another app, a key went Home): that ends the page's proof of
        being on screen, and the screen reader says what is in front
        (the page's rows printed as the screen while the phone showed
        the home screen, found in review Oct 5)."""
        page = getattr(self, "_web", None)
        if page is not None and screen.get("vis") == "visible":
            page.visible_at = _time.monotonic()  # the proof it is on screen
        elif screen.get("vis") == "hidden":
            if page is not None:
                page.visible_at = 0.0
            log("the page says it is hidden now; the screen reader says what is in front")
            return None
        if page is not None:
            page.last_read = screen  # the page's pixels to the screen's: a touch by --xy maps back
        last = getattr(self, "_last_xml", "")
        info = screen_of(last) if last else None
        return _cdp().page_xml(screen, webview_top(last), info[1] if info else 0)

    def _after_page(self, screen):
        """The screen after an action on the page: its read as a screen
        read, or the screen reader's read when the page says it is
        hidden now (the action opened another app or tab). A window the
        action opened over the page (a permission dialog) is not seen
        here; the next read of the screen sees it (see _page_read)."""
        _log_busy(screen)
        xml = self._page_xml(screen)
        if xml is None:
            with _t("dump rpc (page hidden)"):
                xml = read_screen(self.d)
        return xml

    def _page_read(self, assume_chrome=False, hint=None, loaded=False):
        """The page in Chrome as a screen read, or None (see _page). The
        screen reader's tree lags a finger scroll on a heavy page by
        seconds and comes back empty for a while (espn.com, Oct 4); the
        page itself has the current layout. Not with a window of another
        app over the page (see _Look and window_over: the notification
        shade, a permission dialog): the page can't see it, and the
        screen reader's read is the screen then. loaded: a link just
        opened, read once its rows that say "Loading" are filled (see
        cdp.read_loaded)."""
        page = self._page(assume_chrome, hint)
        if page is None:
            return None
        # the phone's windows, read meanwhile; after a launch the page's
        # app is Chrome, whatever the newest read showed (the launcher:
        # its look took Chrome's own window for one over the page, Oct 5)
        look = _Look(self, front=set(_cdp().CHROME_PACKAGES) if assume_chrome else None)
        try:
            with _t("web read"):
                screen = _cdp().read(page)
                if loaded:
                    screen = _cdp().read_loaded(page, screen)
        except Exception as e:
            self._web = None
            log("reading the page failed (%s); reading the screen" % err_text(e, 100))
            return None
        _log_busy(screen)
        over = look.over()
        if over is not None:
            log("a window over the page (%s %r): the screen reader's read" % over)
            # the page isn't the screen: its proof of being on it ends, and
            # the screen is read now, after that window (a read taken
            # before it was drawn showed Chrome: `settings home` printed
            # Chrome's screen, and the next tap tried the page first, ~4s,
            # Oct 7); _dump and _act_batch take this read (_front_look)
            page.visible_at = 0.0
            try:
                with self._lock:
                    with _t("dump rpc (window over the page)"):
                        xml = read_screen(self.d)
                if has_words(xml):
                    self._front_look = xml
            except Exception as e:
                log("the read after the window over the page failed (%s)" % err_text(e, 80))
            return None
        log("web read: %d rows, %sms in the page" % (len(screen.get("rows") or []), screen.get("ms", "?")))
        return self._page_xml(screen)

    def _remember(self, xml):
        """A read just taken after an action: cached when it has words
        (a blank or wordless read is never reused), the newest read
        either way."""
        words = has_words(xml)
        with self._cache_lock:
            self._gen += 1
            if words:
                self._cache = (_time.monotonic(), xml)
        if words:
            self._mute_since, self._wordless_seen = None, False
        self._last_xml, self._last_xml_t = xml, _time.monotonic()

    def _dump(self, fresh=False, replace=True, page_first=False, hint=None):
        """Hierarchy XML; served from cache if < DUMP_TTL old unless fresh.
        replace=False: a wordless read is kept without replacing the
        server (a `wait` close to its deadline). A page in Chrome is read
        from the page itself (see _page_read). page_first: Chrome was just
        launched with a link, and the page is asked without a native read
        first (that read paid 2s and showed the screen reader's lagging
        tree, Oct 5); "" when the page can't be reached, for the caller
        to read its own way. hint: the link just launched."""
        xml = None if (fresh or page_first) else self._fresh_cache()
        if xml is not None:
            log("dump cache hit")
            return xml
        with self._lock:
            gen, t0 = self._gen, _time.monotonic()
            self._front_look = None
            xml = self._page_read(assume_chrome=page_first, hint=hint, loaded=page_first)
            if xml is None and page_first:
                return ""
            # a page's read comes from Chrome's DevTools, not the screen
            # reader: a page with no words (Reddit's JSON, a wall in a
            # frame) said nothing of that server, which was replaced all
            # the same, a 10s `state` (Oct 7)
            from_page = xml is not None
            if xml is None:
                # the page check's own look at the screen (Chrome left the
                # front), when it took one, is this read
                xml, self._front_look = getattr(self, "_front_look", None), None
            if xml is None:
                with _t("dump rpc"):
                    xml = read_screen(self.d)
            if from_page:
                pass
            elif blank_screen(xml):
                xml, _ = self._fix_blank_read(xml, "blank read")
            elif mute_read(xml):
                xml, _ = self._fix_blank_read(xml, "wordless read", replace)
            words = has_words(xml)
            with self._cache_lock:
                if gen == self._gen and words:
                    self._cache = (t0, xml)  # a blank or wordless read is never reused
            if words:
                self._mute_since, self._wordless_seen = None, False
            self._last_xml, self._last_xml_t = xml, _time.monotonic()
        return xml

    def _fix_blank_read(self, xml, what, replace=True):
        """A read with no app on it (see blank_screen) -> (xml, woke), woke
        being True when the screen was off and has been woken. A read with
        an app's nodes but no words (see mute_read) goes to
        _fix_wordless_read, and woke is False.
        Screen off: wake it (the one thing here that changes the phone)
        and read again. Screen on, showing the system UI's bare window:
        keep the read, the phone is mid-transition (the CLI re-reads a
        thin screen). Screen on, no node at all: the server is broken, so
        restart it (killed on the phone, started fresh) and read again.
        Within the cooldown of an earlier restart it is not restarted
        again: that restart's failure is raised, or, if it worked, the
        empty read is kept as what the phone shows. Raises when the
        server doesn't answer, the screen won't wake or the restart
        fails: the caller decides what a failed read means."""
        if mute_read(xml):
            return self._fix_wordless_read(xml, what, replace), False
        if screen_on(self.d) is False:
            log("%s; the screen is off, waking it" % what)
            self._wake()
            with _t("dump rpc (after wake)"):
                xml = read_screen(self.d)
            if blank_screen(xml) and screen_on(self.d) is False:
                raise RuntimeError("the phone's screen is off and would not wake")
            return xml, True
        if real_screen(xml):
            if xml.count("<node") > 2:
                # the system UI's bars alone with the screen on: a moment
                # of a transition, or, read after read, a server that sees
                # no app; the wordless spell decides (see _fix_wordless_read)
                return self._fix_wordless_read(xml, what, replace), False
            return xml, False  # the system UI's bare window: mid-transition
        if not restart_allowed(_time.monotonic() - self._last_restart):
            if self._restart_error:
                raise RuntimeError("the UI server couldn't be restarted a moment ago: %s"
                                   % self._restart_error)
            log("%s; the server was just restarted, keeping it" % what)
            return xml, False
        log("%s; restarting the server" % what)
        self.connect()
        with _t("dump rpc (after restart)"):
            return read_screen(self.d), False

    def _fix_wordless_read(self, xml, what, replace=True):
        """An app's nodes with no word on them (see mute_read). A screen
        loading reads like this for a few seconds, so the read is kept
        until the spell has lasted MUTE_SPELL_S across reads; then the
        server is replaced (a fresh server read the Oct 4 pages in full),
        once per spell: when the fresh server reads no words either, the
        screen has none (a full-screen video) and reads are kept until
        one with words ends the spell. replace=False keeps the read
        whatever the spell. A wordless read is still a read: whatever
        fails here is logged and the read in hand is returned, and the
        cooldown paces another try."""
        now = _time.monotonic()
        if self._mute_since is None:
            self._mute_since = now
        spell = now - self._mute_since
        if (not replace or self._wordless_seen or spell < MUTE_SPELL_S
                or not restart_allowed(now - self._last_restart)):
            return xml
        log("%s for %.0fs on %d nodes; replacing the server"
            % (what, spell, xml.count("<node")))
        try:
            self.connect(force=True)
            with _t("dump rpc (after restart)"):
                again = read_screen(self.d)
            if blank_screen(again):  # the screen may have gone off meanwhile
                again, _ = self._fix_blank_read(again, what + ", after the restart")
        except Exception as e:
            log("%s; couldn't replace the server (%s); keeping it" % (what, err_text(e)))
            return xml
        if has_words(again):
            return again
        self._wordless_seen = True  # a fresh server agrees: no words on this screen
        return again if real_screen(again) else xml

    def _wake(self):
        """Turn the screen on (one RPC), then give it a moment to draw."""
        try:
            self.d.jsonrpc.wakeUp()
        except Exception as e:
            raise RuntimeError("the phone's screen is off and wakeUp failed (%s)" % err_text(e, 80))
        _time.sleep(0.4)

    def _wake_quietly(self):
        """The screen on before the CLI launches something: one wakeUp (a
        no-op with the screen on; 500ms on the phone when it wakes it). A
        failure is logged, not raised: the look after the launch wakes
        an off screen then."""
        try:
            with self._lock:
                self.d.jsonrpc_call("wakeUp", [], timeout=WAKE_RPC_S)
        except Exception as e:
            log("the wake before a launch failed (%s)" % err_text(e, 80))

    def _front_read(self):
        """One read of what is in front now, the screen woken first when it
        is off (a blank read, and the phone says so); remembered when it
        has words. "" when the screen can't be read."""
        try:
            with self._lock:
                with _t("dump rpc (front check)"):
                    xml = read_screen(self.d)
                if blank_screen(xml) and screen_on(self.d) is False:
                    log("the screen is off; waking it")
                    self._wake()
                    with _t("dump rpc (front check)"):
                        xml = read_screen(self.d)
        except Exception as e:
            log("the look at the screen failed (%s)" % err_text(e, 80))
            return ""
        if has_words(xml):
            self._remember(xml)
            self._front_cache = (xml, screen_of(xml))
        return xml

    def _relink(self, why):
        """The phone back on adb, with no restart: the link through the
        tunnel dropped (after hours idle, Oct 6) while the server on the
        phone is fine. The server's handle is kept: it names the phone by
        its address, not by adb's link, and its settings live in the
        server (asking for a new handle and setting them again cost
        1-2.5s on Muse's box). Raises when the phone doesn't come back."""
        failed = getattr(self, "_relink_failed_at", None)
        if failed is not None and _time.monotonic() - failed < RELINK_RETRY_S:
            # tried a moment ago and the phone didn't come back: say so at
            # once, and the CLI recovers it its own way (`ensure`)
            raise RuntimeError("the phone is gone from adb (%s); a reconnect %.0fs ago failed (%s)"
                               % (why, _time.monotonic() - failed, LINK_WHY[0]))
        log("the phone is gone from adb (%s); reconnecting it" % why)
        _KEEPALIVE.close()
        with _t("adb reconnect"):
            back = reconnect_device()
        if not back:
            self._relink_failed_at = _time.monotonic()
            raise RuntimeError("the phone is gone from adb (%s) and did not come back "
                               "on a reconnect (adb connect %s: %s)" % (why, TARGET, LINK_WHY[0]))
        self._relink_failed_at = None
        self.invalidate()

    def _reconnect(self):
        """`burner ensure`: the server is checked, and replaced when it
        can't read, or reads no words while the screen is in a wordless
        spell no fresh server has confirmed (what the guide sends an
        assistant here for)."""
        unconfirmed_spell = self._mute_since is not None and not self._wordless_seen
        try:
            if server_works(self.d, words=unconfirmed_spell):
                return True
            raise RuntimeError("server can't read the screen")
        except Exception:
            log("server check failed, reconnecting")
            try:
                self.connect()
                return True
            except Exception as e:
                log("reconnect failed:", e)
                return False

    CACHED_READ_S = 10.0  # "dump cached": the newest read, while this young

    def cmd_dump(self, arg):
        # "dump fresh" bypasses the cache; "dump cached" is the newest read
        # with words, CACHED_READ_S old at most, and nothing otherwise: no
        # round trip to the phone (a chain looks between its steps for a
        # question the phone asks).
        if arg.strip() == "cached":
            young = _time.monotonic() - self._last_xml_t < self.CACHED_READ_S
            return self._last_xml.encode() if young and has_words(self._last_xml) else b""
        if arg.strip() == "native":
            # the screen reader's read now, never the page's: a keyboard's
            # window and Chrome's own address bar are only there (the CLI's
            # check before Enter, review of Oct 7); an off screen is woken
            with self._lock:
                with _t("dump rpc (native)"):
                    xml = read_screen(self.d)
                if blank_screen(xml):
                    xml, _woke = self._fix_blank_read(xml, "blank native read")
            return (xml or "").encode()
        if arg.strip() == "page" or arg.startswith("page "):
            # Chrome was just launched with a link ("page <link>": that
            # link, the hint for finding its tab): the page, asked at once
            link = arg[5:].strip()
            if link:
                self._opened, self._opened_at = link, _time.monotonic()
            return self._dump(fresh=True, page_first=True, hint=link).encode()
        return self._dump(fresh=(arg.strip() == "fresh")).encode()

    def cmd_text(self, arg):
        """arg: JSON {"from": n, "max": n}. The page's words in Chrome (see
        cdp.page_text), as JSON; "not a page" elsewhere (the CLI prints
        the screen's rows uncut then)."""
        p = json.loads(arg) if arg.strip() else {}
        page = self._page()
        if page is None:
            # a plain miss: no retry, which would cost a round trip to the
            # phone for the same answer (review, Oct 7)
            raise U2NotFound("not a page")
        with self._lock:
            try:
                with _t("web text"):
                    r = _cdp().page_text(page, int(p.get("from", 0)), int(p.get("max", 20000)))
            except Exception as e:
                self._web = None
                raise RuntimeError("the page's text couldn't be read (%s)" % err_text(e, 100))
        return json.dumps(r).encode()

    def cmd_tabs(self, _):
        """Chrome's tabs as DevTools lists them: JSON [{"id", "url",
        "title"}], for `burner tabs`."""
        tabs = _cdp().pages(getattr(self.d, "_dev", None))
        return json.dumps([{"id": t.get("id", ""), "url": t.get("url", ""), "title": t.get("title", "")}
                           for t in tabs]).encode()

    def cmd_invalidate(self, arg):
        if arg.strip() == "acted":
            # the CLI acted its own way (scrcpy, adb): a control tapped by
            # words before it no longer opened what is on the screen (a
            # read's invalidate, `dump --fresh`, keeps it: review of Oct 7)
            self._last_tap = None
        self.invalidate()
        return b""

    def cmd_exists(self, arg):
        # One dump, local substring check on text + content-desc.
        # Single roundtrip for both hit and miss (~1s); no 3.6s tail.
        # Match against XML-escaped form (&, <, " are escaped in dumps).
        import xml.sax.saxutils as saxutils
        h = self._dump()
        if blank_screen(h):
            h = self._dump(fresh=True)  # a blank read says nothing about `arg`
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
                except Exception as e:
                    # The server looks the node up again for a selector
                    # click and threw NullPointerException on a web node
                    # (Chrome, Oct 4): tier 3 taps by coordinates instead.
                    # The head of the message names the error; the tail of
                    # a server error is its Java stack.
                    log("click_text selector failed (%s: %s); tapping by coordinates"
                        % (type(e).__name__, " ".join(str(e).split())[:120]))
                    ok = False
                if ok:
                    self.invalidate()
                    return json.dumps({"clicked": True, "text": arg}).encode()
        n = find_node(self._dump(), arg, names=True)  # a click: only rows the words name
        if n is None:
            raise U2NotFound("no such text: %r" % arg)
        self.invalidate()
        x, y = n["center"]
        return json.dumps({"x": x, "y": y, "text": n["text"], "desc": n["desc"],
                           "bounds": n["bounds"]}).encode()

    def cmd_wait_for(self, arg):
        """arg: JSON {"text", "timeout" (s), "absent" (bool), "exact"
        (bool)}. Polls in the daemon every POLL_S, taking self._lock only
        for each dump RPC so other clients interleave. Match = exact label
        (text/content-desc, case-insensitive), else substring unless
        exact ("Open" is not "OpenAI"). Returns JSON with the matched
        node (or {"gone": true}); U2NotFound on timeout. The last dump stays
        cached, so an immediate `burner tap` needs no further lookup RPC."""
        POLL_S = 0.25
        p = json.loads(arg)
        text, absent, exact = p["text"], bool(p.get("absent")), bool(p.get("exact"))
        t0 = _time.monotonic()
        deadline = t0 + float(p.get("timeout", 30))
        polls, healed, fresh = 0, False, False
        page = self._page()
        if page is not None:
            # A page in Chrome is asked for the words (cdp.find): the
            # whole document, as the screen reader's tree held rows
            # below the fold too; a read of the page when they are there.
            fails = 0
            while True:
                polls += 1
                # A navigation under way (a tap that submitted a form) is
                # waited out first: a page mid-navigation answers a
                # question only once the new document is up.
                page.listen(0.05)
                if page.loading:
                    page.wait_parsed(min(2.0, max(0.1, deadline - _time.monotonic())))
                look = _Look(self)  # the phone's windows, read meanwhile
                try:
                    with _t("web find"):
                        n, screen = _cdp().find_read(page, text, webview_top(self._last_xml), exact)
                except Exception as e:
                    fails += 1
                    if fails > 1:
                        self._web = None
                        log("the page couldn't be asked (%s); the screen reader" % err_text(e, 100))
                        break
                    page.loading = True  # likely mid-navigation: once more after it
                    page.wait_parsed(min(2.0, max(0.1, deadline - _time.monotonic())))
                    continue
                over = look.over()
                if over is not None:
                    # a window of another app over the page (a dialog, the
                    # shade): the words are the screen reader's to find
                    log("a window over the page (%s %r): the wait moves to the screen reader" % over)
                    fresh = True
                    break
                if screen and screen.get("vis") == "hidden":
                    # the page left the front (the tap before opened an
                    # app): what is in front is the screen reader's to say
                    self._page_xml(screen)  # ends the page's proof
                    fresh = True
                    break
                waited = int((_time.monotonic() - t0) * 1000)
                if absent and not n.get("found"):
                    return json.dumps({"gone": True, "waited_ms": waited, "polls": polls}).encode()
                if not absent and n.get("found"):
                    # the read came in the poll's round trip
                    xml = self._page_xml(screen) if screen else self._page_read()
                    if xml is not None:
                        with self._lock:
                            self._remember(xml)
                    n = dict(n, waited_ms=waited, polls=polls)
                    return json.dumps(n).encode()
                if _time.monotonic() + POLL_S > deadline:
                    # the page as the last poll saw it is the newest read
                    # (a failed wait's evidence showed the page from before
                    # the wait, review of Oct 7)
                    xml = self._page_xml(screen) if screen else None
                    if xml is not None:
                        with self._lock:
                            self._remember(xml)
                    raise U2NotFound("timeout waiting for %r" % text)
                _time.sleep(POLL_S)
        while True:
            polls += 1
            try:
                # The first poll may use a fresh cache. Close to the
                # deadline a wordless read isn't worth a server replacement.
                xml = self._dump(fresh=fresh,
                                 replace=(deadline - _time.monotonic()) >= 15)
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
            n = find_node(xml, text, fuzzy=not exact)
            waited = int((_time.monotonic() - t0) * 1000)
            # a blank read shows nothing, so it can't show `text` gone; nor
            # can a wordless one, until a fresh server read it wordless too
            if (absent and n is None and not blank_screen(xml)
                    and (not mute_read(xml) or self._wordless_seen)):
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
        if self._page() is not None:
            # A page in Chrome: the screen reader's idle means nothing
            # there (its tree lags the page), and a page read is current.
            self.invalidate()
            return b"0"
        with self._lock:
            with _t("idle rpc"):
                self.d.jsonrpc.waitForIdle(timeout)
        self.invalidate()
        return str(int((_time.monotonic() - t0) * 1000)).encode()

    IME_HINTS = ("inputmethod", "keyboard", "honeyboard", "swiftkey", ".ime.", "latinime")

    SCREEN_FROM_READ_S = 20.0  # a read this young still says what the screen is

    def cmd_screen(self, _):
        """"w h pkg [kbd]": the screen at its current rotation and the app
        in front. From the newest read while it is under
        SCREEN_FROM_READ_S old (no round trip: a scroll right after a read
        paid 0.4s for this on a busy web page, Oct 4), else from one small
        deviceInfo RPC (a full dump costs ~0.8s). Plus "kbd" when the
        newest read showed a keyboard (a swipe must then start above it:
        on the keys, Gboard glide-types)."""
        info = None
        if _time.monotonic() - self._last_xml_t < self.SCREEN_FROM_READ_S:
            info = screen_of(self._last_xml)
        if info is None:
            with self._lock:
                i = self.d.jsonrpc.deviceInfo()
            info = (i["displayWidth"], i["displayHeight"], i.get("currentPackageName") or "")
        out = "{} {} {}".format(*info).strip()
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
        # vision models that read it shrink it further anyway. A .png name
        # gets the same capture, re-encoded here: the bytes crossing the link
        # set the time (full size at quality 100: 2.2-4.3s; 0.6 at 90:
        # 2.1-2.9s; 0.6 at 75: 1.0-1.6s, Muse 2026-10-06), and assistants
        # name their shots .png out of habit, not for the pixels.
        with self._lock:
            with _t("shot rpc"):
                data = self.d.jsonrpc.takeScreenshot(0.6, 75)
        if not data:
            raise RuntimeError("takeScreenshot returned nothing")
        if png:
            import io
            from PIL import Image
            buf = io.BytesIO()
            Image.open(io.BytesIO(base64.b64decode(data))).save(
                buf, "PNG", compress_level=1)
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

    def _batch(self, calls, timeout=45.0):
        """Several JSON-RPC calls in one HTTP round trip; the server runs
        them in order. One result per call (an Exception for a failed one)."""
        body = [{"jsonrpc": "2.0", "id": i + 1, "method": m, "params": p}
                for i, (m, p) in enumerate(calls)]
        resp = _KEEPALIVE.request(self.d._dev, self.d._device_server_port,
                                  "POST", "/jsonrpc/0", data=body, timeout=timeout,
                                  replay=not any(m in ACTION_METHODS for m, _ in calls))
        return batch_results(resp.json(), len(calls))

    def cmd_act(self, arg):
        """arg: JSON {"tap": [x, y] | "key": code | "set_text": "..."
        (one or none), "idle": ms}. One round trip: the action, a wait for
        the UI to go quiet (`idle` ms at most) and a read, whose XML is
        returned and cached. Never replayed after an error: the action may
        have happened. "act failed after sending: ..." means just that."""
        spec = json.loads(arg) if arg.strip() else {}
        if "open" in spec or "scroll" in spec or "scroll_to" in spec:
            self._last_tap = None  # another action: see _type_into_field
        if "open" in spec:
            # A link opened in the page in Chrome (cdp.navigate): its
            # current tab loads it. No tab for every link, no first
            # contact with a new one. "launched": the CLI just brought
            # Chrome up (no read shows it yet): the page is contacted the
            # way it is after a launch, the link the hint. Elsewhere the
            # CLI launches the link (remembered: the tab it lands in is
            # the one to look for).
            link = str(spec["open"])
            page = (self._page(assume_chrome=True, hint=link) if spec.get("launched")
                    else self._page())
            if page is None:
                self._opened, self._opened_at = link, _time.monotonic()
                if not spec.get("launched"):
                    # the CLI brings Chrome up next: the screen woken first
                    # (Chrome came up behind an off screen; the look after
                    # found it off and woke it, and the tab refused DevTools
                    # for a try more: an 11s open, Oct 7)
                    self._wake_quietly()
                raise RuntimeError("act not sent: not a page")
            with self._lock:
                self.invalidate()
                try:
                    with _t("web open"):
                        r = _cdp().navigate(page, spec["open"], int(spec.get("idle", 1000)))
                except _cdp().NotSent as e:
                    self._web = None
                    raise RuntimeError("act not sent: the page couldn't be asked (%s)" % e)
                except Exception as e:
                    self._web = None
                    raise RuntimeError("act failed after sending: %s" % err_text(e, 120))
                xml = self._after_page(r["screen"])
                self._remember(xml)
            return xml.encode()
        if "scroll_to" in spec:
            # `scroll --to` on a page in Chrome: the page finds the words,
            # below the screen too, and brings them into view (cdp.scroll_to)
            page = self._page()
            if page is None:
                raise RuntimeError("act not sent: not a page")
            with self._lock:
                self.invalidate()
                try:
                    with _t("web scroll to"):
                        r = _cdp().scroll_to(page, str(spec["scroll_to"]), spec.get("direction", "down"))
                except Exception as e:
                    self._web = None
                    raise RuntimeError("act failed after sending: %s" % err_text(e, 120))
                log("web scroll to %r: %s%s" % (spec["scroll_to"], {
                    "1": "in view", "other": "only on the other side" + (", the page's end close by" if r.get("near")
                                                                          else ""),
                    "hidden": "only where it isn't shown", "0": "not on the page"}.get(r["found"], r["found"]),
                    ", moved %s px" % r["moved"] if r["moved"] else ""))
                xml = self._after_page(r["screen"])
                self._remember(xml)
            return xml.encode()
        if "scroll" in spec:
            # A page in Chrome scrolls itself (cdp.scroll): a finger swipe
            # leaves the screen reader's positions behind for seconds.
            # Elsewhere the CLI swipes.
            page = self._page()
            if page is None:
                raise RuntimeError("act not sent: not a page")
            with self._lock:
                self.invalidate()
                try:
                    with _t("web scroll"):
                        r = _cdp().scroll(page, spec.get("scroll", "down"),
                                          int(spec.get("times", 1)),
                                          idle_ms=int(spec.get("idle", 500)),
                                          grow_ms=int(spec.get("grow", 0)))
                except Exception as e:
                    self._web = None
                    raise RuntimeError("act failed after sending: %s" % err_text(e, 120))
                # how far, by what, and whether the page is held (a pop-up
                # over it): the ten scrolls for nothing on allrecipes.com
                # left no word why (Oct 7)
                if r.get("moved") is None:
                    log("web scroll: the page's scroll didn't answer")
                else:
                    log("web scroll: moved %s px (%s)%s%s" % (r.get("moved"), r.get("scroller") or "nothing",
                                                             ", the page held" if r.get("held") else "",
                                                             ", after the page grew" if r.get("grew") else ""))
                xml = self._after_page(r["screen"])
                self._remember(xml)
            return xml.encode()
        if "tap_label" in spec and self._page() is not None:
            # A page in Chrome: the element with these words is found,
            # scrolled into view and touched by the page itself (cdp.tap),
            # which has the current layout.
            label = spec["tap_label"]
            self._last_tap = None  # a page's field is filled by its label (cdp.fill)
            with self._lock:
                self.invalidate()
                try:
                    with _t("web tap"):
                        r = _cdp().tap(self._web, label, spec.get("index"),
                                       int(spec.get("idle", 2000)))
                except _cdp().NotSent as e:
                    raise RuntimeError("act not sent: the page couldn't be asked (%s)" % e)
                except Exception as e:
                    self._web = None
                    raise RuntimeError("act failed after sending: %s" % err_text(e, 120))
                if r.get("found") and r.get("count", 1) == 1 and r.get("at"):
                    # where the touch went: a tap that opened nothing
                    # left nothing to go on without it (airbnb.com, Oct 5)
                    at = r["at"]
                    log("web tap: %s on <%s> at %.0f,%.0f%s%s%s" % (
                        r.get("how", "?"), r.get("tag") or "?", at[0] or 0, at[1] or 0,
                        " laid over the words" if r.get("over") else "",
                        ", scrolled into view" if r.get("moved") else "",
                        ", under a cover" if r.get("covered") else ""))
                if not r.get("found"):
                    # Chrome's own prompts (a permission ask, "Save
                    # password?") are not on the page; the screen reader
                    # has them: one read there, and the row tapped where it is
                    center = self._native_row(label)
                    if center is None:
                        raise RuntimeError("act not sent: not on the page")
                    native = {"tap": list(center), "idle": spec.get("idle", 1200)}
                    if spec.get("quiet"):
                        native["quiet"] = True
                    return self._act_batch(native)
                if r.get("count", 1) != 1:
                    raise RuntimeError("act not sent: %d rows read %r%s" % (
                        r["count"], label, " (%s)" % ", ".join(r["tags"]) if r.get("tags") else ""))
                xml = self._after_page(r["screen"])
                self._remember(xml)
            return xml.encode()
        if "set_text" in spec and spec.get("field") and self._page() is not None:
            # Text into the page's field with this label, by the page
            # (cdp.fill): found by its label, the value set on that very
            # element. One op for `type --field`.
            with self._lock:
                self.invalidate()
                try:
                    with _t("web fill"):
                        r = _cdp().fill(self._web, spec["field"], str(spec["set_text"]),
                                        spec.get("index"))
                except _cdp().NotSent as e:
                    raise RuntimeError("act not sent: %s" % e)
                except _cdp().NotDone as e:
                    raise RuntimeError("act failed after sending: %s" % e)
                except Exception as e:
                    self._web = None
                    raise RuntimeError("act failed after sending: %s" % err_text(e, 120))
                if r.get("how", "").startswith("tap+"):
                    log("web fill: %r is not a field; tapped it, typed into the field %s"
                        % (spec["field"], {"tap+focus": "that took the focus", "tap+label": "with that label",
                                           "tap+only": "in view"}.get(r["how"], "found")))
                if not r.get("found"):
                    raise RuntimeError("act not sent: no field labelled %r on the page" % spec["field"])
                if r.get("count", 1) != 1:
                    raise RuntimeError("act not sent: %d fields read %r%s" % (
                        r["count"], spec["field"], " (%s)" % ", ".join(r["tags"]) if r.get("tags") else ""))
                xml = self._after_page(r["screen"])
                self._remember(xml)
            return xml.encode()
        if "set_text" in spec and self._page() is not None:
            # Text into a page's focused field, by the page (cdp.type_text).
            with self._lock:
                self.invalidate()
                try:
                    with _t("web type"):
                        r = _cdp().type_text(self._web, str(spec["set_text"]),
                                             int(spec.get("idle", 800)))
                except _cdp().NotSent as e:
                    raise RuntimeError("act not sent: %s" % e)
                except _cdp().NotDone as e:
                    raise RuntimeError("act failed after sending: %s" % e)
                except Exception as e:
                    self._web = None
                    raise RuntimeError("act failed after sending: %s" % err_text(e, 120))
                if r.get("only"):
                    log("web type: nothing had the focus; the one text field in view took the text")
                xml = self._after_page(r["screen"])
                self._remember(xml)
            return xml.encode()
        if "tap" in spec and self._page() is not None:
            # A touch by --xy or a snap handle on a page in Chrome: the page
            # takes it at the point itself, with the read in the same round
            # trip (cdp.touch_at); the screen reader's click paid a pause
            # read, an idle wait and a page read after (2.9-3.6s a tap on
            # the ESPN pages, Oct 5). A point outside the WebView (the
            # address bar) is the screen reader's, below. The phone's windows
            # are looked at meanwhile: a dialog that came up since the read
            # the point was taken from is said (the touch went under it).
            page, last = self._web, self._last_xml
            shot, rect = getattr(page, "last_read", None), webview_rect(last)
            x, y = int(spec["tap"][0]), int(spec["tap"][1])
            if shot and rect and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                with self._lock:
                    self.invalidate()
                    look = _Look(self)
                    try:
                        with _t("web touch"):
                            r = _cdp().touch_at(page, x, y, shot, webview_top(last),
                                                int(spec.get("idle", 1200)))
                    except _cdp().NothingSent as e:
                        raise RuntimeError("act not sent: the page couldn't be asked (%s)" % e)
                    except Exception as e:
                        self._web = None
                        raise RuntimeError("act failed after sending: %s" % err_text(e, 120))
                    over = look.over()
                    if over is not None:
                        with _t("dump rpc (window over)"):
                            xml = read_screen(self.d)
                        self._remember(xml)
                        raise RuntimeError("act failed after sending: a window over the page (%s %r) "
                                           "has the screen; the touch went under it" % over)
                    xml = self._after_page(r["screen"])
                    self._remember(xml)
                if spec.get("quiet"):
                    return b"ok"
                return xml.encode()
        if "tap_label" in spec:
            if spec.get("index") is not None:
                # Which of several rows: the caller reads and plans.
                raise RuntimeError("act not sent: --index needs a read")
            # Tap the row with this label. When the newest read (the
            # screen the assistant was shown, BY_WORDS_S old at most) has
            # the control once, whole and clear, the phone finds it by
            # its words at tap time and taps it in the round trip of the
            # wait and the read: a read first is a round trip of its own
            # (0.4-0.75s from afar, Oct 5). A control the phone no longer
            # finds (the screen changed) is tapped where it is now, on a
            # fresh read, not the assistant's. (A web page's rows report
            # their old place for a moment after a scroll, espn.com Oct
            # 4: the tap at a row's old place hit the link below it.)
            # When the row isn't known to be still, because it moved
            # since the assistant's read or that read didn't have it
            # whole, it is read until two reads agree on its place, three
            # at most (a bar that slides in after a scroll pushes the rows
            # down for a moment: the tap meant for Box Score opened
            # Standings). The label must be on the read exactly once and
            # whole; anything else is "not sent": the caller reads the
            # screen and taps by coordinates instead.
            spec = dict(spec)
            label = spec.pop("tap_label")
            spec["tapped_label"] = label  # remembered once the tap is done (see _type_into_field)
            before = self._last_xml
            tried = None
            if before and _time.monotonic() - self._last_xml_t < BY_WORDS_S:
                try:
                    node, alt = label_node(before, label)
                    sel = selector_for(node, alt)
                    # the phone clicks the first row its selector finds:
                    # only when that is the one row with these exact words
                    # (a search box holding "lofi" above the suggestion
                    # "lofi" got the tap, the review of Oct 6)
                    if selector_matches(before, sel) == 1:
                        spec["tap_selector"] = tried = sel
                        spec["words"] = label
                except RuntimeError:
                    pass
            if "tap_selector" in spec:
                try:
                    return self._act_batch(spec)
                except _NotThere as e:
                    spec.pop("tap_selector")
                    log("%r not found by its words (%s); reading afresh" % (label, e))
            try:
                with _t("tap read"):
                    xml = self._dump(fresh=True)
            except Exception as e:
                raise ActNotSent("act not sent: the read before it failed (%s)"
                                 % err_text(e, 100))
            try:
                node, alt = label_node(xml, label)
            except RuntimeError as e:
                raise RuntimeError("act not sent: %s" % e)
            center = tuple(node["center"])
            fresh = selector_for(node, alt)
            if not node["web"] and fresh != tried and selector_matches(xml, fresh) == 1:
                # a native row on the read just taken: the phone finds it
                # by its words at tap time, where it is then, with no
                # second read to see it hold its place (2.7-3.1s for a tap
                # whose row came after the assistant's read, Oct 6)
                spec["tap_selector"], spec["words"] = fresh, label
                try:
                    return self._act_batch(spec)
                except _NotThere as e:
                    spec.pop("tap_selector")
                    log("%r not found by its words on the read just taken (%s)" % (label, e))
            try:
                old = label_target(before, label)
            except RuntimeError:
                old = None
            reads = 1
            while old != center and reads < 3:
                log("%r %s; reading again" % (
                    label, "moved (%s -> %s)" % (old, center) if old
                    else "wasn't whole on the last read"))
                old = center
                try:
                    with _t("tap read (again)"):
                        xml = self._dump(fresh=True)
                    center = label_target(xml, label)
                except Exception as e:
                    raise RuntimeError("act not sent: %s" % err_text(e, 100))
                reads += 1
            spec["tap"] = list(center)
        if "set_text" in spec and spec.get("field"):
            return self._type_into_field(spec)
        if "set_text" in spec and not spec.get("field_selector"):
            # no field named: the focused one; with none focused on the
            # newest read and one text field on it, that one, in the same
            # trip (Translate's box: the focused selector found nothing, and
            # the slow way took four more trips, 5.2s, Oct 7)
            lone = self._lone_field()
            if lone is not None:
                spec["field_selector"] = field_selector(lone.get("rid", ""))
        return self._act_batch(spec)

    def _lone_field(self):
        """The one enabled text field on the newest read (BY_WORDS_S old
        at most), when none has the focus; None otherwise."""
        if not self._last_xml or _time.monotonic() - self._last_xml_t >= BY_WORDS_S:
            return None
        try:
            fields = [n for n in iter_nodes(self._last_xml) if n["field"] and n["enabled"]]
        except Exception:
            return None
        if any(n["focused"] for n in fields) or len(fields) != 1:
            return None
        return fields[0]

    NO_FIELD_TAP = "act not sent: --field on a native screen needs a tap on the field first"

    def _type_into_field(self, spec):
        """`type --field` on a native screen, where setText goes to
        whatever field has the focus. With the field on the newest read
        (the assistant's, BY_WORDS_S old at most): focused there, the text
        goes in at once, into that very field; unfocused there, a read
        now, since the read after a tap can come before the field takes
        the focus (Settings' search box, Oct 6), and on it the text goes
        in at once when the field has the focus, or the field is tapped
        and the text typed in one batch when it is still in its place. A
        tap of the field, a pause and the typing, a trip each, took 4.1s
        (YouTube's search box, Oct 6). Anything else is "not sent": the
        CLI taps the field by its own lookup first, so the typing isn't
        reported as going into this field when another has the focus."""
        spec = dict(spec)
        label = spec.pop("field")
        young = self._last_xml and _time.monotonic() - self._last_xml_t < BY_WORDS_S
        before = field_node(self._last_xml, label) if young else None
        if before is not None and before["focused"] and before.get("rid"):
            # its resource id pins it: a field that lost the focus since is
            # not found by the phone, and nothing is typed
            return self._type_at_once(spec, before)
        # a read now: the field may have come since the newest read (that
        # read, taken as a tap's screen was still sliding, showed Settings'
        # search bar, and the box was typed into 7.5s later, Oct 6); the
        # CLI's tap of the field, when it comes to that, goes by this read
        try:
            with _t("field read"):
                xml = self._dump(fresh=True)
        except Exception as e:
            raise ActNotSent("act not sent: the read before it failed (%s)" % err_text(e, 100))
        now = field_node(xml, label)
        if now is not None and now["focused"]:
            return self._type_at_once(spec, now)
        if now is None or before is None or now["center"] != before["center"]:
            # one more look a moment later: a box a tap opens can come after
            # the reads (Settings' search: the tap's read showed the page
            # before it, the read 0.5s later no box, and the CLI's own taps
            # and typings took 6.2s, Oct 7); and a field seen on one read
            # alone is tapped only once it is seen in the same place twice
            _time.sleep(FIELD_AGAIN_S)
            try:
                with _t("field read (again)"):
                    xml2 = self._dump(fresh=True)
            except Exception as e:
                raise ActNotSent("act not sent: the read before it failed (%s)" % err_text(e, 100))
            again = field_node(xml2, label)
            if again is not None and again["focused"]:
                return self._type_at_once(spec, again)
            before, now, xml = now, again, xml2
        if now is not None and before is not None and now["center"] == before["center"]:
            spec["tap_first"] = list(now["center"])
            spec["field_selector"] = focused_field_selector(now.get("rid", ""))
            return self._act_batch(spec)
        tapped, tapped_at = getattr(self, "_last_tap", None) or ("", -1e9)
        opened = (plain_words(tapped).lower() == plain_words(label).lower()
                  and _time.monotonic() - tapped_at < TAP_OPENED_S)
        lone = lone_focused_field(xml, label) if now is None and opened else None
        if lone is not None:
            # the control with these words was just tapped and is gone: it
            # opened this box (Play's "Search or ask Play" opens "Search
            # apps & games", Oct 6), and the one box on the screen, focused,
            # takes the text. Never without that tap: a password named by
            # a field that isn't on the screen must not go into a search box
            log("no row reads %r; typing into the one field, which has the focus (%s)"
                % (label, lone.get("rid") or lone["cls"]))  # its id: its text is what was typed
            return self._type_at_once(spec, lone)
        raise RuntimeError(self.NO_FIELD_TAP)

    def _type_at_once(self, spec, node):
        """The text into this focused field in one batch: "not sent" when
        the field doesn't have the focus after all (the phone found no
        such field, and nothing was typed)."""
        spec["field_selector"] = focused_field_selector(node.get("rid", ""))
        try:
            return self._act_batch(spec)
        except RuntimeError as e:
            if "UiObjectNotFound" in str(e) or "-32002" in str(e):
                raise RuntimeError(self.NO_FIELD_TAP)
            raise

    def _native_row(self, label):
        """The centre of the row with these words on a fresh read by the
        screen reader (not the page: Chrome's own prompts live only
        there), or None. The read is the newest one known."""
        try:
            with _t("dump rpc (native row)"):
                xml = read_screen(self.d)
        except Exception as e:
            log("the screen reader couldn't be asked for %r (%s)" % (label, err_text(e, 80)))
            return None
        if has_words(xml):
            self._remember(xml)
        try:
            return label_target(xml, label)
        except RuntimeError:
            return None

    def _act_batch(self, spec):
        """The act's calls (see act_calls) in one round trip, and what
        came back: the read after the action, cached, or "ok" for a
        quiet step. Raises _NotThere when a tap by words found no control
        (nothing was tapped), RuntimeError otherwise."""
        calls = act_calls(spec)
        acted = [i for i, (m, _) in enumerate(calls) if m in ACTION_METHODS]
        timeout = int(spec.get("idle", 2000)) / 1000.0 + 20
        last, last_t = getattr(self, "_last_xml", ""), getattr(self, "_last_xml_t", 0.0)
        before = last if last and _time.monotonic() - last_t < BY_WORDS_S else None
        with self._lock:
            self.invalidate()
            with _t("act batch" + (" (by words)" if "tap_selector" in spec else "")):
                try:
                    results = self._batch(calls, timeout=timeout)
                except StreamUnavailable as e:
                    # no stream to the server could be opened: nothing went
                    # out, and the caller acts its own way
                    raise ActNotSent("act not sent: the UI server couldn't be reached (%s)" % e)
                except LinkDead as e:
                    if not e.sent:
                        raise ActNotSent("act not sent: the UI server couldn't be reached (%s)" % e)
                    raise RuntimeError("act failed after sending: %s" % str(e)[:120])
                except Exception as e:
                    raise RuntimeError("act failed after sending: %s" % str(e)[:120])
            if acted and isinstance(results[acted[0]], Exception):
                err = results[acted[0]]
                if "tap_selector" in spec and ("UiObjectNotFound" in str(err) or "-32002" in str(err)):
                    raise _NotThere(err_text(err, 80))  # no such control: nothing was tapped
                # any other error (a NullPointerException on a web node, Oct
                # 4) may have come after the touch went in: no second tap
                raise RuntimeError("act failed after sending: %s" % err)
            later = [results[i] for i in acted[1:] if isinstance(results[i], Exception)]
            if later:
                # a field tapped, then its typing failed: when no field had
                # the focus, the CLI types its other ways (it taps the field
                # by its label first; a second tap on a field is harmless)
                err = later[0]
                gone = "UiObjectNotFound" in str(err) or "-32002" in str(err)
                raise RuntimeError("act failed after sending: the field was tapped; %s (%s)"
                                   % ("no editable field had the focus" if gone else "the typing failed",
                                      err_text(err, 120)))
            if any(calls[i][0] == "setText" and results[i] is False for i in acted):
                # the phone's setText answers false when the field didn't take
                # the text: nothing was typed, and the CLI types another way
                raise RuntimeError("act failed after sending: no editable field took the text "
                                   "(the phone's setText said no)")
            if "tap_selector" in spec and acted:
                n = results[acted[0] - 1]
                if isinstance(n, int) and n > 1:
                    # the read was BY_WORDS_S old: rows with the words came
                    # since, and the phone tapped the first; said, not hidden
                    raise RuntimeError("act failed after sending: %d rows read %r now; the first was tapped"
                                       % (n, spec.get("words", "")))
            if acted and calls[0][0] == "wakeUp" and isinstance(results[0], Exception):
                # The action went to a screen that may be off: dropped, then.
                raise RuntimeError("act failed after sending: the wake before it failed "
                                   "(%s), so it may have been dropped" % results[0])
            xml = results[-1]
            if spec.get("tapped_label"):
                # a control tapped by its words: a `type --field` with them
                # next types into the box it opened (see _type_into_field)
                self._last_tap = (spec["tapped_label"], _time.monotonic())
            elif acted:
                # any other action: what the tapped control opened may be
                # gone (BACK, a tap elsewhere), review of Oct 7
                self._last_tap = None
            if not acted and calls and calls[0][0] == "wakeUp" and isinstance(results[0], Exception):
                log("the wake before the read failed (%s)" % err_text(results[0], 80))
            if spec.get("quiet"):
                # a chained step: the action landed and the UI went quiet;
                # no screen goes back, and the read taken is the newest one
                # known (the next step finds its control by words on it);
                # a blank one isn't, so that step reads afresh
                if isinstance(xml, str) and has_words(xml):
                    self._remember(xml)
                else:
                    self._last_xml_t = 0.0
                return b"ok"
            if isinstance(xml, Exception) or not xml:
                raise RuntimeError("act failed after sending: no read (%s)" % xml)
            if not has_words(xml) and not sleeps_the_screen(spec):
                what = "blank" if blank_screen(xml) else "wordless"
                # The action happened; only the read is repeated. A read
                # that fails here must not read as "not sent" to the CLI.
                try:
                    xml, woke = self._fix_blank_read(xml, what + " read after the action")
                except Exception as e:
                    raise RuntimeError("act failed after sending: the read after it "
                                       "failed (%s)" % err_text(e, 100))
                if woke and acted:
                    raise RuntimeError("act failed after sending: the screen was off, so "
                                       "it was probably dropped; the screen is on now")
            looked = before and acted and "set_text" not in spec
            again = False
            if looked and not sleeps_the_screen(spec) and not chrome_in(xml):
                words = screen_words(xml), screen_words(before)
                # the focus moved into a text field: the tap's own doing,
                # there to see (a second look cost a read, review Oct 7)
                into_field = focused_field(xml) not in (None, focused_field(before))
                again = (screen_sig(xml) == screen_sig(before) or half_drawn_after(xml, before)
                         or (not into_field and words[0] is not None and words[0] == words[1])
                         # a tap by words that left its row there and brought
                         # nothing new: the screen fading out for the next one
                         # (Settings' search, Oct 7)
                         or (not into_field and spec.get("tapped_label") and nothing_new(xml, before, words)
                             and find_node(xml, spec["tapped_label"], fuzzy=False) is not None))
            if again:
                xml = self._relook(xml)
            elif looked:
                # changed, and no second look: how (a tap that opened
                # another app's screen read as the page before it, changed
                # in some rows, and nothing said how, Oct 7)
                changed = words_changed(before, xml)
                if changed and len(changed) <= 6:  # a near miss; a new screen needs no line
                    log("act read: %d rows gone, %d new; %s" % (
                        sum(1 for c in changed if c[0] == "-"), sum(1 for c in changed if c[0] == "+"),
                        "; ".join("%s%r" % c for c in changed[:4])))
            # The action landed in Chrome: the page itself says what it
            # shows now (the screen reader's tree may lag it). The read is
            # the newest first, so that the page is asked only with Chrome
            # in front on it (HOME from a page: no question to a page in
            # the background, whose rows printed as the screen, Oct 5).
            self._remember(xml)
            self._front_look = None
            page_xml = self._page_read()
            if page_xml is None and getattr(self, "_front_look", None):
                # the page check's own read of what is in front (another
                # app's window over the page): newer than the act's
                page_xml, self._front_look = self._front_look, None
            if page_xml is not None:
                xml = page_xml
                self._remember(xml)
        return xml.encode()

    def _relook(self, xml):
        """The read after an action that shows the screen exactly as it
        was before, or a row or two of a dozen (see half_drawn_after):
        one more, a moment later, in one trip. A tap that opens another
        app's window draws nothing for most of a second, so the wait for
        the UI to go quiet ended at once and the read showed the screen
        from before the tap (Settings' search bar, Oct 6). The new read,
        or, when it has no words, that read too (the screen is moving to
        something not readable yet; the CLI reads a thin screen again),
        rather than the read from before the action. The one in hand when
        the read again fails, which is logged."""
        _time.sleep(RELOOK_PAUSE_S)
        try:
            with _t("act relook"):
                res = self._batch([("waitForIdle", [RELOOK_IDLE_MS]),
                                   ("dumpWindowHierarchy", [False, DUMP_DEPTH])],
                                  timeout=RELOOK_IDLE_MS / 1000.0 + 4)
        except Exception as e:
            log("the screen looked unchanged or half drawn after the action; the read again failed (%s)"
                % err_text(e, 80))
            return xml
        again = res[-1]
        if not isinstance(again, str):
            log("the screen looked unchanged or half drawn after the action; the read again failed (%s)"
                % err_text(again, 80))
            return xml
        if not has_words(again):
            log("the screen looked unchanged or half drawn after the action; read again: nothing readable yet")
            return again
        log("the screen looked unchanged or half drawn after the action; read again: %s"
            % ("changed" if screen_sig(again) != screen_sig(xml) else "still the same"))
        return again

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
            if cmd in NO_RETRY and not isinstance(e, ActNotSent):
                # The phone may already have acted (the reply was lost, not
                # the request). Replaying would tap or type twice.
                log("%s %s: %s" % (cmd, arg[:80], err_text(e, 160)))
                raise
            if cmd in NO_RETRY:
                # an act that never went out (its stream wasn't opened, or
                # the read before it failed): mended like a read, and sent
                # once more, which can't make it happen twice (right after
                # `burner update` the server wasn't listening, "AdbError:
                # closed", and a start's reads went the slow way, Oct 6)
                log("%s %s: %s; sending it once more" % (cmd, arg[:80], err_text(e, 160)))
            # Maybe the on-device server died — reconnect once and retry.
            # The RLock makes concurrent handlers queue behind one reconnect.
            with self._lock:
                gone = phone_gone(e) or (isinstance(e, ActNotSent) and device_gone(str(e)))
                answers, why = False, e
                if not gone:
                    try:
                        self.d.info
                        answers = True  # the server answers: the command is sent again
                    except Exception as e2:
                        gone, why = phone_gone(e2), e2
                if gone:
                    # the phone gone from adb (its link dropped), not the
                    # server from the phone: the link is remade whatever
                    # the restart cooldown says, since nothing is killed
                    self._relink(err_text(why, 100))
                elif not answers:
                    if not restart_allowed(_time.monotonic() - self._last_restart):
                        raise  # a restart a moment ago: no kill/relaunch loop
                    log("command %s failed (%s), reconnecting" % (cmd, err_text(e)))
                    self.connect()
            try:
                return fn(arg)
            except U2NotFound:
                return b"__NOT_FOUND__"


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def iter_nodes(xml):
    """Yield {text, desc, bounds, center, rect, enabled, clickable,
    selected, focused, checked, pkg, rid, web, field, cls, parent} for every node with
    non-empty on-screen bounds,
    in document order; web: the node is inside a WebView (a browser's
    page, an app's web content); field: a text field (its text is what
    was typed); cls: its class, lowercased; parent: the index, in this
    order, of the nearest node above it that was yielded, or None."""
    import xml.etree.ElementTree as ET
    count = [0]

    def rec(n, web, parent):
        cls = (n.get("class") or "").lower()
        web = web or "webview" in cls
        m = _BOUNDS_RE.match(n.get("bounds", ""))
        if m:
            x1, y1, x2, y2 = map(int, m.groups())
            if x2 > x1 and y2 > y1:
                yield {"text": n.get("text") or "", "desc": n.get("content-desc") or "",
                       "bounds": n.get("bounds"), "center": [(x1 + x2) // 2, (y1 + y2) // 2],
                       "rect": (x1, y1, x2, y2),
                       "enabled": n.get("enabled") != "false",
                       "clickable": n.get("clickable") == "true",
                       "selected": n.get("selected") == "true", "focused": n.get("focused") == "true",
                       "checked": n.get("checked") == "true", "pkg": n.get("package") or "",
                       "rid": n.get("resource-id") or "", "web": web,
                       "password": n.get("password") == "true",
                       "field": "edittext" in cls, "cls": cls, "parent": parent}
                parent = count[0]
                count[0] += 1
        for c in n:
            yield from rec(c, web, parent)
    yield from rec(ET.fromstring(xml), False, None)


def screen_of(xml):
    """(w, h, pkg) from a read: the screen size at its rotation (the far
    corner of the nodes that touch the left or top edge; the nav bar's
    window counts, so an app window that stops above it doesn't shrink
    the screen) and the app in front (the package with most nodes, the
    system UI and a keyboard aside). None when no node touches an edge
    (a lone dialog): the read doesn't say how big the screen is. Pure."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml or "")
    except ET.ParseError:
        return None
    w = h = 0
    counts = {}
    for n in root.iter("node"):
        m = _BOUNDS_RE.match(n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x1 == 0 or y1 == 0:
            w, h = max(w, x2), max(h, y2)
        pkg = n.get("package", "")
        if pkg and pkg != SYSTEM_UI and not any(k in pkg for k in U2Daemon.IME_HINTS):
            counts[pkg] = counts.get(pkg, 0) + 1
    if w < 300 or h < 300:
        return None
    return w, h, (max(counts, key=counts.get) if counts else "")


def keyboard_in(xml):
    """True if a dump holds a keyboard app's window. Pure."""
    for part in (xml or "").lower().split('package="')[1:]:
        pkg = part.split('"', 1)[0]
        if any(h in pkg for h in U2Daemon.IME_HINTS):
            return True
    return False


# The system UI's icons in the status bar and the navigation bar (the
# battery, the signal, the clock) are never what a fuzzy label means:
# `tap Battery`, with "Battery" below the fold, matched the status bar's
# "Battery 79 percent." and tapped it (the battery task, Oct 6).
BAR_FRACTION = 0.07  # the bars' height, as a share of the screen's


def bar_icon(node, h):
    """Whether a node (from iter_nodes) is one of the system UI's icons in
    the status bar or the navigation bar, on a screen `h` pixels tall. Pure."""
    if node.get("pkg") != SYSTEM_UI or not h:
        return False
    y1, y2 = node["rect"][1], node["rect"][3]
    return y2 <= h * BAR_FRACTION or y1 >= h * (1 - BAR_FRACTION)


# A row whose words merely contain a tap's words stands for them only when
# they name it: `tap Search` on YouTube's results, with no row reading
# "Search", pressed "Search with your voice", and Android asked to record
# audio (Oct 7); "Delete" inside "Delete account" would be worse.
NOTE_MARKS = ",:;([|-\u2013\u2014\u00b7\u2022/"  # what starts a note after a row's name
# A row that says the opposite of a tap's words ("Don't turn off" for `Turn
# off`, review of Oct 7) is never named by them.
NEGATIONS = ("don't", "dont", "not", "never", "no", "stop", "cancel", "undo", "without")


def fuzzy_ok(query, label):
    """Whether `label`, a row's words that contain `query` without being
    it, may stand for it in a tap: the words are whole words of the row
    (a hyphen joins a word: "Add" is not "Add-ons"), not right after a
    negation ("Don't turn off"), and a phrase of three words or more,
    more than half of the row's words, or the row's head with a note
    after it ("Inbox, 3 unread", "Echo Dot (5th Gen) | Smart speaker",
    "Next >"); a symbol before the head is no word ("\u2605 Starred"). Pure."""
    # an em or en dash is a note's start, not a hyphen joining words
    # ("Inbox\u20143 unread", review of Oct 7)
    label = (label or "").replace("\u2014", " - ").replace("\u2013", " - ")
    q, row = plain_words(query).lower(), plain_words(label).lower()
    m = re.search(r"(?<![\w-])" + re.escape(q) + r"(?![\w-])", row) if q else None
    if not m:
        return False
    before = row[:m.start()].split()
    if before and before[-1] in NEGATIONS:
        return False
    words, row_words = len(q.split()), len(row.split())
    if words >= 3 or 2 * words > row_words:
        return True
    # the head: the row as it is, or past a leading symbol the query lacks
    # ("\u2605 Starred"); a query's own symbol stays ("@tropoFarmer \u00b7 2h")
    for head in (row, re.sub(r"^[^\w]+", "", row)):
        if head.startswith(q):
            rest = head[len(q):].strip()
            return rest == "" or rest[0] in NOTE_MARKS or not any(ch.isalnum() for ch in rest)
    return False


def find_node(xml, needle, fuzzy=True, names=False):
    """First node whose text or content-desc matches needle: exact
    (case-insensitive) match wins, then substring if fuzzy (never on a
    status or navigation bar icon, see bar_icon; with `names`, for a tap,
    only a row the words name, see fuzzy_ok). "A || B" matches any of
    the labels (the first found, in that order). None on miss."""
    needles = [plain_words(p).lower() for p in needle.split("||") if p.strip()]
    first_sub = None
    nodes = list(iter_nodes(xml))
    h = max((n["rect"][3] for n in nodes), default=0)

    def named(nl, n, labels, focused_ok=False):
        # for a tap: a row the words name, or a field they name with
        # nothing over it (a field only takes the focus; review of Oct 7)
        if any(fuzzy_ok(nl, l) for l in labels if nl in l):
            return True
        return (field_named(nl, n, focused_ok)
                and row_over(nodes, [n], tuple(n["center"]), screen_size(nodes)) is None)
    for nl in needles:
        focused_sub = None  # a field with the focus, when no other row is named
        for n in nodes:
            labels = (plain_words(n["text"]).lower(), plain_words(n["desc"]).lower())
            if nl in labels:
                return n
            if fuzzy and first_sub is None and any(nl in l for l in labels) and not bar_icon(n, h):
                if not names or named(nl, n, labels):
                    first_sub = n
                elif focused_sub is None and named(nl, n, labels, focused_ok=True):
                    focused_sub = n
        if first_sub is not None:
            return first_sub
        if focused_sub is not None:
            return focused_sub
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
