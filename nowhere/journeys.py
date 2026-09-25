"""Multi-journey management for nowhere.

Stores each journey as a separate JSON file under ~/.nowhere/journeys/.
An index.json tracks the active journey and metadata for all journeys.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import tempfile
from datetime import datetime, timezone

from nowhere.state import WorldState

logger = logging.getLogger(__name__)

_CONTINENT_MAP: dict[str, str] = {
    # Asia
    "CN": "亚洲", "JP": "亚洲", "KR": "亚洲", "KP": "亚洲", "MN": "亚洲",
    "IN": "亚洲", "TH": "亚洲", "VN": "亚洲", "MY": "亚洲", "SG": "亚洲",
    "ID": "亚洲", "PH": "亚洲", "MM": "亚洲", "KH": "亚洲", "LA": "亚洲",
    "NP": "亚洲", "BD": "亚洲", "LK": "亚洲", "PK": "亚洲", "AF": "亚洲",
    "IR": "亚洲", "IQ": "亚洲", "TR": "亚洲", "SA": "亚洲", "AE": "亚洲",
    "IL": "亚洲", "JO": "亚洲", "LB": "亚洲", "SY": "亚洲",
    "KZ": "亚洲", "UZ": "亚洲", "TM": "亚洲", "KG": "亚洲", "TJ": "亚洲",
    "GE": "亚洲", "AM": "亚洲", "AZ": "亚洲",
    # Europe
    "GB": "欧洲", "FR": "欧洲", "DE": "欧洲", "IT": "欧洲", "ES": "欧洲",
    "PT": "欧洲", "NL": "欧洲", "BE": "欧洲", "CH": "欧洲", "AT": "欧洲",
    "SE": "欧洲", "NO": "欧洲", "FI": "欧洲", "DK": "欧洲", "IS": "欧洲",
    "PL": "欧洲", "CZ": "欧洲", "SK": "欧洲", "HU": "欧洲", "RO": "欧洲",
    "BG": "欧洲", "GR": "欧洲", "HR": "欧洲", "RS": "欧洲", "UA": "欧洲",
    "BY": "欧洲", "LT": "欧洲", "LV": "欧洲", "EE": "欧洲",
    "SI": "欧洲", "BA": "欧洲", "ME": "欧洲", "MK": "欧洲", "AL": "欧洲",
    "MD": "欧洲", "IE": "欧洲", "LU": "欧洲",
    "RU": "欧洲",
    # North America
    "US": "北美洲", "CA": "北美洲", "MX": "北美洲", "CU": "北美洲",
    "JM": "北美洲", "HT": "北美洲", "DO": "北美洲", "GT": "北美洲",
    "HN": "北美洲", "SV": "北美洲", "NI": "北美洲", "CR": "北美洲", "PA": "北美洲",
    "BS": "北美洲", "BZ": "北美洲", "GL": "北美洲",
    # South America
    "BR": "南美洲", "AR": "南美洲", "CL": "南美洲", "PE": "南美洲",
    "CO": "南美洲", "VE": "南美洲", "EC": "南美洲", "BO": "南美洲",
    "PY": "南美洲", "UY": "南美洲", "GY": "南美洲", "SR": "南美洲",
    # Africa
    "EG": "非洲", "ZA": "非洲", "NG": "非洲", "KE": "非洲", "ET": "非洲",
    "MA": "非洲", "TN": "非洲", "DZ": "非洲", "TZ": "非洲", "UG": "非洲",
    "GH": "非洲", "SN": "非洲", "ML": "非洲", "NE": "非洲", "TD": "非洲",
    "CM": "非洲", "CD": "非洲", "CG": "非洲", "AO": "非洲", "ZM": "非洲",
    "ZW": "非洲", "MZ": "非洲", "MG": "非洲", "NA": "非洲", "BW": "非洲",
    "SD": "非洲", "LY": "非洲", "SO": "非洲",
    # Oceania
    "AU": "大洋洲", "NZ": "大洋洲", "FJ": "大洋洲", "PG": "大洋洲",
    "SB": "大洋洲", "VU": "大洋洲", "WS": "大洋洲", "TO": "大洋洲",
}

_JOURNEYS_DIR = pathlib.Path(
    os.environ.get("NOWHERE_HOME") or str(pathlib.Path.home() / ".nowhere")
) / "journeys"
_INDEX_FILE = _JOURNEYS_DIR / "index.json"


def _slug(place_name: str, force_new: bool = False) -> str:
    """Normalize place name to a filesystem-safe slug.

    Card 82: if force_new=True and the slug already exists in the index
    (or on disk), append a numeric suffix (-2, -3, ...) to avoid collision.
    """
    s = place_name.strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w一-鿿-]", "", s)
    base = s or "unknown"
    if not force_new:
        return base
    # Check index AND disk: 索引可能损坏/落后于磁盘(手工放入的文件、被重建),
    # 只查索引会让"强制新建"静默覆盖已存在的 <base>.json
    index = _load_index()
    existing_slugs = {j.get("slug") for j in index.get("journeys", [])}
    if base not in existing_slugs and not _journey_path(base).exists():
        return base
    n = 2
    while f"{base}-{n}" in existing_slugs or _journey_path(f"{base}-{n}").exists():
        n += 1
    return f"{base}-{n}"


def _ensure_dir() -> None:
    _JOURNEYS_DIR.mkdir(parents=True, exist_ok=True)


def _default_index() -> dict:
    return {"active": None, "journeys": []}


def _rebuild_index_from_disk() -> dict:
    """扫描旅程目录, 尽力重建索引条目(元数据以文件内容为准)。"""
    index = _default_index()
    if not _JOURNEYS_DIR.exists():
        return index
    best: tuple[str, int] | None = None
    for p in sorted(_JOURNEYS_DIR.glob("*.json")):
        slug = p.stem
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            steps = len(data.get("path") or [])
        except TypeError:
            steps = 0
        landed_at = data.get("landed_at")
        index["journeys"].append({
            "slug": slug,
            "place_name": data.get("place_name") or slug,
            "landed_at": landed_at if isinstance(landed_at, str) else "",
            "last_active": "",
            "departed_at": "",
            "steps": steps,
            "last_text": (data.get("last_text") or "")[:50],
        })
        if best is None or steps > best[1]:
            best = (slug, steps)
    if best is not None:
        index["active"] = best[0]
    return index


def _load_index() -> dict:
    """Load or initialize the journey index.

    损坏时先把原文件改名 .bak 留证, 再从磁盘旅程文件重建 —— 直接返回空索引
    会让下一次 _save_index 用单条记录整体覆盖, 全部旅程元数据不可恢复。
    """
    if _INDEX_FILE.exists():
        try:
            data = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("journeys"), list):
            # 归一化: 只信 dict/list 形状, 消费端不必各自容错
            data["journeys"] = [
                j for j in data["journeys"]
                if isinstance(j, dict) and isinstance(j.get("slug"), str)
            ]
            if not isinstance(data.get("active"), (str, type(None))):
                data["active"] = None
            return data
        try:
            _INDEX_FILE.replace(_INDEX_FILE.with_name("index.json.bak"))
        except OSError as exc:
            logger.warning("损坏的 index.json 改名备份失败: %s", exc)
        logger.warning("index.json 损坏, 已备份为 index.json.bak 并从磁盘重建索引")
        return _rebuild_index_from_disk()
    return _default_index()


def _atomic_write_text(path: pathlib.Path, text: str) -> None:
    """同目录写临时文件后 os.replace 原子替换, 防止半截 JSON 落盘。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)  # fdopen 失败时 fd 未移交, 显式关闭防泄漏
            raise
        with f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass  # 清理失败不许遮蔽原始异常
        raise


