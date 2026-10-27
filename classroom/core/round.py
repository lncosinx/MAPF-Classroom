"""GroupRound：一个组的一局，异步回合机（包住同步的 GroupGame）。

每一步：
  1. 线程池里 begin_step()：整组一次前向，得到模型建议；
  2. 推送各学生视野、老师视角；
  3. 线程池里异步算专家建议（LaCAM 会用满时限，不挡关键路径）；
  4. 收集提交，锁定前可以改；
  5. 全员提交 / 倒计时到点 / 老师点"立即结算"时结算：
     没提交的按 timeout_policy 处理；没人坐、断线的位置按 fill_policy 补位；
  6. 线程池里 apply()，写库，（immediate 模式）揭晓，进入下一步。

torch 和 LaCAM 都不能直接在事件循环里跑（不变式 11），一律 run_in_executor。
建议的保密（不变式 8）：teacher / review 模式下学生永远收不到建议；immediate 模式也只在**结算之后**揭晓，
因为结算前提交还能改，提前揭晓等于让学生照抄。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, Protocol

import numpy as np

from ..ai.model_policy import ModelPolicy
from ..maps.library import get_preset
from ..maps.validate import validate_instance
from ..storage.db import Database
from .coords import WAIT
from .game import GroupGame, StepContext
from .settings import RoundSettings
from .views import ViewMemory, student_view, teacher_view

_executor: Optional[ThreadPoolExecutor] = None

# 全员都由 AI 控制时，每步之间停一下，老师才看得清
AUTO_STEP_DELAY = 0.4


def get_executor() -> ThreadPoolExecutor:
    """torch 前向和 LaCAM 共用的线程池。"""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 2), thread_name_prefix="compute")
    return _executor


class Broadcaster(Protocol):
    async def send_player(self, player_id: int, msg: dict) -> None: ...
    async def kick(self, player_id: int) -> None: ...
    async def send_agent(self, agent: int, msg: dict) -> None: ...
    async def send_teachers(self, msg: dict) -> None: ...


@dataclass
class Choice:
    action: int
    first_ts: float      # 第一次选择的时刻（单调时钟）
    last_ts: float       # 最后一次改动的时刻
    wall_ts: float       # 最后一次改动的墙钟时间（写库用）
    n_changes: int = 0   # 第一次之后又改了几次


class GroupRound:
    def __init__(self, group_id: int, settings: RoundSettings, out: Broadcaster,
                 db: Optional[Database] = None, db_group_id: Optional[int] = None, clock=time.monotonic):
        self.group_id = group_id
        self.settings = settings
        self.out = out
        self.db = db
        self.db_group_id = db_group_id if db_group_id is not None else group_id
        self.clock = clock
        self.status = "lobby"          # lobby / playing / paused / finished
        self.round_id: Optional[int] = None

        preset = get_preset(settings.preset, num_agents=settings.num_agents, seed=settings.placement_seed)
        errs = validate_instance(preset.grid, preset.agents_xy, preset.targets_xy)
        if errs:
            raise ValueError("地图校验未通过：" + "；".join(errs))
        self.preset = preset
        n = len(preset.agents_xy)
        self.num_agents = n
        # 模型补位和"模型此刻会怎么选"都来自同一次整组前向；专家总是算，用于记录和揭晓
        policy = ModelPolicy(n, variant=settings.model_variant, seed=settings.placement_seed, tta=settings.tta)
        self.game = GroupGame(preset.map_str, agents_xy=preset.agents_xy, targets_xy=preset.targets_xy,
                              max_steps=settings.max_steps, map_name=preset.name, controllers=["external"] * n,
                              stop_on_success=True, model=policy, need_expert=True,
                              expert_time_limit=settings.expert_time_limit)
        self.memory = ViewMemory(n, self.game.grid.shape)

        self.seats: dict[int, int] = {}        # agent -> player_id
        self.online: set[int] = set()          # 在线的 player_id
        self.ctx: Optional[StepContext] = None
        self.views: dict[int, dict] = {}       # 本步发给各学生的载荷（写库用：学生当时实际看到的）
        self.choices: dict[int, Choice] = {}   # agent -> 本步的选择
        self.step_start = 0.0
        self.deadline: Optional[float] = None
        self.remaining: Optional[float] = None  # 暂停时冻结的剩余秒数
        self.metrics: Optional[dict] = None
        self._timer: Optional[asyncio.Task] = None
        self._expert_fut: Optional[asyncio.Future] = None
        self._lock = asyncio.Lock()
        self._resolving = False

    # ------------------------------------------------------------------ 座位
    def player_of(self, agent: int) -> Optional[int]:
        return self.seats.get(agent)

    def agent_of(self, player_id: int) -> Optional[int]:
        return next((a for a, p in self.seats.items() if p == player_id), None)

    def student_agents(self) -> list[int]:
        """本步由在线学生控制的智能体。"""
        return sorted(a for a, p in self.seats.items() if p in self.online)

    def free_agents(self) -> list[int]:
        return [a for a in range(self.num_agents) if a not in self.seats]

    def seat(self, agent: int, player_id: int):
        if not 0 <= agent < self.num_agents:
            raise ValueError(f"没有 {agent} 号智能体")
        for a, p in list(self.seats.items()):
            if p == player_id:
                del self.seats[a]
        self.seats[agent] = player_id

    def unseat(self, player_id: int):
        for a, p in list(self.seats.items()):
            if p == player_id:
                del self.seats[a]

    # ------------------------------------------------------------------ 流程
    async def start(self):
        if self.status != "lobby":
            return
        if self.db is not None:
            self.round_id = await self.db.start_round(
                self.db_group_id, self.settings.model_dump(), self.game.map_str,
                self.preset.agents_xy.tolist(), self.preset.targets_xy.tolist())
            for a, p in self.seats.items():
                await self.db.add_seat(self.round_id, p, a)
        self.status = "playing"
        await self._begin_step()

    async def _begin_step(self):
        loop = asyncio.get_running_loop()
        ex = get_executor()
        self.ctx = await loop.run_in_executor(ex, lambda: self.game.begin_step(with_expert=False))
        self.choices = {}
        self.views = {}
        self.step_start = self.clock()
        self.deadline = self.step_start + self.settings.countdown_sec
        self.remaining = None
        self._expert_fut = loop.run_in_executor(ex, self.game.compute_expert)
        await self.push_all()
        self._arm_timer()

    def _arm_timer(self):
        self._cancel_timer()
        if self.status != "playing":
            return
        if not self.student_agents():
            delay = AUTO_STEP_DELAY      # 没有在线学生：自动推进
        else:
            delay = max(0.0, self.deadline - self.clock())
        self._timer = asyncio.get_running_loop().create_task(self._timer_fire(delay, self.game.t))

    def _cancel_timer(self):
        if self._timer is not None and not self._timer.done() and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None

    async def _timer_fire(self, delay: float, t: int):
        await asyncio.sleep(delay)
        if self.status == "playing" and self.game.t == t:
            await self.resolve(timed_out=True)

    async def choose(self, player_id: int, t: int, action: int) -> Optional[str]:
        """学生提交 / 修改本步动作。返回错误信息（中文）或 None。"""
        if self.status not in ("playing", "paused"):
            return "现在不能走"
        agent = self.agent_of(player_id)
        if agent is None:
            return "你还没有分配到智能体"
        if t != self.game.t or self.ctx is None or self._resolving:
            return "这一步已经结束了"
        if not 0 <= action <= 4 or not self.ctx.legal[agent, action]:
            return "那边走不通"
        now = self.clock()
        old = self.choices.get(agent)
        if old is None:
            self.choices[agent] = Choice(action, now, now, time.time())
        elif old.action != action:
            old.action, old.last_ts, old.wall_ts = action, now, time.time()
            old.n_changes += 1
        await self.push_progress()
        if self.status == "playing" and all(a in self.choices for a in self.student_agents()):
            await self.resolve(timed_out=False)
        return None

    async def resolve(self, timed_out: bool):
        """结算本步。timed_out=True 表示倒计时到点或老师强制结算。"""
        async with self._lock:
            if self.status != "playing" or self.ctx is None or self._resolving:
                return
            self._resolving = True
            try:
                await self._resolve_locked()
            finally:
                self._resolving = False

    async def _resolve_locked(self):
        self._cancel_timer()
        ctx = self.ctx
        expert = await self._expert_fut      # 补位 / 记录都要用到专家建议
        students = set(self.student_agents())
        s = self.settings
        acts = np.full(self.num_agents, WAIT, dtype=np.int64)
        controllers: list[str] = []
        timed: list[bool] = []

        def fill(i: int) -> tuple[int, str]:
            if s.fill_policy == "model":
                return int(ctx.model.actions[i]), "model"
            if s.fill_policy == "expert":
                return int(expert.actions[i]), "expert"
            return WAIT, "idle"

        for i in range(self.num_agents):
            if i in students:
                ch = self.choices.get(i)
                if ch is not None:
                    acts[i], c, to = ch.action, "student", False
                elif s.timeout_policy == "ai":
                    a, c = fill(i); acts[i] = a; to = True
                else:
                    acts[i], c, to = WAIT, "student", True
            else:
                a, c = fill(i); acts[i] = a; to = False
            controllers.append(c); timed.append(to)

        loop = asyncio.get_running_loop()
        done = await loop.run_in_executor(get_executor(), self.game.apply, acts)
        pos_after = self.game.positions
        at_goal = self.game.at_goal()

        if self.db is not None and self.round_id is not None:
            rows = []
            for i in range(self.num_agents):
                ch = self.choices.get(i) if i in students else None
                rows.append(dict(
                    round_id=self.round_id, group_id=self.db_group_id, t=int(ctx.t), agent=i,
                    controller=controllers[i], player_id=self.seats.get(i) if i in students else None,
                    view_json=_json(self.views.get(i)) if i in students else None, shared_view_json=None,
                    action=int(acts[i]),
                    submit_ts=ch.wall_ts if ch else None,
                    think_ms=round((ch.last_ts - self.step_start) * 1000.0, 1) if ch else None,
                    timed_out=int(timed[i]), n_changes=ch.n_changes if ch else 0,
                    expert_action=int(expert.actions[i]) if expert is not None else None,
                    expert_source=expert.source if expert is not None else None,
                    model_action=int(ctx.model.actions[i]),
                    model_probs_json=_json(np.round(ctx.model.probs[i], 4).tolist()),
                    legal_mask="".join("1" if x else "0" for x in ctx.legal[i]),
                    pos_before=_json(ctx.positions[i].tolist()), pos_after=_json(pos_after[i].tolist()),
                    at_goal_after=int(at_goal[i])))
            await self.db.write_step(self.round_id, int(ctx.t), ctx.positions.tolist(), rows)

        if s.reveal_mode == "immediate":
            for i in students:
                await self.out.send_agent(i, {
                    "type": "reveal", "t": int(ctx.t), "you": int(acts[i]),
                    "expert": int(expert.actions[i]) if expert is not None else None,
                    "ai": int(ctx.model.actions[i]), "timed_out": timed[i]})
        await self.out.send_teachers({"type": "resolved", "t": int(ctx.t), "actions": acts.tolist(),
                                      "controllers": controllers, "timed_out": timed})
        self.ctx = None
        if done:
            await self._finish()
        else:
            await self._begin_step()

    async def pause(self):
        if self.status != "playing":
            return
        self._cancel_timer()
        self.remaining = max(0.0, self.deadline - self.clock()) if self.deadline else None
        self.status = "paused"
        await self.push_all()

    async def resume(self):
        if self.status != "paused":
            return
        self.status = "playing"
        self.deadline = self.clock() + (self.remaining if self.remaining is not None else self.settings.countdown_sec)
        self.remaining = None
        await self.push_all()
        self._arm_timer()

    async def end(self):
        """老师手动结束。"""
        if self.status == "finished":
            return
        async with self._lock:
            if self.status == "finished":
                return
            self._cancel_timer()
            if self._expert_fut is not None:
                # 等后台的专家求解跑完，避免它和收尾同时碰 game
                await asyncio.gather(self._expert_fut, return_exceptions=True)
            self.game.end()
            await self._finish()

    async def _finish(self):
        self._cancel_timer()
        self.status = "finished"
        self.ctx = None
        self.metrics = self.game.metrics()
        if self.db is not None and self.round_id is not None:
            await self.db.end_round(self.round_id, self.metrics)
        await self.push_all()

    # ------------------------------------------------------------------ 推送
    def countdown_ms(self) -> Optional[int]:
        if self.status == "paused":
            return None if self.remaining is None else int(self.remaining * 1000)
        if self.status != "playing" or self.deadline is None:
            return None
        return max(0, int((self.deadline - self.clock()) * 1000))

    def progress(self) -> dict:
        students = self.student_agents()
        return {"submitted": sum(a in self.choices for a in students), "total": len(students)}

    def student_state(self, agent: int) -> dict:
        """发给学生的完整状态。view 只经由 views.student_view 生成。"""
        msg = {"type": "state", "status": self.status, "ui_mode": self.settings.ui_mode,
               "countdown_ms": self.countdown_ms(), "countdown_total_ms": int(self.settings.countdown_sec * 1000),
               "progress": self.progress(), "max_steps": self.settings.max_steps}
        if self.ctx is not None and self.status in ("playing", "paused"):
            if agent not in self.views:
                self.views[agent] = student_view(self.game, self.ctx, agent, self.settings, self.memory)
            msg["view"] = self.views[agent]
            ch = self.choices.get(agent)
            msg["my_choice"] = ch.action if ch else None
        if self.status == "finished":
            msg["metrics"] = self.metrics
            msg["at_goal"] = bool(self.game.at_goal()[agent])
        return msg

    def teacher_state(self) -> dict:
        return {"type": "teacher_state", "group": self.group_id, "status": self.status,
                "settings": self.settings.model_dump(), "preset_title": self.preset.title,
                "num_agents": self.num_agents, "seats": {str(a): p for a, p in self.seats.items()},
                "online": sorted(self.online), "countdown_ms": self.countdown_ms(),
                "progress": self.progress(), "choices": {str(a): c.action for a, c in self.choices.items()},
                "view": teacher_view(self.game, self.ctx), "metrics": self.metrics}

    async def push_all(self):
        for a in self.student_agents():
            await self.out.send_agent(a, self.student_state(a))
        await self.out.send_teachers(self.teacher_state())

    async def push_progress(self):
        prog = {"type": "progress", "t": int(self.game.t), **self.progress()}
        for a in self.student_agents():
            ch = self.choices.get(a)
            await self.out.send_agent(a, {**prog, "my_choice": ch.action if ch else None})
        await self.out.send_teachers({**prog, "choices": {str(a): c.action for a, c in self.choices.items()}})

    async def player_online(self, player_id: int, online: bool):
        """学生上线 / 断线。断线的位置由 fill_policy 接管，重连后交还。"""
        if online:
            self.online.add(player_id)
        else:
            self.online.discard(player_id)
        agent = self.agent_of(player_id)
        if agent is not None and online:
            await self.out.send_agent(agent, self.student_state(agent))
        await self.out.send_teachers(self.teacher_state())
        # 断线后剩下的人可能都已提交：立即结算；没人在线了：改为自动推进
        if self.status == "playing" and self.ctx is not None and not self._resolving:
            students = self.student_agents()
            if students and all(a in self.choices for a in students):
                await self.resolve(timed_out=False)
            else:
                self._arm_timer()


def _json(x) -> Optional[str]:
    return None if x is None else json.dumps(x, ensure_ascii=False, separators=(",", ":"))
