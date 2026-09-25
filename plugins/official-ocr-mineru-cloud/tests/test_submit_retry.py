# -*- coding: utf-8 -*-
"""问题35 提交链路单测：官方错误码分类 / Token 失效全局标志 / 提交退避
重试。全程注入假 HTTP（覆写 _submit_once），绝不碰真实网络。"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_ocr_mineru_cloud.ocr import (  # noqa: E402
    MineruCloudError,
    _classify_mineru_code,
    _RealHttpClient,
)


class TestClassifyMineruCode(unittest.TestCase):
    def test_official_code_table(self):
        self.assertEqual(_classify_mineru_code("A0202"), "token")
        self.assertEqual(_classify_mineru_code("A0211"), "token")
        self.assertEqual(_classify_mineru_code("-60002"), "fatal")
        self.assertEqual(_classify_mineru_code("-60006"), "fatal")
        self.assertEqual(_classify_mineru_code("-10001"), "transient")
        self.assertEqual(_classify_mineru_code("429"), "transient")
        self.assertEqual(_classify_mineru_code("-99999"), "transient", "未知码宁可多试一次")


class _StubClient(_RealHttpClient):
    """覆写 _submit_once 与 _gate：只测重试/Token 逻辑，不发真实请求。"""

    def __init__(self, failures):
        super().__init__(rate_per_minute=0)
        self.failures = list(failures)
        self.attempts = 0

    def _gate(self) -> None:
        pass

    def _submit_once(self, body, api_key):
        self.attempts += 1
        outcome = self.failures[min(self.attempts - 1, len(self.failures) - 1)]
        if isinstance(outcome, MineruCloudError):
            raise outcome
        return {"batch_id": "b1", "upload_url": "http://put"}


class TestSubmitRetry(unittest.TestCase):
    def setUp(self) -> None:
        # submit 的 API Key 前置校验需要环境变量存在（不发起任何真实请求）
        self._old_key = os.environ.get("MINERU_API_KEY")
        os.environ["MINERU_API_KEY"] = "test-key"

    def tearDown(self) -> None:
        if self._old_key is None:
            os.environ.pop("MINERU_API_KEY", None)
        else:
            os.environ["MINERU_API_KEY"] = self._old_key

    def test_transient_failure_retries_then_succeeds(self):
        client = _StubClient([
            MineruCloudError("服务异常", retryable=True, kind="transient"),
            MineruCloudError("429", retryable=True, kind="transient"),
            "ok",
        ])
        result = client.submit(b"data", "a.pdf", is_ocr=True)
        self.assertEqual(result["batch_id"], "b1")
        self.assertEqual(client.attempts, 3)

    def test_fatal_failure_never_retries(self):
        client = _StubClient([MineruCloudError("超200MB", kind="fatal")])
        with self.assertRaises(MineruCloudError):
            client.submit(b"data", "a.pdf", is_ocr=True)
        self.assertEqual(client.attempts, 1)

    def test_token_failure_sets_global_flag_and_fast_fails_next(self):
        client = _StubClient([MineruCloudError("A0202", kind="token")])
        with self.assertRaises(MineruCloudError):
            client.submit(b"data", "a.pdf", is_ocr=True)
        self.assertEqual(client.attempts, 1, "token 错误不重试")
        self.assertTrue(client.token_invalid())
        with self.assertRaises(MineruCloudError):
            client.submit(b"data", "b.pdf", is_ocr=True)
        self.assertEqual(client.attempts, 1, "置位后不发请求直接快速失败")
        client.reset_token_flag()
        self.assertFalse(client.token_invalid())


if __name__ == "__main__":
    unittest.main()
