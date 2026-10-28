"""学生通信（PLAN.md §6.3）：通信范围、交换视野、预设消息、每步预算。纯逻辑，不做 IO。

和模型内部学到的通信互相独立：这里只管学生之间（以及学生和 AI 队友之间）的交流，
推送和写库由 core/round.py 负责。CommState 每一步新建一个；跨步保留的只有"下一步送达"的消息队列。

口径：
  - 范围按 L2 距离（≤ comm_radius 算在内），用本步**决策时**的位置；
  - 交换视野只在本步有效：成立后双方本步都能看到对方的视野框（不含对方的目标和记忆）；
  - 对方由 AI 控制时交换视野自动同意；发给 AI 的消息照常记录，但 AI 不读也不回；
  - 交换视野请求和发消息各消耗 1 次预算；回应别人的请求不消耗。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .settings import RoundSettings


@dataclass
class CommEvent:
    t: int
    from_agent: int
    to_agent: Optional[int]
    type: str                       # view_request / view_accept / view_reject / msg
    payload: Optional[dict] = None
    accepted: Optional[bool] = None

    def public(self) -> dict:
        return {"t": self.t, "from": self.from_agent, "to": self.to_agent, "type": self.type,
                "payload": self.payload, "accepted": self.accepted}


@dataclass
class Message:
    t_sent: int
    from_agent: int
    to_agent: int
    kind: str

    def public(self) -> dict:
        return {"t": self.t_sent, "from": self.from_agent, "to": self.to_agent, "kind": self.kind}


def contact_matrix(positions: np.ndarray, radius: float) -> np.ndarray:
    """(N,N) bool：i 和 j 的 L2 距离 ≤ radius（不含自己）。"""
    p = np.asarray(positions, dtype=np.float64)
    d2 = ((p[:, None, :] - p[None, :, :]) ** 2).sum(-1)
    m = d2 <= radius * radius + 1e-9
    np.fill_diagonal(m, False)
    return m


class CommState:
    def __init__(self, t: int, positions: np.ndarray, settings: RoundSettings,
                 is_ai: Callable[[int], bool], carry: Optional[list[Message]] = None):
        self.t = t
        self.s = settings
        self.is_ai = is_ai
        self.n = len(positions)
        self.matrix = contact_matrix(positions, settings.comm_radius)
        self.used: dict[int, int] = {}
        self.pending: set[tuple[int, int]] = set()          # (请求方, 被请求方)
        self.rejected: set[tuple[int, int]] = set()
        self.shared: dict[int, set[int]] = {}               # agent -> 本步与之交换成功的智能体
        self.inbox: dict[int, list[Message]] = {}
        self.outbox: dict[int, list[Message]] = {}
        self.next_queue: list[Message] = []                 # comm_delivery=next：下一步送达
        self.events: list[CommEvent] = []                   # 本步全部事件（老师端通信图用）
        self._new: list[CommEvent] = []                     # 还没写库 / 推送的事件
        for m in carry or []:
            self.inbox.setdefault(m.to_agent, []).append(m)

    # ------------------------------------------------------------------ 查询
    def contacts(self, agent: int) -> list[int]:
        if not self.s.comm_enabled:
            return []
        return [int(j) for j in np.nonzero(self.matrix[agent])[0]]

    def budget_left(self, agent: int) -> Optional[int]:
        if self.s.comm_budget is None:
            return None
        return max(0, self.s.comm_budget - self.used.get(agent, 0))

    def view_exchange_allowed(self) -> bool:
        # 全图可见时交换视野没有意义
        return self.s.comm_enabled and self.s.comm_view_exchange and self.s.view_radius is not None

    def drain(self) -> list[CommEvent]:
        out, self._new = self._new, []
        return out

    # ------------------------------------------------------------------ 动作（返回中文错误信息或 None）
    def _check(self, a: int, b: int, spend: bool) -> Optional[str]:
        if not self.s.comm_enabled:
            return "老师没有开启通信"
        if not (0 <= b < self.n) or b == a:
            return "没有这个队友"
        if not self.matrix[a, b]:
            return "对方不在通信范围内"
        if spend and self.budget_left(a) == 0:
            return "这一步的通信次数用完了"
        return None

    def _spend(self, a: int):
        self.used[a] = self.used.get(a, 0) + 1

    def _emit(self, ev: CommEvent):
        self.events.append(ev)
        self._new.append(ev)

    def _make_shared(self, a: int, b: int):
        self.shared.setdefault(a, set()).add(b)
        self.shared.setdefault(b, set()).add(a)

    def request_view(self, a: int, b: int) -> tuple[Optional[str], Optional[str]]:
        """a 请求和 b 交换视野。返回 (结果 accepted/pending, 错误)。"""
        if not self.view_exchange_allowed():
            return None, "现在不能交换视野"
        if b in self.shared.get(a, set()):
            return None, "你们这一步已经交换过视野了"
        if (a, b) in self.pending:
            return None, "已经发过请求了，等对方回应"
        if (b, a) in self.pending:
            # 对方已经先请求了我：我的请求就当作同意，不另扣预算
            return ("accepted", None) if self.answer(a, b, True) is None else (None, "请求失败")
        err = self._check(a, b, spend=True)
        if err:
            return None, err
        self._spend(a)
        auto = self.is_ai(b) or self.s.comm_auto_accept
        self._emit(CommEvent(self.t, a, b, "view_request", None, True if auto else None))
        if auto:
            self._make_shared(a, b)
            self._emit(CommEvent(self.t, b, a, "view_accept", {"auto": True}, True))
            return "accepted", None
        self.pending.add((a, b))
        return "pending", None

    def answer(self, b: int, a: int, accept: bool) -> Optional[str]:
        """b 回应 a 的交换视野请求。"""
        if (a, b) not in self.pending:
            return "这个请求已经失效了"
        self.pending.discard((a, b))
        if accept:
            self._make_shared(a, b)
            self._emit(CommEvent(self.t, b, a, "view_accept", None, True))
        else:
            self.rejected.add((a, b))
            self._emit(CommEvent(self.t, b, a, "view_reject", None, False))
        return None

    def send_message(self, a: int, b: int, kind: str) -> Optional[str]:
        if kind not in self.s.comm_messages:
            return "老师没有开放这种消息"
        err = self._check(a, b, spend=True)
        if err:
            return err
        self._spend(a)
        m = Message(self.t, a, b, kind)
        self.outbox.setdefault(a, []).append(m)
        if self.s.comm_delivery == "now":
            self.inbox.setdefault(b, []).append(m)
        else:
            self.next_queue.append(m)
        self._emit(CommEvent(self.t, a, b, "msg", {"kind": kind, "delivery": self.s.comm_delivery}, None))
        return None

    # ------------------------------------------------------------------ 给学生载荷用
    def student_part(self, agent: int) -> dict:
        """本体能知道的通信状态：剩余预算、收发的消息、请求状态。不含任何位置。"""
        return {
            "radius": self.s.comm_radius,
            "budget_left": self.budget_left(agent),
            "view_exchange": self.view_exchange_allowed(),
            "kinds": list(self.s.comm_messages),
            "inbox": [m.public() for m in self.inbox.get(agent, [])],
            "outbox": [m.public() for m in self.outbox.get(agent, [])],
            "requests_in": sorted(a for (a, b) in self.pending if b == agent),
            "requests_out": sorted(b for (a, b) in self.pending if a == agent),
            "rejected_by": sorted(b for (a, b) in self.rejected if a == agent),
        }
