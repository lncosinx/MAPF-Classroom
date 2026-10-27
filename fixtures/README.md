# fixtures：MADCL 对拍基准（只读）

这些基准在服务器上生成，生成时间 2026-10-26，所用代码是 Chameleon `0fae917`（`third_party/chameleon_snap`），权重是 `capbig_iter_20000.pt`，CPU 推理。

它们的作用是：**笔记本上不需要 Chameleon，也能证明 `classroom/ai/madcl` 和课堂环境的封装跟原始模型逐层一致。**

`make_fixtures.py` 只能在服务器上跑，放在这里只是为了留存来源。笔记本上**不要运行它**。

---

## 1. golden_steps_<family>_n<N>.npz —— 逐步黄金数据

| 文件 | 图（dev 集，按图名排序取前几张） | 每局智能体数 |
|---|---|---|
| `golden_steps_random_n16.npz` | `dev-random-seed-000`、`dev-random-seed-001` | 16 |
| `golden_steps_random_n32.npz` | `dev-random-seed-000` | 32 |
| `golden_steps_mazes_n16.npz` | `dev-mazes-seed-000`、`dev-mazes-seed-001` | 16 |
| `golden_steps_mazes_n32.npz` | `dev-mazes-seed-000` | 32 |

**口径**：on_target=nothing，collision=soft，obs_radius=5，128 步，seed=0。

**执行的动作**：用关 TTA 的 logits 解码得到：先对 wait 的 logit 减 1.0，再除以温度 T=1.0 后 softmax 采样，采样用 `torch.Generator(20261026 + 局号)`。
- 同一条轨迹上还并行跑了一个 TTA 网络，**只记录它的平均概率**，不用它的动作。
- 因为动作已经落盘，回放和随机数无关。

**数组说明**（E = 局数，T = 128，N = 智能体数）：

| key | 形状 / 类型 | 含义 |
|---|---|---|
| `map_name`、`map_str` | (E,) str | 图名和**未 padding** 的地图字符串（`.`/`#`） |
| `grid_config_json` | (E,) str | `GridConfig` 的参数（size / num_agents / max_episode_steps / obs_radius / collision_system / on_target / seed / map_name），用 `GridConfig(map=map_str, **json.loads(...))` 就能重建 |
| `grid` | (E,H,W) int8 | `env.grid.get_obstacles()`，**padded** |
| `targets_xy` | (E,N,2) int32 | 目标，padded |
| `agents_xy` | (E,T,N,2) int32 | 每一步决策时的位置，padded |
| `final_agents_xy` | (E,N,2) int32 | 第 T 步之后的位置 |
| `obs` | (E,T,N,3,11,11) float32 | pogema 原生观测 |
| `patches` | (E,T,N,5,11,11) float32 | `ObservationBuilder` 输出（cost2go 开启，clip=10） |
| `scalars` | (E,T,N,8) float32 | 同上 |
| `masks` | (E,T,N,5) bool | `build_action_mask_batch`，True 表示合法 |
| `prev_actions` | (E,T,N) int64 | 喂给观测构建的上一步动作（第 0 步全是 0） |
| `logits` | (E,T,N,5) float32 | 关 TTA 的原始 logits，非法动作是 `-inf`，**还没减 wait_bias** |
| `tta_probs` | (E,T,N,5) float32 | TTA 8 视角平均后的概率 |
| `attn` | (E,T,N,N) float32 | `communication.last_attn`，即最终生效的通信权重，非零即边 |
| `actions` | (E,T,N) int64 | 实际执行的动作 |

**笔记本上的用法**（`tests/test_golden.py`），对每一局：
1. 用 `GridConfig(map=map_str, **gc)` 加 `pogema_v0` 建 env，`reset(seed=gc["seed"])`，断言起点和终点与 `agents_xy[:,0]`、`targets_xy` 相等。
2. 新建模型实例，调用 `reset_comm_state()`，开启 `store_attn`。然后按 `actions` 逐步回放，每一步断言：
   - `agents_xy`、`obs`、`patches`、`scalars`、`masks`、`prev_actions`：**逐项完全相等**；
   - `logits`（`-inf` 的位置要完全一致）、`attn`，以及另一个独立实例算出的 `tta_probs`：**`np.allclose(rtol=1e-4, atol=1e-3)`**。
     - 服务器上同一台机器，误差通常约 1.5e-5；但在 CPU 争用时出现过**一次** 2.5e-4，之后无法复现，推测是 CPU 内核路径不同。
     - 超差时先单独重跑一次；如果稳定超差，再定位是哪一步、哪个量出的问题。
