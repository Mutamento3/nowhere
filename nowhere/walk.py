"""Walking physics: step movement with terrain-aware slope, water, and time."""

from __future__ import annotations

import math

from nowhere import terrain
from nowhere.state import WorldState

# ── Constants ───────────────────────────────────────────────────────
_DIST_MIN = 0.05  # 50 meters minimum (card 7: short-distance probing)
_DIST_MAX = 5.0
_DIST_MAX_FATIGUED = 3.0  # Card 50: fatigue>6 caps walk distance
_LAND_SPEED_KMH = 4.0
_WATER_SPEED_KMH = 1.5
_SLOPE_SLOW_THRESHOLD_DEG = 20.0
_CLIFF_THRESHOLD_DEG = 45.0
_LAT_LIMIT = 85.0  # beyond ±85° latitude: honest "no further north/south"
_COMPASS_DIRS = 8  # candidate bearings sampled per scan (semantic / ocean search)
_SEMANTIC_DIRECTIONS = {
    "uphill": _COMPASS_DIRS,
    "toward_sea": _COMPASS_DIRS,
}

# ── Latitude limit closing variants (Card 40: honest boundaries) ────
_LAT_LIMIT_CLOSINGS = [
    "再往前没有北了。地平线弯成了弧形,你站在地球的头顶。",
    "北边到头了。风从四面八方同时吹来,没有方向了。",
    "不能再往北走了。脚下是冰,头顶是极夜的黑。你到了。",
]


def _bearing_from_path(path: list[dict]) -> float:
    """Compute bearing from the last two path points, or default north."""
    if len(path) < 2:
        return 0.0  # north
    p1 = path[-2]
    p2 = path[-1]
    lat1, lon1 = math.radians(p1["lat"]), math.radians(p1["lon"])
    lat2, lon2 = math.radians(p2["lat"]), math.radians(p2["lon"])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


def _pick_semantic_bearing(
    lat: float, lon: float, semantic: str, dist_km: float
) -> tuple[float, float]:
    """Try 8 directions and pick the one that best matches the semantic goal.

    Returns ``(bearing, best_elevation_delta)``.
    """
    best_bearing = 0.0
    best_score: tuple[int, float] = (-1, -math.inf)
    best_delta = 0.0
    e_here = terrain.elevation(lat, lon)

    n_dir = _SEMANTIC_DIRECTIONS.get(semantic, _COMPASS_DIRS)
    step = 360.0 / n_dir
    for i in range(n_dir):
        bearing = i * step
        dest_lat, dest_lon = terrain.destination(lat, lon, bearing, dist_km)
        e_dest = terrain.elevation(dest_lat, dest_lon)
        delta = e_dest - e_here

        if semantic == "uphill":
            score = (0, delta)  # maximize elevation gain
        elif semantic == "toward_sea":
            sea_dist = water_ahead_km(lat, lon, bearing, max_km=20.0)
            # An actual ocean heading always beats an elevation-only proxy.
            score = (1, -sea_dist) if sea_dist is not None else (0, -delta)
        else:
            score = (0, 0.0)

        if score > best_score:
            best_score = score
            best_bearing = bearing
            best_delta = delta

    return best_bearing, best_delta


def best_uphill_gain(state: WorldState, dist_km: float = 2.0) -> float:
    """8 个方向里最大的海拔增益(米)。平地返回 <=0。"""
    if state.pos is None:
        return 0.0
    lat, lon = state.pos
    _, best_delta = _pick_semantic_bearing(lat, lon, "uphill", dist_km)
    return best_delta


def water_ahead_km(lat: float, lon: float, bearing_deg: float, max_km: float = 20.0,
                   include_fresh: bool = False) -> float | None:
    """沿方位往前走,多少公里内能碰到水(每 1km 采样)。碰不到返回 None。

    include_fresh=False 时只检测海水(toward_sea 语义);
    include_fresh=True 时检测所有水体(阻挡闸门用)。
    """
    d = 1.0
    while d <= max_km:
        lat2, lon2 = terrain.destination(lat, lon, bearing_deg, d)
        surf = terrain.surface(lat2, lon2)
        if include_fresh:
            is_target = surf.startswith("water")
        else:
            is_target = surf == "water_ocean"
        if is_target:
            # Card 64: coarse-grid false-ocean gate.  Real ocean is at sea
            # level; "water_ocean" above 1000 m is a grid artifact.
            # 闸门只对海水像素生效: include_fresh 承诺检测所有水体,
            # 高原湖泊/山地河流海拔高但真实存在, 不能当网格假象跳过
            if (not include_fresh or surf == "water_ocean") and terrain.elevation(lat2, lon2) > 1000:
                d += 1.0
                continue
            return d
        d += 1.0
    return None


