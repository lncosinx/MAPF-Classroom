"""FastAPI 应用：页面、静态文件、学生 / 老师 WS、加入二维码。

老师端鉴权：来自本机（127.0.0.1 / ::1）的连接直接放行；其他设备（例如老师的平板）
要带启动时打印的 ?key=。学生端不鉴权，但学生只能收到服务端裁剪过的载荷（core/views.py）。
"""
from __future__ import annotations

import io
import logging
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import segno
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..core.session import Classroom
from ..core.settings import RoundSettings
from ..storage.db import Database
from .hub import Hub
from . import teacher_rpc
from ..maps import library
from .protocol import (Assign, Choose, Configure, Join, Remove, Rpc, SendMessage, StudentMsg, TeacherMsg,
                       ViewAnswer, ViewRequest)

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
log = logging.getLogger("classroom.app")


def lan_ip() -> str:
    """探测本机局域网 IP（UDP connect 不会真的发包）。探测不到就退回 127.0.0.1。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def create_app(data_dir: Optional[Path] = None, teacher_key: Optional[str] = None, port: int = 8000,
               settings: Optional[RoundSettings] = None, use_db: bool = True) -> FastAPI:
    teacher_key = teacher_key or secrets.token_urlsafe(6)
    data_dir = Path(data_dir) if data_dir else ROOT / "data"
    # 老师保存的地图和模板：跨课堂长期保存在 <data>/maps/
    library.set_user_dir(data_dir / "maps")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub = Hub()
        db = Database.for_new_session(data_dir) if use_db else None
        room = Classroom(hub, db=db, settings=settings)
        await room.open()
        hub.player_of = lambda agent: room.round.player_of(agent)
        app.state.hub, app.state.room, app.state.db = hub, room, db
        yield
        if db is not None:
            await db.close()

    app = FastAPI(lifespan=lifespan, title="MAPF 课堂")
    app.state.teacher_key = teacher_key
    app.state.port = port
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def no_stale_static(request, call_next):
        # 页面和 JS 模块每次都向服务器校验（ETag 命中时只回 304），升级代码后学生刷新即可拿到新版本
        resp = await call_next(request)
        path = request.url.path
        if path.startswith("/static/") or path in ("/", "/teacher"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    async def student_page():
        return FileResponse(STATIC / "student" / "index.html")

    @app.get("/teacher")
    async def teacher_page():
        return FileResponse(STATIC / "teacher" / "index.html")

    def join_url() -> str:
        return f"http://{lan_ip()}:{app.state.port}/"

    @app.get("/api/join-info")
    async def join_info():
        return JSONResponse({"url": join_url()})

    @app.get("/api/join-qr.svg")
    async def join_qr():
        # 要完整的 SVG 文档（带 xmlns）：svg_inline() 是给 HTML 内嵌用的片段，作为 <img> 的独立文件浏览器不认
        buf = io.BytesIO()
        segno.make(join_url(), error="m").save(buf, kind="svg", scale=6, border=2, xmldecl=False)
        return Response(buf.getvalue(), media_type="image/svg+xml")

    # ------------------------------------------------------------------ 学生
    @app.websocket("/ws/student")
    async def ws_student(ws: WebSocket):
        await ws.accept()
        hub: Hub = app.state.hub
        room: Classroom = app.state.room
        pid = None
        try:
            first = StudentMsg.validate_python(await ws.receive_json())
            if not isinstance(first, Join):
                await ws.send_json({"type": "error", "message": "请先加入"})
                return
            player = await room.join(first.nickname, first.seat, first.token)
            pid = player.id
            old = hub.students.get(pid)
            hub.students[pid] = ws
            if old is not None and old is not ws:
                # 同一个人开了第二个标签页：旧的那个下线
                try:
                    await old.close(code=4000)
                except Exception:  # noqa: BLE001
                    pass
            await ws.send_json({"type": "welcome", "token": player.token, "player": player.public()})
            await room.set_online(pid, True)
            if room.round.agent_of(pid) is None:
                await ws.send_json(room.round.spectator_state())
            while True:
                try:
                    msg = StudentMsg.validate_python(await ws.receive_json())
                except ValidationError:
                    await ws.send_json({"type": "error", "message": "消息格式不对"})
                    continue
                r = room.round
                err = None
                if isinstance(msg, Choose):
                    err = await r.choose(pid, msg.t, msg.action)
                elif isinstance(msg, ViewRequest):
                    err = await r.comm_view_request(pid, msg.t, msg.to)
                elif isinstance(msg, ViewAnswer):
                    err = await r.comm_view_answer(pid, msg.t, msg.frm, msg.accept)
                elif isinstance(msg, SendMessage):
                    err = await r.comm_message(pid, msg.t, msg.to, msg.kind)
                if err:
                    await ws.send_json({"type": "error", "message": err})
        except (WebSocketDisconnect, ValidationError):
            pass
        finally:
            if pid is not None and hub.students.get(pid) is ws:
                del hub.students[pid]
                await room.set_online(pid, False)

    # ------------------------------------------------------------------ 老师
    @app.websocket("/ws/teacher")
    async def ws_teacher(ws: WebSocket):
        host = ws.client.host if ws.client else ""
        key = ws.query_params.get("key")
        if host not in LOCAL_HOSTS and not (key and secrets.compare_digest(key, app.state.teacher_key)):
            await ws.close(code=4403)
            return
        await ws.accept()
        hub: Hub = app.state.hub
        room: Classroom = app.state.room
        hub.teachers.add(ws)
        try:
            await ws.send_json({**room.teacher_snapshot(), "join_url": join_url()})
            while True:
                try:
                    msg = TeacherMsg.validate_python(await ws.receive_json())
                except ValidationError as e:
                    await ws.send_json({"type": "error", "message": f"设置不合法：{e.errors()[0]['msg']}"})
                    continue
                if isinstance(msg, Rpc):
                    try:
                        result = await teacher_rpc.dispatch(msg.method, msg.params)
                        await ws.send_json({"type": "rpc_result", "id": msg.id, "result": result})
                        if msg.method in teacher_rpc.MUTATING:
                            await room.push_teacher()
                    except ValueError as e:
                        await ws.send_json({"type": "rpc_error", "id": msg.id, "message": str(e)})
                    continue
                try:
                    await _teacher_command(room, msg)
                except ValueError as e:
                    await ws.send_json({"type": "error", "message": str(e)})
        except WebSocketDisconnect:
            pass
        finally:
            hub.teachers.discard(ws)

    return app


async def _teacher_command(room: Classroom, msg):
    r = room.round
    if isinstance(msg, Configure):
        await room.configure(msg.settings)
    elif isinstance(msg, Assign):
        await room.assign(msg.player_id, msg.agent)
    elif isinstance(msg, Remove):
        await room.remove(msg.player_id)
    elif msg.type == "start":
        await room.start()
    elif msg.type == "pause":
        await r.pause()
    elif msg.type == "resume":
        await r.resume()
    elif msg.type == "step_now":
        await r.resolve(timed_out=True)
    elif msg.type == "end":
        await r.end()
    elif msg.type == "shuffle":
        await room.shuffle()
    elif msg.type == "refresh":
        await room.push_teacher()
