"""单测注入假 reranker，不碰真实模型——理由同 official-embedder-bge-m3
的测试文件（含"为什么用 mock.patch.dict 而不是依赖开发机没装依赖"的
真实踩坑记录，这里不重复展开）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_reranker.rerank import RerankerEngine, _RealReranker  # noqa: E402


class _FakeReranker:
    """假打分：谁包含查询词，谁分高——足够验证排序逻辑，不需要真模型。"""

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [1.0 if query in t else 0.0 for t in texts]


class TestRerankerEngineWithFake(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        engine = RerankerEngine(reranker=_FakeReranker())
        self.assertEqual(engine.rerank("q", []), [])

    def test_matching_text_ranks_first(self):
        engine = RerankerEngine(reranker=_FakeReranker())
        results = engine.rerank(
            "插件",
            [("c1", "这段话和天气无关"), ("c2", "这段话提到了插件系统")],
        )
        self.assertEqual(results[0][0], "c2")

    def test_top_k_limits_results(self):
        engine = RerankerEngine(reranker=_FakeReranker())
        pairs = [(f"c{i}", "插件") for i in range(5)]
        results = engine.rerank("插件", pairs, top_k=2)
        self.assertEqual(len(results), 2)


class TestRealRerankerLazyLoading(unittest.TestCase):
    def test_construction_does_not_touch_model(self):
        reranker = _RealReranker()
        self.assertIsNone(reranker._model)

    def test_score_without_dependency_raises_clear_import_error(self):
        reranker = _RealReranker()
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            with self.assertRaises(ImportError):
                reranker.score("q", ["a", "b"])


if __name__ == "__main__":
    unittest.main()
