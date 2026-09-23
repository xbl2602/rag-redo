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

from .contracts import ExtractedDocument, LibrarySummary, PageHit, SampledChunk, SearchResult
from .runtime import PluginRuntime


DEFAULT_FUSION_DENSE_WEIGHT = 1.0  # RRF 融合里"向量语义"这一路的权重，对齐 obsidian-rag/config.py 同名默认值
DEFAULT_FUSION_BM25_WEIGHT = 1.0  # RRF 融合里"BM25关键词"这一路的权重，同上


class PipelineError(RuntimeError):
    """编排层缺少必要的已启用插件时抛出——这不是插件自己的失败折叠范畴
    （那是数据层面的"这个文件没收"），是"根本没法开始跑"的配置错误，
    调用方（GUI/CLI/MCP）应该展示成"请先启用 XX 插件"而不是笼统报错。"""


def _chunk_library(chunk_id: str) -> str:
    """chunk_id 约定 f"{library_id}:{path}:{chunk_index}"（见
    core/contracts.py::Chunk 的字段注释）——library_id 是第一个冒号之前
    的部分，纯字符串切分，不用额外查一次数据库就能知道一个 chunk 属于
    哪个库，多库检索合并结果时用得上。"""
    return chunk_id.split(":", 1)[0]


def _chunk_path(chunk_id: str) -> str:
    """同 `_chunk_library`，取中间的 path 段——掐头（library_id，第一个
    冒号前）去尾（chunk_index，最后一个冒号后），中间剩下的原样就是
    path，哪怕 path 本身含冒号（POSIX 文件名理论上合法，Windows 不合法）
    也不会切错，对齐 obsidian-rag/retriever.py::_chunk_file 同样的"从
    chunk_id 直接切出文件路径，不用多查一次"思路。"""
    return chunk_id.split(":", 1)[1].rsplit(":", 1)[0]


def _norm_folder(folder: str) -> str:
    """规范化 folder 参数：去首尾空白与首尾斜杠，反斜杠统一成正斜杠——
    对齐 obsidian-rag/retriever.py::_norm_folder。"""
    return folder.strip().replace("\\", "/").strip("/").strip()


