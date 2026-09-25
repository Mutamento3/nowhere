"""同游者——异步多人系统,默认关闭。

环境变量:
  NOWHERE_COTRAVEL = "1"     完整功能(脚印 + 相遇 + @留言 + 点名)
  NOWHERE_COTRAVEL = "quiet" 仅脚印,不见面不点名
  未设置 / "0" / ""          全部跳过,零开销
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 共享 JSON 的 load→mutate→save 序列全部经此锁: 调用方既有异步 HTTP 入口
# 又有后台线程, 无锁时后写者会整体覆盖先写者的更新(丢失更新)
_io_lock = threading.RLock()


# ── Master switch ──────────────────────────────────────────────────────

def is_enabled() -> bool:
    """True when cotraveler features should be active at all."""
    val = os.environ.get("NOWHERE_COTRAVEL", "")
    return val in ("1", "quiet")


def is_quiet() -> bool:
    """True when only footprints are enabled (no meeting/naming)."""
    return os.environ.get("NOWHERE_COTRAVEL", "") == "quiet"


def _travelers_path() -> Path:
    base = os.environ.get("NOWHERE_HOME") or str(Path.home() / ".nowhere")
    return Path(base) / "travelers.json"


def _archive_path() -> Path:
    base = os.environ.get("NOWHERE_HOME") or str(Path.home() / ".nowhere")
    return Path(base) / "travelers_archive.json"


def _messages_path() -> Path:
    base = os.environ.get("NOWHERE_HOME") or str(Path.home() / ".nowhere")
    return Path(base) / "cotraveler_messages.json"


def _load_json(path: Path) -> dict:
    """读取共享 JSON。损坏时备份留证并抛错, 不静默返回 {}。

    空 dict 会被调用方增量修改后整体写回 —— 一次读坏等于整个注册表/
    脚印/留言被静默清空, 这比读失败本身严重得多。
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        backup = path.with_name(f"{path.name}.corrupt_{datetime.now():%Y%m%d_%H%M%S}")
        try:
            path.replace(backup)
            logger.warning("%s 损坏, 已备份为 %s", path.name, backup.name)
        except OSError as backup_exc:
            logger.warning("%s 损坏且备份改名失败: %s", path.name, backup_exc)
        raise ValueError(f"{path.name} 损坏, 拒绝以空数据覆盖: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 顶层不是 dict")
    return data


