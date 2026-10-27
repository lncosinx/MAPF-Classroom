"""GroupRound 回合机：结算时机、超时处理、补位、断线接管与交还、暂停恢复、写库。"""
import asyncio
import json

import numpy as np
import pytest

from classroom.core.coords import WAIT
from classroom.core.round import GroupRound
from classroom.core.settings import RoundSettings
from classroom.storage.db import STEP_DECISION_COLS, Database


class Sink:
    def __init__(self):
        self.agent_msgs: dict[int, list] = {}
        self.teacher_msgs: list = []

    async def send_agent(self, agent, msg):
        self.agent_msgs.setdefault(agent, []).append(msg)

    async def send_teachers(self, msg):
        self.teacher_msgs.append(msg)

    def last(self, agent, typ):
        return next(m for m in reversed(self.agent_msgs.get(agent, [])) if m["type"] == typ)


def make_round(sink, db=None, **kw):
    s = RoundSettings(preset="doorway", countdown_sec=30, **kw)
    r = GroupRound(0, s, sink, db=db)
    r.online = {11, 12, 13}
    r.seat(0, 11); r.seat(1, 12); r.seat(3, 13)
    return r


def run(coro):
    return asyncio.run(coro)


def test_all_submitted_resolves_immediately():
    async def go():
        sink = Sink(); r = make_round(sink)
        await r.start()
        assert r.game.t == 0
        legal = r.ctx.legal
        await r.choose(11, 0, 4 if legal[0, 4] else 0)
        await r.choose(12, 0, 0)
        assert r.game.t == 0              # 还差一个人
        assert sink.last(0, "progress")["submitted"] == 2
        await r.choose(13, 0, 0)
        assert r.game.t == 1              # 全员提交 -> 立即结算
        await r.end()
    run(go())


def test_change_before_lock_counts():
    async def go():
        sink = Sink(); r = make_round(sink)
        await r.start()
        await r.choose(11, 0, 0)
        await r.choose(11, 0, 2) if r.ctx.legal[0, 2] else None
        assert r.choices[0].n_changes == (1 if r.ctx.legal[0, 2] else 0)
        await r.end()
    run(go())


def test_reject_illegal_and_stale():
    async def go():
        sink = Sink(); r = make_round(sink)
        await r.start()
        # 0 号在 (0,0)：向上、向左出界
        assert await r.choose(11, 0, 1) is not None
        assert await r.choose(11, 5, 0) is not None      # 不是本步
        assert await r.choose(99, 0, 0) is not None      # 没座位
        await r.end()
    run(go())


@pytest.mark.parametrize("timeout_policy", ["wait", "ai"])
def test_timeout_policy(timeout_policy):
    async def go():
        sink = Sink(); r = make_round(sink, timeout_policy=timeout_policy, fill_policy="model")
        await r.start()
        model_a0 = int(r.ctx.model.actions[0])
        await r.resolve(timed_out=True)   # 等价于倒计时到点
        res = next(m for m in sink.teacher_msgs if m["type"] == "resolved")
        if timeout_policy == "wait":
            assert res["actions"][0] == WAIT and res["controllers"][0] == "student"
        else:
            assert res["actions"][0] == model_a0 and res["controllers"][0] == "model"
        assert res["timed_out"][0] is True
        await r.end()
    run(go())


@pytest.mark.parametrize("fill", ["model", "expert", "idle"])
def test_fill_policy_for_empty_seats(fill):
    async def go():
        sink = Sink(); r = make_round(sink, fill_policy=fill)
        await r.start()
        ctx = r.ctx
        expert = await r._expert_fut
        for p in (11, 12, 13):
            await r.choose(p, 0, 0)
        res = next(m for m in sink.teacher_msgs if m["type"] == "resolved")
        # 2、4 号没人坐
        for i in (2, 4):
            want = {"model": int(ctx.model.actions[i]), "expert": int(expert.actions[i]), "idle": WAIT}[fill]
            assert res["actions"][i] == want and res["controllers"][i] == fill
        await r.end()
    run(go())


