"""地图导入导出：pogema 字符串与 pogema 自己的解析逐项一致；放不进字符串时退回 JSON；错误输入给中文提示。"""
import json

import numpy as np
import pytest
from pogema import GridConfig

from classroom.maps.generate import generate, random_pairs
from classroom.core.coords import parse_map
from classroom.maps.mapio import MapSpec, export_text, parse_text, to_pogema_str


def spec_random(n, seed=0, size=12):
    ms = generate("random", size, size, 0.2, seed=seed)
    s, g = random_pairs(parse_map(ms), n, seed=seed)
    return MapSpec(title="t", map=ms.splitlines(), agents_xy=s.tolist(), targets_xy=g.tolist())


@pytest.mark.parametrize("n", [1, 5, 26])
def test_pogema_string_roundtrip_matches_pogema(n):
    spec = spec_random(n, seed=n)
    fmt, text = export_text(spec)
    assert fmt == "pogema"
    # pogema 自己解析：起终点（按字母顺序）与原 spec 逐项相同
    gc = GridConfig(map=text, obs_radius=5)
    assert [tuple(a) for a in gc.agents_xy] == [tuple(a) for a in spec.agents_xy]
    assert [tuple(t) for t in gc.targets_xy] == [tuple(t) for t in spec.targets_xy]
    back = parse_text(text)
    assert back.map == spec.map and back.agents_xy == spec.agents_xy and back.targets_xy == spec.targets_xy


def test_more_than_26_pairs_falls_back_to_json():
    spec = spec_random(30, seed=3, size=16)
    assert to_pogema_str(spec) is None
    fmt, text = export_text(spec)
    assert fmt == "json"
    back = parse_text(text)
    assert back.agents_xy == spec.agents_xy and back.targets_xy == spec.targets_xy


def test_start_on_other_goal_falls_back_to_json():
    spec = MapSpec(map=["....", "...."], agents_xy=[(0, 0), (1, 1)], targets_xy=[(1, 1), (0, 3)])
    assert to_pogema_str(spec) is None
    assert export_text(spec)[0] == "json"


def test_plain_map_and_json_string_map():
    s = parse_text("..#\n...")
    assert s.map == ["..#", "..."] and s.agents_xy == []
    j = parse_text(json.dumps({"map": "..\n..", "agents_xy": [[0, 0]], "targets_xy": [[1, 1]]}))
    assert j.map == ["..", ".."] and j.agents_xy == [(0, 0)]


@pytest.mark.parametrize("text,frag", [
    ("", "空"), ("..\n.", "一样长"), ("a..\n...", "配对"), ("..?", "不认识"),
    ("{oops", "JSON"), (json.dumps({"map": ["..x"]}), "不认识"),
    ("\n".join(["." * 65] * 2), "64"),
])
def test_bad_inputs(text, frag):
    with pytest.raises(ValueError) as e:
        parse_text(text)
    assert frag in str(e.value), str(e.value)
