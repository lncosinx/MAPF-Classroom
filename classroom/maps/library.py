"""预设地图库：pogema 自带的 pogema-logo / four-rooms + 教学专用小图。

预设统一成 Preset（unpadded 坐标）。没有固定起终点的图（pogema 自带的两张）按 num_agents + seed
在最大连通区域里随机放置。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

from ..core.coords import grid_to_str, parse_map
from .generate import random_pairs

PRESET_DIR = Path(__file__).resolve().parent / "presets"

# pogema 自带图没有起终点，给一个默认人数
_DEFAULT_AGENTS = {"pogema-logo": 16, "four-rooms": 8}
_TITLES = {"pogema-logo": "POGEMA 标志", "four-rooms": "四个房间"}


@dataclass
class Preset:
    name: str
    title: str
    map_str: str
    agents_xy: np.ndarray    # (N,2) unpadded
    targets_xy: np.ndarray   # (N,2) unpadded
    note: str = ""

    @property
    def grid(self) -> np.ndarray:
        return parse_map(self.map_str)


def _teaching() -> dict:
    return json.loads((PRESET_DIR / "teaching.json").read_text(encoding="utf-8"))


def _pogema_defaults() -> dict:
    return yaml.safe_load((PRESET_DIR / "default-maps.yaml").read_text(encoding="utf-8"))


def list_presets() -> list[str]:
    return list(_pogema_defaults()) + list(_teaching())


def get_preset(name: str, num_agents: Optional[int] = None, seed: int = 0) -> Preset:
    teach = _teaching()
    if name in teach:
        v = teach[name]
        return Preset(name, v["title"], grid_to_str(parse_map("\n".join(v["map"]))),
                      np.asarray(v["agents_xy"], dtype=np.int32), np.asarray(v["targets_xy"], dtype=np.int32),
                      v.get("note", ""))
    defaults = _pogema_defaults()
    if name in defaults:
        grid = parse_map(defaults[name])
        n = num_agents or _DEFAULT_AGENTS.get(name, 8)
        starts, goals = random_pairs(grid, n, seed=seed)
        return Preset(name, _TITLES.get(name, name), grid_to_str(grid), starts, goals)
    raise KeyError(f"没有预设图 {name}，可选：{list_presets()}")
