"""Turn play tool: schema, say truncation, success/failure semantics.

The ``play_<match>`` tool is the only real action of a notice turn. It takes
``move`` plus an optional short ``say`` (truncated to ``settings.max_say_len``),
reports success only from an explicit ok/status, and stops the reply after a
successful move so the turn does not post a duplicate message.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools_turn import PlayTool, build_turn_context, play_tool_name


class _Grant:
    match_id = "m-action"
    key = "grant-key"
    seat = 1
    kind = "nori_turn"
    move_count = 0


class _Event:
    def __init__(self, grant: _Grant = None, *, extra: dict | None = None) -> None:
        self.sid = "webchat:dm:u1"
        binding = build_turn_context(grant or _Grant())
        self.extra = {"gamehub_turn": binding} if extra is None else extra
        self.message = SimpleNamespace(is_notice=True)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _plugin(submit=None, *, max_say_len: int = 280, director: bool = True):
    director_obj = None
    if director:
        director_obj = SimpleNamespace(submit_play=submit) if submit else SimpleNamespace()
    return SimpleNamespace(
        rooms=None,
        settings=SimpleNamespace(max_say_len=max_say_len),
        _director=director_obj,
    )


def _play(plugin, grant: _Grant = None, *, match_kind: str = "reversi") -> PlayTool:
    return PlayTool(plugin, grant or _Grant(), match_kind=match_kind)


# ----------------------------------------------------------------------
#  Schema
# ----------------------------------------------------------------------
def test_play_schema_has_move_and_optional_say_only() -> None:
    tool = _play(_plugin(director=False))
    properties = tool.parameters["properties"]
    assert set(properties) == {"move", "say"}
    assert tool.parameters["required"] == ["move"]
    assert tool.parameters["additionalProperties"] is False
    assert tool.name == play_tool_name("m-action")
    description = str(tool.description)
    assert "在当前对局执行一步" in description
    assert "可配合文本输出一起使用" in description
    for token in (
        "同一次回复里写一两句",
        "不是工具参数",
        "submit_reply",
        "本局对手名",
        "称呼对手",
        "人设",
    ):
        assert token not in description
    assert "AI" not in str(tool.parameters)
    # The move format of the bound game is carried into the schema.
    assert "D6" in properties["move"]["description"]


async def test_hallucinated_say_kwargs_do_not_break_the_move() -> None:
    calls: list[tuple] = []

    async def submit(match_id, key, move, say=None):
        calls.append((match_id, key, move, say))
        return "已落子 H8。本回合完成。", True

    tool = _play(_plugin(submit))
    event = _Event()
    text = await tool.execute(event, move="H8", unrelated="ignored")
    assert "本次行动授权已完成" in text
    assert calls == [("m-action", "grant-key", "H8", None)]
    assert event.stopped is True


# ----------------------------------------------------------------------
#  say handling
# ----------------------------------------------------------------------
async def test_say_is_truncated_to_configured_limit() -> None:
    calls: list[tuple] = []

    async def submit(match_id, key, move, say=None):
        calls.append((match_id, key, move, say))
        return "已落子 H8。本回合完成。", True

    tool = _play(_plugin(submit, max_say_len=4))
    await tool.execute(_Event(), move="H8", say="这是一句很长的话")
    assert calls[0][3] == "这是一句"


async def test_empty_say_is_not_forwarded() -> None:
    calls: list[tuple] = []

    async def submit(match_id, key, move, say=None):
        calls.append((match_id, key, move, say))
        return "已落子 H8。本回合完成。", True

    tool = _play(_plugin(submit, max_say_len=0))
    await tool.execute(_Event(), move="H8", say="   ")
    assert calls == [("m-action", "grant-key", "H8", None)]


# ----------------------------------------------------------------------
#  Result mapping
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_typed_success_confirms_the_grant_is_done() -> None:
    class _Result:
        ok = True
        status = "played"
        text = "已落子 H8。"
        error = ""
        move = "H8"

    async def submit(match_id, key, move, say=None):
        return _Result()

    event = _Event()
    text = await _play(_plugin(submit)).execute(event, move="H8")
    assert text == (
        "已落子 H8。本次行动授权已完成，请勿重复调用本局落子或状态工具。"
    )
    assert event.stopped is True


@pytest.mark.asyncio
async def test_typed_failure_is_never_reported_as_success() -> None:
    class _Result:
        ok = False
        status = "stale"
        text = "局面已经变化，本手作废。"
        error = ""
        move = ""

    async def submit(match_id, key, move, say=None):
        return _Result()

    event = _Event()
    text = await _play(_plugin(submit)).execute(event, move="H8")
    assert "本手作废" in text
    assert "授权已完成" not in text
    assert event.stopped is False


@pytest.mark.asyncio
async def test_legacy_tuple_non_coordinate_success_still_recognized() -> None:
    async def submit(match_id, key, move, say=None):
        return "已执行动作 抽一张。本回合完成。", True

    text = await _play(_plugin(submit)).execute(_Event(), move="抽一张")
    assert "已执行动作 抽一张" in text
    assert "请勿重复调用" in text


@pytest.mark.asyncio
async def test_legacy_tuple_failure_is_not_terminal() -> None:
    async def submit(match_id, key, move, say=None):
        return "落子 Z99 失败：看不懂这步。", False

    event = _Event()
    text = await _play(_plugin(submit)).execute(event, move="Z99")
    assert "Z99" in text
    assert "授权已完成" not in text
    assert event.stopped is False


@pytest.mark.asyncio
async def test_missing_director_reports_no_grant_instead_of_lying() -> None:
    tool = _play(_plugin(director=False))
    tool._plugin.rooms = None
    event = _Event(extra={})
    text = await tool.execute(event, move="H8")
    assert "授权已完成" not in text
    assert "对局" in text  # no room to apply the move to
    assert event.stopped is False


@pytest.mark.asyncio
async def test_empty_move_is_refused_before_touching_the_director() -> None:
    calls: list[tuple] = []

    async def submit(*args, **kwargs):
        calls.append(args)
        return "ok", True

    event = _Event()
    text = await _play(_plugin(submit)).execute(event, move="   ")
    assert "没有写这步棋" in text
    assert calls == []
    assert event.stopped is False