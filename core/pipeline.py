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
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field, replace as dataclasses_replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .contracts import Chunk, DocumentContent, ExtractedDocument, GraphResponse, LibraryFreshness, LibrarySummary, PageHit, PreviewExtraction, QueryExpansion, SampledChunk, SearchAdviceInput, SearchResponse, SearchResult, SemanticGraphResponse, VisualPageState
from .graph import build_graph, select_semantic_edges
from .extract_cache import ExtractCache
from . import index_integrity
from .index_failures import IndexFailuresStore
from .index_generation import INDEX_MANIFEST_VERSION, IndexGenerationStore, IndexManifestStore
from .index_progress import IndexProgressEvent, IndexStartResult, IndexWorkerManager
from .library_key import library_storage_key
from .note_relations import NoteRelationsStore, extract_wikilink_targets
from .text_cleaning import (TEXT_PIPELINE_VERSION, build_anchor_context, clean_wikilinks,
                            extract_frontmatter, strip_boilerplate_lines, strip_dead_image_refs,
                            strip_page_number_lines, strip_sidecar_noise)
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
IMPORT_UPSERT_BATCH = 500  # 导入时每批 upsert 的块数（旧 config.import_upsert_batch：拥塞小值减慢导入，较大值占内存）
TERMINAL_FAILURE_STATES = frozenset({"unreadable", "empty", "tbd", "scanned", "extract-failed"})
#: IndexReport.skip_reason 的取值：库路径不存在，本轮整库跳过（保留现有索引）。
#: 稳定 token，调用方按它分支，不要写自由文本。
SKIP_MISSING_ROOT = "library-root-missing"

#: 向量化的分片大小（块数）。只用来给进度心跳/停滞看门狗喘气：分片之间没有别的活，
#: 显卡连续工作；嵌入器内部仍按旧项目的批大小（8，按显存自动收紧）逐批编码。
_EMBED_SLICE = 64
#: 向量库写入的缓冲上限（块数）：攒够一批再 upsert，与压缩段用的 1000 一致。
_WRITE_FLUSH_CHUNKS = 1000


@dataclass
class _PendingFile:
    """“已转换切块、等着统一向量化与写入”的一个文件（见 `Pipeline.index_library`）。"""

    plan: dict
    file_report: "IndexFileReport"
    chunks: list
    ctx_by_id: dict
    section_counts: dict
    section_headings: dict
    section_texts: dict
    raw_links: list
    extracted_by: str
    extractor_version: str
    content_hash: str

#: 提取试验台（`Pipeline.preview_extract`）的后端覆盖名 → provider 插件 id。
#: 键名沿用旧项目 guiweb/bridge.py::preview_start 的 `backend` 取值
#: （None/"local"/"mineru-cloud"/"mineru-local"），值是本项目对应的插件 id——
#: 旧项目的 "local" 指的是本机 PDF 解析，本项目对应
#: official-extractor-pdf-text（读 PDF 文字层）。映射放在 core 而不是
#: GUI：试验台和索引链路共用同一批 extractor provider，谁都不该自己认
#: 后端名字。
_PREVIEW_BACKEND_PLUGINS = {
    "local": "official-extractor-pdf-text",
    "mineru-cloud": "official-ocr-mineru-cloud",
    "mineru-local": "official-ocr-mineru-local",
}

#: official-vector-store-chroma 建的集合命名前缀。`libg_` = 按 generation
#: 分段；`lib_` = 无 generation 的老式命名；`libk_` = 库 id 本身不满足
#: Chroma 集合名规则（中文/空格等）时走的哈希化命名，见
#: `official-vector-store-chroma/.../store.py::chroma_collection_name`。
#: 三个前缀都要认，漏一个就会把**正在使用**的集合当残留删掉。
_VECTOR_COLLECTION_PREFIXES = ("libg_", "libk_", "lib_")


def _is_rag_collection_name(name: str) -> bool:
    """这个名字属于本项目的向量库命名空间吗（回收时只碰自己人）。"""
    return name.startswith(_VECTOR_COLLECTION_PREFIXES)


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


