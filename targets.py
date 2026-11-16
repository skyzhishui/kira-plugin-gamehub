"""Notification target resolution (platform / session type / web user id).

KiraAI session grammar is ``adapter:dm|gm:session_id``:

- ``platform_of_session_id`` returns the first segment (the adapter name);
  ``webchat`` is treated as the web platform.
- ``session_type_of`` maps ``dm`` -> ``private`` and ``gm`` -> ``group``.
- ``is_private_session`` is the strict private-chat test used by the tool
  entry points (web / ``dm`` sessions with a parseable session id).
- ``webuserid_of_session_id`` / ``validate_web_session_id`` only accept
  ``webchat:dm:<userid>`` and return the third segment.

Pure functions: no database access, no adapter lookups. Production importers
are ``talk`` (tier routing), ``tools`` (private-link + session-type tests) and
``host.web`` (platform label); not every helper here has a caller.
"""

from __future__ import annotations

import re

_WEB_ADAPTER = "webchat"
_WEB_PLATFORM = "web"
_WEB_SESSION_MAX_LEN = 64
_WEB_USERID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

_TYPE_TO_SESSION_TYPE = {"dm": "private", "gm": "group"}


def _parts_of(session_id: str) -> list[str]:
    sid = str(session_id or "").strip()
    return sid.split(":") if sid else []


def platform_of_session_id(session_id: str) -> str:
    """session_id -> platform routing key (adapter ``user_info.platform``).

    ``adapter:dm|gm:...`` -> first segment; ``webchat`` -> ``web``.
    Empty / malformed ids return ``""``.
    """
    parts = _parts_of(session_id)
    if not parts or not parts[0]:
        return ""
    head = parts[0].strip()
    return _WEB_PLATFORM if head == _WEB_ADAPTER else head


def is_web_session(session_id: str) -> bool:
    parts = _parts_of(session_id)
    return bool(parts) and parts[0].strip() == _WEB_ADAPTER


def session_type_of(session_id: str) -> str:
    """session_id -> ``private`` / ``group`` (lenient, never raises).

    Unknown shapes fall back to ``private``.
    """
    parts = _parts_of(session_id)
    if len(parts) >= 2:
        mapped = _TYPE_TO_SESSION_TYPE.get(parts[1].strip().lower())
        if mapped:
            return mapped
    return "private"


def _web_userid_parts(session_id: str) -> tuple[bool, str]:
    """Return ``(is_webchat_dm, userid)``; userid empty when not parseable."""
    parts = _parts_of(session_id)
    if len(parts) != 3:
        return False, ""
    adapter, kind, userid = (part.strip() for part in parts)
    if adapter != _WEB_ADAPTER or kind != "dm":
        return False, ""
    return True, userid


def webuserid_of_session_id(session_id: str) -> str:
    """Lenient webuserid extraction; non-web / unparseable -> ``""``.

    **Display/compat only**, no validity checks; use
    :func:`validate_web_session_id` when deciding whether a web target is
    actually writable.
    """
    is_web_dm, userid = _web_userid_parts(session_id)
    return userid if is_web_dm else ""


def validate_web_session_id(session_id: str) -> tuple[str, str]:
    """Validate a web session id; return ``(webuserid, error)``."""
    sid = str(session_id or "").strip()
    if not sid:
        return "", "web 会话不能为空"
    is_web_dm, userid = _web_userid_parts(sid)
    if not is_web_dm:
        return "", "session 必须为 web 渠道格式（webchat:dm:<userid>）"
    if not _WEB_USERID_RE.match(userid):
        return (
            "",
            "session 中 webuserid 非法"
            f"（须匹配 ^[A-Za-z0-9_-]{{1,32}}$）: {userid!r}",
        )
    if len(sid) > _WEB_SESSION_MAX_LEN:
        return "", f"session 总长超限（{len(sid)} > {_WEB_SESSION_MAX_LEN}）: {sid!r}"
    return userid, ""


def is_private_session(session_id: str) -> bool:
    """Private-chat test: web sessions and ``dm`` sessions."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    if session_type_of(sid) == "group":
        return False
    return ":" in sid


def target_label(session_id: str, name: str = "", platform: str | None = None) -> str:
    """Config dropdown label: name + platform + id (no chat content)."""
    sid = str(session_id or "").strip()
    platform_text = str(platform or platform_of_session_id(sid) or "未知")
    name_text = str(name or "").strip() or "未命名"
    return f"{name_text}（平台：{platform_text} · ID：{sid}）"
