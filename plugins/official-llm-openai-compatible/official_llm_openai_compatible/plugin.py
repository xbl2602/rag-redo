"""official-llm-openai-compatible 插件：生命周期钩子的薄封装，真实逻辑在 llm.py。

`llm_provider` 扩展点是 `"multi"` 基数——和 `extractor:pdf` 的链式尝试
同一个模式（core/pipeline.py 按插件id字母序依次尝试，直到某个
provider 真的产出非空结果），不是"多个装了但只有一个生效"的单例切换。
当前只有这一个官方实现；未来要接第二个 provider（比如换一个云端API），
新增一个同样声明 `llm_provider = "multi"` 的插件即可，pipeline.py 的
调用方不需要改一行代码（见 AGENTS.md"插件规则"一节"有序回退链"的设计
意图）。
"""
from __future__ import annotations

from .llm import OpenAiCompatibleClient

PLUGIN_ID = "official-llm-openai-compatible"


class OpenAiCompatibleLlmPlugin:
    def __init__(self, http_client=None) -> None:
        self._injected_client = http_client
        self._client: OpenAiCompatibleClient | None = None

    def on_load(self, ctx):
        self._client = OpenAiCompatibleClient(http_client=self._injected_client, logger=ctx.logger)
        ctx.logger.info("OpenAI兼容LLM Provider已加载")

    def on_enable(self, ctx):
        ctx.logger.info("OpenAI兼容LLM Provider已启用")

    def on_disable(self, ctx):
        ctx.logger.info("OpenAI兼容LLM Provider已禁用")

    def on_unload(self, ctx):
        self._client = None

    def complete(self, system: str, user: str, *, timeout: float | None = None, max_tokens: int | None = None) -> str:
        assert self._client is not None
        return self._client.complete(system, user, timeout=timeout, max_tokens=max_tokens)
