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

from core.atomic import atomic_write_text


@dataclass
class LibraryConfig:
    library_id: str
    name: str
    root_path: str
    selection_in: list[str] = field(default_factory=list)
    selection_out: list[str] = field(default_factory=list)
    new_file_default: str = "include"  # "include" | "exclude"
    enabled_extensions: list[str] = field(default_factory=lambda: [".md", ".pdf", ".docx"])
    agent_formats: list[str] = field(default_factory=list)
    exclude_dirs: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)


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
        # 原子写——对齐 obsidian-rag/library.py::save_registry（421-427）对
        # libraries.json 的 tmp+replace 纪律：注册表写半截 = 全部库配置丢失。
        raw = {lib_id: asdict(cfg) for lib_id, cfg in self._libraries.items()}
        atomic_write_text(self.path, json.dumps(raw, ensure_ascii=False, indent=2))

    @staticmethod
    def _validate_identity(library_id: str, name: str, root_path: str) -> tuple[str, str, str]:
        library_id = str(library_id).strip()
        name = str(name).strip()
        root_path = str(root_path).strip()
        if not library_id or library_id in {".", ".."} or any(char in library_id for char in "/\\"):
            raise ValueError(f"非法库 id: {library_id!r}")
        if any(ord(char) < 32 for char in library_id + name):
            raise ValueError("库 id 和名称不能包含控制字符")
        if not name:
            raise ValueError("库名称不能为空")
        if not root_path or "\x00" in root_path:
            raise ValueError(f"非法库路径: {root_path!r}")
        return library_id, name, root_path

    def add_library(self, library_id: str, name: str, root_path: str) -> LibraryConfig:
        library_id, name, root_path = self._validate_identity(library_id, name, root_path)
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

    def set_policy(
        self,
        library_id: str,
        *,
        new_file_default: str | None = None,
        enabled_extensions: list[str] | None = None,
        exclude_dirs: list[str] | None = None,
        exclude_files: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> LibraryConfig:
        """改"新文件默认策略"/"启用格式列表"——这两项在 `add_library` 时
        只能取字段默认值，之前没有任何公开方法能在创建后改它们（GUI 设置
        面板、tools/migrate_libraries_json.py 都需要这个能力，不是只服务
        迁移脚本一个调用方，所以做成正式的公开方法，不是迁移脚本专用的
        私有旁路）。"""
        cfg = self._libraries.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        if new_file_default is not None:
            cfg.new_file_default = new_file_default
        if enabled_extensions is not None:
            cfg.enabled_extensions = enabled_extensions
        if exclude_dirs is not None:
            cfg.exclude_dirs = exclude_dirs
        if exclude_files is not None:
            cfg.exclude_files = exclude_files
        if exclude_patterns is not None:
            cfg.exclude_patterns = exclude_patterns
        self._save()
        return cfg

    def set_agent_formats(self, library_id: str, formats: list[str]) -> LibraryConfig:
        cfg = self._libraries.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        normalized: list[str] = []
        for value in formats:
            extension = str(value).strip().lower()
            if extension and not extension.startswith("."):
                extension = "." + extension
            if extension not in {".pdf", ".docx"}:
                raise ValueError(f"Agent 二进制授权只支持 .pdf/.docx，收到: {value!r}")
            if extension not in normalized:
                normalized.append(extension)
        cfg.agent_formats = normalized
        self._save()
        return cfg

    def remove_library(self, library_id: str) -> None:
        self._libraries.pop(library_id, None)
        self._save()
