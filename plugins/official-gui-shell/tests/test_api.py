"""Api 类是纯 Python，不需要真的渲染一个窗口就能测——这里覆盖的是
"GUI后端调用Pipeline/library-manager对不对、失败会不会被折叠成
{"ok": False, ...}而不是让调用方（js_api桥）异常"，渲染层的验证见
scripts/smoke_gui.py（手动冒烟，不进自动化回归，风格同旧项目
tests/smoke_gui.py）。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
for other_plugin_dir in (_REPO_ROOT / "plugins").glob("*"):
    if other_plugin_dir.is_dir() and str(other_plugin_dir) not in sys.path:
        sys.path.insert(0, str(other_plugin_dir))

from official_gui_shell.api import Api  # noqa: E402

from core.pipeline import Pipeline  # noqa: E402
from core.runtime import PluginRuntime  # noqa: E402

REQUIRED_PLUGINS = [
    "official-extractor-text",
    "official-chunker",
    "official-library-manager",
    "official-lexical-bm25",
    "official-embedder-bge-m3",
    "official-vector-store-chroma",
    "official-fusion-rrf",
    "official-reranker",
]


class _FakeEncoder:
    KEYWORDS = ["插件", "架构"]

    def encode(self, texts):
        return [[float(t.count(k)) for k in self.KEYWORDS] for t in texts]


class _FakeReranker:
    def score(self, query, texts):
        # query.split()，不是逐字符遍历——见 tests/test_demo_vault.py 的
        # 同类注释，逐字符统计在更长的真实文本上容易被噪声干扰。
        terms = query.split()
        return [sum(text.count(term) for term in terms) for text in texts]


class TestApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "notes.md").write_text("# 插件架构\n\n插件系统笔记。", encoding="utf-8")

        self.runtime = PluginRuntime(
            _REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.runtime.scan()
        for plugin_id in REQUIRED_PLUGINS:
            self.runtime.load(plugin_id)
            self.runtime.enable(plugin_id)

        from official_embedder_bge_m3.embed import BGEM3Embedder

        self.runtime.plugins["official-embedder-bge-m3"].instance.embedder = BGEM3Embedder(
            encoder=_FakeEncoder()
        )
        from official_reranker.rerank import RerankerEngine

        self.runtime.plugins["official-reranker"].instance.engine = RerankerEngine(reranker=_FakeReranker())

        lib_mgr = self.runtime.plugins["official-library-manager"].instance
        self.pipeline = Pipeline(self.runtime)
        self.api = Api(self.pipeline, lib_mgr)

    def test_list_libraries_empty_initially(self):
        self.assertEqual(self.api.list_libraries(), [])

    def test_add_library_then_list(self):
        result = self.api.add_library("lib1", "测试库", str(self.vault))
        self.assertTrue(result["ok"])
        libs = self.api.list_libraries()
        self.assertEqual(len(libs), 1)
        self.assertEqual(libs[0]["library_id"], "lib1")

    def test_add_duplicate_library_returns_error_not_exception(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        result = self.api.add_library("lib1", "重复", str(self.vault))
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_reindex_and_search_round_trip(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        reindex_result = self.api.reindex_library("lib1")
        self.assertTrue(reindex_result["ok"])
        self.assertEqual(reindex_result["succeeded"], 1)

        search_result = self.api.search("lib1", "插件 架构")
        self.assertTrue(search_result["ok"])
        self.assertGreater(len(search_result["results"]), 0)
        self.assertEqual(search_result["results"][0]["path"], "notes.md")

    def test_reindex_unknown_library_returns_error_not_exception(self):
        result = self.api.reindex_library("no-such-lib")
        self.assertFalse(result["ok"])

    def test_search_unknown_library_returns_error_not_exception(self):
        """GUI 是零侵入观察者：Pipeline 抛出的任何异常（比如查询一个不存在
        的库）都必须在 Api 这一层被折叠成 {"ok": False}，绝不能让异常
        原样冒泡到 js_api 桥、把整个窗口炸掉（AGENTS.md 架构红线4的GUI
        层体现）。"""
        result = self.api.search("no-such-lib", "随便查点什么")
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_search_empty_query_returns_empty_results_not_error(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        result = self.api.search("lib1", "   ")
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"], [])


if __name__ == "__main__":
    unittest.main()
