"""Notice-turn model routing (``host/notice_model.py`` + director registration).

Two layers are covered:

- the hook gate: a batch is routed to ``notice_main_model`` only when every
  message is one of this plugin's registered turn notices (``is_notice`` AND
  byte-exact text), and nothing else in the batch is overridden. The batch
  event's own ``message_str`` is never populated by the IM pipeline, so the
  gate must read each message's value;
- the registration contract: the four grant-bound send sites register the
  text only after a successful send, resolved grants drop out, and several
  grants sharing one session aggregate.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

import director as director_module
from director import TurnDirector
from host import notice_model
from room import RoomManager

from test_talk import _settings
from test_turn_director import NORI, _Recorder, _Repo, _create, _wait

NOTICE_MODEL = "p:notice-model"

apply_notice_model = notice_model.apply


# ----------------------------------------------------------------------
#  Hook gate (fake director + fake batch)
# ----------------------------------------------------------------------
class _FakeDirector:
    def __init__(self) -> None:
        self.by_sid: dict[str, set[str]] = {}
        self.queried: list[str] = []

    def notice_texts_for(self, sid: str) -> set[str]:
        self.queried.append(sid)
        return set(self.by_sid.get(sid, ()))


class _FakeMessage:
    def __init__(self, text: str, *, is_notice: bool = True) -> None:
        self.message_str = text
        self.is_notice = is_notice


class _FakeBatch:
    def __init__(
        self,
        sid: str = "s1",
        messages: list[_FakeMessage] | None = None,
        model_group: list[Any] | None = None,
    ) -> None:
        self.sid = sid
        self.messages = list(messages or ())
        self.model_group = [] if model_group is None else model_group
        # The real batch event declares this ``init=False`` and the IM
        # pipeline never assigns it; the gate must not rely on it.
        self.message_str: str | None = None


class _FakeCtx:
    def __init__(self, client: Any = NOTICE_MODEL) -> None:
        self.client = client
        self.calls: list[Any] = []

    def get_llm_client(self, model_uuid=None, llm_type=None):
        self.calls.append(model_uuid)
        return self.client


def _plugin(
    *,
    configured: str = NOTICE_MODEL,
    texts: set[str] | None = None,
    client: Any = NOTICE_MODEL,
    sid: str = "s1",
) -> Any:
    """The fake director only knows ``sid``'s texts (mirrors the real gate)."""
    director = _FakeDirector()
    director.by_sid = {sid: set(texts or ())}
    return types.SimpleNamespace(
        settings=types.SimpleNamespace(notice_main_model=configured),
        ctx=_FakeCtx(client),
        _director=director,
    )


TURN_TEXT = "[System: 对局通知 · 1 手：对手刚落子 H8，现在轮到你。]"
NUDGE_TEXT = "[System: 你还没有提交动作。]"


@pytest.mark.parametrize(
    "messages, expected",
    [
        pytest.param([_FakeMessage(TURN_TEXT)], [NOTICE_MODEL], id="registered-notice"),
        pytest.param([_FakeMessage(TURN_TEXT), _FakeMessage(NUDGE_TEXT)], [NOTICE_MODEL],
                     id="several-registered-notices"),
        pytest.param([_FakeMessage(TURN_TEXT, is_notice=False)], [], id="copied-plain-chat"),
        pytest.param([_FakeMessage(TURN_TEXT), _FakeMessage("hi", is_notice=False)], [],
                     id="mixed-with-plain-chat"),
        pytest.param([_FakeMessage(TURN_TEXT), _FakeMessage("[System: 别的插件通知]")], [],
                     id="mixed-with-foreign-notice"),
        pytest.param([_FakeMessage("[System: 对局通知 · 未登记")], [],
                     id="foreign-notice-prefix"),
        pytest.param([], [], id="empty-batch"),
    ],
)
def test_gate_routes_only_all_registered_notices(messages, expected) -> None:
    plugin = _plugin(texts={TURN_TEXT, NUDGE_TEXT})
    event = _FakeBatch(messages=messages)
    assert event.message_str is None  # batch-level value is never populated

    apply_notice_model(plugin, event)

    assert event.model_group == expected


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"configured": ""}, id="empty-config"),
        pytest.param({"configured": "   "}, id="blank-config"),
        pytest.param({"texts": set()}, id="no-active-grant-texts"),
    ],
)
def test_gate_never_overrides_when_ineligible(overrides) -> None:
    kwargs = {"texts": {TURN_TEXT}, **overrides}
    plugin = _plugin(**kwargs)
    event = _FakeBatch(messages=[_FakeMessage(TURN_TEXT)])

    apply_notice_model(plugin, event)

    assert event.model_group == []


