"""One-shot loopback listener that receives the Qwen console access token.

Alibaba's Token Plan quota is not readable with the plan API key — the official
Bailian CLI instead signs in through the console, which hands a short-lived
access token to a loopback port on the user's machine
(``console-login?notice=127.0.0.1:<port>?state=<nonce>``). This module is the
"port": a tiny HTTP listener bound to 127.0.0.1 that accepts exactly one
delivery whose ``state`` matches the nonce we issued, stores the token, and
closes. No cookies are involved; the token is the same one ``bl`` uses and it
expires on its own.

The token lives in process memory and (optionally) a 0600 file under
``CONSOLE_LINK_STORE`` (a tmpfs path in Docker) so a container restart keeps it,
but it is never logged or returned to the browser.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

#: Bind inside this process. 127.0.0.1 when the app runs on the host itself;
#: in Docker the published port arrives on the container interface, so compose
#: sets CONSOLE_LINK_BIND=0.0.0.0 while pinning the HOST side to loopback —
#: the browser never sees anything but 127.0.0.1.
LISTEN_HOST = os.getenv("CONSOLE_LINK_BIND", "127.0.0.1")
LISTEN_PORT = int(os.getenv("CONSOLE_LINK_PORT", "8761"))
#: The port the browser is told to deliver to (host-side published port when Docker maps it).
ADVERTISE_PORT = int(os.getenv("CONSOLE_LINK_ADVERTISE_PORT", str(LISTEN_PORT)))
LINK_TTL_SECONDS = 300
#: Relay mode (browser on another machine): the user's PC runs a tiny listener
#: that forwards the console's loopback delivery to us, proved by this nonce.
RELAY_TTL_SECONDS = 300
RELAY_MAX_FAILURES = 10
STORE_PATH = os.getenv("CONSOLE_LINK_STORE", "")  # empty = memory only
CONSOLE_ORIGIN = {
    "international": "https://modelstudio.console.alibabacloud.com",
    "domestic": "https://bailian.console.aliyun.com",
}

_lock = threading.Lock()
_token: dict[str, object] | None = None


def _load_store() -> None:
    global _token
    if _token is not None or not STORE_PATH:
        return
    try:
        with open(STORE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("access_token"):
            _token = data
    except OSError:
        return


def _save_store(token: dict[str, object]) -> None:
    if not STORE_PATH:
        return
    tmp = f"{STORE_PATH}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(token, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STORE_PATH)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def clear_token() -> None:
    global _token
    with _lock:
        _token = None
    if STORE_PATH:
        try:
            os.unlink(STORE_PATH)
        except OSError:
            pass


def console_token() -> dict[str, object] | None:
    """The linked token, or None.

    Precedence: an interactive link (memory/store) first; then a static
    ``ALIBABA_TOKEN_PLAN_CONSOLE_TOKEN`` for headless setups — the same token
    ``bl auth login --console`` prints, pasted into the keys file."""
    global _token
    with _lock:
        _load_store()
        if _token:
            return dict(_token)
    from .config import get_key

    env_token = get_key(("ALIBABA_TOKEN_PLAN_CONSOLE_TOKEN",))
    if env_token:
        site = (os.getenv("ALIBABA_TOKEN_PLAN_SITE") or "").strip() or None
        region = (os.getenv("ALIBABA_TOKEN_PLAN_REGION") or "").strip() or None
        if region in {"china", "cn", "cn-beijing", "beijing"}:
            region = "cn-beijing"
            site = site or "domestic"
        elif region:
            region = "ap-southeast-1"
        return {
            "access_token": env_token,
            "console_site": site,
            "console_region": region,
            "source": "env",
        }
    return None


class _Handler(http.server.BaseHTTPRequestHandler):
    def _reply(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # CORS preflight for the console's POST
        self._reply(204, "")

    def _try_accept(self, fields: dict[str, str]) -> bool:
        token = (fields.get("access_token") or fields.get("accessToken") or "").strip()
        state = (fields.get("state") or "").strip()
        expected = getattr(self.server, "state", None)  # runtime type: _LinkServer
        if not token or not expected or not secrets.compare_digest(state, str(expected)):
            # Log nothing about the payload — only that a mismatch happened.
            return False
        stored = {
            "access_token": token,
            "console_site": (fields.get("console_site") or "").strip() or None,
            "console_region": (fields.get("console_region") or "").strip() or None,
            "linked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _store_token(stored)
        return True

    def do_POST(self) -> None:
        length = min(int(self.headers.get("content-length") or 0), 65536)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        fields: dict[str, str] = {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                fields = {k: str(v) for k, v in parsed.items() if isinstance(v, (str, int, float))}
        except ValueError:
            for k, v in parse_qs(raw).items():
                fields[k] = v[0]
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        fields = {**query, **fields}
        if self._try_accept(fields):
            done = getattr(self.server, "done", None)  # runtime type: _LinkServer
            if done:
                done()  # flip state before the client can poll again
            self._reply(200, "OK")
        else:
            self._reply(403, "rejected")

    do_GET = do_PUT = do_POST  # tolerate query-string delivery variants

    def log_message(self, *args) -> None:  # never log requests (they carry tokens)
        pass


class _LinkServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, state: str) -> None:
        super().__init__((LISTEN_HOST, LISTEN_PORT), _Handler)
        self.state = state
        self.result: bool | None = None
        self._idle: threading.Timer | None = None

    def serve_and_close(self) -> None:
        try:
            self.serve_forever()
        finally:
            self.server_close()  # release the port once the loop stops

    def done(self) -> None:
        self.result = True
        # Invalidate immediately: a late delivery must not be accepted while the
        # shutdown thread races to stop the loop.
        self.state = ""
        threading.Thread(target=self.shutdown, daemon=True).start()

    def expired(self) -> None:
        if self.result is None:
            self.result = False
            threading.Thread(target=self.shutdown, daemon=True).start()


class LinkFlow:
    """One link attempt at a time; start() returns the console-login URL."""

    def __init__(self) -> None:
        self._server: _LinkServer | None = None
        self._thread: threading.Thread | None = None
        self._state: str | None = None
        self._idle: threading.Timer | None = None
        #: relay mode: {"code":.., "state":.., "site":.., "failures":int} or None
        self._relay: dict[str, Any] | None = None

    def start(self, site: str = "international") -> str | None:
        self.cancel()
        self._state = secrets.token_hex(16)
        try:
            server = _LinkServer(self._state)
        except OSError:
            return None  # port busy — caller reports "link already in use"
        self._server = server
        self._thread = threading.Thread(target=server.serve_and_close, daemon=True)
        self._thread.start()
        self._idle = threading.Timer(LINK_TTL_SECONDS, server.expired)
        self._idle.daemon = True
        self._idle.start()
        origin = CONSOLE_ORIGIN.get(site, CONSOLE_ORIGIN["international"])
        return f"{origin}/console-login?notice=127.0.0.1:{ADVERTISE_PORT}?state={self._state}"

    def cancel(self) -> None:
        self._relay = None  # a cancel disarms relay mode too
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        server = self._server
        if server is not None:
            server.expired()  # marks the attempt abandoned (idempotent)
            self._thread_join()
        self._server = None

    def _thread_join(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    def clear(self) -> None:
        self.cancel()
        clear_token()

    @property
    def linked(self) -> bool:
        return bool(console_token())

    def status(self) -> str:
        server = self._server
        if server is not None:
            return "linked" if server.result is True else "waiting"
        if self.relay_armed():
            return "waiting"  # a relay PC holds the attempt open
        return "idle" if not self.linked else "linked"

    def relay_armed(self) -> bool:
        relay = self._relay
        return bool(relay) and float(relay["armed_at"]) + RELAY_TTL_SECONDS > time.time()

    # --- relay mode: browser on another machine -------------------------------

    def relay_start(self, site: str = "international") -> dict[str, Any]:
        """Arm a relay link: returns the short code the PC-side relay hands
        back to us with each forwarded delivery (proof-of-link), plus the
        console-login URL for reference."""
        self.cancel()
        code = secrets.token_hex(3)          # 6 chars, human-typable in a command
        self._relay = {
            "code": code,
            "state": secrets.token_hex(16),  # nonce embedded in the console URL
            "site": site,
            "failures": 0,
            "armed_at": time.time(),
        }
        return dict(self._relay)

    def relay_claim(self, code: str, port: int) -> str | None:
        """A relay PC announces itself and picks up the console URL. One relay
        at a time; a wrong code means someone is guessing — count it.

        The console delivers to 127.0.0.1:<port> — on the RELAY's machine,
        where the user's browser runs and where nothing else listens."""
        relay = self._relay
        if not relay or float(relay["armed_at"]) + RELAY_TTL_SECONDS < time.time():
            return None
        if not code or not secrets.compare_digest(code, str(relay["code"])):
            relay["failures"] = int(relay["failures"]) + 1
            if relay["failures"] >= RELAY_MAX_FAILURES:
                self._relay = None  # someone is guessing: burn the attempt
            return None
        origin = CONSOLE_ORIGIN.get(str(relay["site"]), CONSOLE_ORIGIN["international"])
        url = f"{origin}/console-login?notice=127.0.0.1:{int(port)}?state={relay['state']}"
        relay["url"] = url
        return url

    def relay_console_url(self) -> str:
        relay = self._relay
        return str((relay or {}).get("url", ""))

    def relay_deliver(self, code: str, state: str, token: dict[str, str]) -> bool:
        """Accept a token forwarded by the user's relay PC. Both the short code
        and the console-URL state nonce must match; then the attempt is spent."""
        relay = self._relay
        if not relay:
            return False
        if float(relay["armed_at"]) + RELAY_TTL_SECONDS < time.time():
            self._relay = None
            return False
        ok_code = secrets.compare_digest(code or "", str(relay["code"]))
        ok_state = secrets.compare_digest(state or "", str(relay["state"]))
        if not (ok_code and ok_state):
            relay["failures"] = int(relay["failures"]) + 1
            if relay["failures"] >= RELAY_MAX_FAILURES:
                self._relay = None  # looks like guessing: burn the attempt
            return False
        access = (token.get("access_token") or token.get("accessToken") or "").strip()
        if not access:
            return False
        _store_token({
            "access_token": access,
            "console_site": (token.get("console_site") or "").strip() or str(relay["site"]) or None,
            "console_region": (token.get("console_region") or "").strip() or None,
            "linked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "via": "relay",
        })
        self._relay = None
        return True


def _store_token(stored: dict[str, object]) -> None:
    global _token
    with _lock:
        _token = stored
    _save_store(stored)


link_flow = LinkFlow()
