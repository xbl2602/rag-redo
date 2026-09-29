"""pywebview 桌面壳的后端 API（js_api 桥）。

只是薄封装——真正的检索/索引逻辑全在 Pipeline / library-manager 插件里，Api 类的每个
方法都直接委托过去，不自己编排任何顺序逻辑。这也是为什么 GUI 和 MCP
（official-mcp-server）天生不会行为不一致：两者调的是同一个 Pipeline，不是各自维护
一份检索逻辑（docs/DATA_FLOW.md 规则4的体现）。

GUI 是零侵入观察者（继承旧项目架构红线，见 AGENTS.md 架构红线6/7）：这个类不直接碰任何
持久化存储，只调用插件已经暴露的方法；任何编排层异常都在这里折叠成
`{"ok": False, "error": ...}` 返回给前端，绝不让一次检索/索引失败带崩整个窗口。

**两套入口，互不遮蔽**：

- 前端（逐字节复刻的旧 guiweb 前端）调用的 **37 个契约方法**全部定义在
  `contract_bridge._LegacyContractMixin`——方法名、参数顺序、返回键集合都由旧项目冻结；
- 本类只放契约**之外**的"精确 API"（库 id 在前、query 在后，供 MCP/CLI/脚本/测试用），
  方法名与契约方法**互不重名**。这条是硬约束：此前 `Api` 里有 6 个与 mixin 同名的方法，
  子类同名方法遮蔽 mixin，契约版整段成了死代码、前端拿到缺键的返回值，而所有测试都直接
  调 `Api` 的旧版所以一片绿。`test_contracts_parity.py` 现在断言两边不许再重名。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.atomic import atomic_write_bytes
from core.pipeline import DEFAULT_CONFIDENCE_WARN_THRESHOLD, Pipeline, confidence_tier
from official_gui_shell.contract_bridge import _LegacyContractMixin
from official_gui_shell.settings_schema import (  # noqa: F401 - 保持旧的导入路径可用
    FIELDS,
    SETTING_FIELD_META,
    setting_is_secret,
)


def _setting_is_secret(key: str) -> bool:
    return setting_is_secret(key)


class Api(_LegacyContractMixin):
    """GUI 的 js_api 桥：契约方法来自 `_LegacyContractMixin`，本类只加精确 API。"""

    def __init__(self, pipeline: Pipeline, lib_mgr: Any) -> None:
        self._pipeline = pipeline
        self._lib_mgr = lib_mgr
        self._init_bridge_state()

    # ---- 库 / 索引（精确 API：库 id 在前）------------------------------------

    def register_library(self, library_id: str, name: str, root_path: str) -> dict[str, Any]:
        """按显式 library_id 注册一个库（迁移/脚本用）。契约名 `add_library(path, name)`
        由路径推导，库 id 取名字本身；两者都走 `store.add_library` 这一个收口。"""
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

    def stop_index_run(self, library_id: str, run_id: str) -> dict[str, Any]:
        """按 (库, run_id) 精确停止一次索引（MCP/脚本用）。GUI 契约里的 `stop_index()`
        不带参数，只停本进程拉起的 run。"""
        try:
            stopped, message = self._pipeline.stop_index_library(library_id, run_id)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "stopped": stopped, "message": message}

    def index_failures(self, library_id: str) -> dict[str, Any]:
        try:
            failures = self._pipeline.index_failures(library_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "failures": (failures or {}).get("failures", [])}

    # ---- 检索（精确 API：库在前、query 在后）--------------------------------------

    def search_scoped(
        self,
        libraries: str,
        query: str,
        top_k: int = 5,
        *,
        exclude: str = "",
        folder: str = "",
    ) -> dict[str, Any]:
        """库在前、query 在后的**精确**多库检索入口（MCP 同款语义，供脚本与测试用）。
        GUI 契约里的 `search(query, top_k, libraries, include_body)` 参数顺序相反
        （pywebview 按位置映射），见 `contract_bridge.py`。

        `libraries` 支持 `core/pipeline.py::search` 的多库选库语法（单库 id/逗号分隔多库/
        空="全部库"/"all"）。"""
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

    def document_text(self, library_id: str, path: str) -> dict[str, Any]:
        """读一篇文档的完整正文（精确 API，库 id 在前）。契约版 `read_document(lib, rel)`
        额外做了渲染与截断，见 `contract_bridge.py`。"""
        try:
            document = self._pipeline.read_document(library_id, path)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": document.path, "text": document.text, "source": document.source}

    def navigate(self, library_id: str, query: str, top_k: int = 5) -> dict[str, Any]:
        """页级视觉导航——独立于 search() 的"第二检索系统"，见 core/pipeline.py::navigate()
        的说明。返回的每条结果只有 PDF 路径+页码+相似度分数，不返回图片本身（同旧项目：
        GUI 只展示状态/结果列表，不做页面图片预览）。"""
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

    # ---- 导出 / 导入（精确 API：用户指定归档路径）------------------------------------

    def export_library(self, library_id: str, dest_path: str) -> dict[str, Any]:
        """把一个库导出成归档文件，写到 dest_path（用户填的目标路径，比如"把这个库搬到另一台
        电脑"场景下先导出到U盘/网盘同步目录）。文件 I/O 在这一层做——core 只产出/消费 bytes，
        不知道"文件"这个概念（见 core/pipeline.py 的 export_library 注释）。"""
        try:
            archive_bytes = self._pipeline.export_library(library_id)
            atomic_write_bytes(Path(dest_path), archive_bytes)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def import_library(self, archive_path: str, root_path: str, library_id: str = "") -> dict[str, Any]:
        """从 export_library 写出的归档文件恢复一个库。library_id 留空（前端不填这个字段）
        则沿用归档里记录的原始 library_id。"""
        try:
            archive_bytes = Path(archive_path).read_bytes()
            new_id = self._pipeline.import_library(
                archive_bytes, root_path=root_path, library_id=library_id or None
            )
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "library_id": new_id}

    # ---- 库简介（精确 API）------------------------------------------------------

    def get_library_summary(self, library_id: str) -> dict[str, Any]:
        try:
            summary = self._pipeline.get_library_summary(library_id)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "text": summary.text, "source": summary.source, "model": summary.model}

    def refresh_library_summary(self, library_id: str, force: bool = False) -> dict[str, Any]:
        """"刷新简介"：真的调一次配置好的 llm_provider 生成新简介，直接写入（不经过写权限
        门禁——GUI 点按钮本身就是明确的人类操作）。当前简介若是用户手写的且 force=False，
        不生成也不覆盖，返回 `needs_confirm=True` 让前端二次确认后带 force=True 重试。
        批量场景用契约方法 `refresh_library_summaries_batch`（后台线程 + 轮询）。"""
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

    # ---- 设置（精确 API：原始键值）--------------------------------------------------

    def dedup_run_library(self, library_id: str, threshold: float = 0.8) -> dict[str, Any]:
        """近似去重分析（只读，单库）。契约名 `dedup_run(threshold)` 是跨全部已注册库的
        聚合版本（打平成相似对），见 `contract_bridge.py`。"""
        try:
            groups = self._pipeline.find_duplicates(library_id, threshold=threshold)
            clusters = [g for provider_groups in groups.values() for g in provider_groups]
            return {"ok": True, "clusters": clusters}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def get_settings_values(self) -> dict[str, Any]:
        """返回 `{"values": {...}, "meta": {key: {label, hint, secret}}}`——对齐
        obsidian-rag/guiweb/bridge.py::get_settings 一次把当前值和字段元信息一起带回的设计：
        `values` 只含"已经被显式设过值"的键，`meta` 覆盖全部已知键（含未设置的）供前端展示
        说明。契约名 `get_settings()` 返回的是分组的形状，见 `contract_bridge.py`。"""
        values = self._pipeline.runtime.settings.all()
        meta: dict[str, Any] = {}
        for key in dict.fromkeys([*FIELDS, *values]):
            entry = dict(SETTING_FIELD_META.get(key, {}))
            entry.setdefault("label", key)
            entry.setdefault("hint", "")
            entry["secret"] = setting_is_secret(key)
            meta[key] = entry
        return {"values": values, "meta": meta}

    def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        """写一个设置项，立即持久化、对后续调用立即生效（不需要重启——`core/pipeline.py`
        等每次调用都现读 `ctx.settings`）。这个方法本身不校验 `value` 的类型/合法性——校验
        发生在真正读取它的那一处（`SettingsStore.get()` 按调用方传的 default 类型核对）；
        设置页走契约方法 `save_settings`，那里按键声明的类型转换。"""
        try:
            self._pipeline.runtime.settings.set(key, value)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def unset_setting(self, key: str) -> dict[str, Any]:
        """删掉一个设置项，恢复成"未设置"（后续读取会拿回调用方自己的默认值）。"""
        try:
            self._pipeline.runtime.settings.unset(key)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    # ---- GPU（精确 API：新能力，旧项目没有对应按钮，见 BC-16）--------------------------

    def release_gpu_memory(self) -> dict[str, Any]:
        """手动立即释放显存——2026-09-29 操作者需求，旧项目 guiweb 没有对应
        按钮（contracts.md 的 GPU 卡片只读展示），已登记为 BC-16 新能力，
        不是契约方法（不占用 37 个契约名额），前端按精确 API 直接调用。

        只卸载模型、归还 GPU 名额，不碰子进程/插件启用状态/持久化配置——
        完整语义见 `core/pipeline.py::release_gpu_memory`。返回
        `{released, skipped, errors}`：`released` 是这次真的卸载了模型的
        插件 id 列表（可能是空列表，比如显存本来就是空的）；`skipped` 是
        没启用/不支持这个操作的插件 id；`errors` 是释放失败的插件 id →
        原因，某个插件失败不影响其它插件已经释放成功的部分。"""
        try:
            return self._pipeline.release_gpu_memory()
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring：绝不让一次操作失败带崩整个窗口
            return {"released": [], "skipped": [], "errors": {"_pipeline": str(exc)}}
