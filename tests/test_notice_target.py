"""Notice target resolution: sid parsing, tier routing, override delivery.

KiraAI session grammar is ``adapter:dm|gm:session_id``. The plugin-level
``notice_target``/``web_api``/config-dropdown parts of the nori suite belong to
the runtime/WebUI hosts and are not ported here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine import NewMatchOptions, NewSeat, create_match
from host.settings import Settings
from targets import (
    is_private_session,
    is_web_session,
    platform_of_session_id,
    session_type_of,
    target_label,
    validate_web_session_id,
    webuserid_of_session_id,
)
from talk import TableTalk

GROUP_SESSION = "qq:gm:g123"
PRIVATE_SESSION = "qq:dm:u42"
LIVE2D_SESSION = "live2d:dm:l2duid"
WEB_SESSION = "webchat:dm:alice"


# ----------------------------------------------------------------------
#  Pure platform / type helpers
# ----------------------------------------------------------------------
def test_platform_and_type_helpers() -> None:
    assert platform_of_session_id(WEB_SESSION) == "web"
    assert platform_of_session_id(LIVE2D_SESSION) == "live2d"
    assert platform_of_session_id(GROUP_SESSION) == "qq"
    assert platform_of_session_id("") == ""
    assert session_type_of(GROUP_SESSION) == "group"
    assert session_type_of(PRIVATE_SESSION) == "private"
    assert session_type_of("nonsense") == "private"  # lenient fallback

    assert is_web_session(WEB_SESSION) is True
    assert is_web_session(PRIVATE_SESSION) is False
    assert webuserid_of_session_id(WEB_SESSION) == "alice"
    assert webuserid_of_session_id(LIVE2D_SESSION) == ""
    assert is_private_session(PRIVATE_SESSION) is True
    assert is_private_session(GROUP_SESSION) is False


def test_validate_web_session_matches_host_rules() -> None:
    uid, error = validate_web_session_id(WEB_SESSION)
    assert error == "" and uid == "alice"

    # non-web adapter
    _, error = validate_web_session_id(PRIVATE_SESSION)
    assert "web 渠道格式" in error

    # illegal webuserid
    _, error = validate_web_session_id("webchat:dm:bad.user")
    assert "webuserid 非法" in error

    # overly long userid is rejected by the userid rule
    _, error = validate_web_session_id(f"webchat:dm:{'u' * 40}")
    assert "webuserid 非法" in error

    _, error = validate_web_session_id("")
    assert "不能为空" in error


def test_target_label_shows_name_platform_and_id() -> None:
    label = target_label(LIVE2D_SESSION, "桌面角色")
    assert "桌面角色" in label
    assert "live2d" in label
    assert LIVE2D_SESSION in label
    assert "未命名" in target_label("", "")


# ----------------------------------------------------------------------
#  Tier routing: page / private / group (same helper for gate + delivery)
# ----------------------------------------------------------------------
class _Ctx:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.session_mgr = _SessionMgr()

    async def publish_notice(self, session, chain, is_mentioned=True):
        text = "".join(getattr(item, "text", "") for item in chain)
        self.sent.append((getattr(session, "session_id", ""), text))


class _SessionMgr:
    async def resolve_session(self, sid: str):
        return SimpleNamespace(session_id=sid)


class _Plugin:
    def __init__(self, settings, ctx, *, target):
        self.settings = settings
        self.ctx = ctx
        self._target = target
        self.stopping = False

    async def notice_target(self, room):
        return self._target


def _room(session_id: str, source: str = ""):
    match = create_match(
        NewMatchOptions(
            id=f"m-{session_id or 'page'}",
            kind="gomoku",
            seats=[
                NewSeat(
                    kind="human",
                    token="t",
                    name="玩家",
                    joined=True,
                    actor="actor",
                    session_id=session_id,
                    source=source,
                ),
                NewSeat(kind="ai", token=""),
            ],
            seed=1,
            now=1000,
        )
    )
    return SimpleNamespace(match=match, status="active", last_talk_ts=0.0)


def test_channel_tiers_route_to_their_own_defaults() -> None:
    ctx = _Ctx()
    plugin = _Plugin(
        Settings(
            {
                "page_chat_talk_default": "off",
                "table_talk_default": "moves",
                "table_talk_group_default": "key",
            }
        ),
        ctx,
        target=None,
    )
    talk = TableTalk(plugin)

    page = _room("webui")
    plugin._target = ("webchat:dm:admin", True)
    assert talk._mode_for(page.match) == "off"

    private = _room(PRIVATE_SESSION)
    plugin._target = (PRIVATE_SESSION, False)
    assert talk._mode_for(private.match) == "moves"

    group = _room(GROUP_SESSION)
    plugin._target = (GROUP_SESSION, False)
    assert talk._mode_for(group.match) == "key"


@pytest.mark.asyncio
async def test_dispatch_delivers_to_the_resolved_target_session() -> None:
    ctx = _Ctx()
    settings = Settings(
        {
            "page_chat_talk_default": "moves",
            "table_talk_default": "moves",
            "table_talk_group_default": "key",
            "talk_min_interval_s": 0.0,
            "talk_cooldown_s": 0.0,
            "table_talk_prompt_template": "[System: {event}]",
        }
    )
    talk = TableTalk(_Plugin(settings, ctx, target=(PRIVATE_SESSION, False)))
    assert await talk._dispatch(_room(PRIVATE_SESSION), {"type": "start", "seat": 0}) == "sent"
    assert ctx.sent[-1][0] == PRIVATE_SESSION

    talk = TableTalk(_Plugin(settings, ctx, target=(GROUP_SESSION, False)))
    assert await talk._dispatch(_room(GROUP_SESSION), {"type": "start", "seat": 0}) == "sent"
    assert ctx.sent[-1][0] == GROUP_SESSION


@pytest.mark.asyncio
async def test_live2d_override_routes_to_live2d_session() -> None:
    ctx = _Ctx()
    settings = Settings(
        {
            "page_chat_talk_default": "moves",
            "talk_min_interval_s": 0.0,
            "talk_cooldown_s": 0.0,
            "table_talk_prompt_template": "[System: {event}]",
        }
    )
    talk = TableTalk(_Plugin(settings, ctx, target=(LIVE2D_SESSION, True)))
    room = _room("webui")
    assert await talk._dispatch(room, {"type": "start", "seat": 0}) == "sent"
    assert ctx.sent[0][0] == LIVE2D_SESSION
    # Page source still picks the page tier, even though delivery is live2d.
    assert talk._mode_for(room.match) == "moves"


def test_disabled_page_still_allows_external_override() -> None:
    plugin = _Plugin(
        Settings(
            {
                "page_chat_enabled": False,
                "notice_target_session_id": LIVE2D_SESSION,
            }
        ),
        _Ctx(),
        target=(LIVE2D_SESSION, False),
    )
    talk = TableTalk(plugin)
    assert talk._enabled() is True

    # Without an override, page-disabled + no chat origin means no notices.
    plugin.settings = Settings({"page_chat_enabled": False, "notice_target_session_id": ""})
    assert talk._enabled() is False


def test_chat_origin_match_keeps_notices_when_page_is_disabled() -> None:
    ctx = _Ctx()

    class _ChatPlugin(_Plugin):
        def match_origin(self, match):
            return {"kind": "chat"}

    plugin = _ChatPlugin(
        Settings({"page_chat_enabled": False, "notice_target_session_id": ""}),
        ctx,
        target=(PRIVATE_SESSION, False),
    )
    talk = TableTalk(plugin)
    assert talk._enabled(_room(PRIVATE_SESSION).match) is True


def test_match_origin_webui_wins_over_session_shape() -> None:
    """Runtime ``match_origin`` marks page matches even if the sid looks chatty."""

    class _PagePlugin(_Plugin):
        def match_origin(self, match):
            return {"kind": "webui", "session_id": ""}

    plugin = _PagePlugin(
        Settings(
            {
                "page_chat_talk_default": "judge",
                "table_talk_default": "moves",
                "table_talk_group_default": "key",
            }
        ),
        _Ctx(),
        target=(PRIVATE_SESSION, True),
    )
    talk = TableTalk(plugin)
    assert talk._mode_for(_room(PRIVATE_SESSION).match) == "judge"