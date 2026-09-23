"""official-visual-wemm 插件：页级视觉导航——"第二检索系统"，不参与
core/pipeline.py::search() 的 BM25+向量+RRF 融合排序，是完全独立、单独
调用的检索面（见 core/contracts.py::PageHit 的说明、
docs/ROADMAP.md TODO 第1条的调查结论）。

这个类本身很薄，真正的模型调用在独立子进程（server.py）里，这里只负责
四件事：①向 GPU 资源仲裁器申请一个名额（同 official-ocr-mineru-local，
"名额协商"不是真实GPU探测）；②用 core.subprocess_service 启动/终止自己
声明的子进程；③把 PDF 页面渲染成图（pymupdf 是核心轻量依赖，这一步不需要
子进程隔离）后转发给子进程编码成向量；④把向量写进/查询自己独立的 Chroma
collection——**故意不复用 official-vector-store-chroma 的存储**，那个
插件只认"library_id -> 文字 chunk collection"的语义，这个插件不是
`vector_store` 扩展点的实现者，伸手进另一个插件的持久化目录违反数据流
铁律5"插件不直接读写持久化存储，一律通过 DataStore API，按扩展点类型
收窄权限"——这里走的是每个插件都有的 `ctx.data_dir/<自己的子目录>`
惯例（同 official-vector-store-chroma/official-lexical-bm25 各自的
`ctx.data_dir/chroma`、`ctx.data_dir/bm25` 用法），只是这个插件自己的
子目录叫 `visual_wemm`，物理上和文字向量库彻底分开，比旧项目"同一个
Chroma 文件、不同 collection"的隔离粒度更彻底，行为效果一致（绝不混
向量空间）。

**已知的、刻意的简化**（相对调查到的旧项目 obsidian-rag 行为）：
- 不做增量判断（每次 index_library 全量重渲染重编码全部PDF）——
  core/pipeline.py 文字那条流水线 Phase 1 现状本身就是全量重跑（见
  official-mcp-server reindex_knowledge 工具的 docstring），这里跟着
  同一个简化程度，不是遗漏，等文字那边的版本号增量机制落地后可以一起补。
- `env_bootstrap` 还没被核心真正执行（同 official-ocr-mineru-local 的
  已知限制，见 core/subprocess_service.py::resolve_plugin_python 的
  docstring），子进程目前会退化用核心自己的解释器。
"""
from __future__ import annotations

import base64
from pathlib import Path

import chromadb

from core.contracts import PageHit
from core.subprocess_service import SubprocessServiceError, SubprocessServiceHandle, resolve_plugin_python

PLUGIN_ID = "official-visual-wemm"
GPU_RESOURCE_ID = "gpu:0"
WEMM_RENDER_DPI = 60  # 页图渲染 DPI，行为对齐旧项目 wemm_indexer.py 的默认值
WEMM_DIM = 512  # 输出向量维度，行为对齐旧项目 config.py 的默认值


def _collection_name(library_id: str) -> str:
    return f"visual_{library_id}"


