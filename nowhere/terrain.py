"""Offline elevation, surface type, and slope — powered by a 1° global grid.

The module tries to load ``nowhere/data/grid.npz`` (0.1°, 13 MB, not in git)
at startup, falling back to ``nowhere/data/grid_tiny.npz`` (1°, in git).
No network requests are made unless you explicitly call the download helper.
"""

from __future__ import annotations

import json as _json
import math
import pathlib
import threading
from typing import Final

import numpy as np

# ── Constants ───────────────────────────────────────────────────────

_DATA_DIR: Final = pathlib.Path(__file__).resolve().parent / "data"
_TINY_PATH: Final = _DATA_DIR / "grid_tiny.npz"
_FULL_PATH: Final = _DATA_DIR / "grid.npz"

# Cover codes (must match build_grid.py)
_SURFACE_MAP: Final[list[str]] = [
    "water_ocean",   # 0
    "water_fresh",   # 1
    "rock",          # 2
    "sand",          # 3
    "snow",          # 4
    "ice",           # 5
    "forest",        # 6
    "grass",         # 7
    "urban",         # 8
    "bare",          # 9
    "wetland",       # 10
]

_EARTH_RADIUS_KM: Final = 6371.0

# ── Grid loading ────────────────────────────────────────────────────

# CON-03: module-level caches are hit from asyncio.to_thread workers.
_CACHE_LOCK = threading.Lock()

_elev: np.ndarray | None = None
_elev_f32: np.ndarray | None = None
_cover: np.ndarray | None = None

# ── Tile cache (high-res elevation tiles, lazy-loaded, LRU) ────────

_TILES_DIR: Final = _DATA_DIR / "tiles"
_TILE_INDEX_PATH: Final = _TILES_DIR / "index.json"
_TILE_INDEX: dict[str, dict] | None = None  # key -> {fname, lat_min, lat_max, lon_min, lon_max}
_TILE_CACHE: dict[str, dict] = {}  # fname -> {elev, surface, lat_min, lat_max, lon_min, lon_max, shape}
_TILE_CACHE_MAX: Final = 4


def _load_tile_index() -> dict[str, dict]:
    """Load and cache the tile index. Returns empty dict if unavailable.

    Pre-loads bounds from each tile's metadata so _find_tile can locate the
    correct tile without loading every elev array into memory.
    """
    global _TILE_INDEX
    with _CACHE_LOCK:
        if _TILE_INDEX is not None:
            return _TILE_INDEX
        _TILE_INDEX = {}
        if not _TILE_INDEX_PATH.exists():
            return _TILE_INDEX
        raw = _json.loads(_TILE_INDEX_PATH.read_text(encoding="utf-8"))
        for key, val in raw.items():
            if isinstance(val, dict):
                # New format: {"file": "tile_...", "lat_min": ..., "lat_max": ..., ...}
                entry: dict = {
                    "fname": val["file"],
                    "lat_min": float(val["lat_min"]),
                    "lat_max": float(val["lat_max"]),
                    "lon_min": float(val["lon_min"]),
                    "lon_max": float(val["lon_max"]),
                }
            else:
                # Legacy format: plain filename string
                fname: str = val
                entry = {"fname": fname}
                try:
                    with np.load(_TILES_DIR / fname) as data:
                        entry["lat_min"] = float(data["lat_min"])
                        entry["lat_max"] = float(data["lat_max"])
                        entry["lon_min"] = float(data["lon_min"])
                        entry["lon_max"] = float(data["lon_max"])
                except Exception:
                    continue  # intentionally ignored: per-tile tolerance, skip corrupt/missing tiles
            _TILE_INDEX[key] = entry
        return _TILE_INDEX


