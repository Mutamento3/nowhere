"""地方记忆——地方记得你来过,也记得你见过什么。

存在 NOWHERE_HOME 下:
- seen_cards.json: {地名: [已见方志卡 key]}
- seen_humanities.json: [已见人文卡 key] (全局,不按地名)
- visits.json: {地名: 次数}
- landings.json: 落点编录
- sightings.json: 动物目击编录
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
import threading
from pathlib import Path


from nowhere.util import _get_home

logger = logging.getLogger(__name__)

# 模块内所有读-改-写共用的进程内锁: server 在多线程 HTTP 与后台线程里
# 调用本模块, 无锁的 load→mutate→dump 会互相覆盖(丢失更新)
_lock = threading.RLock()


def _path(name: str) -> Path:
    # A8: home resolution is shared with notebook / state (nowhere.util).
    return _get_home() / name


def _load(name: str) -> dict:
    p = _path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # ERR-05: 只有内容确实不可解析才改名备份。权限/瞬时 IO 的读取失败
        # 时文件本身是好的, 改名后返回 {} 会让下一次 _dump 用空数据顶替。
        backup = p.with_name(p.name + ".corrupt")
        n = 0
        while backup.exists():
            n += 1
            backup = p.with_name(f"{p.name}.corrupt.{n}")
        try:
            p.replace(backup)
            logger.warning("%s 损坏, 已备份为 %s", p.name, backup.name)
        except OSError as exc:
            logger.warning("%s 损坏且备份改名失败: %s", p.name, exc)
        return {}
    # OSError(权限/EMFILE/瞬时 IO)向上抛: 调用方拿不到数据就不会触发
    # 后续用空数据覆盖原文件的写入链路


def _dump(name: str, data: dict) -> None:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        try:
            f = os.fdopen(tmp_fd, "w", encoding="utf-8")
        except BaseException:
            os.close(tmp_fd)  # fdopen 失败时 fd 未移交 with, 显式关闭防泄漏
            raise
        with f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, p)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # unlink 失败不许遮蔽原始异常
        raise


def seen_cards(place: str) -> set[str]:
    return set(_load("seen_cards.json").get(place, []))


def save_seen_cards(place: str, cards: set[str]) -> None:
    with _lock:
        data = _load("seen_cards.json")
        data[place] = sorted(cards)
        _dump("seen_cards.json", data)


def record_visit(place: str) -> int:
    """记一次到访,返回这是第几次。"""
    with _lock:
        data = _load("visits.json")
        data[place] = data.get(place, 0) + 1
        _dump("visits.json", data)
        return data[place]


def record_landing(
    place: str,
    lat: float,
    lon: float,
    elevation: float | None = None,
    surface: str | None = None,
) -> int:
    """落点编录: 地名+坐标+次数+最近一次+地貌(地图画地形符号用)。返回第几次来。"""
    from datetime import datetime, timezone

    with _lock:
        data = _load("landings.json")
        entry = data.get(place, {"lat": round(lat, 4), "lon": round(lon, 4), "count": 0})
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["lat"] = round(lat, 4)
        entry["lon"] = round(lon, 4)
        if elevation is not None:
            entry["elevation"] = round(elevation)
        if surface:
            entry["surface"] = surface
        entry["last"] = datetime.now(timezone.utc).isoformat()
        data[place] = entry
        _dump("landings.json", data)
        return entry["count"]


def landings() -> list[dict]:
    """全部落点,新的在前。"""
    data = _load("landings.json")
    items = [{"place": k, **v} for k, v in data.items()]
    items.sort(key=lambda x: x.get("last", ""), reverse=True)
    return items


def record_sighting(
    name: str,
    common_name: str,
    lat: float,
    lon: float,
    distance_m: int | None,
    seen_at: str,
    source: str,
) -> None:
    """动物目击编录: 谁/在哪/多远/哪天/来源。上限 200 条。"""
    from datetime import datetime, timezone

    with _lock:
        data = _load("sightings.json")
        items = data.get("items", [])
        items.append({
            "name": name,
            "common_name": common_name,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "distance_m": distance_m,
            "seen_at": seen_at,
            "source": source,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        data["items"] = items[-200:]
        _dump("sightings.json", data)


def sightings() -> list[dict]:
    """全部目击,新的在前。"""
    return list(reversed(_load("sightings.json").get("items", [])))


def seen_humanities() -> set[str]:
    """Load the global set of seen humanities card keys."""
    data = _load("seen_humanities.json")
    return set(data.get("keys", []))


# ── Card 20: Odometer (global total distance across all journeys) ────

def get_total_distance_km() -> float:
    """Return the global total distance walked across all journeys."""
    return float(_load("odometer.json").get("total_km", 0.0))


def add_distance_km(km: float) -> float:
    """Add km to the global odometer. Returns new total (与落盘值一致)."""
    with _lock:
        data = _load("odometer.json")
        total = round(float(data.get("total_km", 0.0)) + km, 3)
        data["total_km"] = total
        _dump("odometer.json", data)
        return total


def save_seen_humanities(keys: set[str]) -> None:
    """Persist the global set of seen humanities card keys."""
    with _lock:
        _dump("seen_humanities.json", {"keys": sorted(keys)})


# ── Card 16: revealed places (global, across journeys) ─────────────

def revealed_places() -> set[str]:
    """Load the global set of places that have been revealed from blind mode."""
    data = _load("revealed_places.json")
    return set(data.get("places", []))


def save_revealed_place(place: str) -> None:
    """Record a place as revealed from blind mode."""
    with _lock:
        places = revealed_places()
        places.add(place)
        _dump("revealed_places.json", {"places": sorted(places)})


# ── 明信片落盘: 文件是真相,谁寄的网页都看得见 ─────────────────────

_POSTCARDS_CAP = 100
_FOOTPRINTS_CAP = 200


def save_postcard(card: dict) -> None:
    """寄出即落盘。跨进程跨会话,墙不空。"""
    with _lock:
        data = _load("postcards.json")
        items = data.get("items", [])
        items.append(card)
        data["items"] = items[-_POSTCARDS_CAP:]
        _dump("postcards.json", data)


def update_postcard(card: dict) -> None:
    """卡内容变了(正面图生成好了)就回写。"""
    with _lock:
        data = _load("postcards.json")
        items = data.get("items", [])
        for i, c in enumerate(items):
            if c.get("id") == card.get("id"):
                items[i] = card
                break
        data["items"] = items
        _dump("postcards.json", data)


def add_postcard_reply(card_id: int, content: str) -> bool:
    """人回一句,落盘。卡在不在文件里,不在就 False。"""
    with _lock:
        data = _load("postcards.json")
        items = data.get("items", [])
        for c in items:
            if c.get("id") == card_id:
                c.setdefault("replies", []).append(content)
                data["items"] = items
                _dump("postcards.json", data)
                return True
        return False


def postcards() -> list[dict]:
    """全部明信片,新的在前。文件空时试着从 state.json 搬一次家。"""
    items = _load("postcards.json").get("items", [])
    if not items:
        state_file = _path("state.json")
        if state_file.exists():
            try:
                old = json.loads(state_file.read_text(encoding="utf-8"))
                items = old.get("postcards", [])
                if items:
                    # 返回值与落盘保持同一份裁剪结果, 边界行为稳定
                    items = items[-_POSTCARDS_CAP:]
                    _dump("postcards.json", {"items": items})
            except (OSError, json.JSONDecodeError):
                pass
    return list(reversed(items))


def delete_postcard(card_id: int) -> bool:
    """撕掉一张。测试卡、废卡,别留在墙上。"""
    with _lock:
        data = _load("postcards.json")
        items = data.get("items", [])
        keep = [c for c in items if c.get("id") != card_id]
        if len(keep) == len(items):
            return False
        data["items"] = keep
        _dump("postcards.json", data)
        return True


# ── 旅行足迹 ──────────────────────────────────────────────

def record_footprint(
    action: str,
    text: str,
    lat: float,
    lon: float,
    place: str | None = None,
    stream_url: str | None = None,
    station: dict | None = None,
) -> None:
    """持久化一次旅行行动，时间为现实 UTC。"""
    from datetime import datetime, timezone

    with _lock:
        data = _load("footprints.json")
        items = data.get("items", [])
        item = {
            "action": action,
            "text": text,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "place": place or "",
            "at": datetime.now(timezone.utc).isoformat(),
        }
        clean_stream_url = str(stream_url or "").strip()
        if clean_stream_url.startswith(("http://", "https://")):
            item["stream_url"] = clean_stream_url
        if station:
            public_station = {
                key: station[key]
                for key in ("name", "genre", "country")
                if station.get(key) not in (None, "")
            }
            if public_station:
                item["station"] = public_station
        items.append(item)
        data["items"] = items[-_FOOTPRINTS_CAP:]
        _dump("footprints.json", data)


def footprints() -> list[dict]:
    """按新到旧返回旅行行动。"""
    return list(reversed(_load("footprints.json").get("items", [])))


# ── 埋藏物品(Card 13: bury/find) ─────────────────────────────────

_BURIED_CAP = 100


def save_buried(entry: dict) -> None:
    """埋一件东西。FIFO 100 上限。"""
    with _lock:
        data = _load("buried.json")
        items = data.get("items", [])
        items.append(entry)
        data["items"] = items[-_BURIED_CAP:]
        _dump("buried.json", data)


def buried_items() -> list[dict]:
    """全部埋藏物。"""
    return _load("buried.json").get("items", [])


def buried_nearby(lat: float, lon: float, radius_km: float = 3.0) -> list[dict]:
    """3km 内的埋藏物。"""
    import math
    items = buried_items()
    result = []
    for item in items:
        pos = item.get("pos")
        if not pos or len(pos) < 2:
            continue
        dlat = pos[0] - lat
        dlon = pos[1] - lon
        if dlon > 180:
            dlon -= 360
        elif dlon < -180:
            dlon += 360
        d = math.sqrt((dlat * 111.0) ** 2 + (dlon * 111.0 * math.cos(math.radians(lat))) ** 2)
        if d <= radius_km:
            result.append(item)
    return result


# ── Journey footprints ─────────────────────────────────────────────────

def journey_footprints() -> list[dict]:
    """返回已记录行动，并诚实补充旧数据中可确认的旅程证据。"""
    from datetime import datetime, timezone

    items = footprints()

    for card in postcards():
        if card.get("sent_at"):
            continue
        stamp = card.get("stamp") or {}
        lat = stamp.get("lat")
        lon = stamp.get("lon")
        if lat is None or lon is None:
            # legacy 条目缺坐标: 下游按 float 使用(画地图/算距离)会炸, 显式跳过
            continue
        items.append({
            "action": "postcard",
            "text": card.get("text", ""),
            "lat": lat,
            "lon": lon,
            "place": stamp.get("place", ""),
            "at": None,
            "journey_at": stamp.get("local_time", ""),
            "legacy": True,
        })

    for landing in landings():
        count = int(landing.get("count", 0))
        items.append({
            "action": "land_history",
            "text": f"在这里留下了 {count} 次抵达记录。",
            "lat": landing.get("lat"),
            "lon": landing.get("lon"),
            "place": landing.get("place", ""),
            "at": landing.get("last"),
            "legacy": True,
        })

    def _ts(item: dict) -> float:
        # 足迹的 at 是 UTC ISO; legacy journey_at 是无时区的本地时间串,
        # 字典序比较得不到正确时间顺序 → 统一解析成时间戳再排
        raw = item.get("at") or item.get("journey_at") or ""
        try:
            dt = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return 0.0
        if dt.tzinfo is None:
            # legacy 本地串无时区信息, 按 UTC 折算只为可比较
            return dt.replace(tzinfo=timezone.utc).timestamp()
        return dt.timestamp()

    items.sort(key=_ts, reverse=True)
    return items


# ── Card 10: 痕迹链 — 世界在你离开后继续过日子 ─────────────────────

_TRACES_DATA_DIR = pathlib.Path(__file__).resolve().parent / "data"
_traces_cache: dict | None = None


def _load_traces() -> dict:
    """Load traces.json once and cache."""
    global _traces_cache
    if _traces_cache is not None:
        return _traces_cache
    fp = _TRACES_DATA_DIR / "traces.json"
    if fp.exists():
        _traces_cache = json.loads(fp.read_text(encoding="utf-8"))
    else:
        _traces_cache = {}
    return _traces_cache


def has_trace(place: str) -> bool:
    """Check if a place has a trace chain."""
    return place in _load_traces()


def get_trace_stage(place: str) -> int:
    """Get the current trace stage for a place. 0-indexed, default 0."""
    data = _load("trace_stages.json")
    return int(data.get(place, 0))


def advance_trace_stage(place: str) -> int:
    """Advance the trace stage for a place. Returns new stage."""
    traces = _load_traces()
    if place not in traces:
        return 0
    max_stage = len(traces[place]["stages"]) - 1
    data = _load("trace_stages.json")
    current = int(data.get(place, 0))
    new_stage = min(current + 1, max_stage)
    data[place] = new_stage
    _dump("trace_stages.json", data)
    return new_stage


def get_trace_text(place: str) -> str | None:
    """Get the trace text for the current stage of a place.

    Returns the text and advances the stage. 读取当前阶段与推进合并为
    单次加锁的 load→compute→dump: 拆成两步时并发线程会都读到 current=N
    再各写回 N+1, 推进丢失或重复消费。
    """
    traces = _load_traces()
    entry = traces.get(place)
    if not isinstance(entry, dict) or not entry.get("stages"):
        # 缺 stages 键或空列表: 防御 IndexError/KeyError, 无链可放 → None
        return None
    stages = entry["stages"]
    with _lock:
        data = _load("trace_stages.json")
        stage = int(data.get(place, 0))
        if stage >= len(stages):
            stage = len(stages) - 1
        text = stages[stage]
        data[place] = min(stage + 1, len(stages) - 1)
        _dump("trace_stages.json", data)
    return text


def trace_stages() -> dict:
    """Return all trace stage data (for inspection/testing)."""
    return _load("trace_stages.json")


# ── Card 50: Lost souvenirs (不可逆) ────────────────────────────────

_LOST_SOUVENIRS_CAP = 50


def record_lost_souvenir(name: str, place: str) -> None:
    """Record a lost souvenir. Card 50: some things lost are lost."""
    from datetime import datetime, timezone
    with _lock:
        data = _load("lost_souvenirs.json")
        items = data.get("items", [])
        items.append({
            "name": name,
            "place": place,
            "lost_at": datetime.now(timezone.utc).isoformat(),
        })
        data["items"] = items[-_LOST_SOUVENIRS_CAP:]
        _dump("lost_souvenirs.json", data)


def lost_souvenirs() -> list[dict]:
    """Return all lost souvenirs (for inspection/testing)."""
    return list(reversed(_load("lost_souvenirs.json").get("items", [])))
