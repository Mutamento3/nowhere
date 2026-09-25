"""Hydrology module -- find nearby water features via OSM Overpass API.

Returns literary descriptions of rivers, lakes, streams, waterfalls,
and reservoirs near a given coordinate.
"""

from __future__ import annotations

import math
import pathlib
import random
from typing import Any

from nowhere import providers

# ── Constants ───────────────────────────────────────────────────────
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_SCENE_DIR = pathlib.Path(__file__).resolve().parent / "data"
_SCENE_FILE = "water_features"

# Bearing labels (8 directions)
_BEARINGS: list[str] = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]

# Map OSM tag values to our type names
_WATERWAY_TO_TYPE: dict[str, str] = {
    "river": "river",
    "stream": "stream",
    "canal": "stream",
    "waterfall": "waterfall",
}
_WATER_TO_TYPE: dict[str, str] = {
    "river": "river",
    "lake": "lake",
    "reservoir": "reservoir",
    "pond": "lake",
    "stream": "stream",
}


from nowhere.terrain import haversine_km as _haversine_km


def _bearing_label(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Return compass bearing label (N/NE/E/SE/S/SW/W/NW) from point 1 to 2."""
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
    idx = round(bearing / 45) % 8
    return _BEARINGS[idx]


def _classify_element(tags: dict[str, str]) -> str | None:
    """Classify an OSM element into our water type, or None to skip."""
    # Waterways take priority
    ww = tags.get("waterway")
    if ww:
        return _WATERWAY_TO_TYPE.get(ww)
    # natural=water
    if tags.get("natural") == "water":
        water = tags.get("water", "")
        return _WATER_TO_TYPE.get(water, "lake")
    return None


def _element_center(el: dict[str, Any]) -> tuple[float, float] | None:
    """Extract (lat, lon) from an Overpass element."""
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    center = el.get("center")
    if center and "lat" in center and "lon" in center:
        return center["lat"], center["lon"]
    return None


def offline_water_nearby(lat: float, lon: float, radius_km: float = 50) -> list[dict]:
    """Look up water features from offline JSON within *radius_km* of (lat, lon).

    Returns a list of dicts sorted by distance:
        {"name": str, "type": str, "distance_km": float, "bearing": str,
         "note": str | None}

    Each entry's radius_km is checked: only entries whose center is within
    (entry_radius + radius_km) of the query point are returned.
    """
    import json as _json

    fp = _SCENE_DIR / "water_features_offline.json"
    if not fp.exists():
        return []
    try:
        data = _json.loads(fp.read_text(encoding="utf-8"))
        entries = data.get("entries", []) if isinstance(data, dict) else []
    except Exception:
        # 静默吞掉会掩盖数据损坏, 留一条告警便于排查
        import logging
        logging.getLogger(__name__).warning(
            "water_features_offline.json 读取失败, 离线水文不可用", exc_info=True,
        )
        return []

    results: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        elat = entry.get("lat")
        elon = entry.get("lon")
        if elat is None or elon is None:
            # 缺坐标的条目不能以 (0,0) 参与距离/方位计算 → 跳过
            continue
        entry_radius = entry.get("radius_km", 50)
        dist = _haversine_km(lat, lon, elat, elon)
        # Entry reachable if distance < entry_radius + query_radius
        if dist > entry_radius + radius_km:
            continue
        bearing = _bearing_label(lat, lon, elat, elon)
        name = entry.get("name", "")
        note = entry.get("note")
        label = f"{name} {note}" if note else name
        results.append({
            "name": name,
            "type": entry.get("type", "river"),
            "distance_km": round(dist, 1),
            "bearing": bearing,
            "note": note,
            "label": label,
            # 与 nearby_water(在线) 的 schema 对齐: 下游键访问不随网络状态漂移
            "detail": "",
        })

    results.sort(key=lambda r: r["distance_km"])
    # Deduplicate by name+type+note (closest wins)
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in results:
        key = f"{r['name']}|{r['type']}|{r['note'] or ''}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


async def nearby_water(lat: float, lon: float, radius_km: float = 10) -> list[dict]:
    """Query OSM Overpass for water features within *radius_km*.

    Returns a list of dicts:
        {"name": str, "type": "river"|"lake"|"stream"|"waterfall"|"reservoir",
         "distance_km": float, "bearing": str, "detail": str}

    Empty list means no water nearby.
    """
    radius_m = int(radius_km * 1000)
    query = (
        f'[out:json][timeout:10];'
        f'('
        f'  node["natural"="water"](around:{radius_m},{lat},{lon});'
        f'  way["natural"="water"](around:{radius_m},{lat},{lon});'
        f'  node["waterway"="waterfall"](around:{radius_m},{lat},{lon});'
        f'  way["waterway"="river"]["name"](around:{radius_m},{lat},{lon});'
        f'  relation["waterway"="river"]["name"](around:{radius_m},{lat},{lon});'
        f'  way["waterway"~"^(stream|canal)$"]["name"](around:{radius_m},{lat},{lon});'
        f');'
        f'out center tags;'
    )
    # 查询串经 params 提交, 不拼进 URL 字符串(会进入异常文本与缓存键)
    data = await providers.fetch_json(
        _OVERPASS_URL, source="overpass", cache_ttl=3600, timeout=15.0,
        params={"data": query},
    )
    if data is None:
        return []

    elements = data.get("elements", [])
    results: list[dict] = []

    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name", "").strip()
        wtype = _classify_element(tags)
        if wtype is None:
            continue
        center = _element_center(el)
        if center is None:
            continue

        elat, elon = center
        dist = _haversine_km(lat, lon, elat, elon)
        bearing = _bearing_label(lat, lon, elat, elon)

        # Build a detail string from useful OSM tags
        detail_parts: list[str] = []
        if tags.get("width"):
            detail_parts.append(f"宽{tags['width']}米")
        if tags.get("depth"):
            detail_parts.append(f"深{tags['depth']}米")
        detail = ",".join(detail_parts)

        results.append({
            "name": name or "无名水域",
            "type": wtype,
            "distance_km": round(dist, 1),
            "bearing": bearing,
            "detail": detail,
            # 与 offline_water_nearby schema 对齐
            "note": None,
            "label": name or "无名水域",
        })

    # Sort by distance first, then deduplicate (closest wins)
    results.sort(key=lambda r: r["distance_km"])
    seen_names: set[str] = set()
    deduped: list[dict] = []
    for r in results:
        dedup_key = f"{r['name']}|{r['type']}"
        if r["name"] != "无名水域" and dedup_key in seen_names:
            continue
        seen_names.add(dedup_key)
        deduped.append(r)
    results = deduped
    return results


def describe_water(features: list[dict], rng: random.Random, biome: str = "") -> str:
    """Pick the most interesting water feature and render a literary description.

    Returns "" if no features. Card 33: reads biome-specific product file
    (scene_water_{biome}.txt). Build time already filtered — runtime zero filtering.
    """
    if not features:
        return ""

    # Pick the most interesting: waterfall > river > lake > stream > reservoir
    priority = {"waterfall": 0, "river": 1, "lake": 2, "stream": 3, "reservoir": 4}
    ranked = sorted(features, key=lambda f: (priority.get(f["type"], 9), f["distance_km"]))
    feature = ranked[0]

    # Card 33: read biome-specific product file directly
    from nowhere import describe
    if biome:
        lines = describe._load_scenes(f"water_{biome}")
    else:
        lines = describe._load_scenes(_SCENE_FILE)  # fallback to legacy

    if lines:
        text = rng.choice(lines)
    else:
        text = f"{feature['bearing']}边有水。"

    return text
