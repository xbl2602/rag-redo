"""单测全程注入假HTTP客户端，绝不碰真实网络/真实API Key——同
official-embedder-bge-m3 的"真实依赖懒加载、测试注入假实现"纪律。

Token 失效作用域（问题35 行为兼容）、在途簿记孤儿清理、每日配额预警三组
回归用例都在这里，理由同"这两个文件就是插件对外的全部行为面"。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_ocr_mineru_cloud.extract import (  # noqa: E402
    _content_hash,
    MineruCloudExtractor,
)
from official_ocr_mineru_cloud.ocr import MineruCloudError, _RealHttpClient  # noqa: E402
from official_ocr_mineru_cloud.plugin import MineruCloudOcrPlugin  # noqa: E402

# 核心侧的记账口径（IndexReport.failed/deferred）直接消费插件返回的
# failure_state——终态范围错了这里就会数错，所以直接拿真类断言而不是
# 自己复刻一份判定。
from core.pipeline import IndexFileReport, IndexReport  # noqa: E402

PLUGIN_LOGGER = "rag_redo.plugin.official-ocr-mineru-cloud"
FAKE_KEY_SENTINEL = "SENTINEL-MINERU-KEY-绝对不许进日志"


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


class _TokenTrippingBatchClient:
    """第 trip_on 次提交抛官方 Token 错误码并置位全局标志——复刻
    ocr.py::_RealHttpClient.submit 的 except 分支（kind=="token" → 置位后
    立即上抛，不重试），用来在假环境里演出"同批第一个任务发现 Token 失效、
    其余文件随后到达"这条真实时序。"""

    def __init__(self, trip_on: int, text: str = "云端正文") -> None:
        self.trip_on = trip_on
        self.text = text
        self.submit_calls = 0
        self._token_invalid = False

    def token_invalid(self) -> bool:
        return self._token_invalid

    def reset_token_flag(self) -> None:
        self._token_invalid = False

    def submit(self, file_bytes: bytes, filename: str, *, is_ocr: bool) -> dict:
        self.submit_calls += 1
        if self.submit_calls == self.trip_on:
            self._token_invalid = True
            raise MineruCloudError("任务提交异常 code=A0202", retryable=False, kind="token")
        return {"batch_id": f"batch-{self.submit_calls}", "upload_url": "https://upload.invalid/f"}

    def upload(self, upload_url: str, file_bytes: bytes) -> None:
        return None

    def poll(self, batch_id: str, *, timeout: float) -> tuple[str | None, str, bytes | None]:
        return self.text, "done", b"[]"


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


