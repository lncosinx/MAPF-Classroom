"""一局的规则设置（老师端配置，服务端校验）。

注意：这里的 view_radius 只作用于学生的显示层；pogema 的 obs_radius 永远是 5（模型的训练口径），
不从这里读取（CLAUDE.md 不变式 3）。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..maps.mapio import MapSpec

# 预设消息：我要往 ↑↓←→ 走 / ✋ 请让一下 / ✅ 你先走 / 🏁 我到了
MESSAGE_KINDS = ("move_up", "move_down", "move_left", "move_right", "yield", "go_first", "arrived")
MessageKind = Literal["move_up", "move_down", "move_left", "move_right", "yield", "go_first", "arrived"]


class RoundSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 地图：预设名 + 人数 + 放置种子（教学图和老师保存的图自带起终点，此时 num_agents 忽略）
    preset: str = "four-rooms"
    # 地图工坊里画好但没保存的图：有值时优先于 preset，并随 settings_json 一起写进库里，便于复现
    custom_map: Optional[MapSpec] = None
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

    # 学生通信（与模型内部学到的通信互相独立，PLAN.md §6.3）
    comm_enabled: bool = True
    # L2 半径，默认与模型的 comm_radius 一致
    comm_radius: float = Field(default=4.0, ge=1.0, le=12.0)
    comm_view_exchange: bool = True
    # 交换视野是否不用对方确认（对方由 AI 控制时总是自动同意）
    comm_auto_accept: bool = False
    comm_messages: list[MessageKind] = Field(default_factory=lambda: list(MESSAGE_KINDS))
    # 每体每步的通信次数（交换视野请求和发消息各算一次），None = 不限
    comm_budget: Optional[int] = Field(default=None, ge=0, le=10)
    # 消息送达时机：now = 立即；next = 下一步开始时
    comm_delivery: Literal["now", "next"] = "now"

    ui_mode: Literal["simple", "advanced"] = "advanced"
    model_variant: Literal["big", "small"] = "big"
    tta: bool = False
    expert_time_limit: float = Field(default=0.2, gt=0.0, le=5.0)
