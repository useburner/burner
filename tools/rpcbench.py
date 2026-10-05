#!/usr/bin/env python3
"""Time the phone's screen-reader calls one by one, to see where an
action's time goes (a native tap pays for reads before and after it).

    .venv/bin/python3 tools/rpcbench.py            # reads and idle waits
    .venv/bin/python3 tools/rpcbench.py 1          # plus a tap on the key "1"

Run from the burner folder with the app to measure in front. With a
label, the row with those words is tapped a few times (a calculator
key is harmless). Prints one line per call: what, how long, and the
size of the reply."""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib", "u2"))

import u2mux  # noqa: E402


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else None
    import uiautomator2 as u2
    u2mux.install_keepalive()
    d = u2.connect(u2mux.TARGET)
    u2mux.apply_fast_config(d)

    def batch(calls):
        """One HTTP round trip with these calls, as the helper's act sends
        them: (ms, results)."""
        body = [{"jsonrpc": "2.0", "id": i + 1, "method": m, "params": p}
                for i, (m, p) in enumerate(calls)]
        t0 = time.monotonic()
        resp = u2mux._KEEPALIVE.request(d._dev, d._device_server_port, "POST", "/jsonrpc/0",
                                        data=body, timeout=45.0)
        ms = (time.monotonic() - t0) * 1000
        return ms, u2mux.batch_results(resp.json(), len(calls))

    def show(what, calls):
        ms, results = batch(calls)
        last = results[-1]
        extra = ""
        if isinstance(last, str) and "<hierarchy" in last:
            extra = "%d nodes, %dKB" % (last.count("<node"), len(last) // 1024)
        elif isinstance(last, Exception):
            extra = "error: %s" % str(last)[:60]
        print("%-44s %6.0fms  %s" % (what, ms, extra), flush=True)
        return results

    dump = ("dumpWindowHierarchy", [False, u2mux.DUMP_DEPTH])
    show("deviceInfo", [("deviceInfo", [])])
    show("deviceInfo (again)", [("deviceInfo", [])])
    xml = show("dump (depth 50)", [dump])[-1]
    show("dump (depth 50, again)", [dump])
    show("dump (compressed, depth 50)", [("dumpWindowHierarchy", [True, u2mux.DUMP_DEPTH])])
    show("dump (depth 12)", [("dumpWindowHierarchy", [False, 12])])
    show("waitForIdle 1200", [("waitForIdle", [1200])])
    show("waitForIdle 1200 (again)", [("waitForIdle", [1200])])
    show("waitForIdle 300", [("waitForIdle", [300])])
    show("wakeUp", [("wakeUp", [])])
    if not label:
        return
    x, y = u2mux.label_target(xml, label)
    click = ("click", [int(x), int(y)])
    print("tapping %r at (%d, %d)" % (label, x, y))
    show("click alone", [click])
    show("  waitForIdle 1200 right after", [("waitForIdle", [1200])])
    show("  dump right after", [dump])
    time.sleep(1.0)
    show("act batch as sent: wake, click, dump, idle, dump",
         [("wakeUp", []), click, dump, ("waitForIdle", [1200]), dump])
    time.sleep(1.0)
    show("wake, click, idle 1200, dump (no pause dump)",
         [("wakeUp", []), click, ("waitForIdle", [1200]), dump])
    time.sleep(1.0)
    show("wake, click, idle 400, dump",
         [("wakeUp", []), click, ("waitForIdle", [400]), dump])
    time.sleep(1.0)
    show("wake, click (quiet step)", [("wakeUp", []), click])
    show("  dump right after", [dump])


if __name__ == "__main__":
    main()
