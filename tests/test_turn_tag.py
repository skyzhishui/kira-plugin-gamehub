"""Tag-channel move submission (``play_<match>`` root control tag).

The move no longer rides a native tool: glm-tier models imitate whatever
structured emission already sits in the session history, and one text-form
``<tool_call>`` reply poisons the native channel for the whole session (the
``<msg>`` envelope's escape conventions are the imitation target). The tag
travels in the reply text itself — there is no second channel to slip into —
and is submitted during ``AFTER_XML_PARSE``, before delivery, with the
reply's ``<msg>`` texts riding as the atomic ``say``.

Covers: the tag surface (name scoping, parent, description, no-op handle),
``submit_play_from_actions`` (extraction, say truncation/persistence,
invalid-move retry notice wiring), and the director's tag-aware corrective
arming (``note_response`` content check).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tools_turn import (
    PlayTurnTag,
    build_play_tag,
    build_turn_tools,
    play_tool_name,
    state_tool_name,
    submit_play_from_actions,
)

from test_turn_director import (  # noqa: F401  (_patch_notices is a fixture)
    NORI,
    _create,
    _harness_bound,
    _patch_notices,
    _wait,
)


class _Grant:
    def __init__(self, match_id: str = "m-tag", key: str = "grant-key", seat: int = 1):
        self.match_id = match_id
        self.key = key
        self.seat = seat
        self.kind = "nori_turn"
        self.move_count = 0
        self.state = "active"


class _Result:
    def __init__(self, ok: bool = True, status: str = "played", error: str = ""):
        self.ok = ok
        self.status = status
        self.error = error
        self.move = "H8"
        self.text = "已落子 H8。" if ok else "落子失败。"


class _Director:
    def __init__(self, result: _Result | None = None) -> None:
        self.result = result or _Result()
        self.calls: list[tuple] = []
        self.notes: list[str] = []
        self.rejected: list[tuple] = []

    async def submit_play(self, match_id, key, move, say=None, *, persist_say=False):
        self.calls.append((match_id, key, move, say, persist_say))
        return self.result

    def note_tool_progress(self, sid) -> None:
        self.notes.append(sid)

    async def note_tag_rejected(self, sid, grant, move, error) -> None:
        self.rejected.append((sid, move, error))


class _Runtime:
    """Duck-typed ``plugin._runtime`` capturing window-line writes."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    async def record_agent_line(self, sid: str, text: str) -> None:
        self.lines.append((sid, text))


class _Event:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _Element:
    def __init__(self, text: str) -> None:
        self.text = text


class _Chain:
    def __init__(self, *texts: str) -> None:
        self.message_list = [_Element(t) for t in texts]


class _TagAction:
    def __init__(self, name: str, value: str) -> None:
        self.tag = SimpleNamespace(name=name)
        self.value = value


def _plugin(
    director: _Director, *, max_say_len: int = 280, runtime: _Runtime | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(max_say_len=max_say_len),
        _runtime=runtime if runtime is not None else _Runtime(),
    )


# ----------------------------------------------------------------------
#  Tag surface
# ----------------------------------------------------------------------
def test_play_tag_name_is_match_scoped():
    a = build_play_tag(_plugin(_Director()), _Grant("aaaa1111"))
    b = build_play_tag(_plugin(_Director()), _Grant("bbbb2222"))
    assert a.name == "play_aaaa1111"
    assert b.name == "play_bbbb2222"
    assert a.name != b.name


def test_play_tag_is_a_root_control_tag_with_format_hint():
    tag = build_play_tag(_plugin(_Director()), _Grant(), match_kind="reversi")
    assert tag.parent is None  # root level: outside <msg>
    assert "落子控制标签" in tag.description
    assert "与<msg>同级" in tag.description
    assert "D6" in tag.description  # the bound game's move format rides along
    # The description teaches format only — never persona or behaviour.
    for token in ("人设", "汐祈", "本局对手名", "称呼对手", "同一次回复里写一两句"):
        assert token not in tag.description


def test_play_tag_without_match_kind_degrades_to_generic_format():
    tag = build_play_tag(_plugin(_Director()), _Grant(), match_kind="")
    assert "格式以本局规则为准" in tag.description
    assert "D6" not in tag.description


async def test_play_tag_handle_is_an_async_noop():
    """Submission happens in AFTER_XML_PARSE; the element never becomes text.

    ``handle`` must be awaitable: the core walks the parsed actions with
    ``await action.tag.handle(...)``.
    """
    tag = PlayTurnTag(_plugin(_Director()), _Grant())
    assert await tag.handle("H8") is None
    assert await tag.handle("H8", say="extra") is None


def test_turn_tools_are_state_only_now():
    """The native play tool is gone; only the read view stays a tool."""
    grant = _Grant()
    tools = build_turn_tools(_plugin(_Director()), grant)
    assert [t.name for t in tools] == [state_tool_name(grant.match_id)]


