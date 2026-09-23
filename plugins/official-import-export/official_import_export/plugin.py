"""official-import-export 插件：生命周期钩子的薄封装，真实逻辑在 archive.py。

这个插件本身不知道 library_manager/lexical_index/vector_store 的存在——
它只认识"manifest/vectors/bm25 三个 JSON 兼容字典打包/解包成一个 zip"这
件事，不直接调用其他插件（架构红线1）。真正跨插件收集/写回数据的编排在
core/pipeline.py 的 export_library/import_library（和 index_library/
search 是同一种"只有编排层知道跨插件顺序"的模式），这个插件只提供
`archive_codec` 扩展点让编排层调用，天生是可替换的——将来想要加密归档、
换更紧凑的二进制格式，都只需要换一个实现同一扩展点的插件，不用碰
core/pipeline.py 一行代码。
"""
from __future__ import annotations

from . import archive


class ImportExportPlugin:
    def on_load(self, ctx):
        ctx.logger.info("导入导出归档编解码器已加载")

    def on_enable(self, ctx):
        ctx.logger.info("导入导出归档编解码器已启用")

    def on_disable(self, ctx):
        ctx.logger.info("导入导出归档编解码器已禁用")

    def on_unload(self, ctx):
        pass

    def pack(self, manifest: dict, vectors: dict, bm25: dict) -> bytes:
        return archive.pack(manifest, vectors, bm25)

    def unpack(self, data: bytes) -> dict:
        return archive.unpack(data)
