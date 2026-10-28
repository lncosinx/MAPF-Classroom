"""复盘数据（只读）：从课堂记录（data/sessions/*.sqlite）读出一局的完整过程和教学指标。

用只读连接（file:...?mode=ro）打开，和正在写库的本节课互不干扰；以前的课也能这样打开。
会话文件名必须是服务端生成的 YYYYMMDD-HHMMSS.sqlite，读之前先核对，杜绝路径穿越。

教学指标（PLAN §6.4）只统计**学生控制**的步：
  - 和专家一致率、和 AI 一致率：学生实际动作 == 专家 / AI 当步建议；
  - 平均思考时间：提交时刻 − 本步开始（超时没交的不算）；
  - 等待次数：实际动作是 wait；
  - 被挡次数：动作不是 wait，但位置没变（撞人或软碰撞让行，墙在前端就被禁掉了）；
  - 通信次数：该智能体发起的通信事件（请求视野 / 发消息）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

SESSION_RE = re.compile(r"^\d{8}-\d{6}\.sqlite$")
ACTION_NAMES = ["停", "上", "下", "左", "右"]


def session_path(data_dir: Path, name: str) -> Path:
    """会话名 -> 文件路径；名字不合法或文件不存在时抛 ValueError。"""
    if not SESSION_RE.match(name or ""):
        raise ValueError("课堂记录的名字不合法")
    p = Path(data_dir) / "sessions" / name
    if not p.exists():
        raise ValueError("找不到这节课的记录")
    return p


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def list_sessions(data_dir: Path, current: Optional[Path] = None) -> list[dict]:
    out = []
    sdir = Path(data_dir) / "sessions"
    if not sdir.exists():
        return out
    for p in sorted(sdir.glob("*.sqlite"), reverse=True):
        if not SESSION_RE.match(p.name):
            continue
        try:
            with closing(_connect(p)) as con:
                n_rounds = con.execute("SELECT count(*) FROM round WHERE ended_at IS NOT NULL").fetchone()[0]
                n_players = con.execute("SELECT count(*) FROM player").fetchone()[0]
        except sqlite3.Error:
            continue
        stem = p.stem
        out.append({"name": p.name, "label": f"{stem[0:4]}-{stem[4:6]}-{stem[6:8]} {stem[9:11]}:{stem[11:13]}",
                    "rounds": n_rounds, "players": n_players,
                    "current": current is not None and Path(current).resolve() == p.resolve()})
    return out


def _round_title(settings: dict) -> str:
    cm = settings.get("custom_map")
    return cm.get("title", "自定义地图") if cm else settings.get("preset", "")


def list_rounds(path: Path) -> list[dict]:
    """已结束的局（进行中的局复盘没有意义，也可能还在写）。"""
    with closing(_connect(path)) as con:
        rows = con.execute(
            "SELECT r.id, r.group_id, r.started_at, r.ended_at, r.settings_json, r.starts_json, r.metrics_json, "
            "g.name AS gname, (SELECT count(*) FROM step_state s WHERE s.round_id = r.id) AS T "
            "FROM round r LEFT JOIN grp g ON g.id = r.group_id WHERE r.ended_at IS NOT NULL ORDER BY r.id DESC"
        ).fetchall()
    out = []
    for r in rows:
        settings = json.loads(r["settings_json"])
        out.append({"id": r["id"], "group": r["gname"] or "", "title": _round_title(settings),
                    "N": len(json.loads(r["starts_json"])), "T": r["T"], "started_at": r["started_at"],
                    "ended_at": r["ended_at"], "metrics": json.loads(r["metrics_json"]) if r["metrics_json"] else None,
                    "reveal_mode": settings.get("reveal_mode")})
    return out


def _round_meta(con, round_id: int) -> sqlite3.Row:
    r = con.execute("SELECT r.*, g.name AS gname FROM round r LEFT JOIN grp g ON g.id = r.group_id WHERE r.id = ?",
                    (round_id,)).fetchone()
    if r is None:
        raise ValueError("找不到这一局")
    return r


def _decisions(con, round_id: int, player_id: Optional[int] = None) -> list[sqlite3.Row]:
    sql = ("SELECT d.*, p.nickname, p.seat FROM step_decision d LEFT JOIN player p ON p.id = d.player_id "
           "WHERE d.round_id = ?")
    args: list = [round_id]
    if player_id is not None:
        sql += " AND d.player_id = ?"
        args.append(player_id)
    return con.execute(sql + " ORDER BY d.t, d.agent", args).fetchall()


def _decision_dict(d: sqlite3.Row) -> dict:
    probs = json.loads(d["model_probs_json"])
    return {"t": d["t"], "agent": d["agent"], "controller": d["controller"], "player_id": d["player_id"],
            "nickname": d["nickname"], "action": d["action"], "expert": d["expert_action"],
            "expert_source": d["expert_source"], "ai": d["model_action"], "ai_probs": probs,
            "think_ms": d["think_ms"], "timed_out": bool(d["timed_out"]), "n_changes": d["n_changes"],
            "pos_before": json.loads(d["pos_before"]), "pos_after": json.loads(d["pos_after"]),
            "at_goal": bool(d["at_goal_after"])}


def teaching_metrics(decisions: list[dict], comm: list[dict]) -> dict[int, dict]:
    """按智能体汇总教学指标（只统计学生控制的步）。键是智能体编号。"""
    out: dict[int, dict] = {}
    for d in decisions:
        if d["controller"] != "student":
            continue
        m = out.setdefault(d["agent"], {"agent": d["agent"], "nickname": d["nickname"], "steps": 0, "agree_expert": 0,
                                        "expert_steps": 0, "agree_ai": 0, "think_ms": [], "waits": 0, "blocked": 0,
                                        "timeouts": 0, "comm": 0})
        m["steps"] += 1
        if d["expert"] is not None:
            m["expert_steps"] += 1
            m["agree_expert"] += int(d["action"] == d["expert"])
        m["agree_ai"] += int(d["action"] == d["ai"])
        if d["think_ms"] is not None and not d["timed_out"]:
            m["think_ms"].append(d["think_ms"])
        m["waits"] += int(d["action"] == 0)
        m["blocked"] += int(d["action"] != 0 and d["pos_before"] == d["pos_after"])
        m["timeouts"] += int(d["timed_out"])
    for e in comm:
        if e["from"] in out and e["type"] in ("view_request", "msg"):
            out[e["from"]]["comm"] += 1
    for m in out.values():
        m["expert_rate"] = round(m["agree_expert"] / m["expert_steps"], 3) if m["expert_steps"] else None
        m["ai_rate"] = round(m["agree_ai"] / m["steps"], 3) if m["steps"] else None
        m["avg_think_ms"] = round(sum(m["think_ms"]) / len(m["think_ms"])) if m["think_ms"] else None
        del m["think_ms"]
    return out


def _comm(con, round_id: int) -> list[dict]:
    return [{"t": c["t"], "from": c["from_agent"], "to": c["to_agent"], "type": c["type"],
             "payload": json.loads(c["payload_json"]) if c["payload_json"] else None,
             "accepted": None if c["accepted"] is None else bool(c["accepted"])}
            for c in con.execute("SELECT * FROM comm_event WHERE round_id = ? ORDER BY id", (round_id,))]


def load_replay(path: Path, round_id: int) -> dict:
    """一局的完整复盘数据（老师用）。frames[t] 是第 t 步决策时所有智能体的位置，最后一帧是终局。"""
    with closing(_connect(path)) as con:
        r = _round_meta(con, round_id)
        frames = [json.loads(s["positions_json"]) for s in
                  con.execute("SELECT positions_json FROM step_state WHERE round_id = ? ORDER BY t", (round_id,))]
        decisions = [_decision_dict(d) for d in _decisions(con, round_id)]
        comm = _comm(con, round_id)
    N = len(json.loads(r["starts_json"]))
    if decisions:
        last_t = max(d["t"] for d in decisions)
        final = [None] * N
        for d in decisions:
            if d["t"] == last_t:
                final[d["agent"]] = d["pos_after"]
        frames.append(final)
    steps = [[None] * N for _ in range(len(frames) - 1)] if frames else []
    for d in decisions:
        steps[d["t"]][d["agent"]] = d
    settings = json.loads(r["settings_json"])
    return {"round": r["id"], "group": r["gname"] or "", "title": _round_title(settings), "settings": settings,
            "map": r["map_str"].splitlines(), "starts": json.loads(r["starts_json"]), "goals": json.loads(r["goals_json"]),
            "metrics": json.loads(r["metrics_json"]) if r["metrics_json"] else None,
            "frames": frames, "steps": steps, "comm": comm,
            "teaching": sorted(teaching_metrics(decisions, comm).values(), key=lambda m: m["agent"])}


def student_replay(path: Path, round_id: int, player_id: int) -> dict:
    """学生复盘：只含本人控制的那些步、本人的轨迹和教学指标，不含别人的轨迹。"""
    with closing(_connect(path)) as con:
        r = _round_meta(con, round_id)
        decisions = [_decision_dict(d) for d in _decisions(con, round_id, player_id)]
        comm = [c for c in _comm(con, round_id)]
    if not decisions:
        raise ValueError("这一局里没有你的记录")
    mine = {d["agent"] for d in decisions}
    comm = [c for c in comm if c["from"] in mine or c["to"] in mine]
    goals = json.loads(r["goals_json"])
    return {"round": r["id"], "group": r["gname"] or "", "title": _round_title(json.loads(r["settings_json"])),
            "map": r["map_str"].splitlines(), "steps": decisions, "goals": {a: goals[a] for a in mine},
            "metrics": json.loads(r["metrics_json"]) if r["metrics_json"] else None,
            "teaching": sorted(teaching_metrics(decisions, comm).values(), key=lambda m: m["agent"])}
