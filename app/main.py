"""FastAPI application: JSON API + one static page."""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .console_link import LINK_TTL_SECONDS, console_token, link_flow
from .providers import Provider, ProviderResult, build_providers, catalog
from .ratelimit import RefreshLimiter, client_key

STATIC_DIR = Path(__file__).parent / "static"

CACHE_TTL_SECONDS = float(os.getenv("CACHE_TTL_SECONDS", "60"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
REFRESH_MIN_INTERVAL_SECONDS = float(os.getenv("REFRESH_MIN_INTERVAL_SECONDS", "10"))
REFRESH_MAX_PER_MINUTE = int(os.getenv("REFRESH_MAX_PER_MINUTE", "20"))
#: Providers without a key are hidden by default; set SHOW_UNCONFIGURED=true to get a
#: placeholder card for each one instead.
SHOW_UNCONFIGURED = os.getenv("SHOW_UNCONFIGURED", "").strip().lower() in {"1", "true", "yes", "on"}
USER_AGENT = os.getenv("USER_AGENT", f"ai-credit-watch/{__version__}")
APP_TITLE = os.getenv("APP_TITLE", "AI Credit Watch")
BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER") or ""
BASIC_AUTH_PASSWORD = os.getenv("BASIC_AUTH_PASSWORD") or ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _provider_payload(provider: Provider, result: ProviderResult | None) -> dict[str, Any]:
    entry = provider.catalog_entry()
    if result is None:
        entry.update(
            {
                "status": "unconfigured",
                "ok": False,
                "error": None,
                "note": f"Not configured — set {entry['key_env']} to include this provider.",
                "balances": [],
                "meta": {},
                "fetched_at": None,
            }
        )
        return entry
    entry.update(result.as_dict())
    entry["status"] = "ok" if result.ok else "error"
    entry["fetched_at"] = _now_iso()
    return entry


async def _fetch_one(client: httpx.AsyncClient, provider: Provider) -> dict[str, Any]:
    if not provider.configured:
        return _provider_payload(provider, None)
    try:
        result = await provider.fetch(client)
    except Exception as exc:  # a broken provider must not break the dashboard
        result = ProviderResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    return _provider_payload(provider, result)


def make_client() -> httpx.AsyncClient:
    """Build the HTTP client used for provider calls.

    Isolated so tests can swap in an ``httpx.MockTransport``.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


async def collect_all() -> dict[str, Any]:
    providers = build_providers()
    started = time.perf_counter()
    async with make_client() as client:
        payloads = await asyncio.gather(*(_fetch_one(client, p) for p in providers))
    return {
        "updated_at": _now_iso(),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "version": __version__,
        "title": APP_TITLE,
        "show_unconfigured": SHOW_UNCONFIGURED,
        "providers": list(payloads),
    }


class BalanceCache:
    """Caching keeps us from hammering provider APIs on every page load."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._payload: dict[str, Any] | None = None
        self._stored_at = 0.0
        self._lock = asyncio.Lock()

    def fresh(self) -> bool:
        return self._payload is not None and (time.monotonic() - self._stored_at) < self.ttl

    async def get(self, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            fresh = self.fresh()
            if fresh and not force:
                return {**self._payload, "cached": True}  # type: ignore[dict-item]
            payload = await collect_all()
            self._payload = payload
            self._stored_at = time.monotonic()
            return {**payload, "cached": False}


cache = BalanceCache(CACHE_TTL_SECONDS)
rate_limiter = RefreshLimiter(
    min_interval=REFRESH_MIN_INTERVAL_SECONDS, max_per_minute=REFRESH_MAX_PER_MINUTE
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Say out loud what this instance is exposing — no secret values, just state."""
    providers = build_providers()
    ready = [p.id for p in providers if p.configured]
    waiting = [p.id for p in providers if not p.configured]
    print(
        "[ai-credit-watch] providers ready: "
        + (", ".join(ready) or "none")
        + (f" | awaiting keys: {', '.join(waiting)}" if waiting else ""),
        flush=True,
    )
    if BASIC_AUTH_USER and BASIC_AUTH_PASSWORD:
        print("[ai-credit-watch] basic auth: enabled", flush=True)
    else:
        print(
            "[ai-credit-watch] basic auth: DISABLED (anyone who can reach this port can read "
            "your balances — set BASIC_AUTH_USER/BASIC_AUTH_PASSWORD if that is not just you)",
            flush=True,
        )
    print(
        f"[ai-credit-watch] cache ttl {CACHE_TTL_SECONDS:g}s, provider timeout {HTTP_TIMEOUT_SECONDS:g}s",
        flush=True,
    )
    if rate_limiter.enabled:
        print(
            f"[ai-credit-watch] refresh limits: 1 upstream refresh per client every "
            f"{rate_limiter.min_interval:g}s, max {rate_limiter.max_per_minute}/min overall",
            flush=True,
        )
    else:
        print("[ai-credit-watch] refresh limits: disabled", flush=True)
    yield


app = FastAPI(
    title=APP_TITLE,
    version=__version__,
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional HTTP Basic auth. Off unless BASIC_AUTH_USER/_PASSWORD are set."""
    if not (BASIC_AUTH_USER and BASIC_AUTH_PASSWORD) or request.url.path == "/healthz":
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            user = password = ""
        if secrets.compare_digest(user, BASIC_AUTH_USER) and secrets.compare_digest(
            password, BASIC_AUTH_PASSWORD
        ):
            return await call_next(request)

    return JSONResponse(
        {"detail": "Authentication required"},
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="AI Credit Watch"'},
    )


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/api/providers")
async def api_providers() -> dict[str, Any]:
    """Provider catalog: names, docs links and whether a key is configured."""
    return {
        "title": APP_TITLE,
        "version": __version__,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "show_unconfigured": SHOW_UNCONFIGURED,
        **rate_limiter.describe(),
        "providers": catalog(),
    }


def _console_site() -> str:
    """Which console login page matches the configured Token Plan region."""
    region = (os.getenv("ALIBABA_TOKEN_PLAN_REGION") or "").strip().lower()
    return "domestic" if region in {"china", "cn", "cn-beijing", "beijing"} else "international"


@app.post("/api/link/start")
async def api_link_start() -> Any:
    """Begin a one-shot loopback link: returns the console-login URL to open."""
    url = link_flow.start(_console_site())
    if url is None:
        return JSONResponse({"detail": "Link port is busy — retry in a moment."}, status_code=409)
    return {"url": url, "status": "waiting", "expires_in_seconds": LINK_TTL_SECONDS}


@app.get("/api/link/status")
async def api_link_status() -> dict[str, Any]:
    return {"status": link_flow.status(), "console_linked": bool(console_token())}


@app.post("/api/link/cancel")
async def api_link_cancel() -> dict[str, Any]:
    link_flow.cancel()
    return {"status": link_flow.status()}


@app.post("/api/link/unlink")
async def api_link_unlink() -> dict[str, Any]:
    link_flow.clear()
    return {"status": link_flow.status(), "console_linked": bool(console_token())}


@app.get("/api/balances", response_model=None)
async def api_balances(request: Request, refresh: bool = False) -> Any:
    """Current balances for every configured provider.

    A cached response needs no budget; only requests that would actually call the
    provider APIs (a forced refresh, or a stale/missing cache) consume a token.
    """
    if refresh or not cache.fresh():
        caller = client_key(
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
        )
        allowed, retry_after = rate_limiter.acquire(caller)
        if not allowed:
            wait = max(1, int(retry_after + 0.999))
            return JSONResponse(
                {
                    "detail": "Refresh rate limit reached — try again shortly.",
                    "retry_after": wait,
                    "refresh_min_interval_seconds": rate_limiter.min_interval,
                    "refresh_max_per_minute": rate_limiter.max_per_minute,
                },
                status_code=429,
                headers={"Retry-After": str(wait), "Cache-Control": "no-store"},
            )
    return await cache.get(force=refresh)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
