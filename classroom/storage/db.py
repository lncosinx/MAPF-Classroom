"""课堂记录：每节课一个 SQLite 文件（data/sessions/<日期时间>.sqlite，已 gitignore，绝不入库）。

所有读写都走一个单线程 executor：sqlite 连接只在那个线程里用，也不阻塞事件循环。
隐私（不变式 14）：player 表只存昵称和座位号，不存真实姓名。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
  id INTEGER PRIMARY KEY, started_at REAL NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS grp (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS player (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, nickname TEXT NOT NULL, seat TEXT,
  joined_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS round (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
  started_at REAL NOT NULL, ended_at REAL,
  settings_json TEXT NOT NULL, map_str TEXT NOT NULL, starts_json TEXT NOT NULL, goals_json TEXT NOT NULL,
  metrics_json TEXT
);
CREATE TABLE IF NOT EXISTS seat (
  round_id INTEGER NOT NULL, player_id INTEGER NOT NULL, agent INTEGER NOT NULL, assigned_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS step_decision (
  id INTEGER PRIMARY KEY,
  round_id INTEGER NOT NULL, group_id INTEGER NOT NULL, t INTEGER NOT NULL, agent INTEGER NOT NULL,
  controller TEXT NOT NULL,            -- student / model / expert / idle
  player_id INTEGER,                   -- 学生控制时的玩家
  view_json TEXT,                      -- 学生当时实际看到的载荷（AI 控制时为空）
  shared_view_json TEXT,               -- 别人共享来的视野（M2）
  action INTEGER NOT NULL,             -- 实际执行的动作（0=wait,1=up,2=down,3=left,4=right）
  submit_ts REAL, think_ms REAL, timed_out INTEGER NOT NULL, n_changes INTEGER NOT NULL,
  expert_action INTEGER, expert_source TEXT,
  model_action INTEGER NOT NULL, model_probs_json TEXT NOT NULL,
  legal_mask TEXT NOT NULL,            -- 5 位 0/1 字符串
  pos_before TEXT NOT NULL, pos_after TEXT NOT NULL, at_goal_after INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS comm_event (
  id INTEGER PRIMARY KEY, round_id INTEGER NOT NULL, group_id INTEGER NOT NULL, t INTEGER NOT NULL,
  from_agent INTEGER NOT NULL, to_agent INTEGER, type TEXT NOT NULL, payload_json TEXT, accepted INTEGER
);
CREATE TABLE IF NOT EXISTS step_state (
  round_id INTEGER NOT NULL, t INTEGER NOT NULL, positions_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_step_decision ON step_decision(round_id, t);
"""

STEP_DECISION_COLS = (
    "round_id", "group_id", "t", "agent", "controller", "player_id", "view_json", "shared_view_json",
    "action", "submit_ts", "think_ms", "timed_out", "n_changes", "expert_action", "expert_source",
    "model_action", "model_probs_json", "legal_mask", "pos_before", "pos_after", "at_goal_after",
)


def _dumps(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        self._conn: Optional[sqlite3.Connection] = None
        self.session_id: Optional[int] = None

    @classmethod
    def for_new_session(cls, data_dir: Path) -> "Database":
        name = time.strftime("%Y%m%d-%H%M%S") + ".sqlite"
        return cls(Path(data_dir) / "sessions" / name)

    # ---- 线程内的同步实现 ----
    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.executescript(SCHEMA)
        return self._conn

    def _exec(self, sql: str, args: Iterable = ()) -> int:
        c = self._c()
        cur = c.execute(sql, tuple(args))
        c.commit()
        return cur.lastrowid

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._ex, fn, *args)

    # ---- 对外的异步接口 ----
    async def start_session(self, note: str = "") -> int:
        self.session_id = await self._run(self._exec, "INSERT INTO session(started_at, note) VALUES (?,?)",
                                          (time.time(), note))
        return self.session_id

    async def add_group(self, name: str) -> int:
        return await self._run(self._exec, "INSERT INTO grp(session_id, name) VALUES (?,?)", (self.session_id, name))

    async def add_player(self, nickname: str, seat: Optional[str]) -> int:
        return await self._run(self._exec, "INSERT INTO player(session_id, nickname, seat, joined_at) VALUES (?,?,?,?)",
                               (self.session_id, nickname, seat, time.time()))

    async def start_round(self, group_id: int, settings: dict, map_str: str, starts, goals) -> int:
        return await self._run(
            self._exec,
            "INSERT INTO round(session_id, group_id, started_at, settings_json, map_str, starts_json, goals_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.session_id, group_id, time.time(), _dumps(settings), map_str, _dumps(starts), _dumps(goals)))

    async def add_seat(self, round_id: int, player_id: int, agent: int):
        await self._run(self._exec, "INSERT INTO seat(round_id, player_id, agent, assigned_at) VALUES (?,?,?,?)",
                        (round_id, player_id, agent, time.time()))

    async def write_step(self, round_id: int, t: int, positions, rows: list[dict]):
        def work():
            c = self._c()
            c.executemany(
                f"INSERT INTO step_decision({','.join(STEP_DECISION_COLS)}) "
                f"VALUES ({','.join('?' * len(STEP_DECISION_COLS))})",
                [tuple(r[k] for k in STEP_DECISION_COLS) for r in rows])
            c.execute("INSERT INTO step_state(round_id, t, positions_json) VALUES (?,?,?)",
                      (round_id, t, _dumps(positions)))
            c.commit()
        await self._run(work)

    async def end_round(self, round_id: int, metrics: dict):
        await self._run(self._exec, "UPDATE round SET ended_at=?, metrics_json=? WHERE id=?",
                        (time.time(), _dumps(metrics), round_id))

    async def query(self, sql: str, args: Iterable = ()) -> list[tuple]:
        return await self._run(lambda: self._c().execute(sql, tuple(args)).fetchall())

    async def close(self):
        def work():
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        await self._run(work)
        self._ex.shutdown(wait=True)
