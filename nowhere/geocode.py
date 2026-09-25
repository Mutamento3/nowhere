"""地理编码: Nominatim 在线 → GeoNames 离线兜底(买断数据)。

离线源: nowhere/data/packs/cities15000.txt(全球 1.5 万+ 城镇,
中文名在 alternatenames 列,按人口排序取最大)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
import urllib.parse
from typing import Final

import httpx

from nowhere.providers import _get_client

logger = logging.getLogger(__name__)

_PACK_PATH = pathlib.Path(__file__).resolve().parent / "data" / "packs" / "cities15000.txt"
_SPECIAL_PATH = pathlib.Path(__file__).resolve().parent / "data" / "special_places.json"

# Cache geocode results to avoid re-scanning places.db / cities15000 on every call
_geocode_cache: dict[str, tuple[float, float] | None] = {}
_GEOCODE_CACHE_MAX: Final = 500
_special_places: dict[str, dict] | None = None

# 负缓存: Nominatim 的一次瞬时故障不该被固化成永久 None,
# 但也不必每次都重打 5s 超时的请求 —— 短 TTL 后允许重试
_negative_cache: dict[str, float] = {}
_NEGATIVE_TTL: Final = 60.0

# cities15000 预处理行: (name_lower, ascii_lower, 别名tokens, pop, coords)
# 只在首次离线查找时加载并规范化一次, 之后查询不再重复 open/.lower()
_city_rows: list[tuple[str, str, list[str], int, tuple[float, float]]] | None = None


def clear_cache() -> None:
    """Clear the geocode cache (for testing)."""
    global _city_rows
    _geocode_cache.clear()
    _negative_cache.clear()
    # _city_rows 是按 _PACK_PATH 派生的解析缓存 —— 测试会把 _PACK_PATH
    # monkeypatch 到不存在的路径, 不重置则解析结果残留, "离线源全缺 → None"
    # 的判定被前序测试污染(见 test_geocode_none_when_down_and_no_pack)。
    _city_rows = None
    # places 侧的派生缓存同理: 测试把 _DB monkeypatch 到不存在的路径,
    # 但已建立的 sqlite 连接与补丁索引仍指向真库, 离线降级路径会照常命中。
    # clear_cache 是"清空 geocode 的查找缓存", 而 places 是它的下游查找源,
    # 故在此一并重置; 否则 _DB 的替换对已缓存连接不生效。
    from nowhere import places as _places

    _places._conn_instance = None
    _places._PATCH_CACHE = None
    _places._PATCH_LOWER_CACHE = None


def _load_special() -> dict[str, dict]:
    """Load special_places.json once and cache."""
    global _special_places
    if _special_places is None:
        if _SPECIAL_PATH.exists():
            _special_places = json.loads(_SPECIAL_PATH.read_text(encoding="utf-8"))
        else:
            _special_places = {}
    return _special_places


def _load_city_rows() -> list[tuple[str, str, list[str], int, tuple[float, float]]]:
    global _city_rows
    if _city_rows is None:
        rows: list[tuple[str, str, list[str], int, tuple[float, float]]] = []
        if _PACK_PATH.exists():
            with open(_PACK_PATH, encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 15:
                        continue
                    try:
                        pop = int(parts[14] or 0)
                    except ValueError:
                        pop = 0
                    try:
                        coords = (float(parts[4]), float(parts[5]))
                    except ValueError:
                        continue  # 坐标非数值的坏行整行跳过
                    alts = [t.strip() for t in parts[3].lower().split(",") if t.strip()]
                    rows.append((parts[1].lower(), parts[2].lower(), alts, pop, coords))
        _city_rows = rows
    return _city_rows


def _offline_lookup(place: str) -> tuple[float, float] | None:
    """在 cities15000 里查地名。精确名 > 别名包含,同优先级取人口最多。"""
    q = place.strip().lower()
    if not q:
        return None

    best: tuple[float, float] | None = None
    best_score = -1.0
    for name, ascii_name, alts, pop, coords in _load_city_rows():
        score = 0.0
        if q == name or q == ascii_name:
            score = 4.0
        elif q in name or q in ascii_name:
            score = 2.0
        else:
            # 别名按 token 匹配: 整词相等 > 词内包含(防"喀什"撞上"马拉喀什")
            for token in alts:
                if token == q:
                    score = max(score, 3.0)
                    break
                if q in token:
                    score = max(score, 1.0)
        if score == 0.0:
            continue
        rank = score * 1e12 + pop
        if rank > best_score:
            best_score = rank
            best = coords
    return best


async def lookup(place: str) -> tuple[float, float] | None:
    """Return ``(lat, lon)`` for *place*, or ``None`` on failure / no result.

    链: special_places → places.db → cities15000 → Nominatim（慢，最后试）。
    """
    key = place.strip().lower()
    if len(_geocode_cache) > _GEOCODE_CACHE_MAX:
        _geocode_cache.clear()  # simple eviction: clear all when full
    if key in _geocode_cache:
        return _geocode_cache[key]

    # Special places (continents, oceans, poles, etc.)
    special = _load_special()
    sp = special.get(place) or special.get(key) or special.get(place.strip())
    # 缺字段/空 dict 条目跳过, KeyError 不该穿透给调用方
    if isinstance(sp, dict) and isinstance(sp.get("lat"), (int, float)) \
            and isinstance(sp.get("lon"), (int, float)):
        result = (float(sp["lat"]), float(sp["lon"]))
        _geocode_cache[key] = result
        return result

    # Offline sources first (fast)
    from nowhere import places

    hit = places.find(place)
    if hit is not None:
        result = (hit["lat"], hit["lon"])
        _geocode_cache[key] = result
        return result

    # 整包逐行扫描是同步 CPU/IO, 放线程外避免阻塞事件循环
    result = await asyncio.to_thread(_offline_lookup, place)
    if result is not None:
        _geocode_cache[key] = result
        return result

    if key in _negative_cache and time.monotonic() - _negative_cache[key] < _NEGATIVE_TTL:
        return None

    # Nominatim last (slow, 5s timeout)
    url = (
        "https://nominatim.openstreetmap.org/search?"
        + urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
    )
    try:
        client = _get_client()
        resp = await client.get(url, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        if data:
            result = (float(data[0]["lat"]), float(data[0]["lon"]))
            _geocode_cache[key] = result
            return result
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        # 留 debug 日志: 网络失败与解析缺陷在此之前完全不可见
        logger.debug("nominatim lookup failed for %r: %s", place, exc)

    # 失败结果只进短 TTL 负缓存, 不进正式缓存 —— 固化的 None 会把
    # 瞬时在线故障放大成永久错误
    _negative_cache[key] = time.monotonic()
    return None
