"""导出：CSV（给 Excel）、JSONL（给研究）、SVG（pogema 原生动画）。数据都来自课堂记录（只读）。

  - CSV 用 utf-8-sig（带 BOM）：Windows 的 Excel 双击打开中文不乱码；
  - JSONL：每局先一行 {"type":"round"}，再是该局的 {"type":"decision"}（视野 / 共享视野 / 概率都解析成对象）
    和 {"type":"comm"}；
  - SVG：用记录的地图和起终点新建 pogema env，按记录的联合动作重放，**逐步核对位置**和 step_state 一致，
    再调用 pogema 自己的动画导出。重放出来的就是 pogema 认可的那一局，不是我们自己画的。
导出的数据含学生作答（只有昵称和座位号），前端下载前会提醒老师妥善保管（CLAUDE.md 不变式 14）。
"""
from __future__ import annotations

import csv
import io
import json
from contextlib import closing
from pathlib import Path
from typing import Optional

import numpy as np
from pogema import GridConfig, pogema_v0

from ..core.coords import OBS_RADIUS, PAD
from .review import ACTION_NAMES, _connect

CSV_COLUMNS = [
    ("课堂", "session"), ("局", "round_id"), ("组", "group"), ("步", "t"), ("智能体", "agent"),
    ("控制方", "controller"), ("昵称", "nickname"), ("座位号", "seat"),
    ("动作", "action"), ("动作名", "action_name"), ("专家动作", "expert_action"), ("专家来源", "expert_source"),
    ("AI动作", "model_action"), ("AI概率_停", "p0"), ("AI概率_上", "p1"), ("AI概率_下", "p2"),
    ("AI概率_左", "p3"), ("AI概率_右", "p4"), ("与专家一致", "agree_expert"), ("与AI一致", "agree_ai"),
    ("思考毫秒", "think_ms"), ("超时", "timed_out"), ("改动次数", "n_changes"),
    ("移动前", "pos_before"), ("移动后", "pos_after"), ("到达", "at_goal_after"),
]
CONTROLLER_NAMES = {"student": "学生", "model": "AI", "expert": "专家", "idle": "不动"}


def _rows(con, round_id: Optional[int]):
    sql = ("SELECT d.*, p.nickname, p.seat, g.name AS gname FROM step_decision d "
           "LEFT JOIN player p ON p.id = d.player_id LEFT JOIN grp g ON g.id = d.group_id")
    args = ()
    if round_id is not None:
        sql += " WHERE d.round_id = ?"
        args = (round_id,)
    return con.execute(sql + " ORDER BY d.round_id, d.t, d.agent", args).fetchall()


def to_csv(path: Path, round_id: Optional[int] = None) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in CSV_COLUMNS])
    session = Path(path).stem
    with closing(_connect(path)) as con:
        for d in _rows(con, round_id):
            probs = json.loads(d["model_probs_json"])
            rec = {"session": session, "round_id": d["round_id"], "group": d["gname"], "t": d["t"], "agent": d["agent"] + 1,
                   "controller": CONTROLLER_NAMES.get(d["controller"], d["controller"]), "nickname": d["nickname"] or "",
                   "seat": d["seat"] or "", "action": d["action"], "action_name": ACTION_NAMES[d["action"]],
                   "expert_action": "" if d["expert_action"] is None else d["expert_action"],
                   "expert_source": d["expert_source"] or "", "model_action": d["model_action"],
                   **{f"p{i}": probs[i] for i in range(5)},
                   "agree_expert": "" if d["expert_action"] is None else int(d["action"] == d["expert_action"]),
                   "agree_ai": int(d["action"] == d["model_action"]),
                   "think_ms": "" if d["think_ms"] is None else d["think_ms"], "timed_out": d["timed_out"],
                   "n_changes": d["n_changes"], "pos_before": d["pos_before"], "pos_after": d["pos_after"],
                   "at_goal_after": d["at_goal_after"]}
            w.writerow([rec[c[1]] for c in CSV_COLUMNS])
    # utf-8-sig：Excel（Windows）靠 BOM 识别 UTF-8
    return buf.getvalue().encode("utf-8-sig")


