#!/usr/bin/env python3
"""M0 验收 ③："全专家"在所有预设图上 CSR=1。

教学小图用固定起终点；pogema-logo / four-rooms 没有起终点，按默认人数各取 4 个放置种子。
用课堂真实口径：on_target=nothing、soft、128 步、全员到达即结束。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from classroom.core.game import GroupGame  # noqa: E402
from classroom.maps.library import get_preset, list_presets  # noqa: E402
from classroom.maps.validate import validate_instance  # noqa: E402

PLACEMENT_SEEDS = (0, 1, 2, 3)


def main():
    all_ok = True
    print(f"{'预设':<16}{'放置种子':>8}{'N':>4}  {'CSR':>4}{'ISR':>6}{'SoC':>6}{'makespan':>9}  专家来源")
    for name in list_presets():
        seeds = PLACEMENT_SEEDS if get_preset(name).name in ("pogema-logo", "four-rooms") else (0,)
        for s in seeds:
            p = get_preset(name, seed=s)
            assert validate_instance(p.grid, p.agents_xy, p.targets_xy) == []
            n = len(p.agents_xy)
            g = GroupGame(p.map_str, agents_xy=p.agents_xy, targets_xy=p.targets_xy, max_steps=128,
                          controllers=["expert"] * n, stop_on_success=True, expert_time_limit=0.5)
            sources = set()
            while not g.done:
                ctx = g.begin_step()
                sources.add(ctx.expert.source)
                g.apply(g.default_actions(ctx))
            m = g.metrics()
            all_ok &= m["CSR"] == 1.0
            print(f"{name:<16}{s:>8}{n:>4}  {m['CSR']:>4.0f}{m['ISR']:>6.2f}{m['SoC']:>6.0f}{m['makespan']:>9.0f}  "
                  f"{'/'.join(sorted(sources))}")
    print(f"\n验收 ③：{'通过' if all_ok else '未通过'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
