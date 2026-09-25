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

import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field, replace as dataclasses_replace
from pathlib import Path
from typing import Callable, Mapping

from .contracts import Chunk, DocumentContent, ExtractedDocument, GraphResponse, LibraryFreshness, LibrarySummary, PageHit, QueryExpansion, SampledChunk, SearchAdviceInput, SearchResponse, SearchResult, SemanticGraphResponse, VisualPageState
from .graph import build_graph, select_semantic_edges
from .extract_cache import ExtractCache
from .index_failures import IndexFailuresStore
from .index_generation import INDEX_MANIFEST_VERSION, IndexGenerationStore, IndexManifestStore
from .index_progress import IndexProgressEvent, IndexStartResult, IndexWorkerManager
from .note_relations import NoteRelationsStore, extract_wikilink_targets
from .text_cleaning import TEXT_PIPELINE_VERSION, build_anchor_context, clean_wikilinks, extract_frontmatter
from .runtime import PluginRuntime, PluginState


DEFAULT_FUSION_DENSE_WEIGHT = 1.0  # RRF 融合里"向量语义"这一路的权重，对齐 obsidian-rag/config.py 同名默认值
DEFAULT_FUSION_BM25_WEIGHT = 1.0  # RRF 融合里"BM25关键词"这一路的权重，同上
# 候选池尺度（对齐 obsidian-rag/config.py 同名键，2026-09-25 终审补齐——
# 此前 top_k*3 的池子把默认 top_k=5 时的候选从旧的 200 静默缩到 15）
DEFAULT_DENSE_CANDIDATE_FACTOR = 8
DEFAULT_DENSE_MIN_CANDIDATES = 200
DEFAULT_RERANK_CANDIDATES = 50  # 送重排器的全局候选池大小（每库融合 top N 进入，对齐旧 rerank_candidates）
DEFAULT_RETURN_CHUNK_LIMIT = 2000  # 检索返回给 LLM 的单块最大字符（对齐旧 config.py::return_chunk_limit）
TRUNCATE_MARK = "… [本块已截断，完整内容见源文件]"  # 截断提示文案（对齐旧 config.py::truncate_mark）
FOLD_WINDOW_FACTOR = 4  # 正文模式交付候选窗口倍数（同节折叠吃掉的候选由窗口补位，旧 retriever.py:29）

# 置信度分档 + 同篇结果封顶（2026-09-23 全面功能审计B类，对齐 obsidian-rag
# retriever.py 的"问题54真分尺度重标定"一节）：DEFAULT_* 都是 obsidian-rag
# config.py 里对应设置项的逐字默认值，真实值走 core/settings.py（GUI/MCP
# 都能读同一份设置，不各自维护一份影子默认值）。
CONF_TIER_STRONG = 0.75  # 置信度≥此值 → "高相关"（真分尺度，重排器自己的相关概率，非批内归一化）
DEFAULT_CONFIDENCE_WARN_THRESHOLD = 0.30  # 低于此值 → "弱相关"，来源行标注"仅供参考"
DEFAULT_CONFIDENCE_DROP_THRESHOLD = 0.0  # 低于此值直接丢弃不返回；0=关闭（obsidian-rag当前默认口径：给满top_k，只标注不丢弃）
DEFAULT_MAX_CHUNKS_PER_FILE = 3  # 同一文件在最终结果里最多出现几条，防止单篇文档占满整个结果列表
TERMINAL_FAILURE_STATES = frozenset({"unreadable", "empty", "tbd", "scanned", "extract-failed"})


def is_tbd_heavy(content: str, ratio: float) -> bool:
    if ratio <= 0:
        return False
    lines = [line for line in content.splitlines() if line.strip()]
    if not lines:
        return False
    pattern = re.compile(r"\[TBD|TBD\s*[—-]|\[todo\]|TODO\s*[—-]", re.IGNORECASE)
    return sum(1 for line in lines if pattern.search(line)) / len(lines) >= ratio


def normalize_failure_state(reason: str | None) -> str:
    value = str(reason or "extract-failed").strip().lower()
    if value == "unreadable" or value.startswith("unreadable:"):
        return "unreadable"
    if value == "empty" or value.startswith("empty:"):
        return "empty"
    if value == "tbd" or value.startswith("tbd:"):
        return "tbd"
    if value == "scanned" or value.startswith("scanned:"):
        return "scanned"
    return "extract-failed"


def confidence_tier(conf: float, warn_threshold: float) -> str:
    """置信度 → 分档词（高相关/中相关/弱相关）。参数与比较都在真分尺度
    （重排器自己的相关概率）上，对齐 obsidian-rag `retriever.py::_conf_tier`
    的分档逻辑——`SearchResult.confidence` 已经是钳位后的原始概率，
    这里只是加一层人类可读标签，不改变排序或过滤。"""
    if conf >= CONF_TIER_STRONG:
        return "高相关"
    if conf >= warn_threshold:
        return "中相关"
    return "弱相关"


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


def _truncate_at_line(doc: str, limit: int, mark: str) -> tuple[str, bool]:
    """返回截断：优先落在完整行边界（表格行/段落不拦腰切），最多 ±300 字符。

    逐字移植 obsidian-rag/retriever.py::_truncate_at_line（282-297）：索引侧
    对含表格的超长块"宁大勿断"整块保留（可 >2000），返回侧硬切会把表格行从
    中间切断；改为在截断点附近找行尾/行首收边。"""
    cut = doc[:limit]
    nl = cut.rfind("\n")
    if nl > 0 and limit - nl <= 300:
        cut = doc[:nl]
    else:
        nxt = doc.find("\n", limit)
        if nxt != -1 and nxt - limit <= 300:
            cut = doc[:nxt]
    return cut + "\n" + mark, True


def _strip_anchor_context(document: str, meta: dict) -> str:
    """剥离索引期拼进块文本的锚点前缀（问题18 v6：ctx 只喂给嵌入/BM25/重排，
    交付给用户的正文不带前缀——旧项目把 ctx 存 metadata 就是供输出剥离）。"""
    ctx = str((meta or {}).get("ctx") or "")
    if ctx and document.startswith(ctx + "\n"):
        return document[len(ctx) + 1 :]
    return document


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
    failure_state: str | None = None
    capability_signature: str | None = None
    chunk_count: int = 0
    action: str = "processed"


@dataclass
class IndexReport:
    library_id: str
    files: list[IndexFileReport] = field(default_factory=list)
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    retried: int = 0

    @property
    def succeeded(self) -> int:
        return sum(1 for f in self.files if f.extracted)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.files if f.failure_state in TERMINAL_FAILURE_STATES)

    @property
    def deferred(self) -> int:
        return sum(1 for f in self.files if f.failure_state == "deferred")


