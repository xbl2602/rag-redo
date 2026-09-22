"""PluginContext：核心传给插件生命周期钩子的唯一接口。

插件不能绕过 ctx 直接 import 核心内部模块——这是插件和"核心内部子模块"的
本质区别，见 ../docs/PLUGIN_SPEC.md 第3节。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .datastore import DataStore
from .resource_arbiter import ResourceArbiter


@dataclass
class PluginContext:
    plugin_id: str
    logger: logging.Logger
    data_store: DataStore
    resource_arbiter: ResourceArbiter
