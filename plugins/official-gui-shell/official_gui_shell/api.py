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

from pathlib import Path
from typing import Any

from core.pipeline import Pipeline


class Api:
    def __init__(self, pipeline: Pipeline, lib_mgr: Any) -> None:
        self._pipeline = pipeline
        self._lib_mgr = lib_mgr

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

    def reindex_library(self, library_id: str) -> dict[str, Any]:
        try:
            report = self._pipeline.index_library(library_id)
        except (KeyError, Exception) as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "succeeded": report.succeeded,
            "failed": report.failed,
            "failures": [
                {"path": f.path, "reason": f.extract_failure}
                for f in report.files
                if f.included and not f.extracted
            ],
        }

    def search(self, library_id: str, query: str, top_k: int = 10) -> dict[str, Any]:
        if not query.strip():
            return {"ok": True, "results": []}
        try:
            results = self._pipeline.search(library_id, query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "results": [
                {
                    "path": r.path,
                    "heading": r.heading_breadcrumb,
                    "text": r.text,
                    "confidence": round(r.confidence, 3),
                }
                for r in results
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
            Path(dest_path).write_bytes(archive_bytes)
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

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
