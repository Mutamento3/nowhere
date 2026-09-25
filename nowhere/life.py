"""Life encounters -- nearby wildlife via iNaturalist."""

from __future__ import annotations

import math
import random
import re
from urllib.parse import urlencode

from nowhere import providers

SOURCE = "inaturalist"

_NOCTURNAL_KEYWORDS = ("owl", "bat", "moth", "nightjar", "opossum", "raccoon", "firefly")
_AMPHIBIAN_KEYWORDS = ("frog", "toad", "salamander", "newt", "caecilian")


def _kw_matches(kw: str, name_lower: str) -> bool:
    """拉丁学名/俗名用词边界匹配, 中文关键词保持子串。

    纯子串会让 "bat" 命中 "batrachostomus"、"crow" 命中 "crowberry"。
    """
    if kw.isascii():
        return re.search(rf"\b{re.escape(kw)}\b", name_lower) is not None
    return kw in name_lower


# ── Seasonal keywords: boost animals that are seasonally appropriate ──
# 匹配目标是学名+俗名, 行为/物候词(breeding/tracks/frost/active/bloom/
# dormant 等)永远不会出现在名字里, 不收入表
_SEASONAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "spring": (
        "chick", "fawn",
        "larva", "caterpillar", "butterfly", "tadpole", "gosling",
        "幼崽", "蝌蚪", "蝴蝶",
    ),
    "summer": (
        "insect", "reptile", "bat", "moth", "frog", "cicada",
        "dragonfly", "lizard", "snake", "firefly", "cricket", "grasshopper",
        "昆虫", "蜥蜴", "蛇", "蝉", "蜻蜓", "蟋蟀", "萤火虫",
    ),
    "autumn": (
        "harvest", "mushroom", "berry", "hawk", "squirrel",
        "deer", "geese", "crane", "fungus",
        "蘑菇", "浆果", "鹰", "松鼠", "鹿", "大雁",
    ),
    "winter": (
        "hibernate", "owl", "fox", "hare",
        "wolf", "crow", "magpie", "evergreen", "conifer",
        "冬眠", "猫头鹰", "狐狸", "野兔", "狼", "乌鸦", "喜鹊",
    ),
}

# ── Biome + season life matrix ─────────────────────────────────────
# ── Biome ↔ animal filtering ──────────────────────────────────────
# Keywords that indicate an animal is alpine/temperate (should NOT appear in tropical/coast)
_ALPINE_KEYWORDS = (
    "marmot", "土拨鼠", "yak", "牦牛", "snow leopard", "雪豹",
    "pika", "鼠兔", "ibex", "山羊", "chamois", "岩羚羊",
    "mountain goat", "alpine", "ptarmigan", "雷鸟",
    "wolverine", "狼獾", "ermine", "白鼬",
)
# Keywords that indicate an animal is tropical (should NOT appear in alpine/tundra)
_TROPICAL_KEYWORDS = (
    "parrot", "鹦鹉", "toucan", "巨嘴鸟", "monkey", "猴",
    "gorilla", "大猩猩", "chimpanzee", "黑猩猩", "orangutan", "猩猩",
    "jaguar", "美洲豹", "piranha", "食人鱼", "sloth", "树懒",
    "macaw", "金刚鹦鹉", "cobra", "眼镜蛇", "gecko", "壁虎",
    "iguana", "鬣蜥", "mango", "芒果", "hummingbird", "蜂鸟",
    "flamingo", "火烈鸟",
)
# Biome categories for filtering
_TROPICAL_BIOMES = frozenset({"coast", "rainforest", "island"})
_ALPINE_BIOMES = frozenset({"mountain", "tundra", "volcano"})


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    p = math.pi / 180
    a = (
        0.5
        - math.cos((lat2 - lat1) * p) / 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    )
    a = min(a, 1.0)
    return 2 * R * math.asin(math.sqrt(a))


def _month_to_season(month: int | None) -> str:
    """Map month number (1-12) to season name. 非法值返回空季节。"""
    if month is None or not isinstance(month, int) or not 1 <= month <= 12:
        return ""
    return ["winter", "winter", "spring", "spring", "spring", "summer",
            "summer", "summer", "autumn", "autumn", "autumn", "winter"][month - 1]


