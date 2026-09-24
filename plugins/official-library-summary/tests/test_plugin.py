"""真实通过 PluginRuntime 走一遍 official-library-summary 的完整生命周期
（真实 ctx.write_gate，不是假的）——覆盖"AI生成的简介随便覆盖 vs 用户
手写的简介需要走写权限门禁确认"这条核心行为，这是 core/write_gate.py
第一次被真实插件调用（见 plugin.py 模块 docstring）。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.contracts import SampledChunk  # noqa: E402
from core.runtime import PluginRuntime, PluginState  # noqa: E402


class TestLibrarySummaryPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.rt = PluginRuntime(REPO_ROOT / "plugins", state_file=self.tmp / "state.json", data_dir=self.tmp / "data")
        self.rt.scan()
        self.assertIn("official-library-summary", self.rt.plugins)
        self.rt.load("official-library-summary")
        self.rt.enable("official-library-summary")
        self.assertEqual(
            self.rt.plugins["official-library-summary"].state,
            PluginState.ENABLED,
            self.rt.plugins["official-library-summary"].error,
        )
        self.instance = self.rt.plugins["official-library-summary"].instance

    def test_get_unknown_library_returns_blank_summary(self):
        summary = self.instance.get("lib1")
        self.assertEqual(summary.text, "")
        self.assertEqual(summary.source, "none")

    def test_propose_over_none_source_writes_directly(self):
        result = self.instance.propose("lib1", "这是一段库简介")
        self.assertTrue(result["ok"])
        self.assertTrue(result["applied"])
        summary = self.instance.get("lib1")
        self.assertEqual(summary.text, "这是一段库简介")
        self.assertEqual(summary.source, "ai")

    def test_propose_over_ai_source_writes_directly_without_gate(self):
        self.instance.propose("lib1", "第一版简介")
        result = self.instance.propose("lib1", "第二版简介")
        self.assertTrue(result["applied"])
        self.assertEqual(self.instance.get("lib1").text, "第二版简介")

    def test_propose_over_user_source_requires_confirmation_not_applied_immediately(self):
        self.instance._store.set("lib1", "用户手写的简介", source="user")  # noqa: SLF001 - 测试直接摆好前置状态
        result = self.instance.propose("lib1", "AI想覆盖的新简介")
        self.assertTrue(result["ok"])
        self.assertFalse(result["applied"])
        self.assertIn("proposal_id", result)
        self.assertIn("confirmation_code", result)
        # 没有 apply 之前，原有的用户手写内容必须原封不动
        self.assertEqual(self.instance.get("lib1").text, "用户手写的简介")
        self.assertEqual(self.instance.get("lib1").source, "user")

    def test_apply_with_correct_code_overwrites_user_summary(self):
        self.instance._store.set("lib1", "用户手写的简介", source="user")  # noqa: SLF001
        proposal = self.instance.propose("lib1", "AI想覆盖的新简介")
        result = self.instance.apply("lib1", proposal["proposal_id"], proposal["confirmation_code"])
        self.assertTrue(result["ok"])
        summary = self.instance.get("lib1")
        self.assertEqual(summary.text, "AI想覆盖的新简介")
        self.assertEqual(summary.source, "ai")  # 门禁通过后写入的仍然是"ai"来源，不是"user"

    def test_apply_with_wrong_code_is_rejected_and_user_summary_untouched(self):
        self.instance._store.set("lib1", "用户手写的简介", source="user")  # noqa: SLF001
        proposal = self.instance.propose("lib1", "AI想覆盖的新简介")
        result = self.instance.apply("lib1", proposal["proposal_id"], "000000")
        self.assertFalse(result["ok"])
        self.assertEqual(self.instance.get("lib1").text, "用户手写的简介")

    def test_apply_proposal_is_one_time_use(self):
        self.instance._store.set("lib1", "用户手写的简介", source="user")  # noqa: SLF001
        proposal = self.instance.propose("lib1", "新简介")
        first = self.instance.apply("lib1", proposal["proposal_id"], proposal["confirmation_code"])
        self.assertTrue(first["ok"])
        second = self.instance.apply("lib1", proposal["proposal_id"], proposal["confirmation_code"])
        self.assertFalse(second["ok"])

    def test_propose_rejects_empty_text(self):
        result = self.instance.propose("lib1", "   ")
        self.assertFalse(result["ok"])

    def test_propose_rejects_overlong_text(self):
        result = self.instance.propose("lib1", "字" * 301)
        self.assertFalse(result["ok"])

    def test_build_prompt_and_finalize_text_are_pure_helpers(self):
        samples = [SampledChunk(path="a.md", heading="标题", text="一些内容")]
        system, user = self.instance.build_prompt("我的知识库", samples)
        self.assertIn("agent", system)
        self.assertIn("我的知识库", user)
        self.assertIn("a.md", user)
        self.assertEqual(self.instance.finalize_text("  超长内容" * 200), self.instance.finalize_text("  超长内容" * 200)[:300])

    def test_propose_accepts_fingerprint_and_is_stale_compares_it(self):
        """指纹的"计算"已归属编排层（core/pipeline.py::
        library_content_fingerprint，manifest 全量 path:hash 聚合，对齐旧
        项目 content_fingerprint 算法）——插件只负责存储与比对。"""
        self.instance.propose("lib1", "简介", fingerprint="fp-current")
        self.assertFalse(self.instance.is_stale("lib1", "fp-current"))
        self.assertTrue(self.instance.is_stale("lib1", "fp-new"))

    def test_is_stale_true_when_fingerprint_differs(self):
        self.instance.propose("lib1", "简介", fingerprint="old-fp")
        self.assertTrue(self.instance.is_stale("lib1", "new-fp"))
        self.assertFalse(self.instance.is_stale("lib1", "old-fp"))

    def test_is_stale_false_when_never_generated(self):
        self.assertFalse(self.instance.is_stale("lib-never-summarized", "any-fp"))


if __name__ == "__main__":
    unittest.main()
