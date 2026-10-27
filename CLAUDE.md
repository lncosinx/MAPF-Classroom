# CLAUDE.md

这是 Claude Code 在本仓库工作时要读的**操作手册**。

## 文档分工

| 文档 | 管什么 |
|---|---|
| `PLAN.md` | **设计与里程碑**的事实来源 |
| 本文件 | 每次干活都要知道的**操作细节**：环境、命令、不变式、易错点 |
| `third_party/README.md` | 只读素材的**来源和用法**：Chameleon 快照、权重、LaCAM、生成器、地图、wheel |
| `fixtures/README.md` | 对拍基准的格式和用法 |

冲突时以设计文档为准，操作以本文件为准；**改设计决策时要几处一起更新**。

---

## 语言约定（最高优先级）

1. 回复、日志、解释**只用简体中文或英文**，**严禁韩文和日文**。发现输出里出现了，立刻重写。
2. 代码标识符（变量、类、函数）用英文；**注释用简体中文**，和 vendor 进来的 Chameleon 代码保持一致。

## 当前状态（2026-10-27）

- **M0 已完成**（验收结果见 `docs/M0_REPORT.md`）：
  - `classroom/ai/madcl`（vendor，见其 `VENDORED.md`）、`ModelPolicy`、`lacam.py` + `ExpertPolicy`（含简易专家回退）；
  - `native/lacam`（已删 `make_log`、改 `Threads::Threads`）；
  - 无头 `core/game.py`、`core/metrics.py`、`maps/{generate,validate,library}` + 教学预设图。
- **M1 代码已完成，待浏览器手测验收**：
  - 后端：`core/{settings,views,round,session}.py`、`storage/db.py`、`server/{app,hub,protocol,__main__}.py`；
  - 前端：`static/{shared,student,teacher}/`，以及 `static/vendor/vue.global.prod.js`（3.5.13）；
  - 测试：`tests/test_{views_no_leak,round,server}.py`；冒烟脚本 `tools/play_bots.py`。
- 下一步：浏览器里走一遍 M1 验收（老师页 + 3 个学生标签页玩完一局），然后做 M2（学生通信）。
- 开发机是 Linux 笔记本（Ryzen 7535H，12 核，无 AVX512），**最终部署在 Windows 老师机上**（见下文"跨平台约束"）。
- 仓库目前挂载在 `/root/Chameleon/MAPF-Classroom`（Chameleon 仓库目录下），只是临时位置；代码里所有路径都相对仓库根，不依赖 Chameleon。

---

## 开发环境（Linux 笔记本）

版本对齐服务器上生成 fixtures 的环境：python 3.12.3、torch 2.8.0、numpy 2.3.2、pydantic 2.13.4、gymnasium 1.3.0、pyyaml 6.0.2、pogema 2.0.0a0。

```bash
# 本机系统 Python 是 3.10，用 uv 装一个独立的 3.12（实装 3.12.16）
python3 -m pip install --user uv && export PATH=$HOME/.local/bin:$PATH
uv python install 3.12 && uv venv .venv -p 3.12
uv pip install -p .venv/bin/python torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu
uv pip install -p .venv/bin/python numpy==2.3.2 pydantic==2.13.4 gymnasium==1.3.0 pyyaml==6.0.2
uv pip install -p .venv/bin/python --no-deps third_party/wheels/pogema-2.0.0a0-py3-none-any.whl
uv pip install -p .venv/bin/python fastapi "uvicorn[standard]" segno pytest httpx websockets
#   实装版本已固定在 requirements.txt

# LaCAM：native/lacam 已改好，编译出 build/lacam/liblacam.so
cmake -S native/lacam -B build/lacam -DCMAKE_BUILD_TYPE=Release && cmake --build build/lacam -j

# 前端只用一个外部文件：Vue 3 global 生产构建，固定版本，下载一次后提交进仓库
curl -L -o static/vendor/vue.global.prod.js https://cdn.jsdelivr.net/npm/vue@3.5.13/dist/vue.global.prod.js
```

## 常用命令

