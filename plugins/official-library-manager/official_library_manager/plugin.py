"""official-library-manager 插件：多库/路径级勾选管理。

裁决算法在 selection.py（decide_included/collect_included_files，唯一
权威实现），配置持久化在 config.py。本文件是生命周期钩子的薄封装+对外
方法入口。
"""
from __future__ import annotations

from pathlib import Path

from .config import LibraryConfig, LibraryConfigStore
from .selection import collect_included_files


class LibraryManagerPlugin:
    def __init__(self) -> None:
        self.store: LibraryConfigStore | None = None

    def on_load(self, ctx):
        self.store = LibraryConfigStore(ctx.data_dir / "libraries.json")
        ctx.logger.info("library-manager 已加载，%d 个库", len(self.store.list_libraries()))

    def on_enable(self, ctx):
        ctx.logger.info("library-manager 已启用")

    def on_disable(self, ctx):
        ctx.logger.info("library-manager 已禁用")

    def on_unload(self, ctx):
        self.store = None

    def resolve_libraries(self, libraries: str = "", exclude: str = "") -> list[LibraryConfig]:
        """按"白名单减法"解析出这次检索该覆盖哪些库——对齐 obsidian-rag/
        library.py::resolve_entries 的多库选择语义（旧项目 GUI 的库勾选树、
        `search_knowledge` MCP 工具的 `libraries`/`exclude` 参数最终都调
        这一个函数）：`libraries` 为空 = 全部已注册库；`"all"` 显式等同于
        空；`"A,B"` 按逗号切开多库并查（去重但保留顺序）；`exclude` 做
        减法，在 `libraries` 解析结果之上再排除；两边出现未知库id直接
        报错并把全部可用库名列出来，不静默忽略打错的名字。

        **一处已知的、如实记录的简化**：obsidian-rag 在 `libraries` 为空时
        还会先查一层全局配置 `default_libraries`（用户可以设定"新增库/
        非笔记库默认不参与检索"），配置也是空才最终回退全部库；rag-redo
        目前还没有通用的插件配置存储（见 core/context.py 的 PluginContext
        字段，没有 settings/config 这类入口），所以这里直接以"全部库"
        作为唯一默认——这是"暂无配置存储基建"的简化，不是"多库选择"这个
        能力本身缺角；default_libraries 这类可配置默认值等通用配置存储
        落地后可以在这里追加一层，不影响调用方已经在用的 libraries/
        exclude 语义。
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
