"""单测全程注入假HTTP客户端，绝不碰真实网络/真实API Key——同
official-embedder-bge-m3 的"真实依赖懒加载、测试注入假实现"纪律。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_ocr_mineru_cloud.extract import MineruCloudExtractor  # noqa: E402
from official_ocr_mineru_cloud.ocr import MineruCloudError, _RealHttpClient  # noqa: E402


class _FakeHttpClient:
    def __init__(self, text: str = "识别出的文字", raise_error: Exception | None = None) -> None:
        self.text = text
        self.raise_error = raise_error
        self.calls: list[tuple[bytes, str]] = []

    def ocr(self, file_bytes: bytes, filename: str) -> str:
        self.calls.append((file_bytes, filename))
        if self.raise_error is not None:
            raise self.raise_error
        return self.text


class _FakeBatchClient:
    def __init__(self, *, first_timeout: bool = False) -> None:
        self.first_timeout = first_timeout
        self.submit_calls = 0
        self.upload_calls = 0
        self.poll_calls = 0

    def submit(self, file_bytes: bytes, filename: str, *, is_ocr: bool) -> dict:
        self.submit_calls += 1
        return {"batch_id": "batch-1", "upload_url": "https://upload.invalid/file"}

    def upload(self, upload_url: str, file_bytes: bytes) -> None:
        self.upload_calls += 1

    def poll(self, batch_id: str, *, timeout: float) -> tuple[str | None, str, bytes | None]:
        self.poll_calls += 1
        if self.first_timeout and self.poll_calls == 1:
            return None, "timeout", None
        return "云端正文", "done", b"[]"


class _RetryClient(_FakeHttpClient):
    def __init__(self) -> None:
        super().__init__()
        self.remaining = 2

    def ocr(self, file_bytes: bytes, filename: str) -> str:
        self.calls.append((file_bytes, filename))
        if self.remaining:
            self.remaining -= 1
            raise MineruCloudError("临时错误", retryable=True)
        return "重试成功"


class TestMineruCloudExtractorWithFakeClient(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_successful_ocr_returns_text(self):
        (self.tmp / "scan.pdf").write_bytes(b"%PDF-fake-bytes")
        fake = _FakeHttpClient(text="这是OCR出来的正文")
        extractor = MineruCloudExtractor(http_client=fake)

        doc = extractor.extract("lib1", "scan.pdf", self.tmp)

        self.assertEqual(doc.text, "这是OCR出来的正文")
        self.assertIsNone(doc.failure_reason)
        self.assertEqual(doc.extracted_by, "official-ocr-mineru-cloud")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][1], "scan.pdf")

    def test_retryable_client_error_is_retried(self):
        (self.tmp / "scan.pdf").write_bytes(b"%PDF-fake-bytes")
        fake = _RetryClient()
        doc = MineruCloudExtractor(http_client=fake).extract("lib1", "scan.pdf", self.tmp)
        self.assertEqual(doc.text, "重试成功")
        self.assertEqual(len(fake.calls), 3)

    def test_batch_pending_resume_and_sidecar(self):
        (self.tmp / "scan.pdf").write_bytes(b"%PDF-fake-bytes")
        pending = self.tmp / "state" / "pending.json"
        sidecars = self.tmp / "state" / "sidecars"
        first_client = _FakeBatchClient(first_timeout=True)
        first = MineruCloudExtractor(
            http_client=first_client,
            pending_path=pending,
            sidecar_dir=sidecars,
        ).extract("lib1", "scan.pdf", self.tmp)
        self.assertEqual(first.failure_state, "deferred")
        self.assertTrue(pending.is_file())
        second_client = _FakeBatchClient()
        second = MineruCloudExtractor(
            http_client=second_client,
            pending_path=pending,
            sidecar_dir=sidecars,
        ).extract("lib1", "scan.pdf", self.tmp)
        self.assertEqual(second.text, "云端正文")
        self.assertEqual(second_client.submit_calls, 0)
        self.assertEqual(second_client.upload_calls, 0)
        self.assertEqual(json.loads(pending.read_text(encoding="utf-8")), {})
        self.assertTrue(list(sidecars.glob("*.json")))

    def test_non_pdf_file_skipped_not_error(self):
        (self.tmp / "notes.txt").write_text("纯文本", encoding="utf-8")
        fake = _FakeHttpClient()
        extractor = MineruCloudExtractor(http_client=fake)

        doc = extractor.extract("lib1", "notes.txt", self.tmp)

        self.assertIsNone(doc.text)
        self.assertIn("不是PDF", doc.failure_reason)
        self.assertEqual(fake.calls, [])  # 根本不该发起调用

    def test_missing_file_folds_to_failure_not_exception(self):
        fake = _FakeHttpClient()
        extractor = MineruCloudExtractor(http_client=fake)
        doc = extractor.extract("lib1", "does-not-exist.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIn("读取失败", doc.failure_reason)

    def test_api_error_folds_to_failure_not_exception(self):
        (self.tmp / "scan.pdf").write_bytes(b"%PDF-fake-bytes")
        fake = _FakeHttpClient(raise_error=MineruCloudError("缺少 MINERU_API_KEY 环境变量"))
        extractor = MineruCloudExtractor(http_client=fake)

        doc = extractor.extract("lib1", "scan.pdf", self.tmp)

        self.assertIsNone(doc.text)
        self.assertIn("MINERU_API_KEY", doc.failure_reason)

    def test_unexpected_exception_from_client_folds_not_crashes(self):
        """extractor 绝不抛异常——即使注入的客户端抛出一个完全没预料到的
        异常类型（不是 MineruCloudError），也必须折叠成失败结果，不能让
        它原样冒泡（继承旧项目"extractor 绝不抛异常"的教训）。"""
        (self.tmp / "scan.pdf").write_bytes(b"%PDF-fake-bytes")
        fake = _FakeHttpClient(raise_error=ValueError("完全没预料到的错误"))
        extractor = MineruCloudExtractor(http_client=fake)

        doc = extractor.extract("lib1", "scan.pdf", self.tmp)

        self.assertIsNone(doc.text)
        self.assertTrue(doc.failure_reason.startswith("extract-failed:"))

    def test_empty_ocr_result_folds_to_failure(self):
        (self.tmp / "scan.pdf").write_bytes(b"%PDF-fake-bytes")
        fake = _FakeHttpClient(text="   ")
        extractor = MineruCloudExtractor(http_client=fake)
        doc = extractor.extract("lib1", "scan.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "empty")

    def test_content_hash_is_stable_for_same_bytes(self):
        (self.tmp / "scan.pdf").write_bytes(b"%PDF-fixed-content")
        extractor = MineruCloudExtractor(http_client=_FakeHttpClient())
        doc1 = extractor.extract("lib1", "scan.pdf", self.tmp)
        doc2 = extractor.extract("lib1", "scan.pdf", self.tmp)
        self.assertEqual(doc1.content_hash, doc2.content_hash)
        self.assertNotEqual(doc1.content_hash, "")


class TestRealHttpClientLazyLoading(unittest.TestCase):
    def test_construction_does_not_touch_network_or_env(self):
        client = _RealHttpClient()  # 不应该报错，即使没配置 MINERU_API_KEY/没有网络
        self.assertTrue(client.endpoint)

    def test_ocr_without_api_key_raises_clear_error(self):
        client = _RealHttpClient()
        import os

        env_backup = os.environ.pop("MINERU_API_KEY", None)
        try:
            with self.assertRaises(MineruCloudError) as ctx:
                client.ocr(b"fake", "x.pdf")
            self.assertIn("MINERU_API_KEY", str(ctx.exception))
        finally:
            if env_backup is not None:
                os.environ["MINERU_API_KEY"] = env_backup


if __name__ == "__main__":
    unittest.main()
