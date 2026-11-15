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
import time
from types import SimpleNamespace

from tools_turn import (
    _EMPTY_BODY_ERROR,
    _MISPLACED_ERROR,
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

    async def note_tag_rejected(
        self, sid, grant, move, error, *, reason: str = "invalid"
    ) -> None:
        # Same uniform signature as the real director (no legacy fallback).
        self.rejected.append((sid, move, error, reason))


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
    assert director.calls == []
    assert director.notes == []
    assert director.rejected == []
    assert event.stopped is False
    assert runtime.lines == []


async def test_empty_body_tag_is_rejected_as_a_format_error():
    """Attribute-form / empty-body tags are never silent: an explicit format
    rejection fires and the trail is recorded on the grant.

    Core ``parse_xml`` parses ``<play move="pass"/>`` into a RootTagAction
    with an empty body — the old code returned None and the move vanished.
    Now the submission path reports tool progress (standing the text nudge
    down) and routes an explicit bounded format rejection.
    """
    director = _Director()
    grant = _Grant()
    plugin = _plugin(director)
    actions = [_TagAction(play_tool_name("m-tag"), "   ")]
    assert await submit_play_from_actions(
        plugin, director, grant, actions, "s1", _Event()
    ) is False
    assert director.calls == []  # nothing was submitted
    assert director.notes == ["s1"]  # the parsed tag stands the nudge down
    assert director.rejected == [("s1", "格式错误", _EMPTY_BODY_ERROR, "format")]


async def test_nested_tag_attempt_is_rejected_with_format_feedback():
    """A tag nested inside <msg> (core no-op swallow) is recorded at parse
    time and rejected explicitly by the submission path.

    Core calls ``PlayTurnTag.handle`` for a tag nested inside ``<msg>`` and
    produces no RootTagAction — the old path saw neither a submission nor a
    failure, while ``note_response`` saw the substring and suppressed the
    correction. Now the parse-level marker makes the AFTER_XML_PARSE path
    reject explicitly.
    """
    director = _Director()
    grant = _Grant()
    plugin = _plugin(director)
    tag = build_play_tag(plugin, grant)
    # The model nested the tag inside <msg>: core invokes handle() as a no-op.
    await tag.handle("pass")
    assert await submit_play_from_actions(
        plugin, director, grant, [_Chain("我要叫分")], "s1", _Event()
    ) is False
    assert director.calls == []
    assert director.notes == ["s1"]
    assert director.rejected == [("s1", "格式错误", _MISPLACED_ERROR, "format")]
    # The marker is consumed: a follow-up tag-less reply is an ordinary
    # text reply (F1 territory), not a second format rejection.
    assert await submit_play_from_actions(
        plugin, director, grant, [_Chain("还想说一句")], "s1", _Event()
    ) is None
    assert len(director.rejected) == 1


async def test_nested_empty_body_tag_still_earns_format_feedback():
    """A nested tag with an EMPTY body is still an attempt: the marker is
    keyed on the timestamp, so the explicit format rejection fires instead
    of the generic F1 text nudge mislabeling the mistake as "only talked"."""
    director = _Director()
    grant = _Grant()
    plugin = _plugin(director)
    tag = build_play_tag(plugin, grant)
    await tag.handle("")  # <play_…></play_…> nested inside <msg>
    # actions=[] is the realistic shape: no MessageChain, no RootTagAction.
    assert await submit_play_from_actions(
        plugin, director, grant, [], "s1", _Event()
    ) is False
    assert director.calls == []
    assert director.notes == ["s1"]
    assert director.rejected == [("s1", "格式错误", _MISPLACED_ERROR, "format")]
    # The marker is consumed either way.
    assert await submit_play_from_actions(
        plugin, director, grant, [], "s1", _Event()
    ) is None


async def test_stale_misplaced_marker_does_not_misfire():
    """A stale marker left over from a previous reply (e.g. a root-level
    dispatch writing back) must not reject the next, tag-less reply."""
    director = _Director()
    grant = _Grant()
    plugin = _plugin(director)
    grant.tag_misplaced = "pass"
    grant.tag_misplaced_at = time.monotonic() - 3600.0
    assert await submit_play_from_actions(
        plugin, director, grant, [_Chain("普通文字")], "s1", _Event()
    ) is None
    assert director.rejected == []


async def test_successful_submission_clears_rejection_trail():
    director = _Director()
    grant = _Grant()
    grant.last_rejection = ("ZZ99", "这里已经有子")
    grant.tag_misplaced = "H8"
    grant.tag_misplaced_at = time.monotonic()
    actions = [_TagAction(play_tool_name("m-tag"), "K9")]
    assert await submit_play_from_actions(
        _plugin(director), director, grant, actions, "s1", _Event()
    ) is True
    assert grant.last_rejection is None
    assert grant.tag_misplaced == ""


async def test_invalid_result_asks_for_a_bounded_retry_and_keeps_delivery():
    director = _Director(result=_Result(ok=False, status="invalid", error="这里已经有子"))
    runtime = _Runtime()
    event = _Event()
    actions = [_TagAction(play_tool_name("m-tag"), "K9")]
    assert await submit_play_from_actions(
        _plugin(director, runtime=runtime), director, _Grant(), actions, "s1", event
    ) is False
    assert director.rejected == [("s1", "K9", "这里已经有子", "invalid")]
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
async def test_rejected_tag_notice_is_budgeted_and_records_the_details():
    """The rejection notice carries status + pointer only; the specifics are
    recorded on the grant and rendered by the context block."""
    rooms, _plugin, recorder, director = _harness_bound(
        turn_play_timeout_s=30.0, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)

    await director.note_tag_rejected(grant.session_id, grant, "ZZ99", "这里已经有子")
    assert grant.tag_retries == 1
    assert grant.last_rejection == ("ZZ99", "这里已经有子")
    notice = next(text for _sid, text in recorder.sent if "没有生效" in text)
    # Short status + pointer; the move/error live in the context block.
    assert "ZZ99" not in notice
    assert "这里已经有子" not in notice
    assert "gamehub_context" in notice
    assert f"state_{room.match.id}" in notice
    # Budget: turn_text_retry_max (default 2) — beyond it the notice is
    # silent but the context trail keeps updating.
    await director.note_tag_rejected(grant.session_id, grant, "YY88", "x")
    await director.note_tag_rejected(grant.session_id, grant, "XX77", "新错误")
    assert grant.tag_retries == 2
    assert grant.last_rejection == ("XX77", "新错误")
    assert len([t for _s, t in recorder.sent if "没有生效" in t]) == 2
    await director.stop()


async def test_note_response_suppression_is_parse_level_not_substring():
    """The substring no longer suppresses the corrective: only a really
    parsed tag (reported as tool progress) means "submitted".

    Incident: the model wrote ``<play_x>…</play_x>`` inside ``<msg>`` (a
    core-level no-op swallow) or in attribute form (empty body) — the reply
    contained the tag substring without ever submitting, the old
    ``note_response`` substring check suppressed the correction, and the
    turn idled silently to the watchdog fill. Now the substring arms (it
    proves nothing); the submission path reports tool progress when a tag
    was really parsed, standing the corrective down.
    """
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
    # (a) A reply that merely CONTAINS the tag substring arms the corrective:
    # the substring proves nothing (the tag may have been swallowed).
    director.note_llm_request(sid)
    director.note_response(sid, f"想堵你这里。<{name}>J9</{name}>")
    await _wait(lambda: grant.text_task is not None and grant.text_task.done())
    assert grant.text_nudges == 1

    # (b) A fresh reply whose tag was really parsed and submitted stands the
    # nudge down through tool progress, never arming a corrective.
    grant.text_task = None
    director.note_llm_request(sid)
    director.note_response(sid)
    director.note_tool_progress(sid)  # what submit_play_from_actions reports
    await asyncio.sleep(0.3)
    assert grant.text_nudges == 1  # unchanged: the second one never armed

    await director.stop()


async def test_note_response_clears_the_misplaced_marker_each_step():
    """Every new step clears the previous reply's leftover misplaced marker."""
    rooms, _plugin, _recorder, director = _harness_bound(
        turn_play_timeout_s=30.0, turn_fallback_delay_s=0.05,
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    sid = grant.session_id

    grant.tag_misplaced = "pass"
    grant.tag_misplaced_at = time.monotonic()
    director.note_response(sid)
    assert grant.tag_misplaced == ""
    assert grant.tag_misplaced_at == 0.0
    await director.stop()
