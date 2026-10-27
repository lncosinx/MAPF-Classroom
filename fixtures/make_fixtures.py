#!/usr/bin/env python3
"""生成 / 自检 MADCL 逐步对拍基准（golden steps）。★ 只能在有 Chameleon 快照的服务器上跑。

笔记本上不需要运行本脚本，只消费它的产物 golden_steps_<family>.npz（见 fixtures/README.md）。
本文件随项目拷走只为留存来源：基准是怎么生成的、用的什么口径，一目了然。

口径（写死，不依赖任何默认值）：
    on_target=nothing / collision=soft / obs_radius=5 / max_steps=128 / seed=0
    规模：每族 N=16 × 2 张图（golden_steps_<family>_n16.npz）+ N=32 × 1 张图（_n32.npz，拥挤、通信多）
    解码 sample + T=1.0 + wait_bias=1.0，关 TTA；同时并行跑一份 TTA 网络只记录其平均概率。
    实际执行的动作由关 TTA 的分支采样（torch.Generator 固定种子），并逐步落盘。

用法：
    python make_fixtures.py --snap <git archive 解出的 Chameleon 快照> \
        --weights ../third_party/weights/capbig_iter_20000.pt \
        --network-config <snap>/configs/network_big.yaml \
        --maps-dir ../third_party/maps/dev --out .
    python make_fixtures.py ... --verify   # 用纯 pogema 重建环境、按记录动作回放、逐项比对

★ 纯 pogema 2.0 的 env 没有 get_global_obstacles()；全局障碍（padded）取 env.grid.get_obstacles()，
  与 PogemaWrapper._extract_global_info 实际走的兜底分支一致。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

FAMILIES = ("random", "mazes")
CASES = ((16, 2), (32, 1))   # (智能体数, 每族取前几张 dev 图)
SEED = 0
MAX_STEPS = 128
BASE_TEMP = 1.0
WAIT_BIAS = 1.0
SAMPLE_SEED = 20261026
GC_KEYS = ("size", "num_agents", "max_episode_steps", "obs_radius",
           "collision_system", "on_target", "seed", "map_name")


def load_net(build_net, nc, ckpt, torch):
    net = build_net(nc)
    sd = torch.load(ckpt, map_location="cpu", weights_only=False)["net"]
    net.load_state_dict({k.replace("_orig_mod.", ""): v for k, v in sd.items()}, strict=True)
    return net.eval()


def tta_probs(net, pt, st, po, mt, masks_np, d4, torch):
    """与快照 eval_bench.rollout_chunk 的 TTA 分支逐行一致（E=1）。返回 (N,5) 平均概率。"""
    transform_patch, transform_dir, perms, elems = d4
    pv, sv, mv = [], [], []
    for k, flip in elems:
        pv.append(transform_patch(pt, k, flip))
        s2 = st.clone()
        s2[..., 0:2] = transform_dir(st[..., 0:2], k, flip)
        perm = perms[elems.index((k, flip))]
        s2[..., 2:7] = torch.zeros_like(st[..., 2:7]).index_copy(-1, perm, st[..., 2:7])
        sv.append(s2)
        mv.append(torch.zeros_like(mt).index_copy(-1, perm, mt))
    logits, _ = net(torch.cat(pv, 0), torch.cat(sv, 0), po.repeat(8, 1, 1, 1),
                    action_masks=torch.cat(mv, 0), reset_comm=False, stateful=True)
    probs_sum = 0.0
    for gi in range(len(elems)):
        pr = torch.softmax(logits[gi:gi + 1, 0], dim=-1)
        probs_sum = probs_sum + pr[..., perms[gi]]
    return (probs_sum / 8.0)[0]


def run_episode(num_agents, env_step, env_state, builder, nets, d4, gen, torch, build_obs, build_mask,
                actions_to_replay=None):
    """跑（或按给定动作回放）一局，逐步返回记录。env_step/env_state 屏蔽 wrapper 与纯 pogema 的差异。"""
    net, net_tta = nets
    net.reset_comm_state(); net_tta.reset_comm_state()
    net.communication.store_attn = True
    builder.reset()
    rec = {k: [] for k in ("agents_xy", "obs", "patches", "scalars", "masks", "prev_actions",
                           "logits", "tta_probs", "attn", "actions")}
    prev = np.zeros(num_agents, dtype=np.int64)
    obs = env_state["obs"]
    grid = env_state["grid"]
    for t in range(MAX_STEPS):
        axy = np.array(env_state["agents_xy"](), dtype=np.int32)
        txy = env_state["targets_xy"]()
        patches, scalars = build_obs(obs, axy.tolist(), txy, prev, builder, grid)
        masks = build_mask(axy[np.newaxis], grid)[0]
        pt = torch.from_numpy(patches)[None, None]
        st = torch.from_numpy(scalars)[None, None]
        po = torch.from_numpy(axy.astype(np.float32))[None, None]
        mt = torch.from_numpy(masks)[None, None]
        with torch.no_grad():
            logits, _ = net(pt, st, po, action_masks=mt, reset_comm=False, stateful=True)
            lg = logits[0, 0]
            attn = net.communication.last_attn[0, 0].clone()
            tp = tta_probs(net_tta, pt, st, po, mt, masks, d4, torch)
        if actions_to_replay is None:
            lg2 = lg.clone()
            lg2[..., 0] = lg2[..., 0] - WAIT_BIAS
            probs = torch.softmax(lg2 / BASE_TEMP, dim=-1)
            acts = torch.multinomial(probs, 1, generator=gen)[:, 0].numpy().astype(np.int64)
        else:
            acts = actions_to_replay[t].astype(np.int64)
        rec["agents_xy"].append(axy)
        rec["obs"].append(np.stack([np.asarray(o, dtype=np.float32) for o in obs]))
        rec["patches"].append(patches); rec["scalars"].append(scalars)
        rec["masks"].append(masks); rec["prev_actions"].append(prev.copy())
        rec["logits"].append(lg.numpy()); rec["tta_probs"].append(tp.numpy())
        rec["attn"].append(attn.numpy()); rec["actions"].append(acts)
        obs = env_step(acts.tolist())
        prev = acts
    rec["final_agents_xy"] = np.array(env_state["agents_xy"](), dtype=np.int32)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snap", required=True)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--network-config", required=True)
    ap.add_argument("--maps-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    sys.path.insert(0, str(Path(args.snap).resolve()))
    import torch
    import yaml
    from pogema import GridConfig, pogema_v0
    from scripts.train_il_meta import build_net
    from scripts.eval_bench import make_env, load_maps
    from envs.observation_builder import ObservationBuilder
    from envs.action_mask import build_action_mask_batch
    from envs.d4_augment import transform_patch, transform_dir, _PERMS, _ELEMS

    torch.set_num_threads(args.threads)
    nc = yaml.safe_load(open(args.network_config, encoding="utf-8"))
    c2g_clip = float(nc.get("cost2go_clip", 10.0))
    nets = (load_net(build_net, nc, args.weights, torch), load_net(build_net, nc, args.weights, torch))
    d4 = (transform_patch, transform_dir, list(_PERMS), list(_ELEMS))

    def build_obs(obs, axy, txy, prev, builder, grid):
        return builder.build(obs, axy, txy, prev, obstacles=grid)

    out_dir = Path(args.out)
    for fam, (num_agents, num_maps) in ((f, c) for f in FAMILIES for c in CASES):
        maps = load_maps(fam, 0, str(Path(args.maps_dir) / f"{fam}.yaml"))[:num_maps]
        path = out_dir / f"golden_steps_{fam}_n{num_agents}.npz"
        if args.verify:
            ref = np.load(path, allow_pickle=False)
        episodes = []
        for ei, (name, map_str) in enumerate(maps):
            builder = ObservationBuilder(num_agents=num_agents, obs_radius=5, use_cost2go=True,
                                         cost2go_clip=c2g_clip)
            if not args.verify:
                # 生成：走快照 eval_bench.make_env（PogemaWrapper）这条权威路径
                w, obs = make_env(map_str, name, num_agents, MAX_STEPS, "nothing", SEED)
                g = w.unwrapped_env.grid_config
                gc = {k: getattr(g, k) for k in GC_KEYS}
                state = dict(obs=obs, grid=w.global_grid, agents_xy=w.get_agents_xy,
                             targets_xy=w.get_targets_xy)
                step = lambda a, w=w: w.step(a)[0]
                gen = torch.Generator().manual_seed(SAMPLE_SEED + ei)
                rec = run_episode(num_agents, step, state, builder, nets, d4, gen, torch, build_obs,
                                  build_action_mask_batch)
                rec.update(map_str=map_str, gc=gc, targets_xy=np.array(w.get_targets_xy(), np.int32),
                           grid=np.asarray(w.global_grid, np.int8))
            else:
                # 自检：只用 GridConfig 原始参数 + 纯 pogema 重建（=笔记本要走的路径），按记录动作回放
                gc = json.loads(str(ref["grid_config_json"][ei]))
                env = pogema_v0(grid_config=GridConfig(map=str(ref["map_str"][ei]), **gc))
                obs, _ = env.reset(seed=gc["seed"])
                grid = np.asarray(env.grid.get_obstacles(), dtype=np.int32)
                state = dict(obs=obs, grid=grid,
                             agents_xy=lambda env=env: [tuple(a) for a in env.get_agents_xy()],
                             targets_xy=lambda env=env: [tuple(a) for a in env.get_targets_xy()])
                step = lambda a, env=env: env.step(a)[0]
                rec = run_episode(num_agents, step, state, builder, nets, d4, None, torch, build_obs,
                                  build_action_mask_batch, actions_to_replay=ref["actions"][ei])
                for k in ("agents_xy", "obs", "patches", "scalars", "masks", "prev_actions"):
                    assert np.array_equal(np.stack(rec[k]), ref[k][ei]), f"{fam}[{ei}] {k} 不一致"
                assert np.array_equal(rec["final_agents_xy"], ref["final_agents_xy"][ei])
                assert np.array_equal(grid, ref["grid"][ei])
                for k in ("logits", "tta_probs", "attn"):
                    a, b = np.stack(rec[k]), ref[k][ei]
                    fin = np.isfinite(b)
                    assert np.array_equal(np.isfinite(a), fin), f"{fam}[{ei}] {k} 掩码位置不一致"
                    err = float(np.abs(a[fin] - b[fin]).max())
                    assert err < 1e-5, f"{fam}[{ei}] {k} 最大误差 {err}"
                print(f"[verify] {fam} N={num_agents} {name}: 全部一致", flush=True)
                continue
            episodes.append(rec)
            print(f"[golden] {fam} N={num_agents} {name}: ISR={float(np.mean(np.all(rec['final_agents_xy'] == rec['targets_xy'], 1))):.3f}",
                  flush=True)
        if args.verify:
            continue
        stack = lambda k: np.stack([np.stack(e[k]) for e in episodes])
        np.savez_compressed(
            path,
            map_name=np.array([e["gc"]["map_name"] for e in episodes]),
            map_str=np.array([e["map_str"] for e in episodes]),
            grid_config_json=np.array([json.dumps(e["gc"]) for e in episodes]),
            grid=np.stack([e["grid"] for e in episodes]),
            targets_xy=np.stack([e["targets_xy"] for e in episodes]),
            final_agents_xy=np.stack([e["final_agents_xy"] for e in episodes]),
            **{k: stack(k) for k in ("agents_xy", "obs", "patches", "scalars", "masks",
                                     "prev_actions", "logits", "tta_probs", "attn", "actions")},
        )
        print(f"-> {path}", flush=True)


if __name__ == "__main__":
    main()