def _in_folder(path: str, folder: str) -> bool:
    """path 是否落在 folder 目录下（或就是 folder 本身，单文件范围）。
    前缀+边界校验：folder="AI" 只匹配 "AI/..."，不匹配 "AIML/..."——对齐
    obsidian-rag/retriever.py::_in_folder，folder 为空时不过滤（全部
    命中）。"""
    if not folder:
        return True
    return path == folder or path.startswith(folder + "/")


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
        pdf_paths: list[str] = []  # 供后面 visual_index 后置阶段复用，不再问 library_manager 第二遍
        for path, included, reason in lib_mgr.resolve_included_files(library_id):
            file_report = IndexFileReport(path=path, included=included, reason=reason)
            report.files.append(file_report)
            if not included:
                continue
            if path.lower().endswith(".pdf"):
                pdf_paths.append(path)

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

        # 页级视觉索引（visual_index，比如 official-visual-wemm）作为独立
        # 后置阶段自动跟随——镜像旧项目 index.py 的 _wemm_auto_phase：每次
        # 文字索引跑完自动同步页级视觉索引，和上面文字那条流水线彻底独立
        # （不影响 report、不影响主链路成功/失败判定）。这里不包一层
        # try/except——按架构红线4的同一条纪律，"绝不抛异常、失败自己折叠"
        # 是 visual_index 插件自己的契约（同 extractor 绝不抛异常），不是
        # 编排层兜底出来的，编排层只负责按顺序调用。没装/没启用任何
        # visual_index 插件时这里是零开销空循环，见 docs/ROADMAP.md TODO
        # 第1条的调查结论。
        for plugin_id in sorted(self.runtime.registry.providers_of("visual_index")):
            visual = self._plugin(plugin_id)
            visual.index_library(library_id, root, pdf_paths)

        return report

    # ---- 查询态 --------------------------------------------------------

    def search(
        self,
        libraries: str,
        query: str,
        *,
        top_k: int = 10,
        exclude: str = "",
        folder: str = "",
    ) -> list[SearchResult]:
        """混合检索：词法 BM25 + 向量 + 每库 RRF 融合 → 跨库候选池 → 全局
        重排 → 装配 SearchResult。

        `libraries` 是 obsidian-rag/retriever.py::hybrid_search 同名参数
        的选库语法（见 official-library-manager 插件的 `resolve_libraries`
        方法，唯一权威实现）——单库名、逗号分隔多库、空字符串=全部库、
        "all"=全部库都合法；`exclude` 做减法；单库场景下传法和旧签名
        完全兼容（一个库名本身就是"逗号分隔列表"里只有一项的特例）。

        跨库排序对齐 obsidian-rag 的"每库先融合、候选池跨库合并、重排器
        统一精排"思路：每个库各自跑 词法+向量→RRF，取本库前 `top_k*2`
        名放进跨库候选池；重排器对整个候选池统一打分，全局 top_k 才是
        最终结果——不是"每库各出 top_k 再简单拼接"，那样会让强相关库的
        第 (k+1) 名被弱相关库的第 1 名挤掉候选池之外都不会发生（因为
        candidate_pool/池化阈值是按库给的，不是按最终名次早早截断）。

        `folder` 过滤在 RRF 融合之前就作用于每库的词法/向量候选列表——
        对齐 obsidian-rag 在 dense/BM25 两路各自过滤 folder 再融合的
        顺序，不是等重排完了再筛，那样候选池会被跟目标目录无关的结果
        提前占满。

        置信度不做"这一批结果内部 min-max 归一化"——直接把重排器给出的
        校准概率（BAAI/bge-reranker-v2-m3 通过 sentence-transformers
        CrossEncoder 默认自带 Sigmoid 激活，`reranker.rerank` 拿到的已经
        是"该块与查询相关的概率"，0.5=无法判断）钳位到 [0,1] 直接用——
        对齐 obsidian-rag 明确记录过的教训（问题54）：置信度必须和排序
        同源、必须是跨查询可比的"真分尺度"，批内归一化会让"整批其实都
        弱相关"的一批结果里排第一的那条被人为拉到接近1.0，误导下游判断。
        """
        lib_mgr = self._singleton("library_manager")
        entries = lib_mgr.resolve_libraries(libraries, exclude)  # 未知库名 ValueError，见该方法说明

        lexical = self._singleton("lexical_index")
        embedder = self._singleton("embedder")
        vector_store = self._singleton("vector_store")
        fusion = self._singleton("fusion")
        reranker = self._singleton("reranker")

        folder_norm = _norm_folder(folder)
        candidate_pool = top_k * 3
        (query_vector,) = embedder.embed_texts([query])
        # RRF 两路权重可调（core/settings.py 通用设置存储，2026-09-23 全面
        # 功能审计发现此前是死值——对齐 obsidian-rag/config.py 的
        # fusion_dense_weight/fusion_bm25_weight，调大 dense 偏语义、调大
        # bm25 偏关键词；没配过就是等权 1.0/1.0，经典无权重 RRF）。
        dense_weight = self.runtime.settings.get("fusion_dense_weight", DEFAULT_FUSION_DENSE_WEIGHT)
        bm25_weight = self.runtime.settings.get("fusion_bm25_weight", DEFAULT_FUSION_BM25_WEIGHT)

        pool_ids: list[str] = []
        for cfg in entries:
            library_id = cfg.library_id
            lexical_hits = lexical.search(library_id, query, top_k=candidate_pool)
            vector_hits = vector_store.query(library_id, list(query_vector), top_k=candidate_pool)
            lexical_ranked = [cid for cid, _ in lexical_hits if _in_folder(_chunk_path(cid), folder_norm)]
            vector_ranked = [cid for cid, _ in vector_hits if _in_folder(_chunk_path(cid), folder_norm)]
            fused = fusion.fuse([lexical_ranked, vector_ranked], weights=[bm25_weight, dense_weight])
            pool_ids.extend(chunk_id for chunk_id, _ in fused[: top_k * 2])
        if not pool_ids:
            return []

        # chunk_id 全局唯一且自带 library_id（见 _chunk_library），按库分组
        # 批量取记录——vector_store.get_by_ids 是单库作用域的 API，不能跨库
        # 一次问完，但也不需要为每个 chunk_id 单独查一次。
        by_library: dict[str, list[str]] = {}
        for chunk_id in pool_ids:
            by_library.setdefault(_chunk_library(chunk_id), []).append(chunk_id)
        records: dict[str, dict] = {}
        for library_id, ids in by_library.items():
            records.update(vector_store.get_by_ids(library_id, ids))

        # 喂给重排器的文本前面带上标题面包屑——重排器只看纯段落正文的话，
        # 少了"这段话出自哪个标题/章节"这个人类读者天然会用到的判断依据，
        # 内容主题相近的几篇笔记之间更容易被判混（真实用 demo-vault 里
        # 四篇主题相关的笔记测才暴露出来，小合成语料没有这个区分度）。
        # 返回给调用方的 SearchResult.text 仍然是不带前缀的原始正文——
        # 这个拼接只是重排器的输入，不改变展示内容。
        rerank_input = []
        for chunk_id in pool_ids:
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

        results: list[SearchResult] = []
        for chunk_id, score in reranked:
            record = records[chunk_id]
            meta = record["metadata"] or {}
            results.append(
                SearchResult(
                    chunk_id=chunk_id,
                    library_id=_chunk_library(chunk_id),
                    path=meta.get("path", ""),
                    heading_breadcrumb=meta.get("heading_breadcrumb", ""),
                    text=record["document"],
                    confidence=max(0.0, min(1.0, float(score))),
                )
            )
        return results

    # ---- 页级视觉导航（独立于 search() 的"第二检索系统"）-------------------

    def navigate(self, library_id: str, query: str, top_k: int = 5) -> list[PageHit]:
        """页级视觉导航——调查过旧项目 obsidian-rag 的 navigate_knowledge/
        wemm_retriever.py 后确认：这不是 search() 的变体，是完全独立的
        检索面，从不与 BM25+向量+RRF 那条融合排序发生任何关系（不混向量
        空间、不混分数），见 core/contracts.py::PageHit 的说明。

        多个 visual_index 插件同时启用时，各自给出各自的排序结果按提供者
        id 顺序拼接——不同视觉模型的相似度量纲不可比，不做跨提供者的分数
        排序合并（这本身也是"绝不混向量空间"原则的自然延伸）。"""
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")

        hits: list[PageHit] = []
        for plugin_id in sorted(self.runtime.registry.providers_of("visual_index")):
            visual = self._plugin(plugin_id)
            hits.extend(visual.navigate(library_id, query, top_k=top_k))
        return hits

    def visual_status(self) -> dict:
        """页级视觉导航（`visual_index` 扩展点）的只读诊断——遍历全部
        已启用的提供者（目前只有 `official-visual-wemm` 一个），按
        plugin_id 汇总各自的 `status()`。对齐 obsidian-rag 的
        `wemm_status` MCP 工具，2026-09-23 全面功能审计发现的缺口。
        没有任何 `visual_index` 插件启用时返回空字典——同 `navigate()`
        的"没装就是空结果不是错误"语义，不强制要求提供者实现
        `status()`（`hasattr` 判断，同 `index_library()` 对
        `lexical.save` 的处理方式，`visual_index` 目前也没有强制的接口
        契约）。"""
        result: dict[str, dict] = {}
        for plugin_id in sorted(self.runtime.registry.providers_of("visual_index")):
            visual = self._plugin(plugin_id)
            if hasattr(visual, "status"):
                result[plugin_id] = visual.status()
        return result

    # ---- 库摘要（library_summary/llm_provider 扩展点，Phase 3）-----------
    #
    # 采样是 vector_store 的事、生成是 llm_provider 的事、存储+写权限门禁
    # 是 library_summary 的事——三方互不知道彼此存在，真正的协调只在这里
    # 发生（架构红线1），和 index_library()/export_library() 是同一种
    # "只有编排层知道跨插件顺序"的模式。两条真实调用路径（对齐调查到的
    # obsidian-rag 行为）：①MCP对话里的agent自己用 sample_library() 拿到
    # 的片段写简介、调 propose_library_summary() 提交，不需要
    # generate_library_summary()（省一次LLM调用）；②GUI"刷新简介"按钮
    # 没有对话中的agent代笔，走 generate_library_summary() 真的调一次
    # 配置好的 llm_provider，见 official-library-summary 插件模块 docstring。

    def get_library_summary(self, library_id: str) -> LibrarySummary:
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        return self._singleton("library_summary").get(library_id)

    def sample_library(self, library_id: str, k: int = 20) -> list[SampledChunk]:
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        vector_store = self._singleton("vector_store")
        if not hasattr(vector_store, "sample"):
            raise PipelineError("当前 vector_store 实现不支持采样（缺少 sample）")
        return vector_store.sample(library_id, k=k)

    def propose_library_summary(self, library_id: str, text: str) -> dict:
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        return self._singleton("library_summary").propose(library_id, text)

    def set_library_summary_direct(self, library_id: str, text: str, *, source: str = "user") -> dict:
        """无条件写入，不经过写权限门禁——给"人类直接操作"这条路径用
        （GUI 手写编辑 / GUI"刷新简介"按钮），见 official-library-summary
        插件 plugin.py::set_direct 的说明。"""
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        return self._singleton("library_summary").set_direct(library_id, text, source=source)

    def apply_library_summary(self, library_id: str, proposal_id: str, confirmation_code: str) -> dict:
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        return self._singleton("library_summary").apply(library_id, proposal_id, confirmation_code)

    def generate_library_summary(self, library_id: str, k: int = 20) -> tuple[str, str]:
        """采样 + 拼prompt + 依次尝试 llm_provider 链（按插件id字母序，
        同 `_extract()` 链式尝试 extractor:pdf 的既定模式），直到某个
        provider 真的产出非空结果为止。返回 (生成的文本, 使用的provider
        插件id)。**不落盘**——落盘是调用方决定要不要走
        propose_library_summary() 的事，这里只负责"编排跨插件生成流程"。
        """
        lib_mgr = self._singleton("library_manager")
        cfg = lib_mgr.store.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")

        samples = self.sample_library(library_id, k=k)
        if not samples:
            raise PipelineError(f"库 {library_id} 尚未建索引或索引为空，无法生成简介（先调用 index_library 建好索引再重试）")

        summary_plugin = self._singleton("library_summary")
        system, user = summary_plugin.build_prompt(cfg.name, samples)

        provider_ids = sorted(self.runtime.registry.providers_of("llm_provider"))
        if not provider_ids:
            raise PipelineError("没有已启用的 llm_provider 插件")
        for plugin_id in provider_ids:
            provider = self._plugin(plugin_id)
            text = provider.complete(system, user)
            if text:
                return summary_plugin.finalize_text(text), plugin_id
        raise PipelineError("所有 llm_provider 均未能生成简介（服务不可用或返回为空，检查本地/云端LLM服务是否在线）")

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
