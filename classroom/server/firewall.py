"""Windows 防火墙一键放行（M7）：由绿色包里的「允许防火墙.bat」以管理员身份调用。

python -m classroom.server.firewall [--port 8000] [--yes]

给嵌入式 Python 的 python.exe 和课堂端口各加一条入站放行规则（先删同名旧规则，可以反复运行）。
默认只放行「专用 / 域」网络；当前网络是「公用」时会问老师要不要也放行公用网络
（Windows 默认把新连上的网络当作公用，学校 WiFi 经常是这样）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

RULE = "MAPF Classroom"        # 规则名用 ASCII：netsh 的参数在中文系统上按 GBK 解析
ROOT = Path(__file__).resolve().parents[2]


def rule_commands(python_exe: str, port: int, profiles: str) -> list[list[str]]:
    """要执行的 netsh 命令（先删旧规则，再加程序规则和端口规则）。单独拿出来便于测试。"""
    base = ["netsh", "advfirewall", "firewall"]
    return [
        base + ["delete", "rule", f"name={RULE}"],
        base + ["add", "rule", f"name={RULE}", "dir=in", "action=allow", f"program={python_exe}",
                "enable=yes", f"profile={profiles}"],
        base + ["add", "rule", f"name={RULE}", "dir=in", "action=allow", "protocol=TCP", f"localport={port}",
                "enable=yes", f"profile={profiles}"],
    ]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # netsh / PowerShell 在中文系统上按 OEM 代码页（GBK）输出
    return subprocess.run(cmd, capture_output=True, text=True, encoding="oem" if sys.platform == "win32" else "utf-8",
                          errors="replace")


def current_categories() -> list[str]:
    """当前联网的网络类型（Public / Private / DomainAuthenticated），拿不到就返回空列表。"""
    r = _run(["powershell", "-NoProfile", "-Command",
              "(Get-NetConnectionProfile | Select-Object -ExpandProperty NetworkCategory) -join ','"])
    return [x.strip() for x in r.stdout.strip().split(",") if x.strip()] if r.returncode == 0 else []


def main():
    ap = argparse.ArgumentParser(description="给 MAPF 课堂放行 Windows 防火墙")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--yes", action="store_true", help="当前是公用网络时不询问，直接也放行公用网络")
    args = ap.parse_args()
    if sys.platform != "win32":
        print("这个脚本只在 Windows 上用。")
        return 1
    python_exe = sys.executable
    cats = current_categories()
    profiles = "private,domain"
    if "Public" in cats:
        print("当前连接的网络是「公用网络」。Windows 在公用网络上默认拦截别的电脑访问本机。")
        print("如果这是学校 / 教室的网络，可以也放行公用网络（只在课堂程序运行时才有端口开着）。")
        ans = "y" if args.yes else input("也放行公用网络吗？输入 y 回车表示是，直接回车表示否：").strip().lower()
        if ans == "y":
            profiles = "private,domain,public"
    ok = True
    for i, cmd in enumerate(rule_commands(python_exe, args.port, profiles)):
        r = _run(cmd)
        if i > 0 and r.returncode != 0:     # 删除旧规则失败（本来就没有）不算错
            ok = False
            print("执行失败：", " ".join(cmd))
            print(r.stdout.strip(), r.stderr.strip())
    if ok:
        print(f"已放行：{python_exe}，以及 TCP {args.port} 端口（网络类型：{profiles}）。")
        print("现在可以关掉这个窗口，双击 start_classroom.bat 开始上课。")
        return 0
    print("没有放行成功。请确认是右键「以管理员身份运行」的，或者按使用说明在控制面板里手动放行。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
