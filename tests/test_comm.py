"""学生通信：邻居计算、预算、消息类型、交换视野（确认 / 自动 / AI 队友）、跨步作废、下一步送达、写库。"""
import asyncio
import json

import numpy as np
import pytest

from classroom.core.comm import CommState, contact_matrix
from classroom.core.round import GroupRound
from classroom.core.settings import MESSAGE_KINDS, RoundSettings
from classroom.storage.db import Database

POS = np.array([[0, 0], [0, 4], [3, 3], [9, 9]])   # 0-1 距离 4（边界，算在内）；0-2 距离 √18≈4.24


def state(**kw):
    s = RoundSettings(**kw)
    return CommState(0, POS, s, is_ai=lambda a: a == 2)


def test_contact_matrix_l2_boundary():
    m = contact_matrix(POS, 4.0)
    assert m[0, 1] and m[1, 0]          # 正好 4：在范围内
    assert not m[0, 2]                  # 4.24：不在
    assert not m[0, 0]                  # 不含自己
    assert not m[3].any()
    assert contact_matrix(POS, 4.25)[0, 2]


def test_out_of_range_rejected():
    c = state()
    assert c.send_message(0, 3, "yield") == "对方不在通信范围内"
    assert c.request_view(0, 3)[1] == "对方不在通信范围内"
    assert c.events == []


def test_budget():
    c = state(comm_budget=2)
    assert c.budget_left(0) == 2
    assert c.send_message(0, 1, "yield") is None
    assert c.request_view(0, 1) == ("pending", None)
    assert c.budget_left(0) == 0
    assert c.send_message(0, 1, "arrived") == "这一步的通信次数用完了"
    # 回应别人的请求不花预算
    assert c.answer(1, 0, True) is None
    assert state().budget_left(0) is None      # 默认不限


def test_message_kinds_filter_and_disabled():
    c = state(comm_messages=["yield"])
    assert c.send_message(0, 1, "go_first") == "老师没有开放这种消息"
    assert c.send_message(0, 1, "yield") is None
    off = state(comm_enabled=False)
    assert off.contacts(0) == [] and off.send_message(0, 1, "yield") is not None


def test_view_exchange_needs_confirmation():
    c = state()
    assert c.request_view(0, 1) == ("pending", None)
    assert c.shared == {}
    assert c.student_part(1)["requests_in"] == [0] and c.student_part(0)["requests_out"] == [1]
    assert c.answer(1, 0, True) is None
    assert c.shared == {0: {1}, 1: {0}}
    assert [e.type for e in c.events] == ["view_request", "view_accept"]
    assert c.request_view(0, 1)[1] == "你们这一步已经交换过视野了"


def test_view_exchange_reject():
    c = state()
    c.request_view(1, 0)
    assert c.answer(0, 1, False) is None
    assert c.shared == {} and c.student_part(1)["rejected_by"] == [0]
    assert c.answer(0, 1, True) == "这个请求已经失效了"


def test_mutual_request_counts_as_accept():
    c = state(comm_budget=1)
    c.request_view(0, 1)
    assert c.request_view(1, 0) == ("accepted", None)
    assert c.shared == {0: {1}, 1: {0}} and c.budget_left(1) == 1


def test_auto_accept_and_ai_teammate():
    c = state(comm_auto_accept=True)
    assert c.request_view(0, 1) == ("accepted", None)
    # AI 队友总是自动同意（2 号与 1 号距离 √10，在范围内）
    c2 = state()
    assert c2.request_view(1, 2) == ("accepted", None)
    assert c2.events[-1].payload == {"auto": True}


def test_full_view_disables_exchange():
    c = state(view_radius=None)
    assert c.request_view(0, 1)[1] == "现在不能交换视野"
    assert c.send_message(0, 1, "yield") is None


def test_delivery_now_vs_next():
    now = state()
    now.send_message(0, 1, "move_down")
    assert [m.kind for m in now.inbox[1]] == ["move_down"]
    nxt = state(comm_delivery="next")
    nxt.send_message(0, 1, "move_down")
    assert 1 not in nxt.inbox and len(nxt.next_queue) == 1
    later = CommState(1, POS, nxt.s, is_ai=lambda a: False, carry=nxt.next_queue)
    assert [(m.t_sent, m.kind) for m in later.inbox[1]] == [(0, "move_down")]


class Sink:
    def __init__(self):
        self.msgs = {}
        self.teacher = []

    async def send_agent(self, agent, msg):
        self.msgs.setdefault(agent, []).append(msg)

    async def send_teachers(self, msg):
        self.teacher.append(msg)

    async def send_player(self, pid, msg):
        pass

    async def kick(self, pid):
        pass


def test_round_comm_flow_and_db(tmp_path):
    """经过回合机：交换视野要确认、跨步作废、写 comm_event 和 shared_view_json。"""
    async def go():
        sink = Sink()
        db = Database(tmp_path / "c.sqlite")
        await db.start_session()
        # crossroads：0 号 (0,3)、1 号 (6,3)、2 号 (3,0)、3 号 (3,6)。半径 5：0-2、1-2、0-3 互通（√18），0-1 距离 6 不通
        r = GroupRound(0, RoundSettings(preset="crossroads", view_radius=1, comm_radius=5.0), sink, db=db)
        r.online = {10, 12, 13}
        r.seat(0, 10); r.seat(2, 12); r.seat(1, 13)
        await r.start()
        t = r.game.t
        assert await r.comm_view_request(10, t, 2) is None
        st2 = sink.msgs[2][-1]
        assert st2["view"]["comm"]["requests_in"] == [0] and st2["view"]["shared"] == []
        assert await r.comm_view_answer(12, t, 0, True) is None
        v0 = sink.msgs[0][-1]["view"]
        assert [s["agent"] for s in v0["shared"]] == [2]
        assert await r.comm_message(10, t, 2, "yield") is None
        assert sink.msgs[2][-1]["view"]["comm"]["inbox"][0]["kind"] == "yield"
        assert await r.comm_message(10, t, 1, "yield") == "对方不在通信范围内"
        # 1 号请求 2 号（学生控制，要确认），挂着不回：结算后作废
        assert await r.comm_view_request(13, t, 2) is None
        assert r.comm.pending == {(1, 2)}
        for p in (10, 12, 13):
            await r.choose(p, t, 0)
        assert r.game.t == t + 1 and r.comm.pending == set() and r.comm.shared == {}
        rows = await db.query("SELECT t, from_agent, to_agent, type, payload_json, accepted FROM comm_event")
        sv = await db.query("SELECT agent, shared_view_json FROM step_decision WHERE t=? AND controller='student'", (t,))
        teacher_events = [m for m in sink.teacher if m["type"] == "comm_event"]
        await r.end(); await db.close()
        return rows, dict(sv), teacher_events
    rows, sv, tev = asyncio.run(go())
    types = [(f, to, ty, acc) for _, f, to, ty, _, acc in rows]
    assert (0, 2, "view_request", None) in types and (2, 0, "view_accept", 1) in types
    assert (0, 2, "msg", None) in types
    assert json.loads(rows[[r[3] for r in rows].index("msg")][4]) == {"kind": "yield", "delivery": "now"}
    assert [s["agent"] for s in json.loads(sv[0])] == [2] and [s["agent"] for s in json.loads(sv[2])] == [0]
    assert json.loads(sv[1]) == []
    assert len(tev) == len(rows)
