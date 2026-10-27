import numpy as np

from classroom.core.coords import parse_map
from classroom.maps.library import get_preset, list_presets
from classroom.maps.validate import validate_instance

G = parse_map("...\n.#.\n..#")


def test_ok():
    assert validate_instance(G, [(0, 0)], [(2, 1)]) == []


def test_on_obstacle():
    assert any("障碍" in e for e in validate_instance(G, [(1, 1)], [(0, 0)]))


def test_overlap():
    errs = validate_instance(G, [(0, 0), (0, 0)], [(0, 1), (0, 2)])
    assert any("重叠" in e for e in errs)


def test_unreachable():
    g = parse_map(".#.\n.#.\n.#.")
    assert any("走不到" in e for e in validate_instance(g, [(0, 0)], [(0, 2)]))


def test_out_of_map():
    assert any("地图外" in e for e in validate_instance(G, [(5, 5)], [(0, 0)]))


def test_all_presets_valid():
    for name in list_presets():
        p = get_preset(name)
        assert validate_instance(p.grid, p.agents_xy, p.targets_xy) == [], name
