"""HTTP client for the Veydrift read API.

Owned by WP1. Wraps `httpx` with `tenacity` retries and a disk cache under
`$VEYDRIFT_HOME/cache/`. `read.py` is the only intended caller.

Retry policy (SPEC.md §5.2): 3 attempts total, exponential backoff. Retries network
errors (`httpx.TransportError` -- connect/read/write/pool timeouts and connection
failures) and 5xx responses. **Never** retries a 4xx -- that is a client error the API
will answer the same way on attempt 2.

Caching: keyed by `path + sorted(params)`, one JSON file per key under
`$VEYDRIFT_HOME/cache/`. Default TTL is 60s; `/health` gets 15s (SPEC.md §5.2). A
non-200 response is **never** written to the cache. Caching is best-effort: a full disk
or unwritable cache directory degrades to "always fetch", never to a crash.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from veydrift_agent.state import veydrift_home

API_BASE_URL = "https://api.veydrift.com"

#: SPEC.md §5.2: "10 s connect / 30 s read timeout".
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 30.0

#: Disk-cache TTLs. `/health` is short-lived on purpose -- it is the input to the guard
#: gate and to `vd tick`'s killswitch-adjacent health check.
DEFAULT_MAX_AGE_S = 60.0
HEALTH_MAX_AGE_S = 15.0

_RETRY_ATTEMPTS = 3


class VeydriftAPIError(Exception):
    """Base class for every error this module raises."""


class VeydriftHTTPError(VeydriftAPIError):
    """A non-retryable 4xx response. Maps to CLI exit code 4 (bad args) by convention --
    every 4xx this API returns on these routes is a caller mistake (bad planetId, missing
    query param), never a transient condition."""

    def __init__(self, status_code: int, path: str, body: str) -> None:
        self.status_code = status_code
        self.path = path
        self.body = body
        super().__init__(f"HTTP {status_code} from {path}: {body[:300]!r}")


class VeydriftServerError(VeydriftAPIError):
    """A 5xx that survived all retry attempts. Maps to CLI exit code 2 (API unhealthy)."""

    def __init__(self, status_code: int, path: str, body: str) -> None:
        self.status_code = status_code
        self.path = path
        self.body = body
        super().__init__(f"HTTP {status_code} from {path} after {_RETRY_ATTEMPTS} attempts: {body[:300]!r}")


class VeydriftNetworkError(VeydriftAPIError):
    """A connection/timeout failure that survived all retry attempts. Maps to CLI exit
    code 3 (network)."""


class _RetryableServerError(Exception):
    """Internal signal so `tenacity` retries a 5xx the same way it retries a transport
    error, without our own callers ever seeing this type."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response


def _route_max_age(path: str, override: float | None) -> float:
    if override is not None:
        return override
    if path.rstrip("/") == "/health":
        return HEALTH_MAX_AGE_S
    return DEFAULT_MAX_AGE_S


def _cache_dir() -> Path:
    d = veydrift_home() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(path: str, params: Mapping[str, Any] | None) -> str:
    normalised = json.dumps(
        {"path": path, "params": dict(sorted((params or {}).items(), key=lambda kv: kv[0]))},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _cache_read(path: str, params: Mapping[str, Any] | None, max_age: float) -> dict[str, Any] | None:
    if max_age <= 0:
        return None
    cache_file = _cache_dir() / f"{_cache_key(path, params)}.json"
    if not cache_file.exists():
        return None
    try:
        payload = json.loads(cache_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    cached_at = payload.get("cached_at")
    if not isinstance(cached_at, (int, float)):
        return None
    if time.time() - cached_at > max_age:
        return None
    return payload.get("body")


def _cache_write(path: str, params: Mapping[str, Any] | None, body: Any) -> None:
    cache_file = _cache_dir() / f"{_cache_key(path, params)}.json"
    payload = {"cached_at": time.time(), "path": path, "params": dict(params or {}), "body": body}
    try:
        cache_file.write_text(json.dumps(payload))
    except OSError:
        pass  # caching is best-effort; a full disk must never break a read


@retry(
    stop=stop_after_attempt(_RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type((httpx.TransportError, _RetryableServerError)),
    reraise=True,
)
def _request(client: httpx.Client, path: str, params: Mapping[str, Any] | None) -> httpx.Response:
    response = client.get(path, params=params)
    if response.status_code >= 500:
        # Raised (rather than just returned) so tenacity's retry predicate catches it the
        # same way it catches a transport-level failure.
        raise _RetryableServerError(response)
    return response


def fetch(
    path: str,
    params: Mapping[str, Any] | None = None,
    *,
    max_age: float | None = None,
) -> dict[str, Any]:
    """GET a JSON route from the Veydrift API, honouring the disk cache.

    ``path`` is the route (e.g. ``"/wallet/0xabc/infrastructure"``), never including the
    scheme/host. ``max_age`` overrides the route's default TTL in seconds; ``0`` forces a
    live fetch.

    Raises `VeydriftHTTPError` for a 4xx, `VeydriftServerError` for a 5xx that survived
    retries, `VeydriftNetworkError` for a connection/timeout failure that survived
    retries.
    """
    resolved_max_age = _route_max_age(path, max_age)
    cached = _cache_read(path, params, resolved_max_age)
    if cached is not None:
        return cached

    timeout = httpx.Timeout(CONNECT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S)
    try:
        with httpx.Client(base_url=API_BASE_URL, timeout=timeout) as client:
            response = _request(client, path, params)
    except _RetryableServerError as exc:
        raise VeydriftServerError(exc.response.status_code, path, exc.response.text) from exc
    except httpx.TransportError as exc:
        raise VeydriftNetworkError(f"network error calling {path}: {exc}") from exc

    if response.status_code >= 400:
        raise VeydriftHTTPError(response.status_code, path, response.text)

    body = response.json()
    _cache_write(path, params, body)
    return body
