"""单测全程注入假 encoder，绝不碰真实模型——同旧项目测试纪律。

**真实踩过的坑**：这份文件曾经靠"本开发环境没装 sentence_transformers"
这个环境事实，让"encode() 应该抛 ModuleNotFoundError"这条断言显得成立。
后来因为要做一次端到端真实验证（用真模型跑一遍 demo-vault），临时装了
sentence_transformers/torch——这条测试当场从"几毫秒的轻量断言"变成了
"真的触发几GB模型下载"，把整个测试套件的运行时间从几秒拖到十几分钟，
内存涨到几个GB，还会真的发网络请求。用真实环境状态当测试断言成立的
前提是脆弱的：环境一变，测试的意图跟着悄悄改变，且不会有任何提示。
现在改用 `unittest.mock.patch.dict(sys.modules, ...)` 强制模拟"这个包
不存在"，不管开发机上实际装没装 sentence_transformers，这条测试的行为
都是确定的、和真实环境解耦的。
"""
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
from official_embedder_bge_m3.embed import GPU_HOLDER_ID, GPU_RESOURCE_ID, BGEM3Embedder, _RealEncoder  # noqa: E402


class _FakeEncoder:
    """确定性假向量：不碰任何真实模型，只依赖文本长度，纯为了让测试可
    重复、可断言。"""

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t) % 7 + 1)] * self.dim for t in texts]


class TestBGEM3EmbedderWithFakeEncoder(unittest.TestCase):
    def test_empty_input_returns_empty_without_calling_encoder(self):
        fake = _FakeEncoder()
        embedder = BGEM3Embedder(encoder=fake)
        self.assertEqual(embedder.embed([]), [])
        self.assertEqual(fake.calls, [])

    def test_embed_returns_one_vector_per_text(self):
        embedder = BGEM3Embedder(encoder=_FakeEncoder(dim=4))
        vectors = embedder.embed(["abc", "de", ""])
        self.assertEqual(len(vectors), 3)
        self.assertTrue(all(len(v) == 4 for v in vectors))

    def test_encoder_receives_exact_texts_passed(self):
        fake = _FakeEncoder()
        embedder = BGEM3Embedder(encoder=fake)
        embedder.embed(["hello", "world"])
        self.assertEqual(fake.calls, [["hello", "world"]])


class TestRealEncoderLazyLoading(unittest.TestCase):
    def test_construction_does_not_touch_model(self):
        encoder = _RealEncoder()  # 不应该报错，即使 sentence_transformers 没装
        self.assertIsNone(encoder._model)

    def test_encode_without_dependency_raises_clear_import_error(self):
        """强制模拟 sentence_transformers 不存在（不依赖开发机实际有没有
        装这个包，见模块 docstring）——真正调用 encode() 时应该是清清楚楚
        的 ImportError，而不是模块导入期就挂掉、也不是某种更晦涩的错误。
        这个测试本身能跑到断言这一步，就已经证明了"构造 _RealEncoder
        不需要这个依赖"（懒加载）。"""
        encoder = _RealEncoder()
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            with self.assertRaises(ImportError):
                encoder.encode(["test"])


class TestRealEncoderGpuArbitration(unittest.TestCase):
    """GPU 生命周期管理回归测试（2026-09-23 补，按 obsidian-rag 真实行为
    移植，见 embed.py 模块 docstring）：设备选择、检索侧高优先级抢占、
    空闲卸载。这台开发机上 torch 是真实装了的（CPU-only），用 mock 强制
    走 CUDA 分支，不依赖真实有没有 GPU 硬件。"""

    def test_no_cuda_selects_cpu_without_touching_arbiter(self):
        arb = ResourceArbiter()
        encoder = _RealEncoder(resource_arbiter=arb)
        with patch("torch.cuda.is_available", return_value=False):
            self.assertEqual(encoder._select_device(), "cpu")
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID))

    def test_cuda_preempts_lower_priority_wemm_style_holder(self):
        """检索侧优先抢占（对齐旧项目行为）：bge-m3 要用 CUDA 时，如果
        WEMM/OCR-local 这类低优先级消费者正占着"gpu:0"，应该被挤开。"""
        arb = ResourceArbiter()
        preempted = []
        arb.acquire("gpu:0", "official-visual-wemm", priority=10, on_preempt=lambda: preempted.append("wemm"), preempt_equal=True)
        encoder = _RealEncoder(resource_arbiter=arb)
        with patch("torch.cuda.is_available", return_value=True), patch(
            "official_embedder_bge_m3.embed.gpu_arbiter.wait_for_vram",
            side_effect=AssertionError("检索侧不得阻塞等待VRAM（对齐旧项目：wait 语义只属于 WEMM/MinerU 服务端）"),
        ) as wait_spy:
            device = encoder._select_device()
        self.assertEqual(device, "cuda")
        self.assertEqual(preempted, ["wemm"])
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        wait_spy.assert_not_called()

    def test_release_gpu_slot_returns_slot_to_arbiter(self):
        """on_disable 的名额归还（对齐 rerank.py 模块 docstring 声明的既有
        设计）：停用后名额必须回到仲裁器的"空闲"状态，否则被禁用的检索侧
        会以高优先级永久占位，WEMM/OCR-local 再也抢不到。"""
        arb = ResourceArbiter()
        encoder = _RealEncoder(resource_arbiter=arb)
        with patch("torch.cuda.is_available", return_value=True):
            self.assertEqual(encoder._select_device(), "cuda")
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        encoder.release_gpu_slot()
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID))

    def test_idle_check_unloads_model_after_timeout(self):
        encoder = _RealEncoder()
        encoder._model = object()  # 假装模型已加载，不需要真的加载一遍
        encoder._last_use = time.time() - 10_000  # 远超默认 300s 空闲阈值
        encoder.idle_check()
        self.assertIsNone(encoder._model)

    def test_idle_check_does_not_unload_recently_used_model(self):
        encoder = _RealEncoder()
        encoder._model = object()
        encoder._last_use = time.time()
        encoder.idle_check()
        self.assertIsNotNone(encoder._model)


if __name__ == "__main__":
    unittest.main()
