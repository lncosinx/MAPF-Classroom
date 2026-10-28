"""Classroom：一节课。管理玩家（昵称 + 座位号，不存真实姓名）、分组、座位，以及各组的这一局。

分组比拼（M4，PLAN §6.4）：
  - 老师设组数；各组用同一张地图、同样的起终点（同一 preset + 同一放置种子），各自独立推进；
  - 开局时把还没分组的在线同学按"人最少的组优先"均分，再在组内随机坐空位；老师可以再调整；
  - 每组一个 GroupOut：智能体编号按**该组**的座位表找到学生，发给老师的消息带上组号；
  - 排行榜和投屏页的推送做 200 ms 去抖，8 组同时结算时不刷屏；
  - 后台对同一实例跑"全 AI 队"和"专家队"作为参照行（core/reference.py）。
"""
from __future__ import annotations

import asyncio
import random
import secrets
from dataclasses import dataclass, field
from typing import Optional

from ..maps.library import list_maps, list_templates
from ..storage.db import Database
from .leaderboard import ranking
from .reference import run_reference
from .round import GroupRound, get_executor, get_expert_executor
from .settings import RoundSettings
from .views import teacher_view

MAX_NICKNAME = 12
SCREEN_DEBOUNCE = 0.2


@dataclass
class Player:
    id: int
    nickname: str
    seat: str
    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    online: bool = False

    def public(self) -> dict:
        return {"id": self.id, "nickname": self.nickname, "seat": self.seat, "online": self.online}


class GroupOut:
    """某一组的推送代理（实现 core.round.Broadcaster）。"""

    def __init__(self, room: "Classroom", g: int):
        self.room, self.g = room, g

    async def send_player(self, pid: int, msg: dict):
        await self.room.hub.send_player(pid, msg)

    async def kick(self, pid: int):
        await self.room.hub.kick(pid)

    async def send_agent(self, agent: int, msg: dict):
        # 智能体编号只在组内有意义：用本组的座位表找学生
        groups = self.room.groups
        pid = groups[self.g].player_of(agent) if self.g < len(groups) else None
        if pid is not None:
            await self.room.hub.send_player(pid, msg)

    async def send_teachers(self, msg: dict):
        await self.room.hub.send_teachers({**msg, "group": self.g})
        self.room.schedule_board()


