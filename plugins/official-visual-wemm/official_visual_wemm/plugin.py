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

**增量页库**：插件自己的 generation 状态记录 PDF 内容指纹、渲染/模型签名、页 id 和
segment。Pipeline 只把新增或修改的 PDF 交给本轮重渲染；删除/失败文件通过当前有效
页 id 集合屏蔽，查询跨 segment 合并。段数达到阈值时复制现有页向量到 compact segment，
不重新编码模型。

**冻结产物限制**：`env_bootstrap` 在源码环境可执行；PyInstaller 冻结主程序没有通用解释器
时仍拒绝退化到自身 exe，安装包需要携带独立便携 Python。

**GPU 生命周期管理（2026-09-23 补齐，按 obsidian-rag 真实行为移植）**：
子进程自己在 server.py 里做懒加载+两级空闲释放（空闲卸载模型/再空闲更久
整体自退出）+ VRAM 门槛等待，这个类只负责三件配合的事：①用
`preempt_equal=True` 向资源仲裁器申请"gpu:0"名额——和
official-ocr-mineru-local 是同一层级的"按需占用"消费者，谁刚需要谁能把
对方挤开（对应旧项目 WEMM/MinerU 互相抢占显存的真实行为，见
core/resource_arbiter.py::acquire 的 preempt_equal 参数说明）；②
`on_preempt` 回调只请求子进程"软驱逐"（调 `/evict` 卸载模型、不杀子进程
本身——比整个重启轻，重新可用只需模型冷加载不需要重新拉起解释器）；
③`_ensure_alive()` 在每次真正使用前按需重新拉起子进程（同旧项目
`gpu_arbiter.ensure_server` 的幂等语义）——子进程可能因为空闲自退出已经
不在了，不这样做的话"空闲自退出省资源"这个优化会变成"用久了突然不工作"
的真实回归。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import chromadb

from core.contracts import PageHit, VisualPageState
from core.index_generation import IndexGenerationStore
from core.subprocess_service import SubprocessServiceError, SubprocessServiceHandle, resolve_plugin_python

PLUGIN_ID = "official-visual-wemm"
GPU_RESOURCE_ID = "gpu:0"
GPU_PRIORITY = 10  # 和 official-ocr-mineru-local 同一层级，互相抢占（preempt_equal）
WEMM_RENDER_DPI = 60  # 页图渲染 DPI，行为对齐旧项目 wemm_indexer.py 的默认值
WEMM_DIM = 512  # 输出向量维度，行为对齐旧项目 config.py 的默认值
VISUAL_INDEX_VERSION = "1"


def _collection_name(library_id: str, generation: str | None = None) -> str:
    if generation:
        key = hashlib.sha256(f"{library_id}\0{generation}".encode("utf-8")).hexdigest()[:40]
        return f"visualg_{key}"
    return f"visual_{library_id}"


