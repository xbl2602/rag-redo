"""库配置的持久化：<data_dir>/libraries.json（data_dir 由核心通过
PluginContext.data_dir 提供，见 core/context.py，插件自己不拼路径）。

**已知的、刻意的 Phase 1 简化**：本模块直接负责自己配置的读写，而不是先
设计一套通用的"DataStore 落盘后端"再削足适履——目前只有这一个插件需要
持久化配置，等 Phase 1 后续插件（比如库摘要）显露出真实的持久化需求形状
后，再回头把"插件私有配置落盘"的通用部分收进 core.datastore，不提前拍
脑袋设计（YAGNI）。这是对 docs/DATA_FLOW.md"插件不直接读写持久化存储"
这条规则的已知临时妥协，在这里明确记录，不是假装没这回事——见
docs/ROADMAP.md Phase 1 进度记录。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class LibraryConfig:
    library_id: str
    name: str
    root_path: str
    selection_in: list[str] = field(default_factory=list)
    selection_out: list[str] = field(default_factory=list)
    new_file_default: str = "include"  # "include" | "exclude"
    enabled_extensions: list[str] = field(default_factory=lambda: [".md", ".txt"])


class LibraryConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._libraries: dict[str, LibraryConfig] = self._load()

    def _load(self) -> dict[str, LibraryConfig]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}  # 损坏时安全降级为空，不崩溃——见 AGENTS.md 失败折叠纪律
        return {lib_id: LibraryConfig(**data) for lib_id, data in raw.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {lib_id: asdict(cfg) for lib_id, cfg in self._libraries.items()}
        self.path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_library(self, library_id: str, name: str, root_path: str) -> LibraryConfig:
        if library_id in self._libraries:
            raise ValueError(f"库 {library_id!r} 已存在")
        cfg = LibraryConfig(library_id=library_id, name=name, root_path=root_path)
        self._libraries[library_id] = cfg
        self._save()
        return cfg

    def list_libraries(self) -> list[LibraryConfig]:
        return list(self._libraries.values())

    def get(self, library_id: str) -> LibraryConfig | None:
        return self._libraries.get(library_id)

    def set_selection(
        self,
        library_id: str,
        *,
        selection_in: list[str] | None = None,
        selection_out: list[str] | None = None,
    ) -> LibraryConfig:
        cfg = self._libraries.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        if selection_in is not None:
            cfg.selection_in = selection_in
        if selection_out is not None:
            cfg.selection_out = selection_out
        self._save()
        return cfg

    def remove_library(self, library_id: str) -> None:
        self._libraries.pop(library_id, None)
        self._save()