class VisualWemmPlugin:
    def __init__(self) -> None:
        self._handle: SubprocessServiceHandle | None = None
        self._client = None
        self._logger = None

    def on_load(self, ctx):
        persist_dir = ctx.data_dir / "visual_wemm" / "chroma"
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._logger = ctx.logger
        ctx.logger.info("WEMM页级视觉导航已加载")

    def on_enable(self, ctx):
        # 名额协商，不是真实GPU探测——见模块docstring、
        # official-ocr-mineru-local/plugin.py 的同名注释。
        ctx.resource_arbiter.acquire(GPU_RESOURCE_ID, ctx.plugin_id, on_preempt=self._stop_handle)

        plugin_dir = Path(__file__).parent
        python = resolve_plugin_python(plugin_dir)
        command = tuple(arg.replace("{python}", python) for arg in ctx.runtime.command)
        self._handle = SubprocessServiceHandle(
            command,
            health_check=ctx.runtime.health_check,
            cwd=plugin_dir,
        )
        self._handle.start()
        ctx.logger.info("WEMM页级视觉导航子进程已启动（端口=%d）", self._handle.port)

    def on_disable(self, ctx):
        self._stop_handle()
        ctx.resource_arbiter.release(GPU_RESOURCE_ID, ctx.plugin_id)

    def on_unload(self, ctx):
        self._stop_handle()

    def _stop_handle(self) -> None:
        if self._handle is not None:
            self._handle.stop()
            self._handle = None

    def _collection(self, library_id: str):
        return self._client.get_or_create_collection(
            name=_collection_name(library_id), metadata={"hnsw:space": "cosine"}
        )

    # ---- 索引态 ----------------------------------------------------------

    def index_library(self, library_id: str, root: Path, pdf_paths: list[str]) -> None:
        """对一个库里已经被 library_manager 裁定"在检索范围内"的全部 PDF
        做页级视觉索引——绝不向调用方抛异常（core/pipeline.py::
        index_library() 调这个方法时不包 try/except，失败折叠是这个方法
        自己的契约，同 extractor "绝不抛异常"的纪律）。pdf_paths 由编排层
        传入（已经是 library_manager 唯一裁决过的结果），这个方法自己不
        重新判断"这个文件算不算在检索范围内"（数据流铁律4）。"""
        if self._handle is None or not self._handle.is_alive:
            self._logger.warning("WEMM子进程未运行，页级索引本轮跳过")
            return

        import pymupdf

        collection = self._collection(library_id)
        current_ids: set[str] = set()
        indexed_pages = 0
        for path in pdf_paths:
            full_path = root / path
            try:
                doc = pymupdf.open(str(full_path))
            except Exception as exc:  # noqa: BLE001 - 单个PDF渲染失败不该拖垮整库
                self._logger.warning("WEMM渲染失败，跳过 %s：%s: %s", path, type(exc).__name__, exc)
                continue
            try:
                ids, embeddings, metadatas = [], [], []
                for page_index in range(doc.page_count):
                    try:
                        page = doc.load_page(page_index)
                        png_bytes = page.get_pixmap(dpi=WEMM_RENDER_DPI).tobytes("png")
                        b64 = base64.b64encode(png_bytes).decode("ascii")
                        result = self._handle.call(
                            "embed", {"kind": "image", "content": b64, "dim": WEMM_DIM}, timeout=120.0
                        )
                    except SubprocessServiceError as exc:
                        self._logger.warning("WEMM调用失败，跳过 %s 第%d页：%s", path, page_index, exc)
                        continue
                    if not result.get("ok"):
                        self._logger.warning(
                            "WEMM编码失败，跳过 %s 第%d页：%s", path, page_index, result.get("error")
                        )
                        continue
                    page_id = f"{path}::{page_index}"
                    ids.append(page_id)
                    embeddings.append(result["embedding"])
                    metadatas.append(
                        {"path": path, "page": page_index, "abs_path": str(full_path), "library_id": library_id}
                    )
                    current_ids.add(page_id)
                if ids:
                    collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)
                    indexed_pages += len(ids)
            finally:
                doc.close()

        # 精确清理：本轮真实存在且渲染成功的页 id 之外的旧条目一律清掉——
        # 对应磁盘上已删除/改名的 PDF 不该留下幽灵页向量（镜像旧项目
        # wemm_indexer.py 的清理逻辑，简化成全量重跑版本：这里的 current_ids
        # 已经是"本轮全部 PDF 全部成功页"的完整集合，不是增量意义上的子集）。
        try:
            existing_ids = collection.get(include=[])["ids"]
            stale = [i for i in existing_ids if i not in current_ids]
            if stale:
                collection.delete(ids=stale)
        except Exception as exc:  # noqa: BLE001 - 清理失败不该让本轮已经写成功的页向量前功尽弃
            self._logger.warning("WEMM清理旧页失败：%s: %s", type(exc).__name__, exc)

        self._logger.info("WEMM页级索引完成：库=%s，%d页", library_id, indexed_pages)

    # ---- 查询态 ----------------------------------------------------------

    def navigate(self, library_id: str, query: str, top_k: int = 5) -> list[PageHit]:
        if self._handle is None or not self._handle.is_alive:
            self._logger.warning("WEMM子进程未运行，页级导航返回空结果")
            return []
        try:
            collection = self._client.get_collection(name=_collection_name(library_id))
        except Exception:  # noqa: BLE001 - 这个库还没建过页级索引（比如没有PDF）是正常情况，不是错误
            return []
        count = collection.count()
        if count == 0:
            return []

        try:
            result = self._handle.call("embed", {"kind": "text", "content": query, "dim": WEMM_DIM}, timeout=60.0)
        except SubprocessServiceError as exc:
            self._logger.warning("WEMM查询编码失败：%s", exc)
            return []
        if not result.get("ok"):
            self._logger.warning("WEMM查询编码失败：%s", result.get("error"))
            return []

        hits = collection.query(
            query_embeddings=[result["embedding"]], n_results=min(top_k, count), include=["metadatas", "distances"]
        )
        metadatas = hits.get("metadatas") or [[]]
        distances = hits.get("distances") or [[]]
        page_hits: list[PageHit] = []
        for meta, distance in zip(metadatas[0], distances[0]):
            page_hits.append(
                PageHit(
                    library_id=library_id,
                    path=meta.get("path", ""),
                    abs_path=meta.get("abs_path", ""),
                    page_index=meta.get("page", 0),
                    score=round(1.0 - float(distance), 4),  # cosine 距离 -> 相似度，对齐旧项目 wemm_retriever.py
                )
            )
        return page_hits
