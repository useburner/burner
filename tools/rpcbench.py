#!/usr/bin/env python3
"""Where a phone round trip's time goes, from the computer burner runs on.

    python3 tools/rpcbench.py

Times, three runs each: one adb call, one small UI-server RPC
(deviceInfo), one screen read (dumpWindowHierarchy, gzip), the same wait
plus read sent as ONE JSON-RPC batch (if the phone's UI server takes
batches, an action can settle and read in one round trip instead of
two), and the UI helper's own socket ("screen", "dump fresh"). Read-only.
Runs itself under the repo's .venv so uiautomator2 is there.
"""
import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(ROOT, ".venv", "bin", "python")
# The venv's python is a symlink to the system one, so compare the venv
# itself (sys.prefix), not the interpreter's real path.
if os.path.exists(VENV) and os.path.realpath(sys.prefix) != os.path.realpath(os.path.dirname(os.path.dirname(VENV))):
    os.execv(VENV, [VENV, os.path.abspath(__file__)] + sys.argv[1:])

import gzip  # noqa: E402
import io  # noqa: E402


def local_port():
    try:
        with open(os.path.join(ROOT, "config.env")) as f:
            for line in f:
                if line.strip().startswith("LOCAL_PORT="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "15555"


TARGET = "127.0.0.1:" + local_port()
ADB = os.path.join(ROOT, ".android-tools", "platform-tools", "adb")
SOCK = os.path.join(ROOT, "run", "u2-mux.sock")


def timed(label, fn, runs=3):
    out = []
    for _ in range(runs):
        t0 = time.time()
        try:
            extra = fn()
        except Exception as e:  # keep going: one failing probe mustn't hide the rest
            print("{:<34} failed: {}".format(label, str(e)[:80]))
            return
        out.append((time.time() - t0, extra))
    print("{:<34} {}".format(label, "  ".join(
        "{:4.0f}ms{}".format(t * 1000, " " + str(x) if x else "") for t, x in out)))


def adb_true():
    subprocess.run([ADB, "-s", TARGET, "shell", "true"], capture_output=True, timeout=30)


def helper(cmd):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(40)
    s.connect(SOCK)
    s.sendall((cmd + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(64)
        if not chunk:
            break
        buf += chunk
    head, rest = buf.split(b"\n", 1)
    n = int(head.split()[1])
    while len(rest) < n:
        chunk = s.recv(min(65536, n - len(rest)))
        if not chunk:
            break
        rest += chunk
    s.close()
    return "{}B".format(len(rest))


def main():
    print("target {}".format(TARGET))
    timed("adb shell true", adb_true)
    try:
        import adbutils
        from uiautomator2.core import AdbHTTPConnection
    except ImportError as e:
        print("uiautomator2 not importable ({}); run bash install.sh".format(e))
        return 1
    dev = adbutils.device(TARGET)
    conn = {"c": None}

    def post(body):
        if conn["c"] is None:
            conn["c"] = AdbHTTPConnection(dev, port=9008)
            conn["c"].connect()
        c = conn["c"]
        c.request("POST", "/jsonrpc/0", body, headers={
            "User-Agent": "uiautomator2", "Accept-Encoding": "gzip",
            "Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        wire = len(data)
        if (r.getheader("Content-Encoding") or "").lower() == "gzip":
            data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
        if r.will_close:
            c.close()
            conn["c"] = None
        return wire, data

    def rpc(method, params):
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

    def device_info():
        post(rpc("deviceInfo", []))

    def dump():
        wire, data = post(rpc("dumpWindowHierarchy", [False, 50]))
        return "{}B wire".format(wire)

    def batch():
        body = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "waitForIdle", "params": [500]},
            {"jsonrpc": "2.0", "id": 2, "method": "dumpWindowHierarchy", "params": [False, 50]},
        ])
        wire, data = post(body)
        try:
            parsed = json.loads(data)
        except ValueError:
            return "not JSON: {!r}".format(data[:60])
        if isinstance(parsed, list) and len(parsed) == 2 and "result" in parsed[1]:
            return "batch OK, {}B wire".format(wire)
        return "no batch: {}".format(str(parsed)[:80])

    def idle():
        post(rpc("waitForIdle", [500]))

    def act_batch():
        # The shape an action uses (minus the action): a read as the
        # on-device pause, the wait for the UI to go quiet, the read that
        # comes back. (waitForWindowUpdate is never batched: inside a
        # batch it crashed the phone's server on Oct 4.)
        body = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "dumpWindowHierarchy", "params": [False, 50]},
            {"jsonrpc": "2.0", "id": 2, "method": "waitForIdle", "params": [500]},
            {"jsonrpc": "2.0", "id": 3, "method": "dumpWindowHierarchy", "params": [False, 50]},
        ])
        wire, data = post(body)
        parsed = json.loads(data)
        ok = isinstance(parsed, list) and len(parsed) == 3 and "result" in parsed[2]
        return "batch OK, {}B wire".format(wire) if ok else "no: {}".format(str(parsed)[:80])

    timed("deviceInfo RPC", device_info)
    timed("waitForIdle(500) RPC", idle)
    timed("dumpWindowHierarchy RPC", dump)
    timed("batch [waitForIdle, dump]", batch)
    timed("batch [dump, idle, dump]", act_batch)
    if os.path.exists(SOCK):
        timed("helper: screen", lambda: helper("screen"))
        timed("helper: dump fresh", lambda: helper("dump fresh"))
    else:
        print("helper socket not found ({}); run a burner command first".format(SOCK))
    return 0


if __name__ == "__main__":
    sys.exit(main())
