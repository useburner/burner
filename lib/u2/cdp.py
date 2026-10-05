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
import select
import socket
import struct
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
    """One page's DevTools session: commands by id, page scripts by
    value, and the page's navigation events (the Page domain is enabled
    at the start): `loading` is True from the main frame's start of a
    load until its document is parsed."""

    def __init__(self, dev, target):
        self.target = target
        self.ws = WebSocket(open_stream(dev), "/devtools/page/" + target)
        self.n = 0
        self.visible_at = 0.0  # when a probe last said the page was visible
        self.loading = False
        self.main_frame = None
        self.url = ""
        res = self.call_many([("Page.enable", {}), ("Page.getFrameTree", {})])
        frame = (res[1].get("frameTree") or {}).get("frame") or {}
        self.main_frame, self.url = frame.get("id"), frame.get("url", "")

    def _event(self, m):
        method, p = m.get("method", ""), m.get("params") or {}
        if method == "Page.frameStartedLoading" and p.get("frameId") == self.main_frame:
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
        while True:
            left = end - time.monotonic()
            if left <= 0 or not self._readable(left):
                return
            self.ws.s.settimeout(5.0)
            m = json.loads(self.ws.recv())
            if "id" not in m:
                self._event(m)

    def wait_parsed(self, cap_s):
        """Wait, listening, until the loading document is parsed, `cap_s`
        at most. True when it is."""
        end = time.monotonic() + cap_s
        while self.loading and time.monotonic() < end:
            self.listen(min(0.25, max(0.0, end - time.monotonic())))
        return not self.loading

    def call(self, method, timeout=10.0, **params):
        self.n += 1
        self.ws.s.settimeout(timeout)
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") != self.n:
                if "id" not in m:
                    self._event(m)
                continue
            if "error" in m:
                raise RuntimeError("%s: %s" % (method, m["error"].get("message", m["error"])))
            return m.get("result", {})

    def call_many(self, cmds, timeout=10.0):
        """Several commands sent at once, one round trip: their results
        in order. cmds: [(method, params), ...]. Raises on the first
        command that failed, after all have been answered."""
        ids = []
        self.ws.s.settimeout(timeout)
        for method, params in cmds:
            self.n += 1
            ids.append(self.n)
            self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
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
                raise RuntimeError("%s: %s" % (method, m["error"].get("message", m["error"])))
            out.append(m.get("result", {}))
        return out

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


PROBE_S = 1.5    # a visibility question to a page: a background tab answers only on the timeout
SCAN_TABS = 3    # tabs looked at for the visible page (Chrome lists the current one first)


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


def front_page(dev, current=None):
    """The page the user sees: `current` while it is still the visible
    one, else the visible page among Chrome's first SCAN_TABS tabs (a
    fresh session for it; Chrome lists the current tab first, and a
    phone had 107). Raises RuntimeError when none of them is visible
    (Chrome isn't in front, or shows a native screen)."""
    if current is not None:
        if visible(current):
            return current
        current.close()
    for t in pages(dev)[:SCAN_TABS]:
        if not t.get("id"):
            continue
        try:
            p = Page(dev, t["id"])
        except Exception:
            continue
        if visible(p):
            return p
        p.close()
    raise RuntimeError("no visible page in Chrome's first %d tabs" % SCAN_TABS)


# ----------------------------------------------------------- page scripts
# Each is a function expression; it is called with JSON arguments.