def _load_tile(fname: str) -> dict | None:
    """Load a tile .npz into cache. Returns the tile dict."""
    with _CACHE_LOCK:
        if fname in _TILE_CACHE:
            # Move to end (most recently used)
            _TILE_CACHE[fname] = _TILE_CACHE.pop(fname)
            return _TILE_CACHE[fname]

        path = _TILES_DIR / fname
        try:
            with np.load(path) as data:
                tile = {
                    "elev": data["elev"],
                    "surface": data["surface"],
                    "lat_min": float(data["lat_min"]),
                    "lat_max": float(data["lat_max"]),
                    "lon_min": float(data["lon_min"]),
                    "lon_max": float(data["lon_max"]),
                    "shape": tuple(int(x) for x in data["elev"].shape),
                }
        except Exception:
            return None  # intentionally ignored: tile load failure, caller handles None

        # Evict oldest if cache is full
        while len(_TILE_CACHE) >= _TILE_CACHE_MAX:
            _TILE_CACHE.pop(next(iter(_TILE_CACHE)))

        _TILE_CACHE[fname] = tile
        return tile


def _find_tile(lat: float, lon: float) -> dict | None:
    """Find and load the tile covering (lat, lon). Returns None if no tile."""
    index = _load_tile_index()
    if not index:
        return None

    # First pass: check bounds from the index (no tile load needed)
    for key, info in index.items():
        if (info["lat_min"] <= lat <= info["lat_max"] and
                info["lon_min"] <= lon <= info["lon_max"]):
            return _load_tile(info["fname"])

    return None


def _tile_bilinear(tile: dict, lat: float, lon: float) -> tuple[float, str]:
    """Bilinear interpolation on a tile. Returns (elevation, surface)."""
    elev_arr = tile["elev"]
    surf_arr = tile["surface"]
    lat_min = tile["lat_min"]
    lat_max = tile["lat_max"]
    lon_min = tile["lon_min"]
    lon_max = tile["lon_max"]
    nrows, ncols = tile["shape"]
    if nrows <= 1 or ncols <= 1:
        return (float(elev_arr[0, 0]), _SURFACE_MAP[int(surf_arr[0, 0])])

    # Map lat/lon to fractional indices
    # Note: lat increases downward in the array (lat_min = row 0)
    fr = (lat - lat_min) / (lat_max - lat_min) * (nrows - 1)
    fc = (lon - lon_min) / (lon_max - lon_min) * (ncols - 1)

    r0 = int(math.floor(fr))
    c0 = int(math.floor(fc))
    r1 = min(r0 + 1, nrows - 1)
    c1 = min(c0 + 1, ncols - 1)
    r0 = max(0, r0)
    c0 = max(0, c0)

    dr = fr - math.floor(fr)
    dc = fc - math.floor(fc)

    # Bilinear for elevation
    e00 = float(elev_arr[r0, c0])
    e01 = float(elev_arr[r0, c1])
    e10 = float(elev_arr[r1, c0])
    e11 = float(elev_arr[r1, c1])
    elev_val = (
        e00 * (1 - dr) * (1 - dc)
        + e01 * (1 - dr) * dc
        + e10 * dr * (1 - dc)
        + e11 * dr * dc
    )

    # Nearest-neighbour for surface (categorical)
    r_nn = max(0, min(int(round(fr)), nrows - 1))
    c_nn = max(0, min(int(round(fc)), ncols - 1))
    surf_code = int(surf_arr[r_nn, c_nn])
    surf_val = _SURFACE_MAP[surf_code] if surf_code < len(_SURFACE_MAP) else "unknown"

    return elev_val, surf_val

# ── Pool override (baked real values near landing spots) ────────────

_pool: list[dict] | None = None
_POOL_RADIUS_DEG: Final = 0.15  # ~15km 内优先用池里的真实值


def _load_pool() -> list[dict]:
    global _pool
    with _CACHE_LOCK:
        if _pool is None:
            path = _DATA_DIR / "pool.json"
            _pool = _json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        return _pool


# ── 城市掩码(cities15000,人口>5万的城市附近算 urban)────────────

