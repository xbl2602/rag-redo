"""Cross-Encoder 重排器。懒加载契约和 official-embedder-bge-m3 完全同一
套理由，见该插件 embed.py 的模块 docstring——这里不重复展开。
"""
from __future__ import annotations

from typing import Protocol

MODEL_VERSION = "BAAI/bge-reranker-v2-m3"


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


class _RealReranker:
    def __init__(self, model_name: str = MODEL_VERSION) -> None:
        self._model_name = model_name
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder  # noqa: PLC0415 - 故意懒加载

            self._model = CrossEncoder(self._model_name)
        return self._model

    def score(self, query: str, texts: list[str]) -> list[float]:
        model = self._ensure_loaded()
        pairs = [[query, text] for text in texts]
        return list(model.predict(pairs))


class RerankerEngine:
    def __init__(self, reranker: Reranker | None = None) -> None:
        self._reranker = reranker if reranker is not None else _RealReranker()

    def rerank(
        self, query: str, chunk_id_text_pairs: list[tuple[str, str]], top_k: int = 10
    ) -> list[tuple[str, float]]:
        if not chunk_id_text_pairs:
            return []
        ids = [chunk_id for chunk_id, _ in chunk_id_text_pairs]
        texts = [text for _, text in chunk_id_text_pairs]
        scores = self._reranker.score(query, texts)
        ranked = sorted(zip(ids, scores), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_k]
