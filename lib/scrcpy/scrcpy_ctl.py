#!/usr/bin/env python3
"""Minimal scrcpy v4.1 control-channel client (control-only, no video).

Speaks the binary control protocol over the adb forward
tcp:27183 -> localabstract:scrcpy (created by scrcpyd.sh).

Message layouts verified against scrcpy v4.1 server source
(ControlMessage.java / ControlMessageReader.java / DesktopConnection.java).
"""
import socket
import struct
import sys
import time

HOST = "127.0.0.1"
PORT = 27183

# --- message types (scrcpy v4.1) ---
T_INJECT_KEYCODE = 0
T_INJECT_TEXT = 1
T_INJECT_TOUCH_EVENT = 2
T_INJECT_SCROLL_EVENT = 3
T_BACK_OR_SCREEN_ON = 4
T_EXPAND_NOTIFICATION_PANEL = 5
T_COLLAPSE_PANELS = 7
T_SET_CLIPBOARD = 9
T_START_APP = 16

# --- actions ---
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

# --- keycodes (android.view.KeyEvent) ---
KEYCODE_HOME = 3
KEYCODE_BACK = 4
KEYCODE_VOLUME_UP = 24
KEYCODE_VOLUME_DOWN = 25
KEYCODE_POWER = 26
KEYCODE_ENTER = 66
KEYCODE_DEL = 67
KEYCODE_TAB = 61
KEYCODE_DPAD_UP = 19
KEYCODE_DPAD_DOWN = 20
KEYCODE_DPAD_LEFT = 21
KEYCODE_DPAD_RIGHT = 22
KEYCODE_WAKEUP = 224
KEYCODE_APP_SWITCH = 187

PRESSURE_FULL = 0xFFFF  # u16 fixed point -> exactly 1.0f server-side
BUTTON_PRIMARY = 1
INJECT_TEXT_MAX = 300


