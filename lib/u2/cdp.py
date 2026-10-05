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
import struct
import time
from xml.sax.saxutils import quoteattr

SOCKET_NAME = "chrome_devtools_remote"
CHROME_PACKAGES = ("com.android.chrome", "com.chrome.beta", "com.chrome.dev",
                   "com.chrome.canary", "org.chromium.chrome")


class NotSent(RuntimeError):
    """A tap that failed before anything touched the page."""


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
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: the stream closed")
            resp += chunk
        head, self.buf = resp.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        if " 101 " not in status:
            raise ConnectionError("websocket handshake refused: %s" % status[:80])
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if accept not in head:
            raise ConnectionError("websocket handshake: wrong accept key")

    def _read(self, n):
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


def open_stream(dev):
    """A socket to Chrome's DevTools socket on the phone, over adb."""
    import adbutils
    return dev.create_connection(adbutils.Network.LOCAL_ABSTRACT, SOCKET_NAME)


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
    """One page's DevTools session: commands by id, page scripts by value."""

    def __init__(self, dev, target):
        self.target = target
        self.ws = WebSocket(open_stream(dev), "/devtools/page/" + target)
        self.n = 0
        self.visible_at = 0.0  # when a probe last said the page was visible

    def call(self, method, timeout=10.0, **params):
        self.n += 1
        self.ws.s.settimeout(timeout)
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") != self.n:
                continue  # an event; none are enabled, but be safe
            if "error" in m:
                raise RuntimeError("%s: %s" % (method, m["error"].get("message", m["error"])))
            return m.get("result", {})

    def eval(self, expression, timeout=10.0):
        """The value of a page script (a promise is awaited)."""
        r = self.call("Runtime.evaluate", timeout, expression=expression,
                      returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            ex = r["exceptionDetails"]
            text = ex.get("exception", {}).get("description") or ex.get("text") or "error"
            raise RuntimeError("page script failed: %s" % text.splitlines()[0][:120])
        return r.get("result", {}).get("value")

    def close(self):
        self.ws.close()


def pages(dev):
    """Chrome's open pages (tabs), as DevTools lists them."""
    return [t for t in http_get(dev, "/json") if t.get("type") == "page"]


def front_page(dev, current=None):
    """The page the user sees: `current` while it is still the visible
    one, else the visible page among Chrome's tabs (a fresh session for
    it). Raises RuntimeError when no page is visible (Chrome isn't in
    front, or shows a native screen)."""
    if current is not None:
        if time.monotonic() - current.visible_at < 1.5:
            return current  # a probe just said so: no round trip
        try:
            if current.eval("document.visibilityState", timeout=3.0) == "visible":
                current.visible_at = time.monotonic()
                return current
        except Exception:
            pass
        current.close()
    for t in pages(dev):
        if not t.get("id"):
            continue
        try:
            p = Page(dev, t["id"])
        except Exception:
            continue
        try:
            if p.eval("document.visibilityState", timeout=3.0) == "visible":
                p.visible_at = time.monotonic()
                return p
        except Exception:
            pass
        p.close()
    raise RuntimeError("no visible page in Chrome")


# ----------------------------------------------------------- page scripts
# Each is a function expression; it is called with JSON arguments.

# The rows a reader needs, in document order, each with its own words
# (its text nodes), else its aria-label, alt, title or placeholder; a
# link's or button's short text when it has no words of its own (its
# children then stay out). Boxes are CSS pixels of the viewport.
READ_JS = r"""
(function(cap){
  const vw = innerWidth, vh = innerHeight;
  const out = [], seen = new Set(), used = [];
  const squash = s => (s || '').replace(/\s+/g, ' ').trim();
  const own = el => { let t = ''; for (const c of el.childNodes) if (c.nodeType === 3) t += c.nodeValue; return squash(t); };
  const attr = el => squash(el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title'));
  const ACTIVE = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch],[role=option],[onclick]';
  const FIELD = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]):not([type=image]),textarea,[contenteditable=true]';
  const CHECK = 'input[type=checkbox],input[type=radio],[role=checkbox],[role=switch],[role=radio]';
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, { acceptNode: el =>
    el.matches('script,style,noscript,svg,template,iframe') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  for (let el = walker.nextNode(); el && out.length < cap; el = walker.nextNode()) {
    if (used.some(a => a.contains(el))) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
    if (el.checkVisibility ? !el.checkVisibility({visibilityProperty: true, opacityProperty: true}) : false) continue;
    const field = el.matches(FIELD), check = el.matches(CHECK), active = el.matches(ACTIVE);
    let text = own(el), desc = attr(el);
    if (!text && !desc && active && !field) { const t = squash(el.innerText); if (t && t.length <= 80) { text = t; used.push(el); } }
    if (!text && !desc && !field && !check) continue;
    const key = text + '|' + desc + '|' + Math.round(r.left) + ',' + Math.round(r.top) + ',' + Math.round(r.right) + ',' + Math.round(r.bottom);
    if (seen.has(key)) continue;
    seen.add(key);
    const kind = field ? 'field' : check ? 'check' : el.matches('button,[role=button],input[type=submit],input[type=button]') ? 'button' : (active ? 'link' : 'text');
    out.push({text: text.slice(0, 160), desc: desc.slice(0, 160), kind: kind,
              value: field ? squash(el.value || el.textContent).slice(0, 160) : '',
              placeholder: field ? squash(el.getAttribute('placeholder')).slice(0, 80) : '',
              click: active || !!el.closest(ACTIVE),
              checked: check ? (el.checked || el.getAttribute('aria-checked') === 'true') : false,
              focused: document.activeElement === el,
              selected: el.matches('[aria-selected=true],[aria-current]:not([aria-current=false])'),
              disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
              l: r.left, t: r.top, w: r.width, h: r.height});
  }
  return {title: document.title, url: location.href, ready: document.readyState,
          dpr: window.devicePixelRatio || 1, vw: vw, vh: vh, rows: out, more: out.length >= cap};
})"""

# The element with these words (exact, case-insensitive, whitespace
# squashed; "A || B" tries each; then as a part of longer words), scrolled
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
(function(label, index){
  const squash = s => (s || '').replace(/\s+/g, ' ').trim();
  const own = el => { let t = ''; for (const c of el.childNodes) if (c.nodeType === 3) t += c.nodeValue; return squash(t); };
  const attr = el => squash(el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder') || (el.tagName === 'INPUT' ? el.value : ''));
  const ACTIVE = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch],[role=option],[onclick]';
  const vw = innerWidth, vh = innerHeight;
  const skip = el => !!el.closest('script,style,noscript,svg,template');
  const visible = el => el.checkVisibility ? el.checkVisibility({visibilityProperty: true, opacityProperty: true}) : true;
  const box = el => el.getBoundingClientRect();
  const inView = r => r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
  const cands = new Set();
  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let t = tw.nextNode(); t; t = tw.nextNode()) { const p = t.parentElement; if (p && squash(t.nodeValue) && !skip(p)) cands.add(p); }
  for (const el of document.body.querySelectorAll('[aria-label],[alt],[title],[placeholder],input,' + ACTIVE)) if (!skip(el)) cands.add(el);
  const names = el => { const n = [own(el), attr(el)]; if (el.matches(ACTIVE)) { const t = squash(el.textContent); if (t && t.length <= 80) n.push(t); } return n.filter(Boolean).map(s => s.toLowerCase()); };
  const alts = label.split('||').map(squash).filter(Boolean);
  let used = '';
  const find = test => { for (const alt of alts) { const want = alt.toLowerCase(); const h = [];
    for (const el of cands) if (visible(el) && names(el).some(n => test(n, want))) h.push(el);
    if (h.length) { used = alt; return h; } } return []; };
  let hits = find((n, w) => n === w);
  if (!hits.length) hits = find((n, w) => n.includes(w));
  if (!hits.length) return {found: false};
  let controls = hits.filter(el => !hits.some(o => o !== el && el.contains(o)));
  const seen = controls.filter(el => inView(box(el)));
  if (seen.length) controls = seen;
  const pick = (index === null || index === undefined) ? null : index;
  if (controls.length > 1 && (pick === null || pick >= controls.length)) {
    return {found: true, count: controls.length, used: used, labels: controls.slice(0, 6).map(el => (names(el)[0] || '').slice(0, 60))};
  }
  const el = controls[pick || 0];
  let r = box(el), moved = false;
  if (!inView(r) || r.top < 0 || r.bottom > vh) { el.scrollIntoView({block: 'center', inline: 'nearest'}); r = box(el); moved = true; }
  window.__burnerTarget = el;
  return {found: true, count: 1, used: used, label: (names(el)[0] || '').slice(0, 60),
          x: r.left + r.width / 2, y: r.top + r.height / 2, moved: moved};
})"""

# A settle probe: the page's readiness and a count of DOM changes, so a
# tap's or scroll's result is read once the page stops changing.
SETTLE_JS = r"""
(function(){
  if (!window.__burnerMut) { window.__burnerMut = 1; try { new MutationObserver(() => { window.__burnerMut++; }).observe(document, {childList: true, subtree: true, characterData: true, attributes: true}); } catch (e) {} }
  return {ready: document.readyState, mut: window.__burnerMut, url: location.href, vis: document.visibilityState};
})"""

# A scroll by `dy` CSS pixels: the window, else the tallest element that
# scrolls (a page laid out inside one scrolling box). "top"/"bottom" go
# to the ends. Returns how far it moved.
SCROLL_JS = r"""
(function(dy, where){
  const go = (el, isWin) => {
    const at = () => isWin ? scrollY : el.scrollTop;
    const before = at();
    if (where === 'top') (isWin ? window : el).scrollTo(0, 0);
    else if (where === 'bottom') (isWin ? window : el).scrollTo(0, 1e9);
    else (isWin ? window : el).scrollBy(0, dy);
    return at() - before;
  };
  let moved = go(window, true);
  if (moved !== 0) return {moved: moved, scroller: 'page'};
  let best = null, bestH = 0;
  for (const el of document.querySelectorAll('div,main,section,article,ul,body')) {
    if (el.scrollHeight <= el.clientHeight + 1 || el.clientHeight < innerHeight / 3) continue;
    const oy = getComputedStyle(el).overflowY;
    if (oy !== 'auto' && oy !== 'scroll') continue;
    if (el.clientHeight > bestH) { best = el; bestH = el.clientHeight; }
  }
  if (!best) return {moved: 0, scroller: null};
  moved = go(best, false);
  return {moved: moved, scroller: best.tagName.toLowerCase()};
})"""


def _js(fn, *args):
    return "(%s)(%s)" % (fn, ", ".join(json.dumps(a) for a in args))


def read(page, cap=160):
    """The page's rows (see READ_JS), with title, url, readiness and the
    viewport's size and pixel ratio."""
    return page.eval(_js(READ_JS, cap)) or {}


