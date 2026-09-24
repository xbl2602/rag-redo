"""PluginContext：核心传给插件生命周期钩子的唯一接口。

插件不能绕过 ctx 直接 import 核心内部模块——这是插件和"核心内部子模块"的
本质区别，见 ../docs/PLUGIN_SPEC.md 第3节。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .datastore import DataStore, StorageHandle
from .manifest import RuntimeSpec
from .resource_arbiter import ResourceArbiter
from .settings import SettingsStore
from .write_gate import WriteGate


@dataclass
class PluginContext:
    plugin_id: str
    logger: logging.Logger
    data_store: DataStore
    resource_arbiter: ResourceArbiter
    write_gate: WriteGate
    #: 通用插件设置存储（2026-09-23 补，见 core/settings.py 模块 docstring）
    #: ——用户可调、需要跨重启持久化的参数（RRF权重、默认库范围、外部
    #: 解释器覆盖路径……）都该走这里 `ctx.settings.get(key, default)`/
    #: `ctx.settings.set(key, value)`，不要自己发明一份环境变量或本地
    #: 文件——那样每个插件各管一套，GUI 没法统一列出"当前有哪些设置"。
    settings: SettingsStore
    storage: StorageHandle
    #: 这个插件自己在 plugin.toml 里声明的 [runtime] 段——subprocess_service
    #: 插件的 on_enable/on_disable 从这里读 command/health_check/
    #: env_bootstrap，用 core.subprocess_service.SubprocessServiceHandle
    #: 启动/终止自己的子进程，而不是在 Python 代码里把 command 再硬编码
    #: 一遍（plugin.toml 是唯一权威来源，见 docs/DATA_FLOW.md"同一件事只
    #: 能在一处定义"）。in_process 插件通常用不到这个字段。
    runtime: RuntimeSpec
