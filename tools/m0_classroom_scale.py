#!/usr/bin/env python3
"""M0 验收 ④：测出教室规模（小图、少量智能体）上模型和专家各自的表现。这个规模从来没评测过（PLAN.md §9）。

口径：random（障碍 0.2）/ maze（墙密度 0.5）× 尺寸 8、12 × N∈{4,8,12,16} × 32 张图（生成种子 0..31，
起终点在最大连通区内随机放置）；on_target=nothing、soft、128 步、全员到达即结束（课堂真实语义）。
模型：sample T=1.0 wait_bias=1.0 关 TTA；专家：LaCAM（时限 0.2s，计划被遵循时复用），失败退回简易专家。
结果写 data/m0/classroom_scale.csv，并打印带 95% bootstrap CI 的汇总表。

用法：python tools/m0_classroom_scale.py [--workers 8] [--maps 32]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KINDS = (("random", 0.2), ("maze", 0.5))
SIZES = (8, 12)
AGENT_COUNTS = (4, 8, 12, 16)
MAX_STEPS = 128
SAMPLE_SEED_BASE = 20261027


def _init_worker():
    if hasattr(os, "nice"):
        try:
            os.nice(19)
        except OSError:
            pass
    import torch
    torch.set_num_threads(1)


def _run_one(job):
    kind, density, size, n, map_seed = job
    from classroom.ai.model_policy import ModelPolicy
    from classroom.core.coords import parse_map
    from classroom.core.game import GroupGame
    from classroom.maps.generate import generate, random_pairs
    ms = generate(kind, size, size, density, seed=map_seed)
    grid = parse_map(ms)
    try:
        starts, goals = random_pairs(grid, n, seed=map_seed)
    except ValueError:
        return []   # 图太挤放不下
    out = []
    for who in ("model", "expert"):
        policy = ModelPolicy(n, seed=SAMPLE_SEED_BASE + map_seed)
        g = GroupGame(ms, agents_xy=starts, targets_xy=goals, max_steps=MAX_STEPS, controllers=[who] * n,
                      stop_on_success=True, model=policy, expert_time_limit=0.2)
        simple = False
        while not g.done:
            ctx = g.begin_step()
            simple |= ctx.expert is not None and ctx.expert.source == "simple"
            g.apply(g.default_actions(ctx))
        out.append(dict(kind=kind, size=size, H=grid.shape[0], W=grid.shape[1], num_agents=n, map_seed=map_seed,
                        policy=who, simple_expert=int(simple), **g.metrics()))
    return out


def boot_ci(x: np.ndarray, level=0.95, n_boot=10000, seed=0):
    rng = np.random.default_rng(seed)
    m = x[rng.integers(0, len(x), size=(n_boot, len(x)))].mean(1)
    return float(np.quantile(m, (1 - level) / 2)), float(np.quantile(m, 1 - (1 - level) / 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 4) - 4)))
    ap.add_argument("--maps", type=int, default=32)
    ap.add_argument("--out", default=str(ROOT / "data" / "m0"))
    ap.add_argument("--reuse", action="store_true")
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    path = out / "classroom_scale.csv"

    if not (args.reuse and path.exists()):
        jobs = [(k, d, s, n, ms) for k, d in KINDS for s in SIZES for n in AGENT_COUNTS for ms in range(args.maps)]
        rows, t0 = [], time.time()
        with ProcessPoolExecutor(args.workers, mp_context=get_context("spawn"), initializer=_init_worker) as ex:
            for i, r in enumerate(ex.map(_run_one, jobs, chunksize=2)):
                rows += r
                if (i + 1) % 64 == 0:
                    print(f"  {i + 1}/{len(jobs)} 个实例，{time.time() - t0:.0f}s", flush=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)

    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    summary = []
    print(f"\n{'类型':<7}{'尺寸':>6}{'N':>4}{'图数':>5}  {'模型 CSR (95%CI)':<22}{'专家 CSR':>9}"
          f"{'模型ISR':>8}{'模型SoC':>8}{'专家SoC':>8}{'模型mk':>7}{'专家mk':>7}{'简易专家':>8}")
    for k, _ in KINDS:
        for s in SIZES:
            for n in AGENT_COUNTS:
                sel = [r for r in rows if r["kind"] == k and int(r["size"]) == s and int(r["num_agents"]) == n]
                if not sel:
                    continue
                mo = [r for r in sel if r["policy"] == "model"]; ex = [r for r in sel if r["policy"] == "expert"]
                col = lambda rs, m: np.array([float(r[m]) for r in rs])  # noqa: E731
                csr = col(mo, "CSR"); lo, hi = boot_ci(csr)
                row = dict(kind=k, size=s, H=int(sel[0]["H"]), num_agents=n, maps=len(mo),
                           model_CSR=csr.mean(), model_CSR_ci=[lo, hi], expert_CSR=col(ex, "CSR").mean(),
                           model_ISR=col(mo, "ISR").mean(), expert_ISR=col(ex, "ISR").mean(),
                           model_SoC=col(mo, "SoC").mean(), expert_SoC=col(ex, "SoC").mean(),
                           model_makespan=col(mo, "makespan").mean(), expert_makespan=col(ex, "makespan").mean(),
                           simple_expert=int(sum(int(r["simple_expert"]) for r in ex)))
                summary.append(row)
                print(f"{k:<7}{row['H']:>3}×{row['H']:<2}{n:>4}{len(mo):>5}  "
                      f"{csr.mean():.3f} [{lo:.3f},{hi:.3f}]   {row['expert_CSR']:>7.3f}"
                      f"{row['model_ISR']:>8.3f}{row['model_SoC']:>8.1f}{row['expert_SoC']:>8.1f}"
                      f"{row['model_makespan']:>7.1f}{row['expert_makespan']:>7.1f}{row['simple_expert']:>8d}")
    (out / "classroom_scale_summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False),
                                                      encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
