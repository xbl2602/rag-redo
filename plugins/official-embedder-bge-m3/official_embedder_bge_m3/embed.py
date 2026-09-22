"""BGE-M3 向量化（sentence-transformers）。

**懒加载契约**：`import sentence_transformers` 和真实下载/加载 BGE-M3
权重（几GB），都推迟到第一次真正需要编码文本时才发生，绝不在模块导入期
或插件 `on_load`/`on_enable` 阶段触发。理由两条：

1. 插件的发现/加载/启用生命周期本身不应该等价于"把几GB模型吃进内存"——
   这两件事没有必然联系，用户只是想让这个插件"待命"，不代表现在就要付出
   模型加载的时间/显存代价。
2. 单测可以注入假 encoder（同旧 obsidian-rag 项目"索引集成测试用假编码器
   numpy 零向量，绝不碰真实模型"的纪律——见旧 AGENTS.md 测试纪律一节），
   不需要在开发机/CI 上背几GB的模型下载就能验证这层逻辑本身对不对。

真实设备上第一次调用 embed() 才会触发下载+加载，和旧项目 README"第一次
搜索要下载模型，等几十秒到几分钟"的用户预期一致，这不是回归，是延续。
"""
from __future__ import annotations

from typing import Protocol

MODEL_VERSION = "BAAI/bge-m3"


class Encoder(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class _RealEncoder:
    """真实的 sentence-transformers 封装。构造它本身不加载任何东西，只有
    第一次调用 encode() 才会真正 import + 加载模型权重。"""

    def __init__(self, model_name: str = MODEL_VERSION) -> None:
        self._model_name = model_name
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415 - 故意懒加载，见模块 docstring

            self._model = SentenceTransformer(self._model_name)
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_loaded()
        return model.encode(texts, normalize_embeddings=True).tolist()


class BGEM3Embedder:
    def __init__(self, encoder: Encoder | None = None) -> None:
        # encoder=None 时用真实的（懒加载）；测试/CI 注入假 encoder。
        self._encoder = encoder if encoder is not None else _RealEncoder()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._encoder.encode(texts)