class Classroom:
    def __init__(self, hub, db: Optional[Database] = None, settings: Optional[RoundSettings] = None):
        self.hub = hub
        self.db = db
        self.settings = settings or RoundSettings()
        self.players: dict[int, Player] = {}
        self.group_of: dict[int, int] = {}          # player_id -> 组号
        self._next_pid = 1
        self._db_groups: dict[int, int] = {}        # 组号 -> 数据库 grp.id
        self.groups: list[GroupRound] = [GroupRound(g, self.settings, GroupOut(self, g), db=db)
                                         for g in range(self.settings.num_groups)]
        self.references: dict[str, dict] = {}
        self._ref_version = 0
        self.screen_live = False                    # 比赛中投屏也显示全图（老师开关）
        self._board_task: Optional[asyncio.Task] = None

    async def open(self):
        if self.db is not None:
            await self.db.start_session()
            await self._ensure_db_groups()
        self._start_references()

    async def _ensure_db_groups(self):
        if self.db is None:
            return
        for r in self.groups:
            if r.group_id not in self._db_groups:
                self._db_groups[r.group_id] = await self.db.add_group(r.group_name)
            r.db_group_id = self._db_groups[r.group_id]

    @property
    def round(self) -> GroupRound:
        """第 1 组（单组时就是唯一的那一局；保留这个别名是为了单组场景好写）。"""
        return self.groups[0]

    def round_of(self, pid: int) -> Optional[GroupRound]:
        g = self.group_of.get(pid)
        return self.groups[g] if g is not None and g < len(self.groups) else None

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

    def _set_group(self, pid: int, g: Optional[int]):
        """把同学移到第 g 组（None = 不分组）。离开原组时让出座位。"""
        old = self.round_of(pid)
        if old is not None and (g is None or old.group_id != g):
            old.unseat(pid)
            old.online.discard(pid)
        if g is None:
            self.group_of.pop(pid, None)
            return
        self.group_of[pid] = g
        p = self.players.get(pid)
        if p is not None and p.online:
            self.groups[g].online.add(pid)

    def _emptiest_group(self, by_free_seats: bool = False) -> int:
        def load(r: GroupRound):
            if by_free_seats:
                return -len(r.free_agents())
            return sum(1 for g in self.group_of.values() if g == r.group_id)
        return min(self.groups, key=lambda r: (load(r), r.group_id)).group_id

    async def set_online(self, pid: int, online: bool):
        p = self.players.get(pid)
        if p is None:
            return
        p.online = online
        r = self.round_of(pid)
        # 比赛进行中有人新加入：放进空位最多的组，坐一个空位
        if online and r is None and self._busy():
            self._set_group(pid, self._emptiest_group(by_free_seats=True))
            r = self.round_of(pid)
        if r is not None:
            if online:
                r.online.add(pid)
                if r.status in ("playing", "paused") and r.agent_of(pid) is None and r.free_agents():
                    await self._seat(r, pid, random.choice(r.free_agents()))
            await r.player_online(pid, online)
        elif online:
            await self.hub.send_player(pid, self.lobby_state())
        await self.push_teacher()

    async def _seat(self, r: GroupRound, pid: int, agent: int):
        r.seat(agent, pid)
        if self.db is not None and r.round_id is not None:
            await self.db.add_seat(r.round_id, pid, agent)

    def lobby_state(self) -> dict:
        """还没分组的同学看到的状态。"""
        return {"type": "state", "status": self._overall_status(), "seated": False,
                "ui_mode": self.settings.ui_mode, "group_name": None}

    def _overall_status(self) -> str:
        sts = {r.status for r in self.groups}
        for s in ("playing", "paused", "lobby"):
            if s in sts:
                return s
        return "finished"

    def _busy(self) -> bool:
        return any(r.status in ("playing", "paused") for r in self.groups)

    # ------------------------------------------------------------------ 老师操作
    async def configure(self, settings: RoundSettings):
        """换规则 / 换地图 / 改组数：各组都新建一局。进行中的局不能改，要先结束。"""
        if self._busy():
            raise ValueError("比赛还没结束，先点「结束」")
        k = settings.num_groups

        def build():
            # 建模型实例 + 校验地图是一次性的 CPU 活，放到线程池里，不卡事件循环
            return [GroupRound(g, settings, GroupOut(self, g), db=self.db) for g in range(k)]
        rounds = await asyncio.get_running_loop().run_in_executor(get_executor(), build)
        old_seats = {r.group_id: dict(r.seats) for r in self.groups}
        self.groups, self.settings = rounds, settings
        # 保留原来的分组和座位（组号、智能体号都还在范围内时）
        for pid, g in list(self.group_of.items()):
            if g >= k:
                self.group_of.pop(pid)
        for r in rounds:
            r.online = {pid for pid, g in self.group_of.items() if g == r.group_id and self.players[pid].online}
            for agent, pid in old_seats.get(r.group_id, {}).items():
                if agent < r.num_agents and self.group_of.get(pid) == r.group_id:
                    r.seat(agent, pid)
        await self._ensure_db_groups()
        self._start_references()
        await self.push_teacher()
        await self.push_players()

    async def push_players(self):
        """给所有在线同学推送当前状态（坐下的推完整视野，没座位的只推大厅状态）。"""
        for p in self.players.values():
            if not p.online:
                continue
            r = self.round_of(p.id)
            if r is None:
                await self.hub.send_player(p.id, self.lobby_state())
                continue
            agent = r.agent_of(p.id)
            if agent is None:
                await self.hub.send_player(p.id, r.spectator_state())
            else:
                await self.hub.send_player(p.id, r.student_state(agent))

    async def assign(self, pid: int, agent: Optional[int], group: int = 0):
        if pid not in self.players:
            raise ValueError("没有这个同学")
        if not 0 <= group < len(self.groups):
            raise ValueError(f"没有第 {group + 1} 组")
        self._set_group(pid, group)
        r = self.groups[group]
        if agent is None:
            r.unseat(pid)
        else:
            other = r.player_of(agent)
            if other is not None and other != pid:
                r.unseat(other)
            await self._seat(r, pid, agent)
        if r.status in ("playing", "paused"):
            await r.player_online(pid, self.players[pid].online)
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
        for t in targets:
            p = self.players.pop(t)
            if p.online:
                await self.hub.kick(t)
            r = self.round_of(t)
            self._set_group(t, None)
            if r is not None:
                # 当作下线处理：比赛中若剩下的人都已提交会立即结算
                await r.player_online(t, False)
        await self.push_teacher()

    async def shuffle(self):
        """重新随机分组、随机分座位（人比座位多时，多出来的人在组里观战）。"""
        if any(r.status != "lobby" for r in self.groups):
            raise ValueError("开始之后不能重新随机分配")
        online = [p.id for p in self.players.values() if p.online]
        random.shuffle(online)
        for r in self.groups:
            r.seats.clear()
            r.online.clear()
        self.group_of.clear()
        for i, pid in enumerate(online):
            self._set_group(pid, i % len(self.groups))
        for r in self.groups:
            members = [pid for pid, g in self.group_of.items() if g == r.group_id]
            for pid, a in zip(members, random.sample(range(r.num_agents), k=min(len(members), r.num_agents))):
                r.seat(a, pid)
        await self.push_teacher()
        await self.push_players()

    async def start(self):
        if any(r.status != "lobby" for r in self.groups):
            raise ValueError("这一局已经开始过了，先「新开一局」")
        # 没分组的在线同学：人最少的组优先；组内没座位的随机坐空位
        for p in self.players.values():
            if p.online and p.id not in self.group_of:
                self._set_group(p.id, self._emptiest_group())
        for r in self.groups:
            for pid in sorted(r.online):
                if r.agent_of(pid) is None and r.free_agents():
                    r.seat(random.choice(r.free_agents()), pid)
        await asyncio.gather(*(r.start() for r in self.groups))
        await self.push_teacher()

    async def pause(self):
        await asyncio.gather(*(r.pause() for r in self.groups))

    async def resume(self):
        await asyncio.gather(*(r.resume() for r in self.groups))

    async def step_now(self, group: Optional[int] = None):
        if group is not None and not 0 <= group < len(self.groups):
            raise ValueError(f"没有第 {group + 1} 组")
        targets = self.groups if group is None else [self.groups[group]]
        await asyncio.gather(*(r.resolve(timed_out=True) for r in targets))

    async def end(self):
        await asyncio.gather(*(r.end() for r in self.groups))
        await self.push_teacher()

    async def set_screen_live(self, live: bool):
        self.screen_live = bool(live)
        await self.push_teacher()

    # ------------------------------------------------------------------ 参照行
    def _start_references(self):
        """后台对同一实例跑"全 AI 队"和"专家队"。设置变了就作废旧结果（版本号）。"""
        self._ref_version += 1
        ver, settings = self._ref_version, self.settings
        self.references = {"ai": {"status": "computing"}, "expert": {"status": "computing"}}

        async def work(kind: str):
            try:
                res = await asyncio.get_running_loop().run_in_executor(
                    get_expert_executor(), run_reference, settings, kind)
            except Exception as e:  # noqa: BLE001  参照行失败不影响上课
                res = {"status": "failed", "error": str(e)}
            if ver == self._ref_version:
                self.references[kind] = res
                self.schedule_board()
        loop = asyncio.get_running_loop()
        for kind in ("ai", "expert"):
            loop.create_task(work(kind))

    # ------------------------------------------------------------------ 排行榜 / 投屏
    def leaderboard(self) -> list[dict]:
        return ranking(self.groups, self.references, self.settings.rank_by)

    def screen_state(self) -> dict:
        """投屏页数据。地图只在该组结束、或老师打开"比赛中显示全图"时才下发（防止大屏泄露迷雾里的信息）。"""
        groups = []
        for r in self.groups:
            g = r.game
            entry = {"group": r.group_id, "name": r.group_name, "status": r.status, "t": int(g.t),
                     "at_goal": int(g.at_goal().sum()), "N": r.num_agents, "max_steps": r.settings.max_steps}
            if r.status == "finished" or self.screen_live:
                v = teacher_view(g, None)        # ctx=None：不含任何建议
                entry["map"] = {k: v[k] for k in ("H", "W", "map", "positions", "targets", "at_goal")}
            groups.append(entry)
        return {"type": "screen", "status": self._overall_status(), "ui_mode": self.settings.ui_mode,
                "rank_by": self.settings.rank_by, "live_maps": self.screen_live,
                "title": self.groups[0].preset.title, "leaderboard": self.leaderboard(), "groups": groups}

    def schedule_board(self):
        """排行榜和投屏页的刷新：200 ms 去抖。"""
        if self._board_task is not None and not self._board_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def later():
            await asyncio.sleep(SCREEN_DEBOUNCE)
            await self.hub.send_screens(self.screen_state())
            await self.hub.send_teachers({"type": "leaderboard", "rows": self.leaderboard(),
                                          "references": self.references})
        self._board_task = loop.create_task(later())

    def teacher_snapshot(self) -> dict:
        players = []
        for p in self.players.values():
            r = self.round_of(p.id)
            players.append({**p.public(), "group": self.group_of.get(p.id),
                            "agent": r.agent_of(p.id) if r is not None else None})
        return {"type": "classroom", "players": players, "maps": list_maps(), "templates": list_templates(),
                "defaults": RoundSettings().model_dump(), "groups": [r.teacher_state() for r in self.groups],
                # 第 1 组的状态，单组场景下用起来方便
                "round": self.groups[0].teacher_state(),
                "leaderboard": self.leaderboard(), "references": self.references, "screen_live": self.screen_live}

    async def push_teacher(self):
        await self.hub.send_teachers(self.teacher_snapshot())
        self.schedule_board()
