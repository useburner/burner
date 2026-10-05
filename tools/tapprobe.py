#!/usr/bin/env python3
"""Where does a page tap land? For a label on the page in Chrome (the
phone's current tab), this listens for clicks on the whole document,
runs `burner tap LABEL`, and prints what the page saw: the element's
box and the viewport before, the click(s) received (target and point),
the url after, and the element under the point burner aimed at.

    .venv/bin/python3 tools/tapprobe.py Submit

Run from the burner folder, with the page already open."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib", "u2"))

import cdp  # noqa: E402
import adbutils  # noqa: E402

BEFORE = r"""
(function(label){
  const want = label.trim().toLowerCase();
  const all = Array.from(document.querySelectorAll('button,a,input,[role=button]'));
  const el = all.find(e => (e.innerText || e.value || '').trim().toLowerCase() === want) || null;
  window.__hits = [];
  document.addEventListener('click', e => window.__hits.push({target: e.target.tagName + (e.target.id ? '#' + e.target.id : ''), x: e.clientX, y: e.clientY}), true);
  document.addEventListener('touchstart', e => { const t = e.touches[0]; window.__hits.push({touch: e.target.tagName, x: t && t.clientX, y: t && t.clientY}); }, true);
  const r = el ? el.getBoundingClientRect() : null;
  const vv = window.visualViewport;
  return {found: !!el, rect: r ? [r.left, r.top, r.width, r.height] : null,
          vv: vv ? [vv.offsetLeft, vv.offsetTop, vv.width, vv.height, vv.scale] : null,
          inner: [innerWidth, innerHeight], scrollY: scrollY,
          active: document.activeElement ? document.activeElement.tagName : null, url: location.href};
})"""

AFTER = r"""
(function(label){
  const want = label.trim().toLowerCase();
  const all = Array.from(document.querySelectorAll('button,a,input,[role=button]'));
  const el = all.find(e => (e.innerText || e.value || '').trim().toLowerCase() === want) || null;
  const r = el ? el.getBoundingClientRect() : null;
  const vv = window.visualViewport;
  const cx = r ? r.left + r.width / 2 : 0, cy = r ? r.top + r.height / 2 : 0;
  const under = r ? document.elementFromPoint(cx, cy) : null;
  return {hits: window.__hits || [], url: location.href, rect: r ? [r.left, r.top, r.width, r.height] : null,
          vv: vv ? [vv.offsetLeft, vv.offsetTop, vv.width, vv.height, vv.scale] : null, scrollY: scrollY,
          under: under ? under.tagName + (under.id ? '#' + under.id : '') + ' ' + (under.innerText || '').slice(0, 30) : null,
          active: document.activeElement ? document.activeElement.tagName : null};
})"""


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "Submit"
    dev = adbutils.adb.device(os.environ.get("BURNER_SERIAL", "127.0.0.1:15555"))
    page = cdp.front_page(dev)
    print("before", json.dumps(page.eval("(%s)(%s)" % (BEFORE, json.dumps(label)))))
    out = subprocess.run([os.path.join(ROOT, "bin", "burner"), "tap", label],
                         capture_output=True, text=True, timeout=60)
    print("tap rc", out.returncode, (out.stdout.strip().splitlines() or [""])[0][:120],
          out.stderr.strip()[:120])
    page = cdp.front_page(dev, page)
    print("after", json.dumps(page.eval("(%s)(%s)" % (AFTER, json.dumps(label)))))


if __name__ == "__main__":
    main()