```bash
# M0（已可用）
.venv/bin/python -m pytest -q                       # 单元测试 + golden 对拍（golden 约 2 分钟）
.venv/bin/python -m pytest -q tests/test_golden.py  # 只跑模型对拍
.venv/bin/python tools/m0_reference.py              # 验收 ②：dev 集 384 局 vs 参照，8 worker 约 6 分钟
.venv/bin/python tools/m0_presets_expert.py         # 验收 ③：全专家跑全部预设图
.venv/bin/python tools/m0_classroom_scale.py        # 验收 ④：教室规模（8×8/12×12，4–16 体）模型 vs 专家
#   tools/ 下的结果写 data/m0/（已 gitignore），加 --reuse 只重做统计

# M1
.venv/bin/python -m classroom.server --host 0.0.0.0 --port 8000
#   启动时打印：学生加入地址 + 终端二维码、老师页（本机直接开；其他设备要带 ?key=）、防火墙提示
#   老师端 http://localhost:8000/teacher；学生端 http://<本机局域网IP>:8000/
.venv/bin/python tools/play_bots.py --port 8000 --students 3   # 对运行中的服务：机器人打一局并检查数据库
#   课堂记录在 data/sessions/<日期时间>.sqlite
#   ⚠️ 重启服务前确认旧进程真的退出了：端口被占时新进程只打一行 "address already in use" 就退出，
#   浏览器连的仍是旧代码，看起来像"改了没生效"（2026-10-27 踩过）。不要用 pgrep -f 找进程（会匹配到自己的 shell），
#   用 `ps -eo pid,cmd | awk '$2 ~ /python/ && $0 ~ /-m classroom.server/'`，并检查启动日志里没有报错

# M4 起（尚未实现）
python tools/loadtest.py --students 40 --groups 8 --agents 16   # 压力测试，目标每轮 < 1 s
```

前端没有构建步骤，也就没有编译期检查。改了 `static/` 下的 JS 之后，可以用 VS Code server 自带的 node 做语法检查：
`~/.vscode-server/bin/*/node --check <文件>`（ES module 要先拷成 `.mjs`）。模板错误只能在浏览器里看到。

⚠️ `fixtures/check_selfcontained.py` 在**无 AVX512 的 CPU**（包括这台开发机）上会在 patch 断言处失败：
numpy 的 float32 `pow` 按指令集分派，足迹通道（ch3）有 27 个衰减值和服务器差 1 ULP。这是预期行为，
以 `tests/test_golden.py`（ch3 放宽到 1e-6，其余逐位相等）为准，详见 `classroom/ai/madcl/VENDORED.md`。

---

## 不变式（改坏了往往不报错，只是结果悄悄出错）

1. **动作顺序**：`0=wait(0,0)`、`1=up(-1,0)`、`2=down(+1,0)`、`3=left(0,-1)`、`4=right(0,+1)`。**wait 在第 0 位。** 前端按键映射、合法掩码、LaCAM 路径转动作、日志、导出全部以此为准。

2. **坐标系**。pogema 内部会在地图四周 pad 一圈，宽度等于 `obs_radius`（=5）：
   - `env.get_agents_xy()`、`env.get_targets_xy()`、`env.grid.get_obstacles()` 返回的都是 **padded** 坐标；
   - `GridConfig(agents_xy=..., targets_xy=...)` 接收的是 **unpadded** 坐标；
   - 两者差 5 格，搞错了每个智能体都会被平移 5 格，而且**不报错**。
   
   约定：**只在 `classroom/ai/madcl` 适配层和 `core/game.py` 的 env 边界上换算**，其余代码一律用 unpadded `(row, col)`。
   - 模型的 `positions` 输入要的是 **padded** 坐标；
   - LaCAM 的场景文件要的是**先列后行**。

3. **pogema 环境的构造**：
   - `GridConfig(map=<str>, ...)` 必须**在构造时传入** map。构造完再 `gc.map = str` 会跳过 validator，解析出错。
   - `a`/`A` 字符串最多 26 对，超过就用 `agents_xy` / `targets_xy` 列表。
   - **`obs_radius` 永远是 5**，因为模型是在这个半径下训练的。老师设的视野半径**只作用于学生的显示层**，不要传给 pogema。
   - 课堂默认：`on_target="nothing"`、`collision_system="soft"`，和训练、评测口径一致。
   - 纯 pogema 2.0 的 env **没有** `get_global_obstacles()`，全局障碍要用 `env.grid.get_obstacles()`（padded）。
   - pogema 会把压在障碍上的起点或终点**悄悄挪走**，所以开局前必须先过地图校验。
   - pogema 原生观测是每个智能体一个 `(3,11,11)`。

4. **模型只吃局部信息**。输入严格走 `ObservationBuilder`：
   - 5 通道：原生 3 通道 + 足迹 + cost2go；
   - 8 维标量：目标方向 2 + 上一步动作 one-hot 5 + 是否到达 1。
   
   不要把全局地图、他人路径或者 LaCAM 的结果喂进模型。cost2go 只取 FOV 内的裁剪，是合法的局部特征。

