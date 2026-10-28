"""排行榜（PLAN §6.4）：默认按 CSR↓ → ISR↓ → SoC↑ → makespan↑ 的字典序，老师也可以只看某一个指标。

  - 已结束的组用最终指标；进行中的组用"如果现在结束"的临时指标（game.metrics()：没到的按 max_steps 计），标注 provisional；
  - "全 AI 队""专家队"是参照行：按同样的规则插在对应位置，但不占名次；
  - 指标口径与 core/metrics.py 一致。
"""
from __future__ import annotations

from typing import Optional

KEYS = {
    "lex": lambda m: (-m["CSR"], -m["ISR"], m["SoC"], m["makespan"]),
    "CSR": lambda m: (-m["CSR"],),
    "ISR": lambda m: (-m["ISR"],),
    "SoC": lambda m: (m["SoC"],),
    "makespan": lambda m: (m["makespan"],),
}
REF_NAMES = {"ai": "全 AI 队", "expert": "专家队"}


def group_entry(r) -> dict:
    """r：GroupRound。"""
    g = r.game
    finished = r.status == "finished" and r.metrics is not None
    return {"kind": "group", "group": r.group_id, "name": r.group_name, "status": r.status, "t": int(g.t),
            "at_goal": int(g.at_goal().sum()), "N": r.num_agents,
            "metrics": r.metrics if finished else g.metrics(), "provisional": not finished}


def ranking(groups: list, refs: Optional[dict] = None, rank_by: str = "lex") -> list[dict]:
    key = KEYS[rank_by]
    rows = [group_entry(r) for r in groups]
    rows.sort(key=lambda e: (key(e["metrics"]), e["group"]))
    # 并列同名次（1, 2, 2, 4）
    prev, rank = None, 0
    for i, e in enumerate(rows):
        k = key(e["metrics"])
        if k != prev:
            rank, prev = i + 1, k
        e["rank"] = rank
    # 参照行：算完的按同样规则插进去，不占名次；还在算的放最后
    pending = []
    for kind, ref in (refs or {}).items():
        entry = {"kind": kind, "name": REF_NAMES.get(kind, kind), "rank": None, "status": ref.get("status"),
                 "metrics": ref.get("metrics"), "t": ref.get("t"), "N": ref.get("N"), "provisional": False,
                 "at_goal": ref.get("at_goal")}
        if entry["metrics"] is None:
            pending.append(entry)
            continue
        k = key(entry["metrics"])
        pos = next((i for i, e in enumerate(rows) if e["kind"] == "group" and key(e["metrics"]) > k), len(rows))
        rows.insert(pos, entry)
    return rows + pending
