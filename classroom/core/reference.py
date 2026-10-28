"""参照行：在和各组相同的实例上，无头跑"全 AI 队"和"专家队"，作为排行榜上的两行参照（PLAN §6.4）。

课堂口径：全员到达即结束、max_steps 截止，与各组一致。全 AI 队用 sample 解码、固定种子，只跑一局，
所以它是"AI 的一次表现"而不是期望值；界面上不需要解释，复盘时可以多跑几局（M5）。
"""
from __future__ import annotations

from ..ai.model_policy import ModelPolicy
from .game import GroupGame
from .round import build_preset
from .settings import RoundSettings

REF_SEED = 20261028


def run_reference(settings: RoundSettings, kind: str) -> dict:
    """kind：ai / expert。同步执行（调用方放到线程池里）。返回 {status, metrics, t, N, at_goal}。"""
    preset = build_preset(settings)
    n = len(preset.agents_xy)
    controller = "model" if kind == "ai" else "expert"
    policy = ModelPolicy(n, variant=settings.model_variant, seed=REF_SEED, tta=settings.tta)
    game = GroupGame(preset.map_str, agents_xy=preset.agents_xy, targets_xy=preset.targets_xy,
                     max_steps=settings.max_steps, controllers=[controller] * n, stop_on_success=True,
                     model=policy, need_expert=(kind == "expert"), expert_time_limit=settings.expert_time_limit)
    metrics = game.run()
    return {"status": "done", "metrics": metrics, "t": int(game.t), "N": n, "at_goal": int(game.at_goal().sum())}
