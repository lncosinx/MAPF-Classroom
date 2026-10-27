"""MADCL 推理代码（vendor 自 Chameleon 0fae917，见 VENDORED.md）+ 课堂侧的加载入口。

load_madcl() 每次返回一个**新的** net 实例：GRU 隐状态和通信迟滞状态都存在 module 属性里，
多个组共用一个实例会互相串状态（CLAUDE.md 不变式 5）。权重张量在进程内只读一次并在各实例间共享。
"""
from __future__ import annotations

import threading
from pathlib import Path

import torch
import yaml

from .action_mask import build_action_mask_batch
from .build import build_net
from .mapf_comm_net import MAPFCommNet
from .observation_builder import ObservationBuilder

WEIGHTS_DIR = Path(__file__).resolve().parents[3] / "weights"

# variant -> (权重文件, 网络配置, 需要覆盖的配置项)
VARIANTS = {
    # 主模型 7.45M
    "big": ("capbig_iter_20000.pt", "network_big.yaml", {}),
    # 备选 2.16M：配置里没有 intent_mode（默认 feature），不覆盖成 action 加载不上
    "small": ("MADCL_final.pt", "network_gru_c2g.yaml", {"intent_mode": "action"}),
}

_cache: dict[str, tuple[dict, dict]] = {}
_cache_lock = threading.Lock()


def load_config(variant: str = "big") -> dict:
    _, cfg_name, override = VARIANTS[variant]
    nc = yaml.safe_load((WEIGHTS_DIR / cfg_name).read_text(encoding="utf-8"))
    nc.update(override)
    return nc


def _load_state(variant: str) -> tuple[dict, dict]:
    with _cache_lock:
        if variant not in _cache:
            ckpt_name, _, _ = VARIANTS[variant]
            sd = torch.load(WEIGHTS_DIR / ckpt_name, map_location="cpu", weights_only=False)["net"]
            sd = {k.replace("_orig_mod.", ""): v for k, v in sd.items()}
            _cache[variant] = (sd, load_config(variant))
        return _cache[variant]


def load_madcl(variant: str = "big") -> tuple[MAPFCommNet, dict]:
    """返回 (eval 模式的新 net 实例, 网络配置)。参数张量与同 variant 的其他实例共享（只读推理）。"""
    sd, nc = _load_state(variant)
    net = build_net(nc)
    # assign=True：直接引用缓存里的张量，不再复制一份，8 组也只占一份权重内存
    net.load_state_dict(sd, strict=True, assign=True)
    net.requires_grad_(False)
    return net.eval(), nc


__all__ = ["load_madcl", "load_config", "build_net", "MAPFCommNet", "ObservationBuilder",
           "build_action_mask_batch", "VARIANTS", "WEIGHTS_DIR"]
