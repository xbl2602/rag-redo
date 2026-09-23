"""真实通过 PluginRuntime 走一遍 official-ocr-mineru-local 的完整生命
周期：真的 Popen 子进程、真的发本机HTTP、真的申请/释放GPU资源租约、
真的在 disable 时把子进程杀干净。

`RAG_REDO_FAKE_OCR=1` 让子进程内部用确定性假OCR结果，不需要真实模型——
验证的是"这条子进程+HTTP+chain-try链路本身通不通"，不是"识别准不准"，
见 official_ocr_mineru_local/plugin.py 模块 docstring。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.runtime import PluginRuntime, PluginState  # noqa: E402


class TestMineruLocalOcrPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env_backup = os.environ.get("RAG_REDO_FAKE_OCR")
        os.environ["RAG_REDO_FAKE_OCR"] = "1"
        self.addCleanup(self._restore_env)

        self.rt = PluginRuntime(REPO_ROOT / "plugins", state_file=self.tmp / "plugins_state.json")
        self.rt.scan()
        self.assertIn("official-ocr-mineru-local", self.rt.plugins)
        self.rt.load("official-ocr-mineru-local")
        self.assertEqual(self.rt.plugins["official-ocr-mineru-local"].state, PluginState.LOADED)
        self.rt.enable("official-ocr-mineru-local")
        self.assertEqual(
            self.rt.plugins["official-ocr-mineru-local"].state,
            PluginState.ENABLED,
            self.rt.plugins["official-ocr-mineru-local"].error,
        )
        self.instance = self.rt.plugins["official-ocr-mineru-local"].instance
        self.addCleanup(lambda: self.rt.disable("official-ocr-mineru-local"))

    def _restore_env(self) -> None:
        if self._env_backup is None:
            os.environ.pop("RAG_REDO_FAKE_OCR", None)
        else:
            os.environ["RAG_REDO_FAKE_OCR"] = self._env_backup

    def test_enable_acquires_gpu_lease(self):
        self.assertEqual(self.rt.resource_arbiter.holder_of("gpu:0"), "official-ocr-mineru-local")

    def test_extract_real_pdf_via_subprocess_returns_fake_text(self):
        (self.tmp / "scan.pdf").write_bytes(b"%PDF-fake-scanned-content")
        doc = self.instance.extract("lib1", "scan.pdf", self.tmp)
        self.assertIsNotNone(doc.text)
        self.assertIn("fake-ocr", doc.text)
        self.assertIn("scan.pdf", doc.text)
        self.assertIsNone(doc.failure_reason)

    def test_extract_non_pdf_skipped(self):
        (self.tmp / "notes.txt").write_text("纯文本", encoding="utf-8")
        doc = self.instance.extract("lib1", "notes.txt", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIn("不是PDF", doc.failure_reason)

    def test_extract_missing_file_folds_to_failure(self):
        doc = self.instance.extract("lib1", "does-not-exist.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIsNotNone(doc.failure_reason)

    def test_disable_releases_gpu_lease_and_kills_subprocess(self):
        pid = self.instance._handle._process.pid  # noqa: SLF001 - 直接问操作系统这个pid还在不在
        self.rt.disable("official-ocr-mineru-local")
        self.assertIsNone(self.rt.resource_arbiter.holder_of("gpu:0"))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_extract_after_disable_folds_to_failure_not_crash(self):
        self.rt.disable("official-ocr-mineru-local")
        doc = self.instance.extract("lib1", "whatever.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIn("未运行", doc.failure_reason)


if __name__ == "__main__":
    unittest.main()
