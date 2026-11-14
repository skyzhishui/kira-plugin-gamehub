"""game_agent 的跨手内存历史（native assistant/tool transcript）。

设计口径：

- 按 ``(match_id, seat)`` 隔离；同一用户开新局、不同用户同时开局都不会
  串历史（``match_id`` 本身已保证唯一）。
- 只保存**已完成走子决策回合**里的 ``assistant`` / ``tool`` 消息；所有
  ``user`` 消息（当前局面、plan、威胁分析、纯文本提醒）一律剥离。因此
  历史永远不会回放旧棋盘、旧 plan / 旧威胁分析。
- 一个 entry = 一手决策；entry 内部保持完整的 ``assistant.tool_calls`` /
  ``tool.tool_call_id`` 配对。窗口按 entry 原子截取，绝不从配对中间切。
- 内存态：进程重启即清空，不落数据库，也不进入宿主聊天历史。
- ``reasoning_content`` 属于当轮回传契约，不写入跨手历史（隐藏思考不
  作为普通文本回放）。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from typing import Any

# 历史里的模型说明只保留“判断要点/动作意图”，避免整盘回显。
# tool 结构/配对永不截断，只对 content 做短文本净化。
MAX_HISTORY_TEXT_CHARS = 600
MAX_HISTORY_TEXT_LINES = 8
MAX_HISTORY_TOOL_CHARS = 1200
MAX_HISTORY_TOOL_LINES = 16


def _looks_like_state_block(line: str) -> bool:
    """通用状态块启发式：大量单字符 token 或超长低多样性行。

    不匹配任何具体棋种/列名；只用于历史短净化，误删也只影响旧回合的
    冗长回显，不影响当前局面（当前局面永远来自最新 user 消息）。
    """
    stripped = line.strip()
    if not stripped:
        return False
    tokens = stripped.split()
    if sum(1 for token in tokens if len(token) == 1) >= 8:
        return True
    nonspace = [char for char in stripped if not char.isspace()]
    return len(stripped) >= 80 and len(set(nonspace)) <= 8


def _shorten_history_text(text: str, max_chars: int, max_lines: int) -> str:
    """剔除明显状态块并按行/字符上限缩短；tool_calls 配对不受影响。"""
    lines = [
        line
        for line in str(text or "").splitlines()
        if not _looks_like_state_block(line)
    ]
    shortened = "\n".join(lines).strip()
    if not shortened:
        return ""
    kept_lines = shortened.splitlines()
    if len(kept_lines) > max_lines:
        shortened = "\n".join(kept_lines[:max_lines]).rstrip() + "\n…"
    if len(shortened) > max_chars:
        shortened = shortened[:max_chars].rstrip() + "…"
    return shortened


def _clean_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """规范化 assistant.tool_calls（保留完整 id / name / arguments）。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            function = {}
        call_id = str(item.get("id") or "")
        name = str(function.get("name") or "") or "unknown_tool"
        arguments = function.get("arguments")
        if arguments is None:
            arguments = ""
        elif not isinstance(arguments, str):
            try:
                arguments = json.dumps(arguments, ensure_ascii=False)
            except (TypeError, ValueError):
                arguments = str(arguments)
        if not call_id and not name:
            continue
        out.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": str(arguments)},
            }
        )
    return out


