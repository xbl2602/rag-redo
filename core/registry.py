"""扩展点注册表。

单例扩展点（embedder/vector_store/... ）同一时刻只能有一个生效实现；多个
已启用插件同时声明同一单例点时必须显式报冲突，不能静默选一个——这是"宁可
诚实空缺，不产出拼接半成品"原则在插件系统里的体现。多值扩展点
（gui_panel/mcp_tool_provider/...）没有这个限制，所有已启用插件一起参与。

**一个点是单例还是多值，由声明它的插件自己在 manifest 里说**
（`provides.<point> = "singleton" | "multi"`），核心不维护一份写死的扩展点
名单——核心不该需要预先知道每一个可能出现的扩展点名字，尤其是第三方插件
完全可以发明全新的扩展点。如果两个插件对同一个点的基数声明不一致（一个说
singleton 一个说 multi），保守按 singleton 处理（宁可多报冲突，不可漏报）。

规则见 ../AGENTS.md"两个核心组件"节、../docs/ARCHITECTURE.md 第2.1/4节、
../docs/PLUGIN_SPEC.md 第2节 provides 字段格式。
"""
from __future__ import annotations

from dataclasses import dataclass, field

Cardinality = str  # "singleton" | "multi"


class ExtensionConflictError(Exception):
    """同一单例扩展点被多个已启用插件同时声明，且没有配置显式指定用哪个"""


@dataclass
class ExtensionRegistry:
    _providers: dict[str, list[str]] = field(default_factory=dict)
    _cardinalities: dict[str, set[Cardinality]] = field(default_factory=dict)
    _active_choice: dict[str, str] = field(default_factory=dict)

    def register(self, plugin_id: str, provides: dict[str, Cardinality]) -> None:
        for point, cardinality in provides.items():
            bucket = self._providers.setdefault(point, [])
            if plugin_id not in bucket:
                bucket.append(plugin_id)
            self._cardinalities.setdefault(point, set()).add(cardinality)

    def unregister(self, plugin_id: str) -> None:
        for providers in self._providers.values():
            if plugin_id in providers:
                providers.remove(plugin_id)

    def set_active(self, point: str, plugin_id: str) -> None:
        self._active_choice[point] = plugin_id

    def providers_of(self, point: str) -> list[str]:
        return list(self._providers.get(point, []))

    def is_singleton(self, point: str) -> bool:
        """一个点只要有任何一个插件声明它是 singleton，就按 singleton 处理——
        宁可多报冲突（安全），不可漏报（危险，等于悄悄拼接了两个不兼容的实现）。"""
        declared = self._cardinalities.get(point, set())
        return "singleton" in declared

    def active_of(self, point: str) -> str | None:
        """单例点当前生效者；多值点不适用（恒返回 None）。

        单一候选时不需要用户选择，直接生效；多个候选且没有显式选择时抛
        ExtensionConflictError——绝不静默挑一个。
        """
        if not self.is_singleton(point):
            return None
        providers = self.providers_of(point)
        if len(providers) == 0:
            return None
        if len(providers) == 1:
            return providers[0]
        choice = self._active_choice.get(point)
        if choice in providers:
            return choice
        raise ExtensionConflictError(
            f"扩展点 {point!r} 有多个已启用实现 {providers}，且未配置当前生效者——"
            "不能静默选一个，必须由用户显式指定"
        )

    def conflicts(self) -> dict[str, list[str]]:
        """列出当前所有处于冲突状态的单例扩展点，供插件管理器展示。"""
        result: dict[str, list[str]] = {}
        for point in self._providers:
            if not self.is_singleton(point):
                continue
            providers = self.providers_of(point)
            if len(providers) > 1 and self._active_choice.get(point) not in providers:
                result[point] = providers
        return result
