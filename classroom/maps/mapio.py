"""地图 + 起终点的数据结构，以及导入导出（unpadded 坐标）。

两种文本格式：
  - pogema 字符串：'.' 空地、'#' 障碍、小写字母 = 起点、大写字母 = 终点，同一字母配成一对，按字母顺序编号。
    解析直接复用 pogema 的 GridConfig.str_map_to_list，口径与 pogema 完全一致。
    局限：最多 26 对；一个格子只能写一个字符，所以"某对的起点正好是另一对的终点"写不出来。
  - JSON：{"title", "map": [行...], "agents_xy": [[r,c]...], "targets_xy": [[r,c]...]}，没有上述局限。
导出时能用 pogema 字符串就用它（和别的 pogema 工具互通），否则用 JSON。
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from pogema import GridConfig

MAX_SIDE = 64
MAX_AGENTS = 64
MAX_LETTER_PAIRS = 26
LETTERS = "abcdefghijklmnopqrstuvwxyz"


class MapSpec(BaseModel):
    title: str = Field(default="自定义地图", max_length=40)
    map: list[str]                                  # 每行一个字符串，只含 '.' / '#'
    agents_xy: list[tuple[int, int]] = Field(default_factory=list)
    targets_xy: list[tuple[int, int]] = Field(default_factory=list)

    @field_validator("map")
    @classmethod
    def _check_map(cls, rows):
        rows = [r.strip() for r in rows if r.strip()]
        if not rows:
            raise ValueError("地图是空的")
        if len({len(r) for r in rows}) != 1:
            raise ValueError("地图每一行要一样长")
        if len(rows) > MAX_SIDE or len(rows[0]) > MAX_SIDE:
            raise ValueError(f"地图最大 {MAX_SIDE}×{MAX_SIDE}")
        bad = {ch for r in rows for ch in r} - set(".#")
        if bad:
            raise ValueError(f"地图里有不认识的字符：{''.join(sorted(bad))}")
        return rows

    @field_validator("agents_xy", "targets_xy")
    @classmethod
    def _check_count(cls, xs):
        if len(xs) > MAX_AGENTS:
            raise ValueError(f"最多 {MAX_AGENTS} 个智能体")
        return xs

    @property
    def map_str(self) -> str:
        return "\n".join(self.map)

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.map), len(self.map[0])


def to_pogema_str(spec: MapSpec) -> Optional[str]:
    """pogema 字符串；超过 26 对或有格子要同时写两个字母时返回 None。"""
    n = len(spec.agents_xy)
    if n > MAX_LETTER_PAIRS or len(spec.targets_xy) != n:
        return None
    grid = [list(r) for r in spec.map]
    used = set()
    for i, ((sr, sc), (gr, gc)) in enumerate(zip(spec.agents_xy, spec.targets_xy)):
        for (r, c), ch in (((sr, sc), LETTERS[i]), ((gr, gc), LETTERS[i].upper())):
            if (r, c) in used or grid[r][c] != ".":
                return None   # 格子已被别的字母占用，或压在墙上（这种图本来也过不了校验）
            grid[r][c] = ch
            used.add((r, c))
    return "\n".join("".join(r) for r in grid)


def export_text(spec: MapSpec) -> tuple[str, str]:
    """返回 (格式, 文本)，格式为 'pogema' 或 'json'。"""
    s = to_pogema_str(spec)
    if s is not None:
        return "pogema", s
    return "json", json.dumps(spec.model_dump(), ensure_ascii=False, indent=1)


def parse_text(text: str, title: str = "导入的地图") -> MapSpec:
    """导入：自动识别 JSON 或 pogema 字符串。出错抛 ValueError（中文）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("内容是空的")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 格式不对：第 {e.lineno} 行") from None
        if isinstance(data.get("map"), str):
            data["map"] = data["map"].splitlines()
        data.setdefault("title", title)
        try:
            return MapSpec.model_validate(data)
        except Exception as e:  # noqa: BLE001  pydantic 的报错转成一句中文
            raise ValueError(_first_error(e)) from None
    # pogema 字符串：交给 pogema 自己解析，保证口径一致
    try:
        obstacles, agents, targets, _, _ = GridConfig.str_map_to_list(text, 0, 1)
    except ValueError as e:
        msg = str(e)
        if "Mismatch" in msg:
            raise ValueError("小写字母（起点）和大写字母（终点）没有一一配对") from None
        if "Unsupported symbol" in msg:
            raise ValueError("地图里有不认识的字符（只能用 . # 和字母）") from None
        if "Inconsistent row width" in msg:
            raise ValueError("地图每一行要一样长") from None
        raise ValueError(f"无法解析：{msg}") from None
    rows = ["".join("#" if v else "." for v in row) for row in obstacles]
    try:
        return MapSpec(title=title, map=rows, agents_xy=[tuple(a) for a in agents], targets_xy=[tuple(t) for t in targets])
    except Exception as e:  # noqa: BLE001
        raise ValueError(_first_error(e)) from None


def _first_error(e: Exception) -> str:
    errs = getattr(e, "errors", None)
    if callable(errs):
        first = errs()[0]
        return str(first.get("ctx", {}).get("error") or first.get("msg"))
    return str(e)
