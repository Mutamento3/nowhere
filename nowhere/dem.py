"""DEM (Digital Elevation Model) fallback from cities15000.txt column 16.

The grid_tiny.npz uses 300 as a fill value for ~18.7% of cells where no
real DEM data exists.  This module reads the DEM column from cities15000
and provides a nearest-city elevation lookup as a fallback between tile
data and the raw grid.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Final

logger = logging.getLogger(__name__)

_PACK_PATH: Final = pathlib.Path(__file__).resolve().parent / "data" / "packs" / "cities15000.txt"

# GeoNames DEM 列的无数据哨兵值; 0 米与负高程(死海、荷兰)是合法值
_DEM_NO_DATA: Final = -9999

# Cache: list of (lat, lon, dem_m)
_cities_dem: list[tuple[float, float, float]] | None = None

# 2° 粗网格分桶: 50km 查询半径跨不出相邻桶, 先筛候选再精确算距离
_BUCKET_DEG = 2.0
_dem_buckets: dict[tuple[int, int], list[int]] = {}


def _load_cities_dem() -> list[tuple[float, float, float]]:
    """Load cities15000.txt and extract (lat, lon, dem) for cities with valid DEM."""
    global _cities_dem, _dem_buckets
    if _cities_dem is not None:
        return _cities_dem
    cities: list[tuple[float, float, float]] = []
    if not _PACK_PATH.exists():
        logger.warning("cities15000 缺失(%s), DEM 城市回退不可用", _PACK_PATH)
        _cities_dem = cities
        return _cities_dem
    try:
        with open(_PACK_PATH, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 17:
                    continue
                try:
                    lat = float(parts[4])
                    lon = float(parts[5])
                    dem_str = parts[16].strip()
                    if not dem_str:
                        continue
                    dem = float(dem_str)
                except (ValueError, IndexError):
                    continue  # intentionally ignored: malformed DEM data line
                if dem == _DEM_NO_DATA:
                    continue
                cities.append((lat, lon, dem))
    except (OSError, UnicodeDecodeError) as exc:
        # 读失败不缓存, 留待下次调用重试; 空结果让调用方走自己的降级
        logger.warning("cities15000 读取失败, DEM 回退暂不可用: %s", exc)
        return []
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, (clat, clon, _dem) in enumerate(cities):
        buckets.setdefault((int(clat // _BUCKET_DEG), int(clon // _BUCKET_DEG)), []).append(i)
    # 只有完整读成功才原子发布缓存
    _cities_dem = cities
    _dem_buckets = buckets
    return _cities_dem


from nowhere.terrain import haversine_km as _haversine_km


def lookup(lat: float, lon: float) -> float | None:
    """Find DEM elevation from the nearest city in cities15000.

    Returns elevation in metres, or None if no city is within 50 km.
    """
    cities = _load_cities_dem()
    if not cities:
        return None

    best_dist = 50.0  # max distance to consider (km)
    best_dem: float | None = None
    blat = int(lat // _BUCKET_DEG)
    blon = int(lon // _BUCKET_DEG)
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            b = (blat + dlat, blon + dlon)
            # 经度桶跨换日线回绕(桶域 -90..89)
            if b[1] > 89:
                b = (b[0], b[1] - 180)
            elif b[1] < -90:
                b = (b[0], b[1] + 180)
            for i in _dem_buckets.get(b, ()):
                clat, clon, dem = cities[i]
                d = _haversine_km(lat, lon, clat, clon)
                if d < best_dist:
                    best_dist = d
                    best_dem = dem
    return best_dem


def is_fill_value(elev: float) -> bool:
    """Check if a grid elevation value is likely a fill/placeholder (300m).

    注释口径 290-310 为闭区间: 两端值同样按 fill 处理。
    """
    return 290.0 <= elev <= 310.0
