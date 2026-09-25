#!/usr/bin/env python3
"""Qwen console-link relay — run this on the machine that has your BROWSER.

Why: the dashboard's one-click console link only works when the browser runs on
the same machine as the dashboard server, because the Qwen console delivers its
token to 127.0.0.1 (a browser security rule, not a dashboard one). When your
browser is elsewhere, this tiny relay listens on THIS machine's loopback,
receives the console's delivery, and forwards it once to the dashboard over
your LAN. Nothing else passes through it; it exits after one delivery or a few
minutes, and prints every step. No dependencies beyond Python 3.8+.

Usage (exactly what the dashboard's "Browser on another device?" button shows):

    python3 link-relay.py --server http://<dashboard-host>:<port> --code <CODE>
"""

from __future__ import annotations

import argparse
import http.server
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

TIMEOUT_SECONDS = 300


class RelayHandler(http.server.BaseHTTPRequestHandler):
    def _reply(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # browser preflight for the console's POST
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("origin") or "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_POST(self) -> None:
        length = min(int(self.headers.get("content-length") or 0), 65536)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            self._reply(400, "this endpoint only accepts the Qwen console's JSON delivery")
            return
        # the console puts our state nonce on the delivery URL's query string
        query = {k: v[0] for k, v in urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.path).query).items()}
        payload = {**query, **payload}
        if not payload.get("access_token"):
            self._reply(403, "rejected")
            return
        ok = self.server.relay.forward(payload)  # runtime attr: Runner
        if ok:
            self._reply(200, "Received — forwarded to the dashboard. You can close this tab.")
        else:
            self._reply(502, "The dashboard rejected this delivery (expired or already used).")

    do_GET = do_POST  # tolerate query-string delivery variants

    def log_message(self, *args) -> None:  # never echo delivery content
        pass


class RelayServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    relay: "Runner"

    def finish(self) -> None:
        threading.Thread(target=self.shutdown, daemon=True).start()


class Runner:
    def __init__(self, server: str, code: str, port: int) -> None:
        self.server = server.rstrip("/")
        self.code = code.strip()
        self.port = port

    def _fetch(self, url: str, body: bytes | None = None) -> dict:
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}")

    def claim(self) -> str | None:
        """Announce ourselves to the dashboard; returns the console-login URL."""
        try:
            data = self._fetch(f"{self.server}/api/link/relay/claim"
                               f"?code={urllib.parse.quote(self.code)}&port={self.port}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"Cannot reach the dashboard at {self.server}: {exc}")
            return None
        if not data.get("ok"):
            print("The dashboard rejected this link code — press the button again for a fresh one.")
            return None
        return data.get("url")

    def forward(self, payload: dict) -> bool:
        body = json.dumps({
            "code": self.code,
            "state": payload.get("state", ""),
            "access_token": payload.get("access_token", ""),
            "console_site": payload.get("console_site", ""),
            "console_region": payload.get("console_region", ""),
        }).encode()
        try:
            data = self._fetch(f"{self.server}/api/link/relay/deliver", body)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"The token arrived but could not reach the dashboard: {exc}")
            return False
        if data.get("ok"):
            print("Token forwarded to the dashboard — the Qwen card should light up. Done.")
            server = getattr(self, "server_obj", None)
            if server is not None:
                server.finish()
            return True
        print("The dashboard rejected this delivery (expired or already used). Press the button again.")
        return False

    def serve(self) -> int:
        try:
            httpd = RelayServer(("127.0.0.1", self.port), RelayHandler)
        except OSError:
            print(f"Port {self.port} is busy on this machine; pass --port <other>.")
            return 2
        httpd.relay = self
        self.server_obj = httpd
        url = self.claim()
        if not url:
            httpd.server_close()
            return 1
        print(f"Listening on 127.0.0.1:{self.port} — opening the Qwen sign-in page…")
        print("If no tab opens, copy this into your browser:\n  " + url)
        webbrowser.open(url)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        timer = threading.Timer(TIMEOUT_SECONDS, httpd.finish)
        timer.daemon = True
        timer.start()
        httpd.serve_forever()
        timer.cancel()
        httpd.server_close()
        left = deadline - time.monotonic()
        if left <= 0:
            print("Timed out waiting for the sign-in. Press the button on the card to try again.")
            return 1
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", required=True, help="dashboard base URL, e.g. http://192.0.2.10:8760")
    parser.add_argument("--code", required=True, help="the short link code shown by the dashboard")
    parser.add_argument("--port", type=int, default=8199, help="local port to listen on (default 8199)")
    args = parser.parse_args()
    return Runner(args.server, args.code, args.port).serve()


if __name__ == "__main__":
    raise SystemExit(main())
