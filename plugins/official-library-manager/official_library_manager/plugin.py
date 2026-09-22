"""official-library-manager 插件：多库/路径级勾选管理。

裁决算法在 selection.py（decide_included/collect_included_files，唯一
权威实现），配置持久化在 config.py。本文件是生命周期钩子的薄封装+对外
方法入口。
"""
from __future__ import annotations

from pathlib import Path

from .config import LibraryConfigStore
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
