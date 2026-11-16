"""AgentHistory 单元测试：窗口原子、-1/0、配对完整性、动态局面剥离。"""

from __future__ import annotations

import json

from agent_history import AgentHistory, sanitize_entry


def _pair(call_id: str, move: str, text: str = "") -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": text or None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "play",
                        "arguments": json.dumps({"move": move}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"已落子 {move}",
        },
    ]


def test_window_is_atomic_across_complete_entries() -> None:
    history = AgentHistory()
    # 第一手包含「非法纠正 + 成功」两轮：两条完整配对必须一起进入窗口。
    history.append(
        "m1",
        1,
        _pair("c1", "Z99", "先试 Z99") + _pair("c2", "J9", "改走 J9"),
    )
    for index in range(3, 13):
        history.append("m1", 1, _pair(f"c{index}", f"J{index}"))

    # 共 11 手；最近 10 手应包含第 2..11 手，且第一手完整（不被切半）。
    window = history.window("m1", 1, 10)
    assistant_calls = [
        call
        for message in window
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    ]
    assert len(assistant_calls) == 10
    tool_results = [m for m in window if m.get("role") == "tool"]
    assert len(tool_results) == 10
    # 第 2 手（索引 1）起，第一手两条配对不进入 10 窗口。
    assert all(
        call["id"] != "c1" and call["id"] != "c2" for call in assistant_calls
    )

    # 把窗口缩小到 1：只取最后一手，且配对完整。
    last = history.window("m1", 1, 1)
    assert [m["role"] for m in last] == ["assistant", "tool"]
    assert last[0]["tool_calls"][0]["id"] == last[1]["tool_call_id"]


def test_window_minus_one_is_unlimited_and_zero_is_disabled() -> None:
    history = AgentHistory()
    for index in range(12):
        history.append("m1", 1, _pair(f"c{index}", f"J{index}"))

    assert history.entry_count("m1", 1) == 12
    assert len(history.window("m1", 1, -1)) == 24
    assert history.window("m1", 1, 0) == []
    assert history.window("m1", 1, 10) == history.window("m1", 1, -1)[-20:]


def test_isolated_by_match_id_and_seat() -> None:
    history = AgentHistory()
    history.append("game-a", 1, _pair("a1", "J9"))
    history.append("game-b", 1, _pair("b1", "K9"))
    history.append("game-a", 0, _pair("a0", "H8"))

    assert "J9" in json.dumps(history.window("game-a", 1, -1))
    assert "K9" not in json.dumps(history.window("game-a", 1, -1))
    assert "K9" in json.dumps(history.window("game-b", 1, -1))
    assert "H8" in json.dumps(history.window("game-a", 0, -1))
    assert history.entry_count("missing", 1) == 0


