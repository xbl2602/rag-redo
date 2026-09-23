"""MinerU 云端 OCR 的真实 HTTP 客户端。

这里"懒"的不是 import（urllib 是标准库，随时能 import，不像
sentence_transformers 那样是个重依赖），而是"不到真正调用 ocr() 的那一刻
绝不发起网络请求、绝不读取/校验 API Key"——构造 `_RealHttpClient()` 本身
在没有网络、没有配置 API Key 的环境下也不应该报错，这是和
official-embedder-bge-m3 的 `_RealEncoder`/official-reranker 的
`_RealReranker` 同一条懒加载纪律的延伸，只是这次"重"的不是本地模型权重，
是需要真实网络/密钥的外部调用。单测全程注入假客户端，不碰真实网络。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class MineruCloudError(Exception):
    """云端OCR调用失败的统一折叠类型。上游HTTP错误的原始文本不直接透传
    ——架构红线9"敏感值错误信息只含类型与摘要，绝不透传上游原文"，云端
    API 报错里可能回显请求头/部分鉴权信息，这里只保留错误类型名。"""


class _RealHttpClient:
    def __init__(self, endpoint: str = "https://mineru.net/api/v4/extract") -> None:
        self.endpoint = endpoint

    def ocr(self, file_bytes: bytes, filename: str) -> str:
        api_key = os.environ.get("MINERU_API_KEY")
        if not api_key:
            raise MineruCloudError("缺少 MINERU_API_KEY 环境变量")
        req = urllib.request.Request(
            self.endpoint,
            data=file_bytes,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/pdf",
                "X-Filename": filename,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                payload = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise MineruCloudError(f"请求失败: {type(exc).__name__}") from exc
        except json.JSONDecodeError as exc:
            raise MineruCloudError(f"响应不是合法JSON: {type(exc).__name__}") from exc
        text = payload.get("markdown") or payload.get("text")
        if not text:
            raise MineruCloudError("响应里没有可用的文本字段")
        return text