def _save_index(index: dict) -> None:
    """Persist the journey index."""
    _atomic_write_text(
        _INDEX_FILE,
        json.dumps(index, ensure_ascii=False, indent=2),
    )


def _journey_path(slug: str) -> pathlib.Path:
    return _JOURNEYS_DIR / f"{slug}.json"


def save_current(state: WorldState, force_new: bool = False) -> None:
    """Save the current state as a journey file and update the index.

    Card 82: force_new=True creates a new journey with suffixed slug
    even if one with the same place name already exists.
    """
    _ensure_dir()
    place = state.place_name or "unknown"
    # Card 82: check transient flag on state as well
    if getattr(state, "force_new_slug", False):
        force_new = True
        state.force_new_slug = False  # consume the flag
    # 优先用 state 上持久化的所属 slug: 带后缀的旅程(上海-2)若每次都从
    # place_name 重新推导, force_new 标志只消费一次, 之后会回落到"上海"
    # 并覆盖同名旧旅程的文件与索引
    persisted_slug = getattr(state, "journey_slug", None)
    if persisted_slug and not force_new:
        slug = persisted_slug
    else:
        slug = _slug(place, force_new=force_new)
    state.journey_slug = slug
    path = _journey_path(slug)

    # Save state
    data = state.to_dict()
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))

    # Update index
    index = _load_index()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Find existing entry
    existing = None
    for j in index["journeys"]:
        if j["slug"] == slug:
            existing = j
            break

    if existing:
        existing["last_active"] = now_iso
        existing["departed_at"] = now_iso
        existing["steps"] = len(state.path)
        existing["last_text"] = (state.last_text or "")[:50]
    else:
        index["journeys"].append({
            "slug": slug,
            "place_name": place,
            "landed_at": state.landed_at.isoformat() if state.landed_at else now_iso,
            "last_active": now_iso,
            "departed_at": now_iso,
            "steps": len(state.path),
            "last_text": (state.last_text or "")[:50],
        })

    index["active"] = slug
    _save_index(index)


def list_journeys() -> list[dict]:
    """List all saved journeys with metadata."""
    index = _load_index()
    return index.get("journeys", [])


