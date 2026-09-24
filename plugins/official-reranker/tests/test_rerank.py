"""单测注入假 reranker，不碰真实模型——理由同 official-embedder-bge-m3
的测试文件（含"为什么用 mock.patch.dict 而不是依赖开发机没装依赖"的
真实踩坑记录，这里不重复展开）。"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.resource_arbiter import ResourceArbiter  # noqa: E402
from official_reranker.rerank import GPU_HOLDER_ID, GPU_RESOURCE_ID, RerankerEngine, _RealReranker  # noqa: E402


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


class TestRealRerankerGpuArbitration(unittest.TestCase):
    """GPU 生命周期管理回归测试，同 official-embedder-bge-m3/tests/
    test_embed.py::TestRealEncoderGpuArbitration 的覆盖点，这里不重复
    展开设计理由。"""

    def test_no_cuda_selects_cpu_without_touching_arbiter(self):
        arb = ResourceArbiter()
        reranker = _RealReranker(resource_arbiter=arb)
        with patch("torch.cuda.is_available", return_value=False):
            self.assertEqual(reranker._select_device(), "cpu")
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID))

    def test_cuda_shares_holder_id_with_embedder_without_self_preempting(self):
        """embedder 和 reranker 共用同一个 GPU_HOLDER_ID——先后各自加载
        不该互相驱逐（见 rerank.py 模块 docstring 的共享 holder_id 设计）。"""
        arb = ResourceArbiter()
        arb.acquire(GPU_RESOURCE_ID, GPU_HOLDER_ID, priority=100)  # 模拟 embedder 先加载过了
        reranker = _RealReranker(resource_arbiter=arb)
        with patch("torch.cuda.is_available", return_value=True), patch(
            "official_reranker.rerank.gpu_arbiter.wait_for_vram",
            side_effect=AssertionError("检索侧不得阻塞等待VRAM（对齐旧项目：wait 语义只属于 WEMM/MinerU 服务端）"),
        ) as wait_spy:
            device = reranker._select_device()
        self.assertEqual(device, "cuda")
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        wait_spy.assert_not_called()

    def test_release_gpu_slot_returns_slot_to_arbiter(self):
        """on_disable 的名额归还（rerank.py 模块 docstring 一直声称这个行为，
        此前代码没实现——现在补齐并对齐）：停用后名额回到空闲状态。"""
        arb = ResourceArbiter()
        reranker = _RealReranker(resource_arbiter=arb)
        with patch("torch.cuda.is_available", return_value=True):
            self.assertEqual(reranker._select_device(), "cuda")
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        reranker.release_gpu_slot()
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID))

    def test_idle_check_unloads_model_after_timeout(self):
        reranker = _RealReranker()
        reranker._model = object()
        reranker._last_use = time.time() - 10_000
        reranker.idle_check()
        self.assertIsNone(reranker._model)


if __name__ == "__main__":
    unittest.main()
