"""数据流/契约管理器：插件唯一被允许读写持久化状态的入口（最小实现）。

Phase 0 只需要证明"按契约类型收窄读写权限"这个机制本身能工作，不需要真正
落盘——用内存字典即可。Phase 1 接入真实存储后端（向量库/词法索引/库配置）
时替换内部实现，对外接口不变。规则见 ../AGENTS.md"两个核心组件"节、
../docs/DATA_FLOW.md 数据的读/写边界表。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class DataAccessError(PermissionError):
    """插件试图读/写不属于自己、且未声明为公开的契约数据——这是数据流铁律5
    "插件不直接读写持久化存储、按扩展点类型收窄权限"的最小可验证形态。"""


@dataclass
class _Entry:
    owner: str
    value: object
    public: bool


@dataclass
class DataStore:
    _entries: dict[str, _Entry] = field(default_factory=dict)

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
