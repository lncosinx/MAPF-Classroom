"""AI 能力提示：小迷宫加较多人时 MADCL 明显变弱，开局前告诉老师。

依据 docs/M0_REPORT.md ④ 的实测（每档 32 张图，课堂口径，模型 sample T=1、wait_bias=1、关 TTA）：
  - maze 9×9：8 / 12 / 16 体的 CSR 分别是 0.66 / 0.53 / 0.31（专家都是 1.0）；
  - maze 13×13：12 / 16 体约 0.81 / 0.78；
  - random 8×8 / 12×12：0.88–1.0，不需要提示。
"迷宫类"不看地图是怎么生成的，而是看形状：可通行格里"上下左右只有 ≤ 2 个可走邻居"的格子（走廊格）占比。
实测各生成器各 32 张（2026-10-28）：random 0.2 密度 0.21–0.40；house 0.13–0.37；warehouse 0.07；
maze 0.5 密度 0.39–0.87（9×9 平均 0.70）。阈值取 0.45：random 不误报，绝大多数迷宫能识别。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

CORRIDOR_RATIO = 0.45    # 走廊格占比超过它就算迷宫类
SMALL_AREA = 100         # ≈ 10×10 以内：对应 9×9 档
MEDIUM_AREA = 200        # ≈ 14×14 以内：对应 13×13 档


def corridor_ratio(grid: np.ndarray) -> float:
    """可通行格里，可走邻居数 ≤ 2 的格子占比。"""
    free = np.asarray(grid) == 0
    if not free.any():
        return 0.0
    p = np.pad(free, 1, constant_values=False)
    deg = p[:-2, 1:-1].astype(int) + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]
    return float((deg[free] <= 2).mean())


def ai_hint(grid: np.ndarray, num_agents: int) -> Optional[str]:
    """返回给老师看的一句提示；不需要提示时返回 None。"""
    g = np.asarray(grid)
    area = g.shape[0] * g.shape[1]
    if corridor_ratio(g) <= CORRIDOR_RATIO:
        return None
    if area <= SMALL_AREA and num_agents > 4:
        return (f"这是一张小迷宫，{num_agents} 个智能体会很挤：AI 在这种图上常常互相堵住"
                f"（实测 9×9 迷宫 8 人时 AI 全员到达只有约 2/3）。想让 AI 队友靠谱，可以减少人数或换大一点的图。")
    if area <= MEDIUM_AREA and num_agents >= 12:
        return (f"迷宫图上 {num_agents} 个智能体比较拥挤，AI 队友偶尔会卡住"
                f"（实测 13×13 迷宫 12–16 人时 AI 全员到达约 80%）。")
    return None
