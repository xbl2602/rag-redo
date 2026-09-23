"""插件运行时：发现→校验→加载→启用/禁用→卸载。

规则见 ../AGENTS.md"两个核心组件"节、../docs/PLUGIN_SPEC.md 第3-4节。
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .context import PluginContext
from .datastore import DataStore
from .manifest import ManifestError, PluginManifest, load_manifest, validate_manifest
from .registry import ExtensionRegistry
from .resource_arbiter import ResourceArbiter
from .settings import SettingsStore
from .write_gate import WriteGate


class PluginState(str, Enum):
    DISCOVERED = "discovered"
    INVALID = "invalid"
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"
    FAILED = "failed"


@dataclass
class Plugin:
    manifest: PluginManifest | None
    state: PluginState
    error: str | None = None
    instance: object | None = None


class PluginRuntime:
    """核心组件之一：插件运行时。

    plugins_dir 可以是真实的 plugins/ 目录，也可以是 examples/ 或测试用的
    临时目录——扫描路径不写死，方便隔离测试（继承旧项目"索引集成测试用隔离
    环境、绝不碰真实数据"的纪律）。state_file 为 None 时纯内存运行（单元测试
    用）；给定路径时启用/禁用状态跨进程持久化，对应 docs/DATA_FLOW.md
    "插件启用状态/配置"这一行的读写权限约定。
    """

    def __init__(
        self,
        plugins_dir: Path,
        *,
        state_file: Path | None = None,
        data_dir: Path = Path("data"),
        data_store: DataStore | None = None,
        resource_arbiter: ResourceArbiter | None = None,
        write_gate: WriteGate | None = None,
        settings: SettingsStore | None = None,
    ) -> None:
        self.plugins_dir = plugins_dir
        self.state_file = state_file
        self.data_dir = data_dir
        self.registry = ExtensionRegistry()
        self.data_store = data_store if data_store is not None else DataStore()
        self.resource_arbiter = resource_arbiter if resource_arbiter is not None else ResourceArbiter()
        self.write_gate = write_gate if write_gate is not None else WriteGate()
        self.settings = settings if settings is not None else SettingsStore(data_dir / "settings.json")
        self.plugins: dict[str, Plugin] = {}
        self._logger = logging.getLogger("rag_redo.core.runtime")
        self._enabled_ids: set[str] = self._load_state()

    # ---- 发现 ----------------------------------------------------------

    def scan(self) -> None:
        """显式触发的发现动作——不做持续文件监听（见 AGENTS.md 插件运行时一节）。

        对曾经启用过、这次重新发现到的插件，自动 load+enable——核心重启后
        插件应该自己恢复到之前的状态，不需要用户每次都手动重新启用一遍。
        """
        if not self.plugins_dir.exists():
            return
        for entry in sorted(self.plugins_dir.iterdir()):
            if not entry.is_dir() or not (entry / "plugin.toml").exists():
                continue
            try:
                manifest = load_manifest(entry)
            except ManifestError as exc:
                self.plugins[entry.name] = Plugin(manifest=None, state=PluginState.INVALID, error=str(exc))
                continue
            errors = validate_manifest(manifest)
            if errors:
                self.plugins[manifest.id] = Plugin(
                    manifest=manifest, state=PluginState.INVALID, error="; ".join(errors)
                )
                continue
            self.plugins[manifest.id] = Plugin(manifest=manifest, state=PluginState.DISCOVERED)

        for plugin_id in sorted(self._enabled_ids):
            plugin = self.plugins.get(plugin_id)
            if plugin is not None and plugin.state == PluginState.DISCOVERED:
                self.load(plugin_id)
                if self.plugins[plugin_id].state == PluginState.LOADED:
                    self.enable(plugin_id)

    # ---- 生命周期 --------------------------------------------------------

    def load(self, plugin_id: str) -> None:
        plugin = self._require(plugin_id)
        if plugin.state != PluginState.DISCOVERED:
            return
        assert plugin.manifest is not None
        try:
            instance = self._instantiate(plugin.manifest)
            ctx = self._make_context(plugin.manifest.id)
            if hasattr(instance, "on_load"):
                instance.on_load(ctx)
            plugin.instance = instance
            plugin.state = PluginState.LOADED
            self.registry.register(plugin.manifest.id, plugin.manifest.provides)
        except Exception as exc:  # noqa: BLE001 - 插件炸了绝不能牵连宿主，架构红线4
            plugin.state = PluginState.FAILED
            plugin.error = f"{type(exc).__name__}: {exc}"
            self._logger.warning("插件 %s 加载失败: %s", plugin_id, plugin.error)

    def enable(self, plugin_id: str) -> None:
        plugin = self._require(plugin_id)
        if plugin.state not in (PluginState.LOADED, PluginState.DISABLED):
            return
        try:
            ctx = self._make_context(plugin_id)
            if hasattr(plugin.instance, "on_enable"):
                plugin.instance.on_enable(ctx)
            plugin.state = PluginState.ENABLED
            plugin.error = None
            self._enabled_ids.add(plugin_id)
        except Exception as exc:  # noqa: BLE001
            plugin.state = PluginState.FAILED
            plugin.error = f"{type(exc).__name__}: {exc}"
            self._enabled_ids.discard(plugin_id)
            self._logger.warning("插件 %s 启用失败: %s", plugin_id, plugin.error)
        self._save_state()

    def disable(self, plugin_id: str) -> None:
        plugin = self._require(plugin_id)
        if plugin.state != PluginState.ENABLED:
            return
        try:
            ctx = self._make_context(plugin_id)
            if hasattr(plugin.instance, "on_disable"):
                plugin.instance.on_disable(ctx)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("插件 %s 禁用时出错（仍标记为 disabled）: %s", plugin_id, exc)
        plugin.state = PluginState.DISABLED
        self._enabled_ids.discard(plugin_id)
        self._save_state()

    def unload(self, plugin_id: str) -> None:
        plugin = self._require(plugin_id)
        try:
            ctx = self._make_context(plugin_id)
            if plugin.instance is not None and hasattr(plugin.instance, "on_unload"):
                plugin.instance.on_unload(ctx)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("插件 %s 卸载时出错: %s", plugin_id, exc)
        if plugin.manifest is not None:
            self.registry.unregister(plugin.manifest.id)
        plugin.instance = None
        plugin.state = PluginState.DISCOVERED
        self._enabled_ids.discard(plugin_id)
        self._save_state()

    # ---- 内部 ----------------------------------------------------------

    def _instantiate(self, manifest: PluginManifest) -> object:
        # in_process 和 subprocess_service 用同一条实例化路径：两者都通过
        # runtime.entry 指向一个本地 Python 类。区别只在于这个类的
        # on_enable/on_disable 内部做什么——in_process 直接做真正的工作，
        # subprocess_service 用 core.subprocess_service.SubprocessServiceHandle
        # 启动/终止一个真正跑重依赖的子进程，把方法调用转发成本机HTTP请求。
        # 见 docs/PLUGIN_SPEC.md 第3节。
        #
        # 简化处理：把插件目录加进 sys.path 才能 import 它的入口模块。真正的
        # 每插件导入隔离（防止两个插件用了同名顶层模块互相冲突）留给有真实
        # 依赖冲突风险时再做，当前的目标只是证明生命周期机制本身能跑通。
        source_dir = str(manifest.source_dir)
        if source_dir not in sys.path:
            sys.path.insert(0, source_dir)
        assert manifest.runtime.entry is not None
        module_name, _, class_name = manifest.runtime.entry.partition(":")
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        return cls()

    def _make_context(self, plugin_id: str) -> PluginContext:
        manifest = self.plugins[plugin_id].manifest
        assert manifest is not None  # ctx 只在插件通过校验后才会被构造
        return PluginContext(
            plugin_id=plugin_id,
            logger=logging.getLogger(f"rag_redo.plugin.{plugin_id}"),
            data_store=self.data_store,
            resource_arbiter=self.resource_arbiter,
            write_gate=self.write_gate,
            settings=self.settings,
            data_dir=self.data_dir,
            runtime=manifest.runtime,
        )

    def _require(self, plugin_id: str) -> Plugin:
        if plugin_id not in self.plugins:
            raise KeyError(f"未知插件: {plugin_id}")
        return self.plugins[plugin_id]

    def _load_state(self) -> set[str]:
        if self.state_file is None or not self.state_file.exists():
            return set()
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return set(data.get("enabled", []))
        except (json.JSONDecodeError, OSError):
            # 状态文件损坏时安全降级为"没有任何插件是启用的"，不崩溃——
            # 这是"失败折叠成诚实终态"在持久化状态读取上的体现。
            return set()

    def _save_state(self) -> None:
        if self.state_file is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps({"enabled": sorted(self._enabled_ids)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