class ScrcpyControl:
    """A persistent control-channel connection. Create once, reuse."""

    def __init__(self, host=HOST, port=PORT, screen=(1080, 2400), timeout=10):
        self.screen = screen
        self.sock = socket.create_connection((host, port), timeout=timeout)
        # send_dummy_byte=true: server writes a single 0x00 on accept
        dummy = self._recv_exact(1)
        if dummy != b"\x00":
            self.close()
            raise RuntimeError(f"scrcpy handshake failed, got {dummy!r}")

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("scrcpy control socket closed")
            buf += chunk
        return buf

    def _send(self, data: bytes):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    # ---- input primitives ----
    def keyevent(self, keycode, action=ACTION_DOWN):
        """Full key press = down + up."""
        if action == ACTION_DOWN:
            self._send(struct.pack(">BBiii", T_INJECT_KEYCODE, ACTION_DOWN, keycode, 0, 0))
            self._send(struct.pack(">BBiii", T_INJECT_KEYCODE, ACTION_UP, keycode, 0, 0))
        else:
            self._send(struct.pack(">BBiii", T_INJECT_KEYCODE, action, keycode, 0, 0))

    def _touch(self, action, x, y, pointer_id=1, buttons=0, screen=None):
        w, h = screen or self.screen
        self._send(struct.pack(
            ">BBqiiHHHii",
            T_INJECT_TOUCH_EVENT, action, pointer_id,
            int(x), int(y), w, h, PRESSURE_FULL, 0, buttons,
        ))

    def tap(self, x, y, hold_ms=70, screen=None):
        self._touch(ACTION_DOWN, x, y, buttons=BUTTON_PRIMARY, screen=screen)
        if hold_ms:
            time.sleep(hold_ms / 1000)
        self._touch(ACTION_UP, x, y, buttons=0, screen=screen)

    def swipe(self, x1, y1, x2, y2, duration_ms=300, steps=12, screen=None):
        self._touch(ACTION_DOWN, x1, y1, buttons=BUTTON_PRIMARY, screen=screen)
        for i in range(1, steps + 1):
            t = i / steps
            x = x1 + (x2 - x1) * t
            y = y1 + (y2 - y1) * t
            self._touch(ACTION_MOVE, x, y, buttons=BUTTON_PRIMARY, screen=screen)
            time.sleep(duration_ms / 1000 / steps)
        # Hold still before lifting: the finger's speed at release is ~0, so
        # the list moves exactly the swipe's length and stops, instead of
        # flinging on for a second (the next screen read would catch it
        # mid-motion, and a fling can skip past content).
        time.sleep(0.1)
        self._touch(ACTION_MOVE, x2, y2, buttons=BUTTON_PRIMARY, screen=screen)
        time.sleep(0.05)
        self._touch(ACTION_UP, x2, y2, buttons=0, screen=screen)

    def drag(self, x, y, hold_ms, legs, screen=None):
        """Press at (x, y), hold `hold_ms` (a long-press: a launcher picks
        the icon up), then carry it along `legs`, each (x, y, move_ms,
        dwell_ms): a glide to the point in ~16ms steps, then a stay there
        (a home screen turns the page while the finger stays at its edge,
        and opens a folder or makes room under it). Lifts at the last
        point; lifts even when a send fails part way, so the phone is never
        left with a finger down."""
        down = False
        try:
            self._touch(ACTION_DOWN, x, y, buttons=BUTTON_PRIMARY, screen=screen)
            down = True
            time.sleep(hold_ms / 1000)
            for x2, y2, move_ms, dwell_ms in legs:
                steps = max(1, int(move_ms) // 16)
                for i in range(1, steps + 1):
                    t = i / steps
                    self._touch(ACTION_MOVE, x + (x2 - x) * t, y + (y2 - y) * t,
                                buttons=BUTTON_PRIMARY, screen=screen)
                    time.sleep(move_ms / 1000 / steps)
                x, y = x2, y2
                time.sleep(dwell_ms / 1000)
            self._touch(ACTION_UP, x, y, buttons=0, screen=screen)
            down = False
        finally:
            if down:
                try:
                    self._touch(ACTION_UP, x, y, buttons=0, screen=screen)
                except OSError:
                    pass

    def text(self, s):
        """Inject text as key events (ASCII-safe path; prefer set_text for fields)."""
        data = s.encode("utf-8")
        for i in range(0, len(data), INJECT_TEXT_MAX):
            chunk = data[i:i + INJECT_TEXT_MAX]
            self._send(struct.pack(">Bi", T_INJECT_TEXT, len(chunk)) + chunk)

    def scroll(self, x, y, h_amount=0.0, v_amount=1.0):
        """Scroll at (x,y); v_amount in 'clicks' (1.0 = one notch down)."""
        w, h = self.screen
        hs = int((h_amount / 16) * 32768)
        vs = int((v_amount / 16) * 32768)
        self._send(struct.pack(">BiiHHhhi", T_INJECT_SCROLL_EVENT,
                               int(x), int(y), w, h, hs, vs, 0))

    def back_or_screen_on(self):
        self._send(struct.pack(">BB", T_BACK_OR_SCREEN_ON, ACTION_DOWN))

    def back(self):
        self.keyevent(KEYCODE_BACK)

    def home(self):
        self.keyevent(KEYCODE_HOME)

    def wake(self):
        self.keyevent(KEYCODE_WAKEUP)

    def expand_notifications(self):
        """Pull the notification shade down (the server asks the status
        bar), as `cmd statusbar expand-notifications` does over adb."""
        self._send(struct.pack(">B", T_EXPAND_NOTIFICATION_PANEL))

    def collapse_panels(self):
        """Close the shade and the quick settings."""
        self._send(struct.pack(">B", T_COLLAPSE_PANELS))

    def set_clipboard(self, text, paste=False):
        data = text.encode("utf-8")
        self._send(struct.pack(">BqBi", T_SET_CLIPBOARD, 0, 1 if paste else 0, len(data)) + data)

    def start_app(self, package_name):
        name = package_name.encode("utf-8")
        if len(name) > 255:
            raise ValueError("package name too long")
        self._send(struct.pack(">BB", T_START_APP, len(name)) + name)

    def ping(self):
        """Liveness check that sends nothing to the phone.

        It used to send BACK_OR_SCREEN_ON with ACTION_UP, but the scrcpy
        server passes that straight to Android as a BACK key-up, which
        pressed Back on the phone every time a command healed the helpers
        (it ejected apps before a scroll or tap). Now: look at the socket
        without reading it. A closed socket peeks as empty bytes; an idle
        one has nothing to read.
        """
        try:
            self.sock.setblocking(False)
            try:
                return self.sock.recv(1, socket.MSG_PEEK) != b""
            except BlockingIOError:
                return True  # open, nothing waiting
        except OSError:
            return False
        finally:
            try:
                self.sock.settimeout(None)
            except OSError:
                pass


def _mux_send(line, timeout=30, resend=True):
    """Send a command to the mux; start the mux daemon if not running.
    Without `resend`, a reply lost after the line went out raises instead
    of sending it again (a drag may be half done)."""
    import os as _os
    # The burner tree this file lives in (lib/scrcpy/scrcpy_ctl.py).
    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.realpath(__file__))))
    run = _os.path.join(root, "run")
    sock_path = _os.path.join(run, "scrcpy-mux.sock")

    def _try():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall((line + "\n").encode())
        data = b""
        try:
            while not data.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError as e:
            if not resend:
                raise RuntimeError(f"the mux's reply was lost part way: {e}")
            raise
        finally:
            s.close()
        if not resend and not data.endswith(b"\n"):
            raise RuntimeError("the mux closed before it replied, part way")
        return data.decode("utf-8", "replace").strip()

    try:
        return _try()
    except (OSError, ConnectionRefusedError, FileNotFoundError):
        pass
    # start the mux daemon
    import subprocess as _sp
    _os.makedirs(run, exist_ok=True)
    mux_py = _os.path.join(root, "lib", "scrcpy", "mux.py")
    _sp.Popen([sys.executable, mux_py],
              stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
              start_new_session=True)
    last = None
    for _ in range(60):
        time.sleep(0.5)
        try:
            return _try()
        except (OSError, ConnectionRefusedError, FileNotFoundError) as e:
            last = e
    raise RuntimeError(f"scrcpy mux did not come up: {last}")


