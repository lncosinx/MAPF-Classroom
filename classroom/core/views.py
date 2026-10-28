"""学生 / 老师载荷。学生载荷是防作弊的**唯一出口**（CLAUDE.md 不变式 9，测试见 tests/test_views_no_leak.py）。

学生载荷只能包含：
  - 自己视野内的格子和智能体（视野 = Chebyshev 距离 ≤ r 的方框，与 pogema 一致）；
  - memory 模式下自己走过时见过的格子（只有障碍 / 空地，不含其他智能体）；
  - 本步和自己交换视野成功的队友的视野框（只含格子和其中的智能体，不含对方的目标和记忆）；
  - 本步可联系的队友编号（视野外的只给编号、不给位置）和自己的通信状态（core/comm.py）；
  - 自己的目标：在视野内（或老师设成"始终可见"）时给坐标；否则只给它在视野框边上的投影，
    算法与 pogema Grid.get_square_target 相同（行、列偏移各自截断到 [-r, r]），即模型看到的 ch2。
地图尺寸 H×W 也会下发：前端要用它画画布，它不暴露任何格子的内容。
**绝不**包含专家建议、模型建议或任何视野外的格子 / 智能体（建议的揭晓走单独的 reveal 消息）。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from .comm import CommState
from .game import GroupGame, StepContext
from .settings import RoundSettings


class ViewMemory:
    """每个智能体见过哪些格子（memory 迷雾模式用）。"""

    def __init__(self, num_agents: int, shape: tuple[int, int]):
        self.seen = np.zeros((num_agents, *shape), dtype=bool)

    def update(self, agent: int, box: tuple[int, int, int, int]):
        r0, c0, r1, c1 = box
        self.seen[agent, r0:r1 + 1, c0:c1 + 1] = True


def view_box(pos, radius: Optional[int], shape) -> tuple[int, int, int, int]:
    """视野方框（含端点，已裁到地图内）。radius=None 表示全图。"""
    H, W = shape
    if radius is None:
        return 0, 0, H - 1, W - 1
    r, c = int(pos[0]), int(pos[1])
    return max(0, r - radius), max(0, c - radius), min(H - 1, r + radius), min(W - 1, c + radius)


def in_box(rc, box) -> bool:
    return box[0] <= rc[0] <= box[2] and box[1] <= rc[1] <= box[3]


def _box_content(game: GroupGame, ctx: StepContext, owner: int, settings: RoundSettings, viewer: int):
    """owner 本步视野框里的格子（按行 '.' / '#'）和智能体（不含 viewer 自己）。"""
    box = view_box(ctx.positions[owner], settings.view_radius, game.grid.shape)
    r0, c0, r1, c1 = box
    rows = ["".join("#" if v else "." for v in game.grid[r, c0:c1 + 1]) for r in range(r0, r1 + 1)]
    agents = [{"agent": int(j), "pos": [int(p[0]), int(p[1])]}
              for j, p in enumerate(ctx.positions) if j != viewer and in_box(p, box)]
    return box, rows, agents


def student_view(game: GroupGame, ctx: StepContext, agent: int, settings: RoundSettings,
                 memory: Optional[ViewMemory] = None, comm: Optional[CommState] = None,
                 ai_agents: frozenset = frozenset()) -> dict:
    """第 agent 号智能体在本步应当看到的全部内容。会顺带更新 memory（共享来的视野不进记忆）。"""
    H, W = game.grid.shape
    pos = ctx.positions[agent]
    box, rows, agents = _box_content(game, ctx, agent, settings, viewer=agent)
    r0, c0, r1, c1 = box

    mem_cells = []
    if memory is not None:
        if settings.fog_mode == "memory" and settings.view_radius is not None:
            seen = memory.seen[agent].copy()
            seen[r0:r1 + 1, c0:c1 + 1] = False          # 当前视野内的已经在 rows 里了
            mem_cells = [[int(r), int(c), int(game.grid[r, c])] for r, c in np.argwhere(seen)]
        memory.update(agent, box)

    goal = game.targets[agent]
    goal_visible = settings.goal_display == "visible" or in_box(goal, box)
    goal_proj = None
    if not goal_visible:
        # 与 pogema 一致：偏移按轴截断到视野半径。pos 和 goal 都在图内，投影点必在两者之间，也就必在图内
        r = settings.view_radius
        goal_proj = (np.asarray(pos) + np.clip(np.asarray(goal) - np.asarray(pos), -r, r)).astype(int).tolist()

    shared, contacts, comm_part = [], [], None
    if comm is not None:
        for j in sorted(comm.shared.get(agent, ())):
            sbox, srows, sagents = _box_content(game, ctx, j, settings, viewer=agent)
            shared.append({"agent": int(j), "box": list(sbox), "rows": srows, "agents": sagents})
        # 可联系的队友：视野内的位置已经在 agents 里；视野外的只给编号（"看不见的同伴也能联系"）
        contacts = [{"agent": j, "visible": bool(in_box(ctx.positions[j], box)),
                     "ai_controlled": j in ai_agents}
                    for j in comm.contacts(agent)]
        comm_part = comm.student_part(agent)

    return {
        "t": int(ctx.t),
        "H": int(H), "W": int(W),
        "radius": settings.view_radius,
        "agent": int(agent),
        "pos": [int(pos[0]), int(pos[1])],
        "box": [r0, c0, r1, c1],
        "rows": rows,
        "memory": mem_cells,
        "agents": agents,
        "goal": [int(goal[0]), int(goal[1])] if goal_visible else None,
        "goal_proj": goal_proj,
        "at_goal": bool(np.array_equal(pos, goal)),
        "legal": [bool(x) for x in ctx.legal[agent]],
        "shared": shared,
        "contacts": contacts,
        "comm": comm_part,
    }


def teacher_view(game: GroupGame, ctx: Optional[StepContext]) -> dict:
    """上帝视角：全部位置、目标，以及本步的模型 / 专家建议和 AI 通信边。只发给老师。"""
    out = {
        "t": int(game.t),
        "H": int(game.grid.shape[0]), "W": int(game.grid.shape[1]),
        "map": game.map_str.splitlines(),
        "positions": game.positions.tolist(),
        "targets": game.targets.tolist(),
        "at_goal": game.at_goal().tolist(),
        "done": bool(game.done),
    }
    if ctx is not None:
        out["model_actions"] = ctx.model.actions.tolist()
        out["model_probs"] = np.round(ctx.model.probs, 3).tolist()
        # 通信边：last_attn 非零即边（M2 的"AI 通信图"叠加层用）
        out["ai_edges"] = [[int(i), int(j)] for i, j in np.argwhere(ctx.model.attn > 0) if i != j]
        if ctx.expert is not None:
            out["expert_actions"] = ctx.expert.actions.tolist()
            out["expert_source"] = ctx.expert.source
    return out
