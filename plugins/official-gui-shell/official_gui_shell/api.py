"""pywebview 桌面壳的后端 API（js_api 桥）。

只是薄封装——真正的检索/索引逻辑全在 Pipeline / library-manager 插件里，
Api 类的每个方法都直接委托过去，不自己编排任何顺序逻辑。这也是为什么
GUI 和 MCP（official-mcp-server）天生不会行为不一致：两者调的是同一个
Pipeline，不是各自维护一份检索逻辑（docs/DATA_FLOW.md 规则4的体现）。

GUI 是零侵入观察者（继承旧项目架构红线，见 AGENTS.md 架构红线6/7）：
这个类不直接碰任何持久化存储，只调用插件已经暴露的方法；任何编排层
异常都在这里折叠成 `{"ok": False, "error": ...}` 返回给前端，绝不让
一次检索/索引失败带崩整个窗口。
"""
from __future__ import annotations

import threading

from pathlib import Path
from typing import Any

from core.atomic import atomic_write_bytes
from core.pipeline import DEFAULT_CONFIDENCE_WARN_THRESHOLD, Pipeline, confidence_tier


#: 已知设置键的展示元信息——对齐 obsidian-rag/gui/config_editor.py 的
#: FIELD_META（label/hint/secret），放在 GUI 插件层而不是 core/settings.py：
#: "这个键在设置面板里怎么展示"是 GUI 的呈现关注点，设置存储本身只管
#: 存取（同"设置项默认值由调用方声明"的分工）。未登记的键回退用键名
#: 本身做 label；`*_api_key`/`*_token` 后缀按规则一律视为敏感（对齐
#: config_editor.py:168/278 对 mineru_api_key/library_summary_llm_api_key
#: 的 secret 标记），未来新增 key 类设置不需要记得回来登记。
SETTING_FIELD_META: dict[str, dict[str, Any]] = {
    "fusion_dense_weight": {"label": "RRF 向量语义路权重", "hint": "调大偏向语义检索；两路等权1.0/1.0为经典无权重RRF"},
    "fusion_bm25_weight": {"label": "RRF BM25 关键词路权重", "hint": "调大偏向关键词检索"},
    "default_libraries": {"label": "默认检索库", "hint": "检索的 libraries 参数留空时先收窄到这些库（库id列表）"},
    "confidence_warn_threshold": {"label": "低置信度警示线", "hint": "低于此值的结果标注“仅供参考”（默认0.30）"},
    "confidence_drop_threshold": {"label": "置信度骤降警示线", "hint": "0=关闭（沿用旧项目当前口径）"},
    "max_chunks_per_file": {"label": "同篇结果封顶", "hint": "正文模式下同一文件最多交付几块（默认3）"},
    "tbd_exclude_ratio": {"label": "占位符占比阈值", "hint": "正文被占位符占据超过此占比的文件记 tbd 终态跳过索引"},
    "pdf_scan_backend": {"label": "扫描件 OCR 后端", "hint": "none=不OCR（混合PDF保持scanned终态）；mineru-cloud/mineru-local"},
    "mineru_python": {"label": "MinerU 解释器覆盖路径", "hint": "留空则按 uv tool 标准落点自动探测本机已装环境"},
    "hyde_enabled": {"label": "HyDE 查询增强开关", "hint": "默认关闭；开启后低置信查询会用 LLM 生成假设文档重查"},
    "hyde_min_confidence": {"label": "HyDE 触发阈值", "hint": "首轮 top1 置信度低于此值才触发（默认0.5）"},
    "hyde_llm_url": {"label": "HyDE LLM 端点", "hint": "OpenAI 兼容 chat/completions 地址"},
    "hyde_llm_model": {"label": "HyDE LLM 模型", "hint": ""},
    "hyde_llm_api_key": {"label": "HyDE LLM API Key", "secret": True, "hint": "敏感信息，不进任何日志"},
    "hyde_llm_timeout_seconds": {"label": "HyDE 请求超时（秒）", "hint": ""},
    "hyde_llm_max_tokens": {"label": "HyDE 生成上限（token）", "hint": ""},
    "cuda_cooldown_seconds": {"label": "CUDA 失败冷却期（秒）", "hint": "CUDA 失败后进入冷却，到期轻量探测自动切回（默认300，对齐旧项目同名配置）"},
}


def _setting_is_secret(key: str) -> bool:
    lowered = key.lower()
    return lowered.endswith(("_api_key", "_token")) or bool(
        SETTING_FIELD_META.get(key, {}).get("secret")
    )


