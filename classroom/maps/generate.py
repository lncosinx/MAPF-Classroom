"""地图生成与起终点随机放置的统一入口（输出 pogema 风格字符串：'.' 空地、'#' 障碍）。"""
from __future__ import annotations

from typing import Optional

import numpy as np

from ..core.bfs import UNREACHABLE, bfs_dist
from ..core.coords import grid_to_str, parse_map
from .generators.house_generator import HouseGenerator
from .generators.maze_generator import MazeGenerator
from .generators.random_generator import generate_map
from .generators.warehouse_generator import WarehouseConfig, generate_warehouse

KINDS = ("random", "maze", "warehouse", "house")


def generate(kind: str, width: int, height: int, density: float = 0.2, seed: int = 0) -> str:
    """density ∈ [0,1]，各生成器各自解释：random=障碍比例；maze=墙密度；house=房间多少；warehouse 忽略。
    maze 的实际尺寸约为 (w//2)*2+1，不完全等于请求的尺寸。"""
    if kind == "random":
        s = generate_map({"width": width, "height": height, "obstacle_density": density, "seed": seed})
    elif kind == "maze":
        s = MazeGenerator.generate_maze(width, height, obstacle_density=density, wall_components=4,
                                        go_straight=0.8, seed=seed)
    elif kind == "house":
        # obstacle_ratio 越小房间越多：density 0→8（少），1→3（多）
        ratio = int(round(8 - 5 * float(np.clip(density, 0, 1))))
        s = HouseGenerator.generate(width, height, obstacle_ratio=max(ratio, 2), remove_edge_ratio=6, seed=seed)
    elif kind == "warehouse":
        # 按目标尺寸粗略推出货架行列数；货架 3×1，间隔 1
        cols = max(1, (width - 4 + 1) // 4)
        rows = max(1, (height - 2) // 3)
        cfg = WarehouseConfig(wall_width=3, wall_height=1, walls_in_row=cols, walls_rows=rows,
                              bottom_gap=2, horizontal_gap=1, vertical_gap=2)
        # warehouse 生成器用 '!' 表示空地
        s = generate_warehouse(cfg).replace("!", ".")
    else:
        raise ValueError(f"未知地图类型 {kind}，可选 {KINDS}")
    return grid_to_str(parse_map(s))


def largest_component(grid: np.ndarray) -> np.ndarray:
    """最大连通区域的 (H,W) bool 掩码。"""
    g = np.asarray(grid)
    best = np.zeros_like(g, dtype=bool)
    seen = np.zeros_like(g, dtype=bool)
    for r, c in np.argwhere(g == 0):
        if seen[r, c]:
            continue
        comp = bfs_dist(g, (r, c)) != UNREACHABLE
        seen |= comp
        if comp.sum() > best.sum():
            best = comp
    return best


def random_pairs(grid: np.ndarray, n: int, seed: int = 0, rng: Optional[np.random.Generator] = None):
    """在最大连通区域里随机放 n 对起终点（2n 个格子互不重叠，保证两两可达）。返回 (starts, goals) unpadded。"""
    rng = rng or np.random.default_rng(seed)
    cells = np.argwhere(largest_component(grid))
    if len(cells) < 2 * n:
        raise ValueError(f"可通行区域只有 {len(cells)} 格，放不下 {n} 对起终点")
    idx = rng.choice(len(cells), 2 * n, replace=False)
    return cells[idx[:n]].astype(np.int32), cells[idx[n:]].astype(np.int32)
