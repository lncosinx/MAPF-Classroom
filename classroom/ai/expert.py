"""ExpertPolicy：全知专家（LaCAM），失败时退回 prioritized 简易专家。全部用 unpadded (row, col)。

每步从当前状态重新求解、只取第一步；若所有智能体都按上一份计划走了一步，就**复用**那份计划，
避免建议逐步闪动（PLAN.md §9）。专家是训练期特权信号，只用来给"专家建议"和"专家队"参照，绝不喂给模型。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from ..core.bfs import UNREACHABLE, bfs_dist
from ..core.coords import ACTION_DELTAS, WAIT, delta_to_action
from . import lacam

SOURCE_LACAM = "lacam"
SOURCE_SIMPLE = "simple"   # UI 上标"简易专家"


@dataclass
class ExpertAdvice:
    actions: np.ndarray   # (N,) int64
    source: str           # lacam / simple
    replanned: bool       # 本步是否重新求解（False=复用上一份计划）


def plan_to_actions(plan: np.ndarray, t: int) -> np.ndarray:
    """计划第 t 步到 t+1 步的位移 -> 动作；计划已走完的体给 wait。"""
    N = plan.shape[1]
    if t + 1 >= len(plan):
        return np.full(N, WAIT, dtype=np.int64)
    d = plan[t + 1] - plan[t]
    return np.array([delta_to_action(dr, dc) for dr, dc in d], dtype=np.int64)


def prioritized_plan(grid: np.ndarray, starts, goals, horizon: Optional[int] = None) -> np.ndarray:
    """简易专家：按到目标距离从远到近排优先级，逐个做时空 BFS（顶点 + 交换冲突预留）。

    规划不出来的体原地等待（它仍然占着自己的格子，后续体会绕开）。返回 (T,N,2)。
    """
    g = np.asarray(grid)
    H, W = g.shape
    starts = [tuple(map(int, s)) for s in starts]
    goals = [tuple(map(int, x)) for x in goals]
    N = len(starts)
    horizon = horizon or max(64, 2 * H * W)
    dists = [bfs_dist(g, goal) for goal in goals]
    order = sorted(range(N), key=lambda i: -int(dists[i][starts[i]]))

    vertex_res: dict[tuple, int] = {}   # (t,r,c) -> agent
    edge_res: set = set()               # (t,(r,c),(r2,c2))：t->t+1 从前者走到后者
    parked: dict[tuple, int] = {}       # 已停在终点的格子 -> 从哪个时刻起永久占用
    paths: list = [None] * N

    def occupied(t, cell):
        if (t, cell[0], cell[1]) in vertex_res:
            return True
        p = parked.get(cell)
        return p is not None and t >= p

    for i in order:
        s, goal, dist = starts[i], goals[i], dists[i]
        path = None
        if dist[s] != UNREACHABLE:
            # 时空 BFS：状态 (t, r, c)；到达终点且之后不再被别人占用即成功
            prev = {(0, s): None}
            frontier = [s]
            for t in range(horizon):
                nxt = []
                for cell in frontier:
                    if cell == goal and all(not ((tt, goal[0], goal[1]) in vertex_res)
                                            for tt in range(t, horizon + 1)):
                        # 回溯
                        path, cur = [], (t, cell)
                        while cur is not None:
                            path.append(cur[1]); cur = prev[cur]
                        path.reverse()
                        break
                    for dr, dc in ACTION_DELTAS:
                        n = (cell[0] + dr, cell[1] + dc)
                        if not (0 <= n[0] < H and 0 <= n[1] < W) or g[n] or (t + 1, n) in prev:
                            continue
                        if dist[n] == UNREACHABLE or occupied(t + 1, n) or (t, n, cell) in edge_res:
                            continue
                        prev[(t + 1, n)] = (t, cell)
                        nxt.append(n)
                if path is not None:
                    break
                frontier = nxt
                if not frontier:
                    break
        if path is None:
            path = [s]   # 规划失败：原地等
        paths[i] = path
        for t, cell in enumerate(path):
            vertex_res[(t, cell[0], cell[1])] = i
            if t + 1 < len(path):
                edge_res.add((t, cell, path[t + 1]))
        parked[path[-1]] = len(path) - 1

    T = max(len(p) for p in paths)
    plan = np.zeros((T, N, 2), dtype=np.int32)
    for i, p in enumerate(paths):
        plan[:, i] = np.array(p + [p[-1]] * (T - len(p)))
    return plan


class ExpertPolicy:
    def __init__(self, grid: np.ndarray, goals, time_limit: float = 0.03, use_lacam: bool = True):
        self.grid = np.asarray(grid, dtype=np.uint8)
        self.goals = np.asarray(goals, dtype=np.int32)
        self.time_limit = time_limit
        self.use_lacam = use_lacam and lacam.available()
        self.reset()

    def reset(self):
        self.plan: Optional[np.ndarray] = None
        self.ptr = 0
        self.source = SOURCE_LACAM

    def act(self, positions) -> ExpertAdvice:
        pos = np.asarray(positions, dtype=np.int32)
        replanned = False
        if self.plan is not None and self.ptr + 1 < len(self.plan) and np.array_equal(pos, self.plan[self.ptr + 1]):
            # 所有体都按计划走了一步：复用
            self.ptr += 1
        elif self.plan is not None and np.array_equal(pos, self.plan[self.ptr]):
            pass   # 状态没变（例如上一步被跳过），继续沿用
        else:
            self._replan(pos)
            replanned = True
        return ExpertAdvice(actions=plan_to_actions(self.plan, self.ptr), source=self.source,
                            replanned=replanned)

    def _replan(self, pos: np.ndarray):
        plan = None
        if self.use_lacam:
            plan = lacam.solve(self.grid, pos, self.goals, self.time_limit)
        if plan is None:
            plan = prioritized_plan(self.grid, pos, self.goals)
            self.source = SOURCE_SIMPLE
        else:
            self.source = SOURCE_LACAM
        self.plan, self.ptr = plan, 0