class VisualWemmPlugin:
    def __init__(self) -> None:
        self._handle: SubprocessServiceHandle | None = None
        self._client = None
        self._logger = None
        self._enabled = False
        self._plugin_dir: Path | None = None
        self._runtime_health_check: str | None = None
        self._runtime_command: tuple[str, ...] | None = None
        self._runtime_env_bootstrap: str | None = None
        self._generations: IndexGenerationStore | None = None
        self._state_root: Path | None = None
        self._resource_arbiter = None
        self._plugin_id = ""

    def on_load(self, ctx):
        storage_root = ctx.storage.directory("visual_wemm", legacy="visual_wemm")
        persist_dir = storage_root / "chroma"
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._generations = IndexGenerationStore(
            ctx.storage.directory("index_generations", legacy="index_generations")
        )
        self._state_root = storage_root / "state"
        self._logger = ctx.logger
        ctx.logger.info("WEMM页级视觉导航已加载")

    def on_enable(self, ctx):
        self._resource_arbiter = ctx.resource_arbiter
        self._plugin_id = ctx.plugin_id
        self._plugin_dir = Path(__file__).parent
        self._runtime_health_check = ctx.runtime.health_check
        self._runtime_command = ctx.runtime.command
        self._runtime_env_bootstrap = ctx.runtime.env_bootstrap
        self._enabled = True
        acquired = ctx.resource_arbiter.acquire(
            GPU_RESOURCE_ID,
            ctx.plugin_id,
            priority=GPU_PRIORITY,
            on_preempt=self._soft_evict,
            preempt_equal=True,
        )
        if acquired:
            self._start_handle()

    def on_disable(self, ctx):
        self._enabled = False
        self._stop_handle()
        ctx.resource_arbiter.release(GPU_RESOURCE_ID, ctx.plugin_id)
        self._resource_arbiter = None
        self._plugin_id = ""

    def on_unload(self, ctx):
        self._enabled = False
        self._stop_handle()
        if self._resource_arbiter is not None and self._plugin_id:
            self._resource_arbiter.release(GPU_RESOURCE_ID, self._plugin_id)
        self._resource_arbiter = None
        self._plugin_id = ""
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
        self._client = None
        self._generations = None
        self._state_root = None

    def _start_handle(self) -> None:
        assert self._plugin_dir is not None and self._runtime_command is not None
        python = resolve_plugin_python(self._plugin_dir, env_bootstrap=self._runtime_env_bootstrap, logger=self._logger)
        command = tuple(arg.replace("{python}", python) for arg in self._runtime_command)
        self._handle = SubprocessServiceHandle(command, health_check=self._runtime_health_check, cwd=self._plugin_dir)
        self._handle.start()
        self._logger.info("WEMM页级视觉导航子进程已启动（端口=%d）", self._handle.port)

    def _stop_handle(self) -> None:
        if self._handle is not None:
            self._handle.stop()
            self._handle = None

    def _soft_evict(self) -> None:
        """资源仲裁器的抢占回调：只请求子进程卸载模型释放显存，不杀子进程
        本身。HTTP 调用失败也绝不阻塞抢占方——fail-open，同
        core/gpu_arbiter.py::request_evict 的策略（这里直接用已经建好的
        handle 发请求，不复用那个独立函数——子进程边的 server.py 完全
        隔离，import 不到 core.*，见 server.py 模块 docstring）。"""
        if self._handle is not None and self._handle.is_alive:
            try:
                self._handle.call("evict", {}, timeout=15.0)
            except SubprocessServiceError:
                pass

    def _ensure_alive(self) -> bool:
        if not self._enabled:
            return False
        if self._resource_arbiter is not None and self._resource_arbiter.holder_of(GPU_RESOURCE_ID) != self._plugin_id:
            acquired = self._resource_arbiter.acquire(
                GPU_RESOURCE_ID,
                self._plugin_id,
                priority=GPU_PRIORITY,
                on_preempt=self._soft_evict,
                preempt_equal=True,
            )
            if not acquired:
                return False
        if self._handle is not None and self._handle.is_alive:
            return True
        try:
            self._start_handle()
            return True
        except SubprocessServiceError as exc:
            self._logger.warning("WEMM子进程重新拉起失败：%s", exc)
            return False

    def _collection(self, library_id: str, generation: str | None = None):
        return self._client.get_or_create_collection(
            name=_collection_name(library_id, generation),
            metadata={"hnsw:space": "cosine", "library_id": library_id},
        )

    def _state_path(self, library_id: str, generation: str) -> Path:
        assert self._state_root is not None
        key = hashlib.sha256(library_id.encode("utf-8")).hexdigest()[:24]
        return self._state_root / key / f"{generation}.json"

    def _read_state(self, library_id: str, generation: str | None) -> dict:
        if self._state_root is None or not generation:
            return {"format_version": 1, "segments": [], "files": {}}
        try:
            data = json.loads(self._state_path(library_id, generation).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"format_version": 1, "segments": [], "files": {}}
        if not isinstance(data, dict) or data.get("format_version") != 1:
            return {"format_version": 1, "segments": [], "files": {}}
        return data

    def _write_state(self, library_id: str, generation: str, state: dict) -> None:
        if self._state_root is None:
            return
        path = self._state_path(library_id, generation)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int, str]:
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return stat.st_size, stat.st_mtime_ns, digest.hexdigest()

    # ---- 索引态 ----------------------------------------------------------

    def index_library(
        self,
        library_id: str,
        root: Path,
        pdf_paths: list[str],
        generation: str | None = None,
        changed_paths: list[str] | None = None,
        previous_generation: str | None = None,
    ) -> None:
        generation_key = generation or "legacy"
        if previous_generation:
            state = self._read_state(library_id, previous_generation)
        elif generation is None:
            state = self._read_state(library_id, generation_key)
        else:
            state = {"format_version": 1, "segments": [], "files": {}}
        files = {
            str(path): dict(record)
            for path, record in state.get("files", {}).items()
            if isinstance(record, dict)
        } if isinstance(state.get("files"), dict) else {}
        segments = [str(value) for value in state.get("segments", []) if value]
        signature = f"{VISUAL_INDEX_VERSION}:{WEMM_RENDER_DPI}:{WEMM_DIM}"
        old_signature = str(state.get("signature", ""))
        requested = set(changed_paths) if changed_paths is not None else set(pdf_paths)
        if old_signature and old_signature != signature:
            requested.update(pdf_paths)
        current_paths = set(pdf_paths)
        files = {path: record for path, record in files.items() if path in current_paths}
        indexed_pages = 0
        alive = self._ensure_alive()
        collection = None
        if alive and requested:
            try:
                collection = self._collection(library_id, generation)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("WEMM页库创建失败：%s: %s", type(exc).__name__, exc)
                alive = False
        if alive:
            import pymupdf

            for path in pdf_paths:
                old = files.get(path, {})
                full_path = root / path
                if path not in requested and old.get("signature") == signature:
                    indexed_pages += len(old.get("page_ids", []))
                    continue
                try:
                    size, mtime_ns, content_hash = self._fingerprint(full_path)
                except OSError as exc:
                    files[path] = {
                        "signature": signature,
                        "status": "failed",
                        "failure_reason": f"{type(exc).__name__}: {exc}",
                        "page_ids": [],
                        "segment": generation_key,
                    }
                    continue
                if old.get("signature") == signature and old.get("content_hash") == content_hash:
                    files[path] = old
                    indexed_pages += len(old.get("page_ids", []))
                    continue
                try:
                    document = pymupdf.open(str(full_path))
                except Exception as exc:  # noqa: BLE001
                    files[path] = {
                        "size": size,
                        "mtime_ns": mtime_ns,
                        "content_hash": content_hash,
                        "signature": signature,
                        "status": "failed",
                        "failure_reason": f"{type(exc).__name__}: {exc}",
                        "page_ids": [],
                        "segment": generation_key,
                    }
                    continue
                ids: list[str] = []
                embeddings: list[list[float]] = []
                metadatas: list[dict] = []
                try:
                    for page_index in range(document.page_count):
                        try:
                            page = document.load_page(page_index)
                            png_bytes = page.get_pixmap(dpi=WEMM_RENDER_DPI).tobytes("png")
                            encoded = base64.b64encode(png_bytes).decode("ascii")
                            result = self._handle.call(
                                "embed", {"kind": "image", "content": encoded, "dim": WEMM_DIM}, timeout=120.0
                            )
                        except SubprocessServiceError as exc:
                            self._logger.warning("WEMM调用失败，跳过 %s 第%d页：%s", path, page_index, exc)
                            continue
                        if not result.get("ok"):
                            self._logger.warning(
                                "WEMM编码失败，跳过 %s 第%d页：%s", path, page_index, result.get("error")
                            )
                            continue
                        ids.append(f"{path}::{page_index}")
                        embeddings.append(result["embedding"])
                        metadatas.append(
                            {
                                "path": path,
                                "page": page_index,
                                "abs_path": str(full_path),
                                "library_id": library_id,
                            }
                        )
                    if ids and collection is not None:
                        collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)
                        indexed_pages += len(ids)
                    files[path] = {
                        "size": size,
                        "mtime_ns": mtime_ns,
                        "content_hash": content_hash,
                        "signature": signature,
                        "status": "indexed" if len(ids) == document.page_count else "partial" if ids else "failed",
                        "failure_reason": None if len(ids) == document.page_count else "部分页面编码失败",
                        "page_ids": ids,
                        "segment": generation_key,
                    }
                finally:
                    document.close()
        else:
            for path in requested:
                files[path] = {
                    "signature": signature,
                    "status": "failed",
                    "failure_reason": "WEMM子进程未运行",
                    "page_ids": [],
                    "segment": generation_key,
                }
            self._logger.warning("WEMM子进程未运行，页级索引本轮跳过")
        if collection is not None and generation_key not in segments and any(
            record.get("segment") == generation_key and record.get("page_ids")
            for record in files.values()
        ):
            segments.append(generation_key)
        active_ids = {
            page_id
            for record in files.values()
            if record.get("status") in {"indexed", "partial"}
            for page_id in record.get("page_ids", [])
        }
        if len(segments) >= 3 and active_ids:
            compact_segment = f"{generation_key}-compact"
            try:
                compact = self._collection(library_id, compact_segment)
                by_segment: dict[str, list[str]] = {}
                for record in files.values():
                    segment = str(record.get("segment", ""))
                    by_segment.setdefault(segment, []).extend(record.get("page_ids", []))
                for segment, page_ids in by_segment.items():
                    if not segment or not page_ids:
                        continue
                    source = self._collection(library_id, segment)
                    rows = source.get(ids=page_ids, include=["embeddings", "metadatas"])
                    if rows.get("ids"):
                        compact.upsert(
                            ids=rows["ids"],
                            embeddings=rows.get("embeddings"),
                            metadatas=rows.get("metadatas"),
                        )
                for record in files.values():
                    record["segment"] = compact_segment
                segments = [compact_segment]
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("WEMM页库压缩失败：%s: %s", type(exc).__name__, exc)
        if generation is None and collection is not None:
            try:
                existing_ids = collection.get(include=[])["ids"]
                stale_ids = [page_id for page_id in existing_ids if page_id not in active_ids]
                if stale_ids:
                    collection.delete(ids=stale_ids)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("WEMM清理旧页失败：%s: %s", type(exc).__name__, exc)
        self._write_state(
            library_id,
            generation_key,
            {
                "format_version": 1,
                "library_id": library_id,
                "generation": generation_key,
                "signature": signature,
                "segments": segments,
                "files": files,
            },
        )
        self._logger.info("WEMM页级索引完成：库=%s，%d页", library_id, indexed_pages)

    def export_state(self, library_id: str, generation: str) -> dict:
        state = dict(self._read_state(library_id, generation))
        collections: dict[str, list[dict]] = {}
        for segment in state.get("segments", []):
            try:
                collection = self._client.get_collection(name=_collection_name(library_id, segment))
                rows = collection.get(include=["embeddings", "documents", "metadatas"])
                collections[str(segment)] = [
                    {
                        "id": str(page_id),
                        "embedding": [float(value) for value in (embedding or [])],
                        "document": document or "",
                        "metadata": dict(metadata or {}),
                    }
                    for page_id, embedding, document, metadata in zip(
                        rows.get("ids", []),
                        rows.get("embeddings", []),
                        rows.get("documents", []),
                        rows.get("metadatas", []),
                    )
                ]
            except Exception:
                continue
        state["collections"] = collections
        return state

    def import_state(self, library_id: str, state: dict, generation: str) -> None:
        if not isinstance(state, dict):
            return
        restored = dict(state)
        restored["library_id"] = library_id
        restored["generation"] = generation
        self._write_state(library_id, generation, restored)
        for segment, rows in (restored.get("collections", {}) or {}).items():
            if not isinstance(rows, list):
                continue
            collection = self._client.get_or_create_collection(
                name=_collection_name(library_id, str(segment)),
                metadata={"hnsw:space": "cosine", "library_id": library_id},
            )
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                collection.upsert(
                    ids=[str(row["id"])],
                    embeddings=[row.get("embedding", [])],
                    documents=[row.get("document", "")],
                    metadatas=[row.get("metadata", {})],
                )

    def graph_page_states(
        self,
        library_id: str,
        generation: str,
    ) -> tuple[VisualPageState, ...]:
        files = self._read_state(library_id, generation).get("files", {})
        if not isinstance(files, dict):
            return ()
        states: list[VisualPageState] = []
        for path, record in sorted(files.items()):
            if not isinstance(path, str) or not isinstance(record, dict):
                continue
            pages = tuple(
                sorted(
                    {
                        int(page_id.rsplit("::", 1)[1]) + 1
                        for page_id in record.get("page_ids", [])
                        if isinstance(page_id, str)
                        and page_id.startswith(f"{path}::")
                        and page_id.rsplit("::", 1)[-1].isdigit()
                    }
                )
            )
            reason = record.get("failure_reason")
            states.append(
                VisualPageState(
                    library_id=library_id,
                    path=path,
                    provider_id="official-visual-wemm",
                    status=str(record.get("status") or "failed"),
                    failure_reason=str(reason) if reason else None,
                    pages=pages,
                )
            )
        return tuple(states)

    def delete_generation(self, library_id: str, generation: str) -> None:
        before = {str(value) for value in self._read_state(library_id, generation).get("segments", [])}
        referenced: set[str] = set()
        if self._generations is not None:
            active = self._generations.active(library_id)
            for state_generation in [active, *self._generations.history(library_id)]:
                if state_generation:
                    referenced.update(str(value) for value in self._read_state(library_id, state_generation).get("segments", []))
        for segment in before - referenced:
            try:
                segment_name = None if segment == "legacy" else segment
                self._client.delete_collection(name=_collection_name(library_id, segment_name))
            except Exception:
                pass
        if generation not in referenced:
            try:
                self._client.delete_collection(name=_collection_name(library_id, generation))
            except Exception:
                pass
        if self._state_root is not None:
            try:
                self._state_path(library_id, generation).unlink(missing_ok=True)
            except OSError:
                pass

    # ---- 只读诊断 ----------------------------------------------------------

    def status(self) -> dict:
        """WEMM 页级视觉导航状态——对齐 obsidian-rag 的 `wemm_status` MCP
        工具："建没建、生效没生效"，用户/AI 一眼能确认，不用靠猜。只读，
        不拉起子进程、不加载模型（同 obsidian-rag 该工具"只读，不启动
        服务、不加载模型"的承诺——用 `self._handle` 的现有快照判断存活，
        不调用 `_ensure_alive()`）。

        当前 generation 的状态文件保留每个 PDF 的成功/部分成功/失败原因；
        本方法汇总有效页数、PDF 数和失败列表，不加载模型。"""
        alive = self._handle is not None and self._handle.is_alive
        libraries: dict[str, dict] = {}
        if self._client is not None:
            active_pairs = self._generations.active_pairs() if self._generations is not None else {}
            for library_id, generation in active_pairs.items():
                state = self._read_state(library_id, generation)
                files = state.get("files", {})
                if not isinstance(files, dict):
                    continue
                page_count = sum(
                    len(record.get("page_ids", []))
                    for record in files.values()
                    if isinstance(record, dict) and record.get("status") in {"indexed", "partial"}
                )
                failures = [
                    {"path": path, "reason": record.get("failure_reason", "未知失败")}
                    for path, record in sorted(files.items())
                    if isinstance(record, dict) and record.get("status") == "failed"
                ]
                libraries[library_id] = {
                    "page_count": page_count,
                    "pdf_count": len(files),
                    "failures": failures,
                }
            for coll in self._client.list_collections():
                if not coll.name.startswith("visual_") or coll.name in {
                    _collection_name(library_id, generation) for library_id, generation in active_pairs.items()
                }:
                    continue
                library_id = coll.name[len("visual_"):]
                metadatas = coll.get(include=["metadatas"])["metadatas"] or []
                pdf_paths = {m["path"] for m in metadatas if m and m.get("path")}
                libraries.setdefault(
                    library_id,
                    {"page_count": len(metadatas), "pdf_count": len(pdf_paths)},
                )
        return {
            "enabled": self._enabled,
            "subprocess_alive": alive,
            "libraries": libraries,
        }

    # ---- 查询态 ----------------------------------------------------------

    def navigate(self, library_id: str, query: str, top_k: int = 5) -> list[PageHit]:
        if not self._ensure_alive():
            self._logger.warning("WEMM子进程未运行，页级导航返回空结果")
            return []
        generation = self._generations.active(library_id) if self._generations is not None else None
        state = self._read_state(library_id, generation or "legacy")
        files = state.get("files", {})
        if not isinstance(files, dict):
            return []
        active_ids = {
            page_id
            for record in files.values()
            if isinstance(record, dict) and record.get("status") in {"indexed", "partial"}
            for page_id in record.get("page_ids", [])
        }
        segments = [str(value) for value in state.get("segments", []) if value]
        if not active_ids or not segments:
            return []
        try:
            result = self._handle.call("embed", {"kind": "text", "content": query, "dim": WEMM_DIM}, timeout=60.0)
        except SubprocessServiceError as exc:
            self._logger.warning("WEMM查询编码失败：%s", exc)
            return []
        if not result.get("ok"):
            self._logger.warning("WEMM查询编码失败：%s", result.get("error"))
            return []
        merged: dict[str, tuple[dict, float]] = {}
        for segment in segments:
            try:
                segment_name = None if segment == "legacy" else segment
                collection = self._client.get_collection(name=_collection_name(library_id, segment_name))
            except Exception:
                continue
            count = collection.count()
            request = min(count, max(top_k * 4, 32))
            while request > 0:
                hits = collection.query(
                    query_embeddings=[result["embedding"]],
                    n_results=min(request, count),
                    include=["metadatas", "distances"],
                )
                metadatas = hits.get("metadatas") or [[]]
                distances = hits.get("distances") or [[]]
                current_count = 0
                for page_id, meta, distance in zip(hits.get("ids", [[]])[0], metadatas[0], distances[0]):
                    if page_id not in active_ids:
                        continue
                    current_count += 1
                    score = 1.0 - float(distance)
                    previous = merged.get(page_id)
                    if previous is None or score > previous[1]:
                        merged[page_id] = (meta or {}, score)
                if current_count >= top_k or request >= count:
                    break
                request = min(count, max(request * 2, top_k + 1))
        return [
            PageHit(
                library_id=library_id,
                path=str(meta.get("path", "")),
                abs_path=str(meta.get("abs_path", "")),
                page_index=int(meta.get("page", 0)),
                score=round(score, 4),
            )
            for _, (meta, score) in sorted(merged.items(), key=lambda item: item[1][1], reverse=True)[:top_k]
        ]
