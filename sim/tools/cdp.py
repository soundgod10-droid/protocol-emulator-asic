# SPDX-License-Identifier: Apache-2.0
"""Drives a headless Chromium browser (Google Chrome, Edge) through the DevTools protocol, standard library only.

Use as a module: with Browser(port, profile) as b: b.open(url); b.wait("js expression"); b.shot(path)

What it does:
  Starts the browser (path in the environment variable CHROME, default google-chrome) with --headless=new, a remote
  debugging port and a fresh profile folder, connects to the page target over a WebSocket (RFC 6455, written here on a
  plain socket), and offers: navigate, evaluate, wait until an expression is true, set the viewport size, take a PNG
  screenshot, and collect console messages and exceptions (page and workers).

Why:
  The simulator's engine loads in a web worker for several seconds, so the page must be read when a stated condition
  holds, not at a fixed time after the load event.
"""

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request

CHROME = os.environ.get("CHROME", "google-chrome")


class WebSocket:
    def __init__(self, url):
        assert url.startswith("ws://")
        hostport, path = url[5:].split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.s.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.s.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError(head.decode(errors="replace"))
        self.buf = b""

    def send(self, text):
        data = text.encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(1 << 20)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        parts = []
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts).decode()


class Browser:
    def __init__(self, port, profile, width=1440, height=900, extra=()):
        self.port, self.profile, self.width, self.height = port, profile, width, height
        self.extra = list(extra)
        self.n = 0
        self.console = []

    def __enter__(self):
        os.makedirs(self.profile, exist_ok=True)
        args = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--no-sandbox", f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}",
                f"--window-size={self.width},{self.height}", "--hide-scrollbars", *self.extra, "about:blank"]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pages = []
        for _ in range(150):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=1))
                pages = [t for t in targets if t["type"] == "page"]
                if pages:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        if not pages:
            self.proc.kill()
            raise RuntimeError(f"{CHROME}: no page target on port {self.port}")
        self.ws = WebSocket(pages[0]["webSocketDebuggerUrl"])
        for m in ("Runtime.enable", "Page.enable", "Log.enable"):
            self.call(m)
        self.call("Target.setAutoAttach", autoAttach=True, waitForDebuggerOnStart=False, flatten=True)
        return self

    def __exit__(self, *exc):
        try:
            self.ws.s.close()
        finally:
            self.proc.kill()
            self.proc.wait()

    def _handle(self, msg):
        method = msg.get("method")
        p = msg.get("params", {})
        if method == "Runtime.consoleAPICalled":
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
            self.console.append(f"[{p.get('type')}] {text}")
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            self.console.append(f"[exception] {d.get('text')} {d.get('exception', {}).get('description', '')}")
        elif method == "Log.entryAdded":
            e = p.get("entry", {})
            self.console.append(f"[log {e.get('level')}] {e.get('text')} {e.get('url', '')}")
        elif method == "Target.attachedToTarget":
            sid = p["sessionId"]
            self._send({"id": self._next(), "method": "Runtime.enable", "sessionId": sid})
            self._send({"id": self._next(), "method": "Runtime.runIfWaitingForDebugger", "sessionId": sid})

    def _next(self):
        self.n += 1
        return self.n

    def _send(self, obj):
        self.ws.send(json.dumps(obj))

    def call(self, method, **params):
        i = self._next()
        self._send({"id": i, "method": method, "params": params})
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == i and "sessionId" not in msg:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self._handle(msg)

    def eval(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    def viewport(self, width, height, scale=1, mobile=False):
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=scale,
                  mobile=mobile)

    def open(self, url):
        self.call("Page.navigate", url=url)

    def wait(self, expr, timeout=120, every=0.25):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                v = self.eval(expr)
            except RuntimeError:
                v = None
            if v:
                return v, time.time() - t0
            time.sleep(every)
        raise TimeoutError(f"{expr} not true after {timeout} s")

    def shot(self, path, full=False):
        params = {"format": "png"}
        if full:
            m = self.call("Page.getLayoutMetrics")
            size = m["cssContentSize"]
            params["clip"] = {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1}
            params["captureBeyondViewport"] = True
        data = self.call("Page.captureScreenshot", **params)["data"]
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
