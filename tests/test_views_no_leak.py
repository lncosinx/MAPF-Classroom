"""防泄露（不变式 9）：学生载荷只能包含视野内 / 记忆中的格子、视野内的智能体、自己的目标（或方向）。

随机地图、随机局面、各种半径和迷雾模式，逐项核对载荷里出现的每一个格子和每一个智能体。
"""
import json

import numpy as np
import pytest

from classroom.core.game import GroupGame
from classroom.core.round import GroupRound
from classroom.core.settings import RoundSettings
from classroom.core.views import ViewMemory, in_box, student_view, view_box
from classroom.maps.generate import generate, random_pairs
from classroom.core.coords import parse_map

FORBIDDEN_KEYS = {"expert", "expert_actions", "expert_source", "ai", "model", "model_actions", "model_probs",
                  "probs", "logits", "attn", "ai_edges", "targets", "positions"}


def all_keys(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from all_keys(v)
    elif isinstance(x, list):
        for v in x:
            yield from all_keys(v)


def check_payload(game, ctx, agent, settings, seen_before, payload):
    H, W = game.grid.shape
    pos = ctx.positions[agent]
    box = view_box(pos, settings.view_radius, (H, W))
    assert payload["box"] == list(box)
    assert payload["pos"] == pos.tolist()
    # 视野内的格子与真实地图一致，且行列数正好是视野框
    rows = payload["rows"]
    assert len(rows) == box[2] - box[0] + 1 and all(len(r) == box[3] - box[1] + 1 for r in rows)
    for i, row in enumerate(rows):
        for j, ch in enumerate(row):
            assert (ch == "#") == bool(game.grid[box[0] + i, box[1] + j])
    # 记忆格：必须是之前真的见过的，而且不在当前视野内
    for r, c, wall in payload["memory"]:
        assert seen_before[r, c], "记忆里出现了没见过的格子"
        assert not in_box((r, c), box)
        assert wall == int(game.grid[r, c])
    if settings.fog_mode == "fog" or settings.view_radius is None:
        assert payload["memory"] == []
    # 智能体：只能是视野内的，坐标真实
    for a in payload["agents"]:
        assert a["agent"] != agent
        assert in_box(a["pos"], box), "载荷里出现了视野外的智能体"
        assert a["pos"] == ctx.positions[a["agent"]].tolist()
    visible_others = {j for j, p in enumerate(ctx.positions) if j != agent and in_box(p, box)}
    assert {a["agent"] for a in payload["agents"]} == visible_others
    # 目标
    goal = game.targets[agent]
    if settings.goal_display == "projection" and not in_box(goal, box):
        # 视野外：不给目标坐标，只给 pogema 式投影——行、列偏移各自截断到 [-r, r]，必落在视野框边上
        assert payload["goal"] is None
        r = settings.view_radius
        want = [int(pos[k] + np.clip(goal[k] - pos[k], -r, r)) for k in (0, 1)]
        assert payload["goal_proj"] == want
        assert in_box(want, box) and (abs(want[0] - pos[0]) == r or abs(want[1] - pos[1]) == r)
    else:
        assert payload["goal"] == goal.tolist() and payload["goal_proj"] is None
    # 不得含任何建议 / 全局字段
    keys = set(all_keys(payload))
    assert not keys & FORBIDDEN_KEYS, keys & FORBIDDEN_KEYS


@pytest.mark.parametrize("radius", [1, 2, 3, 5, None])
@pytest.mark.parametrize("fog_mode", ["fog", "memory"])
@pytest.mark.parametrize("goal_display", ["projection", "visible"])
def test_student_view_no_leak(radius, fog_mode, goal_display):
    rng = np.random.default_rng(hash((radius, fog_mode, goal_display)) % 2**32)
    settings = RoundSettings(view_radius=radius, fog_mode=fog_mode, goal_display=goal_display)
    for trial in range(3):
        ms = generate("random" if trial % 2 == 0 else "maze", 14, 12, 0.25, seed=int(rng.integers(1000)))
        grid = parse_map(ms)
        n = 8
        s, g = random_pairs(grid, n, seed=trial)
        game = GroupGame(ms, agents_xy=s, targets_xy=g, max_steps=20, need_expert=False, stop_on_success=False)
        memory = ViewMemory(n, grid.shape)
        for t in range(12):
            ctx = game.begin_step(with_expert=False)
            for agent in range(n):
                seen_before = memory.seen[agent].copy()
                payload = student_view(game, ctx, agent, settings, memory)
                json.dumps(payload)   # 必须能直接序列化下发
                check_payload(game, ctx, agent, settings, seen_before, payload)
            game.apply(rng.integers(0, 5, size=n))


class Sink:
    def __init__(self):
        self.agent_msgs: dict[int, list] = {}
        self.teacher_msgs: list = []

    async def send_agent(self, agent, msg):
        self.agent_msgs.setdefault(agent, []).append(json.loads(json.dumps(msg)))

    async def send_teachers(self, msg):
        self.teacher_msgs.append(msg)


@pytest.mark.parametrize("reveal", ["teacher", "review", "immediate"])
def test_round_messages_hide_advice(reveal):
    """经过回合机发给学生的全部消息：teacher/review 模式下没有任何建议；immediate 只在结算后的 reveal 消息里出现。"""
    import asyncio

    async def go():
        sink = Sink()
        r = GroupRound(0, RoundSettings(preset="crossroads", reveal_mode=reveal, view_radius=2), sink)
        r.online = {101, 102}
        r.seat(0, 101); r.seat(1, 102)
        await r.start()
        for _ in range(3):
            t = r.game.t
            await r.choose(101, t, 0)
            await r.choose(102, t, 0)
        await r.end()
        return sink

    sink = asyncio.run(go())
    for agent, msgs in sink.agent_msgs.items():
        for m in msgs:
            if m["type"] == "reveal":
                assert reveal == "immediate"
                continue
            assert not set(all_keys(m)) & FORBIDDEN_KEYS, (m["type"], set(all_keys(m)) & FORBIDDEN_KEYS)
        reveals = [m for m in msgs if m["type"] == "reveal"]
        assert (len(reveals) == 3) == (reveal == "immediate")


def test_projection_matches_pogema_ch2():
    """半径 5（= pogema obs_radius）时，学生看到的目标格 / 投影格 = pogema 原生观测 ch2 里标 1 的那一格。"""
    settings = RoundSettings(view_radius=5, goal_display="projection")
    rng = np.random.default_rng(7)
    checked_proj = 0
    for seed in range(4):
        ms = generate("random", 24, 24, 0.2, seed=seed)
        grid = parse_map(ms)
        s, g = random_pairs(grid, 10, seed=seed)
        game = GroupGame(ms, agents_xy=s, targets_xy=g, max_steps=10, need_expert=False, stop_on_success=False)
        for t in range(6):
            ctx = game.begin_step(with_expert=False)
            for agent in range(10):
                payload = student_view(game, ctx, agent, settings)
                cell = payload["goal"] or payload["goal_proj"]
                checked_proj += payload["goal_proj"] is not None
                ch2 = np.asarray(game.obs[agent])[2]
                (rr, cc), = np.argwhere(ch2 > 0)
                pos = ctx.positions[agent]
                assert [int(pos[0] + rr - 5), int(pos[1] + cc - 5)] == cell
            game.apply(rng.integers(0, 5, size=10))
    assert checked_proj > 0, "没覆盖到目标在视野外的情形"
