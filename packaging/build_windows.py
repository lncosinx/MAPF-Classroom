"""在 Linux 上组装 Windows 绿色包（M7）。

    .venv/bin/python packaging/build_windows.py            # 完整构建，产物 packaging/out/MAPF-Classroom-win64-<日期>.zip
    .venv/bin/python packaging/build_windows.py --no-zip   # 只组装目录 packaging/out/MAPF-Classroom（wine 预检用）

需要：uv（装 Windows wheels）、cmake + mingw-w64 的 posix 线程版编译器（交叉编译 lacam.dll）。
下载的东西缓存在 packaging/cache/（嵌入式 Python、装好的 site-packages），再次构建不重新下载；--refresh 强制重装 wheels。

包的结构：
    MAPF-Classroom/
      start_classroom.bat   allow_firewall.bat   README.txt
      python/               嵌入式 Python 3.12 + Lib/site-packages + VC 运行库 DLL
      app/                  classroom/  static/  weights/  native/bin/lacam.dll   （data/ 运行时自己建）
包里的文件名全部用 ASCII：Windows 资源管理器自带的解压不一定认 zip 里的 UTF-8 文件名标记。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"
CACHE = PKG / "cache"
OUT = PKG / "out"
NAME = "MAPF-Classroom"

PY_VERSION = "3.12.10"     # 3.12 系列最后一个带 Windows 安装包的版本
PY_EMBED = f"python-{PY_VERSION}-embed-amd64.zip"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/{PY_EMBED}"
# 2026-10-29 首次下载时记下的 sha256（python.org 的发布页当时取不到校验值）；以后每次构建都和它比对
PY_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
POGEMA_WHEEL = ROOT / "third_party" / "wheels" / "pogema-2.0.0a0-py3-none-any.whl"
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"

# 拷进 app/ 的目录；其余（tests、tools、third_party、fixtures、data、docs……）都不进包
APP_DIRS = ["classroom", "static", "weights"]
# 拷贝时跳过：缓存、课堂记录（不变式 14：含学生作答，绝不进包。课堂记录在仓库根的 data/，本来就不在 APP_DIRS 里，
# 这里再按后缀兜一道底）。⚠️ 不要按目录名跳过 data：site-packages 里有 torch/utils/data 这类正经代码
SKIP_NAMES = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".sqlite-journal"}
# torch 里运行用不到、体积又大的部分：C++ 头文件、静态导入库（.lib）、CMake 配置。
# ⚠️ 只删不含 Python 代码的目录：torch/testing 是正经模块（import torch 时 autograd.gradcheck 就会导入它），
# 曾被当成测试目录删掉，Windows 上一启动就 ModuleNotFoundError（2026-10-29 实机踩过）。prune_dir 会拒绝删含 .py 的目录
TORCH_PRUNE_DIRS = ["include", "share"]
TORCH_PRUNE_GLOBS = ["lib/*.lib"]
# site-packages 顶层：命令行启动器（在绿色包里用不了）和 msvc-runtime 重复放的一份 DLL
SITE_PRUNE_DIRS = ["Scripts", "bin"]


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB"


# ---------------------------------------------------------------- 纯逻辑部分（tests/test_packaging.py 测这些）
def skip_entry(name: str) -> bool:
    return name in SKIP_NAMES or any(name.endswith(s) for s in SKIP_SUFFIXES)


def copy_ignore(directory: str, names: list[str]) -> list[str]:
    """shutil.copytree 的 ignore 回调。"""
    return [n for n in names if skip_entry(n)]


def pth_text() -> str:
    """嵌入式 Python 的 ._pth：决定 sys.path。'..\\app' 让 python -m classroom.server 找得到代码；
    'import site' 让 site-packages 里的 .pth（例如 pywin32 那类）生效。路径相对 python.exe 所在目录。"""
    return "\r\n".join(["python312.zip", ".", r"Lib\site-packages", r"..\app", "import site", ""])


def bat_bytes(text: str) -> bytes:
    """.bat 必须是纯 ASCII + CRLF（cmd 按 OEM 代码页逐行解析；带 BOM 第一行会出错）。"""
    text.encode("ascii")    # 有非 ASCII 字符就直接报错
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")


def txt_bytes(text: str) -> bytes:
    """给老师看的说明：UTF-8 带 BOM + CRLF，老版本记事本也能正确识别中文。"""
    return b"\xef\xbb\xbf" + text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def prune_dir(d: Path) -> None:
    """删掉运行用不到的目录；里面有 .py 就拒绝（那是代码，不是资源）。"""
    if not d.is_dir():
        return
    py = next(d.rglob("*.py"), None)
    if py is not None:
        raise SystemExit(f"拒绝精简 {d}：里面有 Python 代码（例如 {py.name}），删了运行时会缺模块")
    shutil.rmtree(d)


# ---------------------------------------------------------------- 各个步骤
def fetch_python() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    dst = CACHE / PY_EMBED
    if not dst.exists():
        log(f"下载 {PY_URL}")
        tmp = dst.with_suffix(".part")
        urllib.request.urlretrieve(PY_URL, tmp)
        tmp.rename(dst)
    got = sha256(dst)
    if got != PY_SHA256:
        raise SystemExit(f"{dst.name} 的 sha256 对不上：{got}（期望 {PY_SHA256}）。删掉它重新下载，或者核实后更新 PY_SHA256")
    return dst


def install_wheels(refresh: bool) -> Path:
    """把 Windows 版依赖装进缓存目录（只装一次）。用 uv：它能按目标平台（win_amd64）解析依赖并直接解包。"""
    site = CACHE / "site-packages-win"
    stamp = site / ".complete"
    req = PKG / "requirements-win.txt"
    key = sha256(req) + sha256(POGEMA_WHEEL)
    if stamp.exists() and stamp.read_text(encoding="utf-8") == key and not refresh:
        log("Windows wheels 已在缓存里，跳过安装")
        return site
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv).exists() and not shutil.which(uv):
        raise SystemExit("找不到 uv。先装：python3 -m pip install --user uv")
    if site.exists():
        shutil.rmtree(site)
    common = ["--target", str(site), "--python-platform", "x86_64-pc-windows-msvc", "--python-version", "3.12",
              "--only-binary", ":all:"]
    log("安装 Windows 版依赖（torch 约 200 MB，首次要下载一会儿）")
    subprocess.run([uv, "pip", "install", *common, "-r", str(req), "--extra-index-url", TORCH_INDEX,
                    "--index-strategy", "unsafe-best-match"], check=True)
    # pogema 声明的依赖版本与上面冲突，必须 --no-deps（同开发环境）
    subprocess.run([uv, "pip", "install", *common, "--no-deps", str(POGEMA_WHEEL)], check=True)
    stamp.write_text(key, encoding="utf-8")
    return site


def runtime_dlls(site: Path) -> list[Path]:
    """msvc-runtime 包带的 VC 运行库 DLL（msvcp140.dll 等）。它们按 wheel 的 data 方案装在 target 根目录或 bin/ 里。"""
    found = []
    for p in list(site.glob("*.dll")) + list(site.glob("bin/*.dll")) + list(site.glob("msvc_runtime*.data/data/*.dll")):
        if p.name.lower().startswith(("msvcp", "vcruntime", "concrt", "vcomp", "vccorlib")):
            found.append(p)
    return found


def stage_python(py_zip: Path, site: Path, dst: Path) -> None:
    log("组装嵌入式 Python")
    with zipfile.ZipFile(py_zip) as z:
        z.extractall(dst)
    (dst / "python312._pth").write_bytes(pth_text().encode("utf-8"))
    sp = dst / "Lib" / "site-packages"
    shutil.copytree(site, sp, ignore=lambda d, n: [x for x in n if x in (".complete", "__pycache__")])
    dlls = runtime_dlls(sp)
    if not dlls:
        raise SystemExit("没找到 msvc-runtime 的 DLL（msvcp140.dll），torch 在没装 VC 运行库的电脑上会起不来")
    for p in dlls:
        target = dst / p.name
        if not target.exists():      # 嵌入式 Python 自带的 vcruntime140*.dll 不覆盖
            shutil.copy2(p, target)
    log(f"  VC 运行库：{', '.join(sorted(p.name for p in dlls))}")
    for d in SITE_PRUNE_DIRS:
        prune_dir(sp / d)
    torch_dir = sp / "torch"
    before = dir_size(torch_dir)
    for d in TORCH_PRUNE_DIRS:
        prune_dir(torch_dir / d)
    for g in TORCH_PRUNE_GLOBS:
        for p in torch_dir.glob(g):
            p.unlink()
    log(f"  精简 torch：{mb(before)} → {mb(dir_size(torch_dir))}")


def build_lacam(dst_bin: Path) -> None:
    log("交叉编译 lacam.dll")
    bdir = ROOT / "build" / "lacam-win"
    subprocess.run(["cmake", "-S", str(ROOT / "native" / "lacam"), "-B", str(bdir), "-DCMAKE_BUILD_TYPE=Release",
                    f"-DCMAKE_TOOLCHAIN_FILE={PKG / 'mingw-w64.cmake'}"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["cmake", "--build", str(bdir), "-j", str(os.cpu_count() or 4)], check=True, stdout=subprocess.DEVNULL)
    src = bdir / "liblacam.dll"
    dst_bin.mkdir(parents=True, exist_ok=True)
    dll = dst_bin / "lacam.dll"      # classroom/ai/lacam.py 在 Windows 上找的名字
    subprocess.run(["x86_64-w64-mingw32-strip", "--strip-unneeded", "-o", str(dll), str(src)], check=True)
    # 自检：导出了 run_lacam，而且只依赖 Windows 自带的系统库（静态链接了 libstdc++ / winpthread）
    dump = subprocess.run(["x86_64-w64-mingw32-objdump", "-p", str(dll)], check=True, capture_output=True,
                          text=True).stdout
    deps = sorted({ln.split(":", 1)[1].strip() for ln in dump.splitlines() if "DLL Name:" in ln})
    if "run_lacam" not in dump:
        raise SystemExit("lacam.dll 没有导出 run_lacam")
    allowed = {"KERNEL32.dll", "msvcrt.dll", "USER32.dll", "ADVAPI32.dll", "WS2_32.dll"}
    extra = [d for d in deps if d not in allowed]
    if extra:
        raise SystemExit(f"lacam.dll 依赖了非系统 DLL：{extra}（检查 mingw-w64.cmake 的静态链接选项）")
    log(f"  lacam.dll {mb(dll.stat().st_size)}，依赖 {deps}")


def stage_app(dst: Path) -> None:
    log("拷贝课堂程序")
    for d in APP_DIRS:
        shutil.copytree(ROOT / d, dst / d, ignore=copy_ignore)
    build_lacam(dst / "native" / "bin")


VUE_MIT = """MIT License

