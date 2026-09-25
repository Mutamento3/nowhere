"""Art encounters -- geo-aware, mood-matched artwork from the Met Museum.

The Met has 5000+ years of art from every continent.  We bias the search
toward art that is *culturally relevant* to where the AI is standing,
then layer mood on top.  A user in Kyoto sees Japanese prints; a user
in Lagos sees West African sculpture; a user in Paris sees French painting.

Note: Art cards are dynamic (API + local DB), not static card data.
The unified Card schema in cards.py covers static card sources.
"""

from __future__ import annotations

import asyncio
import gzip
import json as _json
import logging
import pathlib
import random
from urllib.parse import quote_plus

from nowhere import providers

logger = logging.getLogger(__name__)

# ── Local art database ─────────────────────────────────────────────
_ART_DB = None
_ART_DB_PATH = pathlib.Path(__file__).resolve().parent / "data" / "art_met.json.gz"


def _load_art_db() -> dict:
    global _ART_DB
    if _ART_DB is None:
        try:
            with gzip.open(_ART_DB_PATH, "rb") as f:
                _ART_DB = _json.loads(f.read().decode("utf-8"))
        except (OSError, EOFError, UnicodeDecodeError, _json.JSONDecodeError) as exc:
            # BadGzipFile/truncation land in OSError/EOFError; leave the reasons in the logs,
            # otherwise "data missing" and "no cultural match" are indistinguishable
            logger.warning("art db load failed (%s): %s", _ART_DB_PATH, exc)
            _ART_DB = {"artworks": [], "by_culture": {}, "count": 0}
    return _ART_DB

SOURCE = "metmuseum"

# ── Geo → culture search terms ──────────────────────────────────────
# Map lat/lon bands to Met search keywords.  Crude but effective:
# the Met's search indexes culture/department/tags, so "Japanese"
# reliably surfaces Japanese art.

_GEO_CULTURE: list[tuple[float, float, float, float, str]] = [
    # (lat_min, lat_max, lon_min, lon_max, search_keyword)
    # Order matters: more specific before more general (first match wins).

    # Central Asia / Mongolia (BEFORE Chinese to avoid overlap)
    (40, 55, 87, 120, "Central Asian"),

    # Middle East -- Iran, Iraq, Arabian Peninsula, Turkey
    (15, 42, 35, 65, "Middle Eastern"),

    # East Asia -- specific first: Japan archipelago → Korean Peninsula → pan-China band.
    # Japanese narrowed to lon>=129, otherwise it swallows East China/the Korean Peninsula/northern Vietnam;
    # the Chinese band and Southeast Asian overlap in lat 18-25, with China taking priority (Hainan/Lingnan
    # are judged as Chinese; the Hanoi area is a known tradeoff of the coarse-partition band)
    (30, 46, 129, 146, "Japanese"),
    (33, 43, 124, 132, "Korean"),
    (18, 55, 73, 135, "Chinese"),

    # South Asia -- mainland + islands (Maldives, Sri Lanka, etc.)
    # BEFORE Chinese: the two bands overlap at lon 73-100, India must match first
    (-10, 38, 60, 100, "South Asian"),

    # Southeast Asia
    (-10, 25, 90, 155, "Southeast Asian"),

    # North Africa
    (15, 40, -15, 35, "North African"),

    # Sub-Saharan Africa
    (-35, 15, -20, 55, "African"),

    # Europe -- countries first, "European" only as the catch-all for the rest of Europe;
    # between Scandinavian and German, Scandinavian comes first to keep Denmark/southern Scandinavia reachable
    (50, 62, -10, 2, "British"),
    (55, 85, 5, 35, "Scandinavian"),
    (47, 60, 5, 30, "German"),
    (36, 48, 6, 18, "Italian"),
    (42, 52, -6, 10, "French"),
    (35, 60, -10, 3, "Spanish"),
    (35, 72, -15, 40, "European"),

    # Americas -- Pre-Columbian before American (the two bands overlap, specific first)
    (15, 33, -120, -85, "Pre-Columbian"),
    (10, 35, -130, -60, "American"),
    (-55, 10, -85, -35, "Latin American"),

    # Oceania
    (-50, -5, 110, 180, "Oceanic"),
]

