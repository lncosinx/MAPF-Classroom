"""一局的规则设置（老师端配置，服务端校验）。

注意：这里的 view_radius 只作用于学生的显示层；pogema 的 obs_radius 永远是 5（模型的训练口径），
不从这里读取（CLAUDE.md 不变式 3）。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class RoundSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 地图：预设名 + 人数 + 放置种子（教学图自带起终点，此时 num_agents 忽略）
    preset: str = "four-rooms"
    num_agents: Optional[int] = Field(default=None, ge=1, le=64)
    placement_seed: int = 0

    # 节奏
    max_steps: int = Field(default=128, ge=1, le=512)
    countdown_sec: float = Field(default=20.0, ge=3.0, le=300.0)

    # 视野：None = 全图可见；fog = 纯迷雾，memory = 走过的格子保留障碍（不显示他人）
    view_radius: Optional[int] = Field(default=2, ge=1, le=5)
    fog_mode: Literal["fog", "memory"] = "memory"
    # projection = 与 pogema 原生观测 ch2 一致：目标在视野外时只显示它在视野框边上的投影
    #              （行、列偏移各自截断到 [-r, r]）；visible = 目标始终可见（低年级 / 第 1 课用）
    goal_display: Literal["projection", "visible"] = "projection"

    # 专家 / AI 建议给谁看：immediate = 本步结算后揭晓；teacher = 只给老师；review = 赛后复盘
    reveal_mode: Literal["immediate", "teacher", "review"] = "teacher"
    # 空位与断线由谁补位
    fill_policy: Literal["model", "expert", "idle"] = "model"
    # 学生超时没提交：wait = 原地不动；ai = 按 fill_policy 代选
    timeout_policy: Literal["wait", "ai"] = "wait"

    ui_mode: Literal["simple", "advanced"] = "advanced"
    model_variant: Literal["big", "small"] = "big"
    tta: bool = False
    expert_time_limit: float = Field(default=0.2, gt=0.0, le=5.0)
