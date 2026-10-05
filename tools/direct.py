#!/usr/bin/env python3
"""Time a call to the phone's UI server on one kept-alive connection,
straight over the tailnet (the server's own port) and through adb's
streams (what the helper uses):

    .venv/bin/python3 tools/direct.py

Run from the burner folder. Goes through the proxy tunnel.sh uses when
HTTPS_PROXY is set (a hosted assistant), else straight to the phone.
Prints each call's time; the first direct one includes the connection."""
import base64
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib", "u2"))

import u2mux  # noqa: E402


def config():
    cfg = {}
    with open(os.path.join(ROOT, "config.env"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def connect_direct(ip, port, timeout=8.0):
    """A TCP connection to ip:port, through the HTTP CONNECT proxy when
    HTTPS_PROXY names one (its port 3130 reaches the tailnet)."""
    proxy = os.environ.get("HTTPS_PROXY", "")
    if not proxy:
        return socket.create_connection((ip, port), timeout)
    rest = proxy.split("://", 1)[-1]
    auth, host = (rest.rsplit("@", 1) + [""])[:2] if "@" in rest else ("", rest)
    host = host.split(":")[0]
    s = socket.create_connection((host, 3130), timeout)
    req = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (ip, port, ip, port)
    if auth:
        req += "Proxy-Authorization: Basic %s\r\n" % base64.b64encode(auth.encode()).decode()
    s.sendall((req + "\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("the proxy closed the connection")
        head += chunk
    status = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 200" not in status:
        raise RuntimeError("proxy: " + status)
    return s


def ping(s):
    """GET /ping on an open connection (kept alive): the body."""
    s.sendall(b"GET /ping HTTP/1.1\r\nHost: phone\r\nConnection: keep-alive\r\n\r\n")
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("the server closed the connection")
        data += chunk
    head, body = data.split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(body) < length:
        chunk = s.recv(4096)
        if not chunk:
            break
        body += chunk
    return body.decode(errors="replace")


def main():
    cfg = config()
    ip, port = cfg["PHONE_TAILSCALE_IP"], int(cfg.get("U2_PORT", "9008"))
    print("direct, one connection to %s:%d (the first call includes connecting):" % (ip, port))
    t0 = time.monotonic()
    s = connect_direct(ip, port)
    s.settimeout(8.0)
    for i in range(4):
        t1 = time.monotonic()
        body = ping(s)
        print("  %-6s %5.0fms%s" % (body.strip()[:6], (time.monotonic() - t1) * 1000,
                                   "  (connect %.0fms)" % ((t1 - t0) * 1000) if i == 0 else ""))
    s.close()

    import uiautomator2 as u2
    u2mux.install_keepalive()
    d = u2.connect(u2mux.TARGET)
    print("through adb, the helper's kept-alive stream:")
    for i in range(4):
        t1 = time.monotonic()
        resp = u2mux._KEEPALIVE.request(d._dev, d._device_server_port, "GET", "/ping", timeout=8.0)
        print("  %-6s %5.0fms" % (resp.content.decode(errors="replace").strip()[:6], (time.monotonic() - t1) * 1000))


if __name__ == "__main__":
    main()
