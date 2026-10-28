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
    # 二维码作为 <img> 的独立文件，必须是带命名空间的完整 SVG，否则浏览器显示不出来
    qr = client.get("/api/join-qr.svg").text
    assert qr.lstrip().startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in qr


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


def test_student_comm_over_ws(client):
    """两个学生：请求 -> 对方同意 -> 双方都拿到共享视野；发消息 -> 对方收件箱有；老师收到事件；数据库有记录。"""
    with client.websocket_connect(f"/ws/teacher?key={KEY}") as teacher:
        recv_until(teacher, lambda m: m["type"] == "classroom")
        teacher.send_json({"type": "configure", "settings": {
            "preset": "crossroads", "view_radius": 1, "comm_radius": 5.0, "countdown_sec": 60}})
        recv_until(teacher, lambda m: m["type"] == "classroom" and m["round"]["settings"]["preset"] == "crossroads")
        with client.websocket_connect("/ws/student") as a, client.websocket_connect("/ws/student") as b:
            a.send_json({"type": "join", "nickname": "甲", "seat": "1"})
            pa = recv_until(a, lambda m: m["type"] == "welcome")["player"]["id"]
            b.send_json({"type": "join", "nickname": "乙", "seat": "2"})
            pb = recv_until(b, lambda m: m["type"] == "welcome")["player"]["id"]
            # crossroads：0 号 (0,3) 与 2 号 (3,0) 相距 √18，在半径 5 内
            teacher.send_json({"type": "assign", "player_id": pa, "agent": 0})
            teacher.send_json({"type": "assign", "player_id": pb, "agent": 2})
            teacher.send_json({"type": "start"})
            va = recv_until(a, lambda m: m["type"] == "state" and "view" in m)["view"]
            recv_until(b, lambda m: m["type"] == "state" and "view" in m)
            assert {c["agent"] for c in va["contacts"]} >= {2}
            assert va["shared"] == []
            t = va["t"]
            a.send_json({"type": "view_request", "t": t, "to": 2})
            vb = recv_until(b, lambda m: m["type"] == "state" and "view" in m and m["view"]["comm"]["requests_in"])["view"]
            assert vb["comm"]["requests_in"] == [0]
            b.send_json({"type": "view_answer", "t": t, "from": 0, "accept": True})
            va = recv_until(a, lambda m: m["type"] == "state" and "view" in m and m["view"]["shared"])["view"]
            vb = recv_until(b, lambda m: m["type"] == "state" and "view" in m and m["view"]["shared"])["view"]
            assert [s["agent"] for s in va["shared"]] == [2] and [s["agent"] for s in vb["shared"]] == [0]
            a.send_json({"type": "message", "t": t, "to": 2, "kind": "go_first"})
            vb = recv_until(b, lambda m: m["type"] == "state" and "view" in m and m["view"]["comm"]["inbox"])["view"]
            assert vb["comm"]["inbox"] == [{"t": t, "from": 0, "to": 2, "kind": "go_first"}]
            # 超出范围回 error（0 号与 1 号 (6,3) 相距 6）
            a.send_json({"type": "message", "t": t, "to": 1, "kind": "yield"})
            m = a.receive_json()
            while m["type"] != "error":
                m = a.receive_json()
            assert m["message"] == "对方不在通信范围内"
            evs = []
            while len(evs) < 3:
                m = teacher.receive_json()
                if m["type"] == "comm_event":
                    evs.append(m["event"]["type"])
            assert evs == ["view_request", "view_accept", "msg"]
            # 两人都提交 -> 结算，shared_view_json 写库
            a.send_json({"type": "choose", "t": t, "action": 0})
            b.send_json({"type": "choose", "t": t, "action": 0})
            recv_until(a, lambda m: m["type"] == "state" and "view" in m and m["view"]["t"] == t + 1)
            teacher.send_json({"type": "end"})
            recv_until(a, lambda m: m["type"] == "state" and m["status"] == "finished")
    con = sqlite3.connect(next(Path(client.tmp_path, "sessions").glob("*.sqlite")))
    ev = con.execute("SELECT from_agent, to_agent, type, accepted FROM comm_event ORDER BY id").fetchall()
    sv = dict(con.execute("SELECT agent, shared_view_json FROM step_decision WHERE t=0 AND controller='student'").fetchall())
    con.close()
    assert ev == [(0, 2, "view_request", None), (2, 0, "view_accept", 1), (0, 2, "msg", None)]
    assert [s["agent"] for s in json.loads(sv[0])] == [2] and [s["agent"] for s in json.loads(sv[2])] == [0]


