"""Chrome's DevTools channel over adb: the exact way to read and drive a
web page in Chrome on the phone.

The accessibility tree that the screen reads come from lags a finger
scroll on a heavy page by seconds (espn.com, Oct 4: the positions stayed
a whole card behind the pixels for five seconds, then the tree came back
empty for a while), so a tap placed by those positions lands on the
neighbour. The page itself always has the current layout: an element is
found by its words, scrolled into view and touched at its centre, the
page scrolls itself, and the rows come from the DOM, handed back as a
screen read (page_xml) so everything that reads screens keeps working.
Chrome exposes this channel on the phone's abstract socket
`chrome_devtools_remote` whenever USB debugging is on; it is reached over
the same adb stream as the on-phone UI server, with a WebSocket written
here (the venv has no websocket module).

Used by the UI helper (u2mux.py), which keeps one page connection warm.
Only Chrome: an app's WebView exposes the channel only when the app asks.
"""
import base64
import hashlib
import json
import os
import re
import select
import socket
import struct
import threading
import time
from xml.sax.saxutils import quoteattr

SOCKET_NAME = "chrome_devtools_remote"
CHROME_PACKAGES = ("com.android.chrome", "com.chrome.beta", "com.chrome.dev",
                   "com.chrome.canary", "org.chromium.chrome")


class NotDone(RuntimeError):
    """The page took the action but it didn't do what was asked (a field
    that didn't take the text): the page session is fine."""


class NotSent(RuntimeError):
    """A tap that failed before anything touched the page."""


class NothingSent(ConnectionError):
    """The page took none of the commands: the stream failed before the
    first went out, or Chrome refused the session. The caller may act
    another way. A failure after that is not this: the page may have
    acted on what it got."""


def is_chrome(pkg):
    """True for a package whose pages DevTools can reach. Pure."""
    return pkg in CHROME_PACKAGES


def xor_mask(data, mask):
    """`data` masked with the 4-byte `mask` (the WebSocket client rule). Pure."""
    n = len(data)
    if not n:
        return b""
    full = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(full, "big")).to_bytes(n, "big")


def frame(text):
    """A masked text frame carrying `text` (bytes or str). Pure."""
    data = text.encode() if isinstance(text, str) else text
    n = len(data)
    head = bytearray([0x81])
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126)
        head += struct.pack(">H", n)
    else:
        head.append(0x80 | 127)
        head += struct.pack(">Q", n)
    mask = os.urandom(4)
    return bytes(head) + mask + xor_mask(data, mask)