3. 最后断言 `final_agents_xy` 也相等。

**已验证**：在服务器上用 `make_fixtures.py --verify` 做过同样的事。它只用纯 pogema 重建 env、不经过 `PogemaWrapper`，按记录的动作回放，结果**全部一致**：浮点项误差 < 1e-5，其余逐项相等。另外，同样的代码跑两次，产物逐字节相同。

**覆盖情况**：
- 每步平均通信边数：random 约 32，mazes 约 21（N=16）。
- 所有通信边的欧氏距离都 ≤ 4.00，也就是"离开半径就断开"的口径。
- N=16 的四局里，最后一次有智能体移动发生在第 23–51 步，之后全员守在目标上。N=32 那两局用来覆盖更拥挤、更多让行的情形。

---

## 2. reference_metrics.json / reference_episodes.csv —— 统计参照

这是在服务器上直接调用快照里的 `scripts/eval_bench.py` 跑出来的结果，**所有口径参数都显式写死**：

```
--families random,mazes --agent-counts 8,16,32
--maps-file third_party/maps/dev/{family}.yaml --num-maps 64 --seeds 0
--max-steps 128 --on-target nothing --batch-envs 16
--action-mode sample --base-temp 1.0 --wait-bias 1.0 --goal-hold none --momentum-bias 0.0
（不加 --tta）--device cpu
```

- 每档用 64 张 dev 图、seed=0，共 384 局。服务器 8 线程大约跑 10 分钟。
- `reference_episodes.csv` 是**每一局**的 CSR / ISR / SoC / makespan。最后三列（escape_frac / avg_neighbors / edge_flip）是**整批 16 局的平均值**，不能当作逐局数据做统计推断。
- `reference_metrics.json` 是各档的聚合值，`args` 字段里记录了完整的参数。

| 族 | N | CSR (±95%CI) | ISR | SoC | makespan | 平均邻居数 |
|---|---|---|---|---|---|---|
| random | 8 | 0.984 ± 0.030 | 0.996 | 128.2 | 27.4 | 0.71 |
| random | 16 | 0.969 ± 0.043 | 0.998 | 302.1 | 37.5 | 1.45 |
| random | 32 | 0.953 ± 0.052 | 0.998 | 720.7 | 48.5 | 2.89 |
| mazes | 8 | 0.984 ± 0.030 | 0.996 | 173.7 | 38.4 | 0.53 |
| mazes | 16 | 0.891 ± 0.076 | 0.984 | 431.0 | 58.5 | 1.28 |
| mazes | 32 | 0.797 ± 0.099 | 0.988 | 1182.8 | 87.8 | 2.70 |

**笔记本上的用法（M0 验收 ②）**：
1. 用无头 GroupGame（全体由 AI 控制，口径同上，每组 B=1）跑**同样的图、同样的 N、seed=0**。
   - 起点和终点会和 eval_bench 完全相同，因为 `split="all"` 时 seed 不做映射，`pogema_v0(GridConfig(map=..., num_agents=N, seed=0, obs_radius=5, ...))` 的 `reset(seed=0)` 给出同样的布局。
   - 图按名字排序遍历。
2. 采样的随机数和 eval_bench 不一样（eval_bench 用全局 torch RNG 并且按批采样），所以**不要求逐局相同**，只做统计比较。
3. **用配对比较**：两边每一局的图和起终点都相同，对 CSR / ISR / SoC / makespan 逐局求差，然后按图做 bootstrap，得到差值的置信区间。
   - 一共 6 档 × 4 个指标 = 24 次检验，建议用 **99% CI**（偶然失败的概率更低）；
   - 每个 CI 都覆盖 0 就算通过；
   - 有不覆盖 0 的，先看是不是集中在某一档或某一个指标，再查适配代码。
4. 和 `results_v5/RESULTS.md` **不能直接比**：那边是 validation 集、开了 TTA。
