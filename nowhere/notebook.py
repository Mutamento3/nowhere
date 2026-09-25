"""旅行手账——五册自然志。

五册: flora(植物) / fauna(动物) / radio(电台) / water(水文) / people(遇见的人)
每条: {name, place, at, first_impression}
first_impression: 初见印象,由渲染层用当场上下文从变体池生成,不调 LLM。

存储: notebook.json(NOWHERE_HOME 下,全局跨旅程)
上限: 每册 200 条 FIFO(丢最旧,"只有一次的"永不丢)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _stable_seed(*parts: object) -> int:
    """Deterministic RNG seed (BND-07: builtin hash() is per-process random)."""
    raw = "|".join(str(p) for p in parts)
    return int(hashlib.md5(raw.encode("utf-8")).hexdigest(), 16)


# ── 文件路径 ─────────────────────────────────────────────────────────

from nowhere.util import _get_home


def _notebook_path() -> Path:
    # A8: home resolution is shared with placememory / state (nowhere.util).
    return _get_home() / "notebook.json"


# ── 原子读写 ─────────────────────────────────────────────────────────

def _load_notebook() -> dict:
    p = _notebook_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = None
    if not isinstance(data, dict):
        # ERR-05: 合法但非对象的 JSON(list/str/数字)同样按损坏处理 ——
        # 直接返回会被下一次 _save_notebook 静默覆盖为 {}, 数据不可恢复
        backup = p.with_name(p.name + ".corrupt")
        n = 0
        while backup.exists():
            n += 1
            backup = p.with_name(f"{p.name}.corrupt.{n}")
        try:
            p.replace(backup)
        except OSError:
            pass  # intentionally ignored: backup failure, still return empty
        return {}
    return data


def _save_notebook(data: dict) -> None:
    p = _notebook_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    fd, tmp_name = tempfile.mkstemp(prefix="notebook-", suffix=".tmp", dir=str(p.parent))
    try:
        try:
            tmp = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)  # fdopen 失败时 fd 未移交 with, 显式关闭防泄漏
            raise
        with tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, str(p))
    except BaseException:
        # 清理自身不许抛(遮蔽原始异常); 非 OSError(如 UnicodeEncodeError)也别留残留
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ── 常量 ─────────────────────────────────────────────────────────────

VOLUMES = ("flora", "fauna", "radio", "water", "people")
_VOLUME_CAP = 200

_VOLUME_NAMES = {
    "flora": "植物册",
    "fauna": "动物册",
    "radio": "电台册",
    "water": "水文册",
    "people": "人物册",
}

# ── 变体池: 6 句/册, 槽位 {0}=name {1}=weather {2}=time {3}=action ──

_FLORA_VARIANTS: list[str] = [
    "{0}藏在{1}底下,蹲下去才看见。",
    "{0},叶子上还挂着{1}的雨。",
    "{2}的光打在{0}上,颜色比照片里深。",
    "脚边是{0},{3}的时候差点踩到。",
    "{1}里{0}开了,不多,就几株。",
    "{0}长在路边,{2}的时候路过,没注意。",
]

_FAUNA_VARIANTS: list[str] = [
    "{0}在{1}里待着,你动了一下,它也动了。",
    "{2}遇到{0},离得不远,它没跑。",
    "{0}从面前过去,{3}的时候吓了一跳。",
    "{1}里{0}出现了,停了一下又走了。",
    "看见{0}的时候是{2},它在{3}。",
    "{0}在{1}里,{2}的光打在身上,看得清楚。",
]

_RADIO_VARIANTS: list[str] = [
    "转到这个频率的时候正好在放{1}的歌。",
    "{2}收到这个台,信号不太稳,但没换。",
    "电台在{1}里断断续续,{2}调到的。",
    "收音机里出来一段{1},不知道是什么语言。",
    "{2},{0}的信号从{1}里钻出来。",
    "这个台是{2}找到的,放的是{1}。",
]

_WATER_VARIANTS: list[str] = [
    "远远看见{0}的水面,{2}的光打在上面。",
    "走到{0}边上,{1}里水声很清楚。",
    "{0}在{1}里,{2}的时候水面有纹路。",
    "站在{0}旁边,{3}的时候水拍了一下岸。",
    "{2}的{0},{1}里看不太远,但水声一直在。",
    "{0}的水是{1}的颜色,{2}看的。",
]

_PEOPLE_VARIANTS: list[str] = [
    "遇见{0}的时候是{2},{1}里站着个人。",
    "{0}在{1}里,{2}的时候碰见的。",
    "{0},{2}遇到的,当时在{3}。",
    "和{0}说话的时候是{2},{1}把声音盖了一半。",
    "{0}在路边,{2}的时候看了一眼,对上了。",
    "{1}里{0}出现了,{2},没说几句。",
]

_VARIANT_POOLS: dict[str, list[str]] = {
    "flora": _FLORA_VARIANTS,
    "fauna": _FAUNA_VARIANTS,
    "radio": _RADIO_VARIANTS,
    "water": _WATER_VARIANTS,
    "people": _PEOPLE_VARIANTS,
}

# ── 空册文案(3 变体/册) ─────────────────────────────────────────────

_EMPTY_VARIANTS: dict[str, list[str]] = {
    "flora": [
        "植物册还空着。走走看,路边总有什么在长。",
        "植物册翻开来是白的。世界那么多草木,还没记下一笔。",
        "植物册没有一页。等走到有花的地方,再打开。",
    ],
    "fauna": [
        "动物册空着。林子里有声音,但还没看见影子。",
        "动物册一页也没有。等遇见什么活的东西再写。",
        "动物册空白的。世界那么大,总有什么在跑。",
    ],
    "radio": [
        "电台一册还空着。世界那么多台。",
        "电台册没有一页。收音机还没响过。",
        "电台册空的。等调到一个频率再记。",
    ],
    "water": [
        "水文册空着。还没走到水边。",
        "水文册一页也没有。等看见水再写。",
        "水文册空白的。世界那么多河,总有一条在等。",
    ],
    "people": [
        "人物册空着。还没遇见谁。",
        "人物册没有一页。路上应该有人的。",
        "人物册空的。等遇见人再记。",
    ],
}


# ── 辅助函数 ─────────────────────────────────────────────────────────

def _weather_word(weather: dict | None) -> str:
    """天气→适合填进模板的词。"""
    if not weather:
        return "风里"
    precip = (weather.get("precip") or "none").lower()
    if precip in ("rain", "drizzle"):
        return "雨里"
    if precip == "snow":
        return "雪里"
    if precip == "storm":
        return "暴雨里"
    wind = weather.get("wind_ms")
    if wind is not None and wind > 8:
        return "大风里"
    if wind is not None and wind > 4:
        return "风里"
    temp = weather.get("temp_c")
    if temp is not None and temp > 33:
        return "热浪里"
    if temp is not None and temp < 0:
        return "冷风里"
    cloud = weather.get("cloud")
    if cloud is not None and cloud > 70:
        return "阴天"
    return "晴天"


def _time_word(dt: datetime | None, lat: float = 30.0, lon: float = 120.0) -> str:
    """时段→适合填进模板的词。"""
    if dt is None:
        return "傍晚"
    try:
        from timezonefinder import TimezoneFinder
        from zoneinfo import ZoneInfo
        tf = TimezoneFinder()
        tz_name = tf.timezone_at(lat=lat, lng=lon)
        if tz_name:
            dt = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        pass  # intentionally ignored: timezone lookup failure, falls back to UTC
    h = dt.hour
    if 5 <= h < 8:
        return "清晨"
    if 8 <= h < 12:
        return "上午"
    if 12 <= h < 14:
        return "中午"
    if 14 <= h < 18:
        return "下午"
    if 18 <= h < 20:
        return "傍晚"
    return "夜里"


def _action_word(weather: dict | None, time_word: str) -> str:
    """天气+时段→动作词。"""
    if not weather:
        return "路过"
    precip = (weather.get("precip") or "none").lower()
    if precip in ("rain", "drizzle"):
        return "躲雨"
    if precip == "snow":
        return "踩雪"
    wind = weather.get("wind_ms")
    if wind is not None and wind > 8:
        return "顶风走"
    temp = weather.get("temp_c")
    if temp is not None and temp > 33:
        return "擦汗"
    if temp is not None and temp < 0:
        return "搓手"
    if time_word == "夜里":
        return "摸黑走"
    return "走路"


def _compute_season(month: int, lat: float) -> str:
    """从月份和纬度推季节。南半球翻转。"""
    if month in (3, 4, 5):
        s = "春天"
    elif month in (6, 7, 8):
        s = "夏天"
    elif month in (9, 10, 11):
        s = "秋天"
    else:
        s = "冬天"
    if lat < 0:
        flip = {"春天": "秋天", "夏天": "冬天", "秋天": "春天", "冬天": "夏天"}
        s = flip[s]
    return s


def _generate_first_impression(
    volume: str,
    name: str,
    env: dict | None,
    lat: float = 30.0,
    lon: float = 120.0,
) -> str | None:
    """从变体池生成初见印象。env 里有时用 env,没有就用默认值。"""
    pool = _VARIANT_POOLS.get(volume)
    if not pool:
        return None

    import random
    rng = random.Random(_stable_seed(volume, name, datetime.now(timezone.utc).isoformat()[:13]))

    weather = (env or {}).get("weather") if env else None
    dt = (env or {}).get("_dt")
    if dt is None:
        dt = datetime.now(timezone.utc)

    w_word = _weather_word(weather)
    t_word = _time_word(dt, lat, lon)
    a_word = _action_word(weather, t_word)

    template = rng.choice(pool)
    try:
        return template.format(name, w_word, t_word, a_word)
    except (IndexError, KeyError):
        # 同参数原样重试必然复现异常; 降级为不依赖占位符的文本并留日志
        logger.warning("first impression template failed for %s/%s", volume, name)
        return f"{name}。"


# ── 核心记录函数 ─────────────────────────────────────────────────────

def record(
    volume: str,
    name: str,
    place: str,
    first_impression: str | None = None,
    sim_time: datetime | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> None:
    """记一笔到指定册。FIFO 上限 200,"只有一次的"永不丢。"""
    if volume not in VOLUMES:
        return
    if not name:
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    entry: dict[str, Any] = {
        "name": name,
        "place": place or "",
        "at": now_iso,
        "sim_time": sim_time.isoformat() if sim_time is not None else None,
        "first_impression": first_impression,
        # 渲染层算季节要用当场坐标; 老条目没有这两个键, 渲染侧回退 30N
        "lat": lat,
        "lon": lon,
    }

    data = _load_notebook()
    vol_list = data.get(volume, [])
    uniques = data.get("uniques", {})
    vol_uniques = uniques.get(volume, [])

    # 添加新条目
    vol_list.append(entry)

    # FIFO: 超限时从最旧的开始丢
    # 唯一条目(count=1)搬进 uniques 永不丢,非唯一直接丢
    while len(vol_list) > _VOLUME_CAP:
        # 统计每个 name 的出现次数: 必须合并 uniques 里已永久保留的同名
        # 条目, 否则 X 进过 uniques 后再记一次, 溢出时会被再次判"唯一",
        # uniques 重复增长且"只有一次"语义被破坏
        name_counts: dict[str, int] = {}
        for e in vol_list:
            n = e.get("name", "")
            name_counts[n] = name_counts.get(n, 0) + 1
        for e in vol_uniques:
            n = e.get("name", "")
            name_counts[n] = name_counts.get(n, 0) + 1

        # 从最旧的(第一个)开始检查
        oldest_name = vol_list[0].get("name", "")
        if name_counts.get(oldest_name, 0) <= 1:
            # 唯一条目: 搬进 uniques
            vol_uniques.append(vol_list.pop(0))
        else:
            # 非唯一: 直接丢弃
            vol_list.pop(0)

    data[volume] = vol_list
    uniques[volume] = vol_uniques
    data["uniques"] = uniques
    _save_notebook(data)


def record_with_env(
    volume: str,
    name: str,
    place: str,
    env: dict | None,
    lat: float = 30.0,
    lon: float = 120.0,
) -> None:
    """记录一笔,自动生成 first_impression。"""
    fi = _generate_first_impression(volume, name, env, lat, lon)
    _dt = (env or {}).get("_dt") if env else None
    record(
        volume, name, place, fi,
        sim_time=_dt if isinstance(_dt, datetime) else None,
        lat=lat, lon=lon,
    )


# ── 查询函数 ─────────────────────────────────────────────────────────

def _volume_entries(volume: str) -> tuple[list[dict], list[dict]]:
    """返回 (主列表, 唯一保留列表)。"""
    data = _load_notebook()
    return data.get(volume, []), data.get("uniques", {}).get(volume, [])


def _all_entries() -> dict[str, tuple[list[dict], list[dict]]]:
    """所有册的 (主列表, 唯一保留列表)。"""
    data = _load_notebook()
    uniques = data.get("uniques", {})
    result = {}
    for v in VOLUMES:
        result[v] = (data.get(v, []), uniques.get(v, []))
    return result


def _time_ago(iso_str: str) -> str:
    """ISO 时间→'昨天''三天前'等。"""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        days = delta.days
        if days == 0:
            hours = delta.seconds // 3600
            if hours == 0:
                return "刚才"
            return f"{hours}小时前"
        if days == 1:
            return "昨天"
        if days < 7:
            return f"{days}天前"
        if days < 30:
            return f"{days // 7}周前"
        if days < 365:
            return f"{days // 30}个月前"
        return f"{days // 365}年前"
    except Exception:
        logger.warning("time_ago parse failed for %r", iso_str, exc_info=True)
        return "不久前"


# ── notebook() 输出 ──────────────────────────────────────────────────

def notebook(volume: str | None = None) -> str:
    """旅行手账输出。

    volume=None: 列出所有册概况
    volume=册名: 指定册全列
    """
    if volume and volume in VOLUMES:
        return _render_volume(volume)
    return _render_overview()


def _render_overview() -> str:
    """所有册概况。"""
    all_data = _all_entries()
    parts: list[str] = []

    for v in VOLUMES:
        main_list, unique_list = all_data[v]
        total = len(main_list) + len(unique_list)
        vname = _VOLUME_NAMES[v]

        if total == 0:
            empty_variants = _EMPTY_VARIANTS.get(v, ["空的。"])
            import random
            rng = random.Random(_stable_seed(v))
            parts.append(rng.choice(empty_variants))
            continue

        # 第一笔: FIFO 从主列表队首淘汰, 进过 uniques 的条目必然比
        # 主列表现存所有条目更早, 全册最早的一笔在 unique_list[0]
        first = unique_list[0] if unique_list else (main_list[0] if main_list else None)
        first_name = first.get("name", "") if first else ""
        first_place = first.get("place", "") if first else ""
        first_season = _entry_season(first) if first else ""

        # 最近一笔
        last = main_list[-1] if main_list else None
        last_ago = _time_ago(last.get("at", "")) if last else ""

        # 组装
        line = f"{vname},{total}笔。"
        if first_name:
            if first_place and first_season:
                line += f"第一笔是{first_name},{first_place},那时是{first_season}。"
            elif first_place:
                line += f"第一笔是{first_name},{first_place}。"
            else:
                line += f"第一笔是{first_name}。"
        if last_ago:
            line += f"最近一笔是{last_ago}。"
        parts.append(line)

    return "\n".join(parts)


def _render_volume(volume: str) -> str:
    """指定册全列。每条两行。"""
    main_list, unique_list = _volume_entries(volume)
    vname = _VOLUME_NAMES[volume]

    if not main_list and not unique_list:
        empty_variants = _EMPTY_VARIANTS.get(volume, ["空的。"])
        import random
        rng = random.Random(_stable_seed(volume))
        return rng.choice(empty_variants)

    lines: list[str] = []

    # 主列表
    for entry in main_list:
        lines.extend(_format_entry(entry))

    # 唯一保留条目(被 FIFO 丢掉但 count=1 的)
    if unique_list:
        lines.append("")
        lines.append("——曾经记下又翻过去的一页——")
        for entry in unique_list:
            lines.extend(_format_entry(entry))

    return "\n".join(lines)


def _entry_season(entry: dict) -> str:
    """条目季节: 优先用持久化的 lat/lon 转本地月份再算(南半球翻转可达);
    老条目没有坐标时回退 30N/UTC 月。"""
    at = entry.get("at", "")
    if not at:
        return ""
    try:
        dt = datetime.fromisoformat(at)
    except (ValueError, TypeError):
        return ""
    lat = entry.get("lat")
    lon = entry.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        try:
            from timezonefinder import TimezoneFinder
            from zoneinfo import ZoneInfo
            tz_name = TimezoneFinder().timezone_at(lat=float(lat), lng=float(lon))
            if tz_name:
                dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass  # intentionally ignored: tz lookup failure, falls back to UTC month
        return _compute_season(dt.month, float(lat))
    return _compute_season(dt.month, 30.0)


def _format_entry(entry: dict) -> list[str]:
    """单条目渲染(主列表与 uniques 共用, 防两分支漂移)。"""
    name = entry.get("name", "")
    place = entry.get("place", "")
    season = _entry_season(entry)

    parts = [name]
    if place and season:
        parts.append(f"{place},{season}")
    elif place:
        parts.append(place)
    elif season:
        parts.append(season)
    lines = ["——".join(parts) if len(parts) > 1 else name]
    fi = entry.get("first_impression")
    if fi:
        lines.append(f"  {fi}")
    return lines


def notebook_unique_section() -> str:
    """"只有一次的"——跨册合并 count=1 的条目。"""
    all_data = _all_entries()
    singles: list[str] = []

    for v in VOLUMES:
        main_list, unique_list = all_data[v]
        # 计数必须合并 uniques: 只在 uniques 里出现过的名字并非"只遇到过
        # 一次"(它被搬进 uniques 恰恰说明只遇过一次, 但之后主列表再出现
        # 同名时, 主列表计数为 1 会误判成唯一) —— 两处相加才是真实次数
        name_counts: dict[str, int] = {}
        for e in main_list:
            n = e.get("name", "")
            name_counts[n] = name_counts.get(n, 0) + 1
        for e in unique_list:
            n = e.get("name", "")
            name_counts[n] = name_counts.get(n, 0) + 1

        # 主列表中真实 count=1 的
        for e in main_list:
            if name_counts.get(e.get("name", ""), 0) == 1:
                fi = e.get("first_impression")
                label = e.get("name", "")
                place = e.get("place", "")
                if place:
                    label = f"{label}({place})"
                if fi:
                    singles.append(f"{label}——{fi}")
                else:
                    singles.append(label)

        # 唯一保留条目
        for e in unique_list:
            fi = e.get("first_impression")
            label = e.get("name", "")
            place = e.get("place", "")
            if place:
                label = f"{label}({place})"
            if fi:
                singles.append(f"{label}——{fi}")
            else:
                singles.append(label)

    if not singles:
        return "还没有只遇到过一次的。"

    return "只遇到过一次的:\n" + "\n".join(f"  {s}" for s in singles)
