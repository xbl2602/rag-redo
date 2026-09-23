"""单测注入假 http_client，不碰真实网络——理由同 official-ocr-mineru-cloud
的测试文件（构造注入模式的既定先例）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_llm_openai_compatible.llm import OpenAiCompatibleClient, _RealHttpClient  # noqa: E402


class _FakeHttpClient:
    def __init__(self, response: str = "这是生成的文本") -> None:
        self.response = response
        self.calls: list[dict] = []

    def complete(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, **kwargs})
        return self.response


class TestOpenAiCompatibleClientWithFake(unittest.TestCase):
    def test_complete_returns_fake_response(self):
        client = OpenAiCompatibleClient(http_client=_FakeHttpClient("你好"))
        self.assertEqual(client.complete("system prompt", "user prompt"), "你好")

    def test_complete_passes_system_and_user_through(self):
        fake = _FakeHttpClient()
        client = OpenAiCompatibleClient(http_client=fake)
        client.complete("规范", "请概括")
        self.assertEqual(fake.calls[0]["system"], "规范")
        self.assertEqual(fake.calls[0]["user"], "请概括")


class TestRealHttpClientConstruction(unittest.TestCase):
    def test_construction_does_not_touch_network(self):
        _RealHttpClient()  # 不应该报错——构造本身不发网络请求

    def test_complete_against_unreachable_url_returns_empty_string_not_raises(self):
        """fail-open：服务不在线时返回空串，不抛异常——调用方（
        official-library-summary）自己决定空串意味着什么。"""
        client = _RealHttpClient()
        text = client.complete(
            "system", "user", url="http://127.0.0.1:1", timeout=2.0
        )
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()
