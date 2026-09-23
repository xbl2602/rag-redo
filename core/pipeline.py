"""编排层：唯一知道"先做什么、再做什么"的地方。

索引态：library_manager裁决 → extractor提取 → chunker切块 → embedder向量化
+ lexical_index词法化（并列，互不依赖）→ vector_store写入。
查询态：lexical_index检索 + embedder编码查询向量→vector_store检索（并列）
→ fusion融合 → reranker重排 → 装配 SearchResult。

GUI/CLI/MCP 要"建索引"或"搜索"都应该调这个模块，不要在各自入口里重新拼
一遍顺序逻辑——这是 docs/DATA_FLOW.md"编排层"一节的字面实现，也是唯一
知道"哪个扩展点该在什么时候被调用"的地方，插件互相之间不知道彼此存在。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ExtractedDocument, SearchResult
from .runtime import PluginRuntime


class PipelineError(RuntimeError):
    """编排层缺少必要的已启用插件时抛出——这不是插件自己的失败折叠范畴
    （那是数据层面的"这个文件没收"），是"根本没法开始跑"的配置错误，
    调用方（GUI/CLI/MCP）应该展示成"请先启用 XX 插件"而不是笼统报错。"""


@dataclass
class IndexFileReport:
    path: str
    included: bool
    reason: str
    extracted: bool = False
    extract_failure: str | None = None
    chunk_count: int = 0


@dataclass
class IndexReport:
    library_id: str
    files: list[IndexFileReport] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for f in self.files if f.extracted)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.files if f.included and not f.extracted)


class Pipeline:
    def __init__(self, runtime: PluginRuntime) -> None:
        self.runtime = runtime

    # ---- 插件解析 --------------------------------------------------------

    def _plugin(self, plugin_id: str):
        plugin = self.runtime.plugins.get(plugin_id)
        if plugin is None or plugin.instance is None:
            raise PipelineError(f"插件 {plugin_id} 未加载/未启用，无法编排")
        return plugin.instance

    def _singleton(self, extension_point: str):
        plugin_id = self.runtime.registry.active_of(extension_point)
        if plugin_id is None:
            raise PipelineError(f"没有已启用的 {extension_point} 插件")
        return self._plugin(plugin_id)

    def _extract(self, library_id: str, path: str, root: Path) -> ExtractedDocument:
        ext = Path(path).suffix.lstrip(".").lower()
        provider_ids = self.runtime.registry.providers_of(f"extractor:{ext}")
        if not provider_ids:
            return ExtractedDocument(
                library_id=library_id,
                path=path,
                text=None,
                failure_reason=f"没有插件能处理 .{ext} 格式",
                extracted_by="core.pipeline",
                extractor_version="-",
                content_hash="",
            )
        last_result: ExtractedDocument | None = None
        # 链式尝试：目前每种格式通常只有一个 provider；Phase 2 起 OCR 插件
        # 也会认领同一个 extractor:pdf 点（处理文字层提取器折叠成
        # "scanned:"的文件），届时"文字层先试、失败了交给OCR"就是这条链
        # 的自然延伸，不需要重新设计这一层。
        for plugin_id in sorted(provider_ids):
            extractor = self._plugin(plugin_id)
            result = extractor.extract(library_id, path, root)
            last_result = result
            if result.text is not None:
                return result
        assert last_result is not None
        return last_result

    # ---- 索引态 --------------------------------------------------------

    def index_library(self, library_id: str) -> IndexReport:
        lib_mgr = self._singleton("library_manager")
        cfg = lib_mgr.store.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        root = Path(cfg.root_path)

        chunker = self._singleton("chunker")
        embedder = self._singleton("embedder")
        lexical = self._singleton("lexical_index")
        vector_store = self._singleton("vector_store")

        report = IndexReport(library_id=library_id)
        for path, included, reason in lib_mgr.resolve_included_files(library_id):
            file_report = IndexFileReport(path=path, included=included, reason=reason)
            report.files.append(file_report)
            if not included:
                continue

            doc = self._extract(library_id, path, root)
            if doc.text is None:
                file_report.extract_failure = doc.failure_reason
                continue

            chunks = chunker.chunk(doc)
            if not chunks:
                file_report.extract_failure = "提取成功但没有产出任何chunk"
                continue

            vectors = embedder.embed_chunks(chunks)
            vector_by_id = {v.chunk_id: list(v.vector) for v in vectors}

            chunk_ids = [c.chunk_id for c in chunks]
            embed_vectors = [vector_by_id[cid] for cid in chunk_ids]
            documents = [c.text for c in chunks]
            metadatas = [
                {"path": c.path, "heading_breadcrumb": c.heading_breadcrumb, "chunk_index": c.chunk_index}
                for c in chunks
            ]
            vector_store.upsert(library_id, chunk_ids, embed_vectors, documents=documents, metadatas=metadatas)
            for chunk in chunks:
                lexical.index_chunk(chunk)

            file_report.extracted = True
            file_report.chunk_count = len(chunks)

        if hasattr(lexical, "save"):
            # BM25 索引不像 Chroma 那样每次 upsert 自动落盘，攒到一整个库
            # 处理完再存一次——见 official-lexical-bm25 插件的模块 docstring
            # （早期实现完全没有这一步，进程重启后 BM25 那一路会悄悄清空，
            # 是端到端测试之外才发现的真实缺口）。`hasattr` 判断是因为
            # `lexical_index` 扩展点目前没有强制的接口契约，不是所有实现
            # 都必须支持持久化——见 docs/PLUGIN_SPEC.md 对 Phase 1 阶段
            # "先把官方实现做对、通用契约留给后续显现真实需求"的说明。
            lexical.save(library_id)

        return report

    # ---- 查询态 --------------------------------------------------------

    def search(self, library_id: str, query: str, top_k: int = 10) -> list[SearchResult]:
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            # 不校验的话，Chroma 的 get_or_create_collection 会给一个不存在
            # 的 library_id 静默造一个空 collection、BM25 那一路对未知库也
            # 只是返回空列表——两边都不报错，最终结果是"安安静静地搜到0条"，
            # 用户/调用方没法区分"这个库真的没有相关内容"和"library_id 打
            # 错了"。宁可现在就报清楚，不要在查询态悄悄放过一个打错的id。
            raise KeyError(f"未知库: {library_id}")

        lexical = self._singleton("lexical_index")
        embedder = self._singleton("embedder")
        vector_store = self._singleton("vector_store")
        fusion = self._singleton("fusion")
        reranker = self._singleton("reranker")

        candidate_pool = top_k * 3
        lexical_hits = lexical.search(library_id, query, top_k=candidate_pool)
        (query_vector,) = embedder.embed_texts([query])
        vector_hits = vector_store.query(library_id, list(query_vector), top_k=candidate_pool)

        lexical_ranked = [chunk_id for chunk_id, _ in lexical_hits]
        vector_ranked = [chunk_id for chunk_id, _ in vector_hits]
        fused = fusion.fuse([lexical_ranked, vector_ranked])
        fused_ids = [chunk_id for chunk_id, _ in fused][: top_k * 2]
        if not fused_ids:
            return []

        records = vector_store.get_by_ids(library_id, fused_ids)
        # 喂给重排器的文本前面带上标题面包屑——重排器只看纯段落正文的话，
        # 少了"这段话出自哪个标题/章节"这个人类读者天然会用到的判断依据，
        # 内容主题相近的几篇笔记之间更容易被判混（真实用 demo-vault 里
        # 四篇主题相关的笔记测才暴露出来，小合成语料没有这个区分度）。
        # 返回给调用方的 SearchResult.text 仍然是不带前缀的原始正文——
        # 这个拼接只是重排器的输入，不改变展示内容。
        rerank_input = []
        for chunk_id in fused_ids:
            record = records.get(chunk_id)
            if record is None or not record["document"]:
                continue
            heading = (record["metadata"] or {}).get("heading_breadcrumb", "")
            prefixed = f"{heading}\n{record['document']}" if heading and heading != "(无标题)" else record["document"]
            rerank_input.append((chunk_id, prefixed))
        if not rerank_input:
            return []
        reranked = reranker.rerank(query, rerank_input, top_k=top_k)
        if not reranked:
            return []

        scores = [score for _, score in reranked]
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0

        results: list[SearchResult] = []
        for chunk_id, score in reranked:
            record = records[chunk_id]
            meta = record["metadata"] or {}
            results.append(
                SearchResult(
                    chunk_id=chunk_id,
                    library_id=library_id,
                    path=meta.get("path", ""),
                    heading_breadcrumb=meta.get("heading_breadcrumb", ""),
                    text=record["document"],
                    confidence=(score - lo) / span,
                )
            )
        return results

    # ---- 导入导出 --------------------------------------------------------
    #
    # 把一个库的已建索引数据（配置+向量+BM25状态）打包成可移植归档，或反过来
    # 从归档恢复——目的是把一个库搬到另一台机器时不需要重新跑一遍索引（重新
    # 索引对大库可能是几十分钟到几小时的真实成本，尤其是要真的调用 embedder
    # 模型的那一段）。这两个方法和 index_library/search 是同一种"只有编排层
    # 知道跨插件顺序"的模式：library_manager/lexical_index/vector_store 三个
    # 插件互不知道对方存在，也互不知道"导入导出"这件事，真正的三方协调只在
    # 这里发生（架构红线1）。archive_codec 插件本身也不知道这三者的存在，只
    # 负责"三个 JSON 兼容字典 <-> 一个 zip"的格式编解码，见
    # official-import-export 插件的模块 docstring。

    def export_library(self, library_id: str) -> bytes:
        lib_mgr = self._singleton("library_manager")
        cfg = lib_mgr.store.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")

        lexical = self._singleton("lexical_index")
        vector_store = self._singleton("vector_store")
        archive_codec = self._singleton("archive_codec")

        manifest = {
            "library_id": cfg.library_id,
            "name": cfg.name,
            # root_path 刻意不导出——它是导出方机器上的本地文件系统路径，
            # 换一台机器大概率不存在或指向完全不相关的目录，带过去只会
            # 造成误导；import_library 要求调用方在导入时显式提供新机器
            # 上的真实路径，见其参数说明。
            "selection_in": cfg.selection_in,
            "selection_out": cfg.selection_out,
            "new_file_default": cfg.new_file_default,
            "enabled_extensions": cfg.enabled_extensions,
        }

        if not hasattr(vector_store, "get_all"):
            raise PipelineError("当前 vector_store 实现不支持导出（缺少 get_all）")
        vectors = vector_store.get_all(library_id)

        if not hasattr(lexical, "export_state"):
            raise PipelineError("当前 lexical_index 实现不支持导出（缺少 export_state）")
        bm25_state = lexical.export_state(library_id)

        return archive_codec.pack(manifest, vectors, bm25_state)

    def import_library(self, archive_bytes: bytes, *, root_path: str, library_id: str | None = None) -> str:
        """把 export_library 产出的归档恢复成一个新库，返回恢复出的
        library_id。

        root_path 是必填参数而不是从归档里读——归档来自另一台机器，
        原始 root_path 在这台机器上通常没有意义（见 export_library 的
        注释），调用方（GUI/MCP/CLI）必须明确问清楚"这些笔记文件现在在
        这台机器的哪个目录"，不能假装归档自己知道答案。

        目标 library_id 如果已存在会直接拒绝，不做"覆盖已有库"这种更
        危险的操作——需要覆盖的话，调用方应该先自己删除旧库，这是显式
        的两步操作，不是这个方法悄悄替用户做的决定（同 AGENTS.md"宁可
        诚实空缺，不产出拼接半成品"原则：部分覆盖导致的新旧数据混杂比
        直接拒绝更难排查）。
        """
        lib_mgr = self._singleton("library_manager")
        lexical = self._singleton("lexical_index")
        vector_store = self._singleton("vector_store")
        archive_codec = self._singleton("archive_codec")

        payload = archive_codec.unpack(archive_bytes)
        manifest = payload["manifest"]
        target_id = library_id or manifest["library_id"]

        if lib_mgr.store.get(target_id) is not None:
            raise ValueError(f"库 {target_id!r} 已存在，导入会拒绝覆盖——请先删除旧库，或换一个 library_id")

        lib_mgr.store.add_library(target_id, manifest["name"], root_path)
        lib_mgr.store.set_selection(
            target_id,
            selection_in=manifest.get("selection_in", []),
            selection_out=manifest.get("selection_out", []),
        )
        lib_mgr.store.set_policy(
            target_id,
            new_file_default=manifest.get("new_file_default", "include"),
            enabled_extensions=manifest.get("enabled_extensions", [".md", ".txt"]),
        )

        vectors = payload["vectors"]
        if vectors:
            chunk_ids = list(vectors.keys())
            embed_vectors = [vectors[cid]["embedding"] for cid in chunk_ids]
            documents = [vectors[cid]["document"] for cid in chunk_ids]
            metadatas = [vectors[cid]["metadata"] for cid in chunk_ids]
            vector_store.upsert(target_id, chunk_ids, embed_vectors, documents=documents, metadatas=metadatas)

        if hasattr(lexical, "import_state"):
            lexical.import_state(target_id, payload["bm25"])

        return target_id
