# third_party：只读素材

本目录里的东西都是**原样拷贝**，在服务器上生成，日期是 2026-10-26。**不要修改这里的任何文件**：
- 需要改的代码，拷到 `classroom/` 或 `native/` 下面再改；
- 改了什么，记进对应的 `VENDORED.md`。

| 目录 | 内容 | 来源 | 许可证 |
|---|---|---|---|
| `chameleon_snap/` | MADCL 推理需要的源码子集 + 两份网络配置 | Chameleon `0fae917`（见 `SOURCE.txt`） | 用户自有 |
| `weights/` | 两个 checkpoint | Chameleon 的 `ckpt_il/`、`results_v5/` | 用户自有 |
| `lacam/` | LaCAM3 C++ 源码 + ctypes 用的 C API（`main.cpp`） | Cognitive-AI-Systems/MAPF-GPT `842384e` 的 `lacam/` | MIT（`LICENSE`；lacam3 原作者 Keisuke Okumura，分发前再核对上游许可证） |
| `pogema_toolbox_generators/` | 地图生成器 + `default-maps.yaml` | Cognitive-AI-Systems/pogema-toolbox `8556e1f` | Apache-2.0（`LICENSE`） |
| `maps/dev/` | dev 集：random 64 张、mazes 64 张（20×20 左右） | 服务器 `/root/autodl-tmp/dev_maps`（`DEV_SEED_BASE=7_000_000`） | — |
| `maps/validation/` | validation 集：random 128 张、mazes 128 张 | Chameleon 的 `marl-gpt/eval_configs/0{1,2}-*/maps.yaml` | — |
| `wheels/` | `pogema-2.0.0a0-py3-none-any.whl` | `pip wheel` 自 pogema git `332ee7d`，已核对与服务器上安装的版本逐文件相同 | MIT |

---

## chameleon_snap（MADCL 推理代码）

文件清单：
- `models/`：`mapf_comm_net`、`communication`、`spatial_encoder`、`temporal_core`、`comm_baselines`
- `envs/`：`observation_builder`、`action_mask`、`potential_field`、`d4_augment`
- `scripts/train_il_meta.py`：**只用其中的 `build_net`**。这个文件还 import 了训练用的模块（buffers、trainers），那些没有拷过来，所以不能直接 import 它，要把 `build_net` 函数本身拷走。
- `scripts/eval_bench.py`：**只作为口径参考**，不 import。参考三处：
  - `rollout_chunk` 的逐步推理和 TTA 写法；
  - 解码规则；
  - 终局指标 CSR/ISR/SoC/makespan 的算法。
- `configs/network_big.yaml`：主模型配置。
- `configs/network_gru_c2g.yaml`：2.16M 模型配置。

要点：
- **通信迟滞是"离开通信半径就断开"的版本**，见 `communication.py` 的迟滞分支：先 `w * is_candidate`，再只对新边施加 `tau_in` 阈值。golden 数据里实测：通信边的最大欧氏距离正好是 4.00，等于 `comm_radius`，没有任何一条边越过半径。
- **注意力留痕已经内置**：设 `net.communication.store_attn = True` 后，每次前向都会写入 `net.communication.last_attn`，形状 `(B,T,N,N)`。它是经过 topk 封顶和迟滞之后**最终生效**的权重，非零元素就是实际存在的通信边。课堂的"AI 通信图"直接读这个，不需要改代码。
  - 开了 TTA 时，batch 是 8 个视角，没有唯一的通信图。所以通信图要从关 TTA 的那一路取。
- `temporal_core.py` 在 import 时会尝试加载 `mamba_ssm`，失败会被捕获，不影响 GRU 路径。CPU 推理不需要任何 CUDA 组件。
- 模型的全部输入：
  - `patches (B,1,N,5,11,11)`、`scalars (B,1,N,8)`；
  - `positions (B,1,N,2)`：**padded 坐标**，float，用来算通信邻居；
  - `action_masks (B,1,N,5)`：bool，True 表示合法；
  - `stateful=True`，并且每局开始调用 `net.reset_comm_state()`。

## weights

| 文件 | 参数量 | 配置 | 用途 |
|---|---|---|---|
| `capbig_iter_20000.pt` | 7.45M | `network_big.yaml` | **主模型（默认）** |
| `MADCL_final.pt` | 2.16M | `network_gru_c2g.yaml` **+ 覆盖 `intent_mode: action`** | 备选，给性能弱的老师机用 |

- 加载方式：`torch.load(..., map_location="cpu", weights_only=False)["net"]`，key 去掉 `_orig_mod.` 前缀，然后 `load_state_dict(strict=True)`。
- 两个组合都已在 0fae917 的代码上验证可以严格加载。
- ⚠️ `network_gru_c2g.yaml` 里**没有** `intent_mode` 这一项，默认值是 `feature`。不覆盖成 `action` 的话 `MADCL_final.pt` 加载不上。

## lacam

C API，已在服务器上编译，并对 5 体小实例验证过路径合法：

```c
const char* run_lacam(const char* map, const char* scen, int N, float time_limit_sec);
```

- **地图格式**：MovingAI octile 格式：
  - `"type octile\nheight H\nwidth W\nmap\n"`，后面每行一个字符串，`@` 是障碍，`.` 是空地。
- **场景格式**：`"version 1\n"`，然后每个智能体一行：
  - `"{i}\ttmp.map\t{H}\t{W}\t{起点列}\t{起点行}\t{终点列}\t{终点行}\t1\n"`
  - ⚠️ 注意是**先列后行**。
- **返回值**：每个时间步一行，内容是 `"x,y|x,y|..."`，其中 x 是列、y 是行。第 0 行是起点。失败时返回的字符串里包含 `ERROR`（可能是 `ERROR_SCENE`、`ERROR_EMPTY` 或 `ERROR_SOLUTION`）。
- ⚠️ **不是线程安全的**：
  - 结果放在 `static std::string` 里；
  - `Planner::*` 参数是全局静态变量。
  
  所以所有调用必须用**一把全局锁**串行起来。注意 ctypes 调用期间会释放 GIL，多个线程会真的同时进入这个函数。
- ⚠️ **每次调用都会在当前目录写 `lacam_log.txt`**（`make_log`）。拷到 `native/lacam` 后**删掉 `make_log` 那一行**。
- ⚠️ `CMakeLists.txt` 写死了链接 `libpthread.so`。在 Linux 上能用，但在 Windows（MSVC 或 mingw）上要改成 `find_package(Threads)` + `Threads::Threads`。
- 求解器的 seed 固定为 0，但 refiner 是 anytime 的，结果受时限影响。同一个状态的求解结果**不保证**每次逐字相同。

## pogema_toolbox_generators

- 包含 `random_generator`、`maze_generator`、`warehouse_generator`、`house_generator`、`generator_utils`。
- 它们都 `from pogema_toolbox.generators.generator_utils import ...`，拷进 `classroom/maps/` 时要改成相对 import。
- 依赖只有 numpy 和 pyyaml。
- `warehouse` 生成器用 `!` 表示空地，用之前要替换成 `.`。
- `maze_generator` 输出的实际尺寸大约是 `(w//2)*2+1`，不完全等于请求的尺寸。
- `default-maps.yaml` 里有 `pogema-logo` 和 `four-rooms` 两张图。
