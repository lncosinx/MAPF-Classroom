"""开局前的地图校验（unpadded 坐标）。

必须在建 env 之前通过：pogema 会把压在障碍上的起点 / 终点**悄悄挪走**，不报错（不变式 3）。
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

from ..core.bfs import UNREACHABLE, bfs_dist

# pogema 字符串格式里 a/A 成对标记最多 26 对
MAX_LETTER_PAIRS = 26


def validate_instance(grid: np.ndarray, starts: Sequence, goals: Sequence) -> list[str]:
    """返回问题列表（中文，可直接展示给老师）；空列表表示通过。"""
    g = np.asarray(grid)
    H, W = g.shape
    errs: list[str] = []
    starts = [tuple(map(int, s)) for s in starts]
    goals = [tuple(map(int, x)) for x in goals]
    if len(starts) != len(goals):
        return [f"起点 {len(starts)} 个、终点 {len(goals)} 个，数量不一致"]
    if not starts:
        return ["至少需要一对起点和终点"]

    def check_cells(cells, name):
        seen: dict = {}
        for i, (r, c) in enumerate(cells):
            if not (0 <= r < H and 0 <= c < W):
                errs.append(f"{i + 1} 号的{name} ({r},{c}) 在地图外")
            elif g[r, c]:
                errs.append(f"{i + 1} 号的{name} ({r},{c}) 压在障碍上")
            if (r, c) in seen:
                errs.append(f"{seen[(r, c)] + 1} 号和 {i + 1} 号的{name}重叠在 ({r},{c})")
            else:
                seen[(r, c)] = i

    check_cells(starts, "起点")
    check_cells(goals, "终点")
    if errs:
        return errs
    for i, (s, goal) in enumerate(zip(starts, goals)):
        if bfs_dist(g, s)[goal] == UNREACHABLE:
            errs.append(f"{i + 1} 号从起点 {s} 走不到终点 {goal}")
    return errs
