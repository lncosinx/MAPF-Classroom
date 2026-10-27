"""LaCAM3 的 ctypes 封装（库由 native/lacam 编译）。

run_lacam 不是线程安全的：结果放在 static std::string 里，Planner::* 是全局静态参数，
而 ctypes 调用期间会释放 GIL —— 所以所有调用都经过同一把全局锁（不变式 12）。
本模块的输入输出都是 unpadded (row, col)；LaCAM 场景文件是**先列后行**，只在这里换算。
"""
from __future__ import annotations

import ctypes
import sys
import threading
from pathlib import Path
from typing import Optional

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
_LIB_NAME = "lacam.dll" if sys.platform == "win32" else ("liblacam.dylib" if sys.platform == "darwin" else "liblacam.so")
# 打包后放 native/bin，开发时在 build/lacam
_SEARCH = [ROOT / "native" / "bin" / _LIB_NAME, ROOT / "build" / "lacam" / _LIB_NAME,
           ROOT / "build" / "lacam" / "Release" / _LIB_NAME]

_lock = threading.Lock()
_lib = None


def _load():
    global _lib
    if _lib is None:
        for p in _SEARCH:
            if p.exists():
                lib = ctypes.CDLL(str(p))
                lib.run_lacam.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_float]
                lib.run_lacam.restype = ctypes.c_char_p
                _lib = lib
                break
        else:
            raise FileNotFoundError(f"找不到 {_LIB_NAME}，先编译 native/lacam（见 CLAUDE.md）。查找过：{_SEARCH}")
    return _lib


def available() -> bool:
    try:
        _load()
        return True
    except (FileNotFoundError, OSError):
        return False


def _map_text(grid: np.ndarray) -> str:
    H, W = grid.shape
    rows = ["".join("@" if v else "." for v in row) for row in grid]
    return f"type octile\nheight {H}\nwidth {W}\nmap\n" + "\n".join(rows) + "\n"


def _scen_text(grid: np.ndarray, starts, goals) -> str:
    H, W = grid.shape
    lines = ["version 1"]
    for i, ((sr, sc), (gr, gc)) in enumerate(zip(starts, goals)):
        # ★ 先列后行
        lines.append(f"{i}\ttmp.map\t{H}\t{W}\t{sc}\t{sr}\t{gc}\t{gr}\t1")
    return "\n".join(lines) + "\n"


def solve(grid: np.ndarray, starts, goals, time_limit: float = 1.0) -> Optional[np.ndarray]:
    """求解一个 MAPF 实例。返回 (T,N,2) int32 的 (row,col) 配置序列（第 0 行是起点），失败返回 None。"""
    grid = np.asarray(grid)
    starts = [tuple(map(int, s)) for s in starts]
    goals = [tuple(map(int, g)) for g in goals]
    N = len(starts)
    if N == 0:
        return np.zeros((1, 0, 2), dtype=np.int32)
    lib = _load()
    m = _map_text(grid).encode("utf-8")
    s = _scen_text(grid, starts, goals).encode("utf-8")
    with _lock:
        raw = lib.run_lacam(m, s, N, ctypes.c_float(time_limit))
        out = raw.decode("utf-8") if raw else ""   # 在锁内拷出，防止下一次调用覆盖 static 缓冲
    if not out or "ERROR" in out:
        return None
    configs = []
    for line in out.strip().splitlines():
        cells = [c for c in line.strip().split("|") if c]
        if len(cells) != N:
            return None
        # 返回的是 x(列),y(行)
        configs.append([(int(c.split(",")[1]), int(c.split(",")[0])) for c in cells])
    plan = np.asarray(configs, dtype=np.int32)
    if not np.array_equal(plan[0], np.asarray(starts)) or not np.array_equal(plan[-1], np.asarray(goals)):
        return None
    return plan
