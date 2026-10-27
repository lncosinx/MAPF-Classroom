"""连接管理：玩家 id -> 学生 WS，老师 WS 列表。实现 core.round.Broadcaster。

发送失败（对方刚断线）只记日志不抛出：断线由各自 WS 处理协程的 finally 负责上报。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import WebSocket

log = logging.getLogger("classroom.hub")


class Hub:
    def __init__(self):
        self.students: dict[int, WebSocket] = {}
        self.teachers: set[WebSocket] = set()
        # agent -> player_id 的查询由 Classroom 注入（当前这一局的座位表）
        self.player_of: Callable[[int], Optional[int]] = lambda agent: None

    async def _send(self, ws: WebSocket, msg: dict):
        try:
            await ws.send_json(msg)
        except Exception as e:  # noqa: BLE001  对方已断开
            log.debug("发送失败：%s", e)

    async def send_player(self, pid: int, msg: dict):
        ws = self.students.get(pid)
        if ws is not None:
            await self._send(ws, msg)

    async def kick(self, pid: int):
        """老师移除了这个同学：通知后关闭连接（4001，前端不再重连并回到加入页）。"""
        ws = self.students.pop(pid, None)
        if ws is not None:
            await self._send(ws, {"type": "kicked", "message": "老师把你移出了课堂，可以重新加入"})
            try:
                await ws.close(code=4001)
            except Exception:  # noqa: BLE001
                pass

    async def send_agent(self, agent: int, msg: dict):
        pid = self.player_of(agent)
        if pid is not None:
            await self.send_player(pid, msg)

    async def send_teachers(self, msg: dict):
        for ws in list(self.teachers):
            await self._send(ws, msg)
