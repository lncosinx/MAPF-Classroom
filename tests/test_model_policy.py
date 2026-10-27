"""ModelPolicy：每组一个实例、状态互不串扰、调用顺序受保护、prev_action 用实际执行的动作。"""
import numpy as np
import pytest

from classroom.ai.model_policy import ModelPolicy
from classroom.core.game import GroupGame


def _play(game, steps, override=None):
    out = []
    for t in range(steps):
        ctx = game.begin_step()
        out.append(ctx.model.logits.copy())
        acts = game.default_actions(ctx) if override is None else override(ctx)
        game.apply(acts)
    return out


def test_instances_do_not_share_state(dev_maps):
    ms = dev_maps["random"][0][1]
    a = GroupGame(ms, num_agents=8, seed=0, stop_on_success=False, need_expert=False)
    ref = _play(a, 6)
    # 同时跑另一组（不同地图），再从头跑同一组：结果应与单独跑时一致
    b1 = GroupGame(ms, num_agents=8, seed=0, stop_on_success=False, need_expert=False)
    other = GroupGame(dev_maps["mazes"][0][1], num_agents=8, seed=0, stop_on_success=False, need_expert=False)
    got = []
    for t in range(6):
        ctx = b1.begin_step(); got.append(ctx.model.logits.copy()); b1.apply(b1.default_actions(ctx))
        oc = other.begin_step(); other.apply(other.default_actions(oc))
    for x, y in zip(ref, got):
        np.testing.assert_array_equal(x, y)
    assert a.model.net is not b1.model.net


def test_weights_shared_between_instances():
    p1, p2 = ModelPolicy(4), ModelPolicy(4)
    w1 = next(p1.net.parameters()); w2 = next(p2.net.parameters())
    assert w1.data_ptr() == w2.data_ptr()


def test_prev_action_is_executed_action(dev_maps):
    g = GroupGame(dev_maps["random"][0][1], num_agents=8, seed=0, stop_on_success=False, need_expert=False)
    ctx = g.begin_step()
    executed = np.zeros(8, dtype=np.int64)   # 全体 wait，而不是模型建议
    g.apply(executed)
    np.testing.assert_array_equal(g.model.prev, executed)
    # 观测里的上一步动作 one-hot（scalars[2:7]）也应是 wait
    g.begin_step()
    assert (g.model.last_scalars[:, 2] == 1).all()


def test_call_order_guarded():
    p = ModelPolicy(2)
    with pytest.raises(RuntimeError):
        p.commit([0, 0])


def test_same_seed_reproducible(dev_maps):
    ms = dev_maps["mazes"][1][1]
    r1 = GroupGame(ms, num_agents=8, seed=3, max_steps=40, stop_on_success=False, need_expert=False)
    r2 = GroupGame(ms, num_agents=8, seed=3, max_steps=40, stop_on_success=False, need_expert=False)
    r1.run(); r2.run()
    np.testing.assert_array_equal(np.array(r1.history.actions), np.array(r2.history.actions))
