from __future__ import annotations

import os

from core.contracts import QueryExpansion

from .hyde import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_S,
    DEFAULT_URL,
    HYDE_PROMPT,
    OpenAiCompatibleHydeClient,
)

PLUGIN_ID = "official-query-enhancer-hyde"


class HydeQueryEnhancerPlugin:
    def __init__(self, http_client=None) -> None:
        self._injected_client = http_client
        self._client = None
        self._settings = None
        self._logger = None

    def on_load(self, ctx):
        self._client = self._injected_client or OpenAiCompatibleHydeClient()
        self._settings = ctx.settings
        self._logger = ctx.logger
        ctx.logger.info("HyDE查询增强已加载")

    def on_enable(self, ctx):
        ctx.logger.info("HyDE查询增强已启用")

    def on_disable(self, ctx):
        ctx.logger.info("HyDE查询增强已禁用")

    def on_unload(self, ctx):
        self._client = None
        self._settings = None
        self._logger = None

    def should_enhance(self, query: str, top_confidence: float | None) -> bool:
        if self._settings is None or not query.strip():
            return False
        if not self._settings.get("hyde_enabled", False):
            return False
        threshold = float(self._settings.get("hyde_min_confidence", 0.5))
        return top_confidence is None or top_confidence < threshold

    def enhance(self, query: str) -> QueryExpansion | None:
        if self._client is None or self._settings is None:
            return None
        url = os.environ.get("RAG_REDO_HYDE_LLM_URL") or self._settings.get("hyde_llm_url", DEFAULT_URL)
        model = os.environ.get("RAG_REDO_HYDE_LLM_MODEL") or self._settings.get("hyde_llm_model", DEFAULT_MODEL)
        api_key = os.environ.get("RAG_REDO_HYDE_LLM_API_KEY") or self._settings.get("hyde_llm_api_key", "")
        timeout = float(self._settings.get("hyde_llm_timeout_seconds", DEFAULT_TIMEOUT_S))
        max_tokens = int(self._settings.get("hyde_llm_max_tokens", DEFAULT_MAX_TOKENS))
        try:
            text = self._client.complete(
                HYDE_PROMPT.format(query=query),
                url=url,
                model=model,
                api_key=api_key,
                timeout=timeout,
                max_tokens=max_tokens,
            ).strip()
        except Exception as exc:
            if self._logger is not None:
                self._logger.warning("HyDE LLM调用失败：%s", type(exc).__name__)
            return None
        if not text:
            return None
        return QueryExpansion(query=text, expanded_by=PLUGIN_ID, reason="low_confidence_hyde")
