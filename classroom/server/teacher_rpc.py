"""老师端 RPC（地图工坊、地图库、课例模板）。

老师 WS 发 {"type": "rpc", "id", "method", "params"}，服务端回 {"type": "rpc_result", "id", "result"}
或 {"type": "rpc_error", "id", "message"}。错误信息都是中文，直接展示给老师。
地图库 / 模板有变动的方法返回后，调用方会再推一份 classroom 快照。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field, ValidationError

from ..core.coords import parse_map
from ..core.settings import RoundSettings
from ..maps import library
from ..maps.ai_hint import ai_hint
from ..maps.generate import KINDS, generate, random_pairs
from ..maps.mapio import MAX_AGENTS, MAX_SIDE, MapSpec, export_text, parse_text
from ..maps.validate import validate_instance

# 会改动地图库 / 模板列表的方法：返回后要推快照
MUTATING = {"map.save", "map.delete", "template.save", "template.delete"}


class GenerateParams(BaseModel):
    kind: str
    width: int = Field(ge=4, le=MAX_SIDE)
    height: int = Field(ge=4, le=MAX_SIDE)
    density: float = Field(default=0.2, ge=0.0, le=0.6)
    seed: int = 0


class PlaceParams(BaseModel):
    map: list[str]
    n: int = Field(ge=1, le=MAX_AGENTS)
    seed: int = 0


def _spec(params: dict) -> MapSpec:
    try:
        return MapSpec.model_validate(params.get("spec") or {})
    except ValidationError as e:
        first = e.errors()[0]
        raise ValueError(str(first.get("ctx", {}).get("error") or first.get("msg"))) from None


def check_spec(spec: MapSpec) -> dict:
    """校验 + AI 能力提示。errors 为空才能开局。"""
    grid = parse_map(spec.map_str)
    errors = validate_instance(grid, spec.agents_xy, spec.targets_xy)
    return {"errors": errors, "ai_hint": ai_hint(grid, len(spec.agents_xy)) if not errors else None}


async def dispatch(method: str, params: dict) -> Any:
    fn = METHODS.get(method)
    if fn is None:
        raise ValueError(f"不支持的操作：{method}")
    return await fn(params or {})


async def _generate(p: dict):
    try:
        g = GenerateParams.model_validate(p)
    except ValidationError:
        raise ValueError(f"生成参数不对：尺寸要在 4–{MAX_SIDE} 之间，密度在 0–0.6 之间") from None
    if g.kind not in KINDS:
        raise ValueError(f"没有这种地图类型：{g.kind}")
    return {"map": generate(g.kind, g.width, g.height, g.density, seed=g.seed).splitlines()}


async def _place_random(p: dict):
    try:
        q = PlaceParams.model_validate(p)
    except ValidationError:
        raise ValueError(f"人数要在 1–{MAX_AGENTS} 之间") from None
    grid = parse_map("\n".join(q.map))
    starts, goals = random_pairs(grid, q.n, seed=q.seed)     # 放不下时抛 ValueError（中文）
    return {"agents_xy": starts.tolist(), "targets_xy": goals.tolist()}


async def _validate(p: dict):
    return check_spec(_spec(p))


async def _save(p: dict):
    spec = _spec(p)
    title = str(p.get("title") or spec.title).strip()[:40] or "自定义地图"
    spec = spec.model_copy(update={"title": title})
    errs = check_spec(spec)["errors"]
    if errs:
        raise ValueError("地图还有问题，先修好再保存：" + errs[0])
    return {"name": library.save_user_map(spec)}


async def _delete(p: dict):
    try:
        library.delete_user_map(str(p.get("name", "")))
    except KeyError as e:
        raise ValueError(str(e).strip("'\"")) from None
    return {}


async def _load(p: dict):
    name = str(p.get("name", ""))
    try:
        if name.startswith(library.USER_PREFIX):
            spec = library.load_user_map(name)
        else:
            pr = library.get_preset(name, seed=int(p.get("seed", 0)), num_agents=p.get("num_agents"))
            spec = MapSpec(title=pr.title, map=pr.map_str.splitlines(), agents_xy=pr.agents_xy.tolist(),
                           targets_xy=pr.targets_xy.tolist())
    except KeyError as e:
        raise ValueError(str(e).strip("'\"")) from None
    return {"spec": spec.model_dump()}


async def _import(p: dict):
    spec = parse_text(str(p.get("text", "")), title=str(p.get("title") or "导入的地图")[:40])
    return {"spec": spec.model_dump(), **check_spec(spec)}


async def _export(p: dict):
    fmt, text = export_text(_spec(p))
    return {"format": fmt, "text": text}


async def _template_save(p: dict):
    title = str(p.get("title") or "").strip()[:40]
    if not title:
        raise ValueError("给模板起个名字")
    try:
        settings = RoundSettings.model_validate(p.get("settings") or {})
    except ValidationError as e:
        raise ValueError(f"规则不合法：{e.errors()[0]['msg']}") from None
    return {"name": library.save_user_template(title, settings.model_dump(), note=str(p.get("note") or "")[:200])}


async def _template_delete(p: dict):
    try:
        library.delete_user_template(str(p.get("name", "")))
    except KeyError as e:
        raise ValueError(str(e).strip("'\"")) from None
    return {}


METHODS: dict[str, Callable[[dict], Awaitable[Any]]] = {
    "map.generate": _generate,
    "map.place_random": _place_random,
    "map.validate": _validate,
    "map.save": _save,
    "map.delete": _delete,
    "map.load": _load,
    "map.import": _import,
    "map.export": _export,
    "template.save": _template_save,
    "template.delete": _template_delete,
}
