"""Shared filesystem/config helpers — no domain logic, importable from anywhere."""

from __future__ import annotations

import os
import pathlib


def _get_home() -> pathlib.Path:
    """Return the NOWHERE_HOME data directory (default ``~/.nowhere``).

    A8: single source of truth — notebook / placememory / state all resolve
    their on-disk home through here.  Reads the environment at call time so
    tests can ``monkeypatch.setenv("NOWHERE_HOME", ...)``.
    """
    home = os.environ.get("NOWHERE_HOME", "").strip()
    if home:
        # 展开并绝对化: "~" 与相对路径会随 cwd 漂移, 破坏共享根目录的确定性
        p = pathlib.Path(home).expanduser()
        return p if p.is_absolute() else pathlib.Path.cwd() / p
    return pathlib.Path.home() / ".nowhere"

