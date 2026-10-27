"""M0 验收 ①：classroom.ai.madcl + GroupGame 的 env 边界，与服务器生成的 golden 逐步对拍。

覆盖 fixtures/golden_steps_*.npz 的**全部局**。比对口径见 fixtures/README.md：
  - agents_xy / obs / patches / scalars / masks / prev_actions / final_agents_xy：逐项相等；
    唯一例外是 patch 的 ch3（时间衰减足迹）允许 ≤1 ULP（numpy float32 pow 按 CPU 指令集分派，
    服务器与无 AVX512 的机器差 1 ULP，见 classroom/ai/madcl/VENDORED.md）；
  - logits（-inf 位置精确一致）、attn、tta_probs：allclose(rtol=1e-4, atol=1e-3)。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import torch

from classroom.ai.model_policy import ModelPolicy
from classroom.core.coords import OBS_RADIUS
from classroom.core.game import GroupGame

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RTOL, ATOL = 1e-4, 1e-3
FOOTPRINT_CH = 3
FOOTPRINT_ATOL = 1e-6


def _cases():
    for fx in sorted(FIXTURES.glob("golden_steps_*.npz")):
        n_ep = len(np.load(fx)["map_name"])
        for e in range(n_ep):
            yield pytest.param(fx, e, id=f"{fx.stem}-ep{e}")


@pytest.fixture(scope="module", autouse=True)
def _threads():
    torch.set_num_threads(4)


@pytest.mark.parametrize("fx,e", list(_cases()))
def test_golden_episode(fx, e):
    ref = np.load(fx)
    gc = json.loads(str(ref["grid_config_json"][e]))
    N, T = gc["num_agents"], ref["actions"].shape[1]
    assert gc["obs_radius"] == OBS_RADIUS and gc["on_target"] == "nothing" and gc["collision_system"] == "soft"

    # TTA 实例同时给出关 TTA 的 logits/attn（主 net）和 8 视角平均概率（独立的 net_tta）
    policy = ModelPolicy(N, variant="big", seed=0, tta=True)
    game = GroupGame(str(ref["map_str"][e]), num_agents=N, seed=gc["seed"], max_steps=gc["max_episode_steps"],
                     map_name=gc["map_name"], stop_on_success=False, model=policy, need_expert=False)
    np.testing.assert_array_equal(game.grid_padded, ref["grid"][e])
    np.testing.assert_array_equal(game.targets + OBS_RADIUS, ref["targets_xy"][e])

    worst = dict(logits=0.0, attn=0.0, tta=0.0, footprint=0.0)
    for t in range(T):
        np.testing.assert_array_equal(game.positions + OBS_RADIUS, ref["agents_xy"][e, t], err_msg=f"t={t}")
        np.testing.assert_array_equal(np.stack(game.obs), ref["obs"][e, t], err_msg=f"t={t}")
        np.testing.assert_array_equal(policy.prev, ref["prev_actions"][e, t], err_msg=f"t={t}")

        ctx = game.begin_step()
        prop = ctx.model
        p, rp = policy.last_patches, ref["patches"][e, t]
        np.testing.assert_array_equal(np.delete(p, FOOTPRINT_CH, 1), np.delete(rp, FOOTPRINT_CH, 1), err_msg=f"t={t}")
        fp_err = float(np.abs(p[:, FOOTPRINT_CH] - rp[:, FOOTPRINT_CH]).max())
        assert fp_err <= FOOTPRINT_ATOL, f"t={t} 足迹通道误差 {fp_err:.2e}"
        np.testing.assert_array_equal(policy.last_scalars, ref["scalars"][e, t], err_msg=f"t={t}")
        np.testing.assert_array_equal(prop.mask, ref["masks"][e, t], err_msg=f"t={t}")

        rl = ref["logits"][e, t]
        fin = np.isfinite(rl)
        np.testing.assert_array_equal(np.isfinite(prop.logits), fin, err_msg=f"t={t} -inf 位置")
        np.testing.assert_allclose(prop.logits[fin], rl[fin], rtol=RTOL, atol=ATOL, err_msg=f"t={t} logits")
        np.testing.assert_allclose(prop.attn, ref["attn"][e, t], rtol=RTOL, atol=ATOL, err_msg=f"t={t} attn")
        np.testing.assert_allclose(prop.tta_probs, ref["tta_probs"][e, t], rtol=RTOL, atol=ATOL, err_msg=f"t={t} tta")
        worst["logits"] = max(worst["logits"], float(np.abs(prop.logits[fin] - rl[fin]).max()))
        worst["attn"] = max(worst["attn"], float(np.abs(prop.attn - ref["attn"][e, t]).max()))
        worst["tta"] = max(worst["tta"], float(np.abs(prop.tta_probs - ref["tta_probs"][e, t]).max()))
        worst["footprint"] = max(worst["footprint"], fp_err)

        game.apply(ref["actions"][e, t])

    np.testing.assert_array_equal(game.positions + OBS_RADIUS, ref["final_agents_xy"][e])
    print(f"{fx.name} 局{e} 最大误差：" + "，".join(f"{k} {v:.2e}" for k, v in worst.items()))