# 地理关键词 → Met 数据库实际 culture key 映射
# 每个区至少 2 个 key；映射不出的区在 _local_art_match 中走"查无不出"。
_GEO_TO_MET_CULTURE: dict[str, list[str]] = {
    "Central Asian": ["central asian", "mongolian", "tibetan", "bactria-margiana"],
    "Middle Eastern": ["iran", "persian", "sasanian", "turkish", "islamic",
                       "assyrian", "babylonian", "parthian", "nabataean",
                       "elamite", "mitanni", "sumerian"],
    "Japanese": ["japan", "japanese"],
    "Korean": ["korea", "korean"],
    "Chinese": ["china", "chinese", "north china", "northeast china"],
    "South Asian": ["india", "indian", "nepal", "sri lankan", "pakistan", "mughal"],
    "Southeast Asian": ["thailand", "cambodia", "indonesia", "java", "javanese",
                        "bornean", "sumatra", "philippine", "myanmar", "burmese",
                        "dyak"],
    "North African": ["moroccan", "egypt", "egyptian", "islamic"],
    "African": ["african", "akan", "dogon", "edo", "kongo", "yoruba",
                "tabwa", "teke", "ghanaian", "nigerian"],
    "European": ["european", "british", "french", "german", "italian", "spanish",
                 "dutch", "netherlandish", "austrian", "swiss", "hungarian", "catalan"],
    "Spanish": ["spanish", "catalan"],
    "Italian": ["italian", "milan", "brescia", "savoy"],
    "French": ["french", "paris"],
    "German": ["german", "augsburg", "nuremberg", "dresden", "landshut",
               "strasburg", "saxony"],
    "British": ["british", "london"],
    "Scandinavian": ["scandinavian", "norwegian", "swedish", "danish", "finnish"],
    "American": ["american", "colonial american", "alaska", "tlingit", "inuit", "yupik"],
    "Latin American": ["mexican", "peruvian", "colombian", "ecuador", "costa rica",
                       "nicaragua", "aztec", "maya", "inca", "moche", "nasca",
                       "chimú", "paracas"],
    "Pre-Columbian": ["maya", "aztec", "mexica", "olmec", "inca", "moche", "nasca",
                      "chimú", "paracas", "toltec", "mezcala"],
    "Oceanic": ["maori", "kanak", "lapita", "polynesian", "melanesian",
                "micronesian", "papua", "bornean", "balinese"],
}


def _culture_key_match(mk: str, ck: str) -> bool:
    """映射键 mk → DB culture 键 ck 的锚定匹配(两侧均应为 lower)。

    不用双向子串: "african"⊂"north african"、"american"⊂"latin american"
    会把跨区作品误配进候选。DB 键的合法形态是 mk 加限定成分
    ("german, augsburg"/"akan peoples"/"central asian or russian")、
    or 复合主文化("tibetan or mongolian")或括号别名("mexica (aztec)")。
    """
    if mk == ck:
        return True
    # mk 后紧跟限定成分
    if ck.startswith(mk + " ") or ck.startswith(mk + ",") or ck.startswith(mk + ";"):
        return True
    # or 复合主文化: 任一段等值即命中
    if any(seg.strip().rstrip("(?)").strip() == mk for seg in ck.split(" or ")):
        return True
    # 括号别名
    _head, lp, tail = ck.partition("(")
    if lp and tail.rstrip(")").rstrip(" ?").strip() == mk:
        return True
    return False


def _geo_culture(lat: float, lon: float) -> str | None:
    """Return a Met search keyword for the region, or None."""
    for lat_min, lat_max, lon_min, lon_max, kw in _GEO_CULTURE:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return kw
    return None


# ── Mood search terms ───────────────────────────────────────────────

_MOOD_SEARCH: dict[str, str] = {
    "rain": "rain",
    "night": "night",
    "snow": "snow winter",
    "dawn": "dawn sunrise morning",
    "storm": "storm tempest",
    "calm": "calm peace serene",
    "sun": "sun sunshine",
    "fog": "fog mist",
    "wind": "wind",
}

_MOOD_WHY: dict[str, str] = {
    "rain": "雨天看这幅画，刚好",
    "night": "夜色正浓，这件作品映着此刻",
    "snow": "雪天的氛围，和这幅画相通",
    "dawn": "黎明时分遇见它，像是巧合",
    "storm": "风暴将至，这幅画也躁动",
    "calm": "此刻平静，这件作品也安静",
    "sun": "阳光下的遇见",
    "fog": "雾气里看到这幅画，别有意境",
    "wind": "风里带着和这幅画一样的气息",
}


def _mood_why(mood: str, *, culture_matched: bool = False) -> str:
    """Return a truthful 'why' string, or empty if nothing applies.

    * ``culture_matched=True``  → mood text or "此刻应景" (culture is genuine)
    * ``culture_matched=False`` → mood text only; unknown mood → ""
    """
    known = _MOOD_WHY.get(mood)
    if known:
        return known
    if culture_matched:
        return "此刻应景"
    return ""


# ── Local art match ─────────────────────────────────────────────────