CLASSES = {"field": "android.widget.EditText", "check": "android.widget.CheckBox",
           "button": "android.widget.Button", "link": "android.view.View",
           "text": "android.widget.TextView"}


def _node(index, text, desc, cls, bounds, pkg, clickable=False, checkable=False,
          checked=False, enabled=True, focused=False, selected=False,
          scrollable=False, children=""):
    attrs = [("index", index), ("text", text), ("resource-id", ""), ("class", cls),
             ("package", pkg), ("content-desc", desc), ("checkable", checkable),
             ("checked", checked), ("clickable", clickable), ("enabled", enabled),
             ("focusable", clickable), ("focused", focused), ("scrollable", scrollable),
             ("long-clickable", False), ("password", False), ("selected", selected),
             ("visible-to-user", True), ("bounds", bounds)]
    body = " ".join("%s=%s" % (k, quoteattr(str(v).lower() if isinstance(v, bool) else str(v)))
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
    Boxes are device pixels: CSS pixels times the pixel ratio. Pure."""
    dpr = float(screen.get("dpr") or 1)
    vw, vh = float(screen.get("vw") or 0), float(screen.get("vh") or 0)
    W, H = int(round(vw * dpr)), int(round(vh * dpr))
    top = int(top or 0)
    rows = []
    for i, r in enumerate(screen.get("rows") or []):
        x1 = max(0, int(round(r.get("l", 0) * dpr)))
        y1 = max(top, int(round(top + r.get("t", 0) * dpr)))
        x2 = min(W, int(round((r.get("l", 0) + r.get("w", 0)) * dpr)))
        y2 = min(top + H, int(round(top + (r.get("t", 0) + r.get("h", 0)) * dpr)))
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
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<hierarchy rotation="0">\n'
            + "\n".join(parts) + "\n</hierarchy>")


def settle(page, idle_ms=1200, poll_s=0.15, quiet_s=0.3):
    """Wait until the page has stopped changing for `quiet_s` (and is
    loaded), `idle_ms` at most. Returns the last probe."""
    t0 = time.monotonic()
    last, since = None, time.monotonic()
    probe = {}
    while True:
        try:
            probe = page.eval(SETTLE_JS, timeout=5.0) or {}
            if probe.get("vis") == "visible":
                page.visible_at = time.monotonic()
        except Exception:
            probe = {"ready": "?", "mut": None}
        key = (probe.get("ready"), probe.get("mut"), probe.get("url"))
        now = time.monotonic()
        if key != last:
            last, since = key, now
        elif probe.get("ready") == "complete" and now - since >= quiet_s:
            return probe
        if now - t0 >= idle_ms / 1000.0:
            return probe
        time.sleep(poll_s)


def touch(page, x, y):
    """A touch at (x, y) in CSS pixels of the viewport, the way a finger
    lands; falls back to the element's own click when touch events are
    refused."""
    try:
        page.call("Input.dispatchTouchEvent", 5.0, type="touchStart",
                  touchPoints=[{"x": x, "y": y}])
        page.call("Input.dispatchTouchEvent", 5.0, type="touchEnd", touchPoints=[])
        return "touch"
    except RuntimeError:
        page.eval("window.__burnerTarget && window.__burnerTarget.click(); 'clicked'")
        return "click"


def tap(page, label, index=None, idle_ms=1200):
    """Find the element by its words, touch it, wait for the page to
    settle, read: {"found": True, "count": 1, "screen": ...}. {"found":
    False} when the words aren't on the page (after one more look 0.7s
    later); {"count": n, "labels": [...]} when several controls carry
    them. Raises NotSent when the page can't be asked, before any touch."""
    try:
        hit = page.eval(_js(FIND_JS, label, index))
        if not hit or not hit.get("found"):
            time.sleep(0.7)  # a page still drawing shows the words a moment later
            hit = page.eval(_js(FIND_JS, label, index))
    except Exception as e:
        raise NotSent(str(e)[:120])
    if not hit or not hit.get("found"):
        return {"found": False, "screen": read(page)}
    if hit.get("count", 1) != 1:
        hit["screen"] = read(page)
        return hit
    if hit.get("moved"):
        time.sleep(0.3)  # the scroll into view
    how = touch(page, hit["x"], hit["y"])
    probe = settle(page, idle_ms)
    return {"found": True, "count": 1, "label": hit.get("label"), "how": how,
            "screen": read(page), "ready": probe.get("ready")}


def scroll(page, direction="down", times=1, fraction=0.6, idle_ms=500):
    """Scroll like a finger would, `times` times (or to an end: "top",
    "bottom"), then read once the page settles. Returns the read and how
    far it moved."""
    where = direction if direction in ("top", "bottom") else None
    dy = 0
    if where is None:
        h = page.eval("innerHeight") or 800
        dy = int(h * fraction) * (1 if direction == "down" else -1)
    moved = 0
    for i in range(1 if where else max(1, int(times))):
        r = page.eval(_js(SCROLL_JS, dy, where)) or {}
        moved += r.get("moved") or 0
        if i < times - 1:
            time.sleep(0.15)
    probe = settle(page, idle_ms)
    return {"moved": moved, "screen": read(page), "ready": probe.get("ready")}