# The rows a reader needs, in document order, each with its own words
# (its text nodes), else its aria-label, alt, title or placeholder; a
# link's or button's short text when it has no words of its own (its
# children then stay out). Boxes are CSS pixels of the viewport.
READ_JS = r"""
(function(cap){
  const t0 = performance.now();
  const vw = innerWidth, vh = innerHeight;
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
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, { acceptNode: el =>
    el.matches('script,style,noscript,svg,template,iframe') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  for (let el = walker.nextNode(); el && out.length < cap; el = walker.nextNode()) {
    if (used.some(a => a.contains(el))) continue;
    const field = el.matches(FIELD), check = el.matches(CHECK), active = el.matches(ACTIVE), sel = el.tagName === 'SELECT';
    let text = own(el), desc = attr(el), fromText = false;
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
    const kind = sel ? 'select' : field ? 'field' : check ? 'check' : el.matches('button,[role=button],input[type=submit],input[type=button]') ? 'button' : (active ? 'link' : 'text');
    out.push({text: text.slice(0, 160), desc: desc.slice(0, 160), kind: kind,
              value: field ? squash(el.value || el.textContent).slice(0, 160) : '',
              placeholder: field ? squash(el.getAttribute('placeholder')).slice(0, 80) : '',
              click: active || !!el.closest(ACTIVE),
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
  return {title: document.title, url: location.href, ready: document.readyState,
          dpr: window.devicePixelRatio || 1, vw: vw, vh: vh, rows: out, more: out.length >= cap,
          ms: Math.round(performance.now() - t0)};
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
(function(label, index, query, fill){
  const squash = s => (s || '').replace(/\s+/g, ' ').trim();
  const own = el => { let t = ''; for (const c of el.childNodes) if (c.nodeType === 3) t += c.nodeValue; return squash(t); };
  const attr = el => squash(el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder') || (el.tagName === 'INPUT' ? el.value : ''));
  const byValue = el => el.tagName === 'INPUT' && !!squash(el.value) && !squash(el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder'));
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
  const cands = new Set();
  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let t = tw.nextNode(); t; t = tw.nextNode()) { const p = t.parentElement; if (p && squash(t.nodeValue) && !skip(p) && p.tagName !== 'OPTION') cands.add(p); }
  for (const el of document.body.querySelectorAll('[aria-label],[alt],[title],[placeholder],input,' + ACTIVE)) if (!skip(el)) cands.add(el);
  if (focusedSelect) for (const o of focusedSelect.options) cands.add(o);
  const names = el => {
    if (el.tagName === 'OPTION') return [squash(el.text).toLowerCase()].filter(Boolean);
    const n = [own(el), attr(el)];
    if (el.tagName === 'SELECT') { const o = el.options[el.selectedIndex]; n.push(squash(o ? o.text : '')); n.push(labelOf(el)); }
    else if (el.matches('input,textarea')) n.push(labelOf(el));
    else if (el.matches(ACTIVE)) { const t = squash(el.innerText); if (t && t.length <= 80) n.push(t); }
    return n.filter(Boolean).map(s => s.toLowerCase()); };
  const alts = label.split('||').map(squash).filter(Boolean);
  let used = '';
  const find = test => { for (const alt of alts) { const want = alt.toLowerCase(); const h = [];
    for (const el of cands) if (visible(el) && names(el).some(n => test(n, want))) h.push(el);
    if (h.length) { used = alt; return h; } } return []; };
  let hits = find((n, w) => n === w);
  if (!hits.length) hits = find((n, w) => n.includes(w));
  if (!hits.length) return {found: false};
  // the words typed into a field are not its label when something else
  // carries them (a search box holding "Pixel 7" beside that suggestion)
  if (hits.length > 1) { const named = hits.filter(el => !byValue(el)); if (named.length) hits = named; }
  let controls = hits.filter(el => !hits.some(o => o !== el && el.contains(o)));
  // a label beside the control it labels is that control
  // a label found by its words stands for the control it labels
  controls = controls.map(el => (el.tagName === 'LABEL' && el.control) ? el.control : el).filter((el, i, a) => a.indexOf(el) === i);
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
  if (controls.length > 1 && (pick === null || pick >= controls.length)) {
    return {found: true, count: controls.length, used: used, labels: controls.slice(0, 6).map(el => (names(el)[0] || '').slice(0, 60))};
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
    if (!inView(r)) el.scrollIntoView({block: 'center', inline: 'nearest'});
    window.__burnerTarget = el;
    return {found: true, count: 1, used: used, label: (names(el)[0] || '').slice(0, 60),
            tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), url: location.href};
  }
  if (!query && el.tagName === 'INPUT' && /^(date|time|month|week|datetime-local|color|range)$/.test(el.type)) {
    // focused, not touched (a touch opens Chrome's native picker, which a
    // page read can't see; a touch on a slider moves it): `type` sets its value
    if (!inView(r)) el.scrollIntoView({block: 'center', inline: 'nearest'});
    el.focus();
    return {found: true, count: 1, used: used, label: (names(el)[0] || '').slice(0, 60), focused: true, url: location.href};
  }
  if (!query && el.tagName === 'SELECT') {
    // focused, not touched (a touch opens Chrome's native popup, which a
    // page read can't see): its options print as rows on the next read
    if (!inView(r)) el.scrollIntoView({block: 'center', inline: 'nearest'});
    el.focus();
    return {found: true, count: 1, used: used, label: (names(el)[0] || '').slice(0, 60), focused: true, url: location.href};
  }
  if (query) {
    return {found: true, count: controls.length, used: used, label: (names(el)[0] || '').slice(0, 60),
            l: r.left, t: r.top, w: r.width, h: r.height, inview: inView(r), dpr: window.devicePixelRatio || 1,
            enabled: !(el.disabled || el.getAttribute('aria-disabled') === 'true')};
  }
  if (!inView(r) || r.top < 0 || r.bottom > vh) { el.scrollIntoView({block: 'center', inline: 'nearest'}); r = box(el); moved = true; }
  window.__burnerTarget = el;
  // a text field with the focus keeps the keyboard up, which pushes the
  // visual viewport: leave it first (as a person tapping elsewhere does),
  // and the caller takes the place afresh (PLACE_JS)
  let blurred = false;
  const act = document.activeElement;
  if (act && act !== el && !el.contains(act) && act.matches('input,textarea,[contenteditable=true]')) { act.blur(); blurred = true; }
  const vv = window.visualViewport;
  // what is under the aimed point: a widget's popup (a date field's
  // calendar) over the target swallows the touch; a part of the same
  // control (an icon beside a button's hidden words) is the target itself
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const top = document.elementFromPoint(cx, cy);
  const ctl = el.closest(ACTIVE) || el;
  const covered = !!(top && top !== el && !ctl.contains(top) && !top.contains(el));
  return {found: true, count: 1, used: used, label: (names(el)[0] || '').slice(0, 60),
          x: cx - (vv ? vv.offsetLeft : 0), y: cy - (vv ? vv.offsetTop : 0),
          moved: moved, blurred: blurred, covered: covered, notField: notField, tag: el.tagName.toLowerCase(),
          cover: covered ? (top.tagName + ' ' + squash(top.innerText).slice(0, 40)) : '', url: location.href};
})"""

