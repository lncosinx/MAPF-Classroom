#!/usr/bin/env python3
"""自足性检查：只用 third_party/chameleon_snap 子集 + 纯 pogema，按 golden 记录的动作重放每个文件的第 0 局。

笔记本装好环境后第一件事就跑它：python fixtures/check_selfcontained.py
通过 ⇒ vendor 子集够用、pogema 版本对、权重对、数值环境对。它也是 tests/test_golden.py 的蓝本
（正式测试要覆盖每个文件的全部局、tta_probs，并改用 classroom/ai/madcl 而不是 third_party）。
服务器（4 线程）实测 logits/attn 最大误差约 1.5e-5，但 CPU 争用下出现过一次 2.5e-4 的抖动（不可复现），
故浮点项容差用 atol=1e-3（正式测试再加 rtol=1e-4）。整数/布尔/观测类逐项精确相等。
"""
import ast, json, sys
from pathlib import Path
import numpy as np

P = Path(__file__).resolve().parent.parent
snap = P / "third_party/chameleon_snap"
sys.path.insert(0, str(snap))
import torch, yaml
from pogema import GridConfig, pogema_v0
from models.mapf_comm_net import MAPFCommNet
from envs.observation_builder import ObservationBuilder
from envs.action_mask import build_action_mask_batch

# 只从源码里抠出 build_net（train_il_meta.py 其余 import 依赖训练模块，子集里没有）
src = (snap / "scripts/train_il_meta.py").read_text(encoding="utf-8")
fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "build_net")
ns = {"MAPFCommNet": MAPFCommNet}
exec(compile(ast.Module([fn], []), "build_net", "exec"), ns)

nc = yaml.safe_load((snap / "configs/network_big.yaml").read_text(encoding="utf-8"))
net = ns["build_net"](nc)
sd = torch.load(P / "third_party/weights/capbig_iter_20000.pt", map_location="cpu", weights_only=False)["net"]
net.load_state_dict({k.replace("_orig_mod.", ""): v for k, v in sd.items()}, strict=True)
net.eval(); torch.set_num_threads(4)

for fx in sorted((P / "fixtures").glob("golden_steps_*.npz")):
    ref = np.load(fx)
    e = 0
    gc = json.loads(str(ref["grid_config_json"][e]))
    env = pogema_v0(grid_config=GridConfig(map=str(ref["map_str"][e]), **gc))
    obs, _ = env.reset(seed=gc["seed"])
    grid = np.asarray(env.grid.get_obstacles(), dtype=np.int32)
    N = gc["num_agents"]
    b = ObservationBuilder(num_agents=N, obs_radius=5, use_cost2go=True, cost2go_clip=float(nc["cost2go_clip"]))
    b.reset(); net.reset_comm_state(); net.communication.store_attn = True
    prev = np.zeros(N, dtype=np.int64); max_err = 0.0
    for t in range(ref["actions"].shape[1]):
        axy = np.array(env.get_agents_xy(), dtype=np.int32)
        assert np.array_equal(axy, ref["agents_xy"][e, t])
        p, s = b.build(obs, axy.tolist(), [tuple(x) for x in env.get_targets_xy()], prev, obstacles=grid)
        m = build_action_mask_batch(axy[None], grid)[0]
        assert np.array_equal(p, ref["patches"][e, t]) and np.array_equal(s, ref["scalars"][e, t])
        with torch.no_grad():
            lg, _ = net(torch.from_numpy(p)[None, None], torch.from_numpy(s)[None, None],
                        torch.from_numpy(axy.astype(np.float32))[None, None],
                        action_masks=torch.from_numpy(m)[None, None], reset_comm=False, stateful=True)
        lg = lg[0, 0].numpy(); r = ref["logits"][e, t]; fin = np.isfinite(r)
        assert np.array_equal(np.isfinite(lg), fin)
        max_err = max(max_err, float(np.abs(lg[fin] - r[fin]).max()),
                      float(np.abs(net.communication.last_attn[0, 0].numpy() - ref["attn"][e, t]).max()))
        a = ref["actions"][e, t]
        obs = env.step(a.tolist())[0]; prev = a
    assert np.array_equal(np.array(env.get_agents_xy()), ref["final_agents_xy"][e])
    assert max_err < 1e-3, f"{fx.name} 浮点误差 {max_err:.2e} 超过 1e-3"
    print(f"{fx.name} 局0：仅用 vendor 子集 + 纯 pogema 回放一致，logits/attn 最大误差 {max_err:.2e}")
