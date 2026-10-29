# M7 验收报告：Windows 绿色包（2026-10-29）

M7 的目标：在 Linux 上组装 Windows 绿色包，内容是嵌入式 Python 3.12、win_amd64 wheels、mingw-w64 交叉编译的 `lacam.dll`、`start_classroom.bat` 和防火墙说明（PLAN §7）。
**验收**：Windows 实机上双击启动，同一局域网的另一台电脑扫码加入，并玩完一局。**已通过**：用户在 Win10/11 笔记本上按 `docs/M7_WALKTHROUGH.md` 测试完毕。第一版包因精简过度启动失败，修复后通过，见 §5。

发布方式：代码（含 `packaging/`）进仓库；zip 作为 GitHub Release `v1.0.0` 的附件，因为它超过了仓库单文件 100 MB 的上限。

## 1. 模块

| 文件 | 职责 |
|---|---|
| `packaging/build_windows.py` | 构建脚本，依次：下载嵌入式 Python 并核对 sha256 → 用 uv 按 win_amd64 平台装依赖 → 精简 torch → 交叉编译 `lacam.dll` 并自检 → 拷贝程序 → 写 `._pth`、bat、说明和许可证汇总 → 打 zip |
| `packaging/mingw-w64.cmake` | 交叉编译工具链：posix 线程版 g++（LaCAM 用到 `std::thread`），全静态链接 |
| `packaging/requirements-win.txt` | 运行期依赖，版本与 `requirements.txt` 一致，另加 `msvc-runtime`（VC 运行库 DLL） |
| `packaging/start_classroom.bat` / `allow_firewall.bat` | 启动 / 防火墙放行（后者需要管理员权限时会自动提权）；只写 ASCII |
| `packaging/README.txt` | 给老师的使用说明（UTF-8 带 BOM + CRLF） |
| `classroom/server/firewall.py` | `netsh` 放行 python.exe 和课堂端口；当前是公用网络时会询问老师 |
| `classroom/server/__main__.py` | `--open-browser`（服务就绪后打开老师页）、`--ip`（手动指定加入地址）；端口被占时给中文提示；路径含中文时给出提醒；Windows 上终端二维码不用 compact 模式 |
| `classroom/server/app.py` | `lan_ips()` 列出所有候选网卡地址；`create_app(join_host=...)` |
| `native/lacam/lacam3/include/utils.hpp` | `_WIN32` 下补 `using uint = unsigned int;`（记入 `native/lacam/VENDORED.md`） |

## 2. 包的结构与体积

```
MAPF-Classroom/
  start_classroom.bat  allow_firewall.bat  README.txt  THIRD_PARTY_LICENSES.txt
  python/   嵌入式 Python 3.12.10 + Lib/site-packages + VC 运行库 DLL       463 MB
  app/      classroom/ static/ weights/（大小两个模型） native/bin/lacam.dll  40 MB
```

- zip 约 172 MB，解压后约 503 MB。
- torch 精简：完整安装 3.1 GB，删掉 `include/`、`share/` 和 `lib/*.lib`（静态导入库）后是 327 MB 左右。
- `lacam.dll` 约 1 MB，导入表只有 `KERNEL32.dll` 和 `msvcrt.dll`（构建时用 objdump 自检）。

## 3. 关键口径

- **文件名全用 ASCII**：资源管理器自带的解压不一定认 zip 里的 UTF-8 文件名标记。
- **bat 只写 ASCII**：cmd 按 OEM 代码页解析批处理，中文提示一律由 Python 打印。Python 写控制台走 Unicode 接口，不受代码页影响。
- **不依赖老师机的环境**：VC 运行库 DLL 放在 python.exe 旁边；`._pth` 把 `..\app` 加进 `sys.path`；课堂记录写在 `app\data`。
- **防火墙**：默认靠首次运行的弹窗（勾选「专用网络」）；`allow_firewall.bat` 用来补救。放行是按程序的，换端口不用重新放行。
- **嵌入式 Python 的校验值**：python.org 的发布页当时取不到校验值，`PY_SHA256` 是首次下载时记下的值，以后每次构建都和它比对。
- **第三方许可证**：各包 `dist-info` 里的原文都保留。`THIRD_PARTY_LICENSES.txt` 另外汇总了以下几项：
  - Python（PSF）、全部 wheel；
  - LaCAM（MIT）、mingw-w64 运行库（静态链接进 `lacam.dll`）、pogema 地图生成器、Vue（MIT）；
  - VC 运行库 DLL 按微软 Visual Studio 再分发条款，随应用分发。

