"""official-chunker 插件：生命周期钩子的薄封装，真实逻辑在 chunk.py。"""
from __future__ import annotations

from core.contracts import Chunk, ExtractedDocument

from .chunk import CHUNKER_VERSION, chunk_document


class ChunkerPlugin:
    def on_load(self, ctx):
        ctx.logger.info("切块器已加载")

    def on_enable(self, ctx):
        ctx.logger.info("切块器已启用")

    def on_disable(self, ctx):
        ctx.logger.info("切块器已禁用")

    def on_unload(self, ctx):
        pass

    def chunk(self, doc: ExtractedDocument) -> list[Chunk]:
        assert doc.text is not None, "chunk() 只接受提取成功的文档"
        pieces = chunk_document(doc.text)
        total = len(pieces)
        return [
            Chunk(
                chunk_id=f"{doc.library_id}:{doc.path}:{i}",
                library_id=doc.library_id,
                path=doc.path,
                chunk_index=i,
                total_chunks=total,
                text=piece.text,
                heading_breadcrumb=piece.heading_breadcrumb,
                chunked_by="official-chunker",
                chunker_version=CHUNKER_VERSION,
            )
            for i, piece in enumerate(pieces)
        ]