def rpc(teacher, method, params, _id=[0]):
    """发一个老师 RPC，等到对应 id 的回复（中途的快照等消息跳过）。"""
    _id[0] += 1
    teacher.send_json({"type": "rpc", "id": _id[0], "method": method, "params": params})
    while True:
        m = teacher.receive_json()
        if m.get("id") == _id[0] and m["type"] in ("rpc_result", "rpc_error"):
            return m


def test_workshop_map_can_start_directly(client):
    """M3 验收：在地图工坊里做出来的图可以直接开局。"""
    with client.websocket_connect(f"/ws/teacher?key={KEY}") as teacher:
        snap = recv_until(teacher, lambda m: m["type"] == "classroom")
        assert {t["name"] for t in snap["templates"]} >= {"lesson:1", "lesson:4"} and snap["defaults"]["preset"]
        g = rpc(teacher, "map.generate", {"kind": "random", "width": 10, "height": 8, "density": 0.15, "seed": 4})
        rows = g["result"]["map"]
        assert len(rows) == 8 and len(rows[0]) == 10
        pl = rpc(teacher, "map.place_random", {"map": rows, "n": 3, "seed": 1})["result"]
        spec = {"title": "工坊测试图", "map": rows, **pl}
        v = rpc(teacher, "map.validate", {"spec": spec})["result"]
        assert v["errors"] == []
        # 起点压在墙上 -> 校验报错
        wall = next((r, c) for r, row in enumerate(rows) for c, ch in enumerate(row) if ch == "#")
        bad = {**spec, "agents_xy": [list(wall)] + pl["agents_xy"][1:]}
        assert any("障碍" in e for e in rpc(teacher, "map.validate", {"spec": bad})["result"]["errors"])
        assert rpc(teacher, "map.save", {"spec": bad, "title": "坏图"})["type"] == "rpc_error"
        # 保存 -> 出现在地图库；导出 -> 导入还原
        name = rpc(teacher, "map.save", {"spec": spec, "title": "工坊测试图"})["result"]["name"]
        snap = recv_until(teacher, lambda m: m["type"] == "classroom" and any(x["name"] == name for x in m["maps"]))
        ex = rpc(teacher, "map.export", {"spec": spec})["result"]
        assert ex["format"] == "pogema"
        im = rpc(teacher, "map.import", {"text": ex["text"]})["result"]
        assert im["spec"]["agents_xy"] == pl["agents_xy"] and im["errors"] == []
        assert rpc(teacher, "map.load", {"name": name})["result"]["spec"]["title"] == "工坊测试图"
        # 不合法的 custom_map 配置被拒
        teacher.send_json({"type": "configure", "settings": {"custom_map": bad}})
        m = teacher.receive_json()
        while m["type"] != "error":
            m = teacher.receive_json()
        # 用工坊的图（未保存版本）直接开局
        teacher.send_json({"type": "configure", "settings": {"custom_map": spec, "view_radius": 2, "countdown_sec": 60}})
        snap = recv_until(teacher, lambda m: m["type"] == "classroom" and m["round"]["preset_title"] == "工坊测试图")
        assert snap["round"]["num_agents"] == 3
        with client.websocket_connect("/ws/student") as ws:
            ws.send_json({"type": "join", "nickname": "丁", "seat": "4"})
            recv_until(ws, lambda m: m["type"] == "welcome")
            teacher.send_json({"type": "start"})
            st = recv_until(ws, lambda m: m["type"] == "state" and "view" in m)
            assert st["view"]["H"] == 8 and st["view"]["W"] == 10
            assert "custom_map" not in json.dumps(st)     # 整张图不会下发给学生
            teacher.send_json({"type": "end"})
        # 删除用户图
        rpc(teacher, "map.delete", {"name": name})
        recv_until(teacher, lambda m: m["type"] == "classroom" and all(x["name"] != name for x in m["maps"]))
        assert rpc(teacher, "map.delete", {"name": "four-rooms"})["type"] == "rpc_error"
        assert rpc(teacher, "nope", {})["type"] == "rpc_error"


def test_ai_hint_in_teacher_state(client):
    with client.websocket_connect(f"/ws/teacher?key={KEY}") as teacher:
        recv_until(teacher, lambda m: m["type"] == "classroom")
        rows = rpc(teacher, "map.generate", {"kind": "maze", "width": 9, "height": 9, "density": 0.5, "seed": 0})["result"]["map"]
        pl = rpc(teacher, "map.place_random", {"map": rows, "n": 8, "seed": 0})["result"]
        teacher.send_json({"type": "configure", "settings": {"custom_map": {"map": rows, **pl}}})
        snap = recv_until(teacher, lambda m: m["type"] == "classroom" and m["round"]["num_agents"] == 8)
        assert snap["round"]["ai_hint"] and "迷宫" in snap["round"]["ai_hint"]


