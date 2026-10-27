"""网格 BFS 工具（unpadded 坐标，4 邻接，grid 中 1=障碍）。"""
from __future__ import annotations

from collections import deque

import numpy as np

UNREACHABLE = -1


def bfs_dist(grid: np.ndarray, src) -> np.ndarray:
    """从 src=(row,col) 出发的测地距离场，(H,W) int32，不可达 / 障碍为 -1。"""
    g = np.asarray(grid)
    H, W = g.shape
    dist = np.full((H, W), UNREACHABLE, dtype=np.int32)
    r0, c0 = int(src[0]), int(src[1])
    if not (0 <= r0 < H and 0 <= c0 < W) or g[r0, c0]:
        return dist
    dist[r0, c0] = 0
    q = deque([(r0, c0)])
    while q:
        r, c = q.popleft()
        d = dist[r, c] + 1
        for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if 0 <= nr < H and 0 <= nc < W and not g[nr, nc] and dist[nr, nc] == UNREACHABLE:
                dist[nr, nc] = d
                q.append((nr, nc))
    return dist
