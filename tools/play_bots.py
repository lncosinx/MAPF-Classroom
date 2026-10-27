#!/usr/bin/env python3
"""冒烟测试：对一个正在运行的课堂服务，起 1 个老师连接 + K 个机器人学生，完整打一局，再检查数据库。

    python -m classroom.server --port 8000 &        # 先起服务
    python tools/play_bots.py --students 3 --preset doorway

机器人只用自己载荷里的信息朝目标走（走不通就随机换一个合法方向），思考时间随机 0.2–1.5 s。
老师连接来自本机，不需要 key。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sqlite3
import sys
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from classroom.storage.db import STEP_DECISION_COLS  # noqa: E402


def pick(view, rng):
    pr, pc = view["pos"]
    g = view["goal"] or view["goal_proj"]      # 视野外时朝投影点走
    dr, dc = g[0] - pr, g[1] - pc
    if (dr, dc) == (0, 0) or view["at_goal"]:
        return 0
    prefs = [2 if dr > 0 else 1, 4 if dc > 0 else 3]
    if abs(dc) > abs(dr):
        prefs.reverse()
    legal = [a for a in prefs if view["legal"][a] and ((a in (1, 2) and dr) or (a in (3, 4) and dc))]
    if legal and rng.random() < 0.85:
        return legal[0]
    return rng.choice([a for a in range(5) if view["legal"][a]])


async def bot(url, i, rng, done: asyncio.Event, log):
    async with websockets.connect(url + "/ws/student") as ws:
        await ws.send(json.dumps({"type": "join", "nickname": f"机器人{i + 1}", "seat": f"R{i + 1}"}))
        last_t = -1
        async for raw in ws:
            m = json.loads(raw)
            if m["type"] == "state" and m.get("status") == "finished":
                log.append((i, m.get("metrics")))
                done.set()
                return
            if m["type"] == "state" and m.get("status") == "playing" and "view" in m and m["view"]["t"] != last_t:
                last_t = m["view"]["t"]
                await asyncio.sleep(rng.uniform(0.2, 1.5))
                await ws.send(json.dumps({"type": "choose", "t": last_t, "action": pick(m["view"], rng)}))


async def main_async(args):
    http = f"http://{args.host}:{args.port}"
    ws_url = f"ws://{args.host}:{args.port}"
    rng = random.Random(0)
    async with websockets.connect(ws_url + "/ws/teacher") as teacher:
        await teacher.send(json.dumps({"type": "configure", "settings": {
            "preset": args.preset, "countdown_sec": args.countdown, "max_steps": args.max_steps,
            "view_radius": 2, "reveal_mode": "immediate", "fill_policy": "model"}}))
        done, log = asyncio.Event(), []
        tasks = [asyncio.create_task(bot(ws_url, i, random.Random(i), done, log)) for i in range(args.students)]
        await asyncio.sleep(1.0)
        await teacher.send(json.dumps({"type": "start"}))
        t0 = asyncio.get_running_loop().time()
        resolved = []

        async def drain():
            async for raw in teacher:
                m = json.loads(raw)
                if m["type"] == "resolved":
                    resolved.append(asyncio.get_running_loop().time())
                    print(f"  第 {m['t'] + 1} 步结算：{m['actions']}  {m['controllers']}", flush=True)
                elif m["type"] == "error":
                    print("  老师端错误：", m["message"])
        dt = asyncio.create_task(drain())
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=args.max_steps * (args.countdown + 2))
        dt.cancel()
        print(f"一局打完：{len(resolved)} 步，用时 {asyncio.get_running_loop().time() - t0:.1f}s")
        print("终局指标：", log[0][1])
    _ = http


def check_db(data_dir: Path):
    dbs = sorted((data_dir / "sessions").glob("*.sqlite"))
    if not dbs:
        print("没找到数据库（服务用了别的 --data-dir？）")
        return 1
    con = sqlite3.connect(dbs[-1])
    rid = con.execute("SELECT max(id) FROM round WHERE metrics_json IS NOT NULL").fetchone()[0]
    rows = con.execute(f"SELECT {','.join(STEP_DECISION_COLS)} FROM step_decision WHERE round_id=?", (rid,)).fetchall()
    con.close()
    ci = STEP_DECISION_COLS.index("controller")
    stu = [dict(zip(STEP_DECISION_COLS, r)) for r in rows if r[ci] == "student"]
    missing = {k for d in stu for k in STEP_DECISION_COLS if k != "shared_view_json" and d[k] is None}
    print(f"数据库 {dbs[-1].name}：round {rid}，step_decision {len(rows)} 行，其中学生 {len(stu)} 行；"
          f"{'学生行字段齐全' if not missing else '缺字段：' + ','.join(sorted(missing))}")
    return 0 if stu and not missing else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--students", type=int, default=3)
    ap.add_argument("--preset", default="doorway")
    ap.add_argument("--countdown", type=float, default=10)
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args()
    asyncio.run(main_async(args))
    return check_db(Path(args.data_dir))


if __name__ == "__main__":
    sys.exit(main())
