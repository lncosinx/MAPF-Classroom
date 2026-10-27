# VENDORED：classroom/ai/madcl

来源：`third_party/chameleon_snap`（Chameleon `0fae917`，2026-10-26 `git archive` 原样导出）。

| 本目录文件 | 来源 | 改动 |
|---|---|---|
| `mapf_comm_net.py` | `models/mapf_comm_net.py` | 4 行 `from models.x import` → `from .x import` |
| `communication.py`、`spatial_encoder.py`、`temporal_core.py`、`comm_baselines.py` | `models/` 同名 | 无 |
| `observation_builder.py` | `envs/observation_builder.py` | 1 行 `from envs.potential_field import` → `from .potential_field import` |
| `potential_field.py`、`action_mask.py`、`d4_augment.py` | `envs/` 同名 | 无 |
| `build.py` | `scripts/train_il_meta.py` 中的 `build_net` 函数 | 只拷这一个函数，函数体未改 |
| `__init__.py` | —— | 课堂自写：`load_madcl()` 加载入口 |

网络配置 `network_big.yaml`、`network_gru_c2g.yaml` 与两份权重放在仓库根的 `weights/`，同样原样拷贝。

## 已知的平台数值差异（2026-10-27 实测）

`observation_builder._build_footprint_channels_vectorized` 里 `0.9 ** ages.astype(np.float32)` 走 numpy 的
float32 `pow`，其实现按 CPU 指令集分派。生成 fixtures 的服务器（推测有 AVX512）在 125 个衰减值中有 27 个
比正确舍入结果**低 1 ULP**（如 age=4：0.6560999 vs 0.6561）；无 AVX512 的机器（开发笔记本 Ryzen 7535H）
给出正确舍入值。因此：

- vendor 代码**不改**（改了也只能对上一种平台）；
- `tests/test_golden.py` 对 patch 的 ch3（足迹）用 `atol=1e-6`（≤1 ULP），其余通道仍逐位相等；
- `fixtures/check_selfcontained.py`（只读）在无 AVX512 的机器上会在 patch 断言处失败，属预期；
  以 `pytest tests/test_golden.py` 为准。下游 logits/attn 误差实测 ≤ 3.2e-5，远小于 1e-3 容差。
