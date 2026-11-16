"""Route gamehub notice-driven action turns to the configured LLM.

The hook runs on ``@on.im_batch_message`` before ``BuildRequestStage`` selects
models. A batch is eligible only when every message is one of this plugin's
own registered turn notices: text matching alone would let a copied notice or
another plugin's text hijack the model choice, so ``is_notice`` and byte-exact
text are both required. Mixed batches keep their original model selection.
"""

from __future__ import annotations

from typing import Any

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)


def apply(plugin: Any, event: Any) -> None:
    """Set ``event.model_group`` for a batch made only of registered notices.

    Empty config, an already-set model group, no live grant for the session,
    a mixed batch or an unresolvable client all leave the event untouched.
    """
    try:
        configured = str(
            getattr(getattr(plugin, "settings", None), "notice_main_model", "") or ""
        ).strip()
        if not configured or getattr(event, "model_group", None):
            return

        director = getattr(plugin, "_director", None)
        if director is None:
            return

        texts = director.notice_texts_for(str(getattr(event, "sid", "") or ""))
        messages = list(getattr(event, "messages", None) or [])
        if not texts or not messages:
            return

        # Strict batch isolation: one ordinary message or unregistered notice
        # anywhere in the batch keeps the original model selection.
        for message in messages:
            text = str(getattr(message, "message_str", "") or "")
            if not getattr(message, "is_notice", False) or text not in texts:
                return

        client = plugin.ctx.get_llm_client(configured)
        if client is None:
            logger.warning(
                "gamehub: notice turn model %r is unavailable; keeping the default model",
                configured,
            )
            return
        event.model_group = [client]
    except Exception:  # noqa: BLE001 - hygiene only; hook errors must not leak state
        logger.warning("gamehub: notice turn model routing failed", exc_info=True)
