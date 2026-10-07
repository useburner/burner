#!/usr/bin/env python3
"""Page taps end to end, without the phone: burner's own `cdp.tap` (the
Python and its page scripts) against a local headless Chrome emulating a
phone, each round trip delayed as the link to the phone delays it, on
pages whose layout moves after the tap's scroll (an image above the
target loading late, a slot growing, a target that never stops moving)
and on pages where a good touch must pass (a card's link laid over its
words, nested words, a slotted link, a wrapped link, a cookie banner, a
link to another page). Prints what each page saw clicked and how long
the tap took; exit 1 when a tap clicked anything but its target.

    python3 tools/pagetaps.py [TRIP_MS ...]       (default: 60 150 400 800)
    CASES=lazy,slot python3 tools/pagetaps.py 400

Chrome: $CHROME, else the usual place for this system."""
import http.server
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib", "u2"))

import cdp  # noqa: E402

CHROMES = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]


def chrome_path():
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    for c in CHROMES:
        if os.path.isabs(c) and os.path.exists(c):
            return c
        if not os.path.isabs(c) and shutil.which(c):
            return shutil.which(c)
    sys.exit("no Chrome found: set CHROME to its path")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def png(w, h):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x88" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


IMG = png(400, 220)
HEAD = ("<html><head><meta name='viewport' content='width=device-width'><style>body{margin:0;font:16px sans-serif}"
        "a{display:block;height:44px;line-height:44px;border-bottom:1px solid #ccc}img{display:block;width:100%}"
        "#slot{height:0;background:#fc0}</style></head><body>"
        "<script>window.__clicks=[];document.addEventListener('click',e=>{const a=e.target.closest('a');"
        "window.__clicks.push(a?a.textContent:e.target.tagName)});</script>")
