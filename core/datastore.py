"""数据流/契约管理器：插件唯一被允许读写持久化状态的入口。

DataStore 同时提供跨插件契约值的权限收窄和按插件发放的 StorageHandle；
存储插件只能拿到自己声明 data_write 后取得的 namespace/path，非存储插件
不能直接取得应用数据根目录。规则见 ../AGENTS.md"两个核心组件"节、
../docs/DATA_FLOW.md 数据的读/写边界表。

**权限校验的准确边界（如实记录）**：`issue_path` 要求 plugin_id 已通过
`storage_handle(..., allowed=True)` 登记为 data_write 授权方——未声明
data_write 的插件从此没有拿路径的通道（此前 issue_path 是公开方法，
handle 的门禁防君子不防小人）。但对已授权的存储插件，DataStore 不校验
"你申请的 namespace 是否属于你"：in_process 插件与核心共享进程，按
AGENTS.md"受信任插件"的定位不宣称抵御恶意代码；且 legacy 命名空间存在
多插件共享同一核心存储目录的真实用法（lexical-bm25/vector-store-chroma/
visual-wemm 共享 index_generations 供 core.index_generation 的分段存储），
全局碰撞检查会把这种刻意共享误伤成错误。这是信任模型内的门禁，不是
安全沙箱。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


class DataAccessError(PermissionError):
    """插件试图读/写不属于自己、且未声明为公开的契约数据——这是数据流铁律5
    "插件不直接读写持久化存储、按扩展点类型收窄权限"的最小可验证形态。"""


@dataclass
class _Entry:
    owner: str
    value: object
    public: bool


class StorageHandle:
    def __init__(
        self,
        store: "DataStore",
        owner: str,
        *,
        allowed: bool,
    ) -> None:
        self._store = store
        self._owner = owner
        self._allowed = allowed

    def directory(self, name: str, *, legacy: str | None = None) -> Path:
        if not self._allowed:
            raise DataAccessError(f"{self._owner} 未声明 data_write 权限")
        return self._store.issue_path(self._owner, name, legacy=legacy)

    def file(self, name: str, *, legacy: str | None = None) -> Path:
        if not self._allowed:
            raise DataAccessError(f"{self._owner} 未声明 data_write 权限")
        path = Path(name)
        if path.name != name or name in {"", ".", ".."}:
            raise DataAccessError(f"无效的数据文件名: {name!r}")
        return self._store.issue_path(
            self._owner,
            path.parent.as_posix() if path.parent != Path(".") else name,
            legacy=legacy or name,
        )

    def path(self, *parts: str) -> Path:
        if not self._allowed:
            raise DataAccessError(f"{self._owner} 未声明 data_write 权限")
        if not parts or any(not part or part in {".", ".."} for part in parts):
            raise DataAccessError("数据路径包含空段或越界段")
        return self._store.issue_path(self._owner, Path(*parts).as_posix())


@dataclass
class DataStore:
    root: Path | None = None
    _entries: dict[str, _Entry] = field(default_factory=dict)
    _issued: dict[tuple[str, str, str], Path] = field(default_factory=dict)
    _granted: set[str] = field(default_factory=set)

    def storage_handle(self, plugin_id: str, *, allowed: bool) -> StorageHandle:
        # allowed=真值登记进授权集合：issue_path 的权限校验以这份集合为
        # 唯一依据——此前 issue_path 是公开方法，任何拿到 ctx.data_store 的
        # 插件（哪怕没声明 data_write）都能直接绕过 handle 的门禁自领路径。
        if allowed:
            self._granted.add(plugin_id)
        return StorageHandle(self, plugin_id, allowed=allowed)

    def issue_path(
        self,
        plugin_id: str,
        name: str,
        *,
        legacy: str | None = None,
    ) -> Path:
        if self.root is None:
            raise DataAccessError("DataStore 未配置持久化根目录")
        if plugin_id not in self._granted:
            raise DataAccessError(
                f"{plugin_id} 未声明 data_write 权限，不能申领持久化路径"
                "（持久化访问必须经由 ctx.storage 发放的 StorageHandle）"
            )
        safe_name = Path(name)
        if safe_name.is_absolute() or any(part in {"", ".", ".."} for part in safe_name.parts):
            raise DataAccessError(f"无效的数据命名空间: {name!r}")
        legacy_key = legacy or ""
        key = (plugin_id, safe_name.as_posix(), legacy_key)
        if key in self._issued:
            return self._issued[key]
        if legacy:
            legacy_path = Path(legacy)
            if legacy_path.is_absolute() or ".." in legacy_path.parts:
                raise DataAccessError(f"无效的数据兼容路径: {legacy!r}")
            path = self.root / legacy_path
        else:
            owner = re.sub(r"[^\w.-]", "_", plugin_id)
            path = self.root / "plugin_data" / owner / safe_name
        self._issued[key] = path
        return path

    def write(self, plugin_id: str, contract_type: str, value: object, *, public: bool = False) -> None:
        existing = self._entries.get(contract_type)
        if existing is not None and existing.owner != plugin_id:
            raise DataAccessError(f"{plugin_id} 不能写 {contract_type!r}——它属于 {existing.owner}")
        self._entries[contract_type] = _Entry(owner=plugin_id, value=value, public=public)

    def read(self, plugin_id: str, contract_type: str) -> object:
        entry = self._entries.get(contract_type)
        if entry is None:
            return None
        if entry.owner == plugin_id or entry.public:
            return entry.value
        raise DataAccessError(f"{plugin_id} 不能读 {contract_type!r}——它属于 {entry.owner} 且未公开")