def nearest_ocean_km_and_bearing(
    lat: float, lon: float, max_km: float = 50.0
) -> tuple[float | None, float | None]:
    """Scan 8 compass directions for the nearest ocean within *max_km*.

    Returns ``(min_km, bearing_deg)`` or ``(None, None)`` if no ocean found.
    """
    min_km: float | None = None
    min_bearing: float | None = None
    step = 360.0 / _COMPASS_DIRS
    for i in range(_COMPASS_DIRS):
        bearing = i * step
        d = water_ahead_km(lat, lon, bearing, max_km=max_km)
        if d is not None:
            if min_km is None or d < min_km:
                min_km = d
                min_bearing = bearing
    return min_km, min_bearing


def step(
    state: WorldState,
    bearing_deg: float | None,
    semantic: str | None,
    dist_km: float,
    max_dist: float = _DIST_MAX,
) -> dict:
    """Execute one walking step and update state.

    Returns {"blocked", "reason", "entered_water", "elevation_delta",
             "slope_deg", "dist_km", "new_surface", "climbed", "no_gain",
             "far_slope", "sea_ahead_km", "clamped", "lat_limit",
             "water_distance_km"}.

    DATA-05: every exit returns the same key set — callers must never have to
    guess which keys a particular path happened to populate.

    里程/时间记账契约: 阻挡出口(water/cliff)的里程与耗时由 step() 记账
    (走过去了只是没穿过); 成功路径的里程由调用方在 Card 20 处记账,
    step() 不重复累加 —— 否则会双重计数。

    max_dist: override the maximum distance per step (Card 50: fatigue cap).
    """
    if state.pos is None:
        raise RuntimeError("state.pos must be set before stepping")
    orig_dist = dist_km
    dist_km = max(_DIST_MIN, min(max_dist, dist_km))
    clamped = dist_km != orig_dist
    lat, lon = state.pos

    # ── Determine bearing ────────────────────────────────────────────
    no_gain = False
    far_slope: tuple[float, float] | None = None  # (bearing, gain_m) 远处的坡
    if bearing_deg is not None:
        bearing = bearing_deg
    elif semantic is not None and semantic in _SEMANTIC_DIRECTIONS:
        bearing, best_delta = _pick_semantic_bearing(lat, lon, semantic, dist_km)
        if semantic == "uphill" and abs(best_delta) < 5.0:
            # 近处没坡?往远处看(5km/10km):有坡就带路,不冤枉说无山
            for far_dist in (5.0, 10.0):
                far_bearing, far_gain = _pick_semantic_bearing(lat, lon, "uphill", far_dist)
                if far_gain > 50.0:
                    far_slope = (far_bearing, far_gain)
                    bearing = far_bearing
                    break
            if far_slope is None:
                no_gain = True
                return {
                    "blocked": False,
                    "reason": None,
                    "entered_water": False,
                    "elevation_delta": 0.0,
                    "slope_deg": 0.0,
                    "dist_km": dist_km,
                    "new_surface": terrain.surface(lat, lon),
                    "climbed": False,
                    "no_gain": True,
                    "far_slope": None,
                    "sea_ahead_km": None,
                    "clamped": clamped,
                    "lat_limit": False,
                    "water_distance_km": None,
                }
    else:
        bearing = _bearing_from_path(state.path)

    # ── Compute destination ──────────────────────────────────────────
    new_lat, new_lon = terrain.destination(lat, lon, bearing, dist_km)

    # ── Sphere wrap (Card 40: honest boundaries) ────────────────────
    # Longitude: auto-wrap at ±180°
    if new_lon > 180.0:
        new_lon -= 360.0
    elif new_lon < -180.0:
        new_lon += 360.0

    # Latitude: ±85° limit with honest closing
    lat_limit_reached = False
    if abs(new_lat) > _LAT_LIMIT:
        new_lat = math.copysign(_LAT_LIMIT, new_lat)
        lat_limit_reached = True

    # ── Water honesty (Card 40): walking toward open water ≥5km ─────
    # Check if destination is water and we're on land
    dest_surface = terrain.surface(new_lat, new_lon)
    was_on_land = not terrain.is_water(lat, lon)
    if was_on_land and dest_surface in ("water_ocean", "water_fresh"):
        # Check distance to water
        water_dist = water_ahead_km(lat, lon, bearing, max_km=10.0, include_fresh=True)
        if water_dist is not None and water_dist >= 5.0:
            # Blocked but distance still accumulates (you walked there)
            # 时间口径与里程一致: 走到了但过不去, 按平地速度计时
            state.total_distance_km += dist_km
            state.elapsed_hours += dist_km / _LAND_SPEED_KMH
            return {
                "blocked": True,
                "reason": "water",
                "entered_water": False,
                "elevation_delta": 0.0,
                "slope_deg": 0.0,
                "dist_km": dist_km,
                "new_surface": terrain.surface(lat, lon),
                "climbed": False,
                "no_gain": no_gain,
                "far_slope": far_slope,
                "sea_ahead_km": None,
                "clamped": clamped,
                "lat_limit": lat_limit_reached,
                "water_distance_km": water_dist,
            }

    # ── Slope check ──────────────────────────────────────────────────
    slope_deg, actual_dist = terrain.slope_between(
        (lat, lon), (new_lat, new_lon)
    )

    if slope_deg > _CLIFF_THRESHOLD_DEG:
        # Blocked but distance still accumulates (you walked there, just couldn't pass)
        # 时间口径与里程一致: 按平地速度估算耗时(坡度未知, 不打折)
        state.total_distance_km += dist_km
        state.elapsed_hours += dist_km / _LAND_SPEED_KMH
        result = {
            "blocked": True,
            "reason": "cliff",
            "entered_water": False,
            "elevation_delta": 0.0,
            "slope_deg": slope_deg,
            "dist_km": dist_km,
            "new_surface": terrain.surface(lat, lon),
            "climbed": False,
            "no_gain": no_gain,
            "far_slope": far_slope,
            "sea_ahead_km": None,
            "clamped": clamped,
            "lat_limit": lat_limit_reached,
            "water_distance_km": None,
        }
        return result

    # ── Elevation delta ──────────────────────────────────────────────
    e_old = terrain.elevation(lat, lon)
    e_new = terrain.elevation(new_lat, new_lon)
    elev_delta = e_new - e_old
    climbed = elev_delta > 0

    # ── Water transition ─────────────────────────────────────────────
    was_water = terrain.is_water(lat, lon)
    now_water = terrain.is_water(new_lat, new_lon)
    entered_water = now_water and not was_water

    if now_water:
        state.mode = "water"
    else:
        state.mode = "land"

    # ── Time accumulation ────────────────────────────────────────────
    speed = _WATER_SPEED_KMH if state.mode == "water" else _LAND_SPEED_KMH
    if slope_deg > _SLOPE_SLOW_THRESHOLD_DEG:
        speed *= 0.5
    # dist_km is already clamped; use actual_dist for time
    travel_hours = actual_dist / speed
    state.elapsed_hours += travel_hours

    # ── Update position and path ─────────────────────────────────────
    new_lat = max(-90, min(90, new_lat))
    new_lon = ((new_lon + 180) % 360) - 180
    state.pos = (new_lat, new_lon)
    state.heading = bearing
    state.path.append({
        "lat": new_lat,
        "lon": new_lon,
        "elevation": e_new,
        "dist_km": dist_km,
    })

    # ── toward_sea: 前方多远有水(给"闻到咸味"的线索)─────────────
    sea_ahead = None
    if semantic == "toward_sea" and state.mode == "land":
        sea_ahead = water_ahead_km(new_lat, new_lon, bearing)

    result = {
        "blocked": False,
        "reason": None,
        "entered_water": entered_water,
        "elevation_delta": elev_delta,
        "slope_deg": slope_deg,
        "dist_km": dist_km,
        "new_surface": terrain.surface(new_lat, new_lon),
        "climbed": climbed,
        "no_gain": no_gain,
        "far_slope": far_slope,  # (bearing, gain_m)|None:近处没坡但远处有
        "sea_ahead_km": sea_ahead,
        "clamped": clamped,
        "lat_limit": lat_limit_reached,
        "water_distance_km": None,
    }
    # 成功路径不在此累加 total_distance_km: 调用方(server Card 20)记账。
    # 但 lat_limit 提前返回的调用方必须自行补记, 否则真实行走漏账。
    return result
