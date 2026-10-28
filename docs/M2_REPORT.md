# M2 验收报告：学生通信（2026-10-28）

M2 的目标是同组学生之间可以通信：通信范围与高亮、交换视野、预设消息、每步预算、日志。
**验收**：防泄露测试通过；通信日志正确。**已通过**：自动化测试和机器人对局都通过了，用户也在浏览器里手测通过。
学生之间的通信和模型内部学到的通信是两回事，互不影响。老师端可以把两者叠加在地图上对比。

## 1. 模块

| 文件 | 职责 |
|---|---|
| `classroom/core/comm.py` | `CommState`：纯逻辑，不做 IO。每一步新建一个，跨步只保留"下一步送达"的消息队列。负责通信范围（L2）、交换视野（请求 / 确认 / 自动同意）、发消息、预算 |
| `classroom/core/views.py` | 学生载荷新增 `shared`（共享视野）、`contacts`（可联系队友）、`comm`（自己的通信状态）。仍然是学生载荷的唯一出口 |
| `classroom/core/round.py` | `comm_view_request` / `comm_view_answer` / `comm_message`；通信后写库，并刷新相关学生和老师的画面；`spectator_state` 给观战者推状态 |
| `classroom/storage/db.py` | `write_comm` 写 `comm_event` 表；`step_decision.shared_view_json` 开始写入 |
| `classroom/core/settings.py` | `comm_enabled`、`comm_radius`（默认 4）、`comm_view_exchange`、`comm_auto_accept`、`comm_messages`、`comm_budget`（默认不限）、`comm_delivery`（now / next） |
| `static/shared/grid-renderer.js` | 通信范围圆、可联系队友的光圈、消息气泡、共享视野区域、学生通信连线 |
| `static/student/`、`static/teacher/` | 学生页：通信卡片、确认条、消息记录。老师页：通信规则、学生通信图、通信范围、本步通信日志；另有地图缩放 |

## 2. 协议增量（在 `docs/M1_REPORT.md` §2 基础上）

**学生 → 服务端**

| 消息 | 说明 |
|---|---|
| `view_request{t, to}` | 请求和 `to` 号交换视野 |
| `view_answer{t, from, accept}` | 回应 `from` 号的请求 |
| `message{t, to, kind}` | 发预设消息。kind 取 `move_up` / `move_down` / `move_left` / `move_right` / `yield` / `go_first` / `arrived` |

**服务端 → 学生**：`state.view` 里新增三个字段，内容都受防泄露测试守护。

| 字段 | 内容 |
|---|---|
| `shared` | `[{agent, box, rows, agents}]`：本步与我交换成功的队友的视野框，只有格子和框里的智能体 |
| `contacts` | `[{agent, visible, ai_controlled}]`：通信半径内的队友。视野外的只给编号，不给位置 |
| `comm` | `{radius, budget_left, view_exchange, kinds, inbox, outbox, requests_in, requests_out, rejected_by}`：只有编号和消息类型，不含任何坐标 |

**服务端 → 老师**：`comm_event{event: {t, from, to, type, payload, accepted}}`；`teacher_state.comm_events` 是本步的全部事件。

## 3. 关键口径（与用户确认）

- **交换视野**：
  - 默认要对方确认，老师可以改成自动接受；
  - 和 AI 控制的队友交换时，对方自动同意；
  - 双方同时请求对方，算作同意，不另扣预算；
  - 共享视野**只在本步有效**，内容是对方本步的视野框，不含对方的目标和记忆，也不进自己的记忆地图；
  - 全图可见时不提供交换视野。
- **消息**：
  - 发给 AI 的消息照常记录，但 AI 不读也不回，界面上标明；
  - 可以选"下一步才送达"，学生页标注"（上一步发的）"。
- **预算**：默认不限。交换视野请求和发消息各算 1 次，回应别人的请求不算。
- **范围**：用决策时位置的 L2 距离，距离正好等于半径也算在内。在半径内但在视野外的队友，只显示编号和"看不见"。

## 4. 验收

- **自动化测试**：`pytest` 共 94 项全部通过，其中 M2 新增 16 项：
  - `test_comm.py` 11 项：邻居边界、预算、消息类型过滤、确认 / 拒绝 / 互相请求 / 自动接受 / AI 队友、全图可见时禁用交换、即时与下一步送达，以及经过回合机的写库；
  - 防泄露 3 组：随机通信下，共享视野严格等于对方视野框，可联系列表不带位置，通信状态不含坐标；
  - 端到端 1 项：两个学生经 WS 完成交换视野和发消息，并核对 `comm_event` 和 `shared_view_json`；
  - 观战者收到开始 / 结束 1 项。
- **机器人对局**（单独端口）：40 步，学生行字段齐全；通信事件包括消息 17 条、视野请求 25 次（同意 19 次、拒绝 1 次）。
- **浏览器手测**：用户试用后通过。

## 5. 试用中发现并修复的问题

| 现象 | 原因 | 修复 |
|---|---|---|
| 老师端认不出通信事件 | 推送时写成 `{"type": "comm_event", **event}`，事件自带的 `type` 把消息类型覆盖了 | 改成包一层 `event` 字段；CLAUDE.md 易错点里记下 |
| 观战的同学一直停在等待页 | 只有新开一局时才给观战者推状态，开始、暂停、结束都不推（M1 就有的问题） | 增加 `spectator_state`，`push_all` 时一并推送 |
| 机器人测试时用户的标签页被分到座位上 | 冒烟测试跑在用户正在用的 8000 端口服务上 | 冒烟测试改为在单独端口起临时服务（写进 CLAUDE.md） |
| 服务突然停了 | 日期变化时，后台进程随会话一起被结束 | 改用 `nohup setsid` 启动，与会话分离 |
| 检查本地服务时 curl 返回 503 | shell 设置了 http_proxy，请求先发给了代理 | 本地自检加 `--noproxy '*'`（写进 CLAUDE.md） |
| 老师端看不全地图 | 地图宽度固定为 100% | 加"适应窗口"和缩放按钮；可用高度改为实际测量；地图居中 |
| 加入二维码不显示 | segno 的 `svg_inline()` 输出没有 `xmlns`，作为独立的 `<img>` 文件无效 | 改为输出完整的 SVG 文档；测试增加 `xmlns` 断言 |

## 6. 已知限制与后续

- 消息只有预设的几种，没有自由文字，这是有意的：小学课堂，便于统计。
- 老师端只显示**本步**的学生通信图，整局回放在 M5（复盘）。
- "侦察兵"（单独设某个智能体的视野半径）暂缓，用户决定这次不做。
