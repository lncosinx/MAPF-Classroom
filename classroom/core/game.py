"""GroupGame：一个组的一局（pogema env + 模型 + 专家），M0 先提供同步、无头的回合机。

这里是 env 边界：pogema 给出的 padded 坐标只在本文件内换算成 unpadded，
对外（history、专家、视野、日志）一律 unpadded (row, col)；只有喂给 ModelPolicy 的仍是 padded。
每一步：begin_step()（算好模型建议 / 专家建议）-> 外部决定动作 -> apply(actions)。
服务层的异步回合机在 round.py，本类保持同步、无 asyncio 依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np
from pogema import GridConfig, pogema_v0

from ..ai.expert import ExpertAdvice, ExpertPolicy
from ..ai.model_policy import ModelPolicy, Proposal
from .coords import ACTION_DELTAS, OBS_RADIUS, WAIT, grid_to_str, parse_map, to_unpadded, unpad_grid
from .metrics import episode_metrics

CONTROLLERS = ("model", "expert", "idle", "external")


@dataclass
class StepContext:
    t: int
    positions: np.ndarray                  # (N,2) unpadded，本步决策时的位置
    legal: np.ndarray                      # (N,5) bool：只看边界和静态障碍，wait 恒合法
    model: Optional[Proposal] = None
    expert: Optional[ExpertAdvice] = None


@dataclass
class GameHistory:
    positions: list = field(default_factory=list)   # 每步决策时 (N,2) unpadded
    actions: list = field(default_factory=list)     # 每步实际执行的 (N,)
    at_goal: list = field(default_factory=list)     # 每步决策时 (N,) bool


class GroupGame:
    def __init__(self, map_str: str, *, num_agents: Optional[int] = None,
                 agents_xy: Optional[Sequence] = None, targets_xy: Optional[Sequence] = None,
                 seed: int = 0, max_steps: int = 128, map_name: Optional[str] = None,
                 controllers: Optional[Sequence[str]] = None, stop_on_success: bool = True,
                 model: Optional[ModelPolicy] = None, model_kwargs: Optional[dict] = None,
                 need_expert: Optional[bool] = None, expert_time_limit: float = 0.2):
        """起终点二选一：给 num_agents（由 pogema 按 seed 随机放置），或给 unpadded 的 agents_xy/targets_xy。
        后者开局前必须先过 maps.validate（pogema 会把压墙的点悄悄挪走）。"""
        grid = parse_map(map_str)
        H, W = grid.shape
        self.map_str = grid_to_str(grid)
        self.max_steps = max_steps
        self.stop_on_success = stop_on_success
        gc = dict(size=max(H, W), max_episode_steps=max_steps, obs_radius=OBS_RADIUS,
                  collision_system="soft", on_target="nothing", seed=seed)
        if map_name:
            gc["map_name"] = map_name
        if agents_xy is not None:
            gc.update(agents_xy=[list(map(int, a)) for a in agents_xy],
                      targets_xy=[list(map(int, t)) for t in targets_xy], num_agents=len(agents_xy))
        else:
            gc["num_agents"] = int(num_agents)
        # map 必须在构造时传入，构造后再赋值会跳过 validator
        self.grid_config = GridConfig(map=self.map_str, **gc)
        self.env = pogema_v0(grid_config=self.grid_config)
        self.obs, _ = self.env.reset(seed=seed)
        self.seed = seed

        self.grid_padded = np.asarray(self.env.grid.get_obstacles(), dtype=np.int32)
        self.grid = unpad_grid(self.grid_padded).astype(np.uint8)
        self._targets_padded = np.asarray(self.env.get_targets_xy(), dtype=np.int32)
        self.targets = to_unpadded(self._targets_padded)
        self.num_agents = N = len(self.targets)
        if agents_xy is not None:
            assert np.array_equal(self.positions, np.asarray(agents_xy)), "pogema 挪动了起点：开局前没过地图校验？"
            assert np.array_equal(self.targets, np.asarray(targets_xy)), "pogema 挪动了终点：开局前没过地图校验？"

        self.controllers = list(controllers) if controllers is not None else ["model"] * N
        if len(self.controllers) != N or not set(self.controllers) <= set(CONTROLLERS):
            raise ValueError(f"controllers 非法：{self.controllers}")

        self.model = model or ModelPolicy(N, seed=seed, **(model_kwargs or {}))
        if self.model.num_agents != N:
            raise ValueError("ModelPolicy 的智能体数与本局不一致")
        self.model.reset(self.grid_padded)
        if need_expert is None:
            need_expert = "expert" in self.controllers
        self.expert = ExpertPolicy(self.grid, self.targets, time_limit=expert_time_limit) if need_expert else None

        self.t = 0
        self.done = False
        self.history = GameHistory()
        self._ctx: Optional[StepContext] = None
        self.last_info: list = []   # pogema 最近一步的 info（只用于对拍，指标以 metrics() 为准）

    # ------------------------------------------------------------------
    @property
    def positions(self) -> np.ndarray:
        return to_unpadded(self.env.get_agents_xy())

    def at_goal(self) -> np.ndarray:
        return np.all(self.positions == self.targets, axis=1)

    def legal_actions(self, positions: Optional[np.ndarray] = None) -> np.ndarray:
        pos = self.positions if positions is None else np.asarray(positions)
        H, W = self.grid.shape
        nxt = pos[:, None, :] + ACTION_DELTAS[None]
        inside = (nxt[..., 0] >= 0) & (nxt[..., 0] < H) & (nxt[..., 1] >= 0) & (nxt[..., 1] < W)
        r = np.clip(nxt[..., 0], 0, H - 1); c = np.clip(nxt[..., 1], 0, W - 1)
        legal = inside & (self.grid[r, c] == 0)
        legal[:, WAIT] = True
        return legal

    def begin_step(self, with_expert: bool = True) -> StepContext:
        """回合开始：算好模型建议（with_expert 时连专家建议一起算），揭晓时不用再等。

        同一步重复调用返回同一个 StepContext，模型只前向一次。服务层用 with_expert=False，
        先推送视野，再在线程池里调 compute_expert()——LaCAM 会用满时限，不能挡在关键路径上。
        """
        if self.done:
            raise RuntimeError("本局已结束")
        if self._ctx is None:
            pos = self.positions
            prop = self.model.propose(self.obs, pos + OBS_RADIUS, self._targets_padded)
            self._ctx = StepContext(t=self.t, positions=pos, legal=self.legal_actions(pos), model=prop)
        if with_expert:
            self.compute_expert()
        return self._ctx

    def compute_expert(self) -> Optional[ExpertAdvice]:
        """给当前这一步补上专家建议（没有专家时返回 None）。只能在 begin_step 之后调用。"""
        ctx = self._ctx
        if ctx is None:
            raise RuntimeError("先调用 begin_step()")
        if ctx.expert is None and self.expert is not None:
            ctx.expert = self.expert.act(ctx.positions)
        return ctx.expert

    def default_actions(self, ctx: StepContext) -> np.ndarray:
        """按 controllers 给出补位动作；external 的位置先填 wait，由调用方覆盖。"""
        acts = np.full(self.num_agents, WAIT, dtype=np.int64)
        for i, c in enumerate(self.controllers):
            if c == "model":
                acts[i] = ctx.model.actions[i]
            elif c == "expert":
                acts[i] = ctx.expert.actions[i]
        return acts

    def apply(self, actions) -> bool:
        """执行一步。actions：(N,) 实际执行的动作。返回本局是否结束。"""
        ctx = self.begin_step(with_expert=False)
        acts = np.asarray(actions, dtype=np.int64)
        if acts.shape != (self.num_agents,) or acts.min() < 0 or acts.max() > 4:
            raise ValueError(f"非法动作：{acts}")
        self.history.positions.append(ctx.positions)
        self.history.actions.append(acts.copy())
        self.history.at_goal.append(np.all(ctx.positions == self.targets, axis=1))
        self.obs, _, _, _, self.last_info = self.env.step(acts.tolist())
        # 模型状态沿真实轨迹演化：prev_action = 实际执行的动作
        self.model.commit(acts)
        self._ctx = None
        self.t += 1
        if self.t >= self.max_steps or (self.stop_on_success and self.at_goal().all()):
            self.done = True
        return self.done

    def end(self):
        """老师手动结束本局。"""
        self.done = True
        self._ctx = None

    def run(self) -> dict:
        """无头跑完：所有 external 位置按 wait 处理。"""
        while not self.done:
            self.apply(self.default_actions(self.begin_step()))
        return self.metrics()

    def metrics(self) -> dict:
        hist = np.array(self.history.at_goal, dtype=bool).reshape(-1, self.num_agents)
        return episode_metrics(hist, self.at_goal(), self.max_steps)
