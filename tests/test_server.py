"""端到端：FastAPI + WS，老师 + 3 个学生打完一局，然后检查数据库；老师端鉴权；静态资源不引用外网。"""
import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from classroom.server.app import STATIC, create_app
from classroom.storage.db import STEP_DECISION_COLS

KEY = "test-key"


def recv_until(ws, pred, limit=200):
    for _ in range(limit):
        m = ws.receive_json()
        if m.get("type") == "error":
            raise AssertionError(m)
        if pred(m):
            return m
    raise AssertionError("没等到期望的消息")


def greedy(view):
    """朝目标走一步（走不通就停），只用学生自己能看到的信息。"""
    pr, pc = view["pos"]
    g = view["goal"] or view["goal_proj"]      # 视野外时朝投影点走
    dr, dc = g[0] - pr, g[1] - pc
    prefs = []
    if abs(dr) >= abs(dc):
        prefs = [2 if dr > 0 else 1, 4 if dc > 0 else 3]
    else:
        prefs = [4 if dc > 0 else 3, 2 if dr > 0 else 1]
    for a in prefs:
        if view["legal"][a] and (dr, dc) != (0, 0):
            return a
    return 0


@pytest.fixture()
def client(tmp_path):
    app = create_app(data_dir=tmp_path, teacher_key=KEY)
    with TestClient(app) as c:
        c.tmp_path = tmp_path
        yield c


def test_pages_served(client):
    for url in ("/", "/teacher", "/static/vendor/vue.global.prod.js", "/static/shared/grid-renderer.js",
                "/static/student/app.js", "/static/teacher/app.js", "/api/join-qr.svg"):
        assert client.get(url).status_code == 200, url
    assert client.get("/api/join-info").json()["url"].startswith("http://")


def test_static_has_no_external_resources():
    """教室可能断网：页面不得引用任何外网脚本、样式、字体或图片。"""
    pat = re.compile(r"""(src|href)\s*=\s*["']https?://|@import\s+url\(\s*["']?https?://|fetch\(\s*["']https?://""")
    for p in STATIC.rglob("*"):
        if p.suffix in (".html", ".css", ".js") and "vendor" not in p.parts:
            text = p.read_text(encoding="utf-8")
            assert not pat.search(text), p
            assert "cdn" not in text.lower(), p


def test_teacher_requires_local_or_key(client):
    # TestClient 的来源主机是 "testclient"，不算本机
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/teacher") as ws:
            ws.receive_json()
    with client.websocket_connect(f"/ws/teacher?key={KEY}") as ws:
        assert ws.receive_json()["type"] == "classroom"


def test_full_round_three_students(client):
    with client.websocket_connect(f"/ws/teacher?key={KEY}") as teacher:
        recv_until(teacher, lambda m: m["type"] == "classroom")
        teacher.send_json({"type": "configure", "settings": {
            "preset": "doorway", "countdown_sec": 60, "max_steps": 40, "view_radius": 2,
            "reveal_mode": "immediate", "fill_policy": "model"}})
        recv_until(teacher, lambda m: m["type"] == "classroom" and m["round"]["settings"]["preset"] == "doorway")

        students, tokens = [], []
        ctxs = [client.websocket_connect("/ws/student") for _ in range(3)]
        for i, cm in enumerate(ctxs):
            ws = cm.__enter__()
            ws.send_json({"type": "join", "nickname": f"同学{i}", "seat": f"A{i}"})
            w = recv_until(ws, lambda m: m["type"] == "welcome")
            tokens.append(w["token"])
            students.append(ws)
        try:
            teacher.send_json({"type": "start"})
            finished = [False] * 3
            reveals = [0] * 3
            for step in range(60):
                if all(finished):
                    break
                for i, ws in enumerate(students):
                    if finished[i]:
                        continue
                    m = recv_until(ws, lambda m: m["type"] in ("state", "reveal") and (
                        m["type"] == "reveal" or m["status"] == "finished" or "view" in m))
                    while m["type"] == "reveal":
                        reveals[i] += 1
                        assert set(m) >= {"you", "expert", "ai"}
                        m = recv_until(ws, lambda m: m["type"] in ("state", "reveal") and (
                            m["type"] == "reveal" or m["status"] == "finished" or "view" in m))
                    if m["status"] == "finished":
                        finished[i] = True
                        assert m["metrics"]["ISR"] >= 0
                        continue
                    ws.send_json({"type": "choose", "t": m["view"]["t"], "action": greedy(m["view"])})
            assert all(finished), "40 步内没跑完"
            assert min(reveals) > 0
        finally:
            for cm in ctxs:
                cm.__exit__(None, None, None)

    dbs = list(Path(client.tmp_path, "sessions").glob("*.sqlite"))
    assert len(dbs) == 1
    con = sqlite3.connect(dbs[0])
    (rid, metrics) = con.execute("SELECT id, metrics_json FROM round WHERE metrics_json IS NOT NULL").fetchone()
    T = con.execute("SELECT count(*) FROM step_state WHERE round_id=?", (rid,)).fetchone()[0]
    rows = con.execute(f"SELECT {','.join(STEP_DECISION_COLS)} FROM step_decision WHERE round_id=?", (rid,)).fetchall()
    players = con.execute("SELECT nickname, seat FROM player").fetchall()
    seats = con.execute("SELECT count(*) FROM seat WHERE round_id=?", (rid,)).fetchone()[0]
    con.close()
    assert T > 0 and len(rows) == T * 5 and seats == 3
    assert sorted(players) == [("同学0", "A0"), ("同学1", "A1"), ("同学2", "A2")]
    student_rows = [dict(zip(STEP_DECISION_COLS, r)) for r in rows if r[STEP_DECISION_COLS.index("controller")] == "student"]
    assert len(student_rows) == T * 3
    for d in student_rows:
        for k in STEP_DECISION_COLS:
            if k != "shared_view_json":
                assert d[k] is not None, k
        assert json.loads(d["view_json"])["agent"] == d["agent"]


