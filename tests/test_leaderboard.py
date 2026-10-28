"""排行榜：字典序 / 单指标排序，并列同名次，参照行插在对应位置但不占名次。"""
from types import SimpleNamespace

import numpy as np

from classroom.core.leaderboard import ranking


def fake_round(g, metrics, status="finished"):
    game = SimpleNamespace(t=10, at_goal=lambda: np.array([True, False]), metrics=lambda: metrics)
    return SimpleNamespace(group_id=g, group_name=f"第 {g + 1} 组", status=status, metrics=metrics, game=game, num_agents=2)


M = lambda csr, isr, soc, mk: {"CSR": csr, "ISR": isr, "SoC": soc, "makespan": mk}  # noqa: E731


def test_lex_order_and_ties():
    rs = [fake_round(0, M(0, 0.5, 200, 128)), fake_round(1, M(1, 1, 90, 40)),
          fake_round(2, M(1, 1, 80, 50)), fake_round(3, M(1, 1, 80, 50))]
    rows = ranking(rs)
    assert [(r["group"], r["rank"]) for r in rows] == [(2, 1), (3, 1), (1, 3), (0, 4)]


def test_single_metric():
    rs = [fake_round(0, M(1, 1, 90, 30)), fake_round(1, M(1, 1, 80, 50))]
    assert [r["group"] for r in ranking(rs, rank_by="makespan")] == [0, 1]
    assert [r["group"] for r in ranking(rs, rank_by="SoC")] == [1, 0]


def test_references_inserted_without_rank():
    rs = [fake_round(0, M(1, 1, 90, 40)), fake_round(1, M(0, 0.5, 200, 128))]
    refs = {"expert": {"status": "done", "metrics": M(1, 1, 60, 30)},
            "ai": {"status": "done", "metrics": M(1, 1, 100, 45)}}
    rows = ranking(rs, refs)
    assert [(r["kind"], r["rank"]) for r in rows] == [("expert", None), ("group", 1), ("ai", None), ("group", 2)]


def test_pending_reference_at_end_and_provisional():
    rs = [fake_round(0, M(0, 0.5, 200, 128), status="playing")]
    rows = ranking(rs, {"ai": {"status": "computing"}})
    assert rows[0]["provisional"] is True and rows[-1]["kind"] == "ai" and rows[-1]["metrics"] is None