class WebSocket:
    """A WebSocket client on an already connected socket (an adb stream to
    the phone's DevTools socket). Text messages in and out; pings answered;
    a close frame or a dropped stream raises ConnectionError."""

    def __init__(self, sock, path, host="localhost", timeout=6.0):
        self.s = sock
        self.s.settimeout(timeout)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, key)).encode())
        # the handshake's reply is read before the first frame (see _read):
        # frames sent meanwhile ride in the handshake's round trip, which
        # is 0.4-0.75s from afar
        self._handshake_key = key

    def handshake(self, timeout):
        """Wait for Chrome's answer to the handshake, `timeout` at most,
        before anything is sent (see Page: a busy Chrome drops a session
        whose first commands come before that answer)."""
        if self._handshake_key:
            self.s.settimeout(timeout)
            self._await_handshake()

    def _await_handshake(self):
        key, self._handshake_key = self._handshake_key, None
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.s.recv(4096)
            if not chunk:
                raise NothingSent("websocket handshake: the stream closed")
            resp += chunk
        head, self.buf = resp.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        if " 101 " not in status:
            raise NothingSent("websocket handshake refused: %s" % status[:80])
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if accept not in head:
            raise NothingSent("websocket handshake: wrong accept key")

    def _read(self, n):
        if self._handshake_key:
            self._await_handshake()
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("websocket: the stream closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        self.s.sendall(frame(text))

    def _control(self, op, payload):
        mask = os.urandom(4)
        self.s.sendall(bytes([0x80 | op, 0x80 | len(payload)]) + mask + xor_mask(payload, mask))

    def recv(self):
        """The next text message (continuations joined)."""
        msg = b""
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = xor_mask(payload, mask)
            if op == 0x8:
                raise ConnectionError("websocket closed by Chrome")
            if op == 0x9:
                self._control(0xA, payload)
                continue
            if op == 0xA:
                continue
            msg += payload
            if b0 & 0x80:
                return msg.decode("utf-8", "replace")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


OPEN_S = 6.0  # a stream to the DevTools socket through adb: a fraction of a second when the link is up


def open_stream(dev, timeout=OPEN_S):
    """A socket to Chrome's DevTools socket on the phone, over adb, opened
    within `timeout`: adbutils gives the open 600s, and a link through the
    tunnel that passes nothing never answers it (with Chrome in front, a
    read hung until adb dropped the link, the review of Oct 6). Raises
    ConnectionError when the phone doesn't answer in time. The callers
    set the socket's own timeouts."""
    t = dev.open_transport(timeout=timeout)
    try:
        t.send_command("localabstract:" + SOCKET_NAME)
        t.check_okay()
    except Exception as e:
        t.close()
        if isinstance(e, (socket.timeout, TimeoutError)) or type(e).__name__ == "AdbTimeout":
            raise ConnectionError("no answer from the phone's adb in %.0fs (DevTools stream open)" % timeout)
        raise
    return t.conn


def dechunk(data):
    """The body of a chunked transfer (sizes in hex, a 0 chunk ends it). Pure."""
    out, pos = b"", 0
    while True:
        end = data.find(b"\r\n", pos)
        if end < 0:
            break
        size = int(data[pos:end].split(b";")[0].strip() or b"0", 16)
        if size == 0:
            break
        out += data[end + 2:end + 2 + size]
        pos = end + 2 + size + 2
    return out


def recv_http(recv):
    """One HTTP/1.1 response read with `recv(n)`: (status line, body).
    The body is read by Content-Length, by chunks, or to the close,
    whichever the headers say: Chrome keeps the connection open despite
    "Connection: close", so a read to the close waits for the timeout
    (the first live run, Oct 4). Pure given recv."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = recv(65536)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = lines[0].decode("utf-8", "replace")
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(b":")
        headers[k.strip().lower()] = v.strip()
    if headers.get(b"transfer-encoding", b"").lower() == b"chunked":
        while not (body.startswith(b"0\r\n") or b"\r\n0\r\n" in body):
            chunk = recv(65536)
            if not chunk:
                break
            body += chunk
        body = dechunk(body)
    elif b"content-length" in headers:
        n = int(headers[b"content-length"] or b"0")
        while len(body) < n:
            chunk = recv(65536)
            if not chunk:
                break
            body += chunk
        body = body[:n]
    else:
        while True:
            chunk = recv(65536)
            if not chunk:
                break
            body += chunk
    return status, body


def http_get(dev, path, timeout=4.0):
    """JSON from DevTools' small HTTP side (/json lists the pages)."""
    s = open_stream(dev)
    s.settimeout(timeout)
    try:
        s.sendall(("GET %s HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                   % path).encode())
        status, body = recv_http(s.recv)
    finally:
        s.close()
    if " 200 " not in status:
        raise RuntimeError("DevTools %s: %s" % (path, status[:80] or "no answer"))
    return json.loads(body.decode("utf-8", "replace"))


class Page:
    """One page's DevTools session: commands by id, page scripts by
    value, and the page's navigation events (the Page domain is enabled
    at the start): `loading` is True from the main frame's start of a
    load until its document is parsed."""

    lock = threading.RLock()  # a page built without __init__ (the tests) has this one

    # A session's first commands go with its handshake (a round trip
    # saved), but Chrome's DevTools server takes no frame before it has
    # accepted the handshake (net/server/web_socket.cc: no encoder, a frame
    # error, the connection closed), and the accept waits on Chrome's main
    # thread: busy (Chrome just brought to the front), it closed every
    # session for 4-11s after a launch (Oct 7). Then the session is opened
    # again, its commands sent once the handshake is answered.
    DROPPED = "websocket handshake: the stream closed"

    def __init__(self, dev, target, probe_s=None, pipeline=True):
        """pipeline: the first commands go with the handshake; False right
        after a launch, when Chrome drops such a session (see DROPPED):
        they wait for the handshake's answer from the start."""
        self.target = target
        self.handshake_again = False  # the session took a second handshake (see DROPPED)
        self.ws = WebSocket(open_stream(dev), "/devtools/page/" + target)
        self.n = 0
        self.lock = threading.RLock()  # one thread on the stream (a wait polls while a tap acts)
        self.visible_at = 0.0  # when a probe last said the page was visible
        self.loading = False
        self.window_opened = None  # the address of a window (a tab) the page opened, once heard
        self.main_frame = None
        self.url = ""
        # bounded like a visibility probe (probe_s, PROBE_S by default, plus
        # the handshake's trip): a tab frozen in the background never
        # answers the question; a session that fails to open closes its stream
        bound = (PROBE_S if probe_s is None else probe_s) + 0.6
        first = [("Page.enable", {}), ("Page.getFrameTree", {}), _evaluate("document.visibilityState")]
        try:
            try:
                if not pipeline:
                    self.ws.handshake(bound)
                res = self.call_many(first, timeout=bound, raise_errors=False)
            except NothingSent as e:
                if str(e) != self.DROPPED:
                    raise
                self.ws.close()
                self.handshake_again = True
                # within the probe's bound (an adb open may take OPEN_S)
                self.ws = WebSocket(open_stream(dev, timeout=min(OPEN_S, bound)), "/devtools/page/" + target)
                self.ws.handshake(bound)
                res = self.call_many(first, timeout=bound, raise_errors=False)
            for r in res[:2]:
                if isinstance(r, Exception):
                    raise r
        except BaseException:
            self.close()
            raise
        frame = (res[1].get("frameTree") or {}).get("frame") or {}
        self.main_frame, self.url = frame.get("id"), frame.get("url", "")
        try:
            if _value(res[2]) == "visible":
                self.visible_at = time.monotonic()
        except RuntimeError:
            pass

    def _event(self, m):
        method, p = m.get("method", ""), m.get("params") or {}
        if method == "Page.windowOpen":
            # a link or a script opened a window: on a phone, a new tab that
            # Chrome brings to the front (weather.gov, Oct 7)
            self.window_opened = p.get("url") or "about:blank"
        elif method == "Page.frameStartedLoading" and p.get("frameId") == self.main_frame:
            self.loading = True
        elif method == "Page.frameNavigated":
            frame = p.get("frame") or {}
            if frame.get("id") == self.main_frame or not frame.get("parentId"):
                self.main_frame, self.url = frame.get("id", self.main_frame), frame.get("url", self.url)
        elif method in ("Page.domContentEventFired", "Page.loadEventFired"):
            self.loading = False
        elif method == "Page.frameStoppedLoading" and p.get("frameId") == self.main_frame:
            self.loading = False

    def _readable(self, timeout):
        """True when a frame is waiting on the stream within `timeout`."""
        try:
            return bool(select.select([self.ws.s], [], [], timeout)[0]) or bool(self.ws.buf)
        except (OSError, ValueError):
            return False

    def listen(self, seconds):
        """Take in the events of the next `seconds` (no round trip)."""
        end = time.monotonic() + seconds
        with self.lock:
            while True:
                left = end - time.monotonic()
                if left <= 0 or not self._readable(left):
                    return
                self.ws.s.settimeout(5.0)
                m = json.loads(self.ws.recv())
                if "id" not in m:
                    self._event(m)

    def wait_loading(self, cap_s):
        """Wait, listening, until a load of the main frame starts, `cap_s`
        at most. True when one did."""
        end = time.monotonic() + cap_s
        while not self.loading and time.monotonic() < end:
            self.listen(min(0.1, max(0.0, end - time.monotonic())))
        return self.loading

    def wait_parsed(self, cap_s):
        """Wait, listening, until the loading document is parsed, `cap_s`
        at most. True when it is."""
        end = time.monotonic() + cap_s
        while self.loading and time.monotonic() < end:
            self.listen(min(0.25, max(0.0, end - time.monotonic())))
        return not self.loading

    def _bound(self, timeout):
        """`timeout`, or STALE_CALL_S when nothing has proved the page on
        screen within QUICK_AFTER_S (see visible)."""
        if time.monotonic() - self.visible_at > QUICK_AFTER_S:
            return min(timeout, STALE_CALL_S)
        return timeout

    def call(self, method, timeout=10.0, **params):
        return self.call_many([(method, params)], timeout)[0]

    def call_many(self, cmds, timeout=10.0, raise_errors=True):
        """Several commands sent at once, one round trip: their results
        in order. cmds: [(method, params), ...]. Raises on the first
        command that failed, after all have been answered; with
        raise_errors False, a failed command's result is its RuntimeError.
        Raises NothingSent when the page took none of them (the stream
        failed before the first went out, or Chrome refused the session);
        any other failure came after a command went out, and the page may
        have acted on it."""
        ids = []
        with self.lock:
            self.ws.s.settimeout(self._bound(timeout))
            for method, params in cmds:
                self.n += 1
                ids.append(self.n)
                try:
                    self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
                except NothingSent:
                    raise
                except Exception as e:
                    if len(ids) == 1:
                        raise NothingSent(str(e)[:120]) from e
                    raise
            got = {}
            while len(got) < len(ids):
                m = json.loads(self.ws.recv())
                if m.get("id") in ids:
                    got[m["id"]] = m
                elif "id" not in m:
                    self._event(m)
        out = []
        for i, (method, _) in zip(ids, cmds):
            m = got[i]
            if "error" in m:
                err = RuntimeError("%s: %s" % (method, m["error"].get("message", m["error"])))
                if raise_errors:
                    raise err
                out.append(err)
                continue
            out.append(m.get("result", {}))
        return out

    def eval(self, expression, timeout=10.0):
        """The value of a page script (a promise is awaited)."""
        return _value(self.call("Runtime.evaluate", timeout, expression=expression,
                                returnByValue=True, awaitPromise=True))

    def close(self):
        self.ws.close()


def pages(dev):
    """Chrome's open pages (tabs), as DevTools lists them."""
    return [t for t in http_get(dev, "/json") if t.get("type") == "page"]


PROBE_S = 1.5    # a visibility question to a page: a background tab answers only on the timeout
SCAN_TABS = 3    # tabs looked at for the visible page (Chrome lists the current one first)
HINT_TABS = 3    # tabs at the hinted address (a link just opened, Chrome's bar), probed with the first
MORE_TABS = 8    # the next tabs, probed once when none of the first ones is visible
LOAD_PROBE_S = 6.0  # the current tab, too busy loading to answer the probe, is given this long


QUICK_PROBE_S = 0.7  # the page in hand asked whether it is still on screen: a visible
                     # page answers in a round trip, a hidden one only on the timeout


def visible(page, timeout=PROBE_S):
    """True while `page` is the one on screen: a probe said so within
    VISIBLE_FOR_S, or it says so now (`timeout` at most: a background
    tab is frozen and answers only on the timeout)."""
    if time.monotonic() - page.visible_at < VISIBLE_FOR_S:
        return True
    try:
        if page.eval("document.visibilityState", timeout=timeout) == "visible":
            page.visible_at = time.monotonic()
            return True
    except Exception:
        pass
    return False


def _address(s):
    """A URL or what Chrome's bar shows, as one spelling: no scheme, no
    "www.", no trailing slash or ellipsis, lower case. Pure."""
    s = (s or "").strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    if s.startswith("www."):
        s = s[4:]
    return s.rstrip("\u2026./ ")


def same_address(url, hint):
    """Whether a tab's URL is at the address in `hint`: a link just opened
    (its scheme or "www." aside), or what Chrome's bar shows (no scheme,
    a long path cut short): the shorter is the start of the longer. Pure."""
    a, b = _address(url), _address(hint)
    n = min(len(a), len(b))
    return n >= 6 and a[:n] == b[:n]


def _short(url):
    return (_address(url) or "(no address)")[:40]


def _probe(dev, ids, first_probe_s=None, slow=(), pipeline=True):
    """Each tab asked whether it is the one on screen, all at once (each
    on its own stream, so the wait is one probe's, not one per tab: three
    in a row cost 6s while a heavy page loaded, Oct 5), and the answer
    taken as soon as one says visible: the others are still being asked,
    and close themselves when they answer (a cold open waited out the 6s
    bound on a frozen first tab while the link's tab had answered within
    a second, Oct 5). The first tab and those in `slow` get the long
    bound `first_probe_s` (the tabs loading the link just opened). By
    `ids`: a Page (visible_at > 0 when it said visible), the exception
    its probe ended in, or None (no answer yet)."""
    found = [None] * len(ids)
    lock, done, pending = threading.Lock(), threading.Event(), [len(ids)]

    def probe(i):
        try:
            p = Page(dev, ids[i], probe_s=first_probe_s if (i == 0 or ids[i] in slow) else None,
                     pipeline=pipeline)
        except Exception as e:
            p = e
        with lock:
            late = done.is_set()
            if not late:
                found[i] = p
                pending[0] -= 1
                if pending[0] == 0 or (isinstance(p, Page) and p.visible_at > 0):
                    done.set()
        if late and isinstance(p, Page):
            p.close()  # the scan was decided without it
    for i in range(len(ids)):
        threading.Thread(target=probe, args=(i,), daemon=True).start()
    done.wait(max(PROBE_S, first_probe_s or 0) + 3.0)
    with lock:
        done.set()
        return list(found)


def _visible_one(found):
    """The first Page in `found` that said visible, the others closed;
    None when none did."""
    page = None
    for p in found:
        if isinstance(p, Page):
            if page is None and p.visible_at > 0:
                page = p
            else:
                p.close()
    return page


def _close_dead(dev, ids, found, urls, hint, keep):
    """Tabs at the hinted address that refused their probe (a renderer
    long gone: leftovers of earlier opens of that link), closed through
    DevTools once the visible page (`keep`) is known, so later scans
    stop wading through them (six dead tabs at one address answered
    "refused" first on every cold open, Oct 5). Best effort, a second
    at most per tab."""
    for i, p in zip(ids, found):
        if (i != keep and isinstance(p, ConnectionError) and hint
                and same_address(urls.get(i, ""), hint)):
            try:
                http_get(dev, "/json/close/" + i, timeout=1.0)
            except Exception:
                pass


def _answer(p):
    if isinstance(p, Page):
        return "hidden"
    if isinstance(p, ConnectionError):
        # with the refusal's words (Chrome's handshake status, a closed
        # stream): seven live tabs refused at once after a helper restart
        # with no word why (Oct 5)
        why = " ".join(str(p).split())[:70]
        return "refused (%s)" % why if why else "refused"
    return "no answer" if p is None else "no answer (%s)" % type(p).__name__


def front_page(dev, current=None, first_probe_s=None, hint=None, quick=False):
    """The page the user sees: `current` while it is still the visible
    one, else the visible page among Chrome's tabs (a fresh session for
    it). The first SCAN_TABS tabs are probed at once (Chrome lists the
    current one first, and a phone had 107), and with `hint` (the link
    just opened, or the address Chrome's bar shows) the tabs at that
    address too, wherever Chrome lists them: a link launched Chrome into
    a tab listed fourth or later, and three probes found nothing while
    the page was there (airbnb.com, Oct 5). When none answers "visible",
    the current tab, listed first, is given LOAD_PROBE_S once more (too
    busy loading to answer is not hidden), then the next MORE_TABS tabs
    are probed once. With `first_probe_s` (Chrome was just launched with
    a link: its current tab is loading it), the current tab is given
    that long from the start. With `quick` (Chrome was just launched,
    and its window is in front: the caller asks again in a moment),
    one short round over the first tabs and the hinted ones, no long
    bound and no sweep: probing a Chrome still coming up, eleven
    sessions a try, kept it from coming up (16-33s cold opens, Oct 5).
    Raises RuntimeError, saying what each tab answered, when none is
    visible (Chrome isn't in front, or shows a native screen)."""
    if current is not None:
        if visible(current):
            return current
        current.close()
    tabs = [t for t in pages(dev) if t.get("id")]
    urls = {t["id"]: t.get("url", "") for t in tabs}
    ids = [t["id"] for t in tabs[:SCAN_TABS]]
    hinted = set()
    if hint:
        for t in tabs[SCAN_TABS:]:
            if same_address(t.get("url", ""), hint):
                ids.append(t["id"])
                hinted.add(t["id"])
                if len(ids) >= SCAN_TABS + HINT_TABS:
                    break
    if not ids:
        raise RuntimeError("no page in Chrome")
    # right after a launch (quick), Chrome drops a session whose commands
    # come with the handshake: they wait for its answer (see Page)
    found = _probe(dev, ids, None if quick else first_probe_s, slow=() if quick else hinted,
                   pipeline=not quick)
    page = _visible_one(found)
    if page is not None:
        _close_dead(dev, ids, found, urls, hint, page.target)
        return page
    if (not quick and first_probe_s is None and not isinstance(found[0], Page)
            and not isinstance(found[0], ConnectionError)):
        # the current tab didn't answer in time: busy with a load, not hidden
        try:
            p = Page(dev, ids[0], probe_s=LOAD_PROBE_S)
        except Exception as e:
            p = e
        if isinstance(p, Page):
            if p.visible_at > 0:
                return p
            p.close()
        found[0] = p
    more = [] if quick else [t["id"] for t in tabs[SCAN_TABS:SCAN_TABS + MORE_TABS] if t["id"] not in ids]
    if more:
        found2 = _probe(dev, more)
        page = _visible_one(found2)
        if page is not None:
            _close_dead(dev, ids, found, urls, hint, page.target)
            return page
        ids, found = ids + more, found + found2
    # the first tabs (the current one, the hinted ones) by name, the
    # rest counted: a log line keeps the end of a long message
    named = [(i, p) for i, p in zip(ids, found)][:SCAN_TABS + len(hinted)]
    rest = [_answer(p) for p in found[len(named):]]
    tail = "; the other %d: %s" % (len(rest), ", ".join(
        "%d %s" % (rest.count(a), a) for a in sorted(set(rest), key=rest.index))) if rest else ""
    raise RuntimeError("no visible page among Chrome's %d tabs probed: %s%s" % (
        len(ids), "; ".join("%s: %s" % (_short(urls.get(i, "")), _answer(p)) for i, p in named), tail))


# ----------------------------------------------------------- page scripts
# Each is a function expression; it is called with JSON arguments.

# The rows a reader needs, in document order, each with its own words
# (its text nodes), else its aria-label, alt, title or placeholder; a
# link's or button's short text when it has no words of its own (its
# children then stay out). Boxes are CSS pixels of the viewport.
READ_JS = r"""
(function(cap){
  const t0 = performance.now();
  const vw = innerWidth, vh = innerHeight, vv = window.visualViewport;
  const out = [], seen = new Set(), used = [];
  const squash = s => (s || '').replace(/\s+/g, ' ').trim();
  const own = el => { let t = ''; for (const c of el.childNodes) if (c.nodeType === 3) t += c.nodeValue; return squash(t); };
  const attr = el => squash(el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title'));
  const labelOf = el => { try { const l = el.labels && el.labels[0]; if (!l) return '';
    // the label's own words: a wrapping label's innerText carries the control's text too
    let t = ''; for (const c of l.childNodes) { if (c.nodeType === 3) t += c.nodeValue + ' '; else if (c.nodeType === 1 && !c.matches('input,select,textarea,button')) t += c.textContent + ' '; }
    return squash(t); } catch (e) { return ''; } };
  const ACTIVE = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch],[role=option],[onclick]';
  const FIELD = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]):not([type=image]),textarea,[contenteditable=true]';
  const CHECK = 'input[type=checkbox],input[type=radio],[role=checkbox],[role=switch],[role=radio]';
  // a run of text (words with a part in bold, a highlight, a span) is one
  // row: an element whose descendants are all inline text elements and
  // none of them a control
  const INLINE = new Set(['B', 'STRONG', 'I', 'EM', 'SPAN', 'BDI', 'BDO', 'U', 'S', 'SMALL', 'SUB', 'SUP', 'MARK', 'ABBR', 'CODE', 'TIME', 'CITE', 'Q', 'VAR', 'KBD', 'SAMP', 'FONT', 'BR', 'WBR']);
  const spans = (a, b) => a.left <= b.left + 2 && a.top <= b.top + 2 && a.right >= b.right - 2 && a.bottom >= b.bottom - 2;
  const run = el => { const kids = el.getElementsByTagName('*'); if (!kids.length || kids.length > 40) return null;
    for (const k of kids) if (!INLINE.has(k.tagName) || k.matches(ACTIVE) || k.hasAttribute('aria-label') || k.hasAttribute('title')) return null;
    return squash(el.textContent); };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, { acceptNode: el =>
    el.matches('script,style,noscript,svg,template,iframe') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  for (let el = walker.nextNode(); el && out.length < cap; el = walker.nextNode()) {
    if (used.some(a => a.contains(el))) continue;
    const field = el.matches(FIELD), check = el.matches(CHECK), active = el.matches(ACTIVE), sel = el.tagName === 'SELECT';
    let text = own(el), desc = attr(el), fromText = false;
    if (text && !field && !sel) { const r = run(el); if (r && r !== text) { text = r; fromText = true; } }
    if (sel) { const o = el.options[el.selectedIndex]; text = squash(o ? o.text : ''); desc = desc || labelOf(el); }
    else if (field || check) desc = desc || labelOf(el);
    if (!sel && !text && !desc && active && !field) { const t = squash(el.innerText); if (t && t.length <= 80) { text = t; fromText = true; } }
    if (!text && !desc && !field && !check && !sel) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
    if (el.checkVisibility ? !el.checkVisibility({visibilityProperty: true, opacityProperty: true}) : false) continue;
    if (fromText) used.push(el);
    const key = text + '|' + desc + '|' + Math.round(r.left) + ',' + Math.round(r.top) + ',' + Math.round(r.right) + ',' + Math.round(r.bottom);
    if (seen.has(key)) continue;
    seen.add(key);
    // a link or a button laid over the words (a card's link, drawn over
    // its title and photo: airbnb.com, Oct 5): a finger there lands on
    // the control, so the row is tappable, as that control
    let over = null;
    if (!active && !el.closest(ACTIVE)) {
      const at = document.elementFromPoint(Math.min(vw - 1, Math.max(0, r.left + r.width / 2)), Math.min(vh - 1, Math.max(0, r.top + r.height / 2)));
      const oc = at && at !== el && !el.contains(at) ? at.closest(ACTIVE) : null;
      if (oc && spans(oc.getBoundingClientRect(), r)) over = oc;
    }
    const kind = sel ? 'select' : field ? 'field' : check ? 'check' : el.matches('button,[role=button],input[type=submit],input[type=button]') ? 'button' : (active ? 'link' : over ? (over.matches('button,[role=button]') ? 'button' : 'link') : 'text');
    out.push({text: text.slice(0, 160), desc: desc.slice(0, 160), kind: kind,
              value: field ? squash(el.value || el.textContent).slice(0, 160) : '',
              placeholder: field ? squash(el.getAttribute('placeholder')).slice(0, 80) : '',
              click: active || !!el.closest(ACTIVE) || !!over,
              checked: check ? (el.checked || el.getAttribute('aria-checked') === 'true') : false,
              focused: document.activeElement === el,
              selected: el.matches('[aria-selected=true],[aria-current]:not([aria-current=false])'),
              disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
              l: r.left, t: r.top, w: r.width, h: r.height});
    if (sel && document.activeElement === el) for (let i = 0; i < el.options.length; i++) {
      if (out.length >= cap) break;
      const o = el.options[i];  // listed under the dropdown, as a native list would be
      out.push({text: squash(o.text).slice(0, 160), desc: '', kind: 'option', value: '', placeholder: '', click: true,
                checked: false, focused: false, selected: !!o.selected, disabled: !!o.disabled,
                l: r.left, t: r.top + r.height * (i + 1), w: r.width, h: r.height});
    }
  }
  // the visual viewport: its scale (a page laid out wider than the screen
  // is drawn scaled down; a pinch zooms in), its corner in the layout and
  // its size, which say where a row is on the screen
  return {title: document.title, url: location.href, ready: document.readyState, vis: document.visibilityState,
          dpr: window.devicePixelRatio || 1, vw: vw, vh: vh, rows: out, more: out.length >= cap,
          vs: vv ? vv.scale : 1, vx: vv ? vv.offsetLeft : 0, vy: vv ? vv.offsetTop : 0,
          vvw: vv ? vv.width : vw, vvh: vv ? vv.height : vh,
          ms: Math.round(performance.now() - t0)};
})"""

# The element with these words (exact, case-insensitive, whitespace
# squashed; "A || B" tries each; then as a part of longer words, unless
# `exact`), scrolled
# into view when it is partly out, and its centre in CSS pixels of the
# viewport. Candidates are the elements with words of their own, with a
# label attribute, or links and buttons with short text (by textContent:
# asking every element for its innerText lays the page out over and
# over). Nested matches (a link around its words) are one control; the
# innermost is used. The matches in view are the ones that count, as
# for a reader of the screen; the rest only when none is in view (then
# it is scrolled into view). Several controls: their count and words,
# no choice, unless `index` picks one.
FIND_JS = r"""
(function(label, index, query, fill, exact){
  const squash = s => (s || '').replace(/[\u2018\u2019\u201B\u2032\u02BC\uFF07]/g, "'").replace(/[\u201C\u201D\u201E\u201F\u2033\uFF02]/g, '"').replace(/[\u2010-\u2015\u2212\uFE63\uFF0D]/g, '-').replace(/\u2026/g, '...').replace(/\s+/g, ' ').trim();
  const own = el => { let t = ''; for (const c of el.childNodes) if (c.nodeType === 3) t += c.nodeValue; return squash(t); };
  const attr = el => squash(el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder') || (el.tagName === 'INPUT' ? el.value : ''));
  const ACTIVE = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch],[role=option],[onclick]';
  const FIELDS = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]):not([type=file]),textarea,select,[contenteditable=true],[role=textbox],[role=searchbox],[role=combobox]';
  const vw = innerWidth, vh = innerHeight;
  const skip = el => !!el.closest('script,style,noscript,svg,template');
  const labelOf = el => { try { const l = el.labels && el.labels[0]; if (!l) return '';
    // the label's own words: a wrapping label's innerText carries the control's text too
    let t = ''; for (const c of l.childNodes) { if (c.nodeType === 3) t += c.nodeValue + ' '; else if (c.nodeType === 1 && !c.matches('input,select,textarea,button')) t += c.textContent + ' '; }
    return squash(t); } catch (e) { return ''; } };
  const focusedSelect = document.activeElement && document.activeElement.tagName === 'SELECT' ? document.activeElement : null;
  const visible = el => el.tagName === 'OPTION' ? (!!focusedSelect && el.closest('select') === focusedSelect)
    : (el.checkVisibility ? el.checkVisibility({visibilityProperty: true, opacityProperty: true}) : true);
  const box = el => el.getBoundingClientRect();
  const inView = r => r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
  const spans = (a, b) => a.left <= b.left + 2 && a.top <= b.top + 2 && a.right >= b.right - 2 && a.bottom >= b.bottom - 2;
  // a run of text (words with a part in bold, a highlight, a span) is one
  // candidate with its whole text as its name: "Pixel 7" inside "Pixel 7a"
  // (a search's highlight of the typed words) is not a "Pixel 7"
  const INLINE = new Set(['B', 'STRONG', 'I', 'EM', 'SPAN', 'BDI', 'BDO', 'U', 'S', 'SMALL', 'SUB', 'SUP', 'MARK', 'ABBR', 'CODE', 'TIME', 'CITE', 'Q', 'VAR', 'KBD', 'SAMP', 'FONT', 'BR', 'WBR']);
  const inlinePart = el => INLINE.has(el.tagName) && !el.matches(ACTIVE) && !el.hasAttribute('aria-label') && !el.hasAttribute('title');
  // a run: an element whose descendants (40 at most) are all inline parts
  const isRun = el => { const kids = el.getElementsByTagName('*'); if (!kids.length || kids.length > 40) return false;
    for (const k of kids) if (!inlinePart(k)) return false; return true; };
  // the run an inline part belongs to: climbed only into a parent that is
  // a run itself (a span beside a block stands on its own: "Wi-Fi" beside
  // "Connected" was found nowhere, and its words in a link instead)
  const runOf = el => { while (el.parentElement && el.parentElement !== document.body && inlinePart(el) && isRun(el.parentElement)) el = el.parentElement; return el; };
  const runText = el => isRun(el) ? squash(el.textContent) : own(el);
  const cands = new Set();
  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let t = tw.nextNode(); t; t = tw.nextNode()) { const p = t.parentElement; if (p && squash(t.nodeValue) && !skip(p) && p.tagName !== 'OPTION') cands.add(runOf(p)); }
  for (const el of document.body.querySelectorAll('[aria-label],[alt],[title],[placeholder],input,' + ACTIVE)) if (!skip(el)) cands.add(el);
  if (focusedSelect) for (const o of focusedSelect.options) cands.add(o);
  const names = el => {
    if (el.tagName === 'OPTION') return [squash(el.text).toLowerCase()].filter(Boolean);
    const n = [runText(el), attr(el)];
    if (el.tagName === 'SELECT') { const o = el.options[el.selectedIndex]; n.push(squash(o ? o.text : '')); n.push(labelOf(el)); }
    else if (el.matches('input,textarea')) n.push(labelOf(el));
    else if (el.matches(ACTIVE)) { const t = squash(el.innerText); if (t && t.length <= 80) n.push(t); }
    return n.filter(Boolean).map(s => s.toLowerCase()); };
  window.__burnerTarget = null;
  window.__burnerGuard = null;
  // when the page last changed, watched from its first find on (see the
  // answer for words not on it)
  if (!window.__burnerWatch) { window.__burnerWatch = true;
    try { new MutationObserver(() => { window.__burnerChangedAt = performance.now(); }).observe(document, {childList: true, subtree: true, characterData: true}); } catch (e) {} }
  const alts = label.split('||').map(squash).filter(Boolean);
  let used = '';
  const find = test => { for (const alt of alts) { const want = alt.toLowerCase(); const h = [];
    for (const el of cands) if (visible(el) && names(el).some(n => test(n, want))) h.push(el);
    if (h.length) { used = alt; return h; } } return []; };
  let hits = find((n, w) => n === w);
  if (!hits.length && !exact) hits = find((n, w) => n.includes(w));
  if (!hits.length) {
    // a page that finished loading a while ago and hasn't changed lately
    // won't show the words a moment later: no second look (0.7s and a
    // round trip for nothing, Oct 7)
    const now = performance.now(), at = window.__burnerChangedAt;
    return {found: false, quiet: document.readyState === 'complete' && now > 5000 && (at === undefined || now - at > 1000)};
  }
  // the words typed into a field are not its label when something else
  // carries them (a search box holding "Pixel 7" beside that suggestion)
  if (hits.length > 1) {
    const typed = hits.filter(el => el.matches('input,textarea') && squash(el.value).toLowerCase() === used.toLowerCase());
    if (typed.length && typed.length < hits.length) hits = hits.filter(el => !typed.includes(el));
  }
  let controls = hits.filter(el => !hits.some(o => o !== el && el.contains(o)));
  // a label beside the control it labels is that control
  // a label found by its words stands for the control it labels
  controls = controls.map(el => (el.tagName === 'LABEL' && el.control) ? el.control : el).filter((el, i, a) => a.indexOf(el) === i);
  // the parts of one link or button are one control (a thumbnail whose
  // alt text is the title beside it: Wikipedia's suggestions, Oct 5)
  const owners = new Map();
  for (const el of controls) { const o = el.closest(ACTIVE) || el; if (!owners.has(o)) owners.set(o, el); }
  controls = Array.from(owners.values());
  // text goes into a field; words on a button or a link (a search icon
  // where the page hides its box) name what opens one: that is touched
  let notField = false;
  if (fill) {
    const fields = controls.filter(el => el.matches(FIELDS));
    if (fields.length) controls = fields; else notField = true;
  }
  const seen = controls.filter(el => inView(box(el)));
  if (seen.length) controls = seen;
  const pick = (index === null || index === undefined) ? null : index;
  if (controls.length > 1 && pick === null) {
    // the same words on links to one address (a story listed twice, its
    // card and its line further down: bbc.com, Oct 7) go to one place:
    // the first is touched. A link within the page or to a script stays
    // ambiguous (two "Edit" links may each do their own thing)
    const dest = el => { const a = el.closest('a[href]'); const h = a && a.getAttribute('href');
      return h && h[0] !== '#' && /^https?:$/.test(a.protocol) ? a.href : null; };
    const d0 = dest(controls[0]);
    if (d0 && controls.every(el => dest(el) === d0)) controls = [controls[0]];
  }
  if (controls.length > 1 && (pick === null || pick >= controls.length)) {
    return {found: true, count: controls.length, used: used, labels: controls.slice(0, 6).map(el => (names(el)[0] || '').slice(0, 60)),
            tags: controls.slice(0, 6).map(el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''))};
  }
  const el = controls[pick || 0];
  let r = box(el), moved = false;
  if (!query && el.tagName === 'OPTION') {
    // picked without Chrome's native popup: the dropdown takes the value
    const s = el.closest('select');
    s.value = el.value;
    s.dispatchEvent(new Event('input', {bubbles: true}));
    s.dispatchEvent(new Event('change', {bubbles: true}));
    return {found: true, count: 1, used: used, label: squash(el.text).slice(0, 60), chose: true, url: location.href};
  }
  if (fill && !notField) {
    // the field to fill: kept for FILL_JS, never touched (a touch opens a
    // picker or moves a slider; a text field is focused there)
    if (!inView(r)) el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    window.__burnerTarget = el;
    return {found: true, count: 1, used: used, label: (names(el)[0] || '').slice(0, 60),
            tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), url: location.href};
  }
  if (!query && el.tagName === 'INPUT' && /^(date|time|month|week|datetime-local|color|range)$/.test(el.type)) {
    // focused, not touched (a touch opens Chrome's native picker, which a
    // page read can't see; a touch on a slider moves it): `type` sets its value
    if (!inView(r)) el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    el.focus();
    return {found: true, count: 1, used: used, label: (names(el)[0] || '').slice(0, 60), focused: true, url: location.href};
  }
  if (!query && el.tagName === 'SELECT') {
    // focused, not touched (a touch opens Chrome's native popup, which a
    // page read can't see): its options print as rows on the next read
    if (!inView(r)) el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    el.focus();
    return {found: true, count: 1, used: used, label: (names(el)[0] || '').slice(0, 60), focused: true, url: location.href};
  }
  if (query) {
    const vq = window.visualViewport;
    return {found: true, count: controls.length, used: used, label: (names(el)[0] || '').slice(0, 60),
            l: r.left, t: r.top, w: r.width, h: r.height, inview: inView(r), dpr: window.devicePixelRatio || 1,
            vs: vq ? vq.scale : 1, vx: vq ? vq.offsetLeft : 0, vy: vq ? vq.offsetTop : 0,
            enabled: !(el.disabled || el.getAttribute('aria-disabled') === 'true')};
  }
  if (!inView(r) || r.top < 0 || r.bottom > vh) { el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'}); r = box(el); moved = true; }
  window.__burnerTarget = el;
  // the touch that follows is judged where it lands (see GUARD_JS)
  window.__burnerGuard = {until: performance.now() + 3000, verdict: null};
  // a text field with the focus keeps the keyboard up, which pushes the
  // visual viewport: leave it first (as a person tapping elsewhere does),
  // and the caller takes the place afresh (PLACE_JS)
  let blurred = false;
  const act = document.activeElement;
  if (act && act !== el && !el.contains(act) && act.matches('input,textarea,[contenteditable=true]')) { act.blur(); blurred = true; }
  const answer = () => {
  const vv = window.visualViewport;
  // what is under the aimed point: a widget's popup (a date field's
  // calendar) over the target swallows the touch; a part of the same
  // control (an icon beside a button's hidden words) is the target itself
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const top = document.elementFromPoint(cx, cy);
  const ctl = el.closest(ACTIVE) || el;
  let covered = !!(top && top !== el && !ctl.contains(top) && !top.contains(el));
  // a link or a button laid over the words (a card's link, drawn over
  // its title and photo: airbnb.com, Oct 5) is what a finger lands on,
  // and the control for them: touched, not bypassed with the words' own
  // click, which opens nothing. A popup over them (a date field's
  // calendar) is not: its controls are small
  let over = null;
  if (covered) { const oc = top.closest(ACTIVE); if (oc && spans(box(oc), r)) { over = oc; covered = false; window.__burnerTarget = oc; } }
  const link = (over || el).closest('a[href]');
  return {found: true, count: 1, used: used, label: (names(el)[0] || '').slice(0, 60),
          x: cx - (vv ? vv.offsetLeft : 0), y: cy - (vv ? vv.offsetTop : 0),
          moved: moved, blurred: blurred, covered: covered, notField: notField, tag: (over || el).tagName.toLowerCase(),
          over: over ? over.tagName.toLowerCase() : '', href: link ? link.href : '',
          cover: covered ? (top.tagName + ' ' + squash(top.innerText).slice(0, 40)) : '', url: location.href};
  };
  if (!moved) return answer();
  // scrolled into view: the page's answer to the scroll (a sticky header,
  // a list filling in) comes a frame or two later; the place is taken
  // after it, in this same trip (a trip of its own for it, Oct 7). An image
  // above it in view with no size yet (a lazy one the scroll brought in)
  // moves it down when it loads: waited for, 400ms at most (the touch
  // landed on the image, review of Oct 7); a later move is the guard's
  return new Promise(done => {
    let sent = false;
    const finish = () => { if (sent) return; sent = true; r = box(el); done(Object.assign(answer(), {settled: true})); };
    // two frames, or 120ms on a page whose frames don't come
    const frames = then => { let n = 0, gone = false; const go = () => { if (!gone) { gone = true; then(); } };
      const tick = () => { if (++n >= 2) go(); else requestAnimationFrame(tick); };
      requestAnimationFrame(tick); setTimeout(go, 120); };
    const sizeless = () => { const top = box(el).top; return Array.from(document.images).filter(im => {
      if (im.complete) return false; const b = im.getBoundingClientRect(); return b.height < 2 && b.top >= -2 && b.top <= top; }); };
    frames(() => {
      const wait = sizeless();
      if (!wait.length) return finish();
      let left = wait.length;
      const one = () => { if (--left === 0) frames(finish); };
      for (const im of wait) { im.addEventListener('load', one, {once: true}); im.addEventListener('error', one, {once: true}); }
      setTimeout(() => frames(finish), 400);
    });
  });
})"""

# The guard on a touch aimed at window.__burnerTarget (FIND_JS, PLACE_JS):
# judged where it lands by the first event of its gesture that reaches the
# page: the target gone, moved from under the point (an image above it
# loading late: the touch took the image, review of Oct 7), or under
# something else there. A miss is held back whole: the page sees none of
# its events, so nothing opens; its verdict is kept for VERDICT_JS. Armed
# by FIND_JS and PLACE_JS (window.__burnerGuard) for one gesture, a few
# seconds at most; installed once a document, in the trip of a find or a
# place. A script's own click is no finger, and passes. A touch on a frame
# (an ad's) never reaches the page: no verdict, and nothing held.
GUARD_JS = r"""
(function(){
  if (window.__burnerGuardOn) return true;
  window.__burnerGuardOn = true;
  const ACTIVE = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch],[role=option],[onclick]';
  const FIRST = ['pointerdown', 'touchstart', 'mousedown', 'click'];
  const judge = (e, T) => {
    if (!T || !T.isConnected) return 'gone';
    const p = (e.touches && e.touches[0]) || (e.changedTouches && e.changedTouches[0]) || e;
    const b = T.getBoundingClientRect();
    if (!(p.clientX >= b.left - 1 && p.clientX <= b.right + 1 && p.clientY >= b.top - 1 && p.clientY <= b.bottom + 1)) return 'moved';
    const path = e.composedPath ? e.composedPath() : [e.target];
    const ctl = T.closest(ACTIVE) || T;
    if (path.indexOf(T) >= 0 || path.indexOf(ctl) >= 0) return 'ok';
    // an element around it, in no other control (a gap between a link's
    // lines): the point the find took for the target's own
    const t = path[0];
    return t && t.contains && t.contains(T) && !(t.closest && t.closest(ACTIVE)) ? 'ok' : 'covered';
  };
  const on = e => {
    const g = window.__burnerGuard;
    if (!g || !e.isTrusted) return;
    const now = performance.now();
    if (!g.verdict) {
      if (now > g.until || FIRST.indexOf(e.type) < 0) return;
      g.verdict = judge(e, window.__burnerTarget);
      g.stop = now + 700;
      if (g.verdict !== 'ok') {
        const t = e.composedPath ? e.composedPath()[0] : e.target;
        g.hit = t && t.tagName ? (t.tagName.toLowerCase() + ' ' + (t.innerText || t.getAttribute('alt') || '')).replace(/\s+/g, ' ').trim().slice(0, 40) : '';
      }
    }
    if (g.verdict !== 'ok' && now <= g.stop) {
      // a touch event's listener is passive (no scroll waits on it): what
      // it starts is held at the click
      if (e.type.slice(0, 5) !== 'touch' && e.cancelable) e.preventDefault();
      e.stopImmediatePropagation();
      if (e.type === 'click') g.stop = 0;
    }
  };
  for (const type of ['pointerdown', 'pointerup', 'pointercancel', 'mousedown', 'mouseup', 'click', 'contextmenu'])
    window.addEventListener(type, on, {capture: true});
  for (const type of ['touchstart', 'touchend', 'touchcancel'])
    window.addEventListener(type, on, {capture: true, passive: true});
  return true;
})"""

# What the guard made of the touch just sent: waited for on the page, `ms`
# at most. {verdict: null}: no event of it reached the page (a frame's
# content took it); null: nothing was armed.
VERDICT_JS = r"""
(function(ms){
  return new Promise(done => {
    const t0 = performance.now();
    const look = () => {
      const g = window.__burnerGuard;
      if (!g) return done(null);
      if (g.verdict || performance.now() - t0 >= ms) return done({verdict: g.verdict || null, hit: g.hit || ''});
      setTimeout(look, 10);
    };
    look();
  });
})"""

# After a touch on what opens a field (a search icon): the field the text
# goes into, kept for FILL_JS. The field with the focus (an overlay's box
# takes it); else the field with the label (the page that opened has its
# own box); else the one field in view (a person types into the only box).
TARGET_JS = r"""
(function(label, waitMs){
  const squash = s => (s || '').replace(/[\u2018\u2019\u201B\u2032\u02BC\uFF07]/g, "'").replace(/[\u201C\u201D\u201E\u201F\u2033\uFF02]/g, '"').replace(/[\u2010-\u2015\u2212\uFE63\uFF0D]/g, '-').replace(/\u2026/g, '...').replace(/\s+/g, ' ').trim();
  const FIELDS = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]):not([type=file]):not([type=checkbox]):not([type=radio]),textarea,[contenteditable=true],[role=textbox],[role=searchbox],[role=combobox]';
  const vh = innerHeight, vw = innerWidth;
  const inView = el => { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw
      && (el.checkVisibility ? el.checkVisibility({visibilityProperty: true, opacityProperty: true}) : true); };
  const labelOf = el => { try { const l = el.labels && el.labels[0]; if (!l) return '';
    let t = ''; for (const c of l.childNodes) { if (c.nodeType === 3) t += c.nodeValue + ' '; else if (c.nodeType === 1 && !c.matches('input,select,textarea,button')) t += c.textContent + ' '; }
    return squash(t); } catch (e) { return ''; } };
  const names = el => [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('title'), labelOf(el), el.name]
    .map(squash).filter(Boolean).map(s => s.toLowerCase());
  const look = () => {
    const a = document.activeElement;
    if (a && a !== document.body && a.matches(FIELDS)) { window.__burnerTarget = a; return {ok: true, how: 'focus', label: names(a)[0] || ''}; }
    const fields = Array.from(document.querySelectorAll(FIELDS)).filter(inView);
    const want = squash(label).toLowerCase();
    const named = fields.filter(el => names(el).some(n => n === want || n.includes(want)));
    const pick = named.length === 1 ? named[0] : (!named.length && fields.length === 1 ? fields[0] : null);
    if (!pick) return {ok: false, fields: fields.length, named: named.length};
    window.__burnerTarget = pick;
    return {ok: true, how: named.length === 1 ? 'label' : 'only', label: names(pick)[0] || ''};
  };
  // the field may still be coming (a search overlay's box is drawn by a
  // script loaded on the tap): looked for every 50ms, waitMs at most, on
  // the page rather than in a round trip of its own
  return new Promise(resolve => {
    const t0 = performance.now();
    const tick = () => { const r = look(); if (r.ok || performance.now() - t0 >= (waitMs || 0)) resolve(r); else setTimeout(tick, 50); };
    tick();
  });
})"""

# The place of the element FIND kept, against the visual viewport (what
# a touch is aimed at), once the keyboard has gone.
PLACE_JS = r"""
(function(){
  const el = window.__burnerTarget;
  if (!el || !el.isConnected) return null;
  const vv = window.visualViewport, vh = vv ? vv.height : innerHeight;
  let r = el.getBoundingClientRect();
  const top = r.top - (vv ? vv.offsetTop : 0), bottom = r.bottom - (vv ? vv.offsetTop : 0);
  if (top < 0 || bottom > vh) { el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'}); r = el.getBoundingClientRect(); }
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const over = document.elementFromPoint(cx, cy);
  const ACTIVE = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch],[role=option],[onclick]';
  const ctl = el.closest(ACTIVE) || el;
  let covered = !!(over && over !== el && !ctl.contains(over) && !over.contains(el));
  if (covered) {
    // a link or a button laid over the words is the control (see FIND_JS)
    const oc = over.closest(ACTIVE), b = oc && oc.getBoundingClientRect();
    if (b && b.left <= r.left + 2 && b.top <= r.top + 2 && b.right >= r.right - 2 && b.bottom >= r.bottom - 2) { covered = false; window.__burnerTarget = oc; }
  }
  // the touch that follows is judged where it lands (see GUARD_JS)
  window.__burnerGuard = {until: performance.now() + 3000, verdict: null};
  return {x: cx - (vv ? vv.offsetLeft : 0), y: cy - (vv ? vv.offsetTop : 0), covered: covered};
})"""

# A settle probe: the page's readiness and a count of DOM changes, so a
# tap's or scroll's result is read once the page stops changing.
SETTLE_JS = r"""
(function(){
  if (!window.__burnerMut) { window.__burnerMut = 1; try { new MutationObserver(() => { window.__burnerMut++; }).observe(document, {childList: true, subtree: true, characterData: true, attributes: true}); } catch (e) {} }
  return {ready: document.readyState, mut: window.__burnerMut, url: location.href, vis: document.visibilityState};
})"""

# A scroll by `fraction` of the viewport's height (down when positive):
# the window, else the tallest element that scrolls (a page laid out
# inside one scrolling box). "top"/"bottom" go to the ends. Returns how
# far it moved. `grow` ms: going down at the end, the page this long at
# most to grow (a feed loads more as it is scrolled), and the scroll again.
SCROLL_JS = r"""
(function(fraction, where, grow){
  const dy = Math.round(innerHeight * fraction);
  const go = (el, isWin) => {
    const at = () => isWin ? scrollY : el.scrollTop;
    const before = at();
    if (where === 'top') (isWin ? window : el).scrollTo(0, 0);
    else if (where === 'bottom') (isWin ? window : el).scrollTo(0, 1e9);
    else (isWin ? window : el).scrollBy(0, dy);
    return at() - before;
  };
  // taller than the screen, but its html or body won't scroll for a
  // finger: a pop-up or a menu holds the page (a script's scroll still
  // moves it, as here: said, not refused)
  const de = document.documentElement, body = document.body;
  const held = Math.max(de.scrollHeight, body ? body.scrollHeight : 0) > innerHeight + 2
    && [de, body].some(e => e && getComputedStyle(e).overflowY === 'hidden');
  let box = null;
  const once = () => {
    const moved = go(window, true);
    if (moved !== 0) return {moved: moved, scroller: 'page', held: held};
    let best = null, bestH = 0;
    for (const el of document.querySelectorAll('div,main,section,article,ul,body')) {
      if (el.scrollHeight <= el.clientHeight + 1 || el.clientHeight < innerHeight / 3) continue;
      const oy = getComputedStyle(el).overflowY;
      if (oy !== 'auto' && oy !== 'scroll') continue;
      if (el.clientHeight > bestH) { best = el; bestH = el.clientHeight; }
    }
    box = best;
    if (!best) return {moved: 0, scroller: null, held: held};
    // a box that scrolls in the page's place is its layout, not a hold
    // (html hidden, body the scroller: "held" for nothing); one inside a
    // fixed box is a pop-up's, over a page it holds (review, Oct 7)
    let popup = false;
    for (let a = best; a && a !== de && !popup; a = a.parentElement) popup = getComputedStyle(a).position === 'fixed';
    return {moved: go(best, false), scroller: best.tagName.toLowerCase(), held: held && popup};
  };
  const r = once();
  if (r.moved !== 0 || !grow || where || dy <= 0) return r;
  const tall = () => box ? box.scrollHeight : Math.max(de.scrollHeight, body ? body.scrollHeight : 0);
  const h0 = tall(), t0 = performance.now();
  return new Promise(done => {
    const tick = () => {
      if (tall() > h0 + 1) done(Object.assign(once(), {grew: true}));
      else if (performance.now() - t0 >= grow) done(r);
      else setTimeout(tick, 100);
    };
    setTimeout(tick, 100);
  });
})"""


# `scroll --to` on a page: the words (any "a || b" alternative, case,
# spaces and curly quotes aside; in the page's text, or an element's
# aria-label, alt or title) where they are drawn, brought to the middle
# of the screen: words on the screen already stay; else words beside it
# (a carousel's slide, a row a list holds out of view), else the nearest
# below (down) or above (up). Not the box around them: a list or a long
# paragraph on the screen can hold them screens below (review, Oct 7).
# {found, moved, used, text}; {found: false, other: true, near} when the
# words are only on the other side (near: the page ends close by this
# way, and may load more as it is scrolled); {found: false, hidden: true}
# when only a box that hides them holds them (a 1px label for screen
# readers, a closed section); {found: false} when the page doesn't hold
# them (yet).
SCROLL_TO_JS = r"""
(function(label, direction){
  const squash = s => (s || '').replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, ' ').trim().toLowerCase();
  // the words in the page's own text: any spaces for a space, a curly
  // quote for a straight one, case aside
  const pattern = want => new RegExp(want.split(' ').map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    .replace(/'/g, "['\u2018\u2019]")).join('\\s+'), 'gi');
  const vh = innerHeight, vw = innerWidth, up = direction === 'up';
  const shown = el => el.checkVisibility ? el.checkVisibility({visibilityProperty: true, opacityProperty: true}) : true;
  const SKIP = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/;
  const texts = root => document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {acceptNode: n =>
    n.nodeType === 3 ? NodeFilter.FILTER_ACCEPT : SKIP.test(n.tagName) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_SKIP});
  const de = document.documentElement, ds = getComputedStyle(de);
  const bodyClips = ds.overflowX !== 'visible' || ds.overflowY !== 'visible';  // else body's overflow is the window's
  // where the words are drawn: cut by every box around them that clips (a
  // carousel's slide off to the side, a list's row scrolled out of it);
  // null where such a box hides them for good (a 1px box for screen
  // readers, a closed section). Not by an element's own box when it is
  // named so (its aria-label, alt or title); not by the boxes between an
  // absolutely placed element and the box it is placed in (a dropdown
  // out of an overflow:hidden bar); none above a fixed one, which no
  // scroll moves (c.fixed)
  const drawn = c => {
    if (c.drawn !== undefined) return c.drawn;
    const r = c.rect;
    let t = r.top, b = r.bottom, l = r.left, rt = r.right, until = null;
    c.fixed = false;
    for (let a = c.el; a && a !== de; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (a === until) until = null;
      if (!until && (a !== c.el || c.range) && (s.overflowX !== 'visible' || s.overflowY !== 'visible')
          && (a !== document.body || bodyClips)) {
        const k = a.getBoundingClientRect();
        if (k.width < 2 || k.height < 2) return (c.drawn = null);
        if (s.overflowX !== 'visible') { l = Math.max(l, k.left); rt = Math.min(rt, k.right); }
        if (s.overflowY !== 'visible') { t = Math.max(t, k.top); b = Math.min(b, k.bottom); }
      }
      if (s.position === 'fixed') { c.fixed = true; break; }
      if (s.position === 'absolute' && !until) until = a.offsetParent;
    }
    return (c.drawn = {top: t, bottom: b, left: l, right: rt});
  };
  // on the screen: most of a line of the words, not a sliver at its edge
  const inView = c => {
    const r = c.rect;
    if (r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) return false;
    const k = drawn(c);
    if (!k) return false;
    const h = Math.min(k.bottom, vh) - Math.max(k.top, 0), w = Math.min(k.right, vw) - Math.max(k.left, 0);
    return h > 0 && w > 0 && h >= Math.min(16, r.height) - 0.5 && w >= Math.min(16, r.width) - 0.5;
  };
  const scrollerOf = el => {
    for (let a = el; a && a !== de; a = a.parentElement) {
      const oy = getComputedStyle(a).overflowY;
      if ((oy === 'auto' || oy === 'scroll') && a.scrollHeight > a.clientHeight + 1) return a;
    }
    return null;
  };
  const measure = c => { c.rect = (c.range || c.el).getBoundingClientRect(); c.drawn = undefined; return c; };
  // the start of what holds the words, as drawn (not a hidden copy's
  // words); an element named so, by that name
  const say = c => {
    const name = c.el.getAttribute('aria-label') || c.el.getAttribute('alt') || c.el.getAttribute('title');
    const s = ((c.range ? c.el.innerText || c.el.textContent : name || c.el.innerText) || '').replace(/\s+/g, ' ').trim();
    return s.length > 80 ? s.slice(0, 79) + '\u2026' : s;
  };
  let other = null, hidden = null;
  for (const want of label.split('||').map(squash).filter(Boolean)) {
    // the elements whose own text holds the words (own), or whose parts
    // do ("Pre<b>heat</b> oven", "<span>Chocolate</span> <span>Cake</span>":
    // joined), each looked at once; and those named so
    const own = new Set(), joined = new Set(), checked = new Set(), named = new Set();
    const tw = texts(document.body);
    for (let t = tw.nextNode(); t; t = tw.nextNode()) {
      const p = t.parentElement;
      if (!p) continue;
      if (t.nodeValue.trim() && squash(t.nodeValue).includes(want)) own.add(p);
      else if (!checked.has(p)) {
        checked.add(p);
        const all = p.textContent;
        if (all.length < 1000 && squash(all).includes(want)) joined.add(p);
      }
    }
    for (const el of document.body.querySelectorAll('[aria-label],[alt],[title]'))
      if (squash(el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title')).includes(want))
        named.add(el);
    // those shown; of them, a box around another is not where the words
    // are, unless its own text holds them (a hidden copy inside, a 1px
    // label for screen readers, leaves the words of the box itself)
    const hits = new Set([...own, ...joined, ...named].filter(shown));
    const outer = new Set();
    for (const h of hits) for (let a = h.parentElement; a && !outer.has(a); a = a.parentElement) outer.add(a);
    const cands = [];
    for (const el of hits) {
      if (outer.has(el) && !own.has(el)) continue;
      let n = 0;
      if (own.has(el) || joined.has(el)) {
        // the words themselves: a long paragraph on the screen can hold them far below it
        const nodes = [];
        let all = '';
        const w = texts(el);
        for (let t = w.nextNode(); t; t = w.nextNode()) { nodes.push([t, all.length]); all += t.nodeValue; }
        const at = (i, end) => {
          let k = 0;
          while (k < nodes.length - 1 && (end ? nodes[k][1] + nodes[k][0].nodeValue.length < i : nodes[k + 1][1] <= i)) k++;
          return [nodes[k][0], i - nodes[k][1]];
        };
        const re = pattern(want);
        for (let m = re.exec(all); m && n < 8; m = re.exec(all)) {
          const range = document.createRange();
          range.setStart(...at(m.index, false));
          range.setEnd(...at(m.index + m[0].length, true));
          const c = measure({el: el, range: range});
          if (c.rect.width > 0 && c.rect.height > 0) { cands.push(c); n++; }
        }
      }
      if (!n) {
        const c = measure({el: el});
        if (c.rect.width > 0 && c.rect.height > 0) cands.push(c);
      }
    }
    if (!cands.length) continue;
    const here = cands.find(inView);
    if (here) return {found: true, moved: 0, used: want, text: say(here)};
    // what a scroll can bring: beside the screen (a slide off to the
    // side, a row a list holds out of view), then the nearest ahead; not
    // what a box hides for good or what is fixed to the screen (a closed
    // menu off its edge)
    const can = c => drawn(c) && !c.fixed;
    const tries = cands.filter(c => c.rect.bottom > 0 && c.rect.top < vh && can(c)).concat(
      cands.filter(c => (up ? c.rect.bottom <= 0 : c.rect.top >= vh) && can(c))
        .sort((a, b) => up ? b.rect.bottom - a.rect.bottom : a.rect.top - b.rect.top));
    if (!tries.length) {
      const back = cands.find(c => (up ? c.rect.top >= vh : c.rect.bottom <= 0) && can(c));
      if (back && !other) {
        // only on the other side; and how far the page goes on this way
        // (one that ends close by may load more as it is scrolled: a feed)
        const se = document.scrollingElement || de;
        const sc = se.scrollHeight > se.clientHeight + 2 ? se : scrollerOf(back.el);
        const room = !sc ? 0 : up ? sc.scrollTop : sc.scrollHeight - sc.clientHeight - sc.scrollTop;
        other = {found: false, other: true, near: room < 1.5 * vh, used: want};
      } else if (!back && !hidden) hidden = {found: false, hidden: true, used: want};
      continue;
    }
    // each in turn, the page put back after one no scroll brought into view
    const x0 = scrollX, y0 = scrollY;
    for (const go of tries.slice(0, 4)) {
      const top = go.rect.top, sc = scrollerOf(go.range ? go.el : go.el.parentElement || go.el);
      const kept = [];  // every box around it, as it was: a scroll into view moves them all
      for (let a = go.el; a; a = a.parentElement) kept.push([a, a.scrollLeft, a.scrollTop]);
      go.el.scrollIntoView({block: go.rect.top < vh && go.rect.bottom > 0 ? 'nearest' : 'center',
                            inline: 'nearest', behavior: 'instant'});
      if (!inView(measure(go))) {
        // a box taller than the screen: the words themselves to its middle
        const box = sc ? sc.getBoundingClientRect() : {top: 0, height: vh};
        (sc || window).scrollBy({top: go.rect.top + go.rect.height / 2 - (box.top + box.height / 2), behavior: 'instant'});
        measure(go);
      }
      if (inView(go)) return {found: true, moved: Math.round(top - go.rect.top), used: want, text: say(go)};
      window.scrollTo({left: x0, top: y0, behavior: 'instant'});
      for (const [a, l, t] of kept)
        if (a.scrollLeft !== l || a.scrollTop !== t) a.scrollTo({left: l, top: t, behavior: 'instant'});
      for (const c of tries) measure(c);
    }
    if (!hidden) hidden = {found: false, hidden: true, used: want};
  }
  return other || hidden || {found: false};
})
"""


# The page's words as a reader sees them (innerText: what is drawn, in
# reading order, its line breaks kept), `count` characters from `start`,
# with the whole length: `burner text`. Rows cut a long text at 160
# characters, and an assistant went around burner to read a JSON answer
# in full (Oct 7). Places count the page's own way (UTF-16 units: an
# emoji is two) and a cut never falls inside a character (half of one
# can't be printed, review Oct 7): `next` is where the rest starts.
TEXT_JS = r"""
(function(start, count, find){
  const raw = (document.body || document.documentElement).innerText || '';
  const text = raw.replace(/[ \t\u00a0]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  const inside = i => i > 0 && i < text.length && (text.charCodeAt(i) & 0xFC00) === 0xDC00
    && (text.charCodeAt(i - 1) & 0xFC00) === 0xD800;
  let a = Math.max(0, Math.min(start, text.length));
  if (find) {
    // every place at or after `start` with the words (any case, any run of
    // spaces between them, a curly quote for a straight one), each with the
    // line before it and the three after (a table's row, a card's price),
    // `count` characters in all at most: the first place in a page's text
    // is often its menu's (coinmarketcap.com, Oct 7)
    const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const q1 = '[\'' + String.fromCharCode(0x2018, 0x2019, 0x2bc) + ']', q2 = '["' + String.fromCharCode(0x201c, 0x201d) + ']';
    const pat = find.trim().split(/\s+/).map(w => esc(w).replace(/'/g, q1).replace(/"/g, q2)).join('\\s+');
    const places = [];
    let more = -1;
    if (pat) {
      const re = new RegExp(pat, 'gi');
      re.lastIndex = a;
      let used = 0, m;
      while ((m = re.exec(text))) {
        if (!m[0].length) { re.lastIndex++; continue; }
        const at = m.index, end = at + m[0].length;
        // lines with words: a page's paragraphs come with blank lines between
        let from = text.lastIndexOf('\n', at - 1) + 1;
        for (let k = 0, n = 0; k < 1 && from > 0 && n < 6; n++) {
          const s0 = text.lastIndexOf('\n', from - 2) + 1;
          if (text.slice(s0, from - 1).trim()) k++;
          from = s0;
        }
        let to = text.indexOf('\n', end);
        if (to < 0) to = text.length;
        for (let k = 0, n = 0; k < 3 && to < text.length && n < 12; n++) {
          let e2 = text.indexOf('\n', to + 1);
          if (e2 < 0) e2 = text.length;
          if (text.slice(to + 1, e2).trim()) k++;
          to = e2;
        }
        if (to - from > 600) { from = Math.max(from, at - 200); to = Math.min(to, Math.max(end, at + 400)); }
        if (inside(from)) from--;
        if (inside(to)) to++;
        if (places.length >= 12 || (places.length && used + to - from > count)) { more = at; break; }
        places.push({at: at, text: text.slice(from, to).split('\n').filter(l => l.trim()).join('\n')});
        used += to - from;
        re.lastIndex = Math.max(re.lastIndex, to);
      }
    }
    return {title: document.title, url: location.href, total: text.length, start: a, places: places, more: more};
  }
  let b = Math.min(text.length, a + Math.max(1, count));
  if (inside(a)) a--;
  if (inside(b)) b = b - 1 > a ? b - 1 : b + 1;
  return {title: document.title, url: location.href, total: text.length, start: a, next: b,
          text: text.slice(a, b)};
})"""


def _js(fn, *args):
    return "(%s)(%s)" % (fn, ", ".join(json.dumps(a) for a in args))


def _evaluate(expression):
    """A page script as a command for call_many (a promise is awaited)."""
    return ("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})


def _later(expression, ms):
    """`expression` run on the page `ms` later: sent in the round trip of
    the input before it, the page answers once the input's first effects
    are in. A round trip to the phone is 0.4-0.75s from afar (Oct 5), the
    page's own work a few ms: the read rides along."""
    return "new Promise(r => setTimeout(() => r(%s), %d))" % (expression, ms)


def _value(res):
    """The value of a page script's result from call or call_many;
    raises on a script that failed or wasn't answered."""
    if isinstance(res, Exception):
        raise RuntimeError(str(res))
    if "exceptionDetails" in res:
        ex = res["exceptionDetails"]
        text = ex.get("exception", {}).get("description") or ex.get("text") or "error"
        raise RuntimeError("page script failed: %s" % text.splitlines()[0][:120])
    return (res.get("result") or {}).get("value")


READ_LATER_MS = 250  # the read in a touch's round trip: this long after it
GUARD_TRIES = 3      # touches held back (see GUARD_JS) before nothing is tapped
VERDICT_WAIT_MS = 400  # a touch's verdict is waited for this long, on the page
MISSES = ("moved", "covered", "gone")
CLICK_TARGET = "window.__burnerGuard = null; window.__burnerTarget && window.__burnerTarget.click(); 'clicked'"
LINK_LOAD_S = 1.0    # a touched link to another page: its load is given this long to start


def read(page, cap=600):
    """The page's rows (see READ_JS), with title, url, readiness and the
    viewport's size and pixel ratio."""
    return page.eval(_js(READ_JS, cap)) or {}


def _read_or_again(page, res):
    """The page read that rode in a round trip (its result from
    call_many), or a read of its own when that one failed."""
    try:
        return _value(res)
    except RuntimeError:
        return read(page)


CLASSES = {"field": "android.widget.EditText", "check": "android.widget.CheckBox",
           "button": "android.widget.Button", "link": "android.view.View",
           "select": "android.widget.Spinner", "option": "android.widget.TextView",
           "text": "android.widget.TextView"}


# What XML can't carry and the reader doesn't need: control characters
# (a page's text can hold them) and lone surrogates (which no encoding
# can write); either made every read of the page fail.
_UNPRINTABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")


def _plain(v):
    return _UNPRINTABLE.sub("", str(v))


def _node(index, text, desc, cls, bounds, pkg, clickable=False, checkable=False,
          checked=False, enabled=True, focused=False, selected=False,
          scrollable=False, children=""):
    attrs = [("index", index), ("text", text), ("resource-id", ""), ("class", cls),
             ("package", pkg), ("content-desc", desc), ("checkable", checkable),
             ("checked", checked), ("clickable", clickable), ("enabled", enabled),
             ("focusable", clickable), ("focused", focused), ("scrollable", scrollable),
             ("long-clickable", False), ("password", False), ("selected", selected),
             ("visible-to-user", True), ("bounds", bounds)]
    body = " ".join("%s=%s" % (k, quoteattr(str(v).lower() if isinstance(v, bool) else _plain(v)))
                    for k, v in attrs)
    if children:
        return "<node %s>\n%s\n</node>" % (body, children)
    return "<node %s />" % body


def page_xml(screen, top, screen_h=0, pkg="com.android.chrome"):
    """A page read (see read) as a screen read: the hierarchy XML the
    screen reader would give, with the page's rows as nodes under a
    WebView placed `top` pixels down the screen (where the last real
    read had it), the address bar above it and the navigation bar below
    (to `screen_h`), so that rows print, plans and taps work unchanged.
    Boxes are device pixels: CSS pixels from the visual viewport's
    corner, times the page's zoom and the pixel ratio (a page laid out
    wider than the screen, no viewport meta, is drawn scaled down: its
    rows printed off the screen's edge, found in review Oct 5). Pure."""
    k = float(screen.get("vs") or 1) * float(screen.get("dpr") or 1)
    vx, vy = float(screen.get("vx") or 0), float(screen.get("vy") or 0)
    vw = float(screen.get("vvw") or screen.get("vw") or 0)
    vh = float(screen.get("vvh") or screen.get("vh") or 0)
    W, H = int(round(vw * k)), int(round(vh * k))
    top = int(top or 0)
    rows = []
    for i, r in enumerate(screen.get("rows") or []):
        x1 = max(0, int(round((r.get("l", 0) - vx) * k)))
        y1 = max(top, int(round(top + (r.get("t", 0) - vy) * k)))
        x2 = min(W, int(round((r.get("l", 0) + r.get("w", 0) - vx) * k)))
        y2 = min(top + H, int(round(top + (r.get("t", 0) + r.get("h", 0) - vy) * k)))
        if x2 <= x1 or y2 <= y1:
            continue
        kind = r.get("kind") or "text"
        text, desc = r.get("text") or "", r.get("desc") or ""
        if kind == "field":
            text, desc = r.get("value") or "", r.get("placeholder") or desc
        rows.append(_node(
            i + 1, text, desc, CLASSES.get(kind, CLASSES["text"]),
            "[%d,%d][%d,%d]" % (x1, y1, x2, y2), pkg,
            clickable=bool(r.get("click")) or kind in ("field", "button", "link", "check"),
            checkable=(kind == "check"), checked=bool(r.get("checked")),
            enabled=not r.get("disabled"), focused=bool(r.get("focused")),
            selected=bool(r.get("selected"))))
    parts = []
    url = screen.get("url") or ""
    if url and top >= 60:
        parts.append(_node(0, url, "", "android.widget.EditText",
                           "[%d,%d][%d,%d]" % (int(W * 0.2), max(0, top - 130),
                                               int(W * 0.62), max(1, top - 10)),
                           pkg, clickable=True))
    parts.append(_node(1, "", screen.get("title") or "", "android.webkit.WebView",
                       "[0,%d][%d,%d]" % (top, W, top + H), pkg, scrollable=True,
                       children="\n".join(rows)))
    if screen_h and screen_h > top + H:
        parts.append(_node(2, "", "", "android.widget.FrameLayout",
                           "[0,%d][%d,%d]" % (top + H, W, int(screen_h)),
                           "com.android.systemui"))
    # page="1": a page's read, not the screen reader's (it holds neither
    # the keyboard nor Chrome's own views, such as its address bar's focus);
    # after a scroll, how far the page moved and whether something holds it
    extra = ""
    if "moved" in screen:
        extra = ' moved="%d"' % int(screen.get("moved") or 0) + (' held="1"' if screen.get("held") else "")
    if "found" in screen:
        # what a `scroll --to` asked the page: whether it holds the words,
        # where, and the start of the element holding them
        extra += ' found="%s"' % screen["found"] + (' near="1"' if screen.get("near") else "")
        if screen.get("found_in"):
            extra += " found_in=%s" % quoteattr(_plain(screen["found_in"]))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<hierarchy rotation="0" page="1"%s>\n' % extra
            + "\n".join(parts) + "\n</hierarchy>")


QUIET_CAP_S = 0.5  # a wait for a quiet DOM, at most: a live page never stops changing
LOAD_CAP_S = 1.8   # a wait for a page that is loading, at most (the caller
                   # reads again when the screen looks half drawn)
BUSY_CAP_S = 3.0   # a read whose rows still stand in for content coming is
                   # read again this long at most (a skeleton that never fills
                   # costs no more)
BUSY_POLL_S = 0.25
VISIBLE_FOR_S = 45.0  # a page's read says whether its document is visible: that proof
                      # holds this long without another (no question of its own), and a
                      # read that says hidden ends it
QUICK_AFTER_S = 2.0   # a command QUICK_AFTER_S or more after the last proof waits
STALE_CALL_S = 3.0    # STALE_CALL_S at most: a tab frozen in the background (a touch
                      # that opened another app) answers nothing, and the caller then
                      # looks at the screen; a page on screen answers well within it


def settle(page, idle_ms=1200, poll_s=0.15, quiet_s=0.3, url=None, loading=False):
    """Wait for the page after a touch or a scroll. A navigation (the url
    differs from `url`, the one before the touch) or a document not yet
    parsed (readyState "loading") is waited out: until the page is
    parsed and quiet for `quiet_s`, `idle_ms` at most but at least
    LOAD_CAP_S. Otherwise a quiet spell of the DOM is waited for
    QUIET_CAP_S at most: a live page never stops changing, and a heavy
    page stays "interactive" for many seconds while its ads load, but
    its rows can be read any time. Returns the last probe."""
    t0 = time.monotonic()
    last, since = None, t0
    while True:
        try:
            probe = page.eval(SETTLE_JS, timeout=5.0) or {}
            if probe.get("vis") == "visible":
                page.visible_at = time.monotonic()
        except Exception:
            probe = {"ready": "?", "mut": None}
        now = time.monotonic()
        if probe.get("ready") == "loading" or (url and probe.get("url") != url):
            loading = True
        key = (probe.get("ready"), probe.get("mut"), probe.get("url"))
        if key != last:
            last, since = key, now
        elif probe.get("ready") != "loading" and now - since >= quiet_s:
            return probe
        limit = max(idle_ms / 1000.0, LOAD_CAP_S) if loading else min(idle_ms / 1000.0, QUIET_CAP_S)
        if now - t0 >= limit:
            return probe
        time.sleep(poll_s)


# The words of a row that stands in for content still coming: a
# skeleton's or a spinner's ("Loading", "Loading\u2026", "Please wait"), and a
# label of one on a row with no words of its own ("Loading price chart").
_BUSY_WORDS = re.compile(r"^(loading|please wait)\s*(\.{1,3}|\u2026)?$", re.I)
_BUSY_LABEL = re.compile(r"^loading\b", re.I)


def placeholders(screen):
    """How many rows of a page read (see read) stand in for content still
    coming (see _BUSY_WORDS). Pure."""
    n = 0
    for r in (screen or {}).get("rows") or []:
        text, desc = (r.get("text") or "").strip(), (r.get("desc") or "").strip()
        # a label counts on a row that isn't a control (a photo's alt text
        # "Loading dock ..." in a link is content, review of Oct 7)
        if _BUSY_WORDS.match(text) or (not text and not r.get("click") and _BUSY_LABEL.match(desc)):
            n += 1
    return n


def read_loaded(page, screen, cap_s=BUSY_CAP_S, poll_s=BUSY_POLL_S):
    """`screen`, a read just taken; or, when rows of it stand in for
    content still coming (see placeholders), the page read again until
    they are gone, `cap_s` at most. Coinbase's price page was read with
    its price still "Loading" (Oct 7), and the assistant took a
    screenshot to see the price. A read again that fails, or holds no
    rows (the page between documents), leaves the last one standing. The
    read returned carries "busy": [such rows at first, rows left, seconds
    waited] when it waited."""
    first = placeholders(screen)
    if not first:
        return screen
    t0, left = time.monotonic(), first
    while left and time.monotonic() - t0 < cap_s:
        time.sleep(poll_s)
        try:
            again = read(page)
        except Exception:
            break
        if again.get("rows"):
            screen, left = again, placeholders(again)
    screen = dict(screen, busy=[first, left, round(time.monotonic() - t0, 2)])
    return screen


def touch(page, x, y, then=()):
    """A touch at (x, y) in CSS pixels of the viewport, the way a finger
    lands (down and up sent together: one round trip), with `then`
    (commands, a read of the page a moment later) in the same trip.
    Returns (how, the results of `then`: a RuntimeError for one that
    failed); falls back to the element's own click when touch events are
    refused, and then nothing of `then` was asked."""
    cmds = [("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]}),
            ("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})] + list(then)
    # a stream that fails here raises (the caller reads the screen); a
    # touch the page refused comes back as its result, and the element's
    # own click stands in
    res = page.call_many(cmds, timeout=5.0, raise_errors=False)
    if any(isinstance(r, Exception) for r in res[:2]):
        page.eval(CLICK_TARGET)
        return "click", []
    return "touch", res[2:]


def after_touch(page, url=None, idle_ms=1200, listen_s=0.15):
    """What a touch led to, heard from the page's events (no round trip,
    and no question to a page mid-navigation, which Chrome answers only
    once the new page is up): a navigation is waited out until the new
    document is parsed, LOAD_CAP_S at most (idle_ms when longer);
    anything else is read right after the listen, since what a touch
    changes on a page is there by then. Returns {"ready": ...}."""
    page.listen(listen_s)
    if page.loading:
        parsed = page.wait_parsed(max(LOAD_CAP_S, idle_ms / 1000.0))
        return {"ready": "complete" if parsed else "loading"}
    return {"ready": "complete"}


TARGET_WAIT_MS = 700  # the field that a touch opens is waited for this long, on the page


def _target_fill(page, label, text):
    """After a touch on what opens a field: the field (TARGET_JS, waited
    for on the page) and its fill (FILL_JS) in one script, so the fill
    runs once the field is there, not on a stale target: (target, filled)."""
    try:
        res = page.eval(_js(TARGET_FILL_JS, label, TARGET_WAIT_MS, text)) or {}
    except RuntimeError:
        return {}, {}
    return res.get("target") or {}, res.get("filled") or {}


def find_read(page, label, top=0, exact=False):
    """find() with the read in the same round trip: (the find's answer,
    the screen). A wait's poll that lands has its read at once."""
    res = page.call_many([_evaluate(_js(FIND_JS, label, None, True, False, exact)),
                          _evaluate(_js(READ_JS, 600))], raise_errors=False)
    try:
        screen = _value(res[1]) or None
    except RuntimeError:
        screen = None
    return _found(_value(res[0]) or {}, label, top), screen


def find(page, label, top=0, exact=False):
    """Whether the page has an element with these words (see FIND_JS;
    the whole words only with `exact`), anywhere in the document:
    {"found": False}, or {"found": True, "text", "bounds" (device
    pixels, `top` down the screen), "enabled", "inview", "count"}. A
    wait's probe: cheaper than a read, and it sees below the fold, as
    the screen reader's tree did."""
    return _found(page.eval(_js(FIND_JS, label, None, True, False, exact)) or {}, label, top)


def _found(hit, label, top):
    if not hit.get("found"):
        return {"found": False}
    k = float(hit.get("vs") or 1) * float(hit.get("dpr") or 1)  # the page's zoom, the pixel ratio
    vx, vy = float(hit.get("vx") or 0), float(hit.get("vy") or 0)
    x1 = int(round((hit.get("l", 0) - vx) * k))
    y1 = int(round(top + (hit.get("t", 0) - vy) * k))
    x2 = int(round((hit.get("l", 0) + hit.get("w", 0) - vx) * k))
    y2 = int(round(top + (hit.get("t", 0) + hit.get("h", 0) - vy) * k))
    return {"found": True, "text": hit.get("label") or label, "desc": "",
            "bounds": "[%d,%d][%d,%d]" % (x1, y1, x2, y2), "enabled": bool(hit.get("enabled", True)),
            "inview": bool(hit.get("inview")), "count": hit.get("count", 1)}


class Held(RuntimeError):
    """Every touch on the target was held back (see GUARD_JS): nothing was
    tapped. `verdict`: the last one ("gone": the page replaced it)."""

    def __init__(self, message, verdict):
        RuntimeError.__init__(self, message)
        self.verdict = verdict


def _find_to_touch(page, label, index):
    """FIND_JS's answer, with the guard installed in its trip."""
    res = page.call_many([_evaluate(_js(FIND_JS, label, index)), _evaluate(_js(GUARD_JS))])
    return _value(res[0])


def _place(page):
    """PLACE_JS's answer ({} for a target gone), with the guard installed
    in its trip (PLACE_JS arms it)."""
    res = page.call_many([_evaluate(_js(PLACE_JS)), _evaluate(_js(GUARD_JS))])
    return _value(res[0]) or {}


def tap(page, label, index=None, idle_ms=1200):
    """Find the element by its words, touch it, read: {"found": True,
    "count": 1, "screen": ...}. {"found": False} when the words aren't
    on the page (after one more look 0.7s later); {"count": n, "labels":
    [...]} when several controls carry them. Two round trips: the find,
    then the touch with the read READ_LATER_MS after it in the same trip;
    a touch that starts a load (the page's events say so) is waited out
    and read afresh. Raises NotSent when the page can't be asked, before
    any touch."""
    try:
        hit = _find_to_touch(page, label, index)
        if (not hit or not hit.get("found")) and not (hit or {}).get("quiet"):
            time.sleep(0.7)  # a page still drawing shows the words a moment later
            hit = _find_to_touch(page, label, index)
    except Exception as e:
        raise NotSent(str(e)[:120])
    if not hit or not hit.get("found"):
        # nothing touched, and no read of the page: the caller looks for
        # the words among Chrome's own rows (a read for nothing, Oct 7)
        return {"found": False}
    if hit.get("count", 1) != 1:
        return hit  # several rows: nothing touched (the caller says so)
    if hit.get("chose") or hit.get("focused"):
        # a dropdown's option picked, or a dropdown focused: no touch
        time.sleep(0.15)
        return {"found": True, "count": 1, "label": hit.get("label"),
                "how": "chose" if hit.get("chose") else "focus", "screen": read(page)}
    page.window_opened = None
    try:
        how, after = touch_hit(page, hit, then=[_evaluate(_later(_js(READ_JS, 600), READ_LATER_MS))])
    except Held as e:
        if e.verdict != "gone":
            raise
        # the page replaced the target (a list drawn again): found again by
        # its words, once; nothing was touched
        again = _find_to_touch(page, label, index) or {}
        if not again.get("found") or again.get("count", 1) != 1 or again.get("chose") or again.get("focused"):
            raise
        hit = again
        how, after = touch_hit(page, hit, then=[_evaluate(_later(_js(READ_JS, 600), READ_LATER_MS))])
    screen, ready = None, "complete"
    if after and not page.loading:
        try:
            screen = _value(after[0])
        except RuntimeError:
            screen = None  # the document went away under the read: a load
    if page.window_opened:
        # the touch opened a new tab: this page isn't going anywhere, and
        # it isn't the screen any more (the caller reads the new tab)
        return {"found": True, "count": 1, "label": hit.get("label"), "how": how,
                "screen": screen or {}, "ready": "complete", "new_tab": page.window_opened,
                "at": [hit.get("x"), hit.get("y")], "tag": hit.get("tag") or "",
                "over": hit.get("over") or "", "moved": bool(hit.get("moved")),
                "covered": bool(hit.get("covered"))}
    if screen is not None and not page.loading and leaves_for(hit, screen):
        # a link to another page, and the page not going there yet a
        # moment after the touch: its load is given LINK_LOAD_S to start
        # (a listing opened half a second after the touch; the read before
        # that showed the list it left, and read as "nothing happened", Oct 5)
        page.wait_loading(LINK_LOAD_S)
    if screen is None or page.loading:
        probe = after_touch(page, hit.get("url"), idle_ms)
        screen, ready = read(page), probe.get("ready")
    screen = read_loaded(page, screen)
    return {"found": True, "count": 1, "label": hit.get("label"), "how": how,
            "screen": screen, "ready": ready,
            # where the touch went, for the helper's log: a failed tap
            # without it left nothing to go on (airbnb.com, Oct 5)
            "at": [hit.get("x"), hit.get("y")], "tag": hit.get("tag") or "",
            "over": hit.get("over") or "", "moved": bool(hit.get("moved")),
            "covered": bool(hit.get("covered")), "held": hit.get("held", 0)}


def leaves_for(hit, screen):
    """Whether the touch on `hit` (FIND_JS's answer) should take the page
    elsewhere, and `screen` (the read a moment after it) shows it hasn't
    gone yet: a link to another web address (not the page's own, a
    fragment aside), and the read's address still the page's. Pure."""
    href = (hit.get("href") or "").split("#")[0]
    here = (hit.get("url") or "").split("#")[0]
    now = ((screen or {}).get("url") or here).split("#")[0]
    return bool(href) and href.startswith("http") and href != here and now == here


def touch_at(page, x, y, screen, top, idle_ms=1200):
    """A touch at (x, y), device pixels of the screen, on the page (a tap
    by --xy or a snap handle): the point in the page's own CSS pixels by
    the mapping of `screen`, the page's last read (see page_xml; `top`
    is the WebView's top edge on the screen), the touch, and the read
    READ_LATER_MS later in the same round trip; a touch that starts a
    load is waited out and read afresh. {"screen", "ready", "how"}."""
    k = float(screen.get("vs") or 1) * float(screen.get("dpr") or 1)
    cx, cy = x / k, (y - top) / k
    page.loading, page.window_opened = False, None
    how, after = touch(page, cx, cy, then=[_evaluate(_later(_js(READ_JS, 600), READ_LATER_MS))])
    shot, ready = None, "complete"
    if after and not page.loading:
        try:
            shot = _value(after[0])
        except RuntimeError:
            shot = None  # the document went away under the read: a load
    if page.window_opened:
        # a new tab: the caller reads it (see tap)
        return {"screen": shot or {}, "ready": "complete", "how": how, "new_tab": page.window_opened}
    if shot is None or page.loading:
        probe = after_touch(page, None, idle_ms)
        shot, ready = read(page), probe.get("ready")
    return {"screen": read_loaded(page, shot), "ready": ready, "how": how}


def touch_hit(page, hit, then=()):
    """Touch the element FIND_JS found (`hit`, with its place): what
    covers it is closed first, its place taken afresh after a scroll
    into view, a keyboard going or a popup closing, and a cover that
    stays is bypassed with the element's own click. `then` rides in the
    touch's round trip (see touch). A touch the page held back because
    it missed the target (see GUARD_JS) is aimed again at the target's
    place now, GUARD_TRIES touches at most (hit["held"]: how many were
    held). Returns (how, results of `then`); raises Held when every
    touch was held back, or the target is gone (nothing was tapped)."""
    if hit.get("covered"):
        # something lies over the target (a date field's calendar):
        # Escape closes a widget's popup, as it would for a person
        page.call_many([("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape",
                                                    "windowsVirtualKeyCode": 27}),
                        ("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape",
                                                    "windowsVirtualKeyCode": 27})], timeout=5.0)
    if (hit.get("moved") and not hit.get("settled")) or hit.get("blurred") or hit.get("covered"):
        # the place taken afresh: the page's scroll into view is instant
        # (its reflow is in by the time this trip reaches it; the find
        # takes it itself, "settled"); the keyboard going and a popup
        # closing take a moment more (a link below the fold paid 0.35s for
        # nothing, Oct 7)
        if hit.get("blurred") or hit.get("covered"):
            time.sleep(0.35)
        place = _place(page)
        if place.get("x") is not None:
            hit["x"], hit["y"] = place["x"], place["y"]
        hit["covered"] = bool(place.get("covered"))
    page.loading = False
    held = []
    while True:
        if hit.get("covered"):
            # still covered: the element's own click, past whatever lies over it
            page.eval(CLICK_TARGET)
            return "click", []
        how, after = touch(page, hit["x"], hit["y"], [_evaluate(_js(VERDICT_JS, VERDICT_WAIT_MS))] + list(then))
        if how != "touch" or not after:
            return how, after
        try:
            verdict = _value(after[0]) or {}
        except RuntimeError:
            verdict = {}  # the document went away: the touch went in (a load)
        hit["held"] = len(held)
        if verdict.get("verdict") not in MISSES:
            return how, after[1:]
        # held back: the page saw none of it; aimed again where the target is
        held.append(verdict["verdict"] + (", %s under the point" % verdict["hit"] if verdict.get("hit") else ""))
        hit["held"] = len(held)
        place = _place(page) if verdict["verdict"] != "gone" else {}
        if place.get("x") is None or len(held) >= GUARD_TRIES:
            raise Held("nothing was tapped: the page moved %r from under the touch %d time%s (%s)" % (
                hit.get("used") or hit.get("label") or "the target", len(held), "" if len(held) == 1 else "s",
                "; ".join(held)),
                "gone" if place.get("x") is None else verdict["verdict"])
        hit["x"], hit["y"], hit["covered"] = place["x"], place["y"], bool(place.get("covered"))


def navigate(page, url, idle_ms=1000):
    """Load `url` in this page (Chrome's current tab), wait for it to be
    parsed and quiet (LOAD_CAP_S at most), read (once its rows that say
    "Loading" are filled, see read_loaded). Raises NotSent when the
    page can't be asked to load it (nothing was sent: the caller opens
    the link its own way; sent twice, a one-time link is used up)."""
    try:
        page.loading = True  # until the new document is parsed
        page.call("Page.navigate", 10.0, url=url)
    except NothingSent as e:
        page.loading = False
        raise NotSent(str(e)[:120])
    except (socket.timeout, TimeoutError):
        pass  # asked; Chrome answers once the load is committed, which a slow
              # server delays past the wait: the load is under way
    parsed = page.wait_parsed(max(LOAD_CAP_S, idle_ms / 1000.0))
    time.sleep(0.2)  # the first paint of a parsed page
    return {"screen": read_loaded(page, read(page)), "ready": "complete" if parsed else "loading"}


# The focused field's content selected, so inserted text replaces it.
SELECT_JS = r"""
(function(text){
  // the element with the focus, through shadow roots (a custom element's
  // box inside its shadow tree: document.activeElement names the host);
  // any editable one keeps it, so the text goes where a person's would
  let el = document.activeElement, only = false;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  const editable = e => !!e && e !== document.body && (e.isContentEditable || e.matches('input,textarea,select,[role=textbox],[role=searchbox],[role=combobox]'));
  if (!editable(el)) {
    // nothing has the focus: the one text field in view takes the text,
    // as a person would tap the only box; none or several: no guess
    const FIELDS = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]):not([type=file]):not([type=checkbox]):not([type=radio]),textarea,[contenteditable]:not([contenteditable=false]),[role=textbox],[role=searchbox],[role=combobox]';
    const vh = innerHeight, vw = innerWidth;
    const fields = Array.from(document.querySelectorAll(FIELDS)).filter(f => { const r = f.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw
        && (f.checkVisibility ? f.checkVisibility({visibilityProperty: true, opacityProperty: true}) : true); });
    if (fields.length !== 1) return {ok: false, fields: fields.length};
    el = fields[0]; only = true;
    try { el.focus(); } catch (e) {}
  }
  const type = (el.type || '').toLowerCase();
  if (/^(date|time|month|week|datetime-local|color|range)$/.test(type)) {
    // these take no typed text: the value is set (2026-10-05, 14:30,
    // #ff0000, 50) and the page told. The field keeps the focus: the
    // text that follows goes to it, and it has nowhere to put it (left,
    // the text went into the last text field instead).
    el.value = text;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return {ok: true, direct: true, value: el.value, type: type, only: only};
  }
  try {
    if (typeof el.select === 'function') el.select();
    else { const r = document.createRange(); r.selectNodeContents(el); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
  } catch (e) {}
  return {ok: true, direct: false, type: type, only: only};
})"""


# The field FIND kept (window.__burnerTarget) given `text`: a dropdown
# takes the option with those words; a date, time, color or range field
# takes it as its value; a checkbox or radio takes on/off; a text field
# is focused with its content selected, for the text that follows
# (Input.insertText), unless it can't take the focus.
FILL_JS = r"""
(function(text){
  const el = window.__burnerTarget;
  if (!el) return {ok: false, why: 'no field found'};
  if (!el.matches('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]):not([type=file]),textarea,select,[contenteditable=true],[role=textbox],[role=searchbox],[role=combobox]')) return {ok: false, why: 'not a field'};
  const squash = s => (s || '').replace(/[\u2018\u2019\u201B\u2032\u02BC\uFF07]/g, "'").replace(/[\u201C\u201D\u201E\u201F\u2033\uFF02]/g, '"').replace(/[\u2010-\u2015\u2212\uFE63\uFF0D]/g, '-').replace(/\u2026/g, '...').replace(/\s+/g, ' ').trim();
  const fire = () => { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); };
  const setValue = v => { const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value'); if (d && d.set) d.set.call(el, v); else el.value = v; fire(); };
  const tag = el.tagName.toLowerCase(), type = (el.type || '').toLowerCase();
  if (tag === 'select') {
    const want = squash(text).toLowerCase();
    const o = Array.from(el.options).find(o => squash(o.text).toLowerCase() === want) || Array.from(el.options).find(o => squash(o.text).toLowerCase().includes(want));
    if (!o) return {ok: false, why: 'no option ' + JSON.stringify(text)};
    el.value = o.value; fire();
    return {ok: true, mode: 'option', value: squash(o.text)};
  }
  if (type === 'checkbox' || type === 'radio') {
    const on = /^(on|yes|true|1|checked|x)$/i.test(squash(text));
    if (el.checked !== on) { el.click(); }
    return {ok: true, mode: 'check', value: el.checked ? 'on' : 'off'};
  }
  if (/^(date|time|month|week|datetime-local|color|range)$/.test(type)) {
    setValue(text);
    return {ok: true, mode: 'value', value: el.value};
  }
  if (el.disabled || el.readOnly) return {ok: false, why: 'the field is ' + (el.disabled ? 'disabled' : 'read-only')};
  try { el.focus(); if (typeof el.select === 'function') el.select(); else { const r = document.createRange(); r.selectNodeContents(el); const s = getSelection(); s.removeAllRanges(); s.addRange(r); } } catch (e) {}
  return {ok: true, mode: document.activeElement === el ? 'insert' : 'set', value: ''};
})"""

# After the insert: the field holds the text, or takes it outright.
FILLED_JS = r"""
(function(text){
  const el = window.__burnerTarget;
  if (!el) return {ok: false};
  const got = el.isContentEditable ? el.textContent : el.value;
  if ((got || '') === text) return {ok: true, value: got};
  const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
  if (el.isContentEditable) el.textContent = text; else if (d && d.set) d.set.call(el, text); else el.value = text;
  el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true, value: el.isContentEditable ? el.textContent : el.value, set: true};
})"""


# TARGET_JS then FILL_JS on what it found, as one promise: the fill runs
# after the field is there (two scripts sent together could run the
# fill first, on the target of the touch).
TARGET_FILL_JS = ("(function(label, waitMs, text){ return (%s)(label, waitMs)"
                  ".then(t => ({target: t, filled: t.ok ? (%s)(text) : null})); })") % (TARGET_JS, FILL_JS)


def fill(page, label, text, index=None):
    """`text` into the field with this label (see FIND_JS with fill, and
    FILL_JS), then a read. Two round trips: the find with the fill
    (the field focused with its content selected, or a value taken
    outright), then the text, the check (FILLED_JS) and the read.
    {"found": False} when no field has the label; {"count": n} when
    several do; raises NotSent when the page can't be asked before
    anything changed, NotDone when the field didn't take the text."""
    try:
        res = page.call_many([_evaluate(_js(FIND_JS, label, index, False, True)),
                              _evaluate(_js(FILL_JS, text)), _evaluate(_js(GUARD_JS))], raise_errors=False)
    except NothingSent as e:
        raise NotSent(str(e)[:120])
    # from here on a failure came after the fill went out (it clicks a
    # checkbox, picks an option): "failed after sending", not "not sent"
    hit = _value(res[0])
    if not hit or not hit.get("found"):
        return {"found": False}  # nothing filled; the caller says so (no read for it, Oct 7)
    if hit.get("count", 1) != 1:
        return hit
    how = "fill"
    if hit.get("notField"):
        # the words are on a button or a link (a search icon where the
        # page hides its box): touched, as a person would, and the text
        # goes to the field that opens (TARGET_JS), looked for once more
        # when the page is still drawing it
        touch_hit(page, hit)
        after_touch(page, hit.get("url"))
        target, filled = _target_fill(page, label, text)
        if not target.get("ok"):
            time.sleep(0.5)  # the page still drawing the field
            target, filled = _target_fill(page, label, text)
        if not target.get("ok"):
            n = target.get("fields", 0)
            raise NotDone("%r is a %s, not a field; tapping it opened %s" % (
                label, hit.get("tag") or "button",
                "no text field" if not n else "%d text fields, none labelled %r" % (n, label)))
        how = "tap+" + target.get("how", "fill")
    else:
        filled = _value(res[1]) or {}
    if not filled.get("ok"):
        raise NotDone("%r: %s" % (label, filled.get("why", "the field didn't take it")))
    mode = filled.get("mode")
    cmds = []
    if mode == "insert":
        cmds.append(("Input.insertText", {"text": text}))
    if mode in ("insert", "set"):
        cmds.append(_evaluate(_js(FILLED_JS, text)))
    cmds.append(_evaluate(_later(_js(READ_JS, 600), 200)))  # the field's own reaction (a list of suggestions)
    res = page.call_many(cmds, timeout=10.0, raise_errors=False)
    value = filled.get("value")
    if mode in ("insert", "set"):
        value = (_value(res[-2]) or {}).get("value")
    return {"found": True, "count": 1, "label": hit.get("label"), "mode": mode, "how": how,
            "value": value, "screen": _read_or_again(page, res[-1])}


def type_text(page, text, idle_ms=800):
    """Type `text` into the page's focused field, replacing its content
    (as the screen reader's set_text does): the content selected, the
    text inserted the way an IME commits it, and the read a moment
    later, in one round trip. Raises NotSent when the page couldn't be
    asked, or no field has the focus (the insert then had nowhere to
    go); a failure after the insert went out is not that."""
    try:
        res = page.call_many([_evaluate(_js(SELECT_JS, text)), ("Input.insertText", {"text": text}),
                              _evaluate(_later(_js(READ_JS, 600), 250))], raise_errors=False)
    except NothingSent as e:
        raise NotSent(str(e)[:120])
    sel = _value(res[0]) or {}
    if not sel.get("ok"):
        n = sel.get("fields", 0)
        raise NotSent("no field has the focus on the page" + (
            " (%d text fields in view; tap one, or type --field with its label)" % n if n else ""))
    if sel.get("direct") and str(sel.get("value", "")) != text:
        raise NotDone("the %s field didn't take %r (it holds %r)"
                      % (sel.get("type"), text, sel.get("value")))
    return {"screen": _read_or_again(page, res[2]), "ready": "complete",
            "direct": bool(sel.get("direct")), "only": bool(sel.get("only"))}


def page_text(page, start=0, count=20000, find=""):
    """The page's words, `count` characters from `start` (see TEXT_JS):
    {"title", "url", "total", "start", "next", "text"}; with `find`, the
    places at or after `start` with those words instead, each with the
    lines around it, `count` characters in all: {"title", "url", "total",
    "start", "places": [{"at", "text"}], "more": where the next place
    left out is, or -1}."""
    return page.eval(_js(TEXT_JS, int(start), int(count), find or ""), timeout=10.0) or {}


def scroll_to(page, label, direction="down"):
    """The words brought into view by the page itself (see SCROLL_TO_JS)
    and the read a moment later, in one round trip: {"found": "1" (in
    view now), "other" (only on the other side), "hidden" (only where
    they aren't shown) or "0" (not on the page), "near", "used", "moved",
    "screen"}; the read carries found, near and found_in for page_xml."""
    res = page.call_many([_evaluate(_js(SCROLL_TO_JS, label, direction)),
                          _evaluate(_later(_js(READ_JS, 600), 150))], timeout=10.0, raise_errors=False)
    try:
        hit = _value(res[0]) or {}
    except RuntimeError:
        hit = {}
    found = ("1" if hit.get("found") else "other" if hit.get("other") else
             "hidden" if hit.get("hidden") else "0")
    near = found == "other" and bool(hit.get("near"))
    screen = dict(_read_or_again(page, res[1]) or {}, found=found, near=near,
                  found_in=(hit.get("text") or "") if found == "1" else "")
    return {"found": found, "near": near, "used": hit.get("used") or "", "moved": hit.get("moved") or 0,
            "screen": screen}


def scroll(page, direction="down", times=1, fraction=0.6, idle_ms=500, grow_ms=0):
    """Scroll like a finger would, `times` times (or to an end: "top",
    "bottom"), and read once the rows are in place, all in one round
    trip (a scroll loads nothing). Returns the read and how far it moved
    (None when no scroll answered). grow_ms: at the end going down, the
    page this long at most to grow (a feed), each scroll and the read
    then waiting for the one before (see SCROLL_JS)."""
    where = direction if direction in ("top", "bottom") else None
    step = fraction * (1 if direction == "down" else -1)
    one = _js(SCROLL_JS, step, where, int(grow_ms))
    later = _later(_js(READ_JS, 600), 150)
    if grow_ms:
        after = "Promise.resolve(window.__burnerScroll).catch(() => null).then(() => %s)"
        one = "window.__burnerScroll = " + after % one
        later = after % later
    n = 1 if where else max(1, int(times))
    res = page.call_many([_evaluate(one)] * n + [_evaluate(later)],
                         timeout=10.0 + n * grow_ms / 1000.0, raise_errors=False)
    moved, last, held, answered, grew = 0, {}, False, 0, False
    for r in res[:-1]:
        try:
            last = _value(r) or {}
        except RuntimeError:
            continue
        answered += 1
        moved += last.get("moved") or 0
        held = held or bool(last.get("held"))
        grew = grew or bool(last.get("grew"))
    # the read says how far the page moved, and whether something over it
    # holds it (see page_xml): a scroll that moved nothing said "scrolled"
    # ten times on allrecipes.com (Oct 7). No scroll answered: it says
    # nothing (not "at its end", review Oct 7)
    screen = dict(_read_or_again(page, res[-1]) or {})
    if answered:
        screen.update(moved=moved, held=held)
    return {"moved": moved if answered else None, "scroller": last.get("scroller"), "held": held,
            "grew": grew, "screen": screen, "ready": "complete"}