_cities: list[tuple[float, float]] | None = None
_city_buckets: dict[tuple[int, int], list[tuple[float, float]]] | None = None


def _load_cities() -> list[tuple[float, float]]:
    global _cities, _city_buckets
    with _CACHE_LOCK:
        if _cities is not None:
            return _cities
        cities: list[tuple[float, float]] = []
        buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
        path = _DATA_DIR / "packs" / "cities15000.txt"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 15:
                        continue
                    try:
                        if int(parts[14] or 0) >= 50000:
                            c = (float(parts[4]), float(parts[5]))
                            cities.append(c)
                            buckets.setdefault((int(math.floor(c[0])), int(math.floor(c[1]))), []).append(c)
                    except ValueError:
                        continue  # intentionally ignored: malformed city data line
        # 局部构建完再原子发布, 失败不会留下半填充缓存
        _cities = cities
        _city_buckets = buckets
        return _cities


def urban_nearby(lat: float, lon: float, km: float = 15.0) -> bool:
    """人口 5 万+ 城市 km 公里内 → True。1° 分桶索引, 只扫邻桶。"""
    global _city_buckets
    _load_cities()
    if not _city_buckets:
        return False
    deg = km / 111.0
    coslat = math.cos(math.radians(lat))
    reach = int(math.ceil(deg))
    blat, blon = int(math.floor(lat)), int(math.floor(lon))
    for dlat in range(-reach, reach + 1):
        for dlon in range(-reach, reach + 1):
            for clat, clon in _city_buckets.get((blat + dlat, blon + dlon), ()):
                if abs(clat - lat) < deg and abs((clon - lon) * coslat) < deg:
                    return True
    return False


def _pool_entry(lat: float, lon: float, require_elev: bool = True) -> dict | None:
    """Nearest pool entry within _POOL_RADIUS_DEG, else None.

    优先用烘焙值——地标坐标的精确海拔。require_elev=False 供 surface()
    使用: 329 条里 258 条只有 surface 没有 elev_m, 卡着 elev_m 会让
    surface() 的 pool 覆盖层完全失效, 退化为瓦片/网格推断值。
    """
    best: dict | None = None
    best_d = _POOL_RADIUS_DEG
    for e in _load_pool():
        if require_elev and "elev_m" not in e:
            continue
        d = abs(e["lat"] - lat) + abs((e["lon"] - lon) * math.cos(math.radians(lat)))
        if d < best_d:
            best_d = d
            best = e
    return best


def pool_biome(lat: float, lon: float, place_name: str = "") -> str | None:
    """Return the hand-tagged ``biome`` of the nearest pool spot, else None.

    A10: pool.json authors a ``biome`` per landing spot (all 329 carry one).
    Prefer that over inferring biome from surface cover — the grid can say
    "grass" over a city.  Unlike :func:`_pool_entry` this does not require an
    ``elev_m`` column (258 of the 329 spots omit it).

    *place_name* — same bidirectional ``name_hint`` guard as :func:`elevation`
    (Card 81), so a nearby landmark's biome cannot clobber this place's.
    """
    best: str | None = None
    best_d = _POOL_RADIUS_DEG
    for e in _load_pool():
        if "biome" not in e:
            continue
        if place_name and e.get("name_hint"):
            hint = e["name_hint"]
            if hint not in place_name and place_name not in hint:
                continue  # name mismatch → this spot is a different place
        d = abs(e["lat"] - lat) + abs((e["lon"] - lon) * math.cos(math.radians(lat)))
        if d < best_d:
            best_d = d
            best = e["biome"]
    return best