@dataclass(frozen=True)
class SearchDelivery:
    """一次检索的**交付态**：结果本体 + "这一批是怎么交出来的"那几个信号。

    为什么不把这几个计数塞进 `SearchResult`：它们是**整批**的属性，不是某
    一条结果的属性——同一批里 20 条结果的 `capped`/`folded` 全都相同，让每
    条结果各背一份是冗余，而且调用方几乎总是"整批一起看"。

    为什么需要它们：旧项目 obsidian-rag/retriever.py:488-497 把 `capped` /
    `folded` 一起传给 advice，并在封顶时追加尾注"（同一文件最多展示 N 块…
    完整内容请打开源文件）"。少了这两个信号，agent 看到同一篇笔记占满 3 条
    时会以为库里就这些内容，不知道后面还有被封顶掉的——这是用户可观察的
    行为缺失，不是内部实现细节。
    """

    results: tuple[SearchResult, ...]
    #: 触发了"同一文件最多 N 块"的封顶（对齐 retriever.py 的 `capped`）
    capped: bool = False
    #: 因"同一小节已交付过"被折叠掉的块数（对齐 `folded`）
    folded: int = 0
    #: 交付前进入过滤流程的候选块数。结果为空时用它区分"压根没候选"与
    #: "有候选但全被置信度下限过滤"——两者的建议文案不同。
    candidates: int = 0

    def empty_reason(self) -> str | None:
        """结果为空时的原因（喂给 `SearchAdviceInput.empty_reason`），
        非空返回 None。对齐 retriever.py:479-483。"""
        if self.results:
            return None
        return "all-below-drop-threshold" if self.candidates else "no-score"


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
    #: 本轮"整个库什么都没做"的单一原因（稳定可机读 token），None = 正常跑完。
    #: 目前只有 SKIP_MISSING_ROOT 一种（库路径不存在，见 `index_library` 里
    #: 对齐 obsidian-rag/index.py:1871-1873 的门禁）。
    #:
    #: **为什么不复用 `deferred`**：`deferred` 是**文件级**语义——"某个文件的
    #: 提取服务瞬态不可用，本轮跳过、下轮重试"，由
    #: `IndexFileReport.failure_state == "deferred"` 汇总而来，它的重试语义
    #: 建立在"旧条目与旧块原样保留"之上（obsidian-rag/index.py:2142-2148）。
    #: 库路径不存在是**库级前置条件不成立**："这一轮连枚举都没开始"，既没有
    #: 任何文件参与，也没有"下轮自动重试"的责任——路径回来了自然就正常跑。
    #: AGENTS.md §5 要求"瞬态服务不可用必须与永久失败区分"，把两者塞进同一个
    #: 计数会让调用方无法回答"这轮到底有没有文件被推迟"，也会让
    #: `deferred > 0` 触发"下轮一定重试"的错误预期。
    skip_reason: str | None = None

    @property
    def skipped(self) -> bool:
        return self.skip_reason is not None

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

    def _read_sidecar(self, library_id: str, content_hash: str) -> list | None:
        """读 MinerU 官方块标注 sidecar（问题48附记 v11 双轨清洗用）：按源
        文件内容哈希向 OCR 插件查询。只有 MinerU 云端新提取过的文件才有
        sidecar；老文件/其他后端读不到返回 None 自动跳过（同旧
        read_cache_sidecar 契约：绝不抛异常，走启发式回退）。"""
        if not content_hash:
            return None
        for plugin_id in sorted(self.runtime.registry.providers_of("extractor:pdf")):
            fn = getattr(self._plugin(plugin_id), "read_sidecar", None)
            if fn is None:
                continue
            try:
                sidecar = fn(content_hash)
            except Exception:
                continue
            if sidecar:
                return sidecar
        return None

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

    def preview_extract(self, path: str, *, backend: str = "") -> "PreviewExtraction":
        """提取试验台：对**任意**本地文件跑一次提取，不写提取缓存、不落
        generation、不碰任何索引数据，也不要求该文件在某个已注册库里。

        `backend` 覆盖后端选择（`""`=跟随全局设置，与索引链路一致；
        `"mineru-cloud"` / `"mineru-local"` / `"local"` 强制走指定后端）。
        强制后端时绕开该 provider 的 `is_active()` 门禁——试验台的用途
        恰恰是"在不改全局配置的前提下试一下这个后端"。

        路由顺序复用 `_extract` 的 provider 链（`extractor:{ext}` 扩展点
        顺序），不另写一份"试验台专用路由"：同一份格式→provider 映射只有
        一处权威实现（docs/DATA_FLOW.md 规则4）。与索引链路的差别只有
        三点，都在这里显式声明而不是隐式：①不查也不写提取缓存；②不要求
        库上下文（library_id 传 `""`）；③`is_active()` 可被 backend 覆盖。

        失败语义与索引链路一致：返回 `failure_reason` 终态字符串
        （unreadable/empty/scanned/extract-failed/deferred/无 provider），
        不抛异常——试验台要把"为什么没产出"如实显示给用户。
        """
        started = time.monotonic()
        file_path = Path(path)
        ext = file_path.suffix.lstrip(".").lower()
        provider_ids = self.runtime.registry.providers_of(f"extractor:{ext}")
        if not provider_ids:
            return PreviewExtraction(
                path=str(file_path),
                ok=False,
                markdown=None,
                reason=f"没有插件能处理 .{ext} 格式",
                route="-",
                backend=backend,
                elapsed=0.0,
                chars=0,
            )
        forced = _PREVIEW_BACKEND_PLUGINS.get(backend) if backend else None
        if backend and forced is None:
            return PreviewExtraction(
                path=str(file_path),
                ok=False,
                markdown=None,
                reason=f"未知的试验台后端: {backend}",
                route="-",
                backend=backend,
                elapsed=0.0,
                chars=0,
            )
        if forced is not None and forced not in provider_ids:
            return PreviewExtraction(
                path=str(file_path),
                ok=False,
                markdown=None,
                reason=f"后端 {backend} 不处理 .{ext} 格式",
                route="-",
                backend=backend,
                elapsed=0.0,
                chars=0,
            )
        last: ExtractedDocument | None = None
        for plugin_id in sorted(provider_ids):
            if forced is not None and plugin_id != forced:
                continue
            extractor = self._plugin(plugin_id)
            if forced is None:
                active = getattr(extractor, "is_active", None)
                if callable(active) and not active():
                    continue
            result = extractor.extract("", str(file_path), file_path.parent)
            last = result
            if result.text is not None:
                return PreviewExtraction(
                    path=str(file_path),
                    ok=True,
                    markdown=result.text,
                    reason="",
                    route=f"extractor:{plugin_id}",
                    backend=backend,
                    elapsed=round(time.monotonic() - started, 3),
                    chars=len(result.text),
                )
        assert last is not None
        return PreviewExtraction(
            path=str(file_path),
            ok=False,
            markdown=None,
            reason=last.failure_reason or "提取管线未产出",
            route=f"extractor:{sorted(provider_ids)[-1]}",
            backend=backend,
            elapsed=round(time.monotonic() - started, 3),
            chars=0,
        )

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
            # 设置页填的 mineru_api_key 与环境变量 MINERU_API_KEY 等价（云端插件
            # 同样是设置优先、环境变量兜底）：补上 Key 必须改变能力签名，存量
            # scanned 终态才会在下一轮自动重试。
            credential = bool(
                str(self.runtime.settings.get("mineru_api_key", "") or "").strip()
                or str(os.environ.get("MINERU_API_KEY", "") or "").strip()
            )
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
        fresh_extract: bool = False,
        progress_callback: Callable[[IndexProgressEvent], None] | None = None,
        format_allowlist: tuple[str, ...] | None = None,
    ) -> IndexReport:
        """`progress_callback(event)` 可选——每进入一个真实索引阶段、完成
        一个文件时调用一次，供 `core/index_progress.py::IndexWorkerManager`
        在工作进程中上报进度。不传就是
        原有的纯同步调用，行为完全不变——GUI/测试目前都是这样直接调用，
        不强制迁移到后台执行那条路径。

        `full` 对齐 obsidian-rag 的 `--full`（index.py:1891 `meta = {}`）：
        所有文件重新切块+重嵌，但**仍然复用提取缓存**——所以拿它改切块粒度
        不会重复烧 MinerU 配额。真正强制重新解析正文是 `fresh_extract`
        （对齐 `--fresh-extract`，index.py:2450-2455）。"""
        lib_mgr = self._singleton("library_manager")
        cfg = lib_mgr.store.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        root = Path(cfg.root_path)

        # 库路径门禁——对齐 obsidian-rag/index.py:1871-1873（`if not
        # Path(vault).is_dir(): log(...); return`，"保留现有索引"）。放在
        # 枚举之前、也放在 token 复位之前，与 LEGACY 的顺序一致。
        # 没有这道门禁时：路径临时不可用（移动的 OneDrive 库、未挂载的网络
        # 盘、盘符掉线）→ 枚举返回 [] → plans 为空 → manifest_files={} →
        # **照样原子发布一个空 generation**，把整库索引一次性清空且不可逆。
        # AGENTS.md §5「停止、异常或崩溃不能把半成品切换成当前索引」在这里
        # 的具体形态就是"什么都没扫到"——它同样是一个不该被发布的半成品。
        # 与 `library_freshness` 的 `missing=True`（index.py:1506-1508）是
        # 同一件事的两道闸：freshness 决定"该不该自动同步"（不该），本门禁
        # 兜住"有人显式/强行调了索引"（不许清空），两道闸不冲突。
        if not root.is_dir():
            logging.getLogger("rag_redo.core.pipeline").warning(
                "库路径不存在，跳过索引（保留现有索引）：%s", root
            )
            return IndexReport(library_id=library_id, skip_reason=SKIP_MISSING_ROOT)

        # Token 失效标志每轮索引复位（问题35：长驻进程跨轮次复用，旧
        # index.py:1874-1878 在云端段开始时 mineru_token_reset() 同语义）
        for provider_id in self.runtime.registry.providers_of("extractor:pdf"):
            reset = getattr(self._plugin(provider_id), "reset_token_flag", None)
            if reset is not None:
                try:
                    reset()
                except Exception:
                    pass

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

        # ---- 一致性自愈（对齐 obsidian-rag/index.py:1903-1910）------------
        # manifest 记的期望块数与向量库实际块数对不上时，增量路径"指纹全命中
        # → 没有新块可写"根本修不了缺失的块（LEGACY 注释原话），必须按全量
        # 重建处理。覆盖 LEGACY 点名的两类场景：--full 中途被杀在清库窗口
        # （count==0）、Chroma 被外部工具/清理/磁盘故障破坏（此前会陷入
        # 「每轮判 stale → 每轮修不了」的死循环）。全终态库期望 0 块，
        # 0==0 不误伤（判定逻辑见 core/index_integrity.py，与
        # `library_freshness` 共用同一份实现）。
        # full=True 时不判：整库重建本来就会把所有块重写一遍，多这一次 count
        # 探测纯属浪费（LEGACY 1903 行 `if not full and meta` 同条件）。
        rebuild_all = False
        if old_manifest is not None and not full:
            integrity_segments = self._manifest_segments(
                old_manifest, "vector_segments", previous
            )
            integrity_actual = index_integrity.count_store_chunks(
                lambda segment: vector_store.count(library_id, generation=segment),
                integrity_segments,
            )
            drift = index_integrity.rebuild_reason(
                old_manifest,
                actual_chunk_count=integrity_actual,
            )
            if drift is not None:
                logging.getLogger("rag_redo.core.pipeline").warning(
                    "一致性校验失败（%s）：manifest 期望 %d 块 vs 向量库实际 %s 块，"
                    "增量无法修复，自动转全量重建",
                    drift,
                    index_integrity.expected_chunk_count(old_manifest),
                    "未知" if integrity_actual is None else integrity_actual,
                )
                rebuild_all = True

        # ---- 三个 force_* 的关系（缺陷3，对齐 LEGACY 的两段语义）----------
        # LEGACY 里 `--full`（index.py:1891）与切块逻辑升级
        # （index.py:1893-1895 `meta.pop("_version") != META_VERSION` → 同样
        # `meta = {}`）都只是**丢掉旧条目表**，每个文件仍然走
        # `extract_to_markdown`，而它第一件事就是查提取缓存
        # （extractors.py:341-343 命中即秒回）。所以这两种重建都可以用来改
        # 切块粒度/换提取器而**不重复烧 MinerU 配额**；真正"当作缓存不存在"
        # 的只有 `--fresh-extract`（index.py:2450-2455 先 purge 缓存再索引）。
        # 三个变量因此必须是：
        #   force_extract = fresh_extract or 提取能力签名变化
        #       —— "本轮每个文件都要重新走一遍提取入口"。它是 LEGACY 那个
        #          `meta = {}` 的等价物：**只清空写盘链**（本轮往哪个 segment
        #          写），**不清空查找链**（可以从哪些 segment 读）——缓存命中
        #          时这次"重新提取"零成本，这才是"改切块粒度不烧配额"能成立
        #          的原因。真正清空查找链的只有 fresh_extract（见下方清空点）。
        #   force_chunks  = full or rebuild_all or force_extract or
        #                   切块器/文本管线签名变化
        #       —— full 与一致性自愈是"重切块 + 重嵌"（`full` 漏掉这一项会让
        #          `--full` 变成"什么也不做"，嵌入器一次都不被调用：LEGACY
        #          `meta = {}` 之后每个文件必然重新切块重嵌）。
        #   force_embed   = force_chunks or 嵌入器/向量库签名变化
        force_extract = fresh_extract
        if old_manifest is not None:
            force_extract = force_extract or any(
                old_signatures.get(point) != signatures.get(point)
                for point in signatures
                if point.startswith("extractor:")
            )
        force_chunks = full or rebuild_all or force_extract or (
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
        # 一致性自愈触发时按 full 的口径重试终态条目（LEGACY `meta = {}` 之后
        # 每个文件都会被重新走到）；`full` 用户显式要求的重建同理。
        rebuild_terminals = rebuild_all or full

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
            chunks_done_value: int | None = None,
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
                        chunks_done=chunks_done if chunks_done_value is None else chunks_done_value,
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
                    if rebuild_terminals or old.get("status") == "deferred" or not stable_terminal:
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
        # 三条链，各管一件事，别再合并成一条（合并过一次，代价就是
        # `full` 变成"什么也不做"）：
        #   cache_segments   —— 查找用：本轮可以从哪些 segment 读提取缓存。
        #   extract_segments —— 写盘用：本轮往哪些 segment 写提取缓存。
        #   extract_carry    —— 压缩用：本轮压缩后哪些 segment 的正文仍然
        #                        有效、必须被搬进 `{generation}-compact`。
        # 三者的差别只在 force_extract / fresh_extract 上体现，且顺序固定为
        # "老 → 新"（压缩段按倒序搬，同 route 只搬最新那份）。
        cache_segments = list(extract_segments)
        extract_carry = list(extract_segments)
        vector_carry = list(vector_segments)
        lexical_segments = self._manifest_segments(old_manifest, "lexical_segments", previous)
        if force_embed:
            # 写盘链从零开始（本轮只写进 `generation`），但**老段仍留在
            # `vector_carry` 里当压缩的读源**：deferred 语义要求旧块原样保留
            # 继续服务（index.py:2142-2148 "不动 meta"），而这一轮被 deferred
            # 跳过的文件一个新块都没写，它的块只在老集合里——压缩时若只从
            # 写盘链搬，这些块会静默从检索里消失（记录还写着 indexed，用户
            # 却再也搜不到自己刚建的库）。
            vector_segments = []
        if force_extract:
            # LEGACY `meta = {}` 的等价物：写盘链从零开始（本轮只写进
            # `generation`），但**查找链原样保留**——deferred/冻结文件的旧
            # 正文还得继续服务（index.py:2142-2148 "不动 meta"），已经重新
            # 提取成功的文件也会把新正文写进本轮 segment（压缩时新正文优先）。
            extract_segments = []
        if fresh_extract:
            # 全项目唯一"当作提取缓存不存在"的地方：清掉查找链，保证真的
            # 重新解析正文（对齐 LEGACY --fresh-extract 先 purge 缓存）。
            cache_segments = []
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

        pending: list[_PendingFile] = []
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
                # "内容没变、只是要重新切块/重嵌"（full、一致性自愈、嵌入器
                # 或切块器签名变化）——对齐 LEGACY：`--full` 下每个文件仍然
                # 走 `extract_to_markdown`，而它先查提取缓存
                # （extractors.py:341-343 命中即秒回），所以改切块粒度不会
                # 重复烧 MinerU 配额。读的是 `cache_segments`（查找用链），
                # `fresh_extract` 已经把它清空，这里必然落空并真的重解析。
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
            # tbd 占位检查只作用于纯文本格式（md/txt）——旧 index.py:1571
            # `if suffix in TEXT_EXTS and is_tbd_heavy(...)`：二进制格式
            # （docx/pdf）的提取文本里出现 [TBD] 占位是正常内容（如课程报告
            # 模板），不做占位率跳过。2026-09-25 实测发现漏了这条限定，导致
            # 旧项目正常建索引的 HEBAT3_Technical_Report_BACKUP.docx（182块）
            # 在这里被误判 tbd 跳过。
            ext_for_tbd = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext_for_tbd in ("md", "txt") and is_tbd_heavy(
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
            # 问题48 清洗链（顺序有讲究，逐字对齐旧 _store_chunks 1934-1939）：
            # 死图链 → [官方 sidecar 精确删（仅 MinerU 云端提取过的文件有；
            # 读不到自动跳过）] → 页码行 → 样板行。先剥图链（避免重复图片
            # 路径行被误判成样板）；sidecar 只精确删官方标注的页眉/页脚/页码，
            # 删不中的残差交给启发式兜底。纯文本变换，不影响终态判定。
            index_text = strip_dead_image_refs(index_text)
            _sidecar = self._read_sidecar(library_id, doc.content_hash or plan["content_hash"])
            if _sidecar:
                index_text = strip_sidecar_noise(index_text, _sidecar)
            index_text = strip_page_number_lines(index_text)
            index_text = strip_boilerplate_lines(index_text)
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
            # 不在这里向量化/写库：先把这个文件的块攒起来，等全部文件都转换切块完，再统一
            # 连续向量化、统一写入——对齐旧项目 obsidian-rag/index.py:2056-2300 的顺序
            # （“先转换、后嵌入、一次写锁”）。此前是“一个文件：提取→切块→向量→写库”再下
            # 一个，显卡每做完一个小文件就闲下来等 CPU，MinerU/WEMM 与向量模型还在每个
            # 文件之间来回抢显存（2026-09-29 真机：GPU 功耗在 0 和高值之间来回跳、显存反复
            # 冒尖又回落）。见本函数循环之后的第二段（向量化）与第三段（写入）。
            pending.append(
                _PendingFile(
                    plan=plan,
                    file_report=file_report,
                    chunks=chunks,
                    ctx_by_id=ctx_by_id,
                    section_counts=section_counts,
                    section_headings=section_headings,
                    section_texts=section_texts,
                    raw_links=raw_links,
                    extracted_by=doc.extracted_by,
                    extractor_version=doc.extractor_version,
                    content_hash=doc.content_hash or plan["content_hash"],
                )
            )
            files_done += 1
            _emit("file_complete", current_path=path, message=f"已转换切块：{path}")

        # ---- 第二段：全部待嵌块连续向量化（对齐旧 index.py:2238-2255）--------------------
        # 分片只为给进度心跳/停滞看门狗喘气，不是“一个文件一次”：跨文件攒成连续的调用，
        # 显卡不会在两个文件之间闲下来等 CPU。片内按文本长度从长到短排好，每个小批里的
        # 块长度相近、填充浪费最少（最长的排最前，显存/OOM 类问题也尽早暴露）；结果按
        # chunk_id 取回，与顺序无关。
        vectors_by_id: dict[str, object] = {}
        pending_chunks = [chunk for item in pending for chunk in item.chunks]
        embedded_total = len(pending_chunks)
        if pending_chunks:
            _emit(
                "embedding",
                chunks_total=embedded_total,
                chunks_done_value=0,
                stall_grace_s=300.0,
                message=f"正在向量化 0/{embedded_total} 块",
            )
            ordered_chunks = sorted(pending_chunks, key=lambda chunk: len(chunk.text), reverse=True)
            embedded = 0
            for start in range(0, embedded_total, _EMBED_SLICE):
                piece = ordered_chunks[start : start + _EMBED_SLICE]
                for vector in embedder.embed_chunks(piece):
                    vectors_by_id[vector.chunk_id] = vector
                embedded += len(piece)
                _emit(
                    "embedding",
                    chunks_total=embedded_total,
                    chunks_done_value=embedded,
                    stall_grace_s=300.0,
                    message=f"正在向量化 {embedded}/{embedded_total} 块",
                )

        # 口径与改动前一致：向量化完成后，chunks_done 是“本库全部块数”（未变文件 + 本轮新嵌的块）
        chunks_done += embedded_total

        # ---- 第三段：统一写入向量库/词法库，生成每个文件的清单记录 -----------------------
        # 仍写进本轮独立 generation，最后才原子发布——任何一步异常整轮不发布，旧索引不动。
        if pending:
            _emit(
                "writing",
                stall_grace_s=180.0,
                message=f"正在写入 {len(pending)} 个文件的索引",
            )
            buffer_ids: list[str] = []
            buffer_vectors: list[list[float]] = []
            buffer_documents: list[str] = []
            buffer_metadatas: list[dict] = []

            def _flush_vector_buffer() -> None:
                if not buffer_ids:
                    return
                vector_store.upsert(
                    library_id,
                    list(buffer_ids),
                    list(buffer_vectors),
                    documents=list(buffer_documents),
                    metadatas=list(buffer_metadatas),
                    generation=generation,
                )
                buffer_ids.clear()
                buffer_vectors.clear()
                buffer_documents.clear()
                buffer_metadatas.clear()

            for item in pending:
                plan = item.plan
                chunks = item.chunks
                chunk_ids = [chunk.chunk_id for chunk in chunks]
                file_vectors = [vectors_by_id[chunk_id] for chunk_id in chunk_ids]
                _drop_old_lexical_chunks(plan)
                buffer_ids.extend(chunk_ids)
                buffer_vectors.extend(list(vector.vector) for vector in file_vectors)
                buffer_documents.extend(chunk.text for chunk in chunks)
                buffer_metadatas.extend(
                    {
                        "path": chunk.path,
                        "heading_breadcrumb": chunk.heading_breadcrumb,
                        "chunk_index": chunk.chunk_index,
                        "section_id": chunk.section_id,
                        "ctx": item.ctx_by_id.get(chunk.chunk_id, ""),
                    }
                    for chunk in chunks
                )
                if len(buffer_ids) >= _WRITE_FLUSH_CHUNKS:
                    _flush_vector_buffer()
                    _emit("writing", stall_grace_s=180.0, message="正在写入向量库")
                if lexical_current:
                    for chunk in chunks:
                        lexical.index_chunk(chunk, generation=generation)
                first_vector = file_vectors[0]
                plan["record"] = {
                    "size": plan["size"],
                    "mtime_ns": plan["mtime_ns"],
                    "content_hash": item.content_hash,
                    "status": "indexed",
                    "failure_state": None,
                    "failure_reason": None,
                    "failure_detail": None,
                    "capability_signature": plan["capability_signature"],
                    "extractor_id": item.extracted_by,
                    "extractor_version": item.extractor_version,
                    "chunker_id": chunks[0].chunked_by,
                    "chunker_version": chunks[0].chunker_version,
                    "embedder_id": first_vector.model_id,
                    "embedder_version": first_vector.model_version,
                    "dim": first_vector.dim,
                    "chunk_ids": chunk_ids,
                    "sections": {
                        section_id: {
                            "heading": item.section_headings[section_id],
                            "text": item.section_texts[section_id],
                            "chunk_count": item.section_counts[section_id],
                        }
                        for section_id in item.section_counts
                    },
                    "links": item.raw_links,
                }
                item.file_report.extracted = True
                item.file_report.chunk_count = len(chunks)
            _flush_vector_buffer()

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
            # 压缩的读源是"老段 + 本轮段"（倒序合并 = 新块覆盖同 id 的老块），
            # 不是写盘链 `vector_segments`：force_embed（full / 一致性自愈 /
            # 嵌入器或切块器签名变化）会把写盘链清空到只剩本轮 generation，
            # 而这一轮被 deferred 跳过的文件一个新块都没写，它的块只在老集合
            # 里——只从写盘链搬，这些块会静默从检索里消失（记录仍写着
            # indexed，用户却再也搜不到自己刚建的库）。同理，正文来源用
            # `extract_carry` 而不是 `extract_segments`（force_extract 会把
            # 写盘链清空），否则 read_document 看不到 deferred 文件的正文。
            vector_source: list[str] = []
            for segment in (*vector_carry, *vector_segments):
                if segment and segment not in vector_source:
                    vector_source.append(segment)
            extract_source: list[str] = []
            for segment in (*extract_carry, *extract_segments):
                if segment and segment not in extract_source:
                    extract_source.append(segment)
            if active_ids:
                _emit("writing", stall_grace_s=180.0, message="正在压缩向量索引")
                rows = self._vector_rows(library_id, vector_source, active_ids)
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
                for segment in reversed(extract_source):
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
                before_serve=self._release_text_models_for_visual,
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
        try:
            self.prune_unreferenced_data()
        except Exception as exc:  # noqa: BLE001 - 回收失败绝不影响索引结果（旧 2496-2499 同纪律）
            logging.getLogger("rag_redo.core.pipeline").info("全局回收失败（忽略）：%s", exc)
        return report

    def prune_unreferenced_data(self) -> tuple[int, int, int]:
        """索引完成后一次性全局回收（问题49，用户拍板"全清，没用到就删"）——
        对齐旧 index.py::prune_unreferenced_data（2307-2437）的职责范围，
        按本项目的存储布局适配：

        1. 已从注册表移除的库 → manifests/index_failures/generations/
           extract_cache 下它的目录一并清（库级文件/块清理每轮索引已做，
           这里只清"整个库都没了"的残留）；
        2. 活跃 generation 的提取缓存孤儿 → 不在 `_index.json` 反查表里的
           `<hash>.txt` 删除；
        3. 向量库残留集合 → `libg_*` / `lib_*` 里既不属于任何在册库、
           也不被任何在册库保留的 generation/segment 引用的删掉
           （判定见 `_prune_stale_collections`）。

        幂等；任何单步失败只记日志、绝不抛，不影响索引主流程。
        返回 (删缓存文件数, 删 collection 数, 删已删库目录数)。"""
        lib_mgr = self._singleton("library_manager")
        live_library_ids = {cfg.library_id for cfg in lib_mgr.store.list_libraries()}
        # 各存储的目录命名规则不同，孤儿判定必须按各自的命名来——
        # manifests / failures / relations 用 `library_storage_key`
        # （安全名+哈希后缀，core/library_key.py）；extract_cache 直接用
        # 原始库 id。用错一边会把活库目录误判成孤儿。
        live_manifest_keys = {self._manifests._key(library_id) for library_id in live_library_ids}
        live_safe_names = {library_storage_key(library_id) for library_id in live_library_ids}

        def _prune_orphan_dirs(root: Path, suffix: str, live_names: set[str]) -> int:
            if not root.is_dir():
                return 0
            removed = 0
            for child in root.iterdir():
                if child.is_dir() and child.name not in live_names:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
                    logging.getLogger("rag_redo.core.pipeline").info("全局回收：删已删库的%s目录 %s", suffix, child.name)
            return removed

        n_dirs = 0
        n_dirs += _prune_orphan_dirs(self._manifests._root, "manifest", live_manifest_keys)
        n_dirs += _prune_orphan_dirs(self._index_failures._root / "generations", "失败诊断", live_safe_names)
        n_dirs += _prune_orphan_dirs(self._note_relations._root / "generations", "关系", live_safe_names)
        n_dirs += _prune_orphan_dirs(self._extract_cache._root, "提取缓存", live_library_ids)

        # 活跃 generation 的提取缓存孤儿（hash 不在反查表里）
        n_cache = 0
        for cfg in lib_mgr.store.list_libraries():
            generation = self._generations.active(cfg.library_id)
            if not generation:
                continue
            cache_dir = self._extract_cache._dir_for(cfg.library_id, generation)
            index = self._extract_cache._load_index(cfg.library_id, generation)
            if not cache_dir.is_dir():
                continue
            for file in cache_dir.iterdir():
                if not file.is_file() or file.name == "_index.json":
                    continue
                stem = file.name.split(".")[0]
                if stem not in index:
                    try:
                        file.unlink()
                        n_cache += 1
                    except OSError:
                        pass

        n_collections = self._prune_stale_collections(live_library_ids)

        if n_cache or n_dirs or n_collections:
            logging.getLogger("rag_redo.core.pipeline").info(
                "全局回收完成：删提取缓存孤儿 %d 个、残留集合 %d 个、已删库目录 %d 个",
                n_cache, n_collections, n_dirs,
            )
        return (n_cache, n_collections, n_dirs)

    def _live_generations(self, library_id: str) -> set[str]:
        """这个库当前**必须留着数据**的 generation/segment 名集合。

        判定范围严格照抄 `index_library()` 收尾那段"谁还在被引用"的算法
        （manifest 分段 + generation 指针 history + 压缩段），
        再加一条**在途**保护：

        - manifest 分段：`self._manifests.list_generations()` 列出的每个
          generation 的 `vector_segments`/`lexical_segments`/`extract_segments`
          ——deferred 与 Agent 冻结语义刻意让**旧** generation 的集合继续
          在用（被冻结文件的旧块只在旧集合里），只看 active 会误删。
        - generation 指针的 active + history：搜索只读 active，但收尾清理
          （`index_library` 末尾的 `candidates - keep - referenced`）会把
          history 也留着，回收必须跟它一致。
        - `-compact` 后缀：压缩把活跃块搬进 `{generation}-compact` 段并删掉
          原段（`index_library` 的 compacted_vector_segment 分支），两个名字
          都要在存活集合里。
        - **在途 run**：`index_progress` 里 stage 仍是 starting/running 的
          worker 正在往 `libg_<sha(库,run_id)>` 写，此刻它还没有 manifest
          （manifest 在索引最末尾才原子发布），只按 manifest 判就会把另一个
          进程正在写的集合删掉——worker 的 run_id 就是它用的 generation_id
          （`core/index_progress.py:415-418` 传的 `generation_id=run_id`）。
        """
        generations: set[str] = set()
        active = self._generations.active(library_id)
        if active:
            generations.add(active)
        generations.update(self._generations.history(library_id))
        for generation in self._manifests.list_generations(library_id):
            generations.add(generation)
            manifest = self._manifests.read(library_id, generation)
            for field in ("vector_segments", "lexical_segments", "extract_segments"):
                values = manifest.get(field, []) if manifest else []
                if isinstance(values, list):
                    generations.update(str(value) for value in values if value)
        try:
            status = self._index_progress.status(library_id)
        except Exception:
            status = None
        if isinstance(status, dict) and status.get("stage") in {"starting", "running"}:
            run_id = str(status.get("run_id") or "")
            if run_id:
                generations.add(run_id)
        return generations

    def _prune_stale_collections(self, live_library_ids: set[str]) -> int:
        """清扫向量库残留集合（对齐 obsidian-rag/index.py:2404-2432）。

        **判定"可删"的确切规则**（缺一不可）：
        1. 名字以 `libg_` / `libk_` / `lib_` 开头——只动 official-vector-store-chroma
           自己建的命名空间。别的插件/别的库共用同一个 Chroma 目录时，
           不属于我们的集合一律不碰。视觉页库（WEMM）用的是**另一个
           Chroma 目录**（`plugins/official-visual-wemm/.../plugin.py:148-150`
           的 `visual_wemm/chroma`）和自己的 `visual_`/`visualg_` 命名空间，
           根本不会出现在这里的清单里，也就不在核心的回收职责内。
        2. 且它不在任何在册库的存活集合里（由插件的 `collection_name_for`
           正向算出来的全集）。

        **正向枚举解决了"名字不可逆"这个老问题**：当年放弃清扫的理由是
        集合名 `libg_<sha256(库id, generation)>` 反推不出库 id。这里根本
        不反推——哈希是单向的，那就从"库 id + generation"**正向算**出名字
        （`_live_generations` 已经知道每个在册库还留着哪些代），再拿名字去
        和实际清单求差集。反推是走不通的，正推是白送的。
        """
        vector_store = self._singleton("vector_store")
        list_names = getattr(vector_store, "list_collection_names", None)
        delete_by_name = getattr(vector_store, "delete_collection_by_name", None)
        if not callable(list_names) or not callable(delete_by_name):
            # 存储插件不支持按名清扫（自定义 vector_store 实现）——降级为
            # 不删，绝不猜接口（AGENTS.md §4.4：命名空间只由 DataStore 发）。
            logging.getLogger("rag_redo.core.pipeline").info(
                "全局回收：当前 vector_store 不支持按名清扫，跳过集合回收"
            )
            return 0
        keep: set[str] = set()

        # 集合名算法归 official-vector-store-chroma 所有（它才是真正
        # 调 Chroma 的人，也只有它知道 Chroma 的集合名合法化规则——见
        # store.py::chroma_collection_name）。这里过去自己复算了一遍
        # `f"lib_{library_id}"`，属于 AGENTS.md §4.5 明令禁止的"同一个业务
        # 判断两个权威实现"：插件一侧把中文库 id 合法化成 `libk_<sha>` 之后，
        # 这里的白名单还只会算 `lib_中文`，于是回收会把一个**正在使用的**
        # 集合当成残留删掉——用户可观察的数据丢失。问插件要名字，两边永远
        # 一致；自定义 store 实现没提供这个方法就退回旧算法并记日志。
        name_for = getattr(vector_store, "collection_name_for", None)
        if not callable(name_for):
            logging.getLogger("rag_redo.core.pipeline").info(
                "全局回收：当前 vector_store 不提供 collection_name_for，"
                "存活白名单退回旧算法（可能与插件实际命名不一致）"
            )

            def _segment_collection(library_id: str, segment: str) -> str:
                digest = hashlib.sha256(
                    f"{library_id}\0{segment}".encode("utf-8")
                ).hexdigest()[:40]
                return f"libg_{digest}"

            def _plain_collection(library_id: str) -> str:
                return f"lib_{library_id}"

        else:

            def _segment_collection(library_id: str, segment: str) -> str:
                return str(name_for(library_id, segment))

            def _plain_collection(library_id: str) -> str:
                return str(name_for(library_id, None))

        for library_id in sorted(live_library_ids):
            # 无 generation 的集合：官方 Chroma store 在 generation 解析为空
            # 时用它（`lib_<库id>`，库 id 不合法时是 `libk_<sha>`），必须
            # 一起保住。
            keep.add(_plain_collection(library_id))
            for segment in self._live_generations(library_id):
                keep.add(_segment_collection(library_id, segment))
                keep.add(_segment_collection(library_id, f"{segment}-compact"))
        removed = 0
        try:
            names = set(list_names())
        except Exception as exc:  # noqa: BLE001 - 回收失败绝不影响索引结果
            logging.getLogger("rag_redo.core.pipeline").info("全局回收：列集合失败（忽略）：%s", exc)
            return 0
        for name in sorted(names):
            if name in keep or not _is_rag_collection_name(name):
                continue
            delete_by_name(name)
            removed += 1
            logging.getLogger("rag_redo.core.pipeline").info("全局回收：删残留集合 %s", name)
        return removed


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

    def library_rows(self) -> list[dict]:
        """list_libraries 的富行数据——对齐旧 library.py::list_summary
        （622-656）：块数（向量库 count，collection 未创建=从未索引=0）与
        最近索引时间（当前 generation manifest 文件的 mtime，从未=None）
        都不加载模型。summary 深浅由 library_summary 插件是否存在决定
        （可选插件缺席时 rows 仍完整，summary 一律 None）。"""
        lib_mgr = self._singleton("library_manager")
        vector_store = self._singleton("vector_store")
        summary_plugin_id = self.runtime.registry.active_of("library_summary")
        summary_plugin = self._plugin(summary_plugin_id) if summary_plugin_id else None
        rows = []
        for cfg in lib_mgr.store.list_libraries():
            generation = self._generations.active(cfg.library_id)
            try:
                blocks = vector_store.count(cfg.library_id, generation=generation)
            except Exception:
                blocks = 0  # collection 未创建 = 从未索引（旧 list_summary 同语义）
            last_indexed = None
            if generation:
                manifest_path = self._manifests._path_for(cfg.library_id, generation)
                try:
                    last_indexed = manifest_path.stat().st_mtime
                except OSError:
                    pass
            summary = None
            summary_stale = False
            if summary_plugin is not None:
                try:
                    summary = summary_plugin.get(cfg.library_id)
                except Exception:
                    summary = None
            if summary is not None and summary.text and summary.fingerprint:
                try:
                    summary_stale = summary_plugin.is_stale(
                        cfg.library_id, self.library_content_fingerprint(cfg.library_id)
                    )
                except Exception:
                    summary_stale = False
            rows.append({
                "library_id": cfg.library_id,
                "name": cfg.name,
                "root_path": cfg.root_path,
                "blocks": blocks,
                "last_indexed": last_indexed,
                "summary": summary.text if (summary is not None and summary.text) else None,
                "summary_stale": summary_stale,
            })
        return rows

    def index_stats(self, library_id: str) -> dict:
        """某个库当前生效 generation 的只读统计（GUI 全局快照的权威数据源）。

        返回 `{files, chunks, indexed_at, state}`：
        - `files` = manifest 里记录的**全部**文件数——含已落终态（empty/tbd/scanned/
          unreadable…）与失败的文件，因为它们同样是"这一轮见过并处理过的文件"。口径同旧
          项目 `gui/store.py::meta_stats_for`（数 meta 里全部 dict 条目，失败条目也在
          meta 里）。想要"成功入库了几个"看 `index_failures(...)["succeeded"]`；
        - `chunks` = 生效的块 id 数；
        - `indexed_at` = manifest 文件 mtime（从未索引=None）；
        - `state` = `none`（没有生效 generation）/ `ok`。

        不加载模型、不碰向量库——全部来自 manifest 文件，所以每秒轮询一次
        的 GUI 快照不会因为反复调用而把 BGE/reranker 拖进显存。
        """
        generation = self._generations.active(library_id)
        if not generation:
            return {"files": 0, "chunks": 0, "indexed_at": None, "state": "none"}
        manifest = self._manifests.read(library_id, generation)
        if manifest is None:
            return {"files": 0, "chunks": 0, "indexed_at": None, "state": "none"}
        indexed_at: float | None
        try:
            indexed_at = self._manifests._path_for(library_id, generation).stat().st_mtime
        except OSError:
            indexed_at = None
        return {
            "files": len(manifest.get("files") or ()),
            "chunks": len(manifest.get("active_chunk_ids") or ()),
            "indexed_at": indexed_at,
            "state": "ok",
        }

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

    def visual_page_states(self, library_id: str) -> tuple[VisualPageState, ...]:
        """某库当前 generation 里，全部 `visual_index` 提供者已建的**逐 PDF 页级状态**
        （只读，不加载模型、不拉起服务）。图谱的页节点和 GUI 的"WEMM 页库状态"表都吃这份
        数据；没有视觉提供者 / 库没有生效 generation → 空元组。单个提供者读失败只跳过
        它自己，不让整份诊断失败。"""
        generation = self._generations.active(library_id)
        if not generation:
            return ()
        states: list[VisualPageState] = []
        for plugin_id in sorted(self.runtime.registry.providers_of("visual_index")):
            try:
                visual = self._plugin(plugin_id)
                page_reader = getattr(visual, "graph_page_states", None)
                generated = page_reader(library_id, generation) if callable(page_reader) else ()
                states.extend(state for state in generated if isinstance(state, VisualPageState))
            except Exception:
                continue
        return tuple(states)

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
            page_states[library_id] = self.visual_page_states(library_id)
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

    def start_index_libraries(
        self,
        library_ids: list[str] | tuple[str, ...],
        source: str = "api",
        full: bool = False,
    ) -> IndexStartResult:
        """一次重建**多个库**——依次一个一个跑，不是同时开跑。

        对齐 obsidian-rag `index.py` 的 `__main__`：一次调用只起一个索引进程、按库逐个
        `for` 循环，某个库失败记日志后继续下一个（旧项目日志 `索引失败（继续下一库）`）。
        此前 GUI 对每个库各调一次 `start_index_library`，4 个库就是 4 个 worker 同时加载
        模型、同时压满显卡和 CPU。排队与"前一个结束再起下一个"由
        `IndexWorkerManager.start_batch` 负责；入口层（GUI）不许自己拼这个顺序。

        任一库不存在就整批拒绝（抛 `KeyError`，和单库入口同口径）；第一个库起不来则整批
        不开始，返回失败原因。
        """
        lib_mgr = self._singleton("library_manager")
        ids = list(dict.fromkeys(library_ids))
        for library_id in ids:
            if lib_mgr.store.get(library_id) is None:
                raise KeyError(f"未知库: {library_id}")
        self._index_progress.set_active_choices(self.runtime.registry.active_choices())
        return self._index_progress.start_batch(ids, source, full)

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
          （index.py:1522-1529），避免每轮无效重建；
        - 能力签名升级（切块/嵌入/提取/存储插件的 `index_signature()` 变了）
          → stale（index.py:1518-1519 的 `version_upgrade`，本项目的
          `META_VERSION` 等价物是 manifest 里的 `signatures` 段）——升级后
          检索前的自动同步必须自己发现并重建，而不是等用户察觉后手动
          `--full`；
        - manifest 期望块数 > 向量库实际块数（块被外部动库/清理/磁盘故障
          弄丢）→ stale（index.py:1592-1597），让"增量修不了"的缺口走
          全量重建自愈，而不是卡在「每轮判 stale → 每轮修不了」。

        显式触发的索引（GUI 完整重建、reindex_knowledge）没有这层保护，
        与旧项目一致：用户明确要求重建时按字面执行。"""
        lib_mgr = self._singleton("library_manager")
        entries = lib_mgr.resolve_libraries(libraries or "all", exclude)
        signatures = self._pipeline_signatures()
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
            # 签名升级自愈（LEGACY index.py:1518-1519 先于逐文件比较返回）——
            # 旧索引是旧逻辑产的，逐文件 stat 全命中也会被判"没变化"，所以
            # 这条必须在文件比较之前短路。
            if index_integrity.rebuild_reason(manifest, signatures=signatures):
                report[library_id] = LibraryFreshness(library_id=library_id, stale=True)
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
            if not stale:
                # 一致性自愈（LEGACY index.py:1592-1597 放在逐文件比较**之后**
                # 或进 stale，而不是提前返回——它是对"已经算出来没变化"这个
                # 结论的额外否决项）。探测失败（count 抛异常）→ 判不出漂移 →
                # fail-open 放行，同 LEGACY `_chroma_count` 返回 None 的处理。
                vector_store = self._singleton("vector_store")
                segments = self._manifest_segments(manifest, "vector_segments", generation)
                actual = index_integrity.count_store_chunks(
                    lambda segment: vector_store.count(library_id, generation=segment),
                    segments,
                )
                stale = (
                    index_integrity.consistency_drift(
                        index_integrity.expected_chunk_count(manifest), actual
                    )
                    is not None
                )
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
        return list(self.search_delivery(
            libraries,
            query,
            top_k=top_k,
            exclude=exclude,
            folder=folder,
            include_body=include_body,
            format_allowlist=format_allowlist,
        ).results)

    def search_delivery(
        self,
        libraries: str,
        query: str,
        *,
        top_k: int = 10,
        exclude: str = "",
        folder: str = "",
        include_body: bool = True,
        format_allowlist: tuple[str, ...] | Mapping[str, tuple[str, ...]] | None = None,
    ) -> SearchDelivery:
        """"交出结果的这一批是怎么交出来的"——结果 + 封顶/折叠/候选计数。

        `search()` 是它的薄封装（只要结果本体）。两者分开而不是给 `search()`
        加个 `return_delivery=False` 开关：调用方要么要列表要么要交付态，
        布尔开关会让两个返回类型在签名里不可见，读代码的人得翻实现才知道
        这次拿到的到底是啥。
        """
        first = self._search_once(
            libraries,
            query,
            top_k=top_k,
            exclude=exclude,
            folder=folder,
            include_body=include_body,
            format_allowlist=format_allowlist,
        )
        top_confidence = first.results[0].confidence if first.results else None
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
            first_confidence = first.results[0].confidence if first.results else -1.0
            second_confidence = second.results[0].confidence if second.results else -1.0
            # 连带交付态一起选：HyDE 换了查询语句，候选集/封顶/折叠计数都是
            # 那一支的，不能拿 first 的计数去描述 second 的结果。
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
        delivery = self.search_delivery(
            libraries,
            query,
            top_k=top_k,
            exclude=exclude,
            folder=folder,
            include_body=include_body,
            format_allowlist=format_allowlist,
        )
        request = SearchAdviceInput(
            results=delivery.results,
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
            capped=delivery.capped,
            folded=delivery.folded,
            empty_reason=delivery.empty_reason(),
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
        return SearchResponse(results=delivery.results, advice=tuple(advice))

    def _search_once(
        self,
        libraries: str,
        query: str,
        *,
        top_k: int,
        exclude: str,
        folder: str,
        include_body: bool,
        format_allowlist: tuple[str, ...] | Mapping[str, tuple[str, ...]] | None,
    ) -> SearchDelivery:
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
            return SearchDelivery(results=())

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
            return SearchDelivery(results=(), candidates=len(pool_ids))

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
            return SearchDelivery(results=(), candidates=0)

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
        # 封顶/折叠计数（对齐 obsidian-rag/retriever.py 传给 advice 的
        # `capped` / `folded`）。这两个信号以前在 `_search_once` 里算出来
        # 却无处可去，交付层直接返回结果本体，把"这一批被封顶/折叠过"这个
        # 用户可观察的事实连同计数一起丢掉了。
        capped = False
        folded = 0
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
                folded += 1
                continue
            if include_body and per_file_count.get(file_key, 0) >= max_chunks_per_file:
                capped = True
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
        return SearchDelivery(
            results=tuple(results),
            capped=capped,
            folded=folded,
            candidates=len(merged),
        )

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

    #: 会真正占用本机 GPU 显存的插件——检索侧 embedder+reranker 共用同一个
    #: "gpu:0" 名额；WEMM/MinerU-local 各自在独立子进程里装自己的模型。
    #: official-ocr-mineru-cloud 是纯云端 API，不占本机显存，不在这个列表里。
    _GPU_CONSUMER_PLUGIN_IDS = (
        "official-embedder-bge-m3",
        "official-reranker",
        "official-visual-wemm",
        "official-ocr-mineru-local",
    )

    def _release_text_models_for_visual(self) -> None:
        """页级视觉索引真要占显卡之前的"让路"：把文字向量模型与重排模型从显卡上卸下来。

        对齐 obsidian-rag/index.py:1784-1806 `_release_for_wemm`（经 wemm_indexer.py 的
        `before_serve` 回调）。2026-09-29 真机：文字嵌入做完后 bge-m3 还带着 GPU 名额，
        名额优先级比 WEMM 高，WEMM 抢不到 → 4 个库的页级索引整轮被跳过、页库全记失败。
        只在视觉插件"确有页要渲染"时才被调用（无页可渲染的纯 md 增量不会白白卸模型，
        下次搜索也不必重新装）。每个插件独立隔离：一个卸不掉不能连累另一个，也绝不
        抛出来拖垮页级索引；模型下次真正用到时会懒加载回来。"""
        for plugin_id in ("official-embedder-bge-m3", "official-reranker"):
            plugin = self.runtime.plugins.get(plugin_id)
            if plugin is None or plugin.instance is None or plugin.state.value != "enabled":
                continue
            release = getattr(plugin.instance, "release_gpu", None)
            if release is None:
                continue
            try:
                release()
            except Exception as exc:  # noqa: BLE001 - 让路失败不能连累其它插件/页级索引
                logging.getLogger("rag_redo.core.pipeline").warning(
                    "页级视觉索引前释放 %s 的显存失败（忽略）：%s: %s", plugin_id, type(exc).__name__, exc
                )

    def release_gpu_memory(self) -> dict:
        """手动立即释放显存（2026-09-29 操作者需求；旧项目 guiweb 没有对应
        按钮，已在 docs/behavior_contract.json 登记为 BC-16 新能力）。

        **只卸载模型、归还 GPU 名额，不碰子进程/插件启用状态/持久化配置**：
        用户要的是"现在不占显存"，不是"把检索/视觉导航/OCR 关掉、以后还得
        手动去设置页重新打开"——GUI 目前也没有运行中重新 enable 一个插件的
        入口，真把插件 disable 掉会让这些功能卡死到下次重启整个 GUI 才能用
        （2026-09-29 与操作者确认过这一点）。

        - embedder/reranker（检索侧，`release_gpu()` 内部调用与
          `on_disable()` 相同的 `release_gpu_slot()`）：模型从显存卸载，
          `_ensure_loaded()` 的懒加载语义保证下一次真正检索/索引时会透明
          重新装回来，用户无感，只是要多等几秒冷启动。
        - official-visual-wemm/official-ocr-mineru-local（各自独立子
          进程）：`release_gpu()` 内部调用软驱逐（`_soft_evict()`，与资源
          仲裁器抢占时走的同一条路径）——请求子进程卸载模型释放显存，
          子进程本身继续存活监听，下次查询会按需重新加载模型。

        每个插件的释放动作互相独立：没启用/没找到就跳过（不是错误——比如
        WEMM 后端本来就关着），某一个插件释放失败不能连累其它插件也释放
        不到（同 `on_disable()` 收口的宽容纪律，try/except 逐个隔离）。"""
        released: list[str] = []
        skipped: list[str] = []
        errors: dict[str, str] = {}
        for plugin_id in self._GPU_CONSUMER_PLUGIN_IDS:
            plugin = self.runtime.plugins.get(plugin_id)
            if plugin is None or plugin.instance is None or plugin.state.value != "enabled":
                skipped.append(plugin_id)
                continue
            release = getattr(plugin.instance, "release_gpu", None)
            if release is None:
                skipped.append(plugin_id)
                continue
            try:
                release()
                released.append(plugin_id)
            except Exception as exc:  # noqa: BLE001 - 一个插件释放失败不能连累其它插件
                errors[plugin_id] = f"{type(exc).__name__}: {exc}"
        return {"released": released, "skipped": skipped, "errors": errors}

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
        threshold: float = 0.8,
        format_allowlist: tuple[str, ...] | None = None,
    ) -> dict[str, list[list[str]]]:
        """近似重复检测（只读建议，绝不自动删/移动文件）——对齐
        obsidian-rag 的 `find_duplicates`（server.py:828-850 / dedup.py:119）。

        返回形状是 `{dedup 插件 id: [[相对路径, ...], ...]}`：**单库**视角。
        多库并查走 `find_duplicates_multi`（旧项目的 `find_duplicates` 本来
        就支持逗号分隔多库与 "all"，MCP 工具直接调那个）。

        阈值默认 **0.8**，对齐旧项目 `dedup.py:34 DEFAULT_THRESHOLD`。原来这里
        是 0.7，而 MCP 工具的默认值是 0.8——同一件事两个默认值，走 GUI 和走
        对话给出的重复组会不一样，排查时根本看不出是阈值差。
        """
        report = self.find_duplicates_multi(
            [library_id], threshold=threshold, format_allowlist=format_allowlist
        )
        return report[library_id]["groups"]

    def find_duplicates_multi(
        self,
        library_ids: Sequence[str],
        *,
        threshold: float = 0.8,
        format_allowlist: tuple[str, ...] | Mapping[str, tuple[str, ...]] | None = None,
    ) -> dict[str, dict[str, object]]:
        """多库近似重复检测——对齐 obsidian-rag/server.py:842-853（逗号分隔或
        "all" 逐库各跑一遍、逐库出一段报告）。

        返回 `{库 id: {"groups": {插件 id: [[路径,...]]}, "scanned": N,
        "skipped": N}}`。`skipped` 是"跳过了多少个拿不到正文的文件"——旧项目
        明确会报这个数（server.py:833「未提取的文件会跳过并计数」），少了它，
        一份"零重复组"的报告就分不清是真的没有重复，还是因为全部文件都没提取
        正文而压根没比较过。

        **`format_allowlist` 的两种形态**（与 `search`/`library_freshness` 同一约定）：
        单个后缀元组 = 每个库都用同一份；**映射 `{库 id: 后缀元组}`** = 每个库用自己的
        Agent 授权格式。多库调用必须传映射——各库授权的二进制格式各不相同，拿第一个库的
        授权去套全部库、或干脆不过滤，都会把某个库**未授权**格式的文件名和重复关系泄露
        给 Agent（BC-02）。映射里缺失的库 fail-closed：视为一个格式都没授权。
        """
        results: dict[str, dict[str, object]] = {}
        for library_id in library_ids:
            groups, scanned, skipped = self._find_duplicates_one(
                library_id,
                threshold=threshold,
                format_allowlist=self._allowlist_for(library_id, format_allowlist),
            )
            results[library_id] = {"groups": groups, "scanned": scanned, "skipped": skipped}
        return results

    @staticmethod
    def _allowlist_for(
        library_id: str,
        spec: tuple[str, ...] | Mapping[str, tuple[str, ...]] | None,
    ) -> tuple[str, ...] | None:
        """把"单个元组 / 每库映射 / 不过滤"三种传法解析成某一个库该用的后缀元组。"""
        if isinstance(spec, Mapping):
            return tuple(spec.get(library_id, ()))
        return spec

    def find_duplicate_links(
        self,
        library_id: str,
        *,
        threshold: float = 0.8,
        format_allowlist: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        """单库近似重复的**成对**结果——GUI 近似去重面板要"有多像"，分组给不出这个数。

        返回 `{"links": [(路径a, 路径b, 相似度), ...], "scanned": N, "skipped": N}`，
        `scanned`/`skipped` 口径同 `find_duplicates_multi`。读正文、过滤授权格式的规则
        与分组版**共用同一个 `_dedup_texts`**，不各写一遍。"""
        texts, skipped = self._dedup_texts(library_id, format_allowlist)
        links: dict[tuple[str, str], float] = {}
        for plugin_id in sorted(self.runtime.registry.providers_of("dedup")):
            reader = getattr(self._plugin(plugin_id), "find_duplicate_links_in_texts", None)
            if reader is None:
                continue
            for a, b, sim in reader(texts, threshold=threshold):
                links[(a, b)] = max(sim, links.get((a, b), 0.0))
        ordered = sorted(
            ((a, b, sim) for (a, b), sim in links.items()),
            key=lambda item: (-item[2], item[0], item[1]),
        )
        return {"links": ordered, "scanned": len(texts), "skipped": skipped}

    def _find_duplicates_one(
        self,
        library_id: str,
        *,
        threshold: float,
        format_allowlist: tuple[str, ...] | None,
    ) -> tuple[dict[str, list[list[str]]], int, int]:
        texts, skipped = self._dedup_texts(library_id, format_allowlist)
        groups: dict[str, list[list[str]]] = {}
        for plugin_id in sorted(self.runtime.registry.providers_of("dedup")):
            dedup_plugin = self._plugin(plugin_id)
            groups[plugin_id] = dedup_plugin.find_duplicates_in_texts(
                texts, threshold=threshold
            )
        return groups, len(texts), skipped

    def _dedup_texts(
        self,
        library_id: str,
        format_allowlist: tuple[str, ...] | None,
    ) -> tuple[dict[str, str], int]:
        """去重要比对的正文集合：`(路径 → 正文, 拿不到正文而跳过的文件数)`。"""
        lib_mgr = self._singleton("library_manager")
        if lib_mgr.store.get(library_id) is None:
            raise KeyError(f"未知库: {library_id}")

        cfg = lib_mgr.store.get(library_id)
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
        texts: dict[str, str] = {}
        skipped = 0
        for path in sorted(paths):
            # `.md`/`.txt`/`.markdown` 现读磁盘，对齐旧 dedup.py:106-117 的
            # `_read_text`：这些格式的正文就是文件本身，没有"提取"这回事，
            # 去读提取缓存等于把"用户刚改完但还没重新索引"的笔记按旧内容比较。
            # 读不到（文件已删/权限）再退回提取缓存，仍然没有才跳过。
            text = self._read_plain_text(Path(cfg.root_path) / path)
            if text is None:
                text = self._read_extract_cache(library_id, path, segments)
            if text is None:
                skipped += 1
                continue
            texts[path] = text
        return texts, skipped

    @staticmethod
    def _read_plain_text(path: Path) -> str | None:
        """纯文本正文直读；不是 md/txt 或读不到返回 None（绝不抛异常——
        与 extractor 契约同一条纪律：单文件失败不该让整次去重失败）。"""
        if path.suffix.lower() not in {".md", ".txt", ".markdown"}:
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

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

    # ---- 导入的 AI 写入门禁 ----------------------------------------------
    #
    # import_library 是"影响范围最大"的写入之一：它会**注册一个新库**，并在
    # root_path 指向的目录上落一整套索引。而 root_path 是调用方自己给的任意
    # 本机绝对路径。同一个编排层里 apply_selection_changes /
    # apply_library_summary 都走 core/write_gate.py 的两段式确认，唯独这条
    # 入口完全裸奔——agent 不经用户任何确认就能往任意目录注册一个库。按
    # AGENTS.md 架构红线 6 收口。
    #
    # 人类路径（GUI 里点、CLI 里敲）仍然无条件走 import_library()：门禁保护
    # 的是"AI 触发的、影响范围较大或不可逆的写入"，不是全部写入路径
    # （core/write_gate.py 模块 docstring 原话）。

    def inspect_import(
        self,
        archive_bytes: bytes,
        *,
        root_path: str,
        library_id: str | None = None,
    ) -> dict:
        """只读预检：解包 → 全量校验 → 算出导入计划，**一个字节都不写**。

        存在的意义是让两段式门禁的"提案"阶段有东西可讲：用户必须能在给确认
        码之前看清"会往哪写、注册一个什么库、恢复多少块、包里有哪些坑"。
        校验失败在这里就抛出，绝不带着一个坏包去做提案。
        """
        lib_mgr = self._singleton("library_manager")
        archive_codec = self._singleton("archive_codec")
        payload = archive_codec.unpack(archive_bytes)
        target_id = library_id or payload["manifest"]["library_id"]
        plan = archive_codec.import_plan(payload, target_id, root_path=root_path)
        manifest = payload["manifest"]
        # 归档里带了 Agent 二进制授权时不恢复（见 import_library 内的
        # 注释），但必须让提案把这事说出来，否则等于静默吞掉一份授权。
        agent_formats = [str(x) for x in (manifest.get("agent_formats") or [])]
        return {
            "target_id": target_id,
            "library_name": plan.library_name,
            "root_path": str(Path(root_path).expanduser().resolve()),
            "chunk_count": len(plan.active_chunk_ids),
            "file_count": len(plan.source_files),
            "source_files_included": bool(plan.source_files_included),
            "missing_source_files": list(plan.missing_source_files),
            "agent_formats_in_archive": agent_formats,
            "notices": list(plan.notices),
            "target_exists": lib_mgr.store.get(target_id) is not None,
        }

    def propose_import_library(
        self,
        archive_bytes: bytes,
        *,
        root_path: str,
        library_id: str | None = None,
    ) -> dict:
        """第一段：只生成待确认提案，**零副作用**（不注册库、不写任何数据）。

        返回 applied=False + 提案号 + 6 位确认码 + 给人看的后果描述；把
        proposal_id 与 confirmation_code 一起交回 apply_import_library 才会
        真正写入。硬编码门禁，无任何配置可绕过。
        """
        summary = self.inspect_import(
            archive_bytes, root_path=root_path, library_id=library_id
        )
        # 顺手清掉过期未确认的提案（write_gate 不自带后台定时器，由核心在
        # propose 之前主动 sweep，见 core/write_gate.py::sweep_expired）。
        self.runtime.write_gate.sweep_expired()
        ticket = self.runtime.write_gate.propose(
            f"导入库「{summary['library_name']}」→ {summary['root_path']}",
            {
                "archive_bytes": archive_bytes,
                "root_path": root_path,
                "library_id": library_id,
            },
        )
        return {
            "applied": False,
            "proposal_id": ticket.proposal_id,
            "confirmation_code": ticket.confirmation_code,
            "expires_at": ticket.expires_at,
            "summary": summary,
            "message": self._import_proposal_message(summary),
        }

    def apply_import_library(self, proposal_id: str, confirmation_code: str) -> dict:
        """第二段：校验提案号 + 确认码，通过才真正执行导入。"""
        from .write_gate import WriteGateError  # 局部导入：只在出错路径需要

        try:
            pending = self.runtime.write_gate.confirm(proposal_id, confirmation_code)
        except WriteGateError as exc:
            raise PipelineError(f"导入提案未通过门禁：{exc}") from exc
        new_id = self.import_library(
            pending["archive_bytes"],
            root_path=pending["root_path"],
            library_id=pending.get("library_id"),
        )
        return {"applied": True, "library_id": new_id}

    @staticmethod
    def _import_proposal_message(summary: dict) -> str:
        """把预检结果拼成一句人话——用户要靠它判断要不要给确认码，所以
        "会往哪写""注册什么""恢复多少"必须都在，不能只给个确认码让人盲签。"""
        lines = [
            f"将把归档恢复成一个新库：「{summary['library_name']}」(id={summary['target_id']})，"
            f"根目录 {summary['root_path']}，共 {summary['chunk_count']} 个向量块 / "
            f"{summary['file_count']} 个文件条目，不重新跑嵌入。",
        ]
        if summary["target_exists"]:
            lines.append(
                f"⚠ 目标 id「{summary['target_id']}」已存在，导入会**直接拒绝**、不会覆盖——"
                f"如需覆盖请先删除旧库，或换一个 library_id。"
            )
        if not summary["source_files_included"]:
            lines.append(
                "包内不含笔记正文，恢复出的目录是空壳：需要你把笔记文件自己放到那个目录，"
                "否则搜索不到内容（不会自动把文件拉进来）。"
            )
        if summary["missing_source_files"]:
            lines.append(
                "以下源文件在目标目录缺失："
                + "、".join(summary["missing_source_files"][:5])
                + ("…" if len(summary["missing_source_files"]) > 5 else "")
            )
        if summary["agent_formats_in_archive"]:
            lines.append(
                "⚠ 包里带着 Agent 二进制格式授权 "
                + "、".join(summary["agent_formats_in_archive"])
                + "，但**不会随导入恢复**——归档无签名，无法证明这份授权是你批准的。"
                "导入完成后需要你到 GUI 里手动勾选授权，否则 agent 索引不了这些二进制文件。"
            )
        for notice in summary["notices"]:
            lines.append(f"· {notice}")
        return "\n".join(lines)

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

        **调用方须知**：这是无条件执行的底层入口，AI 触发的调用必须先走
        propose_import_library()/apply_import_library() 的两段式确认（红线
        6），不要从 MCP/GUI 的 AI 路径直接调这里。另外本方法**不恢复归档里
        的 agent_formats**（原因见函数体内注释），需要 agent 索引二进制格式
        时由用户在 GUI 里手动授权。
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

        # 目标目录可能还不存在（"恢复到一个新位置"是导入的正常用法），对齐
        # obsidian-rag/import.py:246-249：先 mkdir 再注册，且注册时不要求
        # 目录已存在（require_existing_dir=False）。注意归档里**不含笔记正文**，
        # 建出来的目录是空壳——自动同步会判定"文件全删"从而不予自愈，
        # 必须靠 archive 里的源文件清单给出提示（见 official-import-export）。
        target_root = Path(root_path)
        target_root.mkdir(parents=True, exist_ok=True)
        lib_mgr.store.add_library(
            target_id, manifest["name"], str(target_root), require_existing_dir=False
        )
        lib_mgr.store.set_selection(
            target_id,
            selection_in=manifest.get("selection_in", []),
            selection_out=manifest.get("selection_out", []),
        )
        lib_mgr.store.set_policy(
            target_id,
            new_file_default=manifest.get("new_file_default", "follow"),
            enabled_extensions=manifest.get("enabled_extensions", [".md", ".pdf", ".docx"]),
            exclude_dirs=list(manifest.get("exclude_dirs", [])),
            exclude_files=list(manifest.get("exclude_files", [])),
            exclude_patterns=list(manifest.get("exclude_patterns", [])),
        )
        # **故意不恢复 agent_formats**（AGENTS.md 架构红线 6）。归档是
        # base64 传进来的无签名数据包，`archive.verify()` 校验的是结构/校验
        # 和/条目完整性，不证明"这份授权清单是用户批准的"。原来的写法等于
        # 把"归档内容 == 用户已批准"当成前提，于是：agent 自己造一个
        # `agent_formats:[".pdf",".docx"]` 的包 → import_library → 再
        # reindex_knowledge，就完成了"自我授权二进制格式 + 驱动全量索引"，
        # 全程不需要用户点一次确认。授权只能由用户自己在 GUI 里勾。
        # 归档里带了授权时在这里明说，让调用方（CLI/GUI/MCP）能提示用户
        # "需手动授权"，而不是静默丢弃。
        dropped_agent_formats = [str(x) for x in (manifest.get("agent_formats") or [])]
        if dropped_agent_formats:
            logging.getLogger("rag_redo.core.pipeline").warning(
                "导入库 %s：归档里的 Agent 二进制格式授权 %s 未随导入恢复——"
                "归档无签名，无法证明是用户批准的，需要用户在 GUI 里手动授权",
                target_id,
                "、".join(dropped_agent_formats),
            )

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
            # 分批 upsert（旧 import.py:173-177 的 import_upsert_batch=500：
            # 拥塞小值时减慢导入，较大值占用更多内存——流式攒满即写，尾批补写）
            for start in range(0, len(chunk_ids), IMPORT_UPSERT_BATCH):
                batch = chunk_ids[start : start + IMPORT_UPSERT_BATCH]
                vector_store.upsert(
                    target_id,
                    batch,
                    [remapped_vectors[cid]["embedding"] for cid in batch],
                    documents=[remapped_vectors[cid].get("document", "") for cid in batch],
                    metadatas=[remapped_vectors[cid].get("metadata", {}) for cid in batch],
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