Copyright (c) 2018-present, Yuxi (Evan) You and Vue contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
MINGW_COPYRIGHT = Path("/usr/share/doc/mingw-w64-common/copyright")


def _section(title: str, body: str) -> str:
    return f"{'=' * 78}\n{title}\n{'=' * 78}\n\n{body.strip()}\n\n"


def _license_files(dist_info: Path) -> list[Path]:
    keys = ("licen", "copying", "notice", "authors")
    return sorted(p for p in dist_info.rglob("*") if p.is_file() and p.suffix != ".py"
                  and any(k in p.name.lower() for k in keys))


def _meta(dist_info: Path, field: str) -> str:
    for line in (dist_info / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(field + ":"):
            return line.split(":", 1)[1].strip()
        if not line.strip():
            break
    return ""


def third_party_licenses(site: Path, py_dir: Path) -> str:
    """把包里所有第三方软件的许可证汇总成一个文件（各包 dist-info 里的原文也都还在）。"""
    out = ["MAPF 课堂 Windows 绿色包 · 第三方软件许可证\n"
           "Third-party software bundled in this package and their licenses.\n\n"]
    out.append(_section(f"Python {PY_VERSION} (embeddable package) — PSF License",
                        (py_dir / "LICENSE.txt").read_text(encoding="utf-8", errors="replace")))
    for dist_info in sorted(site.glob("*.dist-info"), key=lambda p: p.name.lower()):
        name, ver = _meta(dist_info, "Name"), _meta(dist_info, "Version")
        lic = _meta(dist_info, "License-Expression") or _meta(dist_info, "License") or "见下方原文 / see text below"
        texts = "\n\n".join(f"--- {f.relative_to(dist_info).as_posix()} ---\n"
                              + f.read_text(encoding="utf-8", errors="replace") for f in _license_files(dist_info))
        out.append(_section(f"{name} {ver} — {lic}", texts or "(no license file shipped)"))
    out.append(_section("LaCAM3 (native/lacam, compiled into app/native/bin/lacam.dll) — MIT",
                        (ROOT / "native" / "lacam" / "LICENSE").read_text(encoding="utf-8")))
    if MINGW_COPYRIGHT.exists():
        out.append(_section(
            "mingw-w64 runtime (winpthreads etc., statically linked into lacam.dll)",
            "libstdc++ / libgcc are covered by the GCC Runtime Library Exception (no notice required);\n"
            "the mingw-w64 CRT and winpthreads notices follow.\n\n" + MINGW_COPYRIGHT.read_text(encoding="utf-8")))
    out.append(_section("POGEMA map generators (app/classroom/maps/generators) — see license",
                        (ROOT / "classroom" / "maps" / "generators" / "LICENSE").read_text(encoding="utf-8")))
    out.append(_section("Vue 3.5.13 (app/static/vendor/vue.global.prod.js) — MIT", VUE_MIT))
    return "".join(out)


def stage_top(dst: Path) -> None:
    (dst / "start_classroom.bat").write_bytes(bat_bytes((PKG / "start_classroom.bat").read_text(encoding="utf-8")))
    (dst / "allow_firewall.bat").write_bytes(bat_bytes((PKG / "allow_firewall.bat").read_text(encoding="utf-8")))
    (dst / "README.txt").write_bytes(txt_bytes((PKG / "README.txt").read_text(encoding="utf-8")))
    lic = third_party_licenses(dst / "python" / "Lib" / "site-packages", dst / "python")
    (dst / "THIRD_PARTY_LICENSES.txt").write_bytes(txt_bytes(lic))
    log(f"  第三方许可证汇总：{len(lic) // 1000} KB")


def make_zip(stage: Path) -> Path:
    z = OUT / f"{NAME}-win64-{_dt.date.today():%Y%m%d}.zip"
    log(f"打包 {z.name}（压缩要几分钟）")
    if z.exists():
        z.unlink()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(stage.rglob("*")):
            arc = p.relative_to(stage.parent).as_posix()
            if not arc.isascii():
                raise SystemExit(f"包里有非 ASCII 文件名：{arc}")
            if p.is_file():
                zf.write(p, arc)
    return z


def main() -> int:
    ap = argparse.ArgumentParser(description="组装 Windows 绿色包")
    ap.add_argument("--refresh", action="store_true", help="重新安装 Windows wheels（改了 requirements-win.txt 时会自动重装）")
    ap.add_argument("--no-zip", action="store_true", help="只组装目录，不打 zip")
    args = ap.parse_args()

    py_zip = fetch_python()
    site = install_wheels(args.refresh)
    stage = OUT / NAME
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    stage_python(py_zip, site, stage / "python")
    stage_app(stage / "app")
    stage_top(stage)
    log(f"目录组装完成：{stage}")
    for part in ["python", "app"]:
        log(f"  {part}/ {mb(dir_size(stage / part))}")
    log(f"  合计 {mb(dir_size(stage))}")
    if not args.no_zip:
        z = make_zip(stage)
        log(f"完成：{z}（{mb(z.stat().st_size)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
