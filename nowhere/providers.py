"""Single HTTP exit point with timeout, circuit-breaker, and in-process cache."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from collections import OrderedDict
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# LOG-04: never let credentials reach the log line (exceptions embed the URL).
# 按位置盲脱敏: 凭据参数名枚举不完(pwd/auth/sig/credential/session/...),
# 契约又鼓励凭据走 params, 防护不能依赖参数取名 → query 值一律打码
_SECRET_RE = re.compile(r"([?&][^=&\s'\"]+=)[^&\s'\"]+")


def _redact(text: str) -> str:
    """Mask all query values before logging."""
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
# source -> lock: 熔断状态读改写与半开探测放行的临界区
_circuit_locks: dict[str, asyncio.Lock] = {}
# sources with a half-open probe in flight (只放一个探测请求, 防惊群)
_probe_in_flight: set[str] = set()
CIRCUIT_OPEN_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60.0


def _circuit_lock(source: str) -> asyncio.Lock:
    lock = _circuit_locks.get(source)
    if lock is None:
        # 事件循环单线程内 check-then-set 无 await 间隙, 无需额外加锁
        lock = _circuit_locks[source] = asyncio.Lock()
    return lock


# ── Cache ─────────────────────────────────────────────────────────
# url -> (expiry_timestamp, data); LRU 有界, 防长驻进程内存单调增长
_CACHE_MAX_ENTRIES = 256
_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()


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
    lock = _circuit_lock(source)
    probe = False
    async with lock:
        if _failure_counts.get(source, 0) >= CIRCUIT_OPEN_THRESHOLD:
            opened_at = _circuit_opened_at.get(source, 0)
            if time.monotonic() - opened_at < CIRCUIT_COOLDOWN_SECONDS:
                return None
            # Cooldown elapsed → half-open: 只放一个探测请求, 其余直接让路
            if source in _probe_in_flight:
                return None
            _probe_in_flight.add(source)
            probe = True

    try:
        # ── Cache lookup ──────────────────────────────────────────
        cache_key = url if not params else url + "?" + repr(sorted(params.items()))
        if cache_ttl > 0 and cache_key in _cache:
            expiry, data = _cache[cache_key]
            if time.monotonic() < expiry:
                _cache.move_to_end(cache_key)
                return copy.deepcopy(data)
            else:
                del _cache[cache_key]

        # ── HTTP request ──────────────────────────────────────────
        client = _get_client()
        resp = await client.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("provider response must be a JSON object")
    except Exception as exc:
        # LOG-04: exception text embeds the request URL — redact secrets
        exc_msg = _redact(str(exc))
        async with lock:
            _failure_counts[source] = _failure_counts.get(source, 0) + 1
            if _failure_counts[source] >= CIRCUIT_OPEN_THRESHOLD:
                _circuit_opened_at[source] = time.monotonic()
                logger.warning("Circuit opened for %s after %d failures: %s", source, _failure_counts[source], exc_msg)
            else:
                logger.debug("fetch_json %s failed (%d/%d): %s", source, _failure_counts[source], CIRCUIT_OPEN_THRESHOLD, exc_msg)
        return None
    finally:
        if probe:
            async with lock:
                _probe_in_flight.discard(source)

    # ── Success: reset failure count, populate cache ──────────────
    async with lock:
        _failure_counts[source] = 0

    if cache_ttl > 0:
        # 缓存存私有副本: 命中时再深拷贝返回, 防调用方原地修改污染缓存
        _cache[cache_key] = (time.monotonic() + cache_ttl, copy.deepcopy(data))
        _cache.move_to_end(cache_key)
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)

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