# After a touch on what opens a field (a search icon): the field the text
# goes into, kept for FILL_JS. The field with the focus (an overlay's box
# takes it); else the field with the label (the page that opened has its
# own box); else the one field in view (a person types into the only box).
TARGET_JS = r"""
(function(label){
  const squash = s => (s || '').replace(/\s+/g, ' ').trim();
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
  const a = document.activeElement;
  if (a && a !== document.body && a.matches(FIELDS)) { window.__burnerTarget = a; return {ok: true, how: 'focus', label: names(a)[0] || ''}; }
  const fields = Array.from(document.querySelectorAll(FIELDS)).filter(inView);
  const want = squash(label).toLowerCase();
  const named = fields.filter(el => names(el).some(n => n === want || n.includes(want)));
  const pick = named.length === 1 ? named[0] : (!named.length && fields.length === 1 ? fields[0] : null);
  if (!pick) return {ok: false, fields: fields.length, named: named.length};
  window.__burnerTarget = pick;
  return {ok: true, how: named.length === 1 ? 'label' : 'only', label: names(pick)[0] || ''};
})"""

# The place of the element FIND kept, against the visual viewport (what
# a touch is aimed at), once the keyboard has gone.
PLACE_JS = r"""
(function(){
  const el = window.__burnerTarget;
  if (!el) return null;
  const vv = window.visualViewport, vh = vv ? vv.height : innerHeight;
  let r = el.getBoundingClientRect();
  const top = r.top - (vv ? vv.offsetTop : 0), bottom = r.bottom - (vv ? vv.offsetTop : 0);
  if (top < 0 || bottom > vh) { el.scrollIntoView({block: 'center', inline: 'nearest'}); r = el.getBoundingClientRect(); }
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const over = document.elementFromPoint(cx, cy);
  const ctl = el.closest('a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch],[role=option],[onclick]') || el;
  const covered = !!(over && over !== el && !ctl.contains(over) && !over.contains(el));
  return {x: cx - (vv ? vv.offsetLeft : 0), y: cy - (vv ? vv.offsetTop : 0), covered: covered};
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


def read(page, cap=600):
    """The page's rows (see READ_JS), with title, url, readiness and the
    viewport's size and pixel ratio."""
    return page.eval(_js(READ_JS, cap)) or {}