class _ExtractorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.pending = self.tmp / "state" / "mineru_pending.json"
        self.quota = self.tmp / "state" / "mineru_quota.json"

    def make_pdf(self, name: str, payload: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(payload)
        return path

    def ledger(self) -> dict:
        if not self.pending.is_file():
            return {}
        return json.loads(self.pending.read_text(encoding="utf-8"))

    def seed_ledger(self, data: dict) -> None:
        self.pending.parent.mkdir(parents=True, exist_ok=True)
        self.pending.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestTokenInvalidFailureScope(_ExtractorTestBase):
    """Token 失效的终态范围（对齐 obsidian-rag/index.py:2203-2207 取消任务
    的 `continue`——不落终态、不动 meta——与 :2218-2225 触发任务落
    _terminal_entry 的双分支）。REDO 串行逐文件提取，语义等价物是
    "触发失效的那一个文件落 extract-failed 终态，其余本轮跳过"，
    跳过的必须表达成 deferred 而不是 extract-failed：后者是稳定终态，
    core/pipeline.py:682-701 会因能力签名（只含 backend 与 Key 是否存在）
    没变而判 action="unchanged"，用户补好额度后重跑索引毫无反应。"""

    def _run_batch(self, extractor: MineruCloudExtractor, count: int = 5) -> list:
        docs = []
        for index in range(1, count + 1):
            name = f"scan{index}.pdf"
            self.make_pdf(name, f"%PDF-第{index}个文件".encode("utf-8"))
            docs.append(extractor.extract("lib1", name, self.tmp))
        return docs

    def test_only_the_triggering_file_becomes_terminal_rest_are_deferred(self):
        client = _TokenTrippingBatchClient(trip_on=2)
        extractor = MineruCloudExtractor(http_client=client, pending_path=self.pending)

        docs = self._run_batch(extractor)

        self.assertEqual(docs[0].text, "云端正文", "Token 失效前的文件正常出正文")
        # 触发失效的那一个：真实提交撞上 A0202 → 落终态（LEGACY 同款）
        self.assertIsNone(docs[1].text)
        self.assertEqual(docs[1].failure_state, "extract-failed")
        # 排队等待的其余文件：本轮跳过，绝不能是终态
        for index, doc in enumerate(docs[2:], start=3):
            self.assertIsNone(doc.text, f"第{index}个文件本轮不该出正文")
            self.assertEqual(
                doc.failure_state, "deferred",
                f"第{index}个文件必须 deferred（不落终态、下轮重试）",
            )
            self.assertEqual(doc.failure_reason, "deferred")
        # 核心侧记账口径：只有触发的那一个算 failed，其余算 deferred
        report = IndexReport(
            library_id="lib1",
            files=[
                IndexFileReport(
                    path=doc.path,
                    included=True,
                    reason="",
                    extracted=doc.text is not None,
                    extract_failure=doc.failure_reason,
                    failure_state=doc.failure_state,
                )
                for doc in docs
            ],
        )
        self.assertEqual(report.failed, 1, "Token 失效只能让一个文件进终态")
        self.assertEqual(report.deferred, 3)
        # 未处理的文件既不发请求也不落断点簿记（不烧配额、不留孤儿条目）；
        # 成功那一个的条目在拿到正文时已移除
        self.assertEqual(client.submit_calls, 2, "置位后不该再发提交请求")
        self.assertEqual(self.ledger(), {}, "失效前那个已收口，其余三个不该留下条目")

    def test_deferred_files_are_retried_after_token_reset(self):
        client = _TokenTrippingBatchClient(trip_on=2)
        extractor = MineruCloudExtractor(http_client=client, pending_path=self.pending)
        docs = self._run_batch(extractor)
        self.assertEqual(docs[2].failure_state, "deferred")

        # 下一轮：core/pipeline.py:541-547 调 reset_token_flag 复位标志
        extractor.reset_token_flag()
        healthy = MineruCloudExtractor(
            http_client=_TokenTrippingBatchClient(trip_on=99),
            pending_path=self.pending,
        )
        retried = healthy.extract("lib1", "scan3.pdf", self.tmp)

        self.assertEqual(retried.text, "云端正文", "标志复位后 deferred 文件必须能真重试成功")
        self.assertIsNone(retried.failure_reason)

    def test_pending_resume_survives_dead_token_without_terminal(self):
        """已有在途条目的文件在 Token 失效轮里也不该被记终态：它的结果还在
        服务器上，Key 修好后按 batch_id 续接即可（重复提交纯烧配额）。
        条目必须保留——删掉等于把已付过的配额扔了。"""
        client = _TokenTrippingBatchClient(trip_on=1)
        extractor = MineruCloudExtractor(http_client=client, pending_path=self.pending)
        payload = "%PDF-在途".encode("utf-8")
        path = self.make_pdf("scan.pdf", payload)
        client._token_invalid = True  # 上一轮已经发现 Token 失效
        self.seed_ledger(
            {
                "batch-0": {
                    "path": str(path),
                    "md5": _content_hash(payload),
                    "route": "ocr:mineru-cloud",
                }
            }
        )

        doc = extractor.extract("lib1", "scan.pdf", self.tmp)

        self.assertEqual(doc.failure_state, "deferred")
        self.assertIn("batch-0", self.ledger(), "在途条目必须保留供下轮续接")


class TestPendingPrune(_ExtractorTestBase):
    """在途簿记孤儿清理（对齐 obsidian-rag/index.py:2176-2177 每轮云端段开
    头调 mineru_pending_prune → extractors.py:923-934）。簿记只增不减时，
    残留条目一旦遇到同 path 同内容就会去 poll 一个早已消失的 batch →
    gone → 永久 extract-failed 终态。"""

    def test_prune_keeps_in_flight_and_drops_orphans(self):
        inflight = "%PDF-还在跑".encode("utf-8")
        alive = self.make_pdf("inflight.pdf", inflight)
        gone = self.make_pdf("vanished.pdf", b"%PDF-deleted")
        changed = self.make_pdf("changed.pdf", "%PDF-新内容".encode("utf-8"))
        self.seed_ledger(
            {
                "batch-inflight": {
                    "path": str(alive),
                    "md5": _content_hash(inflight),
                    "route": "ocr:mineru-cloud",
                },
                # batch 已完成：文件在，但字节已变 → 结果没有归宿，孤儿
                "batch-finished": {
                    "path": str(changed),
                    "md5": _content_hash("%PDF-旧内容".encode("utf-8")),
                    "route": "ocr:mineru-cloud",
                },
                # batch 已消失：文件已被删除，孤儿
                "batch-gone": {
                    "path": str(self.tmp / "deleted.pdf"),
                    "md5": _content_hash(b"%PDF-gone"),
                    "route": "ocr:mineru-cloud",
                },
            }
        )
        gone.unlink()
        extractor = MineruCloudExtractor(http_client=_FakeBatchClient(), pending_path=self.pending)

        removed = extractor.prune_pending()

        self.assertEqual(removed, 2)
        self.assertEqual(list(self.ledger()), ["batch-inflight"], "只有在途条目该活下来")

    def test_prune_is_idempotent_and_never_touches_live_entries(self):
        inflight = "%PDF-还在跑".encode("utf-8")
        alive = self.make_pdf("inflight.pdf", inflight)
        self.seed_ledger(
            {
                "batch-inflight": {
                    "path": str(alive),
                    "md5": _content_hash(inflight),
                    "route": "ocr:mineru-cloud",
                }
            }
        )
        extractor = MineruCloudExtractor(http_client=_FakeBatchClient(), pending_path=self.pending)

        self.assertEqual(extractor.prune_pending(), 0)
        self.assertEqual(extractor.prune_pending(), 0)
        self.assertIn("batch-inflight", self.ledger())

    def test_prune_degrades_to_log_when_save_fails(self):
        """清理失败（只读目录/文件锁）只记日志不抛：索引轮次不能因为一次
        簿记清理失败而崩掉，同旧 extractors.py:885-886 的降级口径。"""
        self.make_pdf("gone.pdf", b"%PDF-x")
        self.seed_ledger(
            {
                "batch-gone": {
                    "path": str(self.tmp / "nope.pdf"),
                    "md5": _content_hash(b"%PDF-x"),
                    "route": "ocr:mineru-cloud",
                }
            }
        )
        extractor = MineruCloudExtractor(http_client=_FakeBatchClient(), pending_path=self.pending)

        with patch.object(extractor, "_save_pending", side_effect=OSError("文件被锁")):
            with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as captured:
                removed = extractor.prune_pending()

        self.assertEqual(removed, 1)
        self.assertIn("batch-gone", self.ledger(), "写失败时簿记保持原样，不做半截改写")
        self.assertIn("mineru_pending", "\n".join(captured.output))

    def test_prune_without_pending_path_is_noop(self):
        extractor = MineruCloudExtractor(http_client=_FakeBatchClient())
        self.assertEqual(extractor.prune_pending(), 0)


class TestQuotaWarning(_ExtractorTestBase):
    """每日配额预警（对齐 obsidian-rag/index.py:2181-2187：本轮投影页数
    超过 800 就打提醒）。旧项目只在并行云端段预检一次，REDO 串行逐文件
    提交，等价口径是"提交前把今日累计 + 本文件页数投影一下"。"""

    def _seed_quota(self, pages: int, date: str | None = None) -> None:
        self.quota.parent.mkdir(parents=True, exist_ok=True)
        self.quota.write_text(
            json.dumps({"date": date or time.strftime("%Y-%m-%d"), "files": 39, "pages": pages}),
            encoding="utf-8",
        )

    def _extract_with_pages(self, pages: int) -> object:
        self.make_pdf("scan.pdf", b"%PDF-fake-bytes")
        extractor = MineruCloudExtractor(
            http_client=_FakeBatchClient(),
            pending_path=self.pending,
            quota_path=self.quota,
        )
        with patch.object(MineruCloudExtractor, "_page_count", staticmethod(lambda path: pages)):
            return extractor.extract("lib1", "scan.pdf", self.tmp)

    def test_projection_over_threshold_warns_once_per_round(self):
        self._seed_quota(790)
        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as captured:
            self._extract_with_pages(20)
        joined = "\n".join(captured.output)
        self.assertIn("810", joined, "预警要报出投影后的今日累计页数")
        self.assertIn("800", joined)
        self.assertIn("1000", joined)
        self.assertIn("降优先级", joined)

    def test_under_threshold_does_not_warn(self):
        self._seed_quota(790)
        with self.assertNoLogs(PLUGIN_LOGGER, level="WARNING"):
            self._extract_with_pages(5)

    def test_stale_quota_date_counts_as_zero(self):
        self._seed_quota(9999, date="2000-01-01")
        with self.assertNoLogs(PLUGIN_LOGGER, level="WARNING"):
            self._extract_with_pages(10)

    def test_quota_warning_never_contains_api_key(self):
        self._seed_quota(790)
        backup = os.environ.get("MINERU_API_KEY")
        os.environ["MINERU_API_KEY"] = FAKE_KEY_SENTINEL
        try:
            with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as captured:
                self._extract_with_pages(20)
        finally:
            if backup is None:
                os.environ.pop("MINERU_API_KEY", None)
            else:
                os.environ["MINERU_API_KEY"] = backup
        self.assertNotIn(FAKE_KEY_SENTINEL, "\n".join(captured.output))


class _FakeStorage:
    def __init__(self, base: Path) -> None:
        self.base = base

    def file(self, name: str, legacy: str | None = None) -> Path:
        return self.base / name

    def directory(self, name: str, legacy: str | None = None) -> Path:
        return self.base / name


class _FakeCtx:
    def __init__(self, base: Path) -> None:
        self.settings = {"pdf_scan_backend": "mineru-cloud"}
        self.storage = _FakeStorage(base)
        self.logger = logging.getLogger(PLUGIN_LOGGER)


class TestPluginRoundStartHooks(_ExtractorTestBase):
    """core/pipeline.py:541-547 每轮索引开始对每个 extractor:pdf 插件调
    reset_token_flag()。LEGACY 同一时点做两件事：index.py:1878 复位 Token
    标志、index.py:2176-2177 清理断点簿记孤儿——插件必须真的提供这个钩子，
    否则长驻进程跨轮次复用时标志永不复位（LEGACY 问题41 附记修的就是这个）。"""

    def test_reset_token_flag_is_exposed_by_plugin(self):
        plugin = MineruCloudOcrPlugin()
        self.assertTrue(callable(getattr(plugin, "reset_token_flag", None)))
        plugin.extractor._client._token_invalid.set()
        plugin.reset_token_flag()
        self.assertFalse(plugin.extractor._client.token_invalid())

    def test_round_start_prunes_orphans_when_backend_active(self):
        self.make_pdf("gone.pdf", b"%PDF-x")
        base = self.tmp / "data"
        base.mkdir()
        pending = base / "mineru_pending.json"
        pending.write_text(
            json.dumps(
                {
                    "batch-gone": {
                        "path": str(self.tmp / "nope.pdf"),
                        "md5": _content_hash(b"%PDF-x"),
                        "route": "ocr:mineru-cloud",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        plugin = MineruCloudOcrPlugin()
        plugin.on_load(_FakeCtx(base))

        plugin.reset_token_flag()

        self.assertEqual(json.loads(pending.read_text(encoding="utf-8")), {})

    def test_round_start_skips_prune_when_backend_inactive(self):
        base = self.tmp / "data"
        base.mkdir()
        pending = base / "mineru_pending.json"
        orphan = {
            "batch-gone": {
                "path": str(self.tmp / "nope.pdf"),
                "md5": _content_hash(b"%PDF-x"),
                "route": "ocr:mineru-cloud",
            }
        }
        pending.write_text(json.dumps(orphan, ensure_ascii=False), encoding="utf-8")
        plugin = MineruCloudOcrPlugin()
        ctx = _FakeCtx(base)
        ctx.settings = {"pdf_scan_backend": "mineru-local"}
        plugin.on_load(ctx)

        plugin.reset_token_flag()

        self.assertEqual(
            json.loads(pending.read_text(encoding="utf-8")), orphan,
            "本轮不走云端段时不该动云端簿记",
        )


class TestApiKeyNeverLeaks(unittest.TestCase):
    """附带确认（审计判定"继承良好"）：MinerU 客户端的错误消息只含类型与
    摘要，api_key 与响应体全文绝不进异常文本、日志、测试输出。"""

    def setUp(self) -> None:
        self._backup = os.environ.get("MINERU_API_KEY")
        os.environ["MINERU_API_KEY"] = FAKE_KEY_SENTINEL
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._backup is None:
            os.environ.pop("MINERU_API_KEY", None)
        else:
            os.environ["MINERU_API_KEY"] = self._backup

    def test_submit_auth_error_message_has_no_key(self):
        client = _RealHttpClient(rate_per_minute=0)

        def unauthorized(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        with patch("urllib.request.urlopen", unauthorized):
            with self.assertRaises(MineruCloudError) as ctx:
                client.submit(b"data", "a.pdf", is_ocr=True)

        self.assertNotIn(FAKE_KEY_SENTINEL, str(ctx.exception))
        self.assertTrue(ctx.exception.token_invalid)

    def test_poll_auth_error_message_has_no_key(self):
        client = _RealHttpClient(rate_per_minute=0)

        def forbidden(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        with patch("urllib.request.urlopen", forbidden):
            with self.assertRaises(MineruCloudError) as ctx:
                client.poll("batch-1", timeout=30.0)

        self.assertNotIn(FAKE_KEY_SENTINEL, str(ctx.exception))

    def test_submit_without_key_reports_where_to_configure_only(self):
        # 2026-09-29：Key 现在也可以在设置页填，提示语随之说明两处；性质不变——只说
        # 去哪里配（设置页/环境变量名），绝不带出任何 Key 的值。
        client = _RealHttpClient(rate_per_minute=0)
        os.environ.pop("MINERU_API_KEY", None)
        with self.assertRaises(MineruCloudError) as ctx:
            client.submit(b"data", "a.pdf", is_ocr=True)
        self.assertEqual(
            str(ctx.exception),
            "缺少 MinerU API Key（请在设置页填写，或设置 MINERU_API_KEY 环境变量）",
        )
        self.assertNotIn(FAKE_KEY_SENTINEL, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
