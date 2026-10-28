"""地图库：pogema 自带的 pogema-logo / four-rooms、教学专用小图、老师自己保存的图，以及课例模板。

地图统一成 Preset（unpadded 坐标）。没有固定起终点的图（pogema 自带的两张）按 num_agents + seed
在最大连通区域里随机放置。

老师保存的图和模板放在 <data>/maps/ 下（data/ 不入库），跨课堂长期可用：
  - 地图：<data>/maps/<slug>.json，名字 "my:<slug>"；
  - 模板：<data>/maps/templates/<slug>.json，名字 "mytpl:<slug>"。
slug 由服务端生成，只含 [0-9a-z-]，读文件前再用正则核对一遍，杜绝路径穿越。
"""
from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

from ..core.coords import grid_to_str, parse_map
from .generate import random_pairs
from .mapio import MapSpec

PRESET_DIR = Path(__file__).resolve().parent / "presets"

USER_PREFIX = "my:"
USER_TPL_PREFIX = "mytpl:"
_SLUG = re.compile(r"^[0-9a-z-]{1,40}$")
_user_dir: Path = Path(__file__).resolve().parents[2] / "data" / "maps"


def set_user_dir(path: Path):
    """启动时由服务设定（默认 <仓库>/data/maps）；测试里指向临时目录。"""
    global _user_dir
    _user_dir = Path(path)


def user_dir() -> Path:
    return _user_dir


def _new_slug() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def _slug_path(base: Path, slug: str) -> Path:
    if not _SLUG.match(slug):
        raise KeyError("名字不合法")
    return base / f"{slug}.json"


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
    """内置地图名（不含老师保存的）。"""
    return list(_pogema_defaults()) + list(_teaching())


def preset_from_spec(spec: MapSpec, name: str = "custom") -> Preset:
    return Preset(name, spec.title, spec.map_str, np.asarray(spec.agents_xy, dtype=np.int32).reshape(-1, 2),
                  np.asarray(spec.targets_xy, dtype=np.int32).reshape(-1, 2))


# ---------------------------------------------------------------- 老师保存的地图
def _user_maps() -> list[tuple[str, dict]]:
    out = []
    if _user_dir.exists():
        for p in sorted(_user_dir.glob("*.json")):
            try:
                out.append((p.stem, json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                continue
    return out


def load_user_map(name: str) -> MapSpec:
    if not name.startswith(USER_PREFIX):
        raise KeyError(f"不是自己保存的地图：{name}")
    p = _slug_path(_user_dir, name[len(USER_PREFIX):])
    if not p.exists():
        raise KeyError("这张地图已经不存在了")
    return MapSpec.model_validate(json.loads(p.read_text(encoding="utf-8")))


def save_user_map(spec: MapSpec) -> str:
    _user_dir.mkdir(parents=True, exist_ok=True)
    slug = _new_slug()
    _slug_path(_user_dir, slug).write_text(json.dumps(spec.model_dump(), ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    return USER_PREFIX + slug


def delete_user_map(name: str):
    if not name.startswith(USER_PREFIX):
        raise KeyError("内置地图不能删除")
    p = _slug_path(_user_dir, name[len(USER_PREFIX):])
    if p.exists():
        p.unlink()


def list_maps() -> list[dict]:
    """给老师端的地图列表：内置 + 自己保存的。"""
    out = []
    for name in list_presets():
        p = get_preset(name)
        H, W = p.grid.shape
        out.append({"name": name, "title": p.title, "H": H, "W": W, "n": len(p.agents_xy), "builtin": True,
                    "fixed_agents": name in _teaching()})
    for slug, d in _user_maps():
        try:
            spec = MapSpec.model_validate(d)
        except Exception:  # noqa: BLE001  坏文件跳过
            continue
        H, W = spec.shape
        out.append({"name": USER_PREFIX + slug, "title": spec.title, "H": H, "W": W, "n": len(spec.agents_xy),
                    "builtin": False, "fixed_agents": True})
    return out


# ---------------------------------------------------------------- 课例模板
def _lessons() -> list[dict]:
    return json.loads((PRESET_DIR / "lessons.json").read_text(encoding="utf-8"))


def list_templates() -> list[dict]:
    """[{name, title, note, settings, builtin}]。settings 是 RoundSettings 的部分字段，载入时叠加在默认值上。"""
    out = [{**t, "builtin": True} for t in _lessons()]
    tdir = _user_dir / "templates"
    if tdir.exists():
        for p in sorted(tdir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"name": USER_TPL_PREFIX + p.stem, "title": d.get("title", p.stem), "note": d.get("note", ""),
                        "settings": d.get("settings", {}), "builtin": False})
    return out


def save_user_template(title: str, settings: dict, note: str = "") -> str:
    tdir = _user_dir / "templates"
    tdir.mkdir(parents=True, exist_ok=True)
    slug = _new_slug()
    _slug_path(tdir, slug).write_text(json.dumps({"title": title, "note": note, "settings": settings},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    return USER_TPL_PREFIX + slug


def delete_user_template(name: str):
    if not name.startswith(USER_TPL_PREFIX):
        raise KeyError("内置课例不能删除")
    p = _slug_path(_user_dir / "templates", name[len(USER_TPL_PREFIX):])
    if p.exists():
        p.unlink()


def get_preset(name: str, num_agents: Optional[int] = None, seed: int = 0) -> Preset:
    if name.startswith(USER_PREFIX):
        return preset_from_spec(load_user_map(name), name)
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
