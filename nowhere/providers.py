"""Single HTTP exit point with timeout, circuit-breaker, and in-process cache."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# LOG-04: never let credentials reach the log line (exceptions embed the URL).
_SECRET_RE = re.compile(
    r"((?:key|token|apikey|api_key|secret|password|authorization)=)[^&\s'\"]+",
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    """Mask credential-like query values before logging."""
    return _SECRET_RE.sub(r"\1***", text)

# ── Singleton client ──────────────────────────────────────────────
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": "nowhere-mcp/0.1"},
        )
    return _client


# ── Circuit-breaker state ─────────────────────────────────────────
# source -> consecutive failure count
_failure_counts: dict[str, int] = {}
# source -> monotonic timestamp when circuit opened
_circuit_opened_at: dict[str, float] = {}
CIRCUIT_OPEN_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60.0


# ── Cache ─────────────────────────────────────────────────────────
# url -> (expiry_timestamp, data)
_cache: dict[str, tuple[float, Any]] = {}


# ── Public API ────────────────────────────────────────────────────

async def fetch_json(
    url: str,
    *,
    source: str,
    cache_ttl: float = 0,
    timeout: float = 2.0,
    params: dict[str, Any] | None = None,
) -> dict | None:
    """Fetch JSON from *url*.  Never raises; returns ``None`` on any failure.

    Parameters
    ----------
    url:
        The URL to fetch.  Keep credentials out of here — pass them via
        *params* so they never sit in a string that might get logged.
    source:
        Circuit-breaker key (one key per upstream provider).
    cache_ttl:
        If > 0, cache the result for this many seconds.
    timeout:
        HTTP timeout in seconds.
    params:
        Optional query parameters, forwarded to httpx.  Prefer this over
        inlining secrets into *url* (LOG-04).
    """
    # ── Circuit breaker ───────────────────────────────────────────
    if _failure_counts.get(source, 0) >= CIRCUIT_OPEN_THRESHOLD:
        opened_at = _circuit_opened_at.get(source, 0)
        if time.monotonic() - opened_at < CIRCUIT_COOLDOWN_SECONDS:
            return None
        # Cooldown elapsed → half-open: allow one probe request below

    # ── Cache lookup ──────────────────────────────────────────────
    cache_key = url if not params else url + "?" + repr(sorted(params.items()))
    if cache_ttl > 0 and cache_key in _cache:
        expiry, data = _cache[cache_key]
        if time.monotonic() < expiry:
            return data
        else:
            del _cache[cache_key]

    # ── HTTP request ──────────────────────────────────────────────
    try:
        client = _get_client()
        resp = await client.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("provider response must be a JSON object")
    except Exception as exc:
        _failure_counts[source] = _failure_counts.get(source, 0) + 1
        # LOG-04: exception text embeds the request URL — redact secrets
        exc_msg = _redact(str(exc))
        if _failure_counts[source] >= CIRCUIT_OPEN_THRESHOLD:
            _circuit_opened_at[source] = time.monotonic()
            logger.warning("Circuit opened for %s after %d failures: %s", source, _failure_counts[source], exc_msg)
        else:
            logger.debug("fetch_json %s failed (%d/%d): %s", source, _failure_counts[source], CIRCUIT_OPEN_THRESHOLD, exc_msg)
        return None

    # ── Success: reset failure count, populate cache ──────────────
    _failure_counts[source] = 0

    if cache_ttl > 0:
        _cache[cache_key] = (time.monotonic() + cache_ttl, data)

    return data


def provider_status() -> dict[str, str]:
    """Return ``{source: "ok" | "degraded" | "down"}`` for every known source."""
    result: dict[str, str] = {}
    all_sources = set(_failure_counts.keys())
    for src in all_sources:
        count = _failure_counts.get(src, 0)
        if count == 0:
            result[src] = "ok"
        elif count < CIRCUIT_OPEN_THRESHOLD:
            result[src] = "degraded"
        else:
            result[src] = "down"
    return result


def reset_for_tests() -> None:
    """Clear all circuit-breaker counts and the cache.  Called by conftest."""
    _failure_counts.clear()
    _circuit_opened_at.clear()
    _cache.clear()
