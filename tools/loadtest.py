#!/usr/bin/env python3
"""压力测试（M4 验收）：8 组 × 16 体、20×20 地图、40 个机器人学生、关 TTA，一轮从全员提交到推送完成 < 1 s。

自己在单独端口起一个临时服务（数据写到临时目录），不碰正在上课用的服务：
    python tools/loadtest.py                       # 默认 8 组、16 体、40 人、最多 40 步
    python tools/loadtest.py --groups 8 --agents 16 --students 40 --steps 40

延迟口径：对每组每一步 t，
    延迟 = 该组所有学生都收到第 t+1 步状态的时刻 − 该组最后一个学生提交第 t 步的时刻，
即"全员提交 → 结算（模型前向、env.step、写库）→ 下一步整组前向 → 推送"的全过程。
机器人收到新一步后等 0–0.3 s 再提交（模拟真人，也让各组的结算在时间上交错）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parents[1]


def greedy(view, rng):
    pr, pc = view["pos"]
    g = view["goal"] or view["goal_proj"]
    dr, dc = g[0] - pr, g[1] - pc
    prefs = [2 if dr > 0 else 1, 4 if dc > 0 else 3]
    if abs(dc) > abs(dr):
        prefs.reverse()
    legal = [a for a in prefs if view["legal"][a] and ((a in (1, 2) and dr) or (a in (3, 4) and dc))]
    if legal and rng.random() < 0.85:
        return legal[0]
    return rng.choice([a for a in range(5) if view["legal"][a]])


class Stats:
    def __init__(self):
        self.submit = defaultdict(float)      # (组, t) -> 组内最后一次提交时刻
        self.arrive = defaultdict(float)      # (组, t) -> 组内最后一个学生收到第 t 步的时刻
        self.finished = set()


async def bot(url, i, stats: Stats, rng, stop: asyncio.Event):
    async with websockets.connect(url + "/ws/student", max_size=None, proxy=None) as ws:
        await ws.send(json.dumps({"type": "join", "nickname": f"压测{i + 1}", "seat": str(i + 1)}))
        last_t = -1
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw)
            if m.get("type") != "state":
                continue
            group = m.get("group_name")
            if m.get("status") == "finished":
                stats.finished.add(i)
                return
            if m.get("status") != "playing" or "view" not in m:
                continue
            t = m["view"]["t"]
            if t == last_t:
                continue
            now = time.perf_counter()
            stats.arrive[(group, t)] = max(stats.arrive[(group, t)], now)
            last_t = t
            await asyncio.sleep(rng.uniform(0.0, 0.3))
            await ws.send(json.dumps({"type": "choose", "t": t, "action": greedy(m["view"], rng)}))
            stats.submit[(group, t)] = max(stats.submit[(group, t)], time.perf_counter())


async def teacher_setup(url, args):
    """生成 20×20 random 图、随机放 N 对，配置成 k 组（各组同一实例）。"""
    async with websockets.connect(url + "/ws/teacher", max_size=None, proxy=None) as t:
        nid = 0

        async def rpc(method, params):
            nonlocal nid
            nid += 1
            await t.send(json.dumps({"type": "rpc", "id": nid, "method": method, "params": params}))
            while True:
                m = json.loads(await t.recv())
                if m.get("id") == nid:
                    if m["type"] == "rpc_error":
                        raise RuntimeError(m["message"])
                    return m["result"]
        rows = (await rpc("map.generate", {"kind": "random", "width": args.size, "height": args.size,
                                           "density": 0.2, "seed": 0}))["map"]
        pl = await rpc("map.place_random", {"map": rows, "n": args.agents, "seed": 0})
        await t.send(json.dumps({"type": "configure", "settings": {
            "custom_map": {"title": "压测图", "map": rows, **pl}, "num_groups": args.groups,
            "countdown_sec": 60, "max_steps": args.steps, "view_radius": 2, "tta": False}}))
        while True:
            m = json.loads(await t.recv())
            if m["type"] == "classroom" and len(m["groups"]) == args.groups:
                break
            if m["type"] == "error":
                raise RuntimeError(m["message"])


async def main_async(args, url):
    stats, stop = Stats(), asyncio.Event()
    tasks = [asyncio.create_task(bot(url, i, stats, random.Random(i), stop)) for i in range(args.students)]
    await asyncio.sleep(1.5)                           # 等大家都加入
    async with websockets.connect(url + "/ws/teacher", max_size=None, proxy=None) as t:
        await t.send(json.dumps({"type": "start"}))
        t0 = time.perf_counter()
        deadline = t0 + args.steps * 3 + 60
        while len(stats.finished) < args.students and time.perf_counter() < deadline:
            try:
                await asyncio.wait_for(t.recv(), timeout=0.5)   # 老师端也在收消息（和真实课堂一样）
            except asyncio.TimeoutError:
                pass
        elapsed = time.perf_counter() - t0
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    lat = [stats.arrive[(g, t + 1)] - s for (g, t), s in stats.submit.items() if (g, t + 1) in stats.arrive]
    return np.array(lat), elapsed, len({g for g, _ in stats.submit})


def wait_ready(url_http, timeout=90):
    end = time.time() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))    # 本地服务不走代理
    while time.time() < end:
        try:
            opener.open(url_http + "/api/join-info", timeout=1).read()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", type=int, default=8)
    ap.add_argument("--agents", type=int, default=16)
    ap.add_argument("--students", type=int, default=40)
    ap.add_argument("--size", type=int, default=20)
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--port", type=int, default=8768)
    args = ap.parse_args()

    data = tempfile.mkdtemp(prefix="mapf-loadtest-")
    log = open(Path(data) / "server.log", "w", encoding="utf-8")
    server = subprocess.Popen([sys.executable, "-m", "classroom.server", "--port", str(args.port), "--data-dir", data],
                              cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    try:
        http = f"http://127.0.0.1:{args.port}"
        if not wait_ready(http):
            print("服务没起来，见", Path(data) / "server.log")
            return 2

        async def run():
            await teacher_setup(f"ws://127.0.0.1:{args.port}", args)
            return await main_async(args, f"ws://127.0.0.1:{args.port}")
        lat, elapsed, groups = asyncio.run(run())
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()

    print(f"\n规模：{args.groups} 组 × {args.agents} 体，{args.size}×{args.size} 地图，{args.students} 个机器人学生，关 TTA；"
          f"CPU {os.cpu_count()} 核")
    print(f"共测得 {len(lat)} 个组-步（{groups} 组参与），整轮用时 {elapsed:.0f}s")
    if len(lat):
        p50, p95, mx = np.percentile(lat, 50), np.percentile(lat, 95), lat.max()
        print(f"全员提交 → 推送完成：p50 {p50 * 1000:.0f} ms，p95 {p95 * 1000:.0f} ms，max {mx * 1000:.0f} ms")
        ok = p95 < 1.0
        print(f"验收（p95 < 1 s）：{'通过' if ok else '未通过'}")
        return 0 if ok else 1
    print("没有测到数据")
    return 1


if __name__ == "__main__":
    sys.exit(main())