def test_reconnect_with_token(client):
    with client.websocket_connect(f"/ws/teacher?key={KEY}") as teacher:
        recv_until(teacher, lambda m: m["type"] == "classroom")
        with client.websocket_connect("/ws/student") as ws:
            ws.send_json({"type": "join", "nickname": "小明", "seat": "B1"})
            w = recv_until(ws, lambda m: m["type"] == "welcome")
        with client.websocket_connect("/ws/student") as ws2:
            ws2.send_json({"type": "join", "nickname": "别的名字", "seat": "", "token": w["token"]})
            w2 = recv_until(ws2, lambda m: m["type"] == "welcome")
            assert w2["player"]["id"] == w["player"]["id"] and w2["player"]["nickname"] == "小明"


def test_remove_offline_and_kick_online(client):
    with client.websocket_connect(f"/ws/teacher?key={KEY}") as teacher:
        recv_until(teacher, lambda m: m["type"] == "classroom")
        # 甲加入后关掉页面 -> 离线但仍在列表里
        with client.websocket_connect("/ws/student") as ws:
            ws.send_json({"type": "join", "nickname": "甲", "seat": "1"})
            recv_until(ws, lambda m: m["type"] == "welcome")
        snap = recv_until(teacher, lambda m: m["type"] == "classroom" and any(
            p["nickname"] == "甲" and not p["online"] for p in m["players"]))
        # 乙在线
        with client.websocket_connect("/ws/student") as ws_b:
            ws_b.send_json({"type": "join", "nickname": "乙", "seat": "2"})
            wb = recv_until(ws_b, lambda m: m["type"] == "welcome")
            recv_until(teacher, lambda m: m["type"] == "classroom" and any(
                p["nickname"] == "乙" and p["online"] for p in m["players"]))
            # 一键清除离线：甲没了，乙还在
            teacher.send_json({"type": "remove", "player_id": None})
            snap = recv_until(teacher, lambda m: m["type"] == "classroom" and all(p["nickname"] != "甲" for p in m["players"]))
            assert [p["nickname"] for p in snap["players"]] == ["乙"]
            # 移除在线的乙：乙收到 kicked，连接被关闭
            teacher.send_json({"type": "remove", "player_id": wb["player"]["id"]})
            recv_until(ws_b, lambda m: m["type"] == "kicked")
            with pytest.raises(WebSocketDisconnect):
                ws_b.receive_json()
        snap = recv_until(teacher, lambda m: m["type"] == "classroom" and m["players"] == [])
        # 乙拿旧 token 重连：token 已作废，算新同学
        with client.websocket_connect("/ws/student") as ws_c:
            ws_c.send_json({"type": "join", "nickname": "乙", "seat": "2", "token": wb["token"]})
            wc = recv_until(ws_c, lambda m: m["type"] == "welcome")
            assert wc["player"]["id"] != wb["player"]["id"]


def test_configure_projection_reaches_student(client):
    with client.websocket_connect(f"/ws/teacher?key={KEY}") as teacher:
        recv_until(teacher, lambda m: m["type"] == "classroom")
        # 不合法的设置被拒绝，回 error，原设置不变
        teacher.send_json({"type": "configure", "settings": {"goal_display": "arrow"}})
        m = teacher.receive_json()
        while m["type"] != "error":
            m = teacher.receive_json()
        teacher.send_json({"type": "configure", "settings": {
            "preset": "fog-maze", "view_radius": 1, "goal_display": "projection", "countdown_sec": 60}})
        snap = recv_until(teacher, lambda m: m["type"] == "classroom" and m["round"]["settings"]["preset"] == "fog-maze")
        assert snap["round"]["settings"]["goal_display"] == "projection"
        with client.websocket_connect("/ws/student") as ws:
            ws.send_json({"type": "join", "nickname": "丙", "seat": "3"})
            recv_until(ws, lambda m: m["type"] == "welcome")
            teacher.send_json({"type": "assign", "player_id": 1, "agent": 0})
            teacher.send_json({"type": "start"})
            st = recv_until(ws, lambda m: m["type"] == "state" and "view" in m)
            v = st["view"]
            # fog-maze：0 号从 (0,0) 去 (8,4)，半径 1 看不到，只能拿到投影 (1,1)
            assert v["goal"] is None and v["goal_proj"] == [1, 1]
            teacher.send_json({"type": "end"})


def test_static_not_stale(client):
    for url in ("/", "/teacher", "/static/shared/grid-renderer.js"):
        assert client.get(url).headers["cache-control"] == "no-cache", url
