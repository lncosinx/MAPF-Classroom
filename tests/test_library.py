"""地图库与模板：老师保存的图 / 模板读写删，路径安全，内置项不可删；内置课例都能直接开局。"""
import asyncio

import pytest

from classroom.core.round import GroupRound
from classroom.core.settings import RoundSettings
from classroom.maps import library
from classroom.maps.mapio import MapSpec


@pytest.fixture()
def user_dir(tmp_path):
    old = library.user_dir()
    library.set_user_dir(tmp_path / "maps")
    yield tmp_path / "maps"
    library.set_user_dir(old)


SPEC = MapSpec(title="我的图", map=["....", ".#..", "...."], agents_xy=[(0, 0)], targets_xy=[(2, 3)])


def test_save_list_load_delete(user_dir):
    name = library.save_user_map(SPEC)
    assert name.startswith("my:")
    entry = next(m for m in library.list_maps() if m["name"] == name)
    assert entry["title"] == "我的图" and not entry["builtin"] and (entry["H"], entry["W"], entry["n"]) == (3, 4, 1)
    assert library.load_user_map(name) == SPEC
    p = library.get_preset(name)
    assert p.agents_xy.tolist() == [[0, 0]] and p.map_str == SPEC.map_str
    library.delete_user_map(name)
    assert all(m["name"] != name for m in library.list_maps())


@pytest.mark.parametrize("bad", ["my:../../etc/passwd", "my:..", "my:A B", "my:x/y", "my:"])
def test_no_path_traversal(user_dir, bad):
    with pytest.raises(KeyError):
        library.load_user_map(bad)
    with pytest.raises(KeyError):
        library.delete_user_map(bad)


def test_builtin_cannot_be_deleted(user_dir):
    with pytest.raises(KeyError):
        library.delete_user_map("four-rooms")
    with pytest.raises(KeyError):
        library.delete_user_template("lesson:1")


def test_user_templates(user_dir):
    s = RoundSettings(preset="doorway", comm_budget=1).model_dump()
    name = library.save_user_template("我的模板", s, note="备注")
    t = next(t for t in library.list_templates() if t["name"] == name)
    assert t["title"] == "我的模板" and not t["builtin"] and RoundSettings(**t["settings"]).comm_budget == 1
    library.delete_user_template(name)
    assert all(t["name"] != name for t in library.list_templates())


class Sink:
    async def send_agent(self, a, m): pass
    async def send_teachers(self, m): pass
    async def send_player(self, p, m): pass
    async def kick(self, p): pass


@pytest.mark.parametrize("tpl", [t["name"] for t in library.list_templates() if t["builtin"]])
def test_builtin_lessons_start(tpl):
    """每个内置课例：规则合法、地图校验通过、能开局走一步。"""
    t = next(x for x in library.list_templates() if x["name"] == tpl)
    s = RoundSettings(**{**RoundSettings().model_dump(), **t["settings"]})

    async def go():
        r = GroupRound(0, s, Sink())
        await r.start()
        await r.resolve(timed_out=True)
        assert r.game.t == 1
        await r.end()
    asyncio.run(go())


def test_custom_map_round_and_empty_map_rejected():
    async def go():
        r = GroupRound(0, RoundSettings(custom_map=SPEC), Sink())
        assert r.num_agents == 1 and r.preset.title == "我的图"
        await r.start(); await r.end()
    asyncio.run(go())
    with pytest.raises(ValueError):
        GroupRound(0, RoundSettings(custom_map=MapSpec(map=["...."])), Sink())
    with pytest.raises(ValueError):    # 起点压在墙上
        GroupRound(0, RoundSettings(custom_map=MapSpec(map=["#."], agents_xy=[(0, 0)], targets_xy=[(0, 1)])), Sink())