class Api:
    def __init__(self, pipeline: Pipeline, lib_mgr: Any) -> None:
        self._pipeline = pipeline
        self._lib_mgr = lib_mgr
        self._summary_batch: dict | None = None
        self._summary_batch_lock = threading.Lock()

    def list_libraries(self) -> list[dict[str, Any]]:
        return [
            {"library_id": c.library_id, "name": c.name, "root_path": c.root_path}
            for c in self._lib_mgr.store.list_libraries()
        ]

    def add_library(self, library_id: str, name: str, root_path: str) -> dict[str, Any]:
        try:
            self._lib_mgr.store.add_library(library_id, name, root_path)
            return {"ok": True}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def reindex_library(self, library_id: str, full: bool = False) -> dict[str, Any]:
        try:
            if full:
                result = self._pipeline.start_index_library(library_id, source="gui", full=True)
            else:
                result = self._pipeline.start_index_library(library_id, source="gui")
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "started": result.started,
            "message": result.message,
            "run_id": result.run_id,
            "worker_pid": result.worker_pid,
            "full": full,
        }

    def index_status(self, library_id: str) -> dict[str, Any]:
        try:
            status = self._pipeline.index_status(library_id)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "status": status}

    def stop_index(self, library_id: str, run_id: str) -> dict[str, Any]:
        try:
            stopped, message = self._pipeline.stop_index_library(library_id, run_id)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "stopped": stopped, "message": message}

    def search(
        self,
        libraries: str,
        query: str,
        top_k: int = 10,
        *,
        exclude: str = "",
        folder: str = "",
    ) -> dict[str, Any]:
        """`libraries` 支持 core/pipeline.py::search 的多库选库语法（单库名/
        逗号分隔多库/空="全部库"/"all"）——参数位置保持和旧的单库
        `library_id` 参数一致（第一个位置参数），前端目前仍然只传当前选中
        的单个库id（见 assets/index.html 的 `state.activeLibraryId`），这
        本身就是"逗号分隔列表里只有一项"的合法特例，不需要改调用方也能
        直接享受到多库能力已经在编排层就绪这件事。**真正的多库勾选UI**
        （对齐 obsidian-rag GUI 的库复选树，`checked=None`→全部库/
        `checked=集合`→白名单并查）**还没做**，是一处如实记录的、刻意
        分阶段的简化——底层能力已经完整，缺的只是前端一个新的复选框
        树控件，不影响这个方法本身的正确性。"""
        if not query.strip():
            return {"ok": True, "advice": [], "results": []}
        try:
            response = self._pipeline.search_with_advice(
                libraries,
                query,
                top_k=top_k,
                exclude=exclude,
                folder=folder,
            )
            results = response.results
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        warn_threshold = self._pipeline.runtime.settings.get(
            "confidence_warn_threshold", DEFAULT_CONFIDENCE_WARN_THRESHOLD
        )
        return {
            "ok": True,
            "advice": list(response.advice),
            "results": [
                {
                    "library_id": r.library_id,
                    "path": r.path,
                    "heading": r.heading_breadcrumb,
                    "text": r.text,
                    "backfilled": r.backfilled,
                    "confidence": round(r.confidence, 3),
                    "confidence_tier": confidence_tier(r.confidence, warn_threshold),
                }
                for r in results
            ],
        }

    def graph(self, libraries: str = "") -> dict[str, Any]:
        try:
            response = self._pipeline.graph(libraries or "all")
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        nodes: list[dict[str, Any]] = []
        for node in response.nodes:
            item: dict[str, Any] = {
                "id": node.node_id,
                "lib": node.library_id,
                "rel": node.path,
                "type": node.node_type,
                "chunks": node.chunks,
                "updated": node.updated_ns / 1_000_000_000 if node.updated_ns is not None else None,
                "fail_reason": node.failure_reason,
                "theme": node.theme,
                "pipeline": {
                    "mineru": node.extraction_state,
                    "wemm": node.visual_state,
                },
                "big": node.is_hub,
            }
            if node.page_number is not None:
                item["page"] = node.page_number
            if node.page_count is not None:
                item["pages"] = node.page_count
            nodes.append(item)
        return {
            "ok": True,
            "nodes": nodes,
            "edges": [
                {"a": edge.source, "b": edge.target, "kind": edge.kind}
                for edge in response.edges
            ],
            "libs": list(response.library_ids),
            "stats": {
                "nodes": response.stats.nodes,
                "edges": response.stats.edges,
            },
        }

    def open_source(self, library_id: str, path: str) -> dict[str, Any]:
        try:
            cfg = self._lib_mgr.store.get(library_id)
            if cfg is None:
                raise KeyError(f"未知库: {library_id}")
            root = Path(cfg.root_path).resolve()
            target = (root / path).resolve()
            if root != target and root not in target.parents:
                raise ValueError("源文件路径越出库目录")
            if not target.is_file():
                raise FileNotFoundError(str(target))
            import os

            opener = getattr(os, "startfile", None)
            if callable(opener):
                opener(str(target))
            else:
                return {"ok": True, "opened": False, "path": str(target)}
            return {"ok": True, "opened": True, "path": str(target)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def graph_semantic_edges(
        self,
        libraries: str = "",
        threshold: float = 0.62,
    ) -> dict[str, Any]:
        try:
            response = self._pipeline.graph_semantic_edges(
                libraries or "all",
                threshold=threshold,
            )
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "edges": [
                {"a": edge.source, "b": edge.target, "sim": edge.similarity}
                for edge in response.edges
            ],
            "error": response.error,
        }

    def index_failures(self, library_id: str) -> dict[str, Any]:
        try:
            failures = self._pipeline.index_failures(library_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "failures": (failures or {}).get("failures", [])}

    def read_document(self, library_id: str, path: str) -> dict[str, Any]:
        try:
            document = self._pipeline.read_document(library_id, path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": document.path, "text": document.text, "source": document.source}

    def note_relations(self, library_id: str, path: str) -> dict[str, Any]:
        try:
            relations = self._pipeline.note_relations(library_id, path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **relations}

    def navigate(self, library_id: str, query: str, top_k: int = 5) -> dict[str, Any]:
        """页级视觉导航——独立于 search() 的"第二检索系统"，见
        core/pipeline.py::navigate() 的说明。返回的每条结果只有 PDF 路径+
        页码+相似度分数，不返回图片本身（这个类不做任何缩略图渲染，同
        调查到的旧项目 obsidian-rag 行为：GUI 只展示状态/结果列表，不做
        页面图片预览，见 docs/ROADMAP.md TODO 第1条的调查结论）。"""
        if not query.strip():
            return {"ok": True, "results": []}
        try:
            hits = self._pipeline.navigate(library_id, query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "results": [
                {"path": h.path, "abs_path": h.abs_path, "page": h.page_index + 1, "score": h.score}
                for h in hits
            ],
        }

    def export_library(self, library_id: str, dest_path: str) -> dict[str, Any]:
        """把一个库导出成归档文件，写到 dest_path（用户在前端填的目标路径，
        比如"把这个库搬到另一台电脑"场景下先导出到U盘/网盘同步目录）。文件
        I/O 直接在这里做，不是又新起一个插件——GUI Api 本来就是"把 Pipeline
        能力包装成本地操作"的薄封装层，见模块 docstring，写文件到用户指定
        的本地路径属于这一层该做的事，不属于 core.pipeline（那里只产出/消费
        bytes，不知道"文件"这个概念，见 core/pipeline.py 的 export_library
        注释）。"""
        try:
            archive_bytes = self._pipeline.export_library(library_id)
            atomic_write_bytes(Path(dest_path), archive_bytes)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def get_library_summary(self, library_id: str) -> dict[str, Any]:
        try:
            summary = self._pipeline.get_library_summary(library_id)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "text": summary.text, "source": summary.source, "model": summary.model}

    def set_library_summary(self, library_id: str, text: str) -> dict[str, Any]:
        """用户在 GUI 直接手写/编辑简介：无条件生效，不经过写权限门禁——
        门禁只保护"AI 经 MCP 对话想覆盖用户已写内容"这一种场景，用户改
        自己的东西不需要向自己确认（同调查到的旧项目 guiweb/bridge.py::
        set_library_summary 行为）。"""
        try:
            return self._pipeline.set_library_summary_direct(library_id, text, source="user")
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}

    def refresh_library_summary(self, library_id: str, force: bool = False) -> dict[str, Any]:
        """"刷新简介"：真的调一次配置好的 llm_provider 生成新简介，直接
        写入（不经过写权限门禁——GUI 点按钮本身就是明确的人类操作，同
        set_library_summary）。

        当前简介若是用户手写的且 force=False，不生成也不覆盖，返回
        `needs_confirm=True` 让前端二次确认后带 force=True 重试（同调查
        到的旧项目 guiweb/bridge.py::_run_summary_refresh_batch 的"先探测
        再问要不要覆盖"逻辑）。批量场景用 `refresh_library_summaries_batch`
        （后台线程+轮询，对齐旧 bridge.py 同名方法），单库刷新不再阻塞。"""
        try:
            current = self._pipeline.get_library_summary(library_id)
            if current.source == "user" and not force:
                return {"ok": False, "needs_confirm": True}
            text, fingerprint, provider_id = self._pipeline.generate_library_summary(library_id)
            result = self._pipeline.set_library_summary_direct(
                library_id, text, source="ai", fingerprint=fingerprint, model=provider_id
            )
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        result["provider"] = provider_id
        return result

    # ---- 批量后台刷新简介（对齐旧 guiweb/bridge.py::refresh_library_summaries_batch
    #      + refresh_library_summaries_poll：后台线程逐库生成，前端轮询进度；
    #      用户手写的库不传 force 时跳过并报告 needs_confirm，不阻塞批次）----

    def refresh_library_summaries_batch(self, names: list[str], force: bool = False) -> dict[str, Any]:
        """启动一批库的 AI 简介后台刷新，立即返回（不阻塞 GUI）。逐库在
        后台线程执行，结果经 `refresh_library_summaries_poll()` 轮询读取。
        `force=True` 时连用户手写的简介也覆盖；False 时手写库跳过并记录
        在 `skipped`（免打扰：同一批里用户已拒绝的库不再反复询问）。"""
        with self._summary_batch_lock:
            if self._summary_batch is not None and self._summary_batch["thread"].is_alive():
                return {"ok": False, "error": "已有一个批量刷新任务在进行中"}
            state = {
                "names": list(names),
                "force": force,
                "done": 0,
                "total": len(names),
                "current": "",
                "results": {},
                "skipped": [],
                "thread": None,
            }
            state["thread"] = threading.Thread(
                target=self._summary_refresh_worker, args=(state,), daemon=True, name="summary-refresh-batch"
            )
            self._summary_batch = state
        state["thread"].start()
        return {"ok": True, "total": len(names)}

    def _summary_refresh_worker(self, state: dict) -> None:
        for library_id in state["names"]:
            state["current"] = library_id
            current = self._pipeline.get_library_summary(library_id)
            if current.source == "user" and not state["force"]:
                state["skipped"].append(library_id)
            else:
                try:
                    text, fingerprint, provider_id = self._pipeline.generate_library_summary(library_id)
                    self._pipeline.set_library_summary_direct(
                        library_id, text, source="ai", fingerprint=fingerprint, model=provider_id
                    )
                    state["results"][library_id] = {"ok": True, "text": text, "provider": provider_id}
                except Exception as exc:  # noqa: BLE001 - 单库失败不拖垮批次
                    state["results"][library_id] = {"ok": False, "error": str(exc)}
            state["done"] += 1
        state["current"] = ""

    def refresh_library_summaries_poll(self) -> dict[str, Any]:
        """轮询批量刷新进度：`{running, done, total, current, results,
        skipped}`——对齐旧 bridge.py 的同名轮询协议。"""
        with self._summary_batch_lock:
            state = self._summary_batch
            if state is None:
                return {"running": False, "done": 0, "total": 0, "current": "", "results": {}, "skipped": []}
            running = state["thread"].is_alive()
            return {
                "running": running,
                "done": state["done"],
                "total": state["total"],
                "current": state["current"],
                "results": {k: dict(v) for k, v in state["results"].items()},
                "skipped": list(state["skipped"]),
            }

    def import_library(self, archive_path: str, root_path: str, library_id: str = "") -> dict[str, Any]:
        """从 export_library 写出的归档文件恢复一个库。library_id 留空
        （前端不填这个字段）则沿用归档里记录的原始 library_id。"""
        try:
            archive_bytes = Path(archive_path).read_bytes()
            new_id = self._pipeline.import_library(
                archive_bytes, root_path=root_path, library_id=library_id or None
            )
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "library_id": new_id}

    # ---- 库管理补齐（对齐旧 guiweb/bridge.py 的库管理方法）----

    def remove_library(self, library_id: str, drop_data: bool = False) -> dict[str, Any]:
        """注销库（对齐旧 bridge.py::remove_library）。drop_data=True 时
        尽力删除该库的索引数据（向量 collection）；文件系统上的笔记与
        core 管理的索引目录保留——"删除即不可恢复"的数据清理属于
        旧项目问题49 的 prune 范畴，当前只做注销+尽力清理并如实报告。"""
        try:
            cfg = self._lib_mgr.store.get(library_id)
            if cfg is None:
                return {"ok": False, "error": f"未知库: {library_id}"}
            dropped = []
            if drop_data:
                plugin_id = self._pipeline.runtime.registry.active_of("vector_store")
                if plugin_id:
                    store = self._pipeline._plugin(plugin_id)
                    if hasattr(store, "delete_collection"):
                        store.delete_collection(library_id)
                        dropped.append("向量collection")
            self._lib_mgr.store.remove_library(library_id)
            note = "已从注册表移除；笔记文件与 core 索引目录保留" + (
                f"（已清理：{'、'.join(dropped)}）" if dropped else ""
            )
            return {"ok": True, "note": note}
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}

    def get_library_config(self, library_id: str) -> dict[str, Any]:
        """返回某库的生效配置（对齐旧 bridge.py::get_library_config）。"""
        try:
            cfg = self._lib_mgr.store.get(library_id)
            if cfg is None:
                return {"ok": False, "error": f"未知库: {library_id}"}
            from dataclasses import asdict

            return {"ok": True, "config": asdict(cfg)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def set_library_config(self, library_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """批量写库级配置（对齐旧 bridge.py::set_library_config）：
        支持 new_file_default/enabled_extensions/exclude_dirs/exclude_files/
        exclude_patterns/agent_formats；逐键校验，非法键报错不静默。"""
        try:
            allowed = {
                "new_file_default", "enabled_extensions", "exclude_dirs",
                "exclude_files", "exclude_patterns", "agent_formats",
            }
            unknown = set(updates) - allowed
            if unknown:
                return {"ok": False, "error": f"不支持的配置键: {sorted(unknown)}"}
            if "agent_formats" in updates:
                self._lib_mgr.store.set_agent_formats(library_id, list(updates["agent_formats"]))
            rest = {k: v for k, v in updates.items() if k != "agent_formats"}
            if rest:
                self._lib_mgr.store.set_policy(library_id, **rest)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def dedup_run(self, library_id: str, threshold: float = 0.8) -> dict[str, Any]:
        """近似去重分析（只读，对齐旧 bridge.py::dedup_run）。"""
        try:
            groups = self._pipeline.find_duplicates(library_id, threshold=threshold)
            clusters = [g for groups in groups.values() for g in groups]
            return {"ok": True, "clusters": clusters}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def wemm_status(self) -> dict[str, Any]:
        """页级视觉导航只读诊断（对齐旧 bridge.py::wemm_status）。"""
        try:
            return {"ok": True, "providers": self._pipeline.visual_status()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def open_path(self, path: str) -> dict[str, Any]:
        """用系统默认程序打开任意本地路径（对齐旧 bridge.py::open_path，
        "打开文件夹"按钮用）。"""
        try:
            target = Path(path)
            if not target.exists():
                return {"ok": False, "error": f"路径不存在: {path}"}
            import os

            os.startfile(str(target))  # noqa: S606 - Windows 打开资源管理器，仅本地 GUI
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def get_settings(self) -> dict[str, Any]:
        """返回 `{"values": {...}, "meta": {key: {label, hint, secret}}}`——
        对齐 obsidian-rag/guiweb/bridge.py::get_settings（801-820）一次把
        当前值和字段元信息一起带回的设计：`values` 只含"已经被显式设过值"
        的键（每个设置项的默认值分散在各自的插件/`core/pipeline.py` 里，
        架构原则"同一件事只能在一处定义"，这里不重复维护默认值清单），
        `meta` 覆盖全部已知键（含未设置的）供前端展示说明，`secret=True`
        的键由前端按密码框渲染、列表里打码显示（真实值仍随 values 返回，
        与旧项目一致——打码是展示层行为，不是接口层截断）。"""
        values = self._pipeline.runtime.settings.all()
        meta: dict[str, Any] = {}
        for key in dict.fromkeys([*SETTING_FIELD_META, *values]):
            entry = dict(SETTING_FIELD_META.get(key, {}))
            entry.setdefault("label", key)
            entry.setdefault("hint", "")
            entry["secret"] = _setting_is_secret(key)
            meta[key] = entry
        return {"values": values, "meta": meta}

    def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        """写一个设置项，立即持久化、对后续调用立即生效（不需要重启——
        `core/pipeline.py::search()` 等每次调用都现读 `ctx.settings`，
        不是进程启动时缓存一份快照，同架构红线8"切换实现是配置层面操作，
        不需要重启"的精神）。这个方法本身不校验 `value` 的类型/合法性——
        校验发生在真正读取它的那一处（`SettingsStore.get()` 按调用方传的
        default 类型核对，见该类 docstring），这里收到什么就存什么。"""
        try:
            self._pipeline.runtime.settings.set(key, value)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def unset_setting(self, key: str) -> dict[str, Any]:
        """删掉一个设置项，恢复成"未设置"（后续读取会拿回调用方自己的
        默认值）——"恢复默认值"按钮用得上。"""
        try:
            self._pipeline.runtime.settings.unset(key)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True}
