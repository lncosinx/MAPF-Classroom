"""FastAPI 应用：页面、静态文件、学生 / 老师 WS、加入二维码。

老师端鉴权：来自本机（127.0.0.1 / ::1）的连接直接放行；其他设备（例如老师的平板）
要带启动时打印的 ?key=。学生端不鉴权，但学生只能收到服务端裁剪过的载荷（core/views.py）。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from urllib.parse import quote
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import segno
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..core.session import Classroom
from ..core.settings import RoundSettings
from ..storage.db import Database
from .hub import Hub
from . import teacher_rpc
from ..maps import library
from ..storage import export, review
from .protocol import (Assign, Choose, Configure, Join, Remove, Review, Rpc, Screen, SendMessage, StudentMsg,
                       TeacherMsg, ViewAnswer, ViewRequest)

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
log = logging.getLogger("classroom.app")


def static_version() -> str:
    """前端代码版本：static/ 下所有文件的路径、大小、修改时间的摘要。服务重启后变了，旧页面会自动刷新。"""
    h = hashlib.sha1()
    for p in sorted(STATIC.rglob("*")):
        if p.is_file():
            st = p.stat()
            h.update(f"{p.relative_to(STATIC).as_posix()}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:12]


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
        app.state.hub, app.state.room, app.state.db = hub, room, db
        yield
        if db is not None:
            await db.close()

    app = FastAPI(lifespan=lifespan, title="MAPF 课堂")
    app.state.teacher_key = teacher_key
    app.state.version = static_version()
    app.state.port = port
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def no_stale_static(request, call_next):
        # 页面和 JS 模块每次都向服务器校验（ETag 命中时只回 304），升级代码后学生刷新即可拿到新版本
        resp = await call_next(request)
        path = request.url.path
        if path.startswith("/static/") or path in ("/", "/teacher", "/screen"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    async def student_page():
        return FileResponse(STATIC / "student" / "index.html")

    @app.get("/teacher")
    async def teacher_page():
        return FileResponse(STATIC / "teacher" / "index.html")

    @app.get("/screen")
    async def screen_page():
        return FileResponse(STATIC / "screen" / "index.html")

    def join_url() -> str:
        return f"http://{lan_ip()}:{app.state.port}/"

    def teacher_ok(host: str, key: Optional[str]) -> bool:
        return host in LOCAL_HOSTS or bool(key and secrets.compare_digest(key, app.state.teacher_key))

    @app.get("/api/export/{session}/{target}")
    async def export_file(session: str, target: str, request: Request):
        """下载课堂记录：<局号|all>.<csv|jsonl|svg>。只有老师能下（本机或带 key）。"""
        if not teacher_ok(request.client.host if request.client else "", request.query_params.get("key")):
            return JSONResponse({"error": "只有老师能导出"}, status_code=403)
        stem, _, ext = target.rpartition(".")
        try:
            path = review.session_path(data_dir, session)
            rid = None if stem == "all" else int(stem)
            if ext == "csv":
                body, mime = await asyncio.to_thread(export.to_csv, path, rid), "text/csv; charset=utf-8"
            elif ext == "jsonl":
                body, mime = await asyncio.to_thread(export.to_jsonl, path, rid), "application/x-ndjson; charset=utf-8"
            elif ext == "svg" and rid is not None:
                body, mime = await asyncio.to_thread(export.to_svg, path, rid), "image/svg+xml"
            else:
                return JSONResponse({"error": "不支持的导出格式"}, status_code=400)
        except (ValueError, export.ReplayMismatch) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        name = f"MAPF课堂-{Path(session).stem}-{'全部' if rid is None else f'第{rid}局'}.{ext}"
        return Response(body, media_type=mime,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

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
            await ws.send_json({"type": "welcome", "token": player.token, "player": player.public(),
                                "version": app.state.version})
            await room.set_online(pid, True)
            rr = room.round_of(pid)
            if rr is None:
                await ws.send_json(room.lobby_state())
            elif rr.agent_of(pid) is None:
                await ws.send_json(rr.spectator_state())
            while True:
                try:
                    msg = StudentMsg.validate_python(await ws.receive_json())
                except ValidationError:
                    await ws.send_json({"type": "error", "message": "消息格式不对"})
                    continue
                r = room.round_of(pid)
                err = None
                if r is None and not isinstance(msg, Join):
                    err = "你还没有分到组，等老师开始"
                elif isinstance(msg, Choose):
                    err = await r.choose(pid, msg.t, msg.action)
                elif isinstance(msg, ViewRequest):
                    err = await r.comm_view_request(pid, msg.t, msg.to)
                elif isinstance(msg, ViewAnswer):
                    err = await r.comm_view_answer(pid, msg.t, msg.frm, msg.accept)
                elif isinstance(msg, SendMessage):
                    err = await r.comm_message(pid, msg.t, msg.to, msg.kind)
                elif isinstance(msg, Review):
                    err = await _student_review(app, r, pid, ws)
                if err:
                    await ws.send_json({"type": "error", "message": err})
        except (WebSocketDisconnect, ValidationError):
            pass
        finally:
            if pid is not None and hub.students.get(pid) is ws:
                del hub.students[pid]
                await room.set_online(pid, False)

    # ------------------------------------------------------------------ 投屏
    @app.websocket("/ws/screen")
    async def ws_screen(ws: WebSocket):
        # 不鉴权：投屏数据由服务端裁剪，比赛中（老师没打开"显示全图"时）不含任何地图信息
        await ws.accept()
        hub: Hub = app.state.hub
        room: Classroom = app.state.room
        hub.screens.add(ws)
        try:
            await ws.send_json({**room.screen_state(), "join_url": join_url(), "version": app.state.version})
            while True:
                await ws.receive_text()     # 投屏页只收不发；这里只为感知断开
        except WebSocketDisconnect:
            pass
        finally:
            hub.screens.discard(ws)

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
            await ws.send_json({**room.teacher_snapshot(), "join_url": join_url(), "version": app.state.version})
            while True:
                try:
                    msg = TeacherMsg.validate_python(await ws.receive_json())
                except ValidationError as e:
                    await ws.send_json({"type": "error", "message": f"设置不合法：{e.errors()[0]['msg']}"})
                    continue
                if isinstance(msg, Rpc):
                    try:
                        ctx = {"data_dir": data_dir, "current": app.state.db.path if app.state.db else None}
                        result = await teacher_rpc.dispatch(msg.method, msg.params, ctx)
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
    if isinstance(msg, Configure):
        await room.configure(msg.settings)
    elif isinstance(msg, Assign):
        await room.assign(msg.player_id, msg.agent, msg.group)
    elif isinstance(msg, Remove):
        await room.remove(msg.player_id)
    elif msg.type == "start":
        await room.start()
    elif isinstance(msg, Screen):
        await room.set_screen_live(msg.live_maps)
    elif msg.type == "pause":
        await room.pause()
    elif msg.type == "resume":
        await room.resume()
    elif msg.type == "step_now":
        await room.step_now(msg.group)
    elif msg.type == "end":
        await room.end()
    elif msg.type == "shuffle":
        await room.shuffle()
    elif msg.type == "refresh":
        await room.push_teacher()


REVIEW_ALLOWED = ("review", "immediate")


async def _student_review(app: FastAPI, r, pid: int, ws: WebSocket) -> Optional[str]:
    """学生复盘自己：本局已结束、揭晓模式允许、且这一局里有他的记录。"""
    if r.settings.reveal_mode not in REVIEW_ALLOWED:
        return "这节课的建议只给老师看，不能复盘"
    if r.status != "finished":
        return "这一局结束后才能复盘"
    db = app.state.db
    if db is None or r.round_id is None:
        return "这一局没有记录"
    try:
        data = await asyncio.to_thread(review.student_replay, db.path, r.round_id, pid)
    except ValueError as e:
        return str(e)
    await ws.send_json({"type": "review", **data})
    return None
