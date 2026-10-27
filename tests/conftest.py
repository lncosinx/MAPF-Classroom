from __future__ import annotations

from pathlib import Path

import pytest
import torch
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def _torch_threads():
    torch.set_num_threads(4)


@pytest.fixture(scope="session")
def dev_maps():
    """{family: [(name, map_str), ...]}，按图名排序。"""
    out = {}
    for fam in ("random", "mazes"):
        d = yaml.safe_load((ROOT / "third_party/maps/dev" / f"{fam}.yaml").read_text(encoding="utf-8"))
        out[fam] = sorted(d.items())
    return out
