#!/usr/bin/env python3
"""M0 验收 ②：无头"全 AI"跑 dev 集，与 fixtures/reference_episodes.csv 做配对比较。

口径（全部写死，与 fixtures/README.md §2 一致）：dev 集 random/mazes × N∈{8,16,32} × 64 图（按名排序）、
seed=0、128 步、on_target=nothing、soft、sample T=1.0 wait_bias=1.0、关 TTA、跑满 128 步（不提前结束）。
采样随机数与 eval_bench 不同，所以不要求逐局相同：对每档每个指标逐图求差（ours - ref），
按图 bootstrap 求差值均值的 99% CI，24 项全部覆盖 0 即通过。

用法：python tools/m0_reference.py [--workers 8] [--out data/m0]
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

FAMILIES = ("random", "mazes")
AGENT_COUNTS = (8, 16, 32)
METRICS = ("CSR", "ISR", "SoC", "makespan")
SEED = 0
MAX_STEPS = 128
SAMPLE_SEED_BASE = 20261027


def _init_worker():
    # 低优先级、单线程，避免把机器卡死（worker 数 ≤ nproc-4）
    if hasattr(os, "nice"):
        try:
            os.nice(19)
        except OSError:
            pass
    import torch
    torch.set_num_threads(1)


def _run_one(job):
    fam, n, name, ms, k = job
    from classroom.ai.model_policy import ModelPolicy
    from classroom.core.game import GroupGame
    policy = ModelPolicy(n, variant="big", seed=SAMPLE_SEED_BASE + k, base_temp=1.0, wait_bias=1.0,
                         action_mode="sample", tta=False)
    g = GroupGame(ms, num_agents=n, seed=SEED, max_steps=MAX_STEPS, map_name=name,
                  stop_on_success=False, model=policy, need_expert=False)
    m = g.run()
    return dict(family=fam, num_agents=n, map_name=name, **m)


def paired_ci(diff: np.ndarray, level: float = 0.99, n_boot: int = 20000, seed: int = 0):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(diff), size=(n_boot, len(diff)))
    means = diff[idx].mean(1)
    a = (1 - level) / 2
    return float(diff.mean()), float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def main():
    import yaml
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 4) - 4)))
    ap.add_argument("--out", default=str(ROOT / "data" / "m0"))
    ap.add_argument("--reuse", action="store_true", help="已有 ours.csv 时只重新做统计")
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    ours_csv = out / "reference_ours.csv"

    if not (args.reuse and ours_csv.exists()):
        jobs, k = [], 0
        for fam in FAMILIES:
            maps = yaml.safe_load((ROOT / "third_party/maps/dev" / f"{fam}.yaml").read_text(encoding="utf-8"))
            for n in AGENT_COUNTS:
                for name in sorted(maps):
                    jobs.append((fam, n, name, maps[name], k)); k += 1
        t0 = time.time()
        rows = []
        with ProcessPoolExecutor(args.workers, mp_context=get_context("spawn"), initializer=_init_worker) as ex:
            for i, r in enumerate(ex.map(_run_one, jobs, chunksize=2)):
                rows.append(r)
                if (i + 1) % 32 == 0:
                    print(f"  {i + 1}/{len(jobs)} 局，{time.time() - t0:.0f}s", flush=True)
        with ours_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
        print(f"跑完 {len(rows)} 局，用时 {time.time() - t0:.0f}s")

    def load(p):
        with Path(p).open(encoding="utf-8") as f:
            return {(r["family"], int(r["num_agents"]), r["map_name"]): r for r in csv.DictReader(f)}

    ours, ref = load(ours_csv), load(ROOT / "fixtures/reference_episodes.csv")
    assert set(ours) == set(ref), "局集合与参照不一致"
    summary, all_pass = [], True
    print(f"\n{'族':<7}{'N':>3}  {'指标':<9}{'ours':>9}{'ref':>9}{'差值':>9}   99% CI          判定")
    for fam in FAMILIES:
        for n in AGENT_COUNTS:
            keys = sorted(k for k in ours if k[0] == fam and k[1] == n)
            for m in METRICS:
                a = np.array([float(ours[k][m]) for k in keys]); b = np.array([float(ref[k][m]) for k in keys])
                d, lo, hi = paired_ci(a - b)
                ok = lo <= 0 <= hi
                all_pass &= ok
                summary.append(dict(family=fam, num_agents=n, metric=m, ours=a.mean(), ref=b.mean(),
                                    diff=d, ci_lo=lo, ci_hi=hi, ok=ok))
                print(f"{fam:<7}{n:>3}  {m:<9}{a.mean():>9.3f}{b.mean():>9.3f}{d:>+9.3f}   "
                      f"[{lo:+8.3f},{hi:+8.3f}]  {'通过' if ok else '★不覆盖0'}")
    (out / "reference_summary.json").write_text(json.dumps(dict(all_pass=all_pass, rows=summary), indent=1,
                                                           ensure_ascii=False), encoding="utf-8")
    print(f"\n验收 ②：{'通过' if all_pass else '未通过'}（{sum(r['ok'] for r in summary)}/{len(summary)} 项覆盖 0）")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