5. **模型状态**：
   - 每局开始调用 `net.reset_comm_state()` 和 `builder.reset()`；
   - **一局内 N 不能变**：GRU 会报错，而通信状态会**静默清零**；
   - **每个组一个 net 实例**：GRU 状态和迟滞状态都存在 module 的属性里，多组共用一个实例会互相串状态。权重可以共享同一份 CPU 张量。

6. **掩码与解码**：
   - 非法动作用 `-inf` 掩码（模型内部已经处理）。
   - 课堂默认解码：`sample`、`T=1.0`、`wait_bias=1.0`、**关 TTA**。TTA 是可选的"精细模式"，写法照抄 `third_party/chameleon_snap/scripts/eval_bench.py` 的 `rollout_chunk`。
   - **argmax 只在测试里用**：共享参数的策略在对称局面下用 argmax 会互相锁死。

7. **`prev_action` 用实际执行的动作**，也就是学生的选择，不是模型的建议。模型状态沿真实轨迹演化。

8. **建议的保密**：模型建议和专家建议在**回合开始时**算好。在"只给老师看"或"赛后复盘"模式下，**任何时候都不下发给学生**；在"立即揭晓"模式下，**提交之后**才下发。

9. **服务器是唯一权威**。学生的 WS 载荷只能包含：
   - 自己视野内的格子和智能体；
   - 别人共享过来的视野；
   - 自己的目标（或者指向它的方向箭头）。
   
   必须有测试守护这一条（`tests/test_views_no_leak.py`）。

10. **指标口径**和 `eval_bench.py` 的 `rollout_chunk` 结尾完全一致：
    - CSR 和 ISR 看最后一步；
    - `arrive_t` = 最后一次不在目标上的那一步 + 1，失败的记 `max_steps`；
    - SoC = Σ arrive_t，makespan = max arrive_t。
    
    不要用 `utils/metrics.py` 那套旧口径（首次到达）。

11. **不能阻塞事件循环**：
    - torch 前向和 LaCAM 都用 `run_in_executor` 放到线程池里跑；
    - 启动时 `torch.set_num_threads(min(4, os.cpu_count() // 2))`。

12. **LaCAM 不是线程安全的**：结果放在 `static std::string` 里，`Planner::*` 是全局静态参数。所以**所有 `run_lacam` 调用都要加一把全局锁**。ctypes 调用期间会释放 GIL，不加锁会真的并发。

13. **`third_party/` 和 `fixtures/` 是只读的。** 要修改就把文件拷到 `classroom/` 或 `native/` 下面再改，改动记进那里的 `VENDORED.md`。

14. **隐私**：
    - 不存真实姓名，只存昵称和座位号；
    - `data/` 和 `*.sqlite` 已经加进 gitignore，绝不提交；
    - 导出时提醒老师妥善保管。

---

## MADCL 适配要点（M0）

- **代码来源**：Chameleon `0fae917`，详见 `third_party/README.md`。
  - 拷进 `classroom/ai/madcl/` 时**只改 import 路径**，例如 `from models.x` 改成相对 import。
  - `build_net` 要从 `scripts/train_il_meta.py` 里**只拷这一个函数**，因为那个文件还 import 了训练用的模块，那些没有 vendor。
- **主模型**：`capbig_iter_20000.pt` + `network_big.yaml`。
- **备选**：`MADCL_final.pt` + `network_gru_c2g.yaml`，**必须覆盖 `intent_mode: action`**。
- **加载方式**：`torch.load(p, map_location="cpu", weights_only=False)["net"]`，去掉 key 里的 `_orig_mod.` 前缀，然后 `strict=True` 加载。
- **单步前向**（每组 B=1）：
  - 调用：`net(patches[None,None], scalars[None,None], positions[None,None].float(), action_masks=mask[None,None], reset_comm=False, stateful=True)`；
  - 返回 `logits (1,1,N,5)`。
- **AI 通信图**：设 `net.communication.store_attn = True`，然后读 `net.communication.last_attn[0, 0]`，形状 `(N,N)`，非零元素就是边。
  - 边一旦离开 `comm_radius=4` 就会断开，golden 数据里实测最长的边正好是 4.00。
  - 开 TTA 时没有唯一的通信图，要从关 TTA 的那一路取。
- **采样**：用每个组自己的 `torch.Generator`，不要用全局 RNG，这样复盘可以复现。

## LaCAM（M0，在 `native/lacam` 里改，不改 third_party）

