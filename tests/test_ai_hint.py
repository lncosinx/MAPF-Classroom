"""AI 能力提示：小迷宫 + 多人要提示；random 图和教学小图不误报。"""
import pytest

from classroom.core.coords import parse_map
from classroom.maps.ai_hint import ai_hint, corridor_ratio
from classroom.maps.generate import generate
from classroom.maps.library import get_preset, list_presets


def test_small_maze_many_agents_warns():
    hits = sum(ai_hint(parse_map(generate("maze", 9, 9, 0.5, seed=s)), 8) is not None for s in range(32))
    assert hits >= 28, hits      # 绝大多数 9×9 迷宫都要提示


def test_small_maze_few_agents_quiet():
    assert all(ai_hint(parse_map(generate("maze", 9, 9, 0.5, seed=s)), 4) is None for s in range(16))


@pytest.mark.parametrize("size,n", [(8, 16), (12, 16), (20, 32)])
def test_random_maps_never_warn(size, n):
    assert all(ai_hint(parse_map(generate("random", size, size, 0.2, seed=s)), n) is None for s in range(32))


def test_medium_maze_soft_warning():
    hints = [ai_hint(parse_map(generate("maze", 13, 13, 0.5, seed=s)), 12) for s in range(16)]
    assert any(h and "80%" in h for h in hints)


def test_builtin_presets_quiet():
    for name in list_presets():
        p = get_preset(name)
        assert ai_hint(p.grid, len(p.agents_xy)) is None, name


def test_corridor_ratio_extremes():
    assert corridor_ratio(parse_map("...\n...\n...")) == pytest.approx(4 / 9)   # 只有 4 个角落邻居 ≤ 2
    assert corridor_ratio(parse_map(".....")) == 1.0
