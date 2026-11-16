"""Host-side LLM adapter for ``game_agent`` (KiraAI provider layer).

The nori ``game_agent`` consumes an async generator shaped like
``task_router.run_replyer_stream``:

    {"type": "text", "delta": "..."}        # assistant text increment
    {"type": "reasoning", "delta": "..."}   # observation only, never replayed
    {"type": "tool_calls", "tool_calls": [...], "reasoning_content": "..."}

``run_replyer_stream`` reproduces that contract on top of the KiraAI host:

- messages are ``system_prompt`` + ``user_prompt`` + ``extra_messages``
  (cross-turn transcript, then the latest board view);
- OpenAI tool schemas are wrapped in throwaway ``BaseTool`` subclasses so the
  provider sees exactly the nori ``play`` surface; ``execute`` is never called
  because ``game_agent`` parses ``tool_calls`` itself;
- ``ai_models`` values are ``provider:model_id`` strings resolved through
  ``ctx.get_llm_client`` and tried in order (ordered failover); an empty list
  falls back to ``ctx.get_default_llm_client()``;
- streaming uses ``client.chat_stream(request, reasoning_effort=...)`` and
  degrades to ``client.chat(request, reasoning_effort=...)`` when streaming
  fails before any output. The caller chooses the level (``game_agent`` reads
  ``game_agent_reasoning_effort``, default ``none``) and the provider validates
  it against the selected model.

Importing ``core.provider`` is deferred to call time so standalone plugin
tests (which run outside the full host dependency set) can import this module
and stub the provider layer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any, Literal

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # pragma: no cover - standalone tests outside the host
    import logging

    logger = logging.getLogger(__name__)


def _schema_tool(schema: dict[str, Any]) -> Any:
    """Wrap one OpenAI tool schema in a never-executed ``BaseTool``."""
    from core.utils.tool_utils import BaseTool

    function = schema.get("function") if isinstance(schema, dict) else None
    function = function if isinstance(function, dict) else {}
    name = str(function.get("name") or "tool")
    description = str(function.get("description") or "")
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}

    class _SchemaTool(BaseTool):
        async def execute(self, *args: Any, **kwargs: Any) -> str:  # pragma: no cover
            # game_agent parses tool calls from the response itself; the host
            # agent loop never executes these schema-only tools.
            raise RuntimeError("schema-only tool cannot be executed")

    _SchemaTool.name = name
    _SchemaTool.description = description
    _SchemaTool.parameters = parameters
    return _SchemaTool()


def _build_tool_set(tools: list[dict] | None) -> Any:
    from core.agent.tool import ToolSet

    return ToolSet([_schema_tool(schema) for schema in (tools or [])])


def _build_request(
    system_prompt: str,
    user_prompt: str,
    extra_messages: list[Any] | None,
    tools: list[dict] | None,
) -> Any:
    """Assemble the provider-neutral ``LLMRequest`` for one round."""
    from core.provider import LLMRequest

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": str(system_prompt or "")},
        {"role": "user", "content": str(user_prompt or "")},
    ]
    for message in extra_messages or []:
        if isinstance(message, dict):
            messages.append(message)
    return LLMRequest(
        messages=messages,
        tool_set=_build_tool_set(tools),
        tool_choice="auto" if tools else "none",
    )


def _merge_tool_call_deltas(acc: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge incremental tool-call fragments into OpenAI tool-call dicts."""
    merged: list[dict[str, Any]] = []
    for index in sorted(acc):
        item = acc[index]
        function = item.get("function") or {}
        arguments = function.get("arguments")
        if arguments is None:
            arguments = ""
        elif not isinstance(arguments, str):
            try:
                arguments = json.dumps(arguments, ensure_ascii=False)
            except (TypeError, ValueError):
                arguments = str(arguments)
        merged.append(
            {
                "id": str(item.get("id") or "") or f"call_{index}",
                "type": "function",
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": str(arguments),
                },
            }
        )
    return merged


def _contract_event(
    tool_calls: list[dict[str, Any]],
    reasoning: str,
    finish_reason: str,
) -> dict[str, Any]:
    return {
        "type": "tool_calls",
        "tool_calls": tool_calls,
        "reasoning_content": reasoning,
        "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
    }