def _load_grid() -> None:
    """Load the best available grid into module-level arrays."""
    global _elev, _cover, _elev_f32
    with _CACHE_LOCK:
        if _elev is not None:
            return

        path = _FULL_PATH if _FULL_PATH.exists() else _TINY_PATH
        with np.load(path) as data:
            _elev = data["elev"]  # int16 [181, 360]
            _cover = data["cover"]  # uint8 [181, 360]
        # 浮点副本只做一次: 每次调用现转是 260KB 分配 + 6.5 万元素转换
        _elev_f32 = _elev.astype(np.float32)


def _ensure_loaded() -> None:
    if _elev is None:
        _load_grid()


# ── Helpers ─────────────────────────────────────────────────────────

def _latlon_to_grid(lat: float, lon: float) -> tuple[float, float]:
    """Convert lat/lon to fractional row/col in the grid."""
    row = 90.0 - lat
    col = (lon + 180.0) % 360.0
    return row, col


def _bilinear(arr: np.ndarray, row: float, col: float) -> float:
    """Bilinear interpolation on a [181, 360] grid."""
    nrows, ncols = arr.shape

    # 先钳 r0 再算 r1: row < -1 时 r1 会是负数, numpy 负索引静默回绕
    r0 = max(0, min(int(math.floor(row)), nrows - 1))
    c0 = int(math.floor(col))
    r1 = min(r0 + 1, nrows - 1)
    c1 = (c0 + 1) % ncols  # wrap longitude

    fr = row - math.floor(row)
    fc = col - math.floor(col)
    fr = max(0.0, min(fr, 1.0))
    fc = max(0.0, min(fc, 1.0))

    v00 = float(arr[r0, c0])
    v01 = float(arr[r0, c1])
    v10 = float(arr[r1, c0])
    v11 = float(arr[r1, c1])

    return (
        v00 * (1 - fr) * (1 - fc)
        + v01 * (1 - fr) * fc
        + v10 * fr * (1 - fc)
        + v11 * fr * fc
    )


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in kilometres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    a = min(a, 1.0)
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# Public alias for cross-module use (A1: single source of truth)
haversine_km = _haversine_km


# ── Public API ──────────────────────────────────────────────────────

def elevation(lat: float, lon: float, place_name: str = "") -> float:
    """Return interpolated elevation in metres at (*lat*, *lon*).

    Priority: pool baked values > tile data > **DEM column** > grid_tiny.
    Only the grid fallback path consults DEM: if the grid value is a fill
    value or differs from DEM by >100m, trust DEM (cities15000 column 16 has
    SRTM-corrected elevations for named cities).  Tile hits return early and
    are never cross-checked against DEM.

    *place_name* — when given, pool entries are only used if their
    ``name_hint`` is a substring of *place_name*.  This prevents a nearby
    landmark (e.g. Vesuvius at 1281 m) from clobbering the elevation of
    a city that happens to be within the pool radius (e.g. Naples at ~30 m).
    """
    from nowhere.dem import lookup as dem_lookup, is_fill_value

    entry = _pool_entry(lat, lon, require_elev=True)
    if entry is not None:
        # Card 81: only trust pool elevation when name matches place_name.
        # Match is bidirectional: "维苏威" matches "维苏威火山" and vice versa.
        if place_name and entry.get("name_hint"):
            hint = entry["name_hint"]
            if hint not in place_name and place_name not in hint:
                entry = None  # name mismatch → fall through to tile/DEM
        if entry is not None:
            return float(entry["elev_m"])
    # Try high-res tile (trust tile over DEM — tile has better resolution)
    tile = _find_tile(lat, lon)
    if tile is not None:
        elev_val, _ = _tile_bilinear(tile, lat, lon)
        return elev_val
    # Fall back to global grid
    _ensure_loaded()
    if _elev_f32 is None:
        raise RuntimeError("elevation grid not loaded")
    row, col = _latlon_to_grid(lat, lon)
    grid_elev = float(_bilinear(_elev_f32, row, col))
    # DEM override: if grid is fill value (~300) OR differs from DEM by >100m,
    # trust the cities15000 DEM column (SRTM-corrected for named cities).
    dem_val = dem_lookup(lat, lon)
    if dem_val is not None:
        if is_fill_value(grid_elev) or abs(dem_val - grid_elev) > 100:
            return dem_val
    return grid_elev


