"""业务 CLI（core/cli.py）的端到端测试：真实插件运行时 + 假模型。"""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.cli import main  # noqa: E402


class _FakeEncoder:
    KEYWORDS = ["插件", "架构", "厨房", "食谱"]

    def encode(self, texts):
        return [[float(t.count(k)) for k in self.KEYWORDS] for t in texts]


def _inject_fakes(runtime):
    from official_embedder_bge_m3.embed import BGEM3Embedder
    from official_reranker.rerank import RerankerEngine

    runtime.plugins["official-embedder-bge-m3"].instance.embedder = BGEM3Embedder(
        encoder=_FakeEncoder()
    )
    fake_reranker = type(
        "_FakeRerankerEngine",
        (),
        {
            "idle_check": lambda self: None,
            "release_gpu_slot": lambda self: None,
            "rerank": lambda self, query, pairs, top_k=10: [
                (cid, 0.9 - i * 0.01) for i, (cid, _text) in enumerate(pairs)
            ][:top_k],
        },
    )()
    runtime.plugins["official-reranker"].instance.engine = fake_reranker


class TestBusinessCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "plugin-notes.md").write_text(
            "# 插件架构笔记\n\n这篇笔记讲插件系统的架构设计。", encoding="utf-8"
        )
        (self.vault / "cooking.md").write_text(
            "# 厨房笔记\n\n这篇笔记记录了几个食谱。", encoding="utf-8"
        )
        self.data_root = self.tmp / "data"
        self.args = [
            "--plugins-dir", str(REPO_ROOT / "plugins"),
            "--state-file", str(self.tmp / "plugins_state.json"),
            "--data-root", str(self.data_root),
        ]

    def _run(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([*self.args, *argv])
        return code, buffer.getvalue()

    def test_libraries_add_list_remove_round_trip(self):
        self._run("libraries", "add", str(self.vault), "--name", "测试库", "--id", "test-lib")
        code, out = self._run("libraries", "list")
        self.assertEqual(code, 0)
        self.assertIn("test-lib", out)
        self.assertIn("测试库", out)
        code, _ = self._run("libraries", "remove", "test-lib")
        self.assertEqual(code, 0)
        _, out = self._run("libraries", "list")
        self.assertNotIn("test-lib", out)

    def test_index_and_dedup_commands(self):
        # 先注入假模型再跑 index——CLI 每次调用独立 boot，注入要落在 boot 之后
        original_boot = None
        import core.cli as cli

        original_boot = cli._boot_pipeline

        def _boot_with_fakes(args):
            runtime, pipeline = original_boot(args)
            _inject_fakes(runtime)
            return runtime, pipeline

        with patch.object(cli, "_boot_pipeline", side_effect=_boot_with_fakes):
            self._run("libraries", "add", str(self.vault), "--id", "test-lib")
            code, out = self._run("index", "--library", "test-lib")
            self.assertEqual(code, 0, out)
            self.assertIn("索引完成", out)
            self.assertIn("成功 2", out)
            code, out = self._run("dedup", "--library", "test-lib")
            self.assertEqual(code, 0, out)

    def test_export_import_round_trip(self):
        import core.cli as cli

        original_boot = cli._boot_pipeline

        def _boot_with_fakes(args):
            runtime, pipeline = original_boot(args)
            _inject_fakes(runtime)
            return runtime, pipeline

        with patch.object(cli, "_boot_pipeline", side_effect=_boot_with_fakes):
            self._run("libraries", "add", str(self.vault), "--id", "test-lib")
            self._run("index", "--library", "test-lib")
            out_zip = self.tmp / "backup.zip"
            code, out = self._run("export", "--library", "test-lib", "--out", str(out_zip))
            self.assertEqual(code, 0, out)
            self.assertTrue(out_zip.is_file())
            restored_root = self.tmp / "restored"
            code, out = self._run(
                "import", str(out_zip), "--root", str(restored_root), "--library-id", "restored-lib"
            )
            self.assertEqual(code, 0, out)
            _, listing = self._run("libraries", "list")
            self.assertIn("restored-lib", listing)

    def test_unknown_library_fails_cleanly(self):
        import io as _io
        import contextlib as _contextlib
        import core.cli as cli

        original_boot = cli._boot_pipeline

        def _boot_with_fakes(args):
            runtime, pipeline = original_boot(args)
            _inject_fakes(runtime)
            return runtime, pipeline

        with patch.object(cli, "_boot_pipeline", side_effect=_boot_with_fakes):
            buffer = _io.StringIO()
            with _contextlib.redirect_stderr(buffer):
                code = main([*self.args, "index", "--library", "no-such-lib"])
            self.assertEqual(code, 1)
            self.assertIn("错误", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