STORIES = "".join("<a href='#r%d'>Story %d</a>" % (i, i) for i in range(150))
TAIL = "".join("<a href='#z%d'>Tail %d</a>" % (i, i) for i in range(40)) + "</body></html>"
MIDDLE = "".join("<a href='#m%d'>Middle %d</a>" % (i, i) for i in range(30))
PAGES = {
    "/lazy": HEAD + STORIES + "<a href='#a'>Story A</a><img loading=lazy src='/img.png?d=100' alt=''>"
             "<a href='#b'>Story B</a><a href='#c'>Story C</a><a id=t href='#more'>More</a>" + TAIL,
    "/slot": HEAD + STORIES + "<a href='#a'>Story A</a><div id=slot></div><a href='#b'>Story B</a>"
             "<a id=t href='#more'>More</a>" + TAIL +
             "<script>let grown=false;addEventListener('scroll',()=>{if(grown)return;grown=true;"
             "setTimeout(()=>{document.getElementById('slot').style.height='250px'},80)});</script>",
    "/plain": HEAD + STORIES + "<a href='#a'>Story A</a><a href='#b'>Story B</a><a id=t href='#more'>More</a>" + TAIL,
    "/carousel": HEAD + STORIES + "<div id=slot></div><a id=t href='#more'>More</a>" + TAIL +
                 "<script>let h=0;setInterval(()=>{h=(h+90)%360;document.getElementById('slot').style.height=h+'px'},150);"
                 "</script>",
    "/overlay": HEAD + STORIES + "<div style='position:relative;height:120px;border:1px solid #999'><h3>Cabin in the "
                "woods</h3><p>Lovely place</p><a href='#cabin' aria-label='Open listing' style='position:absolute;"
                "inset:0;border:0'></a></div>" + TAIL,
    "/nested": HEAD + STORIES + "<a href='#nest' style='height:auto'><span><b>More</b> stories</span></a>" + TAIL,
    "/slotted": HEAD + STORIES + "<my-card><a slot=x href='#slotted'>Read more</a></my-card>" + TAIL +
                "<script>customElements.define('my-card',class extends HTMLElement{constructor(){super();"
                "this.attachShadow({mode:'open'}).innerHTML='<div style=\"padding:20px;border:1px solid red\">"
                "<slot name=x></slot></div>'}});</script>",
    "/wrapped": HEAD + STORIES + "<p style='width:220px'>Some words before it <a href='#wrap' style='display:inline;"
                "height:auto;line-height:20px;border:0'>a long link that wraps across lines</a> and after.</p>" + TAIL,
    "/banner": HEAD + STORIES + "<a id=t href='#more'>More</a>" + TAIL +
               "<div style='position:fixed;left:0;right:0;bottom:0;height:900px;background:#eee'>Cookies? "
               "<button>OK</button></div>",
    "/away": HEAD + STORIES + "<a href='/plain?from=away'>More</a>" + TAIL,
    "/twice": HEAD + STORIES + "<a href='/plain?from=twice'><h2>Bishop jailed</h2></a>" + MIDDLE +
              "<a href='/plain?from=twice'><h2>Bishop jailed</h2></a>" + TAIL,
    "/twodests": HEAD + STORIES + "<a href='/plain?from=a'>Edit</a>" + MIDDLE +
                 "<a href='/plain?from=b'>Edit</a>" + TAIL,
    # an option whose box is hidden (a screen reader's only) inside its label
    "/option": HEAD + STORIES + "<style>.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}"
               "</style><ul><li role=option><label><input type=checkbox class=sr name=a> Option A</label></li>"
               "<li role=option><label><input type=checkbox class=sr name=b> Option B</label></li></ul>" + TAIL,
    # a link wrapped over two lines, its middle between them, in a card that takes a tap
    "/wrapgap": HEAD + STORIES + "<div role=button onclick='0' style='padding:10px;border:1px solid #999'>"
                "<p style='width:220px;margin:0;line-height:20px'>Words Words Words <a href='#wrap' "
                "style='display:inline;height:auto;line-height:20px;border:0'>a link that wraps</a> and after it more "
                "words.</p></div>" + TAIL,
    # an ad's frame slides over the target just after the tap's scroll
    "/ad": "<html><body style='margin:0;background:#fd0'>"
           "<script>addEventListener('click',()=>{document.body.textContent='AD CLICKED'})</script>an ad</body></html>",
    "/adframe": HEAD + STORIES + "<a href='#a'>Story A</a><a id=t href='#more'>More</a>" + TAIL +
                "<iframe id=ad src='/ad' style='position:absolute;display:none;width:100%;height:60px;border:0;left:0'>"
                "</iframe><script>let shown=false;addEventListener('scroll',()=>{if(shown)return;shown=true;"
                "setTimeout(()=>{const t=document.getElementById('t').getBoundingClientRect();"
                "const f=document.getElementById('ad');f.style.top=(scrollY+t.top-8)+'px';f.style.display='block'},"
                "150)});</script>",
    # a lazy image hidden at phone width (never loads) above the target
    "/hiddenlazy": HEAD + STORIES + "<img loading=lazy src='/img.png?d=5000' style='display:none' alt=''>"
                   "<a href='#a'>Story A</a><a id=t href='#more'>More</a>" + TAIL,
}
# (page, the words tapped, what the page must see: the link's text, the
# address it goes to, "ambiguous": nothing touched, the rows counted, or
# "frame": nothing on the page, the touch said to have gone to a frame and
# the next touch not held; a target that never stops moving may be left
# untapped, never tapped wrong)
CASES = [("plain", "More", "More"), ("lazy", "More", "More"), ("slot", "More", "More"),
         ("carousel", "More", "More"), ("overlay", "Cabin in the woods", ""), ("nested", "More stories", "More stories"),
         ("slotted", "Read more", "Read more"), ("wrapped", "a long link that wraps across lines",
                                                  "a long link that wraps across lines"),
         ("banner", "More", "More"), ("away", "More", "/plain?from=away"),
         ("twice", "Bishop jailed", "/plain?from=twice"), ("twodests", "Edit", "ambiguous"),
         ("option", "Option B", "checked:b"), ("wrapgap", "a link that wraps", "a link that wraps"),
         ("adframe", "More", "frame"), ("hiddenlazy", "More", "More")]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionError:
            pass  # Chrome dropped a request on its way to another page

    def do_GET(self):
        path, _, q = self.path.partition("?")
        if path == "/img.png":
            delay = dict(p.split("=") for p in q.split("&") if "=" in p).get("d", "0")
            time.sleep(int(delay) / 1000.0)
            body, ctype = IMG, "image/png"
        elif path in PAGES:
            body, ctype = PAGES[path].encode(), "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def connect(dt_port, tid):
    """A cdp.Page on a local Chrome's tab, emulating a phone."""
    page = cdp.Page.__new__(cdp.Page)
    page.target, page.handshake_again = tid, False
    page.ws = cdp.WebSocket(socket.create_connection(("127.0.0.1", dt_port)), "/devtools/page/" + tid)
    page.n, page.lock = 0, threading.RLock()
    page.visible_at = time.monotonic() + 1e6  # always on screen: no short bounds
    page.loading, page.window_opened, page.main_frame, page.url = False, None, None, ""
    page.ws.handshake(5)
    res = page.call_many([("Page.enable", {}), ("Page.getFrameTree", {})])
    page.main_frame = res[1]["frameTree"]["frame"]["id"]
    page.call_many([("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 800, "deviceScaleFactor": 2.625,
                                                             "mobile": True}),
                    ("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})])
    return page


def delayed(page, trip_ms):
    """Each round trip `trip_ms` long: half before the commands go, half
    after their answers come."""
    real = cdp.Page.call_many

    def call_many(cmds, timeout=10.0, raise_errors=True):
        time.sleep(trip_ms / 2000.0)
        try:
            return real(page, cmds, timeout, raise_errors)
        finally:
            time.sleep(trip_ms / 2000.0)
    page.call_many = call_many


def main():
    trips = [int(a) for a in sys.argv[1:]] or [60, 150, 400, 800]
    only = [c for c in os.environ.get("CASES", "").split(",") if c]
    web_port, dt_port = free_port(), free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", web_port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prof = tempfile.mkdtemp(prefix="burner-pagetaps-")
    chrome = subprocess.Popen([chrome_path(), "--headless=new", "--remote-debugging-port=%d" % dt_port,
                               "--user-data-dir=" + prof, "--no-first-run", "--no-default-browser-check",
                               "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wrong = 0
    try:
        tid = None
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json/list" % dt_port, timeout=1))
                tid = next(t["id"] for t in tabs if t.get("type") == "page")
                break
            except Exception:
                time.sleep(0.2)
        if tid is None:
            sys.exit("Chrome's DevTools didn't answer")
        page = connect(dt_port, tid)
        for case, label, want in CASES:
            if only and case not in only:
                continue
            for trip in trips:
                page.call_many = lambda cmds, timeout=10.0, raise_errors=True: cdp.Page.call_many(
                    page, cmds, timeout, raise_errors)
                page.call("Page.navigate", url="http://127.0.0.1:%d/%s?n=%d" % (web_port, case, trip))
                page.loading = True
                page.wait_parsed(5)
                time.sleep(0.3)
                delayed(page, trip)
                t0, r, err = time.monotonic(), {}, ""
                try:
                    r = cdp.tap(page, label)
                except cdp.Held as e:
                    err = str(e)
                held = r.get("held", 0)
                took = time.monotonic() - t0
                page.call_many = lambda cmds, timeout=10.0, raise_errors=True: cdp.Page.call_many(
                    page, cmds, timeout, raise_errors)
                time.sleep(0.3)
                seen = page.eval("[location.pathname + location.search].concat(window.__clicks || [])")
                address, clicks = seen[0], seen[1:]
                if want == "ambiguous":
                    right = r.get("count", 1) > 1 and not clicks and address.startswith("/" + case)
                elif want.startswith("checked:"):
                    right = page.eval("[...document.querySelectorAll('input[type=checkbox]')].filter(i => i.checked)"
                                      ".map(i => i.name).join(',')") == want[8:]
                elif want == "frame":
                    # and a touch after it, on what is on screen, isn't held
                    pt = page.eval("(()=>{const a=[...document.querySelectorAll('a')].find(a=>a.textContent==='Tail 2');"
                                   "const b=a.getBoundingClientRect();return [b.left+b.width/2, b.top+b.height/2]})()")
                    cdp.touch(page, pt[0], pt[1])
                    time.sleep(0.4)
                    after = page.eval("window.__clicks.slice()")
                    right = bool(r.get("unjudged")) and not clicks and after == ["Tail 2"]
                elif want.startswith("/"):
                    right = address == want and not clicks
                else:
                    right = clicks == [want] or bool(not clicks and err and case == "carousel")
                wrong += not right
                print("%-9s %4dms trip: %-6s %.2fs, %s%s%s" % (
                    case, trip, "ok" if right else "WRONG", took,
                    "%d rows, nothing touched" % r.get("count", 1) if want == "ambiguous" and right else
                    "the touch went to the frame; the next one went through" if want == "frame" and right else
                    address if want.startswith("/") else "clicked %s" % json.dumps(clicks),
                    ", %d held" % held if held else "", "; " + err if err else ""))
    finally:
        chrome.terminate()
        try:
            chrome.wait(5)
        except Exception:
            chrome.kill()
        srv.shutdown()
        shutil.rmtree(prof, ignore_errors=True)
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
