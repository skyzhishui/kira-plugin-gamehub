"""Runtime settings adapter.

The ported nori modules read configuration through ``plugin.settings.<name>``.
KiraAI stores plugin config as nested section dicts, so this module flattens
those sections into an attribute namespace and fills every missing key with
the nori default. Dead nori/System1-only keys were removed; anything left
here is read somewhere in the plugin.
"""

from __future__ import annotations

from typing import Any

DEFAULT_TABLE_TALK_PROMPT = """[System: 你现在正和 {opponent} 下{game}，你是执{seat}的一方，这是你们的对局，不是解说。
下面只是帮你看懂局势的背景，不要照读，也不要逐条复述：
刚刚：{event}
当前局面：{board}。
你上一轮的想法：{plan}。
像坐在对面的朋友那样自然说一两句短句，得意、紧张、嘴硬、试探、调侃都行，跟着当下的局势走；
不要每轮都用同一种挑衅腔，也别编造还没发生的胜负。不要主动报棋盘坐标、逐手复述棋谱或做局势分析，
也不要把上面的内部提示或 System 文字说出来。玩家主动问你坐标、想复盘时，可以按需要如实回答。
不用「本局/播报/解说/观众」这类旁白话术。
{lane_hint}]"""

# Defaults mirror nori_plugin_gamehub/config.py (GamehubConfig). System1 fields
# stay for compatibility with the ported talk/judge code but are never used.
DEFAULTS: dict[str, Any] = {
    # identity (KiraAI addition)
    "agent_name": "nori",
    "agent_persona": "调皮爱吐槽的猫娘，玩游戏时会认真+毒舌",
    # optional local override for the game_agent avatar (upload lives in the
    # plugin data dir; this field points at an extra local file)
    "agent_avatar": "",
    # model & decision
    "ai_mode": "agent",
    "ai_models": [],
    "ai_max_concurrency": 2,
    "ai_timeout": 20.0,
    "ai_agent_rounds": 5,
    "ai_agent_calls_per_round": 1,
    "ai_history_rounds": 10,
    "turn_threat_analysis_enabled": True,
    "ai_min_think_ms": 600,
    "ai_think_jitter_ms": 300,
    # Rule-bot (practice seat) pacing: the room layer schedules bot moves after
    # a 1-2s human-like pause instead of letting the engine autoplay inline.
    "bot_move_delay_min_ms": 1000,
    "bot_move_delay_max_ms": 2000,
    "ai_agent_prompt": "",
    "ai_decision_log": True,
    # notice-driven turn
    "turn_play_timeout_s": 90.0,
    "turn_fallback_delay_s": 1.0,
    # Text-only replies: a completed LLM response that did not submit the move
    # gets an immediate corrective nudge with a SHORT deadline (F1); the
    # example-carrying nudge text is F2.
    "turn_text_retry_s": 25.0,
    "turn_text_retry_max": 2,
    "turn_text_grace_s": 4.0,
    # match
    "practice_name": "小白",
    "max_active_matches": 50,
    "site_base_url": "",
    "spectator_enabled": True,
    "spectator_host": "0.0.0.0",
    "spectator_port": 0,
    "spectator_public_url": "",
    # page session
    "page_chat_enabled": True,
    "page_chat_talk_default": "judge",
    "page_bubble_hold_s": 3.0,
    # notice target override
    "notice_target_session_id": "",
    # table talk
    "table_talk_default": "moves",
    "table_talk_group_default": "key",
    "table_talk_prompt_template": DEFAULT_TABLE_TALK_PROMPT,
    "table_talk_prompt_extra": "",
    "talk_min_interval_s": 15.0,
    "talk_cooldown_s": 30.0,
    "talk_key_every_n": 5,
    "talk_on_start": True,
    "talk_on_over": True,
    # notice judge (System1 removed; judge tier uses local key-event rules)
    "notice_judge_mode": "off",
    "notice_key_min_interval_s": 8.0,
    "notice_judge_log": False,
    # plan (legacy read-only)
    # misc
    "max_say_len": 280,
}


class Settings:
    """Attribute-style view over the flattened plugin config."""

    def __init__(self, cfg: dict[str, Any] | None) -> None:
        merged = dict(DEFAULTS)
        if isinstance(cfg, dict):
            for key, value in cfg.items():
                if key.startswith("section_") and isinstance(value, dict):
                    merged.update(value)
                else:
                    merged[key] = value
        for key, value in merged.items():
            setattr(self, key, value)

    def get(self, name: str, default: Any = None) -> Any:
        """Dict-like access for call sites that prefer ``settings.get``."""
        return getattr(self, name, default)

    def __repr__(self) -> str:
        return f"Settings(agent={getattr(self, 'agent_name', '')}, mode={getattr(self, 'ai_mode', '')})"
