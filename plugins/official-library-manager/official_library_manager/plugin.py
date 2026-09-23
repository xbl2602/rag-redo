"""official-library-manager 插件：多库/路径级勾选管理。

裁决算法在 selection.py（decide_included/collect_included_files，唯一
权威实现），配置持久化在 config.py。本文件是生命周期钩子的薄封装+对外
方法入口。
"""
from __future__ import annotations

from pathlib import Path

from core.write_gate import WriteGateError

from .config import LibraryConfig, LibraryConfigStore
from .selection import apply_selection_changes, collect_included_files, normalize_selection_changes


class LibraryManagerPlugin:
    def __init__(self) -> None:
        self.store: LibraryConfigStore | None = None
        self._settings = None
        self._write_gate = None
        self._logger = None

    def on_load(self, ctx):
        self.store = LibraryConfigStore(ctx.data_dir / "libraries.json")
        self._settings = ctx.settings
        self._write_gate = ctx.write_gate
        self._logger = ctx.logger
        ctx.logger.info("library-manager 已加载，%d 个库", len(self.store.list_libraries()))

    def on_enable(self, ctx):
        ctx.logger.info("library-manager 已启用")

    def on_disable(self, ctx):
        ctx.logger.info("library-manager 已禁用")

    def on_unload(self, ctx):
        self.store = None
        self._settings = None
        self._write_gate = None
        self._logger = None

    def resolve_libraries(self, libraries: str = "", exclude: str = "") -> list[LibraryConfig]:
        """按"白名单减法"解析出这次检索该覆盖哪些库——对齐 obsidian-rag/
        library.py::resolve_entries 的多库选择语义（旧项目 GUI 的库勾选树、
        `search_knowledge` MCP 工具的 `libraries`/`exclude` 参数最终都调
        这一个函数）：`libraries` 为空 = 先查 `default_libraries` 设置
        （见下），配置也是空才回退全部已注册库；`"all"` 显式等同于"忽略
        default_libraries、就是要全部库"；`"A,B"` 按逗号切开多库并查
        （去重但保留顺序）；`exclude` 做减法，在解析结果之上再排除；
        两边出现未知库id直接报错并把全部可用库名列出来，不静默忽略
        打错的名字。

        **`default_libraries` 设置（2026-09-23 接入 core/settings.py 通用
        设置存储后补齐，此前是已知的、如实记录的简化）**：对齐
        obsidian-rag/config.py 的 `default_libraries` 项——用户可以设定
        "新增库/非笔记库默认不参与检索"，`libraries` 参数留空时优先用这份
        默认范围，而不是不由分说地查全部库；配置里的库id如果已经被删除，
        静默跳过（不算入未知库名报错），配置项全部失效或本来就没配才
        回退全部库——同 obsidian-rag `resolve_entries` 的"默认库全部失效
        →回退全部库（旧行为）"语义。
        """
        assert self.store is not None
        entries = self.store.list_libraries()
        by_id = {cfg.library_id: cfg for cfg in entries}
        if not by_id:
            raise ValueError("当前没有已注册库，请先创建一个库。")
        if libraries:
            names = list(dict.fromkeys(n.strip() for n in libraries.split(",") if n.strip()))
            if names and names[0].lower() == "all":
                names = list(by_id)
        else:
            defaults = self._settings.get("default_libraries", []) if self._settings is not None else []
            names = [n for n in defaults if n in by_id]
            if not names:
                names = list(by_id)
        ex = [n.strip() for n in exclude.split(",") if n.strip()]
        unknown = sorted({n for n in names + ex if n not in by_id})
        if unknown:
            raise ValueError(f"未知库id：{', '.join(unknown)}。可用库：{', '.join(sorted(by_id))}")
        final = [n for n in names if n not in ex]
        if not final:
            raise ValueError(
                f"所选范围为空：exclude 覆盖了全部指定库（libraries={libraries or '全部'}，"
                f"exclude={exclude or '无'}）。可用库：{', '.join(sorted(by_id))}"
            )
        return [by_id[n] for n in final]

    def resolve_included_files(self, library_id: str) -> list[tuple[str, bool, str]]:
        """返回某个库里全部文件的裁决结果（含未纳入的，附原因）——给"文件
        生效明细"这类界面直接用，不用重新扫一遍。"""
        assert self.store is not None
        cfg = self.store.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        all_paths = self._enumerate_files(cfg.root_path)
        return collect_included_files(
            all_paths,
            selection_in=cfg.selection_in,
            selection_out=cfg.selection_out,
            new_file_default=cfg.new_file_default,  # type: ignore[arg-type]
            enabled_extensions=cfg.enabled_extensions,
        )

    @staticmethod
    def _enumerate_files(root_path: str) -> list[str]:
        root = Path(root_path)
        if not root.exists():
            return []
        return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())

    # ---- 路径级勾选变更（写权限门禁保护，2026-09-23 全面功能审计B类）------
    #
    # 对齐 obsidian-rag 的 get_selection/propose_selection_changes/
    # apply_selection_changes：判定逻辑（decide_included）早就有了，缺的
    # 是"AI 想改这份配置"这条写路径。与 official-library-summary 的
    # propose()/apply() 不同之处——**这里没有"此前非用户手写就直接生效"
    # 的分支，永远走门禁**：被排除的文件会从整个 RAG 流程里消失（不扫描/
    # 不嵌入/不OCR），风险等级比库简介文本高一截，obsidian-rag 原文档
    # 用词是"硬性确认门禁：本工具绝不直接生效"，逐字照做。

    def get_selection(self, library_id: str) -> dict:
        """只读：查看某库当前的路径级勾选状态。"""
        cfg = self.store.get(library_id) if self.store else None
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        return {"selection_in": list(cfg.selection_in), "selection_out": list(cfg.selection_out)}

    def propose_selection_changes(self, library_id: str, changes: list[dict]) -> dict:
        assert self.store is not None and self._write_gate is not None
        cfg = self.store.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        norm = normalize_selection_changes(changes)  # 非法项直接抛异常，整体拒绝
        ticket = self._write_gate.propose(f"变更库「{library_id}」的路径级勾选", {
            "library_id": library_id,
            "changes": norm,
        })
        self._logger.info(
            "勾选变更提案已生成（库=%s，提案=%s，%d 项）——等待用户确认",
            library_id, ticket.proposal_id, len(norm),
        )
        return {
            "ok": True,
            "proposal_id": ticket.proposal_id,
            "confirmation_code": ticket.confirmation_code,
            "changes": norm,
        }

    def apply_selection_changes(self, library_id: str, proposal_id: str, confirmation_code: str) -> dict:
        assert self.store is not None and self._write_gate is not None
        try:
            payload = self._write_gate.confirm(proposal_id, confirmation_code)
        except WriteGateError as exc:
            self._logger.warning(
                "AUDIT 勾选变更提案被拒（库=%s，提案=%s）：%s", library_id, proposal_id, exc
            )
            return {"ok": False, "error": str(exc)}
        if payload.get("library_id") != library_id:
            return {"ok": False, "error": "提案与库名不匹配"}
        cfg = self.store.get(library_id)
        if cfg is None:
            return {"ok": False, "error": f"未知库: {library_id}"}
        new_in, new_out = apply_selection_changes(cfg.selection_in, cfg.selection_out, payload["changes"])
        self.store.set_selection(library_id, selection_in=new_in, selection_out=new_out)
        self._logger.info(
            "AUDIT 勾选变更已生效（库=%s，提案=%s，经用户确认，%d 项）",
            library_id, proposal_id, len(payload["changes"]),
        )
        return {"ok": True, "selection_in": new_in, "selection_out": new_out}
