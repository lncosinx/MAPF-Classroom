"""指标口径：手算用例 + 与 pogema info['metrics'] 对拍 CSR / ISR。"""
import numpy as np
import pytest

from classroom.core.game import GroupGame
from classroom.core.metrics import episode_metrics


def test_last_arrival_not_first():
    # 0 号 t=1 到达、t=2 离开、t=3 再到达 -> arrive_t=3；1 号从未到达 -> max_steps
    hist = np.array([[0, 0], [1, 0], [0, 0], [1, 0], [1, 0]], dtype=bool)
    m = episode_metrics(hist, np.array([True, False]), max_steps=5)
    assert m == dict(CSR=0.0, ISR=0.5, SoC=3.0 + 5.0, makespan=5.0)


def test_start_on_goal_counts_zero():
    m = episode_metrics(np.ones((4, 1), dtype=bool), np.array([True]), max_steps=4)
    assert m["SoC"] == 0.0 and m["CSR"] == 1.0


@pytest.mark.parametrize("fam,k,n", [("random", 0, 8), ("random", 1, 16), ("mazes", 0, 8), ("mazes", 2, 16)])
def test_against_pogema_info(dev_maps, fam, k, n):
    name, ms = dev_maps[fam][k]
    g = GroupGame(ms, num_agents=n, seed=0, max_steps=64, stop_on_success=True, need_expert=False)
    m = g.run()
    pm = g.last_info[0]["metrics"]   # 首次终局（全员到达或截断）那一步的 pogema 指标是可靠的
    assert m["CSR"] == pm["CSR"] and m["ISR"] == pytest.approx(pm["ISR"])
    if m["CSR"] == 1.0:
        # 全员到达且到达后没再离开时，两种口径的 makespan 一致
        assert m["makespan"] == pm["makespan"]