def test_prev_action_is_student_choice():
    async def go():
        sink = Sink(); r = make_round(sink, fill_policy="model")
        await r.start()
        for p in (11, 12, 13):
            await r.choose(p, 0, 0)
        assert r.game.model.prev[0] == 0 and r.game.model.prev[1] == 0
        await r.end()
    run(go())


def test_disconnect_takeover_and_return():
    async def go():
        sink = Sink(); r = make_round(sink, fill_policy="model")
        await r.start()
        await r.choose(11, 0, 0); await r.choose(12, 0, 0)
        # 13 号断线：剩下的人都已提交 -> 立即结算，3 号由模型接管
        await r.player_online(13, False)
        assert r.game.t == 1
        res = next(m for m in sink.teacher_msgs if m["type"] == "resolved")
        assert res["controllers"][3] == "model"
        # 重连：收到当前状态，重新算作学生控制
        await r.player_online(13, True)
        assert sink.last(3, "state")["view"]["t"] == 1
        assert 3 in r.student_agents()
        await r.end()
    run(go())


def test_pause_freezes_and_resume():
    async def go():
        sink = Sink(); r = make_round(sink)
        await r.start()
        await r.pause()
        assert r.status == "paused" and r.remaining is not None
        frozen = r.countdown_ms()
        await asyncio.sleep(0.05)
        assert r.countdown_ms() == frozen
        # 暂停时可以改选择，但不结算
        for p in (11, 12, 13):
            await r.choose(p, 0, 0)
        assert r.game.t == 0
        await r.resume()
        assert r.status == "playing"
        await r.end()
    run(go())


def test_no_students_auto_advances():
    async def go():
        sink = Sink()
        r = GroupRound(0, RoundSettings(preset="corridor-swap", max_steps=40), sink)
        await r.start()
        for _ in range(200):
            if r.status == "finished":
                break
            await asyncio.sleep(0.05)
        assert r.status == "finished" and r.metrics is not None
    run(go())


def test_step_decision_rows_complete(tmp_path):
    async def go():
        sink = Sink()
        db = Database(tmp_path / "s.sqlite")
        await db.start_session()
        r = make_round(sink, db=db, fill_policy="expert", max_steps=6)
        await r.start()
        rid = r.round_id
        while r.status == "playing":
            t = r.game.t
            for p, a in ((11, 0), (12, 0), (13, 0)):
                await r.choose(p, t, a)
        rows = await db.query(f"SELECT {','.join(STEP_DECISION_COLS)} FROM step_decision WHERE round_id=?", (rid,))
        steps = await db.query("SELECT count(*) FROM step_state WHERE round_id=?", (rid,))
        metrics = await db.query("SELECT metrics_json FROM round WHERE id=?", (rid,))
        await db.close()
        return r, rows, steps, metrics
    r, rows, steps, metrics = run(go())
    T, N = r.game.t, r.num_agents
    assert len(rows) == T * N and steps[0][0] == T
    assert json.loads(metrics[0][0]) == r.metrics
    for row in rows:
        d = dict(zip(STEP_DECISION_COLS, row))
        always = [k for k in STEP_DECISION_COLS if k not in
                  ("player_id", "view_json", "shared_view_json", "submit_ts", "think_ms")]
        assert all(d[k] is not None for k in always), d
        if d["controller"] == "student":
            assert d["player_id"] in (11, 12, 13) and d["view_json"] and d["submit_ts"] and d["think_ms"] is not None
            view = json.loads(d["view_json"])
            assert view["t"] == d["t"] and view["agent"] == d["agent"]
        assert len(d["legal_mask"]) == 5 and len(json.loads(d["model_probs_json"])) == 5
        assert d["expert_source"] in ("lacam", "simple")
