"""LaCAM 封装：路径合法、无冲突、先列后行换算正确、并发调用安全、不写日志文件。"""
import os
import threading
from pathlib import Path

import numpy as np
import pytest

from classroom.ai import lacam
from classroom.ai.expert import ExpertPolicy, plan_to_actions, prioritized_plan
from classroom.maps.generate import generate, random_pairs
from classroom.maps.library import get_preset

pytestmark = pytest.mark.skipif(not lacam.available(), reason="liblacam 未编译")


def assert_valid_plan(grid, plan, starts, goals):
    assert np.array_equal(plan[0], starts) and np.array_equal(plan[-1], goals)
    for t in range(len(plan)):
        assert len({tuple(p) for p in plan[t]}) == len(starts), f"t={t} 顶点冲突"
        assert all(grid[r, c] == 0 for r, c in plan[t])
        if t:
            step = np.abs(plan[t] - plan[t - 1]).sum(1)
            assert (step <= 1).all(), f"t={t} 跳格"
            for i in range(len(starts)):
                for j in range(i + 1, len(starts)):
                    swap = (tuple(plan[t][i]) == tuple(plan[t - 1][j])) and (tuple(plan[t][j]) == tuple(plan[t - 1][i]))
                    assert not (swap and step[i]), f"t={t} 交换冲突"
    plan_to_actions(plan, 0)   # 位移都能映射到动作


def test_rectangular_map_row_col():
    # 非方形地图，行列写反会立刻越界或撞墙
    p = get_preset("doorway")
    plan = lacam.solve(p.grid, p.agents_xy, p.targets_xy, 0.5)
    assert plan is not None
    assert_valid_plan(p.grid, plan, p.agents_xy, p.targets_xy)


def test_random_instances():
    grid = np.array([[ch == "#" for ch in r] for r in generate("random", 16, 12, 0.2, seed=3).splitlines()], np.uint8)
    for n in (4, 12):
        s, g = random_pairs(grid, n, seed=n)
        plan = lacam.solve(grid, s, g, 0.3)
        assert plan is not None
        assert_valid_plan(grid, plan, s, g)


def test_error_returns_none():
    p = get_preset("corridor-swap")
    bad_starts = np.array([[0, 0], [1, 6]])     # (0,0) 是墙
    assert lacam.solve(p.grid, bad_starts, p.targets_xy, 0.2) is None


def test_concurrent_calls_and_no_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = get_preset("crossroads")
    out = []

    def work():
        out.append(lacam.solve(p.grid, p.agents_xy, p.targets_xy, 0.1))

    ts = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert all(o is not None for o in out)
    for o in out:
        assert_valid_plan(p.grid, o, p.agents_xy, p.targets_xy)
    assert not (Path(tmp_path) / "lacam_log.txt").exists()


def test_prioritized_fallback_valid():
    p = get_preset("doorway")
    plan = prioritized_plan(p.grid, p.agents_xy, p.targets_xy)
    assert_valid_plan(p.grid, plan, p.agents_xy, p.targets_xy)


def test_expert_reuses_plan_when_followed():
    p = get_preset("crossroads")
    ex = ExpertPolicy(p.grid, p.targets_xy, time_limit=0.2)
    pos = p.agents_xy.copy()
    a0 = ex.act(pos)
    assert a0.replanned and a0.source == "lacam"
    plan = ex.plan
    # 照着计划走一步 -> 复用
    a1 = ex.act(plan[1])
    assert not a1.replanned and ex.plan is plan
    # 偏离计划 -> 重新求解
    a2 = ex.act(p.agents_xy)
    assert a2.replanned
