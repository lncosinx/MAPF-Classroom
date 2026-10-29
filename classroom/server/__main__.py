"""启动课堂服务：python -m classroom.server --host 0.0.0.0 --port 8000

Windows 绿色包里由 start_classroom.bat 调用（加 --open-browser）。这里的提示都是写给老师看的。
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def port_in_use(host: str, port: int) -> bool:
    """试绑一次端口。被占（例如已经开着一个课堂窗口）时返回 True。
    SO_REUSEADDR 只在非 Windows 上设：Linux 上不设的话，刚关掉的服务留下的 TIME_WAIT 连接会让重启被误判为"被占"
    （uvicorn 自己也设了它）；Windows 上它的语义是"允许抢占正在监听的端口"，反而测不出来。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def non_ascii_path(p: Path) -> bool:
    """路径里有中文等非 ASCII 字符。torch 在这种路径下加载 DLL 有已知问题。"""
    return not str(p).isascii()


def open_when_ready(port: int, url: str, timeout: float = 120.0) -> None:
    """后台线程：等服务能连上了再打开浏览器（模型加载要几秒，早开会看到"无法访问"）。"""
    def wait():
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            return
        webbrowser.open(url)
    threading.Thread(target=wait, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="MAPF 课堂服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default=None, help="课堂记录目录，默认 <仓库>/data")
    ap.add_argument("--ip", default=None, help="学生加入地址里的 IP（自动探测选错网卡时手动指定）")
    ap.add_argument("--open-browser", action="store_true", help="服务就绪后在本机浏览器打开老师页")
    args = ap.parse_args()
    # 输出被重定向（例如写日志文件）时也要立刻看到地址
    sys.stdout.reconfigure(line_buffering=True)

    if non_ascii_path(ROOT):
        print("!" * 60)
        print(f"  注意：程序所在的文件夹路径里有中文或特殊字符：{ROOT}")
        print("  AI 模型可能加载失败。建议把整个文件夹挪到纯英文路径，例如 D:\\MAPF-Classroom")
        print("!" * 60)
    if port_in_use(args.host, args.port):
        print(f"\n{args.port} 端口已被占用：可能已经开着一个课堂窗口（先关掉它），")
        print(f"或者换一个端口启动，例如：python -m classroom.server --port {args.port + 1}")
        sys.exit(2)

    # 重量级依赖放到检查之后再导入：端口被占时能立刻给出提示
    import segno
    import torch
    import uvicorn

    from ..ai.madcl import load_madcl
    from .app import create_app, lan_ips

    # 限制 torch 线程数，给事件循环和 LaCAM 留 CPU（不变式 11）
    torch.set_num_threads(max(1, min(4, (os.cpu_count() or 2) // 2)))
    print("正在加载 AI 模型……", flush=True)
    load_madcl("big")   # 预热权重缓存，开局时不用再等

    key = secrets.token_urlsafe(6)
    ips = lan_ips()
    ip = args.ip or (ips[0] if ips else "127.0.0.1")
    url = f"http://{ip}:{args.port}/"
    print("\n" + "=" * 60)
    print(f"  学生加入地址：{url}")
    others = [x for x in ips if x != ip]
    if others:
        print(f"  （本机还有这些地址：{'、'.join(others)}。学生连不上时，换成和学生同一个网的那个，")
        print(f"    用 --ip 指定后重新启动）")
    print(f"  老师页面（本机）：http://localhost:{args.port}/teacher")
    print(f"  老师页面（其他设备）：http://{ip}:{args.port}/teacher?key={key}")
    print("  首次运行时 Windows 防火墙弹窗请勾选「专用网络」，否则学生连不上。")
    print("  关闭这个窗口，课堂就结束了。")
    print("=" * 60)
    # compact 用半格方块字符；中文 Windows 的控制台把它们画成全角，二维码会变形。
    # Windows 上改用 segno 的背景色空格画法（老师页右上角也有二维码）
    segno.make(url, error="m").terminal(compact=sys.platform != "win32")

    app = create_app(data_dir=args.data_dir, teacher_key=key, port=args.port, join_host=args.ip)
    if args.open_browser:
        open_when_ready(args.port, f"http://localhost:{args.port}/teacher")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
