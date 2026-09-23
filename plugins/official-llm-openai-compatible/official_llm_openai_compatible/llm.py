"""OpenAI 兼容 chat completions 客户端——`llm_provider` 扩展点的参考实现。

移植自旧项目 obsidian-rag 的 `library_summary.py::call_llm`（`retriever.py::
hyde_generate` 用的是同一种端点协议）：本地服务（如 LM Studio）默认监听
`http://localhost:1234/v1/chat/completions`、免鉴权；云端服务同协议、加
`Authorization: Bearer <api_key>`。这里"懒"的不是 import（urllib 是标准
库），而是"不到真正调用 complete() 的那一刻绝不发起网络请求、绝不读取/
校验密钥"，同 official-ocr-mineru-cloud/ocr.py 的 `_RealHttpClient` 一条
纪律。

**没有配置系统**：本项目插件配置的既定约定是硬编码模块常量（见 AGENTS.md
"插件规则"一节），但 LLM 端点/密钥天生是用户环境相关的值，不可能有一个
放诸四海皆准的默认——用环境变量（`RAG_REDO_LLM_URL`/`RAG_REDO_LLM_MODEL`/
`RAG_REDO_LLM_API_KEY`）而不是模块常量，同 `official-ocr-mineru-cloud`
用 `MINERU_API_KEY` 环境变量的先例；默认值对齐旧项目 `config.py` 的
`hyde_llm_url`/`library_summary_llm_url`（本地 LM Studio），不需要用户
装任何东西也能跑，真要接云端/换模型改环境变量即可。

**思考型模型的超时/token预算**：本地推理模型很可能是"思考型"（如 Qwen3
系列，先输出隐藏的 reasoning_content 再给最终 content），thinking 阶段
可能就要吃掉几十秒——默认 timeout/max_tokens 给得比"一次简单问答"更宽裕
（180s/2000 tokens），照抄旧项目 library_summary.py 真实踩过的坑（HyDE
那种查询期的 30s 量级在这里等不到最终答案就被掐断）。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "qwen2.5-3b-instruct"
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_MAX_TOKENS = 2000


class _RealHttpClient:
    """http_client 可注入——默认懒加载的这个真实实现，测试传入假客户端
    （同 BGEM3Embedder(encoder=...) / MineruCloudExtractor(http_client=...)
    的构造注入模式，不需要另起一套机制）。"""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger if logger is not None else logging.getLogger("rag_redo.plugin.official-llm-openai-compatible")

    def complete(
        self,
        system: str,
        user: str,
        *,
        url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """调用 OpenAI 兼容 chat completions 端点。失败（服务不在线/超时/
        返回异常）一律返回空串——fail-open，同旧项目 hyde_generate/
        call_llm 的降级口径：这不是一个"必须成功"的调用，调用方（比如
        official-library-summary）自己决定空串意味着什么、要不要报错给
        用户看见。"""
        url = url or os.environ.get("RAG_REDO_LLM_URL", DEFAULT_URL)
        model = model or os.environ.get("RAG_REDO_LLM_MODEL", DEFAULT_MODEL)
        api_key = api_key if api_key is not None else os.environ.get("RAG_REDO_LLM_API_KEY", "")
        timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_S
        max_tokens = max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.5,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            message = data["choices"][0]["message"]
            text = (message.get("content") or "").strip()
            if not text and message.get("reasoning_content"):
                # 思考型模型：思考阶段没走完就撞上 max_tokens 预算，
                # content 空、reasoning_content 非空——如实报告，别悄悄把
                # 思考过程当结果用（那不是"一段概括"，是内心独白）。
                self._logger.warning("LLM只吐了思考过程就用完了token预算，content为空；考虑调大max_tokens或换非思考型模型")
            return text
        except Exception as exc:  # noqa: BLE001 - 敏感信息(api_key走header)不进日志，只留类型与摘要，架构红线9
            self._logger.warning("LLM调用失败：%s: %s", type(exc).__name__, exc)
            return ""


class OpenAiCompatibleClient:
    def __init__(self, http_client=None, *, logger: logging.Logger | None = None) -> None:
        self._client = http_client if http_client is not None else _RealHttpClient(logger=logger)

    def complete(self, system: str, user: str, *, timeout: float | None = None, max_tokens: int | None = None) -> str:
        return self._client.complete(system, user, timeout=timeout, max_tokens=max_tokens)
