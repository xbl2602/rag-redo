"""单测全程注入假 encoder，绝不碰真实模型——同旧项目测试纪律。本开发环境
也刻意没装 sentence_transformers/torch（几GB，Phase 1 靠假 encoder 验证
逻辑），这顺便验证了"懒加载"契约是真的懒：如果哪天有人不小心把
`from sentence_transformers import ...` 挪到了模块顶层，仅仅 import 这份
测试文件依赖的 embed.py 模块就会在测试收集阶段直接报 ModuleNotFoundError，
不会等到某个用例跑起来才发现。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_embedder_bge_m3.embed import BGEM3Embedder, _RealEncoder  # noqa: E402


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
        """本开发环境没装 sentence_transformers——真正调用 encode() 时
        应该是清清楚楚的 ModuleNotFoundError，而不是模块导入期就挂掉、
        也不是某种更晦涩的错误。这个测试本身能跑到这里，就已经证明了
        "构造 _RealEncoder 不需要这个依赖"。"""
        encoder = _RealEncoder()
        with self.assertRaises(ModuleNotFoundError):
            encoder.encode(["test"])


if __name__ == "__main__":
    unittest.main()
