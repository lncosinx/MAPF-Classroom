# VENDORED：classroom/maps/generators

来源：`third_party/pogema_toolbox_generators`（Cognitive-AI-Systems/pogema-toolbox `8556e1f`，Apache-2.0，见 `LICENSE`）。

| 文件 | 改动 |
|---|---|
| `random_generator.py`、`maze_generator.py`、`warehouse_generator.py`、`house_generator.py` | `from pogema_toolbox.generators.generator_utils import` → `from .generator_utils import`（各 1 行） |
| `generator_utils.py` | 无 |

课堂侧的统一入口在 `classroom/maps/generate.py`（warehouse 的 `!` 空地在那里替换成 `.`）。
`classroom/maps/presets/default-maps.yaml`（`pogema-logo`、`four-rooms`）原样拷自同一目录。
