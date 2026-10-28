"""连接管理：玩家 id -> 学生 WS，老师 WS 列表，投屏页 WS 列表。
（智能体编号只在组内有意义，按组找学生由 core/session.py 的 GroupOut 负责。）

发送失败（对方刚断线）只记日志不抛出：断线由各自 WS 处理协程的 finally 负责上报。
"""
from __future__ import annotations

import logging

from fastapi import WebSocket

log = logging.getLogger("classroom.hub")


class Hub:
    def __init__(self):
        self.students: dict[int, WebSocket] = {}
        self.teachers: set[WebSocket] = set()
        self.screens: set[WebSocket] = set()

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

    async def send_teachers(self, msg: dict):
        for ws in list(self.teachers):
            await self._send(ws, msg)

    async def send_screens(self, msg: dict):
        for ws in list(self.screens):
            await self._send(ws, msg)
