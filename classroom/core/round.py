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
from ..maps.ai_hint import ai_hint
from ..maps.library import get_preset, preset_from_spec
from ..maps.validate import validate_instance
from ..storage.db import Database
from .comm import CommState
from .coords import WAIT
from .game import GroupGame, StepContext
from .settings import RoundSettings
from .views import ViewMemory, student_view, teacher_view

_executor: Optional[ThreadPoolExecutor] = None
_expert_executor: Optional[ThreadPoolExecutor] = None

# 全员都由 AI 控制时，每步之间停一下，老师才看得清
AUTO_STEP_DELAY = 0.4


def get_expert_executor() -> ThreadPoolExecutor:
    """专家（LaCAM）专用的单线程池。LaCAM 有全局锁、而且会用满时限，多组同时求解只能排队；
    如果和模型前向共用线程池，排队的 LaCAM 会占住线程，把各组的模型前向也堵住（M4 压测前的隐患）。"""
    global _expert_executor
    if _expert_executor is None:
        _expert_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="expert")
    return _expert_executor


def get_executor() -> ThreadPoolExecutor:
    """torch 前向和 LaCAM 共用的线程池。"""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 2), thread_name_prefix="compute")
    return _executor


def build_preset(settings: RoundSettings):
    """按设置得到这一局的地图和起终点（各组、参照行都用它，保证是同一实例）。不合法抛 ValueError（中文）。"""
    if settings.custom_map is not None:
        preset = preset_from_spec(settings.custom_map)
    else:
        try:
            preset = get_preset(settings.preset, num_agents=settings.num_agents, seed=settings.placement_seed)
        except KeyError as e:
            raise ValueError(str(e).strip("'\"")) from None
    if len(preset.agents_xy) == 0:
        raise ValueError("地图上还没有放起点和终点")
    errs = validate_instance(preset.grid, preset.agents_xy, preset.targets_xy)
    if errs:
        raise ValueError("地图校验未通过：" + "；".join(errs))
    return preset


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
        self.group_name = f"第 {group_id + 1} 组"
        self.settings = settings
        self.out = out
        self.db = db
        self.db_group_id = db_group_id if db_group_id is not None else group_id
        self.clock = clock
        self.status = "lobby"          # lobby / playing / paused / finished
        self.round_id: Optional[int] = None

        preset = build_preset(settings)
        self.preset = preset
        n = len(preset.agents_xy)
        # 小迷宫上 AI 表现差（M0 实测），开局前提示老师
        self.ai_hint = ai_hint(preset.grid, n)
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
        self.views: dict[int, dict] = {}       # 本步最后一次发给各学生的载荷（写库用：学生当时实际看到的）
        self.comm: Optional[CommState] = None  # 本步的学生通信状态（core/comm.py）
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

    def ai_agents(self) -> list[int]:
        """本步由 AI / 补位方控制的智能体（没人坐或学生断线）。"""
        students = set(self.student_agents())
        return [a for a in range(self.num_agents) if a not in students]

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
        # 学生通信每步重来；上一步"下一步送达"的消息带过来
        carry = self.comm.next_queue if self.comm is not None else None
        self.comm = CommState(int(self.ctx.t), self.ctx.positions, self.settings,
                              is_ai=lambda a: a not in self.student_agents(), carry=carry)
        self.step_start = self.clock()
        self.deadline = self.step_start + self.settings.countdown_sec
        self.remaining = None
        self._expert_fut = loop.run_in_executor(get_expert_executor(), self.game.compute_expert)
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

    # ------------------------------------------------------------------ 学生通信
    def _comm_ready(self, player_id: int, t: int) -> tuple[Optional[int], Optional[str]]:
        if self.status != "playing" or self.comm is None or self._resolving:
            return None, "现在不能通信" if self.status != "paused" else "暂停中不能通信"
        if t != self.game.t:
            return None, "这一步已经结束了"
        agent = self.agent_of(player_id)
        if agent is None:
            return None, "你还没有分配到智能体"
        return agent, None

    async def comm_view_request(self, player_id: int, t: int, to: int) -> Optional[str]:
        a, err = self._comm_ready(player_id, t)
        if err:
            return err
        _, err = self.comm.request_view(a, to)
        if err:
            return err
        await self._after_comm({a, to})
        return None

    async def comm_view_answer(self, player_id: int, t: int, frm: int, accept: bool) -> Optional[str]:
        b, err = self._comm_ready(player_id, t)
        if err:
            return err
        err = self.comm.answer(b, frm, accept)
        if err:
            return err
        await self._after_comm({b, frm})
        return None

    async def comm_message(self, player_id: int, t: int, to: int, kind: str) -> Optional[str]:
        a, err = self._comm_ready(player_id, t)
        if err:
            return err
        err = self.comm.send_message(a, to, kind)
        if err:
            return err
        # 下一步才送达时，接收方本步不用刷新
        await self._after_comm({a, to} if self.settings.comm_delivery == "now" else {a})
        return None

    async def _after_comm(self, agents: set[int]):
        """通信状态变了：写库、刷新相关学生的载荷、通知老师。"""
        events = self.comm.drain()
        if self.db is not None and self.round_id is not None and events:
            await self.db.write_comm(self.round_id, self.db_group_id, [e.public() for e in events])
        students = set(self.student_agents())
        for a in sorted(agents & students):
            await self.out.send_agent(a, self.student_state(a))
        for e in events:
            # 事件自己也有 type 字段，包一层，别把消息类型覆盖掉
            await self.out.send_teachers({"type": "comm_event", "event": e.public()})

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
                    view_json=_json(self.views.get(i)) if i in students else None,
                    shared_view_json=_json((self.views.get(i) or {}).get("shared")) if i in students else None,
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
               "progress": self.progress(), "max_steps": self.settings.max_steps,
               "group_name": self.group_name if self.settings.num_groups > 1 else None}
        if self.ctx is not None and self.status in ("playing", "paused"):
            # 通信会在一步之内改变载荷（共享视野、消息），所以每次推送都重新生成，并记下最后一版
            self.views[agent] = student_view(self.game, self.ctx, agent, self.settings, self.memory,
                                             comm=self.comm, ai_agents=frozenset(self.ai_agents()))
            msg["view"] = self.views[agent]
            ch = self.choices.get(agent)
            msg["my_choice"] = ch.action if ch else None
        if self.status == "finished":
            msg["metrics"] = self.metrics
            msg["at_goal"] = bool(self.game.at_goal()[agent])
            # 赛后复盘 / 立即揭晓模式下，学生可以复盘自己的每一步（"只给老师看"时不行，不变式 8）
            msg["can_review"] = self.settings.reveal_mode in ("review", "immediate") and self.game.t > 0
        return msg

    def teacher_state(self) -> dict:
        return {"type": "teacher_state", "group": self.group_id, "group_name": self.group_name, "status": self.status,
                "settings": self.settings.model_dump(), "preset_title": self.preset.title, "ai_hint": self.ai_hint,
                "num_agents": self.num_agents, "seats": {str(a): p for a, p in self.seats.items()},
                "online": sorted(self.online), "countdown_ms": self.countdown_ms(),
                "progress": self.progress(), "choices": {str(a): c.action for a, c in self.choices.items()},
                "view": teacher_view(self.game, self.ctx), "metrics": self.metrics,
                "comm_events": [e.public() for e in self.comm.events] if self.comm is not None and self.ctx else []}

    def spectator_state(self) -> dict:
        """在线但没坐下的同学（观战）：只知道这一局的状态，没有任何视野。"""
        msg = {"type": "state", "status": self.status, "seated": False, "ui_mode": self.settings.ui_mode,
               "group_name": self.group_name if self.settings.num_groups > 1 else None}
        if self.status == "finished":
            msg["metrics"] = self.metrics
        return msg

    async def push_all(self):
        for a in self.student_agents():
            await self.out.send_agent(a, self.student_state(a))
        # 观战的同学也要知道开始 / 暂停 / 结束
        seated = set(self.seats.values())
        for pid in sorted(self.online - seated):
            await self.out.send_player(pid, self.spectator_state())
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