def _save_json(path: Path, data: dict) -> None:
    """同目录临时文件 + os.replace 原子替换, 不给"中途崩溃制造损坏档"留口。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)  # fdopen 失败时 fd 未移交 with, 显式关闭防泄漏
            raise
        with f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass  # 清理失败不许遮蔽原始异常
        raise


# ── Registry: register / refresh / expire ─────────────────────────────

_ARCHIVE_DAYS = 7


def register(name: str, place: str, lat: float, lon: float) -> None:
    """Register or refresh a traveler on open_door."""
    if not is_enabled():
        return
    with _io_lock:
        data = _load_json(_travelers_path())
        entry = data.get(name, {})
        entry["place"] = place
        entry["pos"] = [round(lat, 4), round(lon, 4)]
        entry["last_seen"] = datetime.now(timezone.utc).isoformat()
        entry["door_count"] = int(entry.get("door_count", 0)) + 1
        data[name] = entry
        # 互斥不变量: 过期归档后再次注册 = 复活, 必须从归档移除,
        # 否则同一人两处并存, 会被按过去式渲染成旧脚印
        archive = _load_json(_archive_path())
        if name in archive:
            archive.pop(name)
            _save_json(_archive_path(), archive)
        _save_json(_travelers_path(), data)


def refresh_pos(name: str, lat: float, lon: float) -> None:
    """Update position (called every 5 walk steps)."""
    if not is_enabled():
        return
    with _io_lock:
        data = _load_json(_travelers_path())
        if name in data:
            data[name]["pos"] = [round(lat, 4), round(lon, 4)]
            data[name]["last_seen"] = datetime.now(timezone.utc).isoformat()
            _save_json(_travelers_path(), data)


def expire_inactive() -> None:
    """Move travelers inactive for 7+ days to archive."""
    if not is_enabled():
        return
    with _io_lock:
        data = _load_json(_travelers_path())
        archive = _load_json(_archive_path())
        cutoff = datetime.now(timezone.utc) - timedelta(days=_ARCHIVE_DAYS)
        to_archive = []
        for name, entry in data.items():
            last = entry.get("last_seen", "")
            if last:
                try:
                    dt = datetime.fromisoformat(last)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        to_archive.append(name)
                except (ValueError, TypeError):
                    pass
        for name in to_archive:
            archive[name] = data.pop(name)
            archive[name]["archived"] = True
        if to_archive:
            _save_json(_travelers_path(), data)
            _save_json(_archive_path(), archive)


def get_active_travelers() -> dict[str, dict]:
    """Return dict of name -> entry for non-archived travelers."""
    if not is_enabled():
        return {}
    expire_inactive()
    return _load_json(_travelers_path())


def get_archived_travelers() -> dict[str, dict]:
    """Return archived (inactive 7+ days) travelers."""
    if not is_enabled():
        return {}
    return _load_json(_archive_path())


# ── Distance helper ────────────────────────────────────────────────────

def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    dlat = math.radians(a[0] - b[0])
    lon_delta = (a[1] - b[1] + 180.0) % 360.0 - 180.0
    dlon = math.radians(lon_delta) * math.cos(math.radians((a[0] + b[0]) / 2))
    return 6371.0 * math.sqrt(dlat * dlat + dlon * dlon)


def _bearing_word(bearing_deg: float) -> str:
    """Return the Chinese compass word for a bearing in degrees."""
    dirs = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    return dirs[int((bearing_deg + 22.5) / 45) % 8]


# ── Footprint tracking ────────────────────────────────────────────────

_FOOTPRINT_MAX = 500


def record_footprint(name: str, lat: float, lon: float, place: str) -> None:
    """Record a footprint entry for a traveler (called on walk)."""
    if not is_enabled():
        return
    with _io_lock:
        data = _load_json(_travelers_path())
        if name not in data:
            return
        fp_key = "footprints"
        fps = data[name].setdefault(fp_key, [])
        # Compute bearing from last footprint if exists
        bearing = None
        if fps:
            last = fps[-1]
            prev_pos = (last["lat"], last["lon"])
            curr_pos = (lat, lon)
            dlat = curr_pos[0] - prev_pos[0]
            dlon = curr_pos[1] - prev_pos[1]
            if abs(dlat) > 0.0001 or abs(dlon) > 0.0001:
                # 方位角: dlon 须按 cos(平均纬度) 收缩, 否则纬度越高
                # 东西向的方位偏差越大(与 _km 的口径一致)
                mean_lat = math.radians((curr_pos[0] + prev_pos[0]) / 2)
                bearing = math.degrees(math.atan2(dlon * math.cos(mean_lat), dlat)) % 360
        fps.append({
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "place": place,
            "at": datetime.now(timezone.utc).isoformat(),
            "bearing": round(bearing, 1) if bearing is not None else None,
        })
        data[name][fp_key] = fps[-_FOOTPRINT_MAX:]
        _save_json(_travelers_path(), data)


def check_footprints(
    my_name: str,
    lat: float,
    lon: float,
    rng,
    encounter_counts: dict[str, int],
) -> str | None:
    """Check if we're walking through another's recent footprints.

    Returns a footprint text line or None.
    - 3km radius, 24h window for active; any time for archived
    - 15% chance
    - First encounter: anonymous; 3rd+ with same person: name them
    """
    if not is_enabled():
        return None
    if rng.random() > 0.15:
        return None

    data = _load_json(_travelers_path())
    # Also include archived travelers (their footprints persist)
    archive = _load_json(_archive_path())
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    candidates: list[tuple[str, dict, float]] = []
    # Check active travelers (24h window)
    for name, entry in data.items():
        if name == my_name:
            continue
        for fp in entry.get("footprints", []):
            fp_at = fp.get("at", "")
            if not fp_at:
                continue
            try:
                fp_dt = datetime.fromisoformat(fp_at)
                if fp_dt.tzinfo is None:
                    fp_dt = fp_dt.replace(tzinfo=timezone.utc)
                if fp_dt < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            dist = _km((lat, lon), (fp["lat"], fp["lon"]))
            if dist <= 3.0:
                candidates.append((name, fp, dist))
                break  # one match per traveler is enough
    # Check archived travelers (no time cutoff — old footprints still visible)
    for name, entry in archive.items():
        if name == my_name:
            continue
        for fp in entry.get("footprints", []):
            dist = _km((lat, lon), (fp["lat"], fp["lon"]))
            if dist <= 3.0:
                candidates.append((name, fp, dist))
                break

    if not candidates:
        return None

    # Pick the closest
    candidates.sort(key=lambda x: x[2])
    other_name, fp, dist = candidates[0]
    count = encounter_counts.get(other_name, 0) + 1
    encounter_counts[other_name] = count

    # Determine surface from current environment (caller provides)
    bearing = fp.get("bearing")
    bearing_word = _bearing_word(bearing) if bearing is not None else ""

    # Check if archived (past tense) — 活跃优先: 复活后的旅者会同时出现在
    # 两张表里(register 已改为从归档移除, 这里再兜一层), 以活跃表为准,
    # 不把还在走的旅者渲染成过去式
    is_archived = other_name in archive and other_name not in data

    if count >= 3:
        if is_archived:
            return _fp_named_archived(other_name, bearing_word, rng)
        return _fp_named(other_name, bearing_word, rng)
    else:
        if is_archived:
            return _fp_anon_archived(bearing_word, rng)
        return _fp_anon(bearing_word, rng)


# ── Footprint variant pools ───────────────────────────────────────────

_FP_ANON_VARIANTS = [
    "沙面上有一串脚印,不是你的。{bearing}",
    "地上有脚印,比你的大。{bearing}",
    "泥地里留着别人的脚印,还没干。{bearing}",
    "雪地上有另一行印子,比你先到。{bearing}",
    "湿地里有脚印,鞋纹和你不一样。{bearing}",
    "碎石路上有踩过的痕迹,不是你的。{bearing}",
]

_FP_ANON_ARCHIVED = [
    "沙面上有一串旧脚印,雨都下过一场了。{bearing}",
    "地上有脚印,已经被风抹得模糊。{bearing}",
    "泥地里留着旧印子,边缘塌了。{bearing}",
]

_FP_NAMED_VARIANTS = [
    "这脚印你认得了——是{name}的。{bearing}",
    "地上那串脚印,{name}来过。{bearing}",
    "又是{name}的脚印。{bearing}",
    "脚印和上次一样,{name}走的。{bearing}",
    "{name}刚走过这里,脚印还是新的。{bearing}",
    "泥里有{name}的鞋印,{bearing}",
]

_FP_NAMED_ARCHIVED = [
    "这脚印你认得了——是{name}的。旧了,像很久以前走的。{bearing}",
    "{name}的脚印还在,但已经不新了。{bearing}",
]


def _fp_anon(bearing_word: str, rng) -> str:
    template = rng.choice(_FP_ANON_VARIANTS)
    b = f"朝{bearing_word}去了。" if bearing_word else "看不清方向。"
    return template.format(bearing=b)


def _fp_anon_archived(bearing_word: str, rng) -> str:
    template = rng.choice(_FP_ANON_ARCHIVED)
    b = f"朝{bearing_word}方向。" if bearing_word else ""
    return template.format(bearing=b)


def _fp_named(name: str, bearing_word: str, rng) -> str:
    template = rng.choice(_FP_NAMED_VARIANTS)
    b = f"朝{bearing_word}走了。" if bearing_word else ""
    return template.format(name=name, bearing=b)


def _fp_named_archived(name: str, bearing_word: str, rng) -> str:
    template = rng.choice(_FP_NAMED_ARCHIVED)
    b = f"朝{bearing_word}方向。" if bearing_word else ""
    return template.format(name=name, bearing=b)


# ── Meeting (synchronous, restrained) ─────────────────────────────────

def check_meeting(
    my_name: str,
    lat: float,
    lon: float,
    rng,
    meeting_log: dict[str, str],
) -> tuple[str | None, str | None]:
    """Check if a meeting happens with another active traveler.

    Conditions: both active within 24h AND currently <3km apart.
    Max 1 meeting per pair per 7 days (simulated time).
    Returns (my_text, their_text) or (None, None).
    """
    if not is_enabled() or is_quiet():
        return None, None

    data = _load_json(_travelers_path())
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    seven_days_ago = now - timedelta(days=7)

    for name, entry in data.items():
        if name == my_name:
            continue
        # Check activity within 24h
        last = entry.get("last_seen", "")
        if not last:
            continue
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if last_dt < cutoff:
                continue
        except (ValueError, TypeError):
            continue
        # Check distance
        pos = entry.get("pos")
        if not pos or len(pos) < 2:
            continue
        dist = _km((lat, lon), (pos[0], pos[1]))
        if dist >= 3.0:
            continue
        # Check cooldown: 7 days per pair
        pair_key = "|".join(sorted([my_name, name]))
        last_meet = meeting_log.get(pair_key, "")
        if last_meet:
            try:
                meet_dt = datetime.fromisoformat(last_meet)
                if meet_dt.tzinfo is None:
                    meet_dt = meet_dt.replace(tzinfo=timezone.utc)
                if meet_dt > seven_days_ago:
                    continue
            except (ValueError, TypeError):
                pass

        # Meeting happens
        my_text = rng.choice(_MEETING_MY_VARIANTS)
        their_text = rng.choice(_MEETING_THEIR_VARIANTS)
        meeting_log[pair_key] = now.isoformat()
        return my_text, their_text

    return None, None


_MEETING_MY_VARIANTS = [
    "河边坐着另一个旅者,你们点了下头。各自看各自的水。",
    "远处有个人影,朝你这边看了一会儿,又转身走了。",
    "你看见另一个人的背影,在路的尽头拐了个弯。",
    "有人坐在石头上,你路过时他抬了下头。你们谁也没开口。",
    "路上有另一个人的脚印,方向和你相反。",
]

_MEETING_THEIR_VARIANTS = [
    "路边有个人在歇脚,走过时互相看了一眼。没有说话。",
    "远处有个人影朝你走来,又转向另一条路去了。",
    "有人从你来的方向走过来,你们错身而过。",
    "河边有人站着看了一会儿水,然后走了。",
    "路上有个旅者经过,你们没有打招呼。",
]


# ── @ Messaging ───────────────────────────────────────────────────────

def send_at_message(from_name: str, to_name: str, place: str) -> None:
    """Queue a @name message for delivery on recipient's next open_door."""
    if not is_enabled():
        return
    with _io_lock:
        data = _load_json(_messages_path())
        msgs = data.get(to_name, [])
        msgs.append({
            "from": from_name,
            "place": place,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        data[to_name] = msgs[-50:]
        _save_json(_messages_path(), data)


def check_at_messages(name: str, rng) -> str | None:
    """Check and consume pending @messages for this traveler.

    Returns a hint text or None.
    """
    if not is_enabled():
        return None
    with _io_lock:
        data = _load_json(_messages_path())
        msgs = data.get(name, [])
        if not msgs:
            return None
        # Consume one
        msg = msgs.pop(0)
        data[name] = msgs
        _save_json(_messages_path(), data)
    place = msg.get("place", "某个地方")
    return rng.choice(_AT_HINT_VARIANTS).format(place=place)


_AT_HINT_VARIANTS = [
    "土里有人给你留了话,在{place}。是谁,得自己去看。",
    "有人说给你带了句话,指向{place}。去看看。",
    "{place}那边有个人找过你。去看看吧。",
]


# ── walk_alone: per-journey opt-out ───────────────────────────────────

def walk_alone_active(state) -> bool:
    """Check if the current journey has walk_alone enabled."""
    return getattr(state, "cotraveler_alone", False)


def set_walk_alone(state, value: bool) -> None:
    """Set walk_alone for the current journey."""
    state.cotraveler_alone = value
