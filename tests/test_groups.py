"""分组比拼：均分、组内座位、按组路由消息、各组同一实例、各组独立、参照行、投屏不泄露。"""
import asyncio
import json

import numpy as np
import pytest

from classroom.core.session import Classroom
from classroom.core.settings import RoundSettings


class FakeHub:
    def __init__(self):
        self.player_msgs: dict[int, list] = {}
        self.teacher_msgs: list = []
        self.screen_msgs: list = []

    async def send_player(self, pid, msg):
        self.player_msgs.setdefault(pid, []).append(json.loads(json.dumps(msg)))

    async def send_teachers(self, msg):
        self.teacher_msgs.append(msg)

    async def send_screens(self, msg):
        self.screen_msgs.append(json.loads(json.dumps(msg)))

    async def kick(self, pid):
        pass


async def make_room(k=3, n_players=7, **kw):
    hub = FakeHub()
    room = Classroom(hub, settings=RoundSettings(num_groups=k, preset="four-rooms", num_agents=4, countdown_sec=60, **kw))
    await room.open()
    for i in range(n_players):
        p = await room.join(f"同学{i}", str(i))
        await room.set_online(p.id, True)
    return hub, room


def run(coro):
    return asyncio.run(coro)


def test_even_split_and_unique_seats():
    async def go():
        hub, room = await make_room(k=3, n_players=7)
        await room.start()
        sizes = sorted(sum(1 for g in room.group_of.values() if g == r.group_id) for r in room.groups)
        assert sizes == [2, 2, 3]
        for r in room.groups:
            assert len(set(r.seats)) == len(r.seats) and all(room.group_of[p] == r.group_id for p in r.seats.values())
        await room.end()
    run(go())


def test_same_instance_across_groups():
    async def go():
        hub, room = await make_room(k=3, n_players=0)
        g0 = room.groups[0].game
        for r in room.groups[1:]:
            assert r.game.map_str == g0.map_str
            assert np.array_equal(r.game.positions, g0.positions) and np.array_equal(r.game.targets, g0.targets)
            assert r.game.model is not g0.model and r.game.model.net is not g0.model.net
    run(go())


def test_messages_routed_by_group():
    """两组里坐同一个智能体号的两个学生，各自只收到自己组的视野。"""
    async def go():
        hub, room = await make_room(k=2, n_players=2)
        pids = list(room.players)
        await room.assign(pids[0], 1, group=0)
        await room.assign(pids[1], 1, group=1)
        await room.start()
        hub.player_msgs.clear()
        t = room.groups[0].game.t
        await room.round_of(pids[0]).choose(pids[0], t, 0)          # 第 1 组唯一的学生提交 -> 第 1 组结算
        assert room.groups[0].game.t == t + 1 and room.groups[1].game.t == t
        got0 = [m for m in hub.player_msgs.get(pids[0], []) if m["type"] == "state" and "view" in m]
        got1 = [m for m in hub.player_msgs.get(pids[1], []) if m["type"] == "state" and "view" in m]
        assert got0 and got0[-1]["view"]["t"] == t + 1 and got0[-1]["group_name"] == "第 1 组"
        assert not got1, "第 2 组的学生不该收到第 1 组的推送"
        # 发给老师的消息带组号
        assert any(m.get("type") == "resolved" and m["group"] == 0 for m in hub.teacher_msgs)
        await room.end()
    run(go())


def test_step_now_single_group_and_end_independent():
    async def go():
        hub, room = await make_room(k=3, n_players=3)
        await room.start()
        await room.step_now(1)
        assert [r.game.t for r in room.groups] == [0, 1, 0]
        await room.groups[1].end()
        assert [r.status for r in room.groups] == ["playing", "finished", "playing"]
        await room.end()
    run(go())


def test_remove_and_disconnect_only_affect_own_group():
    async def go():
        hub, room = await make_room(k=2, n_players=4)
        await room.start()
        pid = next(p for p, g in room.group_of.items() if g == 1)
        other = [r.seats.copy() for r in room.groups]
        await room.remove(pid)
        assert pid not in room.group_of and pid not in room.groups[1].seats.values()
        assert room.groups[0].seats == other[0]
        await room.end()
    run(go())


def test_late_joiner_goes_to_group_with_most_free_seats():
    async def go():
        hub, room = await make_room(k=2, n_players=3)
        await room.start()
        p = await room.join("迟到", "9")
        await room.set_online(p.id, True)
        r = room.round_of(p.id)
        assert r is not None and r.agent_of(p.id) is not None
        await room.end()
    run(go())


def test_references_and_screen_no_leak():
    async def go():
        hub, room = await make_room(k=2, n_players=2, view_radius=1)
        await room.start()
        for _ in range(100):                       # 等后台参照行算完
            if all(v.get("status") == "done" for v in room.references.values()):
                break
            await asyncio.sleep(0.05)
        assert {k: v["status"] for k, v in room.references.items()} == {"ai": "done", "expert": "done"}
        lb = room.leaderboard()
        assert {r["kind"] for r in lb} == {"group", "ai", "expert"}
        assert all(r["rank"] is None for r in lb if r["kind"] != "group")
        # 比赛中、没开"显示全图"：投屏载荷里不能有任何地图 / 位置 / 目标
        st = json.dumps(room.screen_state())
        for k in ("map", "positions", "targets", "rows", "agents_xy"):
            assert f'"{k}"' not in st, k
        await room.set_screen_live(True)
        assert all("map" in g for g in room.screen_state()["groups"])
        await room.set_screen_live(False)
        await room.groups[0].end()
        s = room.screen_state()
        assert "map" in s["groups"][0] and "map" not in s["groups"][1]     # 结束的组才显示全图
        await asyncio.sleep(0.3)
        assert hub.screen_msgs and hub.screen_msgs[-1]["type"] == "screen"   # 去抖后推给了投屏页
        await room.end()
    run(go())


def test_configure_keeps_groups_within_range():
    async def go():
        hub, room = await make_room(k=3, n_players=6)
        await room.shuffle()
        kept = dict(room.group_of)
        await room.configure(RoundSettings(num_groups=2, preset="four-rooms", num_agents=4))
        assert all(g < 2 for g in room.group_of.values())
        assert {p for p, g in kept.items() if g < 2} == set(room.group_of)
    run(go())