def test_unresolvable_client_warns_and_keeps_default_model(monkeypatch) -> None:
    warnings: list[tuple] = []
    monkeypatch.setattr(notice_model.logger, "warning", lambda *a, **kw: warnings.append(a))
    plugin = _plugin(texts={TURN_TEXT}, client=None)
    event = _FakeBatch(messages=[_FakeMessage(TURN_TEXT)])

    apply_notice_model(plugin, event)

    assert event.model_group == []
    assert plugin.ctx.calls == [NOTICE_MODEL]  # it was attempted, not skipped
    assert warnings and "unavailable" in warnings[0][0]


def test_gate_skips_when_sid_has_no_registered_texts() -> None:
    plugin = _plugin(texts={TURN_TEXT}, sid="s1")
    event = _FakeBatch(sid="other", messages=[_FakeMessage(TURN_TEXT)])

    apply_notice_model(plugin, event)

    assert event.model_group == []
    assert plugin._director.queried == ["other"]


def test_gate_keeps_an_already_set_model_group() -> None:
    plugin = _plugin(texts={TURN_TEXT})
    event = _FakeBatch(
        messages=[_FakeMessage(TURN_TEXT)], model_group=["p:other"]
    )

    apply_notice_model(plugin, event)

    assert event.model_group == ["p:other"]
    assert plugin.ctx.calls == []  # resolution never even attempted


def test_gate_skips_absent_director() -> None:
    plugin = _plugin(texts={TURN_TEXT})
    plugin._director = None
    event = _FakeBatch(messages=[_FakeMessage(TURN_TEXT)])

    apply_notice_model(plugin, event)

    assert event.model_group == []


# ----------------------------------------------------------------------
#  Director registration (real director, patched notice channel)
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patch_notices(monkeypatch: pytest.MonkeyPatch):
    async def fake_send(plugin: Any, sid: str, text: str) -> bool:
        recorder = getattr(plugin, "_recorder", None)
        if recorder is None:
            return True
        return await recorder(plugin, sid, text)

    monkeypatch.setattr(director_module, "send_notice", fake_send)


def _harness(**cfg_overrides: Any):
    settings = _settings(
        turn_play_timeout_s=cfg_overrides.pop("turn_play_timeout_s", 30.0),
        turn_fallback_delay_s=cfg_overrides.pop("turn_fallback_delay_s", 0.05),
        **cfg_overrides,
    )
    recorder = _Recorder()
    rooms = RoomManager(repository=_Repo(), config=settings)
    plugin = types.SimpleNamespace()
    plugin.settings = settings
    plugin.rooms = rooms
    plugin._rooms = rooms
    plugin.stopping = False
    plugin.target_override = ""
    plugin._recorder = recorder

    async def notice_target(room=None):
        human = next((s for s in room.match.seats if s.kind == "human"), None)
        session = str(getattr(human, "session_id", "") or "")
        if getattr(plugin, "target_override", ""):
            session = str(plugin.target_override)
        return session, bool(session)

    plugin.notice_target = notice_target
    director = TurnDirector(plugin)
    plugin._director = director
    rooms.event_hook = director.on_event
    return rooms, plugin, recorder, director


async def test_turn_notice_registers_the_delivered_text() -> None:
    rooms, _plugin, recorder, director = _harness()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))

    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(recorder.sent))

    grant = director.grant_of(room.match.id)
    assert grant is not None
    assert grant.notice_texts == {recorder.sent[-1][1]}
    assert director.notice_texts_for(grant.session_id) == {recorder.sent[-1][1]}
    await director.stop()


