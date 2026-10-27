"""ModelPolicy：一个组一个实例的有状态 MADCL 推理器（同步 API；异步包装在 core 层做）。

每一步严格按 propose() -> commit(实际执行的动作) 的顺序调用一次：
  - propose 会推进 ObservationBuilder 的足迹和 net 的 GRU/通信迟滞状态，**不能为同一步调两次**；
  - commit 记下的 prev_action 是实际执行的动作（学生的选择），不是模型的建议（不变式 7）。
所有位置参数都是 **padded** 坐标——本类属于模型适配层，是允许见到 padded 坐标的两处之一。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import torch

from .madcl import ObservationBuilder, build_action_mask_batch, load_madcl
from .madcl.d4_augment import _ELEMS, _PERMS, transform_dir, transform_patch


@dataclass
class Proposal:
    actions: np.ndarray          # (N,) int64，模型此刻会选的动作
    probs: np.ndarray            # (N,5) float32，实际采样所用的分布（已含 wait_bias 和温度）
    logits: np.ndarray           # (N,5) float32，关 TTA 的原始 logits，非法动作为 -inf，未减 wait_bias
    attn: np.ndarray             # (N,N) float32，最终生效的通信权重，非零即边（取自关 TTA 的一路）
    mask: np.ndarray             # (N,5) bool，合法动作
    tta_probs: Optional[np.ndarray] = None   # (N,5)，开 TTA 时 8 视角平均概率


class ModelPolicy:
    def __init__(self, num_agents: int, variant: str = "big", seed: int = 0,
                 base_temp: float = 1.0, wait_bias: float = 1.0, action_mode: str = "sample",
                 tta: bool = False):
        if action_mode not in ("sample", "argmax"):
            raise ValueError(action_mode)
        self.num_agents = num_agents
        self.variant = variant
        self.base_temp = base_temp
        self.wait_bias = wait_bias
        # argmax 只在测试里用：共享参数策略在对称局面下会互相锁死
        self.action_mode = action_mode
        self.tta = tta
        self.seed = seed
        self.net, self.nc = load_madcl(variant)
        # TTA 的 8 个视角各自维护 GRU/通信状态，用独立实例，不污染主实例的状态和通信图
        self.net_tta = load_madcl(variant)[0] if tta else None
        self.builder = ObservationBuilder(num_agents=num_agents, obs_radius=int(self.nc["obs_radius"]),
                                          use_cost2go=True, cost2go_clip=float(self.nc["cost2go_clip"]))
        self.gen = torch.Generator()
        self.grid: Optional[np.ndarray] = None
        self.prev = np.zeros(num_agents, dtype=np.int64)
        self.last_patches: Optional[np.ndarray] = None
        self.last_scalars: Optional[np.ndarray] = None
        self._pending = False

    def reset(self, grid_padded: np.ndarray, seed: Optional[int] = None):
        """每局开始调用：清空足迹、GRU、通信迟滞状态，重置采样随机数。"""
        self.grid = np.asarray(grid_padded, dtype=np.int32)
        self.builder.reset()
        self.net.reset_comm_state()
        self.net.communication.store_attn = True
        if self.net_tta is not None:
            self.net_tta.reset_comm_state()
        self.gen.manual_seed(self.seed if seed is None else seed)
        self.prev = np.zeros(self.num_agents, dtype=np.int64)
        self._pending = False

    @torch.no_grad()
    def propose(self, obs, agents_xy, targets_xy) -> Proposal:
        """obs：pogema 原生观测列表（每体 (3,11,11)）；agents_xy / targets_xy：padded (N,2)。"""
        if self.grid is None:
            raise RuntimeError("先调用 reset()")
        if self._pending:
            raise RuntimeError("上一步的 propose() 还没有 commit()")
        axy = np.asarray(agents_xy, dtype=np.int32)
        if axy.shape != (self.num_agents, 2):
            raise ValueError(f"一局内 N 不能变：期望 {self.num_agents} 体，得到 {axy.shape}")
        patches, scalars = self.builder.build(obs, axy.tolist(), [tuple(map(int, t)) for t in targets_xy],
                                              self.prev, obstacles=self.grid)
        mask = build_action_mask_batch(axy[None], self.grid)[0]
        # 留底本步的模型输入，供对拍测试和日志使用
        self.last_patches, self.last_scalars = patches, scalars
        pt = torch.from_numpy(patches)[None, None]
        st = torch.from_numpy(scalars)[None, None]
        po = torch.from_numpy(axy.astype(np.float32))[None, None]
        mt = torch.from_numpy(mask)[None, None]

        logits, _ = self.net(pt, st, po, action_masks=mt, reset_comm=False, stateful=True)
        lg_raw = logits[0, 0]
        attn = self.net.communication.last_attn[0, 0].clone()

        tta_probs = None
        if self.net_tta is not None:
            tta_probs = self._tta_probs(pt, st, po, mt)
            # 与 eval_bench 一致：回到 logit 尺度再做 wait_bias / 温度
            lg = torch.log(tta_probs.clamp_min(1e-12)).masked_fill(~torch.from_numpy(mask), float("-inf"))
        else:
            lg = lg_raw.clone()
        # 等待抑制：wait 的 logit 减偏置
        lg[:, 0] = lg[:, 0] - self.wait_bias
        probs = torch.softmax(lg / self.base_temp, dim=-1)
        if self.action_mode == "sample":
            acts = torch.multinomial(probs, 1, generator=self.gen)[:, 0]
        else:
            acts = lg.argmax(dim=-1)
        self._pending = True
        return Proposal(actions=acts.numpy().astype(np.int64), probs=probs.numpy(),
                        logits=lg_raw.numpy().copy(), attn=attn.numpy(), mask=mask,
                        tta_probs=None if tta_probs is None else tta_probs.numpy())

    def _tta_probs(self, pt, st, po, mt) -> torch.Tensor:
        """测试期 D4 增强，逐行照抄 eval_bench.rollout_chunk 的 TTA 分支（E=1）。返回 (N,5) 平均概率。"""
        pv, sv, mv = [], [], []
        for k, flip in _ELEMS:
            pv.append(transform_patch(pt, k, flip))
            s2 = st.clone()
            s2[..., 0:2] = transform_dir(st[..., 0:2], k, flip)
            perm = _PERMS[_ELEMS.index((k, flip))]
            s2[..., 2:7] = torch.zeros_like(st[..., 2:7]).index_copy(-1, perm, st[..., 2:7])
            sv.append(s2)
            mv.append(torch.zeros_like(mt).index_copy(-1, perm, mt))
        logits, _ = self.net_tta(torch.cat(pv, 0), torch.cat(sv, 0), po.repeat(8, 1, 1, 1),
                                 action_masks=torch.cat(mv, 0), reset_comm=False, stateful=True)
        probs_sum = 0.0
        for gi in range(len(_ELEMS)):
            pr = torch.softmax(logits[gi:gi + 1, 0], dim=-1)
            # 逆置换回原动作空间：p_orig[a] = p_trans[perm[a]]
            probs_sum = probs_sum + pr[..., _PERMS[gi]]
        return (probs_sum / 8.0)[0]

    def commit(self, executed_actions):
        """记下本步**实际执行**的动作，作为下一步观测里的 prev_action。"""
        if not self._pending:
            raise RuntimeError("commit() 之前必须先 propose()")
        a = np.asarray(executed_actions, dtype=np.int64)
        if a.shape != (self.num_agents,) or a.min() < 0 or a.max() > 4:
            raise ValueError(f"非法动作数组：{a}")
        self.prev = a.copy()
        self._pending = False
