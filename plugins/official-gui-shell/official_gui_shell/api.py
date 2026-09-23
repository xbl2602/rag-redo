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
            Path(dest_path).write_bytes(archive_bytes)
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
        再问要不要覆盖"逻辑）。

        **已知的简化**：旧项目这一步是后台线程+前端轮询（本地思考型模型
        一次生成可能要几十秒到几分钟，同步等待会让弹层"卡住"）——rag-redo
        的 GUI Api 层目前完全没有"后台任务+轮询"基础设施，这里简化成
        同步阻塞调用，是刻意的简化，不是假装做了异步，调用方（前端）
        目前需要自己接受这次调用可能较慢。"""
        try:
            current = self._pipeline.get_library_summary(library_id)
            if current.source == "user" and not force:
                return {"ok": False, "needs_confirm": True}
            text, provider_id = self._pipeline.generate_library_summary(library_id)
            result = self._pipeline.set_library_summary_direct(library_id, text, source="ai")
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": str(exc)}
        result["provider"] = provider_id
        return result

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