async def test_notice_texts_for_is_empty_while_director_is_stopping() -> None:
    rooms, _plugin, recorder, director = _harness()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(recorder.sent))
    grant = director.grant_of(room.match.id)
    assert director.notice_texts_for(grant.session_id)

    director._stopping = True

    assert director.notice_texts_for(grant.session_id) == set()
    await director.stop()


async def test_failed_turn_notice_is_not_registered() -> None:
    rooms, _plugin, _recorder, director = _harness(
        turn_play_timeout_s=0.6, turn_fallback_delay_s=0.05
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))

    original = director_module.send_notice

    async def failing(plugin, sid, text):
        return False

    director_module.send_notice = failing
    try:
        await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
        await _wait(lambda: director.grant_of(room.match.id) is not None)
        await _wait(lambda: director.grant_of(room.match.id).state == "filled")
    finally:
        director_module.send_notice = original

    assert director.grant_of(room.match.id).notice_texts == set()
    await director.stop()


async def test_text_nudge_registers_the_corrective_text() -> None:
    rooms, _plugin, recorder, director = _harness(
        turn_text_grace_s=0.02, turn_text_retry_s=0.3, turn_text_retry_max=1
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    await _wait(lambda: bool(recorder.sent))

    director.tools_for(grant.session_id)
    director.note_llm_request(grant.session_id)
    director.note_response(grant.session_id)
    await _wait(lambda: any("刚才只发了文字" in t for _s, t in recorder.sent))

    corrective = next(t for _s, t in recorder.sent if "刚才只发了文字" in t)
    assert corrective in grant.notice_texts
    await director.stop()


async def test_tag_rejection_registers_only_on_delivery() -> None:
    rooms, _plugin, recorder, director = _harness()
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: director.grant_of(room.match.id) is not None)
    grant = director.grant_of(room.match.id)
    baseline = set(grant.notice_texts)

    original = director_module.send_notice

    async def failing(plugin, sid, text):
        return False

    director_module.send_notice = failing
    try:
        await director.note_tag_rejected("s", grant, "zz", "无效动作")
    finally:
        director_module.send_notice = original
    assert grant.notice_texts == baseline

    await director.note_tag_rejected("s", grant, "zz", "无效动作")
    assert grant.notice_texts == baseline | {recorder.sent[-1][1]}
    await director.stop()


async def test_watchdog_nudge_registers_the_reminder_text() -> None:
    rooms, _plugin, recorder, director = _harness(
        turn_play_timeout_s=0.6, turn_fallback_delay_s=0.3
    )
    room, _seat, _token = await _create(rooms, mode_options=dict(NORI))
    await room.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await _wait(lambda: bool(recorder.nudges))

    grant = director.grant_of(room.match.id)
    assert recorder.nudges[-1][1] in grant.notice_texts
    await director.stop()


async def test_multiple_grants_aggregate_and_resolved_grants_drop_out() -> None:
    rooms, plugin, recorder, director = _harness()
    plugin.target_override = "shared-sid"
    room_a, _s, _t = await _create(rooms, actor="u1", mode_options=dict(NORI))
    room_b, _s, _t = await _create(rooms, actor="u2", mode_options=dict(NORI))
    await room_a.apply(0, {"type": "move", "move": "H8"}, client_version=1)
    await room_b.apply(0, {"type": "move", "move": "J9"}, client_version=1)
    await _wait(lambda: len(director._grants) >= 2)
    await _wait(lambda: len(recorder.sent) >= 2)

    grant_a = director.grant_of(room_a.match.id)
    grant_b = director.grant_of(room_b.match.id)
    assert director.notice_texts_for("shared-sid") == (
        grant_a.notice_texts | grant_b.notice_texts
    )

    result = await director.submit_play(room_a.match.id, grant_a.key, "J9")
    assert result.ok
    assert director.notice_texts_for("shared-sid") == grant_b.notice_texts
    assert director.notice_texts_for("private-u1") == set()
    await director.stop()