## 4. 验收

- **自动化测试**：`pytest` 全部通过，其中新增 `tests/test_packaging.py` 14 项：
  - `._pth` 的内容；
  - bat 是 ASCII + CRLF + 无 BOM，说明是 UTF-8 BOM + CRLF；
  - 拷贝时跳过缓存和课堂记录，但保留 `torch/utils/data` 这类同名目录；
  - VC 运行库 DLL 的定位；
  - Windows 依赖版本与开发环境一致；
  - 网卡地址过滤、端口占用检测、中文路径检测；
  - netsh 规则；
  - 精简时拒绝删除含 `.py` 的目录；
  - 许可证汇总的覆盖面。
- **Linux 上的预检**：
  - 用 Linux 的 Python 运行包里的 `app/`，机器人打完一局，数据库记录完整；
  - 包里的 `.py`、`.pyd`、`.dll` 与完整安装逐一比对，只少了特意删掉的 `Scripts\` 下重复的 DLL；
  - 启动服务并打一局用到的 182 个标准库模块，嵌入式 Python 里都有（只有冻结在解释器里的两个 importlib 模块不在 zip 里，这是正常的）；
  - wine：系统自带的 6.0 和免安装的 10.0 都能跑嵌入式 Python 和 `lacam.dll`（求解小实例结果正确），但 `import numpy` 失败。wine 的 ucrtbase 缺 `fetestexcept` / `crealf`，所以 torch 和整个服务只能在实机上验证。
- **Windows 实机**：用户按 `docs/M7_WALKTHROUGH.md` 测完，通过。

## 5. 发现并修复的问题

| 现象 | 原因 | 修复 |
|---|---|---|
| mingw 编译 LaCAM 报 `'uint' does not name a type` | `uint` 是 glibc 在 `<sys/types.h>` 里给的别名 | `utils.hpp` 在 `_WIN32` 下补定义 |
| 交叉编译出来的文件名是 `liblacam.dll` | CMake 的 Windows 平台模块会重设库名前缀，工具链文件里改不掉 | 构建脚本 strip 后改名为 `lacam.dll` |
| 实机双击启动：`ModuleNotFoundError: No module named 'torch.testing'` | 精简 torch 时把 `testing` 当成测试目录删掉了，其实它是正经模块，`import torch` 时就要用 | 只删资源目录；`prune_dir` 遇到含 `.py` 的目录会拒绝删除；加了测试 |
| 拷贝时按目录名跳过 `data` 会删掉 `torch/utils/data` | 跳过规则写得太宽 | 课堂记录本来就不在拷贝范围内，只按后缀兜底，不再按目录名跳过 |
| 构建过程中系统盘写满 | uv 缓存和打包缓存各有一份解压后的 Windows torch（各 3 GB） | 删掉 uv 缓存里那份；打包只用 `packaging/cache` |

## 6. 已知限制与后续

- 包里带的是 7.45M 主模型和 2.16M 轻量模型。整包体积主要来自 torch；以后可以考虑导出 ONNX 换成 onnxruntime，但不在 M7 范围内（PLAN §9）。
- 程序放在中文路径下时只给警告，不阻止启动；torch 在这种路径下能不能用，取决于具体电脑。
- 没有代码签名，个别杀毒软件可能拦截，说明里写了加入信任的办法。
- 仓库本身还没有 LICENSE 文件，项目自己的代码和模型权重用什么许可，由作者决定。