async def nearby(
    lat: float,
    lon: float,
    *,
    night: bool,
    weather_text: str,
    radius_km: int = 10,
    biome: str | None = None,
    rng: random.Random | None = None,
    month: int | None = None,
) -> dict | None:
    """Return a nearby wildlife observation, or *None* if nothing found / API down.

    Parameters
    ----------
    night:
        If *True*, prefer nocturnal species.
    weather_text:
        Free-form weather string; if it contains rain-related characters,
        prefer amphibians.
    radius_km:
        Search radius;城市给小,荒野给大。
    biome:
        Current biome name (e.g. "mountain", "coast", "rainforest").
        Used to filter out biome-inappropriate animals.
    month:
        Month (1-12) for seasonal filtering. Animals that are seasonally
        appropriate get a score boost.
    """
    params = urlencode(
        {
            "lat": lat,
            "lng": lon,
            "radius": radius_km,
            "per_page": 20,
            "order": "desc",
            "order_by": "observed_on",
            "locale": "zh-CN",  # 俗名要中文的
            "captive": "false",  # Card 67: filter aquarium/seafood-market records
        }
    )
    url = f"https://api.inaturalist.org/v1/observations?{params}"
    data = await providers.fetch_json(url, source=SOURCE, cache_ttl=300, timeout=5.0)
    if not data or not data.get("results"):
        return None

    results: list[dict] = data["results"]

    # Build scored list: (score, observation)
    rain = any(ch in weather_text for ch in ("雨", "rain", "雷", "storm"))
    season = _month_to_season(month)
    scored: list[tuple[float, dict]] = []
    for obs in results:
        taxon = obs.get("taxon") or {}
        name_lower = ((taxon.get("name") or "") + " " + (taxon.get("preferred_common_name") or "")).lower()
        score = 0.0
        if night:
            for kw in _NOCTURNAL_KEYWORDS:
                if _kw_matches(kw, name_lower):
                    score += 2.0
                    break
        if rain:
            for kw in _AMPHIBIAN_KEYWORDS:
                if _kw_matches(kw, name_lower):
                    score += 1.5
                    break
        # Seasonal boost: prefer animals that are seasonally active
        if season and season in _SEASONAL_KEYWORDS:
            for kw in _SEASONAL_KEYWORDS[season]:
                if _kw_matches(kw, name_lower):
                    score += 1.0
                    break
        score += (rng or random).random()  # jitter
        scored.append((score, obs))

    scored.sort(key=lambda t: t[0], reverse=True)

    # Filter out biome-inappropriate animals
    biome_filtered = True
    if biome:
        is_tropical = biome in _TROPICAL_BIOMES
        is_alpine = biome in _ALPINE_BIOMES
        if is_tropical or is_alpine:
            filtered: list[tuple[float, dict]] = []
            for score, obs in scored:
                taxon = obs.get("taxon") or {}
                name_lower = ((taxon.get("name") or "") + " " + (taxon.get("preferred_common_name") or "")).lower()
                skip = False
                if is_tropical and any(_kw_matches(kw, name_lower) for kw in _ALPINE_KEYWORDS):
                    skip = True
                if is_alpine and any(_kw_matches(kw, name_lower) for kw in _TROPICAL_KEYWORDS):
                    skip = True
                if not skip:
                    filtered.append((score, obs))
            if filtered:
                scored = filtered
            else:
                # 全被过滤时刻意兜底回未过滤列表, 但显式告知调用方未过滤
                biome_filtered = False

    best = scored[0][1]

    taxon = best.get("taxon") or {}
    geo = best.get("geojson") or {}
    coords = (geo.get("coordinates") or [None, None])
    obs_lon, obs_lat = coords[0], coords[1]

    dist_m: float | None = None
    if obs_lat is not None and obs_lon is not None:
        dist_m = round(_haversine_m(lat, lon, obs_lat, obs_lon))

    # Extract photo URL
    photos = best.get("photos") or best.get("observation_photos") or []
    photo_url = ""
    if photos:
        photo_url = photos[0].get("url", "")
        # iNaturalist returns sizes; prefer medium
        if photo_url:
            photo_url = photo_url.replace("square", "medium")

    iconic = (taxon.get("iconic_taxon_name") or "").lower()
    unit = "一棵" if iconic in ("plantae", "fungi") else "一只"

    return {
        "name": taxon.get("name", ""),
        "common_name": taxon.get("preferred_common_name", ""),
        "seen_at": best.get("observed_on", ""),
        "distance_m": dist_m,
        "photo_url": photo_url,
        "unit": unit,
        "season": season,
        "biome": biome or "",
        "biome_filtered": biome_filtered,
    }