def sanitize_entry(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把一手决策的回合内消息净化为可持久复用的 native history。

    规则：
    - 丢弃 user 消息（动态局面/plan/分析/提醒）；
    - 丢弃 reasoning_content（隐藏推理只在当轮内回传）；
    - assistant.tool_calls 只保留「本 entry 内确有 tool result」的调用，
      避免悬挂声明；没有对应 assistant 声明的 tool 消息也丢弃；
    - 纯文本 assistant 只保留短判断/动作意图：剔除明显状态块，并按
      行/字符上限缩短；tool_calls/tool_call_id 配对结构永不截断；
    - tool result 同样只做短文本净化，保留动作/错误事实。
    """
    source = [m for m in messages if isinstance(m, dict)]
    result_ids = {
        str(m.get("tool_call_id") or "")
        for m in source
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    declared: set[str] = set()
    out: list[dict[str, Any]] = []
    for message in source:
        role = message.get("role")
        if role == "assistant":
            tool_calls = _clean_tool_calls(message.get("tool_calls"))
            if tool_calls:
                kept = [
                    tc
                    for tc in tool_calls
                    if tc.get("id") and tc["id"] in result_ids
                ]
                if not kept:
                    # 整条 assistant 都是悬空调用：不进入历史。
                    continue
                declared.update(tc["id"] for tc in kept)
                content = message.get("content")
                text = (
                    ""
                    if content is None
                    else _shorten_history_text(
                        str(content),
                        MAX_HISTORY_TEXT_CHARS,
                        MAX_HISTORY_TEXT_LINES,
                    )
                )
                out.append(
                    {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": kept,
                    }
                )
            else:
                content = message.get("content")
                text = (
                    ""
                    if content is None
                    else _shorten_history_text(
                        str(content),
                        MAX_HISTORY_TEXT_CHARS,
                        MAX_HISTORY_TEXT_LINES,
                    )
                )
                if text.strip():
                    out.append({"role": "assistant", "content": text})
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if not call_id or call_id not in declared:
                continue
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _shorten_history_text(
                        str(message.get("content") or ""),
                        MAX_HISTORY_TOOL_CHARS,
                        MAX_HISTORY_TOOL_LINES,
                    ),
                }
            )
    return out


class AgentHistory:
    """``(match_id, seat) -> [entry, ...]`` 的内存历史仓库。"""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], list[tuple[dict[str, Any], ...]]] = {}

    @staticmethod
    def _key(match_id: str, seat: int) -> tuple[str, int]:
        return str(match_id), int(seat)

    def append(self, match_id: str, seat: int, messages: Iterable[dict[str, Any]]) -> None:
        """记录一手完成的决策（自动剥离动态 user 消息与悬空配对）。"""
        entry = sanitize_entry(messages)
        if not entry:
            return
        self._entries.setdefault(self._key(match_id, seat), []).append(tuple(entry))

    def window(self, match_id: str, seat: int, rounds: int) -> list[dict[str, Any]]:
        """取最近 ``rounds`` 手历史；``-1``=不限，``0``=关闭。

        窗口以 entry 为单位原子截取，entry 内完整配对不会被切开。
        """
        try:
            limit = int(rounds)
        except (TypeError, ValueError):
            limit = 0
        if limit == 0:
            return []
        entries = self._entries.get(self._key(match_id, seat), ())
        chosen = entries if limit < 0 else entries[-limit:]
        out: list[dict[str, Any]] = []
        for entry in chosen:
            out.extend(copy.deepcopy(list(entry)))
        return out

    def trim(self, match_id: str, seat: int, rounds: int) -> None:
        """按当前配置原子裁剪底层 entries：正值只留最近 N 手，0 清空，-1 不限。

        与 ``window`` 分离：``window`` 是只读视图；``trim`` 用于存储回收，
        支持热更缩小 ``ai_history_rounds`` 后下次决策立即释放旧 entry。
        """
        try:
            limit = int(rounds)
        except (TypeError, ValueError):
            return
        key = self._key(match_id, seat)
        entries = self._entries.get(key)
        if limit == 0:
            self._entries.pop(key, None)
            return
        if limit < 0 or not entries:
            return
        if len(entries) > limit:
            del entries[:-limit]

    def entry_count(self, match_id: str, seat: int) -> int:
        return len(self._entries.get(self._key(match_id, seat), ()))

    def clear_match(self, match_id: str) -> None:
        match_id = str(match_id)
        for key in [k for k in self._entries if k[0] == match_id]:
            self._entries.pop(key, None)

    def retain_matches(self, match_ids: Iterable[str]) -> None:
        """只保留指定 match 的历史（终局/已卸载对局的资源回收）。

        ``match_ids`` 通常来自管理器的 active_rooms；当前正在决策的局
        由调用方显式加入，避免刚结束/边界状态被误清。``-1`` 只表示不按
        回合数截断，不改变本回收口径。
        """
        keep = {str(match_id) for match_id in match_ids}
        for key in [k for k in self._entries if k[0] not in keep]:
            self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()