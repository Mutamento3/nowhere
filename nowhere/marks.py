"""Local bookmark storage — save / list / get named places."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _marks_path() -> Path:
    """Return the path to marks.json, reading NOWHERE_HOME on every call."""
    base = os.environ.get("NOWHERE_HOME") or str(Path.home() / ".nowhere")
    return Path(base) / "marks.json"


def _load() -> list[dict]:
    """读取书签: 文件不存在 → []; 损坏/读失败 → 向上抛。

    解析失败绝不能静默当空列表返回 —— save() 会以空结果为基础写回,
    一次读坏等于全部书签静默销毁。损坏文件先备份改名留证再抛。
    """
    p = _marks_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        backup = p.with_name(f"marks.json.corrupt_{datetime.now():%Y%m%d_%H%M%S}")
        try:
            p.replace(backup)
            logger.warning("marks.json 损坏, 已备份为 %s", backup.name)
        except OSError as backup_exc:
            logger.warning("marks.json 损坏且备份改名失败: %s", backup_exc)
        raise ValueError(f"marks.json 损坏, 不予覆盖写入: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("marks.json 顶层不是 list, 不予覆盖写入")
    return data


def _dump(marks: list[dict]) -> None:
    """同目录临时文件 + os.replace 原子替换, 防半截 JSON。"""
    p = _marks_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="marks.", suffix=".tmp", dir=p.parent)
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)  # fdopen 失败时 fd 未移交, 显式关闭防泄漏
            raise
        with f:
            f.write(json.dumps(marks, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass  # 清理失败不许遮蔽原始异常
        raise


# ── Public API ────────────────────────────────────────────────────

def save(name: str, lat: float, lon: float, note: str = "", overwrite: bool = False) -> None:
    """Save a bookmark by *name*.

    Raises ``ValueError`` if *name* already exists and *overwrite* is ``False``.
    """
    marks = _load()
    existing = [m for m in marks if isinstance(m, dict) and m.get("name") == name]
    if existing and not overwrite:
        raise ValueError(f"「{name}」已经标过了。要覆盖的话用 mark 的覆盖选项。")
    entry = {
        "name": name,
        "lat": lat,
        "lon": lon,
        "note": note,
        "marked_at": datetime.now(timezone.utc).isoformat(),
    }
    # 用户可编辑的外部数据: 非 dict/缺 name 的条目跳过比较但不丢弃
    marks = [
        m for m in marks
        if not (isinstance(m, dict) and m.get("name") == name)
    ]
    marks.append(entry)
    _dump(marks)


def all() -> list[dict]:  # noqa: A001 — shadows builtin intentionally
    """Return every saved bookmark."""
    return _load()


def get(name: str) -> dict | None:  # noqa: A001
    """Return the bookmark with *name*, or ``None``."""
    for m in _load():
        if isinstance(m, dict) and m.get("name") == name:
            return m
    return None