def test_no_bare_state_class_rules():
    """.on / .active / .sel 这类状态类名会被很多元素同时使用；裸写成规则会误伤别的元素
    （曾把选中的按钮压成 9×9 px）。状态类必须带限定前缀，例如 .tool.on。"""
    pat = re.compile(r"(^|[}\s,])\.(on|off|active|sel|selected|yes|no)\s*[{,.:]", re.M)
    for p in STATIC.rglob("*.css"):
        if "vendor" in p.parts:
            continue
        bad = [m.group(0).strip() for m in pat.finditer(p.read_text(encoding="utf-8"))]
        assert not bad, f"{p.name}: {bad}"


def test_two_groups_full_round_and_screen(client):
    """M4：两组各 2 个学生打完；投屏页比赛中不含地图、结束后有；数据库按组分开。"""
    assert client.get("/screen").status_code == 200
    with client.websocket_connect(f"/ws/teacher?key={KEY}") as teacher, client.websocket_connect("/ws/screen") as screen:
        recv_until(teacher, lambda m: m["type"] == "classroom")
        first = screen.receive_json()
        assert first["type"] == "screen" and "join_url" in first
        teacher.send_json({"type": "configure", "settings": {
            "preset": "doorway", "num_groups": 2, "countdown_sec": 60, "max_steps": 30, "view_radius": 2}})
        snap = recv_until(teacher, lambda m: m["type"] == "classroom" and len(m["groups"]) == 2)
        assert [g["group_name"] for g in snap["groups"]] == ["第 1 组", "第 2 组"]
        ctxs, studs = [], []
        for i in range(4):
            cm = client.websocket_connect("/ws/student"); ws = cm.__enter__(); ctxs.append(cm)
            ws.send_json({"type": "join", "nickname": f"生{i}", "seat": str(i)})
            recv_until(ws, lambda m: m["type"] == "welcome")
            studs.append(ws)
        try:
            teacher.send_json({"type": "start"})
            snap = recv_until(teacher, lambda m: m["type"] == "classroom" and all(g["status"] == "playing" for g in m["groups"]))
            sizes = sorted(sum(1 for p in snap["players"] if p["group"] == g) for g in (0, 1))
            assert sizes == [2, 2]
            # 比赛中投屏载荷不含地图
            sc = screen.receive_json()
            while sc["type"] != "screen" or sc["status"] != "playing":
                sc = screen.receive_json()
            assert all("map" not in g for g in sc["groups"])
            done = [False] * 4
            for _ in range(200):
                if all(done):
                    break
                for i, ws in enumerate(studs):
                    if done[i]:
                        continue
                    m = recv_until(ws, lambda m: m["type"] == "state" and (m["status"] == "finished" or "view" in m))
                    if m["status"] == "finished":
                        done[i] = True
                        assert m["group_name"] in ("第 1 组", "第 2 组")
                        continue
                    ws.send_json({"type": "choose", "t": m["view"]["t"], "action": greedy(m["view"])})
            assert all(done)
        finally:
            for cm in ctxs:
                cm.__exit__(None, None, None)
        # 结束后：排行榜两组都有名次，投屏页两组都有全图
        # 比赛期间没读老师端的消息，队列里积压了很多，放宽上限
        lb = recv_until(teacher, lambda m: m["type"] == "leaderboard" and
                        sum(1 for r in m["rows"] if r["kind"] == "group" and r["status"] == "finished") == 2, limit=20000)
        assert sorted(r["rank"] for r in lb["rows"] if r["kind"] == "group")[0] == 1
        sc = screen.receive_json()
        while not (sc["type"] == "screen" and all(g["status"] == "finished" for g in sc["groups"])):
            sc = screen.receive_json()
        assert all("map" in g for g in sc["groups"])
    con = sqlite3.connect(next(Path(client.tmp_path, "sessions").glob("*.sqlite")))
    groups = con.execute("SELECT id, name FROM grp ORDER BY id").fetchall()
    per = dict(con.execute("SELECT group_id, count(DISTINCT round_id) FROM step_decision GROUP BY group_id").fetchall())
    con.close()
    assert [n for _, n in groups][:2] == ["第 1 组", "第 2 组"]
    assert len(per) == 2 and all(v == 1 for v in per.values())


def _play_to_end(teacher, studs, reveal):
    teacher.send_json({"type": "configure", "settings": {"preset": "corridor-swap", "reveal_mode": reveal,
                                                           "countdown_sec": 60, "max_steps": 20, "view_radius": 2}})
    recv_until(teacher, lambda m: m["type"] == "classroom" and m["round"]["settings"]["reveal_mode"] == reveal)
    teacher.send_json({"type": "start"})
    done = [False] * len(studs)
    for _ in range(100):
        if all(done):
            break
        for i, ws in enumerate(studs):
            if done[i]:
                continue
            m = recv_until(ws, lambda m: m["type"] == "state" and (m["status"] == "finished" or "view" in m), limit=500)
            if m["status"] == "finished":
                done[i] = True
                continue
            ws.send_json({"type": "choose", "t": m["view"]["t"], "action": greedy(m["view"])})


