"""坐标约定：GridConfig 吃 unpadded，env 吐 padded；GroupGame 对外一律 unpadded。"""
import numpy as np

from classroom.core.coords import ACTION_DELTAS, PAD, delta_to_action, parse_map, to_padded, to_unpadded
from classroom.core.game import GroupGame
from classroom.maps.library import get_preset


def test_action_order_wait_first():
    assert ACTION_DELTAS.tolist() == [[0, 0], [-1, 0], [1, 0], [0, -1], [0, 1]]
    assert delta_to_action(0, 0) == 0 and delta_to_action(-1, 0) == 1 and delta_to_action(0, 1) == 4


def test_pad_roundtrip():
    xy = np.array([[0, 0], [3, 7]])
    assert PAD == 5
    assert np.array_equal(to_unpadded(to_padded(xy)), xy)


def test_game_boundary_unpadded():
    p = get_preset("doorway")
    g = GroupGame(p.map_str, agents_xy=p.agents_xy, targets_xy=p.targets_xy, need_expert=False)
    assert np.array_equal(g.positions, p.agents_xy)
    assert np.array_equal(g.targets, p.targets_xy)
    assert np.array_equal(np.asarray(g.env.get_agents_xy()), p.agents_xy + PAD)
    assert np.array_equal(g.grid, parse_map(p.map_str))
    # 向右走一步，位置按 unpadded 更新
    acts = np.zeros(len(p.agents_xy), dtype=np.int64); acts[0] = 4
    g.begin_step(); g.apply(acts)
    assert tuple(g.positions[0]) == (0, 1)


def test_legal_actions_walls_and_border():
    g = GroupGame("..\n#.", agents_xy=[[0, 0]], targets_xy=[[1, 1]], need_expert=False)
    legal = g.legal_actions()[0]
    # wait 恒合法；上、左出界；下是墙；右可走
    assert legal.tolist() == [True, False, False, False, True]
