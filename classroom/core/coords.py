"""坐标与动作约定。课堂代码一律用 unpadded (row, col)，padded 只出现在 env 边界和模型适配层。

pogema 在地图四周 pad 一圈，宽度 = obs_radius = 5（CLAUDE.md 不变式 2）：
  - env.get_agents_xy() / get_targets_xy() / grid.get_obstacles() 是 padded；
  - GridConfig(agents_xy=..., targets_xy=...) 吃 unpadded。
"""
from __future__ import annotations

import numpy as np

# 模型训练口径，永远是 5；老师设的视野半径只作用于学生显示层
OBS_RADIUS = 5
PAD = OBS_RADIUS

# 动作顺序（不变式 1）：wait 在第 0 位
WAIT, UP, DOWN, LEFT, RIGHT = 0, 1, 2, 3, 4
ACTION_DELTAS = np.array([(0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)], dtype=np.int32)
_DELTA_TO_ACTION = {tuple(d): a for a, d in enumerate(ACTION_DELTAS.tolist())}


def to_padded(xy):
    """unpadded (…,2) -> padded，返回 int32 数组。"""
    return np.asarray(xy, dtype=np.int32) + PAD


def to_unpadded(xy):
    """padded (…,2) -> unpadded，返回 int32 数组。"""
    return np.asarray(xy, dtype=np.int32) - PAD


def unpad_grid(grid_padded: np.ndarray) -> np.ndarray:
    """去掉 pogema 障碍图四周的 pad 圈。"""
    return np.asarray(grid_padded)[PAD:-PAD, PAD:-PAD]


def delta_to_action(dr: int, dc: int) -> int:
    """相邻两格的位移 -> 动作编号；不相邻抛 KeyError。"""
    return _DELTA_TO_ACTION[(int(dr), int(dc))]


def parse_map(map_str: str) -> np.ndarray:
    """pogema 风格地图字符串（'.' 空地、'#' 障碍）-> (H,W) uint8，1=障碍。忽略空行与行首尾空白。"""
    rows = [r.strip() for r in map_str.strip().splitlines() if r.strip()]
    if not rows or len({len(r) for r in rows}) != 1:
        raise ValueError("地图必须是非空的矩形")
    bad = {ch for r in rows for ch in r} - set(".#")
    if bad:
        raise ValueError(f"地图含非法字符：{sorted(bad)}")
    return np.array([[ch == "#" for ch in r] for r in rows], dtype=np.uint8)


def grid_to_str(grid: np.ndarray) -> str:
    return "\n".join("".join("#" if v else "." for v in row) for row in np.asarray(grid))