def test_review_export_and_student_review(client):
    with client.websocket_connect(f"/ws/teacher?key={KEY}") as teacher:
        recv_until(teacher, lambda m: m["type"] == "classroom")
        with client.websocket_connect("/ws/student") as a, client.websocket_connect("/ws/student") as b:
            for ws, nick in ((a, "甲"), (b, "乙")):
                ws.send_json({"type": "join", "nickname": nick, "seat": "1"})
                recv_until(ws, lambda m: m["type"] == "welcome")
            # 「只给老师看」：结束后学生也不能复盘
            _play_to_end(teacher, [a, b], "teacher")
            a.send_json({"type": "review"})
            m = a.receive_json()
            while m["type"] not in ("error", "review"):
                m = a.receive_json()
            assert m["type"] == "error" and "只给老师" in m["message"]
            # 「赛后复盘」：结束前被拒，结束后拿到的只有自己的行
            teacher.send_json({"type": "configure", "settings": {"preset": "corridor-swap", "reveal_mode": "review",
                                                                   "countdown_sec": 60, "max_steps": 20}})
            recv_until(teacher, lambda m: m["type"] == "classroom" and m["round"]["settings"]["reveal_mode"] == "review")
            teacher.send_json({"type": "start"})
            va = recv_until(a, lambda m: m["type"] == "state" and m.get("status") == "playing" and "view" in m, limit=500)
            recv_until(b, lambda m: m["type"] == "state" and m.get("status") == "playing" and "view" in m, limit=500)
            a.send_json({"type": "review"})
            m = a.receive_json()
            while m["type"] not in ("error", "review"):
                m = a.receive_json()
            assert m["type"] == "error" and "结束后" in m["message"]
            # 走一步再结束（一步没走的局没有记录可复盘）
            t0 = va["view"]["t"]
            a.send_json({"type": "choose", "t": t0, "action": 0})
            b.send_json({"type": "choose", "t": t0, "action": 0})
            recv_until(a, lambda m: m["type"] == "state" and "view" in m and m["view"]["t"] == t0 + 1, limit=500)
            teacher.send_json({"type": "end"})
            recv_until(a, lambda m: m["type"] == "state" and m["status"] == "finished", limit=500)
            a.send_json({"type": "review"})
            rv = recv_until(a, lambda m: m["type"] == "review", limit=500)
            assert rv["steps"] and len({d["agent"] for d in rv["steps"]}) == 1 and "frames" not in rv
        # 老师 RPC：课 -> 局 -> 复盘数据
        ss = rpc(teacher, "review.sessions", {})["result"]["sessions"]
        assert len(ss) == 1 and ss[0]["current"]
        rounds = rpc(teacher, "review.rounds", {"session": ss[0]["name"]})["result"]["rounds"]
        assert len(rounds) == 2
        rep = rpc(teacher, "review.load", {"session": ss[0]["name"], "round": rounds[-1]["id"]})["result"]
        assert len(rep["frames"]) == rounds[-1]["T"] + 1 and rep["teaching"]
        assert rpc(teacher, "review.rounds", {"session": "../../etc/passwd"})["type"] == "rpc_error"
    # 导出：不带 key 被拒（TestClient 不算本机）；带 key 三种格式都能下
    sess, rid = ss[0]["name"], rounds[-1]["id"]
    assert client.get(f"/api/export/{sess}/{rid}.csv").status_code == 403
    r1 = client.get(f"/api/export/{sess}/{rid}.csv?key={KEY}")
    assert r1.status_code == 200 and r1.headers["content-type"].startswith("text/csv")
    assert "filename*=UTF-8''" in r1.headers["content-disposition"] and r1.content.startswith(b"\xef\xbb\xbf")
    r2 = client.get(f"/api/export/{sess}/all.jsonl?key={KEY}")
    assert r2.status_code == 200 and all(json.loads(x) for x in r2.text.strip().splitlines())
    r3 = client.get(f"/api/export/{sess}/{rid}.svg?key={KEY}")
    assert r3.status_code == 200 and r3.headers["content-type"] == "image/svg+xml" and "<svg" in r3.text
    assert client.get(f"/api/export/{sess}/all.svg?key={KEY}").status_code == 400
    assert client.get(f"/api/export/..%2F..%2Fx/{rid}.csv?key={KEY}").status_code in (400, 404)