# ----------------------------------------------------------------------
#  submit_play_from_actions
# ----------------------------------------------------------------------
async def test_submit_extracts_tag_rides_say_stops_and_records():
    director = _Director()
    runtime = _Runtime()
    event = _Event()
    grant = _Grant()
    actions = [_Chain("堵你这里喵～"), _TagAction(play_tool_name("m-tag"), "K9")]
    assert await submit_play_from_actions(
        _plugin(director, runtime=runtime), director, grant, actions, "s1", event
    ) is True
    assert director.calls == [("m-tag", "grant-key", "K9", "堵你这里喵～", True)]
    assert director.notes == ["s1"]  # stands down a pending corrective nudge
    assert event.stopped is True  # A plan: the delivery tail is skipped
    assert runtime.lines == [("s1", "堵你这里喵～")]  # window line via read model


async def test_say_is_truncated_and_persisted_with_the_move():
    director = _Director()
    runtime = _Runtime()
    actions = [_Chain("这是一句很长的话"), _TagAction(play_tool_name("m-tag"), "K9")]
    await submit_play_from_actions(
        _plugin(director, max_say_len=4, runtime=runtime),
        director, _Grant(), actions, "s1",
    )
    assert director.calls[0][3] == "这是一句"
    assert director.calls[0][4] is True
    assert runtime.lines == [("s1", "这是一句")]


async def test_no_table_talk_means_no_say_and_no_window_line():
    director = _Director()
    runtime = _Runtime()
    event = _Event()
    actions = [_TagAction(play_tool_name("m-tag"), "K9")]
    assert await submit_play_from_actions(
        _plugin(director, runtime=runtime), director, _Grant(), actions, "s1", event
    ) is True
    assert director.calls == [("m-tag", "grant-key", "K9", None, False)]
    assert event.stopped is True
    assert runtime.lines == []


async def test_missing_or_foreign_tag_is_a_noop():
    director = _Director()
    runtime = _Runtime()
    event = _Event()
    grant = _Grant()
    plugin = _plugin(director, runtime=runtime)
    assert await submit_play_from_actions(
        plugin, director, grant, [_Chain("你好")], "s1", event
    ) is None
    assert await submit_play_from_actions(
        plugin, director, grant, [_TagAction("play_other", "H8")], "s1", event
    ) is None
    assert await submit_play_from_actions(
        plugin, director, grant, [_TagAction(play_tool_name("m-tag"), "  ")], "s1", event
    ) is None
    assert director.calls == []
    assert director.notes == []
    assert event.stopped is False
    assert runtime.lines == []


async def test_invalid_result_asks_for_a_bounded_retry_and_keeps_delivery():
    director = _Director(result=_Result(ok=False, status="invalid", error="这里已经有子"))
    runtime = _Runtime()
    event = _Event()
    actions = [_TagAction(play_tool_name("m-tag"), "K9")]
    assert await submit_play_from_actions(
        _plugin(director, runtime=runtime), director, _Grant(), actions, "s1", event
    ) is False
    assert director.rejected == [("s1", "K9", "这里已经有子")]
    assert event.stopped is False  # a rejected tag never terminates the reply
    assert runtime.lines == []


async def test_runtimeless_plugin_still_stops():
    """The window-line write is cosmetic: its absence must not break the stop."""
    director = _Director()
    event = _Event()
    plugin = SimpleNamespace(settings=SimpleNamespace(max_say_len=280))
    actions = [_Chain("堵你这里喵～"), _TagAction(play_tool_name("m-tag"), "K9")]
    assert await submit_play_from_actions(
        plugin, director, _Grant(), actions, "s1", event
    ) is True
    assert event.stopped is True


async def test_terminal_failure_does_not_ask_for_retry():
    director = _Director(result=_Result(ok=False, status="stale"))
    actions = [_TagAction(play_tool_name("m-tag"), "K9")]
    await submit_play_from_actions(_plugin(director), director, _Grant(), actions, "s1")
    assert director.rejected == []


# ----------------------------------------------------------------------
#  Director integration (real harness)
# ----------------------------------------------------------------------
async def test_rejected_tag_notice_is_budgeted():
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=30.0, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    await director.note_tag_rejected(grant.session_id, grant, "ZZ99", "这里已经有子")
    assert grant.tag_retries == 1
    assert any("ZZ99" in text for _sid, text in recorder.sent)
    # Budget: turn_text_retry_max (default 2) — beyond it the notice is silent.
    await director.note_tag_rejected(grant.session_id, grant, "YY88", "x")
    await director.note_tag_rejected(grant.session_id, grant, "XX77", "x")
    assert grant.tag_retries == 2
    await director.stop()


async def test_note_response_tag_content_skips_the_corrective():
    """A reply that carries the play tag never arms the text-only nudge."""
    rooms, _plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=30.0, turn_fallback_delay_s=0.05,
        turn_text_grace_s=0.05, turn_text_retry_s=0.5,
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    sid = grant.session_id
    director.tools_for(sid)

    name = play_tool_name(room.match.id)
    director.note_llm_request(sid)
    director.note_response(sid, f"想堵你这里。<{name}>J9</{name}>")
    await asyncio.sleep(0.3)
    assert grant.text_nudges == 0
    assert grant.text_task is None

    # A genuinely text-only reply still arms the corrective.
    director.note_llm_request(sid)
    director.note_response(sid, "J9，堵你。")
    await _wait(lambda: grant.text_task is not None and grant.text_task.done())
    assert grant.text_nudges == 1
    await director.stop()
