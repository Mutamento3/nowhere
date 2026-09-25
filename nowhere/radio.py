"""Find nearby radio stations via Radio-Browser API with fallback to a local JSON list."""

from __future__ import annotations

import json
import math
import pathlib
import random
from typing import Final

import httpx

from nowhere import country

# ── Constants ───────────────────────────────────────────────────────

_MIRRORS: Final[list[str]] = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]

_DATA_DIR: Final = pathlib.Path(__file__).resolve().parent / "data"
_FALLBACK_PATH: Final = _DATA_DIR / "radio_fallback.json"

_EARTH_RADIUS_KM: Final = 6371.0

# 兜底清单是静态资源, 模块级缓存一次(每次请求重复读盘会阻塞事件循环)
_fallback_cache: list[dict] | None = None


# ── Helpers ─────────────────────────────────────────────────────────

from nowhere.terrain import haversine_km as _haversine_km


def _load_fallback() -> list[dict]:
    global _fallback_cache
    if _fallback_cache is None:
        try:
            with open(_FALLBACK_PATH, encoding="utf-8") as f:
                _fallback_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            _fallback_cache = []  # intentionally ignored: fallback data missing
    return _fallback_cache


def _pick_nearest_from_fallback(lat: float, lon: float, country_code: str | None = None) -> dict | None:
    """Pick the fallback station closest to (lat, lon) by haversine distance.

    With a known *country_code*, only same-country stations are eligible
    (Card 71: Budapest ≠ CZ) and the nearest one wins outright.  Without one,
    the globally nearest station is taken if it is within 3000 km, else the
    nearest among the closest region's representative countries.

    All fallback entries have ``lat``/``lon`` fields.  Returns *None* only if
    no eligible station is available.
    """
    _MAX_NEARBY_KM: Final = 3000.0

    # Regional representative stations (picked from fallback list by country)
    _REGION_REPS: dict[str, list[str]] = {
        "asia":      ["KR", "JP", "CN", "VN", "TH", "ID", "IN", "KG"],
        "europe":    ["GB", "FR", "DE", "NO", "IS", "CZ"],
        "americas":  ["US", "CA", "BR", "AR", "PE"],
        "africa":    ["KE", "TZ", "ZA", "CM"],
        "oceania":   ["AU", "NZ", "FJ"],
        "mideast":   ["JO", "AE"],
    }

    stations = _load_fallback()
    if not stations:
        return None

    # Index stations by country code for regional lookup
    _by_cc: dict[str, list[dict]] = {}
    for st in stations:
        cc = st.get("country", "")
        if cc:
            _by_cc.setdefault(cc, []).append(st)

    def _find_nearest(cc_list: list[str]) -> dict | None:
        """Find nearest station from a list of country codes."""
        best_st: dict | None = None
        best_d = math.inf
        for cc in cc_list:
            for st in _by_cc.get(cc, []):
                st_lat = st.get("lat")
                st_lon = st.get("lon")
                if st_lat is None or st_lon is None:
                    continue
                d = _haversine_km(lat, lon, st_lat, st_lon)
                if d < best_d:
                    best_d = d
                    best_st = st
        return best_st

    # 1. Same country first
    if country_code and country_code in _by_cc:
        same_country = _find_nearest([country_code])
        if same_country is not None:
            return same_country

    # 2. Find globally nearest station (Card 71: cc mismatch rejection)
    best: dict | None = None
    best_dist = math.inf
    for st in stations:
        st_lat = st.get("lat")
        st_lon = st.get("lon")
        if st_lat is None or st_lon is None:
            continue
        if country_code and st.get("country", "") != country_code:
            continue
        d = _haversine_km(lat, lon, st_lat, st_lon)
        if d < best_dist:
            best_dist = d
            best = st

    # 4. Find nearest region by computing distance to each region's centroid
    _REGION_CENTROIDS: dict[str, tuple[float, float]] = {
        "asia":     (30.0, 105.0),
        "europe":   (50.0, 10.0),
        "americas": (15.0, -80.0),
        "africa":   (5.0, 25.0),
        "oceania":  (-25.0, 160.0),
        "mideast":  (30.0, 45.0),
    }

    def _region_best() -> dict | None:
        nearest_region = None
        region_dist = math.inf
        for rname, (rlat, rlon) in _REGION_CENTROIDS.items():
            d = _haversine_km(lat, lon, rlat, rlon)
            if d < region_dist:
                region_dist = d
                nearest_region = rname
        if nearest_region is None:
            return None
        rep_ccs = _REGION_REPS.get(nearest_region, [])
        return _find_nearest(rep_ccs)

    if best is None:
        # 同国无台(国界附近/清单缺国) → 返回 None 落到在线 API。
        # 不做跨 cc 兜底: Card 71 B2 明确 cc 不匹配即拒绝(利比亚不能拿
        # 希腊台, 布达佩斯不能拿捷克站), 宁可离线安静也不配错台
        return None

    # If within 3000 km, return directly
    if best_dist <= _MAX_NEARBY_KM:
        return best

    # 5. From that region, pick the nearest station to the user (复用 _find_nearest)
    region_best = _region_best()

    return region_best or best


# ── Public API ──────────────────────────────────────────────────────

async def nearest(lat: float, lon: float, country_code: str | None, rng: random.Random | None = None) -> dict | None:
    """Return a station dict ``{name, genre, stream_url, homepage}`` or ``None``.

    离线优先：先查本地兜底清单，再试外网 API。
    """
    # ── 1. Offline fallback first (instant) ─────────────────────────
    if country_code is None:
        country_code = country.country_code_of(lat, lon)
    fallback = _pick_nearest_from_fallback(lat, lon, country_code=country_code)
    if fallback is not None:
        return fallback

    mirrors = list(_MIRRORS)
    (rng or random).shuffle(mirrors)

    _EXCLUDED_TAGS = {"game", "gaming", "gamemusic", "video game", "esports"}

    def _is_excluded(station: dict) -> bool:
        tags = (station.get("tags") or "").lower()
        return any(excl in tags for excl in _EXCLUDED_TAGS)

    if country_code:
        async with httpx.AsyncClient(timeout=8.0) as client:
            for base in mirrors:
                try:
                    url = (
                        f"{base}/json/stations/search"
                        f"?countrycode={country_code}&limit=50"
                        f"&order=clickcount&has_geo_info=true"
                    )
                    resp = await client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
                    if data:
                        # Filter out gaming/non-music stations
                        data = [st for st in data if not _is_excluded(st)]
                        if not data:
                            continue  # try next mirror or fallback
                        # 有坐标的台里挑地理最近的,都不带坐标就用最热的一个
                        best = None
                        best_d = math.inf
                        for st in data:
                            glat, glon = st.get("geo_lat"), st.get("geo_long")
                            if glat is None or glon is None:
                                continue
                            d = _haversine_km(lat, lon, glat, glon)
                            if d < best_d:
                                best_d = d
                                best = st
                        st = best or data[0]
                        name = st.get("name") or "Unknown"
                        return {
                            "name": name,
                            "genre": st.get("tags", ""),
                            "stream_url": st.get("url_resolved", st.get("url", "")),
                            "homepage": st.get("homepage", ""),
                        }
                except (httpx.HTTPError, httpx.TimeoutException, ValueError):
                    continue  # intentionally ignored: per-station network failure, try next

    # ── Fallback ─────────────────────────────────────────────────────
    return _pick_nearest_from_fallback(lat, lon, country_code=country_code)