def to_jsonl(path: Path, round_id: Optional[int] = None) -> bytes:
    lines = []
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
    with closing(_connect(path)) as con:
        rounds = con.execute("SELECT r.*, g.name AS gname FROM round r LEFT JOIN grp g ON g.id = r.group_id"
                             + (" WHERE r.id = ?" if round_id is not None else "") + " ORDER BY r.id",
                             (round_id,) if round_id is not None else ()).fetchall()
        for r in rounds:
            lines.append(dump({"type": "round", "round_id": r["id"], "group": r["gname"], "started_at": r["started_at"],
                               "ended_at": r["ended_at"], "settings": json.loads(r["settings_json"]),
                               "map": r["map_str"].splitlines(), "starts": json.loads(r["starts_json"]),
                               "goals": json.loads(r["goals_json"]),
                               "metrics": json.loads(r["metrics_json"]) if r["metrics_json"] else None}))
            for d in _rows(con, r["id"]):
                lines.append(dump({
                    "type": "decision", "round_id": d["round_id"], "group": d["gname"], "t": d["t"], "agent": d["agent"],
                    "controller": d["controller"], "player_id": d["player_id"], "nickname": d["nickname"],
                    "seat": d["seat"], "view": json.loads(d["view_json"]) if d["view_json"] else None,
                    "shared_view": json.loads(d["shared_view_json"]) if d["shared_view_json"] else None,
                    "action": d["action"], "submit_ts": d["submit_ts"], "think_ms": d["think_ms"],
                    "timed_out": bool(d["timed_out"]), "n_changes": d["n_changes"], "expert_action": d["expert_action"],
                    "expert_source": d["expert_source"], "model_action": d["model_action"],
                    "model_probs": json.loads(d["model_probs_json"]), "legal_mask": d["legal_mask"],
                    "pos_before": json.loads(d["pos_before"]), "pos_after": json.loads(d["pos_after"]),
                    "at_goal_after": bool(d["at_goal_after"])}))
            for c in con.execute("SELECT * FROM comm_event WHERE round_id = ? ORDER BY id", (r["id"],)):
                lines.append(dump({"type": "comm", "round_id": c["round_id"], "t": c["t"], "from": c["from_agent"],
                                   "to": c["to_agent"], "kind": c["type"],
                                   "payload": json.loads(c["payload_json"]) if c["payload_json"] else None,
                                   "accepted": None if c["accepted"] is None else bool(c["accepted"])}))
    return ("\n".join(lines) + "\n").encode("utf-8")


class ReplayMismatch(RuntimeError):
    pass


def to_svg(path: Path, round_id: int) -> bytes:
    """重放并导出 pogema 原生 SVG 动画。重放位置与记录不一致时抛 ReplayMismatch。"""
    with closing(_connect(path)) as con:
        r = con.execute("SELECT * FROM round WHERE id = ?", (round_id,)).fetchone()
        if r is None:
            raise ValueError("找不到这一局")
        states = [json.loads(s["positions_json"]) for s in
                  con.execute("SELECT positions_json FROM step_state WHERE round_id = ? ORDER BY t", (round_id,))]
        acts: dict[int, dict[int, int]] = {}
        after: dict[int, dict[int, list]] = {}
        for d in con.execute("SELECT t, agent, action, pos_after FROM step_decision WHERE round_id = ?", (round_id,)):
            acts.setdefault(d["t"], {})[d["agent"]] = d["action"]
            after.setdefault(d["t"], {})[d["agent"]] = json.loads(d["pos_after"])
    if not states:
        raise ValueError("这一局还没有走过一步")
    starts, goals = json.loads(r["starts_json"]), json.loads(r["goals_json"])
    settings = json.loads(r["settings_json"])
    N, T = len(starts), len(states)
    rows = r["map_str"].splitlines()
    gc = GridConfig(map=r["map_str"], agents_xy=starts, targets_xy=goals, num_agents=N, size=max(len(rows), len(rows[0])),
                    obs_radius=OBS_RADIUS, on_target="nothing", collision_system="soft",
                    max_episode_steps=max(int(settings.get("max_steps", T)), T + 1), seed=0)
    env = pogema_v0(grid_config=gc)
    env.enable_animation()
    env.reset(seed=0)
    for t in range(T):
        pos = (np.asarray(env.get_agents_xy()) - PAD).tolist()
        if pos != states[t]:
            raise ReplayMismatch(f"第 {t} 步重放位置和记录不一致")
        env.step([acts.get(t, {}).get(i, 0) for i in range(N)])
    final = (np.asarray(env.get_agents_xy()) - PAD).tolist()
    if T - 1 in after and final != [after[T - 1].get(i) for i in range(N)]:
        raise ReplayMismatch("终局位置和记录不一致")
    return str(env.render_animation()).encode("utf-8")
