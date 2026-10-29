"""Windows 绿色包（M7）的纯逻辑部分：不联网、不交叉编译。真正的构建见 packaging/build_windows.py。"""
from __future__ import annotations

import importlib.util
import shutil
import socket
from pathlib import Path

import pytest

from classroom.server import __main__ as entry
from classroom.server import firewall
from classroom.server.app import lan_ips, usable_ip

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"


def _build_module():
    spec = importlib.util.spec_from_file_location("build_windows", PKG / "build_windows.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bw = _build_module()


def test_pth_puts_app_and_site_packages_on_path():
    lines = bw.pth_text().split("\r\n")
    assert lines[:4] == ["python312.zip", ".", r"Lib\site-packages", r"..\app"]
    assert "import site" in lines


def test_bat_files_are_ascii_crlf_without_bom():
    for name in ["start_classroom.bat", "allow_firewall.bat"]:
        data = bw.bat_bytes((PKG / name).read_text(encoding="utf-8"))
        assert not data.startswith(b"\xef\xbb\xbf")
        data.decode("ascii")
        assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    with pytest.raises(UnicodeEncodeError):
        bw.bat_bytes("echo 中文\n")


def test_start_bat_runs_server_from_app_dir():
    text = (PKG / "start_classroom.bat").read_text(encoding="utf-8")
    assert r'cd /d "%~dp0app"' in text
    assert r'"%~dp0python\python.exe" -m classroom.server --open-browser' in text
    assert "pause" in text


def test_readme_is_utf8_bom_crlf():
    data = bw.txt_bytes((PKG / "README.txt").read_text(encoding="utf-8"))
    assert data.startswith(b"\xef\xbb\xbf")
    assert "防火墙" in data[3:].decode("utf-8")
    assert b"\n" not in data.replace(b"\r\n", b"")


def test_copy_skips_caches_and_classroom_records(tmp_path):
    src = tmp_path / "src"
    (src / "pkg" / "__pycache__").mkdir(parents=True)
    (src / "pkg" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"")
    (src / "pkg" / "a.py").write_text("x = 1", encoding="utf-8")
    (src / "pkg" / "old.sqlite").write_bytes(b"")
    (src / "pkg" / "utils" / "data").mkdir(parents=True)          # 形如 torch/utils/data：必须保留
    (src / "pkg" / "utils" / "data" / "x.py").write_text("", encoding="utf-8")
    shutil.copytree(src, tmp_path / "dst", ignore=bw.copy_ignore)
    got = sorted(p.relative_to(tmp_path / "dst").as_posix() for p in (tmp_path / "dst").rglob("*") if p.is_file())
    assert got == ["pkg/a.py", "pkg/utils/data/x.py"]


def test_app_dirs_exclude_dev_and_private_material():
    assert set(bw.APP_DIRS) == {"classroom", "static", "weights"}
    for bad in ["data", "tests", "tools", "third_party", "fixtures", "docs"]:
        assert bad not in bw.APP_DIRS


def test_runtime_dlls_found_in_wheel_layouts(tmp_path):
    (tmp_path / "msvcp140.dll").write_bytes(b"")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "concrt140.dll").write_bytes(b"")
    (tmp_path / "bin" / "python3.dll").write_bytes(b"")
    names = sorted(p.name for p in bw.runtime_dlls(tmp_path))
    assert names == ["concrt140.dll", "msvcp140.dll"]


def test_requirements_win_matches_dev_pins():
    dev = {l.split("==")[0].lower(): l.split("==")[1] for l in (ROOT / "requirements.txt").read_text(encoding="utf-8").split("\n")
           if "==" in l and not l.startswith("#")}
    win = [l for l in (PKG / "requirements-win.txt").read_text(encoding="utf-8").split("\n") if "==" in l and not l.startswith("#")]
    assert win
    for l in win:
        name, ver = l.split("==")
        assert dev[name.lower()] == ver, name
    assert "pytest" not in {l.split("==")[0] for l in win}


def test_usable_ip_filters_loopback_and_link_local():
    assert usable_ip("192.168.1.23") and usable_ip("10.0.0.5")
    for bad in ["127.0.0.1", "169.254.3.4", "0.0.0.0", "::1", "fe80::1"]:
        assert not usable_ip(bad)
    assert all(usable_ip(ip) for ip in lan_ips())
    assert len(lan_ips()) == len(set(lan_ips()))


def test_port_in_use_detects_listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert entry.port_in_use("127.0.0.1", port)
    finally:
        s.close()
    assert not entry.port_in_use("127.0.0.1", port)


def test_non_ascii_path_warning():
    assert entry.non_ascii_path(Path("C:/Users/张三/Desktop/MAPF"))
    assert not entry.non_ascii_path(Path("D:/MAPF-Classroom/app"))


def test_firewall_rules_delete_then_add_program_and_port():
    cmds = firewall.rule_commands(r"D:\MAPF\python\python.exe", 8000, "private,domain")
    assert cmds[0][3] == "delete"
    assert any(r"program=D:\MAPF\python\python.exe" in c for c in cmds[1])
    assert "localport=8000" in cmds[2]
    for c in cmds:
        assert f"name={firewall.RULE}" in c
        assert all(x.isascii() for x in c[:4])
    assert all("profile=private,domain" in c for c in cmds[1:])


def test_prune_refuses_directories_with_python_code(tmp_path):
    """torch/testing 是正经模块，曾被当成测试目录删掉，Windows 上 import torch 直接失败。"""
    code = tmp_path / "testing"
    code.mkdir()
    (code / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        bw.prune_dir(code)
    assert code.exists()
    res = tmp_path / "include"
    (res / "c10").mkdir(parents=True)
    (res / "c10" / "a.h").write_text("", encoding="utf-8")
    bw.prune_dir(res)
    assert not res.exists()
    assert "testing" not in bw.TORCH_PRUNE_DIRS


def test_third_party_licenses_cover_python_wheels_and_native(tmp_path):
    py = tmp_path / "python"
    di = py / "Lib" / "site-packages" / "demo-1.0.dist-info"
    (di / "licenses").mkdir(parents=True)
    (py / "LICENSE.txt").write_text("PSF LICENSE AGREEMENT", encoding="utf-8")
    (di / "METADATA").write_text("Metadata-Version: 2.4\nName: demo\nVersion: 1.0\nLicense-Expression: MIT\n\nbody", encoding="utf-8")
    (di / "licenses" / "LICENSE").write_text("demo license text", encoding="utf-8")
    text = bw.third_party_licenses(py / "Lib" / "site-packages", py)
    for needle in ["PSF LICENSE AGREEMENT", "demo 1.0 — MIT", "demo license text", "LaCAM3", "Vue 3.5.13", "POGEMA"]:
        assert needle in text, needle
