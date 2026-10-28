"""复盘与导出：复盘数据和对局一致、教学指标手算对拍、CSV / JSONL / SVG 能打开且内容正确、防路径穿越。"""
import asyncio
import csv
import io
import json
import shutil
import sqlite3
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from classroom.core.round import GroupRound
from classroom.core.settings import RoundSettings
from classroom.storage import export, review
from classroom.storage.db import Database


class Sink:
    async def send_agent(self, a, m): pass
    async def send_teachers(self, m): pass
    async def send_player(self, p, m): pass
    async def kick(self, p): pass


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    """crossroads 4 体：0 号、2 号由学生控制（玩家 10 / 12），其余 AI；学生按固定动作走，含一次通信。"""
    base = tmp_path_factory.mktemp("data")
    name = "20261028-090000.sqlite"
    db = Database(base / "sessions" / name)

    async def go():
        await db.start_session()
        gid = await db.add_group("第 1 组")
        p10 = await db.add_player("小甲", "A1")
        p12 = await db.add_player("小乙", "A2")
        r = GroupRound(0, RoundSettings(preset="crossroads", view_radius=1, comm_radius=5.0, max_steps=12,
                                        reveal_mode="review"), Sink(), db=db, db_group_id=gid)
        r.online = {p10, p12}
        r.seat(0, p10); r.seat(2, p12)
        await r.start()
        await r.comm_message(p10, 0, 2, "yield")
        plan0 = [2, 2, 0, 2, 2, 2]          # 0 号：往下（第 3 步故意停一下）
        plan2 = [4, 4, 4, 4, 4, 4]          # 2 号：往右
        while r.status == "playing":
            t = r.game.t
            await r.choose(p10, t, plan0[t] if t < len(plan0) and r.ctx.legal[0, plan0[t]] else 0)
            await r.choose(p12, t, plan2[t] if t < len(plan2) and r.ctx.legal[2, plan2[t]] else 0)
        await db.close()
        return r, p10, p12
    r, p10, p12 = asyncio.run(go())
    return base, name, r, p10, p12


def test_list_sessions_and_rounds(session):
    base, name, r, *_ = session
    ss = review.list_sessions(base, base / "sessions" / name)
    assert ss[0]["name"] == name and ss[0]["current"] and ss[0]["rounds"] == 1 and ss[0]["players"] == 2
    rounds = review.list_rounds(review.session_path(base, name))
    assert len(rounds) == 1 and rounds[0]["N"] == 4 and rounds[0]["T"] == r.game.t
    assert rounds[0]["metrics"] == r.metrics and rounds[0]["title"] == "crossroads"


@pytest.mark.parametrize("bad", ["../x.sqlite", "20261028-090000.sqlite/../../etc", "a.sqlite", "", "20261028-0900.sqlite"])
def test_session_name_validation(session, bad):
    with pytest.raises(ValueError):
        review.session_path(session[0], bad)


def test_replay_matches_game(session):
    base, name, r, p10, p12 = session
    rep = review.load_replay(review.session_path(base, name), r.round_id)
    T = r.game.t
    assert len(rep["frames"]) == T + 1
    for t in range(T):
        assert rep["frames"][t] == r.game.history.positions[t].tolist()
    assert rep["frames"][T] == r.game.positions.tolist()
    for t in range(T):
        assert [d["action"] for d in rep["steps"][t]] == r.game.history.actions[t].tolist()
    assert rep["steps"][0][0]["nickname"] == "小甲" and rep["steps"][0][1]["controller"] == "model"
    assert rep["comm"][0] == {"t": 0, "from": 0, "to": 2, "type": "msg", "payload": {"kind": "yield", "delivery": "now"},
                              "accepted": None}


def test_teaching_metrics_by_hand(session):
    base, name, r, *_ = session
    rep = review.load_replay(review.session_path(base, name), r.round_id)
    m0 = next(m for m in rep["teaching"] if m["agent"] == 0)
    rows = [s[0] for s in rep["steps"]]
    acts = [d["action"] for d in rows]
    assert m0["steps"] == len(rows) and m0["comm"] == 1
    assert m0["waits"] == acts.count(0)
    assert m0["ai_rate"] == round(sum(d["action"] == d["ai"] for d in rows) / len(rows), 3)
    assert m0["blocked"] == sum(d["action"] != 0 and d["pos_before"] == d["pos_after"] for d in rows)
    assert {m["agent"] for m in rep["teaching"]} == {0, 2}       # AI 控制的不统计


def test_student_replay_only_own_rows(session):
    base, name, r, p10, p12 = session
    rep = review.student_replay(review.session_path(base, name), r.round_id, p10)
    assert {d["agent"] for d in rep["steps"]} == {0} and set(rep["goals"]) == {0}
    assert "frames" not in rep
    with pytest.raises(ValueError):
        review.student_replay(review.session_path(base, name), r.round_id, 999)


def test_csv(session):
    base, name, r, *_ = session
    data = export.to_csv(review.session_path(base, name), r.round_id)
    assert data.startswith(b"\xef\xbb\xbf")                    # BOM：Excel 识别 UTF-8
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0][:3] == ["课堂", "局", "组"] and len(rows) - 1 == r.game.t * r.num_agents
    first = dict(zip(rows[0], rows[1]))
    assert first["昵称"] == "小甲" and first["控制方"] == "学生" and first["智能体"] == "1"
    assert abs(sum(float(first[f"AI概率_{k}"]) for k in "停上下左右") - 1) < 1e-3


def test_jsonl(session):
    base, name, r, *_ = session
    lines = export.to_jsonl(review.session_path(base, name)).decode("utf-8").strip().splitlines()
    objs = [json.loads(x) for x in lines]
    kinds = [o["type"] for o in objs]
    assert kinds[0] == "round" and kinds.count("decision") == r.game.t * r.num_agents and kinds.count("comm") == 1
    d = next(o for o in objs if o["type"] == "decision" and o["controller"] == "student")
    assert d["view"]["agent"] == d["agent"] and len(d["model_probs"]) == 5


def test_svg_replay_and_mismatch(session, tmp_path):
    base, name, r, *_ = session
    svg = export.to_svg(review.session_path(base, name), r.round_id)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    # 篡改一步记录 -> 重放对不上 -> 报错
    bad = tmp_path / "sessions"; bad.mkdir()
    shutil.copy(base / "sessions" / name, bad / name)
    con = sqlite3.connect(bad / name)
    con.execute("UPDATE step_decision SET action = CASE action WHEN 0 THEN 2 ELSE 0 END WHERE t = 0 AND agent = 0")
    con.commit(); con.close()
    with pytest.raises(export.ReplayMismatch):
        export.to_svg(bad / name, r.round_id)