CLASSES = {"field": "android.widget.EditText", "check": "android.widget.CheckBox",
           "button": "android.widget.Button", "link": "android.view.View",
           "select": "android.widget.Spinner", "option": "android.widget.TextView",
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


QUIET_CAP_S = 0.5  # a wait for a quiet DOM, at most: a live page never stops changing
LOAD_CAP_S = 1.8   # a wait for a page that is loading, at most (the caller
                   # reads again when the screen looks half drawn)
VISIBLE_FOR_S = 6.0  # a page a probe found visible needs no new check this long: a
                     # tap that opens another tab leaves this one hidden on its own probe


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
    probe = {}
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


def touch(page, x, y):
    """A touch at (x, y) in CSS pixels of the viewport, the way a finger
    lands (down and up sent together: one round trip); falls back to the
    element's own click when touch events are refused."""
    try:
        page.call_many([("Input.dispatchTouchEvent",
                         {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]}),
                        ("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})],
                       timeout=5.0)
        return "touch"
    except RuntimeError:
        page.eval("window.__burnerTarget && window.__burnerTarget.click(); 'clicked'")
        return "click"


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


def find(page, label, top=0):
    """Whether the page has an element with these words (see FIND_JS),
    anywhere in the document: {"found": False}, or {"found": True,
    "text", "bounds" (device pixels, `top` down the screen), "enabled",
    "inview", "count"}. A wait's probe: cheaper than a read, and it sees
    below the fold, as the screen reader's tree did."""
    hit = page.eval(_js(FIND_JS, label, None, True)) or {}
    if not hit.get("found"):
        return {"found": False}
    dpr = float(hit.get("dpr") or 1)
    x1 = int(round(hit.get("l", 0) * dpr))
    y1 = int(round(top + hit.get("t", 0) * dpr))
    x2 = int(round((hit.get("l", 0) + hit.get("w", 0)) * dpr))
    y2 = int(round(top + (hit.get("t", 0) + hit.get("h", 0)) * dpr))
    return {"found": True, "text": hit.get("label") or label, "desc": "",
            "bounds": "[%d,%d][%d,%d]" % (x1, y1, x2, y2), "enabled": bool(hit.get("enabled", True)),
            "inview": bool(hit.get("inview")), "count": hit.get("count", 1)}


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
    if hit.get("chose") or hit.get("focused"):
        # a dropdown's option picked, or a dropdown focused: no touch
        time.sleep(0.15)
        return {"found": True, "count": 1, "label": hit.get("label"),
                "how": "chose" if hit.get("chose") else "focus", "screen": read(page)}
    how = touch_hit(page, hit)
    probe = after_touch(page, hit.get("url"), idle_ms)
    return {"found": True, "count": 1, "label": hit.get("label"), "how": how,
            "screen": read(page), "ready": probe.get("ready")}


def touch_hit(page, hit):
    """Touch the element FIND_JS found (`hit`, with its place): what
    covers it is closed first, its place taken afresh after a scroll
    into view, a keyboard going or a popup closing, and a cover that
    stays is bypassed with the element's own click. Returns how."""
    if hit.get("covered"):
        # something lies over the target (a date field's calendar):
        # Escape closes a widget's popup, as it would for a person
        page.call_many([("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape",
                                                    "windowsVirtualKeyCode": 27}),
                        ("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape",
                                                    "windowsVirtualKeyCode": 27})], timeout=5.0)
    if hit.get("moved") or hit.get("blurred") or hit.get("covered"):
        time.sleep(0.35)  # the scroll into view, the keyboard going, a popup closing
        place = page.eval(_js(PLACE_JS)) or {}
        if place.get("x") is not None:
            hit["x"], hit["y"] = place["x"], place["y"]
        hit["covered"] = bool(place.get("covered"))
    page.loading = False
    if hit.get("covered"):
        # still covered: the element's own click, past whatever lies over it
        page.eval("window.__burnerTarget && window.__burnerTarget.click(); 'clicked'")
        return "click"
    return touch(page, hit["x"], hit["y"])


def navigate(page, url, idle_ms=1000):
    """Load `url` in this page (Chrome's current tab), wait for it to be
    parsed and quiet (LOAD_CAP_S at most), read. Raises NotSent when the
    page can't be asked to load it."""
    try:
        page.loading = True  # until the new document is parsed
        page.call("Page.navigate", 10.0, url=url)
    except Exception as e:
        page.loading = False
        raise NotSent(str(e)[:120])
    parsed = page.wait_parsed(max(LOAD_CAP_S, idle_ms / 1000.0))
    time.sleep(0.2)  # the first paint of a parsed page
    return {"screen": read(page), "ready": "complete" if parsed else "loading"}


