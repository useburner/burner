#!/usr/bin/env python3
"""scrcpy control-channel multiplexer.

Holds ONE persistent scrcpy control connection (the server handles a single
client per launch, then exits) and serves local commands over a unix socket,
so every `burner` invocation gets ~5ms input without paying server startup.

Unix socket: <burner folder>/run/scrcpy-mux.sock
Line protocol:  "tap 540 1200" | "swipe x1 y1 x2 y2 ms" | "key 4" |
                "back" | "home" | "wake" | "text hello" | "scroll x y h v" |
                "startapp com.pkg" | "ping"
Reply: "ok" or "err <message>" (one line).

Auto-recovers: if the control socket dies, kills any stale server on the
phone, relaunches scrcpyd.sh, reconnects, and retries the command once.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

HOME = os.path.expanduser("~")
# The burner tree this file lives in (lib/scrcpy/mux.py).
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
RUN = os.path.join(BASE, "run")
SOCK_PATH = os.path.join(RUN, "scrcpy-mux.sock")
PID_PATH = os.path.join(RUN, "scrcpy-mux.pid")
ADB = next((p for p in (
    os.path.join(d, ".android-tools", "platform-tools", "adb")
    for d in (os.environ.get("BURNER_WORKSPACE"), BASE, os.path.dirname(BASE)) if d)
    if os.path.exists(p)), "adb")
ANDROID_SERIAL = os.environ.get("ANDROID_SERIAL", "127.0.0.1:15555")
SCRCPY_PORT = int(os.environ.get("SCRCPY_PORT", "27183"))
SERVER_APK_JAR = "/data/local/tmp/scrcpy-server.jar"
SCRCPYD = os.path.join(BASE, "lib", "scrcpy", "scrcpyd.sh")

KEY_NAMES = {
    "back": 4, "home": 3, "wakeup": 224, "wake": 224, "sleep": 223,
    "app_switch": 187, "recent": 187, "menu": 82, "search": 84,
    "del": 67, "delete": 67, "enter": 66, "tab": 61, "space": 62,
    "escape": 111, "dpad_up": 19, "dpad_down": 20, "dpad_left": 21,
    "dpad_right": 22, "dpad_center": 23, "volume_up": 24, "volume_down": 25,
    "power": 26, "camera": 27,
}


def _keycode(arg):
    a = arg.strip().lower()
    if a in KEY_NAMES:
        return KEY_NAMES[a]
    return int(a)

sys.path.insert(0, os.path.join(BASE, "lib", "scrcpy"))
from scrcpy_ctl import ScrcpyControl  # noqa: E402


def adb(*args, timeout=20):
    env = dict(os.environ, ANDROID_SERIAL=ANDROID_SERIAL)
    return subprocess.run([ADB, *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def screen_size():
    try:
        out = adb("shell", "wm", "size").stdout
        m = re.search(r"(\d+)x(\d+)", out)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass
    return (1080, 2400)


class Mux:
    def __init__(self):
        self.ctl = None
        self.server_proc = None
        self.lock = threading.Lock()
        self.screen = screen_size()

    def _kill_stale_server(self):
        try:
            adb("shell", "pkill -f [c]om.genymobile.scrcpy.Server", timeout=10)
        except Exception:
            pass
        time.sleep(0.5)

    def _ensure_forward(self):
        try:
            fwd = adb("forward", "--list", timeout=10).stdout
            if f"tcp:{SCRCPY_PORT}" not in fwd:
                adb("forward", f"tcp:{SCRCPY_PORT}", "localabstract:scrcpy", timeout=10)
        except Exception:
            pass

    def _launch_server(self):
        self._kill_stale_server()
        self._ensure_forward()
        env = dict(os.environ, ANDROID_SERIAL=ANDROID_SERIAL,
                   SCRCPY_PORT=str(SCRCPY_PORT), ADB=ADB)
        self.server_proc = subprocess.Popen(
            ["/bin/bash", SCRCPYD], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _connect(self):
        last = None
        for _ in range(40):  # ~20s for app_process boot
            try:
                self.ctl = ScrcpyControl(port=SCRCPY_PORT, screen=self.screen,
                                         timeout=5)
                return
            except Exception as e:
                last = e
                time.sleep(0.5)
        raise RuntimeError(f"scrcpy connect failed: {last}")

    def ensure(self):
        """Make sure we have a live control connection; relaunch if not."""
        if self.ctl is not None:
            try:
                if self.ctl.ping():
                    return
            except Exception:
                pass
            try:
                self.ctl.close()
            except Exception:
                pass
            self.ctl = None
        if self.server_proc is not None and self.server_proc.poll() is None:
            try:
                self.server_proc.kill()
            except Exception:
                pass
        self._launch_server()
        self._connect()

    def run_command(self, line):
        parts = line.strip().split(" ", 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        c = self.ctl
        if cmd == "tap":
            x, y, *rest = arg.split()
            w, h = (int(rest[0]), int(rest[1])) if len(rest) >= 2 else (None, None)
            c.tap(int(x), int(y), screen=(w, h) if w else None)
        elif cmd == "swipe":
            x1, y1, x2, y2, *rest = arg.split()
            ms = int(rest[0]) if rest else 300
            w = h = None
            if len(rest) >= 3:
                w, h = int(rest[1]), int(rest[2])
            c.swipe(int(x1), int(y1), int(x2), int(y2), ms, screen=(w, h) if w else None)
        elif cmd == "key":
            c.keyevent(_keycode(arg))
        elif cmd == "back":
            c.back()
        elif cmd == "home":
            c.home()
        elif cmd == "wake":
            c.wake()
        elif cmd == "text":
            c.text(arg)
        elif cmd == "scroll":
            x, y, *rest = arg.split()
            h = float(rest[0]) if len(rest) > 0 else 0.0
            v = float(rest[1]) if len(rest) > 1 else 1.0
            c.scroll(int(x), int(y), h, v)
        elif cmd == "startapp":
            c.start_app(arg.strip())
        elif cmd == "shade":
            c.expand_notifications()
        elif cmd == "collapse":
            c.collapse_panels()
        elif cmd == "ping":
            if not c.ping():
                raise RuntimeError("ping failed")
        else:
            raise ValueError(f"unknown command {cmd!r}")
        return "ok"

    def handle(self, line):
        with self.lock:
            try:
                self.ensure()
                return self.run_command(line)
            except Exception as e:
                # one recovery attempt
                try:
                    self.ctl = None
                    self.ensure()
                    return self.run_command(line)
                except Exception as e2:
                    return f"err {e2}"


def serve():
    os.makedirs(RUN, exist_ok=True)
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))
    mux = Mux()
    # connect eagerly so failures surface at startup
    mux.ensure()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    srv.listen(16)
    while True:
        conn, _ = srv.accept()
        try:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            line = data.decode("utf-8", "replace").strip()
            reply = mux.handle(line) if line else "err empty"
            conn.sendall((reply + "\n").encode())
        except Exception as e:
            try:
                conn.sendall(f"err {e}\n".encode())
            except OSError:
                pass
        finally:
            conn.close()


if __name__ == "__main__":
    serve()
