"""离线国家码查询——用买断的 GeoNames cities15000 就近推断。

数据在 nowhere/data/packs/cities15000.txt(gitignored,资源包)。
包不在就返回 None,调用方走自己的降级路径,不炸。
"""

from __future__ import annotations

import math
import pathlib

_PACK_PATH = pathlib.Path(__file__).resolve().parent / "data" / "packs" / "cities15000.txt"

_cities: list[tuple[float, float, str]] | None = None
_loaded = False


def _load() -> None:
    global _cities, _loaded
    if _loaded:
        return
    # 先在局部构建,成功后再原子赋值: 中途失败不会留下"已加载"的半成品缓存
    cities: list[tuple[float, float, str]] = []
    if _PACK_PATH.exists():
        try:
            with open(_PACK_PATH, encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 9:
                        continue
                    try:
                        cities.append((float(parts[4]), float(parts[5]), parts[8]))
                    except ValueError:
                        continue  # intentionally ignored: malformed coordinate in city data
        except (OSError, UnicodeDecodeError):
            # 包损坏与包缺失同口径降级(模块头约定: 不炸),不留半填充缓存
            cities = []
    _cities = cities
    _loaded = True


def country_code_of(lat: float, lon: float) -> str | None:
    """返回最近城市的 ISO 国家码(如 "VN");数据包缺失返回 None。

    坐标非法(NaN/inf/越界)同样返回 None,与"包缺失"可区分开靠调用方自查;
    4 万城市线性扫,几毫秒,够用了。
    """
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    _load()
    if not _cities:
        return None
    best_cc: str | None = None
    best_d = math.inf
    cos_lat = math.cos(math.radians(lat))
    for clat, clon, cc in _cities:
        dlat = clat - lat
        dlon = clon - lon
        # Wrap longitude at date line (e.g. 179 to -179 is 2 degrees, not 358)
        if dlon > 180:
            dlon -= 360
        elif dlon < -180:
            dlon += 360
        d = dlat ** 2 + (dlon * cos_lat) ** 2
        if d < best_d:
            best_d = d
            best_cc = cc
    return best_cc
