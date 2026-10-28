"""Classroom：一节课。管理玩家（昵称 + 座位号，不存真实姓名）、座位分配和当前这一局。

M1 只有一个组；结构上按"班级 → 组 → 局"组织，M4 扩成多组时只需要把 self.round 换成列表。
"""
from __future__ import annotations

import asyncio
import random
import secrets
from dataclasses import dataclass, field
from typing import Optional

from ..maps.library import list_maps, list_templates
from ..storage.db import Database
from .round import Broadcaster, GroupRound, get_executor
from .settings import RoundSettings

MAX_NICKNAME = 12


@dataclass
class Player:
    id: int
    nickname: str
    seat: str
    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    online: bool = False

    def public(self) -> dict:
        return {"id": self.id, "nickname": self.nickname, "seat": self.seat, "online": self.online}


class Classroom:
    def __init__(self, out: Broadcaster, db: Optional[Database] = None, settings: Optional[RoundSettings] = None):
        self.out = out
        self.db = db
        self.players: dict[int, Player] = {}
        self._next_pid = 1
        self.group_db_id: Optional[int] = None
        self.round = GroupRound(0, settings or RoundSettings(), out, db=db)

    async def open(self):
        if self.db is not None:
            await self.db.start_session()
            self.group_db_id = await self.db.add_group("第 1 组")
            self.round.db_group_id = self.group_db_id

    # ------------------------------------------------------------------ 玩家
    async def join(self, nickname: str, seat: str, token: Optional[str] = None) -> Player:
        """新加入，或凭 token 重连。"""
        if token:
            for p in self.players.values():
                if secrets.compare_digest(p.token, token):
                    return p
        nickname = (nickname or "").strip()[:MAX_NICKNAME] or "同学"
        seat = (seat or "").strip()[:8]
        pid = await self.db.add_player(nickname, seat) if self.db is not None else self._next_pid
        self._next_pid = max(self._next_pid, pid) + 1
        p = Player(pid, nickname, seat)
        self.players[pid] = p
        return p

    async def set_online(self, pid: int, online: bool):
        p = self.players.get(pid)
        if p is None:
            return
        p.online = online
        r = self.round
        # 比赛进行中有人新加入：自动坐到一个空位上
        if online and r.status in ("playing", "paused") and r.agent_of(pid) is None and r.free_agents():
            await self._seat(pid, random.choice(r.free_agents()))
        await r.player_online(pid, online)
        await self.push_teacher()

    async def _seat(self, pid: int, agent: int):
        self.round.seat(agent, pid)
        if self.db is not None and self.round.round_id is not None:
            await self.db.add_seat(self.round.round_id, pid, agent)

    # ------------------------------------------------------------------ 老师操作
    async def configure(self, settings: RoundSettings):
        """换规则 / 换地图：新建一局。进行中的局不能改，要先结束。"""
        if self.round.status in ("playing", "paused"):
            raise ValueError("本局还没结束，先点「结束」")
        old_seats = dict(self.round.seats)
        # 建模型实例 + 校验地图是一次性的 CPU 活，也放到线程池里，不卡事件循环
        r = await asyncio.get_running_loop().run_in_executor(
            get_executor(), lambda: GroupRound(0, settings, self.out, db=self.db, db_group_id=self.group_db_id))
        r.online = {p.id for p in self.players.values() if p.online}
        for agent, pid in old_seats.items():
            if agent < r.num_agents:
                r.seat(agent, pid)
        self.round = r
        await self.push_teacher()
        await self.push_players()

    async def push_players(self):
        """给所有在线同学推送当前状态（坐下的推完整视野，观战的只推大厅状态）。"""
        r = self.round
        for p in self.players.values():
            if not p.online:
                continue
            agent = r.agent_of(p.id)
            if agent is None:
                await self.out.send_player(p.id, r.spectator_state())
            else:
                await self.out.send_agent(agent, r.student_state(agent))

    async def assign(self, pid: int, agent: Optional[int]):
        r = self.round
        if pid not in self.players:
            raise ValueError("没有这个同学")
        if agent is None:
            r.unseat(pid)
        else:
            other = r.player_of(agent)
            if other is not None and other != pid:
                r.unseat(other)
            await self._seat(pid, agent)
        await self.push_teacher()
        await self.push_players()

    async def remove(self, pid: Optional[int] = None):
        """移除同学（pid 为空 = 移除所有离线的）。被移除者的重连 token 作废，座位交给补位方；
        数据库里的历史记录保留。"""
        if pid is None:
            targets = [p.id for p in self.players.values() if not p.online]
        else:
            if pid not in self.players:
                raise ValueError("没有这个同学")
            targets = [pid]
        r = self.round
        for t in targets:
            p = self.players.pop(t)
            if p.online:
                await self.out.kick(t)
            r.unseat(t)
            # 当作下线处理：比赛中若剩下的人都已提交会立即结算
            await r.player_online(t, False)
        await self.push_teacher()

    async def shuffle(self):
        """随机分座位：在线的同学随机坐到智能体上（人比智能体多时，多出来的人观战）。"""
        r = self.round
        if r.status != "lobby":
            raise ValueError("开始之后不能重新随机分配")
        r.seats.clear()
        online = [p.id for p in self.players.values() if p.online]
        random.shuffle(online)
        agents = random.sample(range(r.num_agents), k=min(len(online), r.num_agents))
        for pid, a in zip(online, agents):
            r.seat(a, pid)
        await self.push_teacher()
        await self.push_players()

    async def start(self):
        r = self.round
        if r.status != "lobby":
            raise ValueError("本局已经开始过了")
        # 还没座位的在线同学随机补上
        for p in self.players.values():
            if p.online and r.agent_of(p.id) is None and r.free_agents():
                r.seat(random.choice(r.free_agents()), p.id)
        await r.start()

    # ------------------------------------------------------------------ 视图
    def teacher_snapshot(self) -> dict:
        r = self.round
        return {"type": "classroom", "players": [p.public() for p in self.players.values()],
                "maps": list_maps(), "templates": list_templates(),
                "defaults": RoundSettings().model_dump(), "round": r.teacher_state()}

    async def push_teacher(self):
        await self.out.send_teachers(self.teacher_snapshot())