def _local_art_match(lat: float, lon: float, mood: str, rng: random.Random) -> dict | None:
    """Find artwork from local database matching geo region.

    Returns *None* when culture cannot be verified -- silent miss is better
    than showing a mismatched piece (the "lying" scenario of card 61).
    """
    db = _load_art_db()
    artworks = db.get("artworks", [])
    if not artworks:
        return None

    # ── Step 1: resolve geo → culture keyword ────────────────────────
    culture = _geo_culture(lat, lon)
    if not culture:
        logger.info("art_geo: no geo region for (%s, %s)", lat, lon)
        return None

    # ── Step 2: map to Met database culture keys ─────────────────────
    met_keys = _GEO_TO_MET_CULTURE.get(culture, [])
    if not met_keys:
        logger.info("art_geo: region=%s has no met_keys mapping", culture)
        return None

    by_culture = db.get("by_culture", {})
    matching_indices: list[int] = []
    for mk in met_keys:
        mk_lower = mk.lower()
        for ck, idxs in by_culture.items():
            if _culture_key_match(mk_lower, ck.lower()):
                matching_indices.extend(idxs)
    # 同一索引会被多个 key 命中(如 china+chinese), 去重避免抽样过采样
    candidates = [
        artworks[i] for i in dict.fromkeys(matching_indices) if i < len(artworks)
    ]

    if not candidates:
        logger.info("art_geo: region=%s met_keys=%s → 0 candidates", culture, met_keys)
        return None

    # ── Step 3: pick from the culture-matched pool ───────────────────
    pool = candidates[:50]
    rng.shuffle(pool)
    # 遍历整个池: 前 5 条恰好都缺图缺标题时不应漏配 (候选都在内存)
    for art in pool:
        if art.get("image") and art.get("title"):
            return {
                "title": art["title"],
                "artist": art.get("artist", "佚名"),
                "artist_bio": art.get("bio", ""),
                "year": art.get("year", ""),
                "image_url": art.get("image", ""),
                "culture": art.get("culture", ""),
                "medium": art.get("class", ""),
                "classification": art.get("class", ""),
                "department": art.get("dept", ""),
                "tags": [],
                "zim_extract": None,  # will be filled by caller
                "why": _mood_why(mood, culture_matched=True),
            }
    return None


# ── Main match function ─────────────────────────────────────────────

async def match(lat: float, lon: float, mood: str, rng: random.Random | None = None) -> dict | None:
    """Return a geo-aware, mood-matched artwork, or *None*.

    Strategy:
      1. Try local database (culture-verified only, no global pool)
      2. Fall back to Met API (culture + mood, then mood only)
      3. No match → None  (silent miss beats a lie)
    """
    if rng is None:
        rng = random.Random()
    if not mood or mood.lower() in ("none", ""):
        mood = "calm"

    # ── 1. Try local database first ─────────────────────────────────
    # 首次 gzip 解压 + 全量 JSON 解析放到线程外, 避免阻塞事件循环
    await asyncio.to_thread(_load_art_db)
    result = _local_art_match(lat, lon, mood, rng)
    if result:
        return result

    # ── 2. Fall back to Met API ─────────────────────────────────────
    mood_term = _MOOD_SEARCH.get(mood, mood)
    culture = _geo_culture(lat, lon)

    # Try culture + mood (API result culture is unverifiable → mood-only why)
    if culture:
        search_term = f"{culture} {mood_term}"
        api_result = await _search_and_pick(search_term, mood, rng)
        if api_result:
            return api_result

    # Fall back to mood only
    api_result = await _search_and_pick(mood_term, mood, rng)
    if api_result:
        return api_result

    # No last-resort generic search -- if nothing matches, stay silent.
    return None


async def _search_and_pick(search_term: str, mood: str, rng: random.Random) -> dict | None:
    """Search Met API and return a random artwork, or None.

    API results cannot be culture-verified, so ``why`` is always
    mood-only text -- never "此刻应景".
    """
    # Resolve mood-only why; if mood unknown, no card.
    why = _mood_why(mood, culture_matched=False)
    if not why:
        return None

    search_url = (
        "https://collectionapi.metmuseum.org/public/collection/v1/search"
        f"?hasImages=true&q={quote_plus(search_term)}"
    )
    search_data = await providers.fetch_json(
        search_url, source=SOURCE, cache_ttl=600, timeout=5.0,
    )
    if not search_data or not search_data.get("objectIDs"):
        return None

    # Pick from top 20 for variety
    object_ids: list[int] = search_data["objectIDs"][:20]
    # Shuffle and try all of them (some entries have no image) -- a fixed
    # first-5 cap would false-negative when those 5 all lack images
    rng.shuffle(object_ids)
    for oid in object_ids:
        obj_url = (
            "https://collectionapi.metmuseum.org/public/collection/v1"
            f"/objects/{oid}"
        )
        obj_data = await providers.fetch_json(
            obj_url, source=SOURCE, cache_ttl=600, timeout=5.0,
        )
        if not obj_data:
            continue
        image_url = obj_data.get("primaryImage") or ""
        title = obj_data.get("title", "")
        if not image_url or not title or title.lower() == "none":
            continue

        tags_raw = obj_data.get("tags") or []
        tags = [
            t.get("name", "")
            for t in tags_raw
            if isinstance(t, dict) and t.get("name")
        ]

        return {
            "title": title,
            "artist": obj_data.get("artistDisplayName", "") or "佚名",
            "artist_bio": obj_data.get("artistDisplayBio", ""),
            "year": obj_data.get("objectDate", ""),
            "image_url": image_url,
            "culture": obj_data.get("culture", ""),
            "medium": obj_data.get("medium", ""),
            "classification": obj_data.get("classification", ""),
            "department": obj_data.get("department", ""),
            "tags": tags[:5],
            "zim_extract": None,
            "why": why,
        }
    return None
