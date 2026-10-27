"""启动课堂服务：python -m classroom.server --host 0.0.0.0 --port 8000"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

import segno
import torch
import uvicorn

from ..ai.madcl import load_madcl
from .app import create_app, lan_ip


def main():
    ap = argparse.ArgumentParser(description="MAPF 课堂服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default=None, help="课堂记录目录，默认 <仓库>/data")
    args = ap.parse_args()
    # 输出被重定向（例如写日志文件）时也要立刻看到地址
    sys.stdout.reconfigure(line_buffering=True)

    # 限制 torch 线程数，给事件循环和 LaCAM 留 CPU（不变式 11）
    torch.set_num_threads(max(1, min(4, (os.cpu_count() or 2) // 2)))
    print("正在加载 AI 模型……", flush=True)
    load_madcl("big")   # 预热权重缓存，开局时不用再等

    key = secrets.token_urlsafe(6)
    ip = lan_ip()
    url = f"http://{ip}:{args.port}/"
    print("\n" + "=" * 60)
    print(f"  学生加入地址：{url}")
    print(f"  老师页面（本机）：http://localhost:{args.port}/teacher")
    print(f"  老师页面（其他设备）：http://{ip}:{args.port}/teacher?key={key}")
    print("  首次运行时 Windows 防火墙弹窗请勾选「专用网络」，否则学生连不上。")
    print("=" * 60)
    segno.make(url, error="m").terminal(compact=True)

    app = create_app(data_dir=args.data_dir, teacher_key=key, port=args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