async def _stream_one(
    client: Any, request: Any, *, reasoning_effort: str
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield text/reasoning events, then the final contract event."""
    reasoning_parts: list[str] = []
    tool_acc: dict[int, dict[str, Any]] = {}
    finish_reason = ""
    saw_any = False

    async for chunk in client.chat_stream(request, reasoning_effort=reasoning_effort):
        saw_any = True
        delta_text = getattr(chunk, "delta_text", "") or ""
        if delta_text:
            yield {"type": "text", "delta": delta_text}
        delta_reasoning = getattr(chunk, "delta_reasoning", "") or ""
        if delta_reasoning:
            reasoning_parts.append(delta_reasoning)
            yield {"type": "reasoning", "delta": delta_reasoning}
        for fragment in getattr(chunk, "tool_calls_delta", None) or []:
            if not isinstance(fragment, dict):
                continue
            try:
                index = int(fragment.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            slot = tool_acc.setdefault(
                index, {"id": "", "function": {"name": "", "arguments": ""}}
            )
            if fragment.get("id") and not slot.get("id"):
                slot["id"] = str(fragment["id"])
            function = fragment.get("function")
            if not isinstance(function, dict):
                continue
            if function.get("name"):
                slot["function"]["name"] += str(function["name"])
            if function.get("arguments"):
                slot["function"]["arguments"] += str(function["arguments"])
        if getattr(chunk, "is_final", False) and getattr(chunk, "finish_reason", ""):
            finish_reason = str(chunk.finish_reason)

    if not saw_any:
        raise RuntimeError("LLM stream produced no chunks")

    yield _contract_event(
        _merge_tool_call_deltas(tool_acc),
        "".join(reasoning_parts),
        finish_reason,
    )


async def _chat_one(
    client: Any, request: Any, *, reasoning_effort: str
) -> AsyncGenerator[dict[str, Any], None]:
    """Non-streaming path: text first, then the contract event."""
    response = await client.chat(request, reasoning_effort=reasoning_effort)
    text = getattr(response, "text_response", "") or ""
    reasoning = getattr(response, "reasoning_content", "") or ""
    tool_calls = [tc for tc in (getattr(response, "tool_calls", None) or []) if isinstance(tc, dict)]
    if text:
        yield {"type": "text", "delta": text}
    if reasoning:
        yield {"type": "reasoning", "delta": reasoning}
    yield _contract_event(
        tool_calls,
        reasoning,
        "tool_calls" if tool_calls else "stop",
    )


NO_DEFAULT_MODEL_MESSAGE = "no default LLM client is configured"

ReasoningEffort = Literal["none", "low", "medium", "high"]


class NoDefaultModel(RuntimeError):
    """The host has no default LLM client, so the rule bot must take over."""

    def __init__(self) -> None:
        super().__init__(NO_DEFAULT_MODEL_MESSAGE)


def _clients_for(plugin: Any, model_list: list[str] | None) -> list[tuple[str, Any]]:
    """Resolve ordered ``(label, client)`` pairs; empty list = host default."""
    ctx = getattr(plugin, "ctx", None)
    if ctx is None:
        return []
    models = [
        str(name).strip()
        for name in (model_list or [])
        if str(name).strip()
    ]
    if not models:
        client = ctx.get_default_llm_client()
        if client is None:
            raise NoDefaultModel()
        return [("default", client)]
    pairs: list[tuple[str, Any]] = []
    for model in models:
        client = ctx.get_llm_client(model)
        if client is None:
            logger.warning("gamehub host.llm: model %s is unavailable, skipping", model)
            continue
        pairs.append((model, client))
    if not pairs:
        raise RuntimeError(f"none of the configured models are available: {models}")
    return pairs


async def run_replyer_stream(
    plugin: Any,
    *,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict] | None = None,
    extra_messages: list | None = None,
    model_list: list[str] | None = None,
    stream: bool = True,
    media: Any = None,
    reasoning_effort: ReasoningEffort = "none",
) -> AsyncGenerator[dict[str, Any], None]:
    """Host-side stand-in for nori ``task_router.run_replyer_stream``.

    ``media`` is accepted for signature compatibility and ignored: game matches
    carry no image input (``game_agent`` also reports ``None`` media).
    """
    _ = media  # no image input in game matches
    request = _build_request(system_prompt, user_prompt, extra_messages, tools)
    pairs = _clients_for(plugin, model_list)

    last_error: BaseException | None = None
    for label, client in pairs:
        produced = False
        events = (
            _stream_one(client, request, reasoning_effort=reasoning_effort)
            if stream
            else _chat_one(client, request, reasoning_effort=reasoning_effort)
        )
        try:
            async for event in events:
                produced = True
                yield event
            return
        except Exception as exc:
            last_error = exc
            if produced:
                # Part of the answer already reached the caller; retrying
                # another model would duplicate text in the transcript.
                logger.warning(
                    "gamehub host.llm: model %s failed mid-stream (%s); aborting",
                    label,
                    exc,
                )
                raise
            logger.warning(
                "gamehub host.llm: model %s failed (%s); trying next model",
                label,
                exc,
            )
            if stream:
                # Streaming may be unsupported by this provider/model; retry
                # the same model once with the non-streaming path before
                # moving on to the next model.
                try:
                    async for event in _chat_one(
                        client, request, reasoning_effort=reasoning_effort
                    ):
                        produced = True
                        yield event
                    return
                except Exception as chat_exc:
                    last_error = chat_exc
                    if produced:
                        raise
                    logger.warning(
                        "gamehub host.llm: model %s chat fallback failed (%s)",
                        label,
                        chat_exc,
                    )
            continue

    if last_error is not None:
        raise last_error
    raise RuntimeError("no LLM client available for game agent")
