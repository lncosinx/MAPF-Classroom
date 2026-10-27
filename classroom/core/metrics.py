"""终局指标，口径与 Chameleon eval_bench.rollout_chunk 结尾逐行一致（不变式 10）。

  - CSR / ISR 只看最后一步；
  - arrive_t = 最后一次"不在目标上"的决策时刻 + 1（一直在目标上记 0），失败的记 max_steps；
  - SoC = Σ arrive_t，makespan = max arrive_t（右删失于 max_steps）。
不要用 Chameleon utils/metrics.py 的旧口径（首次到达）。
"""
from __future__ import annotations

import numpy as np


def episode_metrics(at_goal_hist: np.ndarray, final_at_goal: np.ndarray, max_steps: int) -> dict:
    """at_goal_hist：(T,N) bool，第 t 行是第 t 步**决策时**是否在目标上（T ≤ max_steps）；
    final_at_goal：(N,) bool，最后一步之后是否在目标上。"""
    hist = np.asarray(at_goal_hist, dtype=bool)[:max_steps]
    final = np.asarray(final_at_goal, dtype=bool)
    N = final.shape[0]
    arrive_t = np.full(N, float(max_steps), dtype=np.float32)
    for i in range(N):
        if final[i]:
            notat = np.nonzero(~hist[:, i])[0] if hist.size else np.zeros(0, dtype=np.int64)
            arrive_t[i] = float(notat[-1] + 1) if len(notat) else 0.0
    return dict(CSR=float(final.all()), ISR=float(final.mean()),
                SoC=float(arrive_t.sum()), makespan=float(arrive_t.max()) if N else 0.0)
