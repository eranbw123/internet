"""Minimal Chrome DevTools Protocol (CDP) client, stdlib-only.

Vendored verbatim from the sibling `ai` repo's cdp.py (the proven client its
claude.ai export scripts and council bot run on); keep the two copies in sync
rather than diverging.

Connects to a Chrome instance launched with --remote-debugging-port and lets
Python run JavaScript inside one of its real, already-authenticated tabs via
Runtime.evaluate -- the same mechanism the DevTools Console itself uses.

This is not a general WebSocket library: just enough of RFC 6455 (client-to-
server masked text frames, up to Chrome's typical response sizes) to talk to
a local CDP endpoint.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import urllib.request
from urllib.parse import urlparse


def list_tabs(port=9222, timeout=None):
    # timeout=None keeps this call blocking indefinitely, same as before --
    # health.py's preflight() is the one caller that needs a bound (a
    # port that accepts connections but never answers must not hang a
    # "free" reachability check forever) and passes one explicitly.
    with urllib.request.urlopen(f"http://localhost:{port}/json", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Paths on the app's own origin that are NOT the app. Another tool sharing this
# Chrome may park a scratch tab on one purely to get a same-origin JS context --
# the sibling `ai` repo's corpus_backfill.own_tab() opens <origin>/robots.txt on
# purpose, precisely so it does not have to touch this appliance's tab. /json
# lists targets newest-first, so a scratch tab opened seconds ago outranks the
# real logged-in one, and a bare prefix match hands it straight back. Driving
# someone else's tab then breaks both sides: they close it mid-request (our
# websocket dies), and on claude.ai/chatgpt.com a plain-text document is not the
# signed-in SPA the send JS expects. "No app tab" is the honest answer instead.
NON_APP_PATHS = frozenset({
    "/robots.txt", "/favicon.ico", "/sitemap.xml", "/manifest.json", "/ads.txt",
})


def is_app_tab(url):
    """False for a same-origin scratch/asset page (see NON_APP_PATHS)."""
    try:
        path = (urlparse(url).path or "/").lower()
    except ValueError:
        return False
    return path not in NON_APP_PATHS


def find_tab(url_prefix, port=9222, timeout=None):
    for tab in list_tabs(port, timeout=timeout):
        url = tab.get("url", "")
        if tab.get("type") == "page" and url.startswith(url_prefix) and is_app_tab(url):
            return tab
    return None


def find_claude_tab(port=9222, timeout=None):
    return find_tab("https://claude.ai", port, timeout=timeout)


def find_chatgpt_tab(port=9222, timeout=None):
    # chatgpt.com is the live origin; some sessions still land on
    # chat.openai.com, so accept either -- same pair the ai repo's exporter
    # checks (chatgpt_export_cdp.connect).
    return (find_tab("https://chatgpt.com", port, timeout=timeout)
            or find_tab("https://chat.openai.com", port, timeout=timeout))


class CDPConnection:
    def __init__(self, ws_url):
        parsed = urlparse(ws_url)
        self.host = parsed.hostname
        self.port = parsed.port
        self.path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self._id = 0
        self.sock = socket.create_connection((self.host, self.port), timeout=30)
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("utf-8"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed (connection closed)")
            response += chunk
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WebSocket handshake rejected: {response[:200]!r}")

    def _send_text(self, text):
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        header = bytearray([0x81])  # FIN + text frame opcode
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_frame(self):
        header = self._recv_exact(2)
        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask_key = self._recv_exact(4) if masked else b""
        data = self._recv_exact(length)
        if masked:
            data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        return opcode, data

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket connection closed unexpectedly")
            buf += chunk
        return buf

    def send(self, method, params=None, timeout=60):
        self._id += 1
        msg_id = self._id
        self._send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        self.sock.settimeout(timeout)
        while True:
            opcode, data = self._recv_frame()
            if opcode != 0x1:  # text frame
                continue
            message = json.loads(data.decode("utf-8"))
            if message.get("id") == msg_id:
                return message

    def evaluate(self, expression, await_promise=True, timeout=120):
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
            timeout=timeout,
        )
        remote = result.get("result", {})
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS exception: {result['exceptionDetails']}")
        return remote.get("result", {}).get("value")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