def test_dynamic_user_messages_and_reasoning_are_stripped() -> None:
    history = AgentHistory()
    messages = [
        {"role": "user", "content": "Game m1 · Gomoku · Current plan: 放水"},
        {
            "role": "assistant",
            "content": "先看局面",
            "reasoning_content": "hidden-chain-of-thought",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "play", "arguments": "{\"move\": \"J9\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "已落子 J9"},
        {"role": "user", "content": "请调用 play"},
    ]
    history.append("m1", 1, messages)

    dumped = json.dumps(history.window("m1", 1, -1), ensure_ascii=False)
    assert "Current plan" not in dumped
    assert "请调用 play" not in dumped
    assert "hidden-chain-of-thought" not in dumped
    assert "已落子 J9" in dumped
    assert [m["role"] for m in history.window("m1", 1, -1)] == [
        "assistant",
        "tool",
    ]


def test_dangling_assistant_tool_calls_are_not_recorded() -> None:
    entry = sanitize_entry(
        [
            {
                "role": "assistant",
                "content": "想落子",
                "tool_calls": [
                    {
                        "id": "dangling",
                        "type": "function",
                        "function": {"name": "play", "arguments": "{\"move\": \"J9\"}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "other-id", "content": "不匹配"},
        ]
    )
    assert entry == []


def test_window_returns_copies_not_shared_mutable_state() -> None:
    history = AgentHistory()
    history.append("m1", 1, _pair("c1", "J9"))
    window = history.window("m1", 1, -1)
    window[0]["content"] = "mutated"
    window[1]["content"] = "mutated"
    assert history.window("m1", 1, -1)[0]["content"] is None
    assert history.window("m1", 1, -1)[1]["content"] == "已落子 J9"


def test_bot_fallback_pair_is_factual_and_closed() -> None:
    history = AgentHistory()
    history.append(
        "m1",
        1,
        [
            {
                "role": "assistant",
                "content": "（规则 bot 兜底落子。）",
                "tool_calls": [
                    {
                        "id": "bot-1",
                        "type": "function",
                        "function": {
                            "name": "play",
                            "arguments": json.dumps({"move": "K9"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "bot-1",
                "content": "已落子 K9（规则 bot 兜底，不是模型候选）",
            },
        ],
    )
    window = history.window("m1", 1, -1)
    assert len(window) == 2
    assert window[0]["tool_calls"][0]["id"] == window[1]["tool_call_id"]
    assert "bot" in window[1]["content"]


def test_retain_matches_prunes_only_unlisted() -> None:
    history = AgentHistory()
    history.append("keep-a", 1, _pair("a1", "J9"))
    history.append("drop-b", 1, _pair("b1", "K9"))
    history.append("keep-c", 1, _pair("c1", "L9"))

    history.retain_matches(["keep-a", "keep-c"])

    assert history.entry_count("keep-a", 1) == 1
    assert history.entry_count("drop-b", 1) == 0
    assert history.entry_count("keep-c", 1) == 1


def test_assistant_state_dump_is_shortened_without_breaking_pair() -> None:
    board_block = (
        "    A B C D E F G H J K L M N O P\n"
        "15  . . . . . . . . . . . . . . .\n"
        "14  . . . . . . . . . . . . . . ."
    )
    long_text = (
        "判断：先占中腹。\n"
        + board_block
        + "\n"
        + "这是一段很长的判断。" * 80
    )
    history = AgentHistory()
    history.append(
        "m1",
        1,
        [
            {
                "role": "assistant",
                "content": long_text,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "play", "arguments": "{\"move\": \"J7\"}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "已执行动作 J7"},
        ],
    )
    entry = history.window("m1", 1, -1)
    assert [m["role"] for m in entry] == ["assistant", "tool"]
    content = entry[0]["content"]
    assert "先占中腹" in content
    assert "A B C" not in content  # 明显棋盘块被剔除
    assert len(content) <= 602  # 短文本上限（含省略号）
    assert entry[0]["tool_calls"][0]["id"] == entry[1]["tool_call_id"]


def test_trim_positive_entries_atomic_zero_clears_and_minus_one_keeps() -> None:
    history = AgentHistory()
    for index in range(12):
        history.append("m1", 1, _pair(f"c{index}", f"J{index}"))

    history.trim("m1", 1, 10)
    assert history.entry_count("m1", 1) == 10
    window = history.window("m1", 1, -1)
    ids = [
        call["id"]
        for message in window
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    ]
    assert "c0" not in ids and "c1" not in ids
    assert "c11" in ids

    # -1 不裁剪；0 清空。
    history.trim("m1", 1, -1)
    assert history.entry_count("m1", 1) == 10
    history.trim("m1", 1, 0)
    assert history.entry_count("m1", 1) == 0


def test_clear_match_and_clear() -> None:
    history = AgentHistory()
    history.append("a", 1, _pair("a1", "J9"))
    history.append("b", 1, _pair("b1", "K9"))
    history.clear_match("a")
    assert history.entry_count("a", 1) == 0
    assert history.entry_count("b", 1) == 1
    history.clear()
    assert history.entry_count("b", 1) == 0

# ----------------------------------------------------------------------
#  D-17 regression: a single normalize_tool_calls implementation serves
#  both consumers.
# ----------------------------------------------------------------------
def test_normalize_tool_calls_synthesizes_ids_for_the_live_transcript():
    from agent_history import normalize_tool_calls

    raw = [
        {"function": {"name": "play", "arguments": {"move": "H8"}}},
        {"id": "call-x", "function": {"name": "", "arguments": ""}},
        "not-a-dict",
    ]
    calls = normalize_tool_calls(raw, id_factory=lambda i: f"call_0_{i}")
    assert [c["id"] for c in calls] == ["call_0_0", "call-x"]
    assert calls[0]["function"]["arguments"] == '{"move": "H8"}'
    assert calls[1]["function"]["name"] == "unknown_tool"


def test_normalize_tool_calls_drops_unidentified_for_history():
    from agent_history import normalize_tool_calls

    raw = [
        {"function": {"name": "play", "arguments": "{}"}},  # no id, no info trail
        {"id": "call-1", "function": {"name": "play", "arguments": '{"move": "J9"}'}},
    ]
    assert normalize_tool_calls(raw, drop_unidentified=True) == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "play", "arguments": '{"move": "J9"}'},
        }
    ]
    # Non-list input degrades to [] for both consumers.
    assert normalize_tool_calls(None) == []
    assert normalize_tool_calls("x", id_factory=lambda i: f"c{i}") == []