class Pipeline:
    def __init__(self, runtime: PluginRuntime) -> None:
        self.runtime = runtime
        # 提取结果缓存（core/extract_cache.py，2026-09-23 补齐）——只有
        # 编排层自己用（read_document/find_duplicates/index_library），
        # 插件不需要访问，所以不放进 PluginContext，直接归 Pipeline 自己
        # 持有，同"谁需要就给谁配、不无谓扩大插件可见接口"的原则。
        self._extract_cache = ExtractCache(runtime.data_dir / "extracted")
        self._generations = IndexGenerationStore(runtime.data_dir / "index_generations")
        self._manifests = IndexManifestStore(runtime.data_dir / "index_manifests")
        required_points = ("library_manager", "chunker", "embedder", "lexical_index", "vector_store")
        worker_plugin_ids: set[str] = set()
        for point in required_points:
            plugin_id = runtime.registry.active_of(point)
            if plugin_id is not None:
                worker_plugin_ids.add(plugin_id)
        for point in runtime.registry.provider_points():
            if point == "visual_index" or point.startswith("extractor:"):
                worker_plugin_ids.update(runtime.registry.providers_of(point))
        self._index_progress = IndexWorkerManager(
            runtime.plugins_dir,
            runtime.data_dir,
            sorted(worker_plugin_ids),
            active_choices=runtime.registry.active_choices(),
        )
        self._index_progress.set_cleanup_callback(self.discard_index_generation)
        self._note_relations = NoteRelationsStore(runtime.data_dir / "note_relations")
        self._index_failures = IndexFailuresStore(runtime.data_dir / "index_failures")
        self._graph_semantic_cache: tuple[tuple[object, ...], SemanticGraphResponse] | None = None

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
            active = getattr(extractor, "is_active", None)
            if callable(active) and not active():
                continue
            result = extractor.extract(library_id, path, root)
            last_result = result
            if result.text is not None:
                return result
        assert last_result is not None
        return last_result

    def _manifest(self, library_id: str, generation: str | None = None) -> dict | None:
        if generation is None:
            generation = self._generations.active(library_id)
        return self._manifests.read(library_id, generation)

    def _plugin_signature(self, plugin_id: str) -> list[str]:
        plugin = self.runtime.plugins.get(plugin_id)
        version = plugin.manifest.version if plugin is not None and plugin.manifest is not None else ""
        signature = getattr(plugin.instance, "index_signature", None) if plugin is not None else None
        if callable(signature):
            version = str(signature())
        return [plugin_id, version]

    def _extractor_cache_routes(self, extension: str) -> tuple[str, ...]:
        providers = self.runtime.registry.providers_of(f"extractor:{extension}")
        preferred = (
            (
                "official-ocr-mineru-cloud",
                "official-ocr-mineru-local",
                "official-extractor-pdf-text",
            )
            if extension == "pdf"
            else ()
        )
        ordered = [plugin_id for plugin_id in preferred if plugin_id in providers]
        ordered.extend(plugin_id for plugin_id in sorted(providers) if plugin_id not in ordered)
        routes: list[str] = []
        for plugin_id in ordered:
            plugin = self.runtime.plugins.get(plugin_id)
            version = plugin.manifest.version if plugin is not None and plugin.manifest is not None else "0"
            routes.append(f"{plugin_id}:{version}")
        return tuple(routes)

    def _read_extract_cache(
        self,
        library_id: str,
        path: str,
        segments: list[str],
    ) -> str | None:
        extension = Path(path).suffix.lstrip(".").lower()
        routes = self._extractor_cache_routes(extension)
        for segment in reversed(segments):
            if routes:
                text = self._extract_cache.read_preferred(library_id, path, routes, generation=segment)
                if text is None and extension != "pdf":
                    text = self._extract_cache.read(library_id, path, generation=segment)
            else:
                text = self._extract_cache.read(library_id, path, generation=segment)
            if text is not None:
                return text
        return None

    def _pipeline_signatures(self) -> dict[str, list[list[str]]]:
        signatures: dict[str, list[list[str]]] = {}
        for point in self.runtime.registry.provider_points():
            if point == "visual_index" or not point.startswith("extractor:"):
                continue
            signatures[point] = [
                self._plugin_signature(plugin_id)
                for plugin_id in sorted(self.runtime.registry.providers_of(point))
            ]
        for point in ("chunker", "embedder", "lexical_index", "vector_store"):
            plugin_id = self.runtime.registry.active_of(point)
            signatures[point] = [self._plugin_signature(plugin_id)] if plugin_id else []
        # 索引文本管线（wikilink 清洗/锚点拼接，core/text_cleaning.py）：
        # 不属于任何插件，但直接决定嵌入/BM25 的文本内容——逻辑升级必须
        # 使旧 generation 失效，对齐旧项目 META_VERSION 机制
        signatures["text_pipeline"] = [[str(TEXT_PIPELINE_VERSION)]]
        return signatures

    def _extraction_capability_signature(
        self,
        path: str,
        signatures: dict[str, list[list[str]]] | None = None,
    ) -> str:
        extension = Path(path).suffix.lower().lstrip(".")
        active = signatures if signatures is not None else self._pipeline_signatures()
        providers = tuple(
            (point, tuple(tuple(item) for item in active.get(point, [])))
            for point in sorted(active)
            if point == f"extractor:{extension}"
        )
        material: tuple[object, ...] = (extension, providers)
        if extension == "pdf":
            backend = str(self.runtime.settings.get("pdf_scan_backend", "none") or "none")
            credential = bool(str(os.environ.get("MINERU_API_KEY", "") or "").strip())
            material += (backend, credential)
        return hashlib.sha256(repr(material).encode("utf-8")).hexdigest()

    def failure_will_retry(self, record: dict) -> bool:
        status = str(record.get("status") or "")
        if status == "deferred":
            return True
        state = normalize_failure_state(
            str(record.get("failure_state") or record.get("failure_reason") or "")
        )
        if state not in {"scanned", "extract-failed"}:
            return False
        return str(record.get("capability_signature") or "") != self._extraction_capability_signature(
            str(record.get("path") or "")
        )

    @staticmethod
    def _failure_record(
        plan: dict,
        reason: str,
        *,
        state: str | None = None,
    ) -> dict:
        state = state or normalize_failure_state(reason)
        return {
            "size": plan["size"],
            "mtime_ns": plan["mtime_ns"],
            "content_hash": plan["content_hash"],
            "status": "terminal",
            "failure_state": state,
            "failure_reason": state,
            "failure_detail": str(reason) if str(reason) != state else None,
            "capability_signature": plan["capability_signature"],
            "chunk_ids": [],
            "links": [],
        }

    @staticmethod
    def _file_fingerprint(path: Path) -> tuple[int, int, str]:
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return stat.st_size, stat.st_mtime_ns, digest.hexdigest()

    @staticmethod
    def _manifest_files(manifest: dict | None) -> dict[str, dict]:
        files = manifest.get("files", {}) if manifest else {}
        return {
            str(path): dict(record)
            for path, record in files.items()
            if isinstance(path, str) and isinstance(record, dict)
        } if isinstance(files, dict) else {}

    @staticmethod
    def _manifest_segments(manifest: dict | None, field: str, fallback: str | None) -> list[str]:
        values = manifest.get(field, []) if manifest else []
        if isinstance(values, list):
            segments = [str(value) for value in values if value]
            if segments or not fallback:
                return segments
        return [fallback] if fallback else []

    @staticmethod
    def _active_chunk_ids(manifest: dict | None) -> set[str] | None:
        files = Pipeline._manifest_files(manifest)
        if manifest is None:
            return None
        result: set[str] = set()
        for record in files.values():
            if record.get("status") != "indexed":
                continue
            chunk_ids = record.get("chunk_ids", [])
            if isinstance(chunk_ids, list):
                result.update(str(chunk_id) for chunk_id in chunk_ids if chunk_id)
        return result

    def _vector_records(
        self,
        library_id: str,
        chunk_ids: list[str],
        generations: list[str],
    ) -> dict[str, dict]:
        vector_store = self._singleton("vector_store")
        records: dict[str, dict] = {}
        for generation in reversed(generations):
            missing = [chunk_id for chunk_id in chunk_ids if chunk_id not in records]
            if not missing:
                break
            records.update(vector_store.get_by_ids(library_id, missing, generation=generation))
        return records

    def _vector_rows(
        self,
        library_id: str,
        generations: list[str],
        active_ids: set[str] | None,
    ) -> dict[str, dict]:
        vector_store = self._singleton("vector_store")
        rows: dict[str, dict] = {}
        for generation in reversed(generations):
            for chunk_id, row in vector_store.get_all(library_id, generation=generation).items():
                if active_ids is not None and chunk_id not in active_ids:
                    continue
                rows.setdefault(chunk_id, row)
        return rows

    def _query_vector_segments(
        self,
        library_id: str,
        query_vector: list[float],
        top_k: int,
        generations: list[str],
        active_ids: set[str] | None,
    ) -> list[tuple[str, float]]:
        vector_store = self._singleton("vector_store")
        merged: dict[str, float] = {}
        for generation in generations:
            count = vector_store.count(library_id, generation=generation)
            if count <= 0:
                continue
            request = min(count, max(top_k * 4, 32))
            while True:
                hits = vector_store.query(
                    library_id,
                    query_vector,
                    top_k=request,
                    generation=generation,
                )
                current = {
                    chunk_id: score
                    for chunk_id, score in hits
                    if active_ids is None or chunk_id in active_ids
                }
                for chunk_id, score in current.items():
                    merged[chunk_id] = max(score, merged.get(chunk_id, score))
                if len(current) >= top_k or request >= count:
                    break
                request = min(count, max(request * 2, top_k + 1))
        return sorted(merged.items(), key=lambda item: item[1], reverse=True)[:top_k]

    # ---- 索引态 --------------------------------------------------------

    def index_library(
        self,
        library_id: str,
        *,
        generation_id: str | None = None,
        full: bool = False,
        progress_callback: Callable[[IndexProgressEvent], None] | None = None,
        format_allowlist: tuple[str, ...] | None = None,
    ) -> IndexReport:
        """`progress_callback(event)` 可选——每进入一个真实索引阶段、完成
        一个文件时调用一次，供 `core/index_progress.py::IndexWorkerManager`
        在工作进程中上报进度。不传就是
        原有的纯同步调用，行为完全不变——GUI/测试目前都是这样直接调用，
        不强制迁移到后台执行那条路径。"""
        lib_mgr = self._singleton("library_manager")
        cfg = lib_mgr.store.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        root = Path(cfg.root_path)

        chunker = self._singleton("chunker")
        embedder = self._singleton("embedder")
        lexical = self._singleton("lexical_index")
        vector_store = self._singleton("vector_store")

        generation = generation_id or uuid.uuid4().hex
        previous = None if full else self._generations.active(library_id)
        old_manifest = self._manifest(library_id, previous)
        old_files = self._manifest_files(old_manifest)
        signatures = self._pipeline_signatures()
        old_signatures = old_manifest.get("signatures", {}) if old_manifest else {}
        force_extract = full
        if old_manifest is not None:
            force_extract = force_extract or any(
                old_signatures.get(point) != signatures.get(point)
                for point in signatures
                if point.startswith("extractor:")
            )
        force_chunks = force_extract or (
            old_manifest is not None
            and (
                old_signatures.get("chunker") != signatures.get("chunker")
                or old_signatures.get("text_pipeline") != signatures.get("text_pipeline")
            )
        )
        force_embed = force_chunks or (
            old_manifest is not None
            and (
                old_signatures.get("embedder") != signatures.get("embedder")
                or old_signatures.get("vector_store") != signatures.get("vector_store")
            )
        )
        force_lexical = old_manifest is not None and old_signatures.get("lexical_index") != signatures.get("lexical_index")

        report = IndexReport(library_id=library_id)
        included_files = (
            lib_mgr.resolve_included_files(library_id)
            if format_allowlist is None
            else lib_mgr.resolve_included_files(
                library_id,
                format_allowlist=format_allowlist,
            )
        )
        files_total = len(included_files)
        files_done = 0
        chunks_done = 0
        plans: list[dict] = []
        current_alive_paths: set[str] = set()

        def _emit(
            phase: str,
            *,
            current_path: str = "",
            chunks_total: int | None = None,
            stall_grace_s: float = 0.0,
            message: str = "",
        ) -> None:
            if progress_callback is not None:
                progress_callback(
                    IndexProgressEvent(
                        phase=phase,
                        files_done=files_done,
                        files_total=files_total,
                        current_path=current_path,
                        chunks_done=chunks_done,
                        chunks_total=chunks_total,
                        message=message,
                        stall_grace_s=stall_grace_s,
                    )
                )

        _emit("scanning", message=f"发现 {files_total} 个文件")
        for path, included, reason in included_files:
            old_record = old_files.get(path, {})
            plan = {
                "path": path,
                "included": included,
                "reason": reason,
                "old": old_record,
                "size": -1,
                "mtime_ns": -1,
                "content_hash": "",
                "fingerprint_error": "",
                "capability_signature": self._extraction_capability_signature(path, signatures),
                "action": "excluded" if not included else "added",
            }
            if not included:
                if (
                    format_allowlist is not None
                    and old_record
                    and ("." + path.rsplit(".", 1)[-1].lower()) not in {str(v).lower() for v in format_allowlist}
                ):
                    # Agent 未授权格式：冻结——对齐 obsidian-rag/index.py:2058-2066
                    # （"保留既有条目与块（不裁剪不清理），零 I/O、不转换、不计
                    # 变更"；无条目则视同不存在，待人类路径首建）。撤销授权不得
                    # 把未授权文件当"已删除"清掉旧索引；记录原样保留（含旧
                    # mtime），重新授权后 stat 未变 → unchanged，无需重新提取。
                    plan["action"] = "frozen"
                    plan["record"] = dict(old_record)
                    current_alive_paths.add(path)
                plans.append(plan)
                continue
            current_alive_paths.add(path)
            try:
                stat = (root / path).stat()
                plan["size"] = stat.st_size
                plan["mtime_ns"] = stat.st_mtime_ns
            except OSError as exc:
                plan["fingerprint_error"] = f"读取文件状态失败：{type(exc).__name__}: {exc}"
            old = plan["old"]
            if not plan["fingerprint_error"] and old:
                same_stat = (
                    plan["size"] == old.get("size")
                    and plan["mtime_ns"] == old.get("mtime_ns")
                )
                if same_stat and old.get("content_hash"):
                    plan["content_hash"] = str(old["content_hash"])
                else:
                    try:
                        plan["size"], plan["mtime_ns"], plan["content_hash"] = self._file_fingerprint(root / path)
                    except OSError as exc:
                        plan["fingerprint_error"] = f"读取文件失败：{type(exc).__name__}: {exc}"
                if old.get("status") == "indexed":
                    if force_extract or force_chunks or force_embed:
                        plan["action"] = "rebuilt"
                    elif same_stat or plan["content_hash"] == old.get("content_hash"):
                        plan["action"] = "unchanged"
                    else:
                        plan["action"] = "changed"
                else:
                    old_state = normalize_failure_state(
                        str(old.get("failure_state") or old.get("failure_reason") or "")
                    )
                    capability_changed = (
                        old_state in {"scanned", "extract-failed"}
                        and str(old.get("capability_signature") or "")
                        != str(plan["capability_signature"])
                    )
                    stable_terminal = (
                        old.get("status") in {"failed", "terminal"}
                        and old_state not in {"scanned", "extract-failed"}
                        and not (old_state == "unreadable" and not plan["fingerprint_error"])
                    ) or (
                        old.get("status") in {"failed", "terminal"}
                        and not capability_changed
                        and str(old.get("capability_signature") or "") == str(plan["capability_signature"])
                    )
                    if full or old.get("status") == "deferred" or not stable_terminal:
                        plan["action"] = "retried"
                    elif same_stat or plan["content_hash"] == old.get("content_hash"):
                        plan["action"] = "unchanged"
                    else:
                        plan["action"] = "retried" if old.get("status") != "indexed" else "changed"
            elif not plan["fingerprint_error"]:
                try:
                    plan["size"], plan["mtime_ns"], plan["content_hash"] = self._file_fingerprint(root / path)
                except OSError as exc:
                    plan["fingerprint_error"] = f"读取文件失败：{type(exc).__name__}: {exc}"
            plan["needs_source"] = plan["action"] in {"added", "changed", "retried"} or force_extract
            plan["needs_chunks"] = plan["needs_source"] or force_chunks or force_embed
            plan["needs_embed"] = plan["needs_chunks"]
            plans.append(plan)

        removed_paths = set(old_files) - current_alive_paths
        report.removed = len(removed_paths)
        report.added = sum(1 for plan in plans if plan["action"] == "added")
        report.changed = sum(1 for plan in plans if plan["action"] in {"changed", "rebuilt"})
        report.unchanged = sum(1 for plan in plans if plan["action"] == "unchanged")
        report.retried = sum(1 for plan in plans if plan["action"] == "retried")

        vector_segments = self._manifest_segments(old_manifest, "vector_segments", previous)
        extract_segments = self._manifest_segments(old_manifest, "extract_segments", previous)
        cache_segments = list(extract_segments)
        lexical_segments = self._manifest_segments(old_manifest, "lexical_segments", previous)
        if force_embed:
            vector_segments = []
        if force_extract:
            extract_segments = []
        needs_vector_segment = any(plan.get("needs_embed") for plan in plans)
        needs_extract_segment = any(plan.get("needs_source") for plan in plans)
        lexical_changed = force_lexical or bool(removed_paths) or any(
            bool(plan.get("needs_chunks")) for plan in plans
        )
        lexical_current = False
        if lexical_changed:
            source_lexical = lexical_segments[-1] if lexical_segments else ""
            if hasattr(lexical, "delete_generation"):
                lexical.delete_generation(library_id, generation)
            if source_lexical and not force_lexical:
                if not hasattr(lexical, "export_state") or not hasattr(lexical, "import_state"):
                    raise PipelineError("当前 lexical_index 不支持增量复制")
                lexical.import_state(library_id, lexical.export_state(library_id, generation=source_lexical), generation=generation)
            if generation not in lexical_segments:
                lexical_segments.append(generation)
            lexical_current = True
            if not force_lexical:
                for removed_path in removed_paths:
                    for chunk_id in old_files.get(removed_path, {}).get("chunk_ids", []):
                        lexical.remove_chunk(library_id, chunk_id, generation=generation)

        if needs_vector_segment and generation not in vector_segments:
            vector_segments.append(generation)
        if needs_extract_segment and generation not in extract_segments:
            extract_segments.append(generation)

        def _drop_old_lexical_chunks(plan: dict) -> None:
            """丢弃该文件的旧词法块——只允许在本轮结果已确定"不是 deferred"
            的丢弃/替换点调用。对齐 obsidian-rag 的语义：deferred 文件的旧块
            原样保留继续服务（index.py:2142-2148 "不动 meta"），终态失败与
            成功重写才清掉旧块（旧项目由 meta[rel]["chunks"]=0 驱动清理）。"""
            if lexical_current and plan["needs_chunks"]:
                for chunk_id in plan["old"].get("chunk_ids", []):
                    lexical.remove_chunk(library_id, chunk_id, generation=generation)

        for plan in plans:
            path = str(plan["path"])
            file_report = IndexFileReport(
                path=path,
                included=bool(plan["included"]),
                reason=str(plan["reason"]),
                action=str(plan["action"]),
            )
            report.files.append(file_report)
            if not plan["included"]:
                files_done += 1
                _emit(
                    "file_complete",
                    current_path=path,
                    message=(
                        f"已冻结（保留旧索引）：{path}"
                        if plan["action"] == "frozen"
                        else f"已跳过：{path}"
                    ),
                )
                continue
            old = plan["old"]
            action = str(plan["action"])
            if action == "unchanged" and not plan["needs_chunks"]:
                record = dict(old)
                record["size"] = plan["size"]
                record["mtime_ns"] = plan["mtime_ns"]
                record["content_hash"] = plan["content_hash"] or old.get("content_hash", "")
                plan["record"] = record
                file_report.extracted = record.get("status") == "indexed"
                file_report.failure_state = str(record.get("failure_state")) if record.get("failure_state") else None
                file_report.capability_signature = str(record.get("capability_signature")) if record.get("capability_signature") else None
                file_report.chunk_count = len(record.get("chunk_ids", []))
                files_done += 1
                chunks_done += file_report.chunk_count
                _emit("file_complete", current_path=path, message=f"未变化：{path}")
                continue
            if plan["fingerprint_error"]:
                reason = str(plan["fingerprint_error"])
                _drop_old_lexical_chunks(plan)
                record = self._failure_record(plan, reason, state="unreadable")
                plan["record"] = record
                file_report.extract_failure = reason
                file_report.failure_state = str(record["failure_state"])
                file_report.capability_signature = str(record["capability_signature"])
                files_done += 1
                _emit("file_complete", current_path=path, message=f"索引失败：{reason}")
                continue

            # 旧块的词法删除已迁移到各确定丢弃点（_drop_old_lexical_chunks）：
            # 提前删除会把 deferred 文件的旧块从可检索集合里丢掉，违反
            # obsidian-rag/index.py:2142-2148 的"不动 meta"语义。
            doc: ExtractedDocument | None = None
            if plan["needs_source"]:
                if plan["action"] in {"unchanged", "rebuilt"} and plan["content_hash"] == old.get("content_hash"):
                    cached = self._read_extract_cache(library_id, path, cache_segments)
                    if cached is not None:
                        doc = ExtractedDocument(
                            library_id=library_id,
                            path=path,
                            text=cached,
                            failure_reason=None,
                            extracted_by=str(old.get("extractor_id", "core.pipeline")),
                            extractor_version=str(old.get("extractor_version", "-")),
                            content_hash=str(plan["content_hash"] or old.get("content_hash", "")),
                        )
                        self._extract_cache.write(
                            library_id,
                            path,
                            cached,
                            generation,
                            route=f"{doc.extracted_by}:{doc.extractor_version}",
                        )
                if doc is None:
                    _emit("extracting", current_path=path, stall_grace_s=300.0, message="正在提取：{path}")
                    doc = self._extract(library_id, path, root)
                    if doc.text is not None:
                        self._extract_cache.write(
                            library_id,
                            path,
                            doc.text,
                            generation,
                            route=f"{doc.extracted_by}:{doc.extractor_version}",
                        )

            else:
                cached = self._read_extract_cache(library_id, path, extract_segments)
                if cached is not None:
                    doc = ExtractedDocument(
                        library_id=library_id,
                        path=path,
                        text=cached,
                        failure_reason=None,
                        extracted_by=str(old.get("extractor_id", "core.pipeline")),
                        extractor_version=str(old.get("extractor_version", "-")),
                        content_hash=str(plan["content_hash"] or old.get("content_hash", "")),
                    )
                if doc is None:
                    _emit("extracting", current_path=path, stall_grace_s=300.0, message=f"正在重新提取：{path}")
                    doc = self._extract(library_id, path, root)
                    if doc.text is not None:
                        self._extract_cache.write(
                            library_id,
                            path,
                            doc.text,
                            generation,
                            route=f"{doc.extracted_by}:{doc.extractor_version}",
                        )
            if doc is None or doc.text is None:
                reason = str(plan["fingerprint_error"] or (doc.failure_reason if doc else "无法读取提取缓存"))
                is_deferred = reason.strip().lower() == "deferred" or reason.strip().lower().startswith("deferred:")
                if is_deferred:
                    # 对齐 obsidian-rag/index.py:2142-2148（R3b）：本地服务瞬态
                    # 不可用 → 本轮跳过——不落终态、不动 manifest 记录、不计
                    # changed；旧条目与旧块原样保留继续服务（检索不受影响），
                    # 文件 stat 与旧记录的差异驱动下一轮 stale → 自动重试。
                    # 无旧条目则视同本轮不存在（也不写记录），同旧项目。
                    if plan["old"]:
                        plan["record"] = dict(plan["old"])
                    file_report.extract_failure = reason
                    file_report.failure_state = "deferred"
                    files_done += 1
                    _emit(
                        "file_complete",
                        current_path=path,
                        message="本轮延后：" + reason,
                    )
                    continue
                state = str(doc.failure_state) if doc is not None and doc.failure_state else None
                _drop_old_lexical_chunks(plan)
                record = self._failure_record(
                    plan,
                    reason,
                    state=state,
                )
                plan["record"] = record
                file_report.extract_failure = reason
                file_report.failure_state = str(record["failure_state"])
                file_report.capability_signature = str(record["capability_signature"])
                files_done += 1
                _emit(
                    "file_complete",
                    current_path=path,
                    message=f"索引失败：{reason}",
                )
                continue
            if is_tbd_heavy(
                doc.text,
                float(self.runtime.settings.get("tbd_exclude_ratio", 0.1) or 0.0),
            ):
                reason = "tbd"
                _drop_old_lexical_chunks(plan)
                record = self._failure_record(plan, reason, state="tbd")
                record["content_hash"] = doc.content_hash or plan["content_hash"]
                record["links"] = extract_wikilink_targets(doc.text)
                plan["record"] = record
                file_report.extract_failure = reason
                file_report.failure_state = "tbd"
                file_report.capability_signature = str(record["capability_signature"])
                files_done += 1
                _emit("file_complete", current_path=path, message="已跳过占位重文件：tbd")
                continue
            # ---- 索引文本管线（core/text_cleaning.py，对齐旧 _store_chunks 的
            # 清洗顺序）——链接先从原文抽取（note_relations 的语义是读者看到的
            # 原始链接，问题28），再拆 frontmatter、清洗 wikilink（问题15/F9）。
            # 清洗只影响切块/嵌入/BM25 的文本；提取缓存与 read_document 交付的
            # 原文不动（对齐旧项目"只动索引层"的决策）。
            raw_text = doc.text or ""
            raw_links = extract_wikilink_targets(raw_text)
            source_ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if source_ext in ("md", "txt", "markdown"):
                front, body = extract_frontmatter(raw_text)
            else:
                front, body = {}, raw_text
            index_text = clean_wikilinks(body)
            doc_parts = [p for p in (Path(path).stem, front.get("title", ""), front.get("tags", "")) if p]
            if index_text != raw_text:
                doc = dataclasses_replace(doc, text=index_text)
            if not plan["needs_chunks"]:
                record = dict(old)
                record["size"] = plan["size"]
                record["mtime_ns"] = plan["mtime_ns"]
                record["content_hash"] = doc.content_hash or plan["content_hash"]
                plan["record"] = record
                file_report.extracted = record.get("status") == "indexed"
                file_report.chunk_count = len(record.get("chunk_ids", []))
                files_done += 1
                chunks_done += file_report.chunk_count
                _emit("file_complete", current_path=path, message=f"已完成：{path}")
                continue

            chunks = chunker.chunk(doc)
            # 文件级锚点 + 标题链拼进每块文本（问题18/审计 F20 的 v6 决策）：
            # 嵌入与 BM25 同受益；ctx 存 metadata 供检索输出剥离（旧项目同款）。
            ctx_by_id: dict[str, str] = {}
            for _chunk_index, _chunk in enumerate(chunks):
                _ctx = build_anchor_context(doc_parts, _chunk.heading_breadcrumb)
                if _ctx:
                    ctx_by_id[_chunk.chunk_id] = _ctx
                    chunks[_chunk_index] = dataclasses_replace(
                        _chunk, text=_ctx + "\n" + _chunk.text
                    )
            section_counts: dict[str, int] = {}
            section_headings: dict[str, str] = {}
            section_texts: dict[str, str] = {}
            for chunk in chunks:
                if not chunk.section_id:
                    continue
                section_counts[chunk.section_id] = section_counts.get(chunk.section_id, 0) + 1
                section_headings.setdefault(chunk.section_id, chunk.heading_breadcrumb)
                section_texts.setdefault(chunk.section_id, chunk.section_text)
            if not chunks:
                reason = "提取成功但没有产出任何chunk"
                _drop_old_lexical_chunks(plan)
                record = self._failure_record(plan, reason, state="empty")
                record["content_hash"] = doc.content_hash or plan["content_hash"]
                record["links"] = raw_links
                plan["record"] = record
                file_report.extract_failure = reason
                file_report.failure_state = "empty"
                file_report.capability_signature = str(record["capability_signature"])
                files_done += 1
                _emit("file_complete", current_path=path, message=f"索引失败：{reason}")
                continue
            _emit("embedding", current_path=path, stall_grace_s=300.0, message=f"正在嵌入：{path}")
            vectors = embedder.embed_chunks(chunks)
            vector_by_id = {vector.chunk_id: list(vector.vector) for vector in vectors}
            chunk_ids = [chunk.chunk_id for chunk in chunks]
            embed_vectors = [vector_by_id[chunk_id] for chunk_id in chunk_ids]
            _emit("writing", current_path=path, stall_grace_s=180.0, message=f"正在写入：{path}")
            _drop_old_lexical_chunks(plan)
            vector_store.upsert(
                library_id,
                chunk_ids,
                embed_vectors,
                documents=[chunk.text for chunk in chunks],
                metadatas=[
                    {
                        "path": chunk.path,
                        "heading_breadcrumb": chunk.heading_breadcrumb,
                        "chunk_index": chunk.chunk_index,
                        "section_id": chunk.section_id,
                        "ctx": ctx_by_id.get(chunk.chunk_id, ""),
                    }
                    for chunk in chunks
                ],
                generation=generation,
            )
            if lexical_current:
                for chunk in chunks:
                    lexical.index_chunk(chunk, generation=generation)
            record = {
                "size": plan["size"],
                "mtime_ns": plan["mtime_ns"],
                "content_hash": doc.content_hash or plan["content_hash"],
                "status": "indexed",
                "failure_state": None,
                "failure_reason": None,
                "failure_detail": None,
                "capability_signature": plan["capability_signature"],
                "extractor_id": doc.extracted_by,
                "extractor_version": doc.extractor_version,
                "chunker_id": chunks[0].chunked_by,
                "chunker_version": chunks[0].chunker_version,
                "embedder_id": vectors[0].model_id,
                "embedder_version": vectors[0].model_version,
                "dim": vectors[0].dim,
                "chunk_ids": chunk_ids,
                "sections": {
                    section_id: {
                        "heading": section_headings[section_id],
                        "text": section_texts[section_id],
                        "chunk_count": section_counts[section_id],
                    }
                    for section_id in section_counts
                },
                "links": raw_links,
            }
            plan["record"] = record
            file_report.extracted = True
            file_report.chunk_count = len(chunks)
            files_done += 1
            chunks_done += len(chunks)
            _emit("file_complete", current_path=path, message=f"已完成：{path}")

        if lexical_current:
            if force_lexical:
                if hasattr(lexical, "delete_generation"):
                    lexical.delete_generation(library_id, generation)
                active_ids = self._active_chunk_ids({"files": {str(plan["path"]): plan.get("record", {}) for plan in plans if plan.get("record")}})
                records = self._vector_records(library_id, sorted(active_ids or set()), vector_segments)
                total_by_path: dict[str, int] = {}
                for record in records.values():
                    meta = record.get("metadata") or {}
                    path = str(meta.get("path", ""))
                    index = int(meta.get("chunk_index", 0))
                    total_by_path[path] = max(total_by_path.get(path, 0), index + 1)
                for chunk_id, record in sorted(records.items()):
                    meta = record.get("metadata") or {}
                    path = str(meta.get("path", ""))
                    state = next((plan.get("record", {}) for plan in plans if plan.get("path") == path), {})
                    lexical.index_chunk(
                        Chunk(
                            chunk_id=chunk_id,
                            library_id=library_id,
                            path=path,
                            chunk_index=int(meta.get("chunk_index", 0)),
                            total_chunks=total_by_path.get(path, 1),
                            text=str(record.get("document") or ""),
                            heading_breadcrumb=str(meta.get("heading_breadcrumb", "")),
                            chunked_by=str(state.get("chunker_id", "core.pipeline")),
                            chunker_version=str(state.get("chunker_version", "-")),
                        ),
                        generation=generation,
                    )
            if hasattr(lexical, "save"):
                _emit("writing", stall_grace_s=180.0, message="正在保存词法索引")
                lexical.save(library_id, generation=generation)

        manifest_files = {
            str(plan["path"]): dict(plan["record"])
            for plan in plans
            if plan.get("record") and (plan.get("included") or plan.get("action") == "frozen")
        }
        active_ids = self._active_chunk_ids({"files": manifest_files})
        compacted_state = bool(old_manifest and old_manifest.get("compacted", False))
        compacted_vector_segment = False
        compaction_due = callable(getattr(vector_store, "get_all", None)) and (
            len(vector_segments) >= 3
            or bool(old_manifest is not None and not old_manifest.get("compacted", False))
        )
        if compaction_due:
            compact_segment = f"{generation}-compact"
            if active_ids:
                _emit("writing", stall_grace_s=180.0, message="正在压缩向量索引")
                rows = self._vector_rows(library_id, vector_segments, active_ids)
                compact_ids = sorted(rows)
                for start in range(0, len(compact_ids), 1000):
                    batch = compact_ids[start : start + 1000]
                    vector_store.upsert(
                        library_id,
                        batch,
                        [rows[chunk_id]["embedding"] for chunk_id in batch],
                        documents=[rows[chunk_id]["document"] for chunk_id in batch],
                        metadatas=[rows[chunk_id]["metadata"] for chunk_id in batch],
                        generation=compact_segment,
                    )
                vector_segments = [compact_segment]
                compacted_vector_segment = True
            else:
                vector_segments = []
            compacted_extract_segments: list[str] = []
            compacted_any = False
            for path, record in manifest_files.items():
                if record.get("status") != "indexed":
                    continue
                seen_routes: set[str] = set()
                for segment in reversed(extract_segments):
                    for text, route in self._extract_cache.iter_entries(library_id, path, segment):
                        route_key = route or "legacy"
                        if route_key in seen_routes:
                            continue
                        seen_routes.add(route_key)
                        self._extract_cache.write(
                            library_id,
                            path,
                            text,
                            compact_segment,
                            route=route,
                        )
                        compacted_any = True
            if compacted_any:
                compacted_extract_segments.append(compact_segment)
            if lexical_segments and hasattr(lexical, "export_state") and hasattr(lexical, "import_state"):
                lexical.import_state(
                    library_id,
                    lexical.export_state(library_id, generation=lexical_segments[-1]),
                    generation=compact_segment,
                )
                compacted_lexical_segments = [compact_segment]
            else:
                compacted_lexical_segments = []
            extract_segments = compacted_extract_segments
            lexical_segments = compacted_lexical_segments
            compacted_state = True
        # 冻结（Agent 未授权）的 PDF 对齐 obsidian-rag/wemm_indexer.py:190
        # （"仅处理这些格式的 PDF，其余冻结"）：保留在有效集合里（页不被
        # 屏蔽），但不进 changed_paths（不重渲染）。
        pdf_paths = [
            str(plan["path"])
            for plan in plans
            if (plan.get("included") or plan.get("action") == "frozen")
            and str(plan["path"]).lower().endswith(".pdf")
        ]
        changed_pdf_paths = [
            str(plan["path"])
            for plan in plans
            if plan.get("included")
            and str(plan["path"]).lower().endswith(".pdf")
            and plan.get("action") not in {"unchanged", "frozen"}
        ]
        _emit("visual", chunks_total=chunks_done, stall_grace_s=300.0, message="正在建立视觉索引")
        for plugin_id in sorted(self.runtime.registry.providers_of("visual_index")):
            visual = self._plugin(plugin_id)
            visual.index_library(
                library_id,
                root,
                pdf_paths,
                generation=generation,
                changed_paths=changed_pdf_paths,
                previous_generation=previous,
            )

        links_by_path = {
            path: [str(link) for link in record.get("links", [])]
            for path, record in manifest_files.items()
            if record.get("status") == "indexed"
        }
        _emit("finalizing", chunks_total=chunks_done, message="正在完成索引")
        self._note_relations.write_library(library_id, links_by_path, generation)
        failures = [
            {
                "path": path,
                "reason": str(record.get("failure_state") or record.get("failure_reason") or "extract-failed"),
                "detail": record.get("failure_detail"),
                "capability_signature": record.get("capability_signature"),
                "will_retry": self.failure_will_retry({"path": path, **record}),
            }
            for path, record in manifest_files.items()
            if record.get("status") in {"failed", "terminal"}
        ]
        self._index_failures.write_library(
            library_id,
            succeeded=report.succeeded,
            failures=failures,
            generation=generation,
        )
        manifest = {
            "format_version": INDEX_MANIFEST_VERSION,
            "library_id": library_id,
            "generation": generation,
            "previous_generation": previous,
            "signatures": signatures,
            "files": manifest_files,
            "vector_segments": vector_segments,
            "extract_segments": extract_segments,
            "lexical_segments": lexical_segments,
            "active_chunk_ids": sorted(active_ids or set()),
            "compacted": compacted_state,
        }
        if not self._manifests.write(manifest):
            self.discard_index_generation(library_id, generation)
            raise PipelineError("索引清单写入失败，旧索引保持不变")
        known_generations = set(self._manifests.list_generations(library_id))
        if previous:
            known_generations.add(previous)
        if not self._generations.commit(library_id, generation):
            self.discard_index_generation(library_id, generation)
            raise PipelineError("索引数据已生成，但发布 generation 失败，旧索引保持不变")
        if compacted_vector_segment:
            vector_store.delete_generation(library_id, generation)
        keep_generations = {generation, *self._generations.history(library_id)}
        referenced_generations = self._manifests.referenced_generations(library_id, [generation])
        candidates = set(known_generations)
        for known_generation in list(candidates):
            data = self._manifests.read(library_id, known_generation)
            if data:
                for field in ("vector_segments", "extract_segments", "lexical_segments"):
                    values = data.get(field, [])
                    if isinstance(values, list):
                        candidates.update(str(value) for value in values if value)
        for candidate in sorted(candidates - keep_generations - referenced_generations):
            self.discard_index_generation(library_id, candidate)
            self._manifests.clear(library_id, candidate)
        return report


    def discard_index_generation(self, library_id: str, generation: str) -> None:
        if self._generations.active(library_id) == generation:
            return
        self._extract_cache.clear_library(library_id, generation)
        self._note_relations.clear_generation(library_id, generation)
        self._index_failures.clear_generation(library_id, generation)
        vector_plugin = self.runtime.registry.active_of("vector_store") or ""
        visual_plugins = set(self.runtime.registry.providers_of("visual_index"))
        for plugin_id in sorted(
            {
                self.runtime.registry.active_of("lexical_index") or "",
                vector_plugin,
                *visual_plugins,
            }
        ):
            if not plugin_id:
                continue
            plugin = self._plugin(plugin_id)
            cleanup = getattr(plugin, "delete_generation", None)
            if cleanup is None:
                continue
            targets = [generation]
            if plugin_id == vector_plugin or plugin_id in visual_plugins:
                targets.append(f"{generation}-compact")
            for target in targets:
                try:
                    cleanup(library_id, target)
                except Exception:
                    pass

    def index_failures(self, library_id: str) -> dict | None:
        """索引失败溯源（只读诊断，对齐 obsidian-rag 的 `index_failures` 工具）：列出
        最近一次 `index_library()` 跑完后，库内"没转成/没索引上"的文件
        及原因。库存在但从没索引过时返回 `None`（不是错误）。"""
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        return self._index_failures.read(library_id, self._generations.active(library_id))

    def note_relations(self, library_id: str, path: str) -> dict:
        """双链关系查询（对齐 obsidian-rag 的 `note_relations` 工具）：
        给定笔记标识（库内相对路径，或不含扩展名的标题），返回其出链
        （本文链接到谁）与入链（谁链接到本文），基于最近一次
        `index_library()` 记录的 `[[wikilink]]` 目标现算——只存出链，
        入链永远现算，见 `core/note_relations.py` 模块 docstring。

        库不存在会报错（同其他工具一致的"未知库"处理）；库存在但从没
        索引过、或指定的笔记不存在/找不到，都返回 `resolved=False`，
        不是错误，调用方自己决定怎么展示这两种"没有关系数据"的情况。
        """
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        return self._note_relations.resolve(library_id, path, self._generations.active(library_id))

    def graph(self, libraries: str = "all") -> GraphResponse:
        lib_mgr = self._singleton("library_manager")
        entries = lib_mgr.resolve_libraries(libraries or "all")
        library_ids = tuple(entry.library_id for entry in entries)
        manifests: dict[str, dict | None] = {}
        relation_edges: dict[str, tuple[tuple[str, str], ...]] = {}
        included_files: dict[str, tuple[tuple[str, bool, str], ...]] = {}
        page_states: dict[str, tuple[VisualPageState, ...]] = {}
        for library_id in library_ids:
            generation = self._generations.active(library_id)
            manifests[library_id] = self._manifest(library_id, generation)
            relation_edges[library_id] = self._note_relations.resolved_edges(library_id, generation)
            included_files[library_id] = tuple(lib_mgr.resolve_included_files(library_id))
            states: list[VisualPageState] = []
            for plugin_id in sorted(self.runtime.registry.providers_of("visual_index")):
                try:
                    visual = self._plugin(plugin_id)
                    page_reader = getattr(visual, "graph_page_states", None)
                    generated = page_reader(library_id, generation) if generation and callable(page_reader) else ()
                    states.extend(state for state in generated if isinstance(state, VisualPageState))
                except Exception:
                    continue
            page_states[library_id] = tuple(states)
        return build_graph(
            library_ids=library_ids,
            manifests=manifests,
            relation_edges=relation_edges,
            included_files=included_files,
            page_states=page_states,
        )

    def graph_semantic_edges(
        self,
        libraries: str = "all",
        *,
        threshold: float = 0.62,
    ) -> SemanticGraphResponse:
        response = self.graph(libraries)
        nodes = [
            node
            for node in response.nodes
            if node.node_type in {"md", "txt", "docx"}
            or (node.node_type == "pdf" and node.chunks > 0)
        ]
        if len(nodes) < 3:
            return SemanticGraphResponse(edges=())
        plugin_id = self.runtime.registry.active_of("embedder") or ""
        signature = tuple(tuple(value) for value in self._plugin_signature(plugin_id))
        key = (
            tuple((node.node_id, node.updated_ns, node.chunks, node.failure_reason) for node in nodes),
            float(threshold),
            signature,
        )
        if self._graph_semantic_cache is not None and self._graph_semantic_cache[0] == key:
            return self._graph_semantic_cache[1]
        texts = [f"{Path(node.path).stem} {node.path}" for node in nodes]
        try:
            vectors = self._singleton("embedder").embed_texts(texts)
            if len(vectors) != len(nodes) or any(len(vector) == 0 for vector in vectors):
                raise ValueError("嵌入结果数量或维度无效")
            result = SemanticGraphResponse(
                edges=select_semantic_edges(
                    [node.node_id for node in nodes],
                    vectors,
                    threshold=threshold,
                )
            )
        except Exception as exc:
            return SemanticGraphResponse(
                edges=(),
                error=f"语义边计算失败：{type(exc).__name__}",
            )
        self._graph_semantic_cache = (key, result)
        return result

    def start_index_library(
        self,
        library_id: str,
        source: str = "api",
        full: bool = False,
        *,
        format_allowlist: tuple[str, ...] | None = None,
    ) -> IndexStartResult:
        """后台重建索引——对齐 obsidian-rag 的 `reindex_knowledge`"后台
        执行、立即返回"语义。真正的索引逻辑仍是 `index_library()`，由独立
        worker 进程执行并上报进度。

        返回值仍可按 `(started, message)` 两值解包；库不存在时提前校验并
        直接抛 `KeyError`。
        """
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        self._index_progress.set_active_choices(self.runtime.registry.active_choices())
        if format_allowlist is None:
            if full:
                return self._index_progress.start(library_id, source, full=True)
            return self._index_progress.start(library_id, source)
        return self._index_progress.start(
            library_id,
            source,
            full=full,
            format_allowlist=format_allowlist,
        )

    def stop_index_library(self, library_id: str, run_id: str = "") -> tuple[bool, str]:
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        return self._index_progress.stop(library_id, run_id)

    def index_status(self, library_id: str) -> dict | None:
        """查询索引进度——对齐 obsidian-rag 的 `index_status` 工具。返回
        `None` 表示这个库从没跑过（后台）索引，调用方自己决定怎么展示
        "从没跑过"和"跑过但已完成/失败"的区别。"""
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        return self._index_progress.status(library_id)

    def has_index(self, library_id: str) -> bool:
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        generation = self._generations.active(library_id)
        return generation is not None and self._manifest(library_id, generation) is not None

    def library_freshness(
        self,
        libraries: str = "all",
        *,
        exclude: str = "",
        format_allowlist: Mapping[str, tuple[str, ...]] | None = None,
    ) -> dict[str, LibraryFreshness]:
        """单库 freshness 扫描——对齐 obsidian-rag/index.py::kb_stale 返回的
        (stale, stats) 形状：

        - 库路径不存在（不是目录）→ stale 且 `missing=True`
          （index.py:1506-1508）——"本轮看不到"不等于"确认删除"，消费方
          （MCP 搜索前自动同步）据此跳过同步并保留旧索引；
        - 目录存在但扫不到任何文件、而 manifest 有真实记录 → stale 且
          `emptied=True`（index.py:1511-1517，2026-08-14 审计 F16：源文件
          没放回去 ≠ 用户删光，同步 = 不可逆清空）；
        - 从未索引过且扫不到任何文件 → 收敛态，不判 stale
          （index.py:1522-1529），避免每轮无效重建。

        显式触发的索引（GUI 完整重建、reindex_knowledge）没有这层保护，
        与旧项目一致：用户明确要求重建时按字面执行。"""
        lib_mgr = self._singleton("library_manager")
        entries = lib_mgr.resolve_libraries(libraries or "all", exclude)
        report: dict[str, LibraryFreshness] = {}
        for entry in entries:
            library_id = entry.library_id
            root = Path(entry.root_path)
            if not root.is_dir():
                report[library_id] = LibraryFreshness(
                    library_id=library_id, stale=True, missing=True
                )
                continue
            generation = self._generations.active(library_id)
            manifest = self._manifest(library_id, generation)
            records = self._manifest_files(manifest) if manifest is not None else {}
            allowlist = (
                format_allowlist.get(library_id, ())
                if format_allowlist is not None
                else None
            )
            decisions = (
                lib_mgr.resolve_included_files(library_id)
                if allowlist is None
                else lib_mgr.resolve_included_files(
                    library_id,
                    format_allowlist=allowlist,
                )
            )
            has_files = bool(decisions)
            if generation is None or manifest is None:
                # 旧项目 kb_stale：无 meta 时有文件=首跑待建；条目与文件双空=收敛
                report[library_id] = LibraryFreshness(
                    library_id=library_id, stale=has_files
                )
                continue
            if not has_files and records:
                report[library_id] = LibraryFreshness(
                    library_id=library_id, stale=True, emptied=True
                )
                continue
            included = {
                path: included
                for path, included, _reason in decisions
                if included
            }
            allowed = (
                {str(value).lower() for value in allowlist}
                if allowlist is not None
                else None
            )
            scoped_paths = {
                path
                for path in included
                if allowed is None
                or ("." + path.rsplit(".", 1)[-1].lower()) in allowed
            }
            record_paths = {
                path
                for path in records
                if allowed is None
                or ("." + path.rsplit(".", 1)[-1].lower()) in allowed
            }
            stale = scoped_paths != record_paths
            if not stale:
                for path in scoped_paths:
                    record = records[path]
                    if self.failure_will_retry({"path": path, **record}):
                        stale = True
                        break
                    try:
                        stat = (root / path).stat()
                    except OSError:
                        stale = True
                        break
                    if (
                        stat.st_size != record.get("size")
                        or stat.st_mtime_ns != record.get("mtime_ns")
                    ):
                        stale = True
                        break
            report[library_id] = LibraryFreshness(library_id=library_id, stale=stale)
        return report

    def stale_libraries(
        self,
        libraries: str = "all",
        *,
        exclude: str = "",
        format_allowlist: Mapping[str, tuple[str, ...]] | None = None,
    ) -> list[str]:
        """`library_freshness` 的列表投影：只返回 stale 的库 id。"""
        return [
            info.library_id
            for info in self.library_freshness(
                libraries, exclude=exclude, format_allowlist=format_allowlist
            ).values()
            if info.stale
        ]

    # ---- 查询态 --------------------------------------------------------

    def search(
        self,
        libraries: str,
        query: str,
        *,
        top_k: int = 10,
        exclude: str = "",
        folder: str = "",
        include_body: bool = True,
        format_allowlist: tuple[str, ...] | Mapping[str, tuple[str, ...]] | None = None,
    ) -> list[SearchResult]:
        first = self._search_once(
            libraries,
            query,
            top_k=top_k,
            exclude=exclude,
            folder=folder,
            include_body=include_body,
            format_allowlist=format_allowlist,
        )
        top_confidence = first[0].confidence if first else None
        for plugin_id in sorted(self.runtime.registry.providers_of("query_enhancer")):
            try:
                enhancer = self._plugin(plugin_id)
                if not enhancer.should_enhance(query, top_confidence):
                    continue
                expansion = enhancer.enhance(query)
                if not isinstance(expansion, QueryExpansion) or not expansion.query.strip():
                    continue
                second = self._search_once(
                    libraries,
                    expansion.query,
                    top_k=top_k,
                    exclude=exclude,
                    folder=folder,
                    include_body=include_body,
                    format_allowlist=format_allowlist,
                )
            except Exception:
                continue
            first_confidence = first[0].confidence if first else -1.0
            second_confidence = second[0].confidence if second else -1.0
            return second if second_confidence > first_confidence else first
        return first

    def search_with_advice(
        self,
        libraries: str,
        query: str,
        *,
        top_k: int = 10,
        exclude: str = "",
        folder: str = "",
        include_body: bool = True,
        format_allowlist: tuple[str, ...] | Mapping[str, tuple[str, ...]] | None = None,
    ) -> SearchResponse:
        results = self.search(
            libraries,
            query,
            top_k=top_k,
            exclude=exclude,
            folder=folder,
            include_body=include_body,
            format_allowlist=format_allowlist,
        )
        request = SearchAdviceInput(
            results=tuple(results),
            query=query,
            mode="body" if include_body else "list",
            top_k=top_k,
            default_libraries=tuple(
                str(value)
                for value in self.runtime.settings.get("default_libraries", [])
            ),
            warn_threshold=float(
                self.runtime.settings.get(
                    "confidence_warn_threshold",
                    DEFAULT_CONFIDENCE_WARN_THRESHOLD,
                )
            ),
            strong_threshold=CONF_TIER_STRONG,
        )
        advice: list[str] = []
        for plugin_id in sorted(self.runtime.registry.providers_of("result_advisor")):
            try:
                generated = self._plugin(plugin_id).advise(request)
            except Exception:
                continue
            if not isinstance(generated, tuple):
                continue
            for line in generated:
                text = str(line).strip()
                if text and text not in advice:
                    advice.append(text)
                if len(advice) >= 2:
                    break
            if len(advice) >= 2:
                break
        return SearchResponse(results=tuple(results), advice=tuple(advice))

    def _search_once(
        self,
        libraries: str,
        query: str,
        *,
        top_k: int = 10,
        exclude: str = "",
        folder: str = "",
        include_body: bool = True,
        format_allowlist: tuple[str, ...] | Mapping[str, tuple[str, ...]] | None = None,
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

        folder_norm = _norm_folder(folder)
        # 候选池尺度对齐 obsidian-rag/retriever.py:640（问题21/10）：每路
        # dense_k = max(top_k × 8, 200)——无条件垫底 200 修"folder 小目录/
        # 小库召回天花板"，此前 top_k*3 的实现把 top_k=5 时的候选池从旧
        # 的 200 静默缩到 15，是检索召回最大的隐性回退（2026-09-25 终审）。
        dense_candidate_factor = self.runtime.settings.get("dense_candidate_factor", DEFAULT_DENSE_CANDIDATE_FACTOR)
        dense_min_candidates = self.runtime.settings.get("dense_min_candidates", DEFAULT_DENSE_MIN_CANDIDATES)
        rerank_candidates = self.runtime.settings.get("rerank_candidates", DEFAULT_RERANK_CANDIDATES)
        rerank_enabled = self.runtime.settings.get("rerank_enabled", True)
        candidate_pool = max(top_k * dense_candidate_factor, dense_min_candidates)
        # 重排可用性对齐旧 retriever.py:627（rerank_enabled/池为 0/插件加载
        # 失败 → 纯融合降级继续出结果，绝不因重排器缺失让检索整体失败）。
        reranker = None
        if rerank_enabled and rerank_candidates > 0:
            try:
                reranker = self._singleton("reranker")
            except PipelineError:
                reranker = None
        (query_vector,) = embedder.embed_texts([query])
        # RRF 两路权重可调（core/settings.py 通用设置存储，2026-09-23 全面
        # 功能审计发现此前是死值——对齐 obsidian-rag/config.py 的
        # fusion_dense_weight/fusion_bm25_weight，调大 dense 偏语义、调大
        # bm25 偏关键词；没配过就是等权 1.0/1.0，经典无权重 RRF）。
        dense_weight = self.runtime.settings.get("fusion_dense_weight", DEFAULT_FUSION_DENSE_WEIGHT)
        bm25_weight = self.runtime.settings.get("fusion_bm25_weight", DEFAULT_FUSION_BM25_WEIGHT)

        pool_ids: list[str] = []
        per_library_fused: list[list[tuple[str, float]]] = []
        for cfg in entries:
            library_id = cfg.library_id
            generation = self._generations.active(library_id)
            manifest = self._manifest(library_id, generation)
            lexical_segments = self._manifest_segments(manifest, "lexical_segments", generation)
            vector_segments = self._manifest_segments(manifest, "vector_segments", generation)
            active_ids = self._active_chunk_ids(manifest)
            library_allowlist = (
                format_allowlist.get(library_id, ())
                if isinstance(format_allowlist, Mapping)
                else format_allowlist
            )
            if library_allowlist is not None:
                allowed = {str(value).lower() for value in library_allowlist}
                states = self._manifest_files(manifest)
                active_ids = {
                    chunk_id
                    for path, record in states.items()
                    if ("." + path.rsplit(".", 1)[-1].lower()) in allowed
                    for chunk_id in record.get("chunk_ids", [])
                }
            lexical_hits = []
            for segment in reversed(lexical_segments):
                lexical_hits = lexical.search(
                    library_id,
                    query,
                    top_k=candidate_pool,
                    generation=segment,
                )
                if lexical_hits:
                    break
            vector_hits = self._query_vector_segments(
                library_id,
                list(query_vector),
                candidate_pool,
                vector_segments,
                active_ids,
            )
            lexical_ranked = [
                cid
                for cid, _ in lexical_hits
                if (active_ids is None or cid in active_ids) and _in_folder(_chunk_path(cid), folder_norm)
            ]
            vector_ranked = [cid for cid, _ in vector_hits if _in_folder(_chunk_path(cid), folder_norm)]
            fused = fusion.fuse([lexical_ranked, vector_ranked], weights=[bm25_weight, dense_weight])
            # 全量保序保留（含池外余量）：重排池取每库融合前 rerank_candidates
            # 进全局精排，池外余量按各库融合序接在重排结果后（旧 retriever.py:681-696）。
            per_library_fused.append(fused)
            pool_ids.extend(chunk_id for chunk_id, _ in fused[:rerank_candidates])
        if not pool_ids:
            return []

        # chunk_id 全局唯一且自带 library_id（见 _chunk_library），按库分组
        # 批量取记录——vector_store.get_by_ids 是单库作用域的 API，不能跨库
        # 一次问完，但也不需要为每个 chunk_id 单独查一次。
        by_library: dict[str, list[str]] = {}
        manifest_chunk_totals: dict[tuple[str, str], list] = {}
        for chunk_id in pool_ids:
            by_library.setdefault(_chunk_library(chunk_id), []).append(chunk_id)
        records: dict[str, dict] = {}
        sections_by_library: dict[str, dict[str, dict]] = {}
        for library_id, ids in by_library.items():
            generation = self._generations.active(library_id)
            manifest = self._manifest(library_id, generation)
            vector_segments = self._manifest_segments(manifest, "vector_segments", generation)
            records.update(self._vector_records(library_id, ids, vector_segments))
            manifest_files_for_lib = self._manifest_files(manifest)
            sections_by_library[library_id] = {
                path: dict(record.get("sections", {}))
                for path, record in manifest_files_for_lib.items()
                if record.get("status") == "indexed" and isinstance(record.get("sections"), dict)
            }
            for path, record in manifest_files_for_lib.items():
                manifest_chunk_totals[(library_id, path)] = record.get("chunk_ids", [])

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
            meta_for_rerank = record["metadata"] or {}
            if meta_for_rerank.get("ctx"):
                # document 已带"文件名/title/tags/标题链"锚点前缀（问题18 v6），
                # 重排器看到的就是存储文本，与旧项目一致，不再叠加面包屑
                prefixed = record["document"]
            else:
                heading = meta_for_rerank.get("heading_breadcrumb", "")
                prefixed = (
                    heading + "\n" + record["document"]
                    if heading and heading != "(无标题)"
                    else record["document"]
                )
            rerank_input.append((chunk_id, prefixed))
        if not rerank_input:
            return []

        # 排序与置信度对齐旧 retriever.py:662-757：重排生效 → 全局精排序 +
        # 重排器概率直接作置信度（只钳位不二次激活，问题54）；重排关闭/
        # 失败 → 各库融合分按库内最高分归一化合并（_merge_normalized），
        # 置信度退回 RRF 双路一致度（score/[(wd+wb)/(k+1)]，k=2）。
        merged: list[str] = []
        rr_conf: dict[str, float] = {}
        if reranker is not None and rerank_input:
            try:
                reranked = reranker.rerank(query, rerank_input, top_k=len(rerank_input))
                if reranked:
                    rr_conf = {
                        chunk_id: max(0.0, min(1.0, float(score)))
                        for chunk_id, score in reranked
                    }
                    merged = [chunk_id for chunk_id, _ in reranked]
                    for fused in per_library_fused:
                        merged.extend(chunk_id for chunk_id, _ in fused[rerank_candidates:])
            except Exception:
                rr_conf = {}
        if not merged:
            normalized: list[tuple[str, float]] = []
            for fused in per_library_fused:
                best = max((score for _, score in fused), default=0.0)
                normalized.extend(
                    (chunk_id, score / best if best > 0 else 0.0) for chunk_id, score in fused
                )
            normalized.sort(key=lambda item: item[1], reverse=True)
            merged = [chunk_id for chunk_id, _ in normalized]
        # 退路置信度的分母：RRF 分上限 = (w_dense + w_bm25)/(k+1)（两路都
        # 第一时），k 取融合插件同一常数 2。
        rrf_max = (float(dense_weight) + float(bm25_weight)) / 3.0
        rrf_scores: dict[str, float] = {
            chunk_id: score for fused in per_library_fused for chunk_id, score in fused
        }
        if not merged:
            return []

        drop_threshold = self.runtime.settings.get("confidence_drop_threshold", DEFAULT_CONFIDENCE_DROP_THRESHOLD)
        max_chunks_per_file = self.runtime.settings.get("max_chunks_per_file", DEFAULT_MAX_CHUNKS_PER_FILE)

        # 交付候选窗口对齐旧 retriever.py:29-41（FOLD_WINDOW_FACTOR=4）：
        # 正文模式同节折叠会吃掉候选，窗口补位让被折叠名次之后的真正候选
        # 进得来；list 模式不折叠不封顶，直接 top_k。
        fold_window_factor = 4
        delivery_ids = merged[: top_k * fold_window_factor] if include_body else merged[:top_k]

        small_to_big = include_body and self.runtime.settings.get("small_to_big", True)
        return_chunk_limit = self.runtime.settings.get("return_chunk_limit", DEFAULT_RETURN_CHUNK_LIMIT)
        results: list[SearchResult] = []
        per_file_count: dict[tuple[str, str], int] = {}
        emitted_sections: set[tuple[str, str, str]] = set()
        for chunk_id in delivery_ids:
            if len(results) >= top_k:
                break
            confidence = rr_conf.get(chunk_id)
            if confidence is None:
                raw = rrf_scores.get(chunk_id, 0.0)
                confidence = min(1.0, raw / rrf_max) if rrf_max > 0 else 0.0
            if confidence < drop_threshold:
                continue
            record = records.get(chunk_id)
            if record is None:
                continue
            meta = record["metadata"] or {}
            library_id = _chunk_library(chunk_id)
            path = str(meta.get("path", ""))
            file_key = (library_id, path)
            section_id = str(meta.get("section_id") or "")
            section = (
                sections_by_library.get(library_id, {})
                .get(path, {})
                .get(section_id, {})
            )
            parent_text = ""
            backfilled = False
            if small_to_big and section_id and isinstance(section, dict):
                try:
                    section_chunks = int(section.get("chunk_count") or 0)
                except (TypeError, ValueError):
                    section_chunks = 0
                candidate_parent = str(section.get("text") or "")
                if section_chunks > 1 and len(candidate_parent) > 300:
                    parent_text = candidate_parent
                    backfilled = True
            section_key = (library_id, path, section_id)
            if backfilled and section_key in emitted_sections:
                continue
            if include_body and per_file_count.get(file_key, 0) >= max_chunks_per_file:
                continue
            if include_body:
                per_file_count[file_key] = per_file_count.get(file_key, 0) + 1
            if backfilled:
                emitted_sections.add(section_key)
            delivered = _strip_anchor_context(record["document"], meta)
            chunk_index = -1
            try:
                chunk_index = int(meta.get("chunk_index", -1))
            except (TypeError, ValueError):
                chunk_index = -1
            total_chunks = len(record.get("chunk_ids") or manifest_chunk_totals.get((library_id, path), []))
            truncated = False
            if include_body and return_chunk_limit > 0 and len(delivered) > return_chunk_limit:
                delivered, truncated = _truncate_at_line(delivered, return_chunk_limit, TRUNCATE_MARK)
            results.append(
                SearchResult(
                    chunk_id=chunk_id,
                    library_id=library_id,
                    path=path,
                    heading_breadcrumb=meta.get("heading_breadcrumb", ""),
                    text=parent_text or delivered,
                    confidence=confidence,
                    backfilled=backfilled,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    truncated=truncated,
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

    def read_document(self, library_id: str, path: str) -> DocumentContent:
        """读取某文档的完整正文——对齐 obsidian-rag 的 `read_document`
        MCP 工具（2026-09-23 全面功能审计发现的缺口）：检索命中后想通读
        全文时用，不是 search() 的替代品，没有 query/confidence。

        `.md`/`.txt` 直接重读源文件（拿到当前最新内容，比任何缓存都准）；
        其余格式（pdf/docx 等需要真正"提取"的格式）读上一次
        `index_library()` 写入的提取结果缓存（`core/extract_cache.py`）
        ——不在这里现场重新提取，尤其本机OCR代价很高，"精读一篇已经索引
        过的文档"不该悄悄触发一次重扫描，对齐 obsidian-rag"绝不后台
        触发扫描件OCR或云端调用"的承诺。缓存里没有就说明这个文件还没有
        被成功索引过，报错提示先建索引，不是静默返回空。

        被排除出检索范围的文件拒绝读取（对齐 obsidian-rag 问题44的教训：
        "被用户显式排除的文件对 RAG 系统完全不存在，Agent 不可访问"）。
        """
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")

        decisions = lib_mgr.resolve_included_files(library_id)
        matches = [f for f in decisions if f[0] == path]
        if not matches and path and "." not in path:
            # 标题回退（对齐旧 read_document）：path 不带扩展名时按文件名 stem
            # 匹配——AI 从检索结果的 heading/标题过来时常常不知道真实扩展名
            candidates = [
                f
                for f in decisions
                if Path(f[0]).stem == path or f[0].rsplit(".", 1)[0] == path
            ]
            if len(candidates) == 1:
                matches = candidates
            elif len(candidates) > 1:
                raise KeyError(
                    f"标题 {path!r} 命中多个文件（{[c[0] for c in candidates]}），请带完整相对路径重试"
                )
        if not matches:
            raise KeyError(f"库「{library_id}」里找不到文件: {path!r}")
        _, included, reason = matches[0]
        if not included:
            raise ValueError(f"「{path}」已被排除出检索范围（{reason}），拒绝读取")
        resolved_path = matches[0][0]  # 标题回退命中时返回真实相对路径

        ext = resolved_path.rsplit(".", 1)[-1].lower() if "." in resolved_path else ""
        cfg = lib_mgr.store.get(library_id)
        abs_path = Path(cfg.root_path) / resolved_path
        if ext in ("md", "txt", "markdown"):
            try:
                text = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise ValueError(f"读取源文件失败: {type(exc).__name__}: {exc}") from exc
            return DocumentContent(
                library_id=library_id, path=resolved_path, text=text,
                source="源文件直读", abs_path=str(abs_path),
            )

        generation = self._generations.active(library_id)
        manifest = self._manifest(library_id, generation)
        state = self._manifest_files(manifest).get(resolved_path)
        if manifest is not None and (state is None or state.get("status") != "indexed"):
            raise ValueError(f"「{resolved_path}」还没有被成功索引过，先调用 index_library 建好索引再重试")
        segments = self._manifest_segments(manifest, "extract_segments", generation)
        cached = self._read_extract_cache(library_id, resolved_path, segments)
        if cached is None:
            raise ValueError(f"「{resolved_path}」还没有被成功索引过，先调用 index_library 建好索引再重试")
        return DocumentContent(
            library_id=library_id, path=resolved_path, text=cached,
            source="提取缓存", abs_path=str(abs_path),
        )

    def find_duplicates(
        self,
        library_id: str,
        *,
        threshold: float = 0.7,
        format_allowlist: tuple[str, ...] | None = None,
    ) -> dict[str, list[list[str]]]:
        """近似重复检测（只读建议，绝不自动删/移动文件）——对齐
        obsidian-rag 的 `find_duplicates` MCP 工具（2026-09-23 全面功能
        审计发现的缺口）：找出库内"内容几乎相同"的重复文档（同一课件多份
        拷贝、文档转出的多个副本），比较的是"上一次成功索引时的提取正文"
        （`core/extract_cache.py`），不产生新向量、不改索引、不触发重新
        提取。

        `dedup` 是多值扩展点（同 `visual_index`）——遍历全部已启用的
        提供者，各自独立现算一遍，按 plugin_id 汇总返回，不做跨提供者
        合并（不同去重算法给出的分组语义上互相独立，同 `navigate()`
        "不同视觉模型不混叠"的原则）。没有被成功索引过的文件不参与比较
        （同 obsidian-rag"未提取的文件跳过"的做法）。
        """
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")

        texts: dict[str, str] = {}
        generation = self._generations.active(library_id)
        manifest = self._manifest(library_id, generation)
        states = self._manifest_files(manifest)
        segments = self._manifest_segments(manifest, "extract_segments", generation)
        paths = {
            path
            for path, record in states.items()
            if record.get("status") == "indexed"
        } if manifest is not None else set(self._extract_cache.list_relative_paths(library_id, generation))
        if format_allowlist is not None:
            allowed = {str(value).lower() for value in format_allowlist}
            paths = {
                path
                for path in paths
                if ("." + path.rsplit(".", 1)[-1].lower()) in allowed
            }
        for path in paths:
            text = self._read_extract_cache(library_id, path, segments)
            if text is not None:
                texts[path] = text

        result: dict[str, list[list[str]]] = {}
        for plugin_id in sorted(self.runtime.registry.providers_of("dedup")):
            dedup_plugin = self._plugin(plugin_id)
            result[plugin_id] = dedup_plugin.find_duplicates_in_texts(texts, threshold=threshold)
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

    def sample_library(
        self,
        library_id: str,
        k: int = 20,
        *,
        format_allowlist: tuple[str, ...] | None = None,
    ) -> list[SampledChunk]:
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        vector_store = self._singleton("vector_store")
        if not hasattr(vector_store, "sample"):
            raise PipelineError("当前 vector_store 实现不支持采样（缺少 sample）")
        generation = self._generations.active(library_id)
        manifest = self._manifest(library_id, generation)
        vector_segments = self._manifest_segments(manifest, "vector_segments", generation)
        active_ids = self._active_chunk_ids(manifest)
        if format_allowlist is not None:
            allowed = {str(value).lower() for value in format_allowlist}
            states = self._manifest_files(manifest)
            active_ids = {
                chunk_id
                for path, record in states.items()
                if ("." + path.rsplit(".", 1)[-1].lower()) in allowed
                for chunk_id in record.get("chunk_ids", [])
            }
        if hasattr(vector_store, "sample_records"):
            rows = self._vector_rows(library_id, vector_segments, active_ids)
            return vector_store.sample_records(rows, k=k)
        if format_allowlist is not None:
            raise PipelineError("当前 vector_store 实现不支持带格式门禁的采样")
        return vector_store.sample(library_id, k=k, generation=vector_segments[-1] if vector_segments else generation)

    def library_content_fingerprint(self, library_id: str) -> str:
        """库当前内容的指纹——逐字对齐 obsidian-rag/library_summary.py::
        content_fingerprint（40-50 行）的算法：聚合全部已索引文件的
        "相对路径:内容哈希"（排序后以 | 连接，sha256 截断 16 位），任何
        增删改都会变化。旧项目的数据源是 meta 条目的 hash 字段，这里的
        等价数据源是 per-file manifest 的 content_hash（两者都是"内容哈希"，
        语义一致）。用于判断已生成的简介是否可能已过时，不参与采样。"""
        generation = self._generations.active(library_id)
        manifest = self._manifest(library_id, generation)
        if manifest is None:
            return ""
        records = self._manifest_files(manifest)
        parts = sorted(
            f"{path}:{record.get('content_hash', '')}"
            for path, record in records.items()
            if record.get("content_hash")
        )
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def propose_library_summary(self, library_id: str, text: str) -> dict:
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        # 对齐 obsidian-rag/server.py:508-509：AI 提交的简介在写入时刻
        # 现算当前内容指纹一并落盘，is_stale 才有比对基准
        fingerprint = self.library_content_fingerprint(library_id)
        return self._singleton("library_summary").propose(library_id, text, fingerprint=fingerprint)

    def set_library_summary_direct(
        self,
        library_id: str,
        text: str,
        *,
        source: str = "user",
        fingerprint: str | None = None,
        model: str | None = None,
    ) -> dict:
        """无条件写入，不经过写权限门禁——给"人类直接操作"这条路径用
        （GUI 手写编辑 / GUI"刷新简介"按钮），见 official-library-summary
        插件 plugin.py::set_direct 的说明。AI 刷新路径传入 fingerprint/model
        （对齐旧项目 bridge.py:414）；用户手写不带指纹（对齐 bridge.py:356-365，
        无指纹 = 不参与过时判定）。"""
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        return self._singleton("library_summary").set_direct(
            library_id, text, source=source, fingerprint=fingerprint, model=model
        )

    def apply_library_summary(self, library_id: str, proposal_id: str, confirmation_code: str) -> dict:
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")
        return self._singleton("library_summary").apply(library_id, proposal_id, confirmation_code)

    def generate_library_summary(self, library_id: str, k: int = 20) -> tuple[str, str, str]:
        """采样 + 拼prompt + 依次尝试 llm_provider 链（按插件id字母序，
        同 `_extract()` 链式尝试 extractor:pdf 的既定模式），直到某个
        provider 真的产出非空结果为止。返回 (生成的文本, 当前内容指纹,
        使用的provider插件id)——对齐 obsidian-rag/library_summary.py::
        generate_summary（225-247 行）的返回三元组，指纹供写入方一并落盘。
        **不落盘**——落盘是调用方决定要不要走 propose_library_summary()/
        set_direct() 的事，这里只负责"编排跨插件生成流程"。
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
                return summary_plugin.finalize_text(text), self.library_content_fingerprint(library_id), plugin_id
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

        config_manifest = {
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
            "agent_formats": list(cfg.agent_formats),
            "exclude_dirs": list(cfg.exclude_dirs),
            "exclude_files": list(cfg.exclude_files),
            "exclude_patterns": list(cfg.exclude_patterns),
        }

        if not hasattr(vector_store, "get_all"):
            raise PipelineError("当前 vector_store 实现不支持导出（缺少 get_all）")
        generation = self._generations.active(library_id)
        index_manifest = self._manifest(library_id, generation)
        vector_segments = self._manifest_segments(index_manifest, "vector_segments", generation)
        active_ids = self._active_chunk_ids(index_manifest)
        vectors = self._vector_rows(library_id, vector_segments, active_ids)

        if not hasattr(lexical, "export_state"):
            raise PipelineError("当前 lexical_index 实现不支持导出（缺少 export_state）")
        lexical_segments = self._manifest_segments(index_manifest, "lexical_segments", generation)
        bm25_state = lexical.export_state(
            library_id,
            generation=lexical_segments[-1] if lexical_segments else generation,
        )

        extracted_state = (
            self._extract_cache.export_state(library_id, generation)
            if generation
            else {}
        )
        relations_state = (
            self._note_relations.read_library(library_id, generation)
            if generation
            else {}
        )
        failures_state = (
            self._index_failures.read(library_id, generation)
            if generation
            else None
        )
        visual_states: dict[str, dict] = {}
        for plugin_id in sorted(self.runtime.registry.providers_of("visual_index")):
            visual = self._plugin(plugin_id)
            exporter = getattr(visual, "export_state", None)
            if generation and callable(exporter):
                state = exporter(library_id, generation)
                if isinstance(state, dict):
                    visual_states[plugin_id] = state
        return archive_codec.pack(
            config_manifest,
            vectors,
            bm25_state,
            index_manifest=index_manifest,
            extracted=extracted_state,
            relations=relations_state,
            failures=failures_state,
            visual=visual_states or None,
        )

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
            enabled_extensions=manifest.get("enabled_extensions", [".md", ".pdf", ".docx"]),
            exclude_dirs=list(manifest.get("exclude_dirs", [])),
            exclude_files=list(manifest.get("exclude_files", [])),
            exclude_patterns=list(manifest.get("exclude_patterns", [])),
        )
        lib_mgr.store.set_agent_formats(target_id, list(manifest.get("agent_formats", [])))

        generation = uuid.uuid4().hex
        source_manifest = payload.get("index_manifest") or {}
        source_files = source_manifest.get("files", {})
        if not isinstance(source_files, dict):
            source_files = {}
        vectors = payload["vectors"] if isinstance(payload.get("vectors"), dict) else {}
        id_map: dict[str, str] = {}
        remapped_vectors: dict[str, dict] = {}
        for source_chunk_id, row in vectors.items():
            if not isinstance(row, dict):
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            path = str(metadata.get("path", ""))
            chunk_index = int(metadata.get("chunk_index", 0))
            target_chunk_id = f"{target_id}:{path}:{chunk_index}"
            id_map[str(source_chunk_id)] = target_chunk_id
            remapped_vectors[target_chunk_id] = row

        files: dict[str, dict] = {}
        for path, source_record in source_files.items():
            if not isinstance(path, str) or not isinstance(source_record, dict):
                continue
            record = dict(source_record)
            record["chunk_ids"] = [
                id_map.get(str(chunk_id), f"{target_id}:{path}:{index}")
                for index, chunk_id in enumerate(record.get("chunk_ids", []))
            ]
            try:
                record["size"], record["mtime_ns"], record["content_hash"] = self._file_fingerprint(
                    Path(root_path) / path
                )
            except OSError:
                record.setdefault("size", -1)
                record.setdefault("mtime_ns", -1)
                record.setdefault("content_hash", "")
            files[path] = record
        for target_chunk_id, row in remapped_vectors.items():
            metadata = row.get("metadata") or {}
            path = str(metadata.get("path", ""))
            record = files.setdefault(
                path,
                {
                    "size": -1,
                    "mtime_ns": -1,
                    "content_hash": "",
                    "status": "indexed",
                    "failure_state": None,
                    "failure_reason": None,
                    "failure_detail": None,
                    "links": [],
                },
            )
            record.setdefault("chunk_ids", []).append(target_chunk_id)
        for record in files.values():
            record["chunk_ids"] = list(dict.fromkeys(record.get("chunk_ids", [])))

        if remapped_vectors:
            chunk_ids = list(remapped_vectors)
            vector_store.upsert(
                target_id,
                chunk_ids,
                [remapped_vectors[cid]["embedding"] for cid in chunk_ids],
                documents=[remapped_vectors[cid].get("document", "") for cid in chunk_ids],
                metadatas=[remapped_vectors[cid].get("metadata", {}) for cid in chunk_ids],
                generation=generation,
            )

        if hasattr(lexical, "import_state"):
            bm25_state = payload.get("bm25") or {}
            if isinstance(bm25_state, dict):
                bm25_state = dict(bm25_state)
                bm25_state["doc_lengths"] = {
                    id_map.get(str(key), f"{target_id}:{key}"): value
                    for key, value in (bm25_state.get("doc_lengths", {}) or {}).items()
                }
                bm25_state["doc_tokens_cache"] = {
                    id_map.get(str(key), f"{target_id}:{key}"): value
                    for key, value in (bm25_state.get("doc_tokens_cache", {}) or {}).items()
                }
            lexical.import_state(target_id, bm25_state, generation=generation)

        extracted = payload.get("extracted")
        if isinstance(extracted, dict) and extracted:
            self._extract_cache.import_state(target_id, extracted, generation)

        index_manifest = {
            "format_version": INDEX_MANIFEST_VERSION,
            "library_id": target_id,
            "generation": generation,
            "previous_generation": None,
            "signatures": self._pipeline_signatures(),
            "files": files,
            "vector_segments": [generation] if remapped_vectors else [],
            "extract_segments": [generation] if extracted else [],
            "lexical_segments": [generation],
            "active_chunk_ids": list(remapped_vectors),
            "compacted": True,
        }
        relations = payload.get("relations")
        self._note_relations.write_library(
            target_id,
            relations if isinstance(relations, dict) else {},
            generation,
        )
        visual_states = payload.get("visual")
        if isinstance(visual_states, dict):
            for plugin_id, state in visual_states.items():
                if plugin_id not in self.runtime.registry.providers_of("visual_index"):
                    continue
                visual = self._plugin(plugin_id)
                importer = getattr(visual, "import_state", None)
                if callable(importer):
                    importer(target_id, state, generation)
        failures = payload.get("failures")
        if not isinstance(failures, dict):
            failures = {
                "succeeded": sum(1 for record in files.values() if record.get("status") == "indexed"),
                "failures": [
                    {"path": path, "reason": record.get("failure_state", "extract-failed")}
                    for path, record in files.items()
                    if record.get("status") in {"failed", "terminal"}
                ],
            }
        self._index_failures.write_library(
            target_id,
            succeeded=int(failures.get("succeeded", 0)),
            failures=list(failures.get("failures", [])),
            generation=generation,
        )
        if not self._manifests.write(index_manifest):
            self.discard_index_generation(target_id, generation)
            lib_mgr.store.remove_library(target_id)
            raise PipelineError("导入数据已生成，但索引清单写入失败")
        if not self._generations.commit(target_id, generation):
            self.discard_index_generation(target_id, generation)
            self._manifests.clear(target_id, generation)
            lib_mgr.store.remove_library(target_id)
            raise PipelineError("导入数据已生成，但发布 generation 失败")

        return target_id