# The focused field's content selected, so inserted text replaces it.
SELECT_JS = r"""
(function(text){
  let el = document.activeElement, only = false;
  if (!el || el === document.body || !el.matches('input,textarea,select,[contenteditable=true],[role=textbox],[role=searchbox],[role=combobox]')) {
    // nothing has the focus: the one text field in view takes the text,
    // as a person would tap the only box; none or several: no guess
    const FIELDS = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]):not([type=file]):not([type=checkbox]):not([type=radio]),textarea,[contenteditable=true],[role=textbox],[role=searchbox],[role=combobox]';
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
  const squash = s => (s || '').replace(/\s+/g, ' ').trim();
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


def fill(page, label, text, index=None):
    """`text` into the field with this label (see FIND_JS with fill, and
    FILL_JS), then a read. {"found": False} when no field has the label;
    {"count": n} when several do; raises NotSent when the page can't be
    asked before anything changed, RuntimeError when the field didn't
    take the text."""
    try:
        hit = page.eval(_js(FIND_JS, label, index, False, True))
    except Exception as e:
        raise NotSent(str(e)[:120])
    if not hit or not hit.get("found"):
        return {"found": False, "screen": read(page)}
    if hit.get("count", 1) != 1:
        hit["screen"] = read(page)
        return hit
    how = "fill"
    if hit.get("notField"):
        # the words are on a button or a link (a search icon where the
        # page hides its box): touched, as a person would, and the text
        # goes to the field that opens (TARGET_JS), looked for once more
        # when the page is still drawing it
        how = touch_hit(page, hit)
        after_touch(page, hit.get("url"))
        target = page.eval(_js(TARGET_JS, label)) or {}
        if not target.get("ok"):
            time.sleep(0.5)
            target = page.eval(_js(TARGET_JS, label)) or {}
        if not target.get("ok"):
            n = target.get("fields", 0)
            raise NotDone("%r is a %s, not a field; tapping it opened %s" % (
                label, hit.get("tag") or "button",
                "no text field" if not n else "%d text fields, none labelled %r" % (n, label)))
        how = "tap+" + target.get("how", "fill")
    res = page.eval(_js(FILL_JS, text))
    if not res.get("ok"):
        raise NotDone("%r: %s" % (label, res.get("why", "the field didn't take it")))
    mode = res.get("mode")
    if mode == "insert":
        page.call("Input.insertText", 10.0, text=text)
        res = page.eval(_js(FILLED_JS, text))
    elif mode == "set":
        res = page.eval(_js(FILLED_JS, text))
    time.sleep(0.2)  # the field's own reaction (a list of suggestions)
    return {"found": True, "count": 1, "label": hit.get("label"), "mode": mode, "how": how,
            "value": res.get("value"), "screen": read(page)}


def type_text(page, text, idle_ms=800):
    """Type `text` into the page's focused field, replacing its content
    (as the screen reader's set_text does): the content selected, the
    text inserted the way an IME commits it, a wait for the page, a
    read. Raises NotSent when no field has the focus."""
    try:
        res = page.call_many([("Runtime.evaluate", {"expression": _js(SELECT_JS, text), "returnByValue": True}),
                              ("Input.insertText", {"text": text})])
    except Exception as e:
        raise NotSent(str(e)[:120])
    sel = (res[0].get("result") or {}).get("value") or {}
    if not sel.get("ok"):
        n = sel.get("fields", 0)
        raise NotSent("no field has the focus on the page" + (
            " (%d text fields in view; tap one, or type --field with its label)" % n if n else ""))
    if sel.get("direct") and str(sel.get("value", "")) != text:
        raise NotDone("the %s field didn't take %r (it holds %r)"
                      % (sel.get("type"), text, sel.get("value")))
    time.sleep(0.25)  # the field's own reaction (a list of suggestions)
    return {"screen": read(page), "ready": "complete", "direct": bool(sel.get("direct")),
            "only": bool(sel.get("only"))}


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
    time.sleep(min(0.15, idle_ms / 1000.0))  # a scroll loads nothing: the rows are in place
    return {"moved": moved, "screen": read(page), "ready": "complete"}