def main(argv):
    if len(argv) < 2:
        print("usage: scrcpy_ctl.py tap X Y | swipe X1 Y1 X2 Y2 [MS] | drag HOLD X Y (X Y MOVE DWELL)... | key CODE | back | home | wake | text STR | scroll X Y [H V] | startapp PKG | ping")
        return 2
    cmd = argv[1]
    if cmd == "tap":
        line = f"tap {argv[2]} {argv[3]}"
    elif cmd == "swipe":
        ms = argv[6] if len(argv) > 6 else "300"
        line = f"swipe {argv[2]} {argv[3]} {argv[4]} {argv[5]} {ms}"
    elif cmd == "drag":
        line = "drag " + " ".join(argv[2:])
    elif cmd == "key":
        line = f"key {argv[2]}"
    elif cmd == "scroll":
        h = argv[4] if len(argv) > 4 else "0"
        v = argv[5] if len(argv) > 5 else "1"
        line = f"scroll {argv[2]} {argv[3]} {h} {v}"
    elif cmd in ("back", "home", "wake", "ping"):
        line = cmd
    elif cmd == "text":
        line = "text " + " ".join(argv[2:])
    elif cmd == "startapp":
        line = f"startapp {argv[2]}"
    elif cmd in ("shade", "collapse"):
        line = cmd
    else:
        print(f"unknown command {cmd}")
        return 2
    reply = _mux_send(line)
    print(reply)
    return 0 if reply == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
