"""PluginContext：核心传给插件生命周期钩子的唯一接口。

插件不能绕过 ctx 直接 import 核心内部模块——这是插件和"核心内部子模块"的
本质区别，见 ../docs/PLUGIN_SPEC.md 第3节。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .datastore import DataStore
from .resource_arbiter import ResourceArbiter
from .write_gate import WriteGate


@dataclass
class PluginContext:
    plugin_id: str
    logger: logging.Logger
    data_store: DataStore
    resource_arbiter: ResourceArbiter
    write_gate: WriteGate
    #: 本应用的数据根目录（向量库/配置/缓存都应该落在这底下）。插件绝不
    #: 应该自己硬编码一个相对路径当数据目录——那样的插件在"从桌面快捷方式
    #: 启动、cwd 不是仓库根目录"这种真实场景下会把数据写到意料之外的地方，
    #: 或者压根找不到之前写的数据。AGENTS.md 架构红线7"所有数据落在 data/
    #: 目录下"说的就是这个目录，来源必须是 ctx，不是插件自己拼字符串。
    data_dir: Path
