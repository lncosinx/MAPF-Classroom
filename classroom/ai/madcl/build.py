"""build_net：原样拷自 Chameleon 0fae917 的 scripts/train_il_meta.py（只拷这一个函数）。

train_il_meta.py 其余部分 import 了训练用模块（buffers/trainers），没有 vendor，故不能整文件拷。
"""
from __future__ import annotations

from .mapf_comm_net import MAPFCommNet


def build_net(nc: dict) -> MAPFCommNet:
    return MAPFCommNet(
        patch_channels=nc.get("patch_channels", 4), scalar_feat_dim=nc.get("scalar_feat_dim", 8),
        obs_radius=nc.get("obs_radius", 5), num_actions=nc.get("num_actions", 5),
        d_model=nc.get("d_model", 192), cnn_channels=nc.get("cnn_channels", 64),
        d_scalar=nc.get("d_scalar", 32), n_res_blocks=nc.get("n_res_blocks", 1),
        comm_rounds=nc.get("comm_rounds", 1), intent_dim=nc.get("intent_dim", 128),
        comm_dim=nc.get("comm_dim", 128), comm_radius=nc.get("comm_radius", 4),
        comm_topk_cap=nc.get("comm_topk_cap", 6), entmax_alpha=nc.get("entmax_alpha", 1.5),
        tau_in=nc.get("tau_in", 0.05),
        stickiness_bonus=nc.get("stickiness_bonus", 0.1), ema_decay=nc.get("ema_decay", 0.0),
        num_attention_heads=nc.get("num_attention_heads", 4),
        temporal_backend=nc.get("temporal_backend", "mamba"),
        temporal_checkpoint=nc.get("temporal_checkpoint", False),
        temporal_layers=nc.get("temporal_layers", 2), d_state=nc.get("d_state", 16),
        d_conv=nc.get("d_conv", 4), mamba_expand=nc.get("mamba_expand", 2),
        dt_init=nc.get("dt_init", "torch_default"),
        final_norm=nc.get("final_norm", False),
        head_hidden=nc.get("head_hidden", 128),
        spatial_pool=nc.get("spatial_pool", 0),   # 0=全展平（保留空间布局）
        comm_type=nc.get("comm_type", "madcl"),   # SQ3：只换通信模块的消融轴
        intent_mode=nc.get("intent_mode", "feature"),  # SQ3：消息内容（特征 vs 显式动作意图）
        hard_topk_select=nc.get("hard_topk_select", False),
    )