def get_active_slug() -> str | None:
    """Return the active journey slug, or None."""
    return _load_index().get("active")


def switch(slug_or_place: str) -> WorldState | None:
    """Switch to a journey by slug or exact place name. Returns WorldState or None.

    Card 68: only exact match — no substring matching.
    "上海" must NOT match "长江上海段".
    """
    index = _load_index()
    target = _slug(slug_or_place)

    # Try exact slug match
    for j in index["journeys"]:
        if j["slug"] == target:
            return _load_journey(j["slug"], index)

    # Try exact place_name match (case-insensitive)
    query_lower = slug_or_place.strip().lower()
    for j in index["journeys"]:
        if j.get("place_name", "").strip().lower() == query_lower:
            return _load_journey(j["slug"], index)

    return None


def get_journey_meta(slug_or_place: str) -> dict | None:
    """Return index metadata for a journey, or None if not found."""
    index = _load_index()
    target = _slug(slug_or_place)

    # Try exact slug match first
    for j in index["journeys"]:
        if j["slug"] == target:
            return j

    # Try exact match (case-insensitive)
    slug_or_lower = slug_or_place.strip().lower()
    for j in index["journeys"]:
        if j.get("place_name", "").strip().lower() == slug_or_lower:
            return j

    return None


def _load_journey(slug: str, index: dict) -> WorldState | None:
    """Load a journey file and set it as active.

    Validates that the loaded state's place_name matches the index entry
    to prevent cross-contamination (e.g. file overwritten by a different journey).
    """
    path = _journey_path(slug)
    if not path.exists():
        return None
    # 只把"文件解析/反序列化失败"折叠成 None; 索引写盘错误不在此列,
    # 否则磁盘满/权限问题会被调用方误判为"旅程不存在"而走新建落地
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = WorldState.from_dict(data)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError, TypeError) as exc:
        logger.warning("旅程 %s 加载失败: %s", slug, exc)
        return None
    # 无条件比较: 空值双方经 _slug 都归一为 'unknown', 天然一致不误报;
    # 原"任一侧为空就跳过校验"恰好放过了被覆盖成空地名的旅程文件
    expected_place = ""
    for j in index.get("journeys", []):
        if j.get("slug") == slug:
            expected_place = j.get("place_name", "") or ""
            break
    if _slug(state.place_name or "unknown") != _slug(expected_place or "unknown"):
        return None  # cross-contamination detected
    state.journey_slug = slug
    index["active"] = slug
    _save_index(index)
    return state


_SLUG_SAFE_RE = re.compile(r"[\w\u4e00-\u9fff-]+")


def delete(slug: str) -> bool:
    """Delete a journey file.

    Returns True only if a file was actually deleted; False when the slug
    is illegal (可能携带路径) or the journey file did not exist.
    """
    # 本模块唯一绕过 _slug() 白名单的入口, 校验后再拼路径, 防 '../../x'
    # 之类输入拼出 journeys 目录之外的文件路径
    if not re.fullmatch(_SLUG_SAFE_RE, slug):
        return False
    path = _journey_path(slug)
    deleted = False
    if path.exists():
        path.unlink()
        deleted = True
    index = _load_index()
    index["journeys"] = [
        j for j in index.get("journeys", []) if j.get("slug") != slug
    ]
    if index.get("active") == slug:
        index["active"] = None
    _save_index(index)
    return deleted


def atlas() -> dict:
    """聚合全部旅程: 地方数、大洲数、极端方向。

    Returns dict with keys: places, continents, extremes.
    """
    from nowhere.country import country_code_of

    index = _load_index()
    journeys_list = index.get("journeys", [])

    if not journeys_list:
        return {"places": 0, "continents": 0, "extremes": {}}

    places: list[dict] = []
    for j in journeys_list:
        slug = j.get("slug")
        place_name = j.get("place_name", "")
        path = _journey_path(slug)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                pos = data.get("pos")
                if pos and len(pos) >= 2:
                    places.append({
                        "name": place_name,
                        "lat": pos[0],
                        "lon": pos[1],
                    })
            except Exception:
                continue  # intentionally ignored: geocode lookup failure, skip place

    if not places:
        return {"places": 0, "continents": 0, "extremes": {}}

    # Continent count
    continents: set[str] = set()
    for p in places:
        cc = country_code_of(p["lat"], p["lon"])
        if cc:
            cont = _CONTINENT_MAP.get(cc, "")
            if cont:
                continents.add(cont)

    # Extremes
    extremes: dict[str, dict] = {}
    extremes["north"] = max(places, key=lambda p: p["lat"])
    extremes["south"] = min(places, key=lambda p: p["lat"])
    extremes["east"] = max(places, key=lambda p: p["lon"])
    extremes["west"] = min(places, key=lambda p: p["lon"])

    return {
        "places": len(places),
        "continents": len(continents),
        "extremes": extremes,
    }
