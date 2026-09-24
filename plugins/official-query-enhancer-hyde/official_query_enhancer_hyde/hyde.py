from __future__ import annotations

import json
import urllib.request

DEFAULT_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "qwen2.5-3b-instruct"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_TOKENS = 200

HYDE_PROMPT = (
    "你是一个航空航天/工程背景的工程师，正在整理自己的个人知识库笔记。"
    "下面是一个检索查询。请写一段 60~150 字的中文笔记正文，内容是：如果这份笔记里"
    "记录了这个问题，它大概会包含哪些具体工具、术语、专有名词、清单和要点。"
    "要具体、贴近工程实际（如软件名、方法名、参数），不要泛泛而谈通用能力。"
    "直接输出这段笔记正文，不要任何解释、引导语或列表符号外的包装。\n\n"
    "查询：{query}"
)


class OpenAiCompatibleHydeClient:
    def complete(
        self,
        prompt: str,
        *,
        url: str,
        model: str,
        api_key: str,
        timeout: float,
        max_tokens: int,
    ) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        message = data["choices"][0]["message"]
        return str(message.get("content") or "").strip()
