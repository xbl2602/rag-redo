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

    def pack(
        self,
        manifest: dict,
        vectors: dict,
        bm25: dict,
        *,
        index_manifest: dict | None = None,
        extracted: dict | None = None,
        relations: dict | None = None,
        failures: dict | None = None,
        visual: dict | None = None,
        sources: dict[str, bytes] | None = None,
        include_source_files: bool = True,
    ) -> bytes:
        return archive.pack(
            manifest,
            vectors,
            bm25,
            index_manifest=index_manifest,
            extracted=extracted,
            relations=relations,
            failures=failures,
            visual=visual,
            sources=sources,
            include_source_files=include_source_files,
        )

    def unpack(self, data: bytes) -> dict:
        """解包 + 全量校验（逐条目 CRC/JSON/sha256 + 结构 + id 互指）。
        校验在返回之前全部完成，所以调用方拿到 payload 就意味着"这个包
        已经被验过"——对齐 obsidian-rag/import.py:86-108 的"改动前中止"。"""
        return archive.unpack(data)

    def verify(self, payload: dict) -> None:
        """写库之前的独立一道闸：给 pipeline 一个可以单独调用的校验入口，
        正常返回 None，不通过抛 ArchiveFormatError。"""
        return archive.verify(payload)

    def import_plan(
        self,
        payload: dict,
        target_id: str,
        *,
        root_path: str | None = None,
        upsert_batch: int = 500,
    ):
        """纯函数：算出导入要做的全部动作 + 回滚所需信息，不写任何东西。"""
        return archive.import_plan(
            payload, target_id, root_path=root_path, upsert_batch=upsert_batch
        )

    def notices(self, payload: dict) -> tuple[str, ...]:
        """按读到的清单重算给用户看的提示（"包里没有正文""词法条目被丢弃"…）。"""
        return archive.notices(payload)