def surface(lat: float, lon: float, place_name: str = "") -> str:
    """Return surface type string at (*lat*, *lon*).

    Priority: pool baked values > tile data > grid_tiny.

    *place_name* — Card 81 同款双向 ``name_hint`` 守卫(与 elevation 一致):
    传入时, 半径内地标条目的 surface 不得覆盖本地点。
    """
    entry = _pool_entry(lat, lon, require_elev=False)
    if entry is not None and "surface" in entry:
        if place_name and entry.get("name_hint"):
            hint = entry["name_hint"]
            if hint not in place_name and place_name not in hint:
                entry = None  # name mismatch → fall through to tile/grid
        if entry is not None:
            return entry["surface"]
    # Try high-res tile
    tile = _find_tile(lat, lon)
    if tile is not None:
        _, surf_val = _tile_bilinear(tile, lat, lon)
        if surf_val not in ("water_ocean", "water_fresh") and urban_nearby(lat, lon):
            return "urban"
        return surf_val
    # Fall back to global grid
    _ensure_loaded()
    if _cover is None:
        raise RuntimeError("cover grid not loaded")
    row, col = _latlon_to_grid(lat, lon)
    # For cover, use nearest-neighbour (categorical data)
    r = max(0, min(int(round(row)), _cover.shape[0] - 1))
    c = int(round(col)) % _cover.shape[1]
    code = int(_cover[r, c])
    # 网格数据可能有超范围填充码(如 255 nodata), 与 _tile_bilinear 同防御
    s = _SURFACE_MAP[code] if code < len(_SURFACE_MAP) else "unknown"
    # tiny 网格没有城市分类: 人口城市附近盖 urban(#13 #26)
    if s not in ("water_ocean", "water_fresh") and urban_nearby(lat, lon):
        return "urban"
    return s


def is_water(lat: float, lon: float) -> bool:
    """Return ``True`` if the surface at (*lat*, *lon*) is water."""
    s = surface(lat, lon)
    return s.startswith("water")


def slope_between(
    a: tuple[float, float], b: tuple[float, float]
) -> tuple[float, float]:
    """Return ``(slope_degrees, distance_km)`` between two points."""
    lat1, lon1 = a
    lat2, lon2 = b
    dist_km = _haversine_km(lat1, lon1, lat2, lon2)
    e1 = elevation(lat1, lon1)
    e2 = elevation(lat2, lon2)
    diff_m = e2 - e1
    dist_m = dist_km * 1000.0
    if dist_m == 0:
        return (0.0, 0.0)
    slope_rad = math.atan2(abs(diff_m), dist_m)
    slope_deg = math.degrees(slope_rad)
    return (slope_deg, dist_km)


def destination(
    lat: float, lon: float, bearing_deg: float, dist_km: float
) -> tuple[float, float]:
    """Return the destination (*lat*, *lon*) after moving *dist_km* on *bearing_deg*.

    Uses the spherical destination formula.
    """
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    brng_r = math.radians(bearing_deg)
    angular = dist_km / _EARTH_RADIUS_KM

    sin_lat = math.sin(lat_r)
    cos_lat = math.cos(lat_r)
    sin_angular = math.sin(angular)
    cos_angular = math.cos(angular)

    new_lat = math.asin(
        sin_lat * cos_angular + cos_lat * sin_angular * math.cos(brng_r)
    )
    new_lon = lon_r + math.atan2(
        math.sin(brng_r) * sin_angular * cos_lat,
        cos_angular - sin_lat * math.sin(new_lat),
    )
    lat = math.degrees(new_lat)
    lon = math.degrees(new_lon)
    lon = ((lon + 180) % 360) - 180
    return (lat, lon)