1. 删掉 `main.cpp` 里的 `make_log(...)` 调用。否则每次求解都会在当前目录写一个 `lacam_log.txt`。
2. `CMakeLists.txt` 里的 `libpthread.so` 改成：
   ```cmake
   find_package(Threads REQUIRED)
   target_link_libraries(lacam lacam3 Threads::Threads)
   ```
   Windows 上必须这么改。
3. Python 侧按平台选库文件名：`liblacam.so`（Linux）/ `lacam.dll`（Windows）。
   - 地图和场景的格式见 `third_party/README.md`；
   - 返回的字符串里有 `ERROR` 就算失败，退回 prioritized 简易专家，并在 UI 上标明"简易专家"。
4. 每步从当前状态重新求解，只取第一步。如果所有智能体都按上一次的计划走了一步，就**复用**那份计划，避免建议前后跳变。
5. **LaCAM* 是 anytime 的，会把时限用满**（实测 ≥16 体时 `time_limit=1.0` 就真的耗 1 s）。加上全局锁，8 组串行就是 8 倍。
   所以课堂默认 `expert_time_limit=0.2`，并且专家建议要在**推送新视野之后**异步算，不能挡在"全员提交 → 推送"的关键路径上（M1 落实）。

## 跨平台约束（最终跑在 Windows 上）

- 路径一律用 `pathlib`。
- 文本读写一律显式写 `encoding="utf-8"`：Windows 的默认编码是 GBK，中文会乱码。
- 不依赖 `fork`、`uvloop`、`signal` 技巧或其他 Linux 专有 API。需要多进程时只用 spawn 也安全的写法。
- 动态库用 ctypes 按平台加载，不写死 `.so`。
- 服务要监听 `0.0.0.0`。启动脚本里要打印局域网地址和二维码，并提示老师"首次运行时 Windows 防火墙弹窗请勾选'专用网络'"。

## 前端约定

- **不要构建步骤**，不要 node，**不要引用任何外网 CDN**，教室可能断网。
  - 唯一的第三方前端文件是 `static/vendor/vue.global.prod.js`，固定版本。
  - 其余都是原生 ES modules 加手写 CSS 和 SVG。
- pogema 色板写成 CSS 变量（`static/shared/theme.css`），具体色值见 PLAN.md §6.7。
- 字体：`"Microsoft YaHei","PingFang SC",system-ui`。
- 两种模式（简洁 / 进阶）由老师在服务端设置并下发，学生端不能切换。
- 触控目标 ≥ 56px；学生端必须支持方向键和空格。
- 地图绘制统一走 `static/shared/grid-renderer.js`，老师端、学生端、投屏页共用，保证三处外观一致。

## 性能目标

- 每组每步的模型前向，在 7.45M 模型、关 TTA 的情况下：24 体约 30 ms，64 体约 60 ms（服务器 4 线程实测）。
- 一轮从全员提交到推送完成要 **< 1 s**，测试规模是 8 组 × 16 体，关 TTA。
- 老师机性能弱时切换到 2.16M 的备选权重。

---

## 易错点清单

- [ ] 动作 0 是不是 wait？
- [ ] 换算的是 padded 还是 unpadded 坐标？LaCAM 场景是不是先列后行？
- [ ] pogema 的 `obs_radius` 有没有被老师的视野设置改掉？它必须是 5。
- [ ] 学生载荷里有没有混进视野外的信息？防泄露测试还在跑吗？
- [ ] 学生载荷是不是只经由 `core/views.py` 的 `student_view` 生成？（新加字段要同步更新 `tests/test_views_no_leak.py` 的禁用键表）
- [ ] immediate 揭晓是不是在**结算之后**才发？结算前学生还能改选择，提前揭晓等于让他们照抄
- [ ] 老师 WS 是不是只放行本机或带正确 `?key=` 的连接？
- [ ] 多个组是不是共用了同一个 net 实例？
- [ ] 每局开始有没有调用 `reset_comm_state()` 和 `builder.reset()`？
- [ ] `prev_action` 用的是不是实际执行的动作？
- [ ] LaCAM 调用有没有加全局锁？`make_log` 删了没有？
- [ ] torch 或 LaCAM 有没有直接在事件循环里跑？
- [ ] 文本 IO 有没有写 `encoding="utf-8"`？
- [ ] 前端有没有偷偷引用外网资源？
- [ ] 指标口径是不是和 eval_bench 一致（看最后一步，makespan 右删失）？
- [ ] 改了模型适配代码之后，`tests/test_golden.py` 还能过吗？
