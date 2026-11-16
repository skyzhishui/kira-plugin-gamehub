"""Config/manifest/identity contract regressions (2026-11-16 review).

Guards the documentation-vs-code drift class of findings: schema hints that
contradict the code (P1-11), hints pointing at retired session ids (C-9),
config fields consumed nowhere (C-8), the manifest repo URL (C-40) and the
identity module's dead-implementation removal (P1-12).

These read ``schema.json`` / ``manifest.json`` (shipped contract data) and the
identity module's public surface. Behaviour behind the hints is covered
where it lives: the empty-``ai_models`` fallback in ``test_game_agent.py`` and
the ``0 / 0`` bot-delay contract in ``test_room.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _schema() -> dict:
    return json.loads((_PLUGIN_ROOT / "schema.json").read_text(encoding="utf-8"))


def test_game_agent_reasoning_effort_schema_matches_defaults():
    """The effort selector offers every level the host forwards to providers."""
    from host.settings import DEFAULTS

    field = _schema()["section_ai"]["fields"]["game_agent_reasoning_effort"]
    assert field["default"] == "none"
    assert field["options"] == ["none", "low", "medium", "high"]
    assert DEFAULTS["game_agent_reasoning_effort"] == field["default"]
    assert "must support" in field["hint"]
    assert field["locales"]["zh"]["name"] == "对弈思考强度"


def test_notice_main_model_schema_matches_defaults():
    """The notice-turn model is a single LLM dropdown; empty = host default."""
    from host.settings import DEFAULTS

    field = _schema()["section_ai"]["fields"]["notice_main_model"]
    assert field["type"] == "model_select"
    assert field["model_type"] == "llm"
    assert field["default"] == ""
    assert DEFAULTS["notice_main_model"] == field["default"]
    assert "host default LLM" in field["hint"]
    zh = field["locales"]["zh"]
    assert zh["name"] == "主AI游戏模型"
    assert "留空=核心主 LLM" in zh["hint"]


def test_ai_models_hint_matches_the_llm_fallback():
    """P1-11: empty ``ai_models`` means the host default LLM, not bot-only."""
    hint = _schema()["section_ai"]["fields"]["ai_models"]
    assert "host default LLM" in hint["hint"]
    assert "Empty = rule bot only" not in hint["hint"]
    zh = hint["locales"]["zh"]["hint"]
    assert "宿主默认模型" in zh
    assert "留空=只用规则 bot" not in zh


def test_agent_avatar_config_field_is_gone():
    """C-8: the avatar comes from the upload path (host/avatar.py), the dead
    config field must not reappear in the form or the defaults."""
    assert "agent_avatar" not in _schema()["section_identity"]["fields"]
    from host.settings import DEFAULTS

    assert "agent_avatar" not in DEFAULTS


def test_retired_session_ids_never_appear_in_config():
    """C-9: hints point at ``gamehub:gm:room``, not the retired dm session."""
    raw = (_PLUGIN_ROOT / "schema.json").read_text(encoding="utf-8")
    assert "gamehub:dm:admin" not in raw
    assert "gamehub:gm:room" in raw


def test_manifest_repo_points_at_the_real_gitea_project():
    """C-40: ``nori/kira-plugin-gamehub`` only exists as a redirect."""
    manifest = json.loads((_PLUGIN_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert "nori-home/kira-plugin-gamehub" in manifest["repo"]


def test_identity_module_keeps_only_the_shared_cap():
    """P1-12: the fictional mapping system is gone; ``MAX_NICKNAME_LEN`` stays."""
    import identity

    assert identity.MAX_NICKNAME_LEN == 40
    for dead in (
        "resolve_player_identity",
        "PlayerIdentity",
        "WEB_USERID_RE",
        "LEGACY_PAGE_ACTOR",
        "IDENTITY_MAPPING",
    ):
        assert not hasattr(identity, dead), dead
