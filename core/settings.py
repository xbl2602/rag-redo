"""core/settings.py — 通用插件设置存储（核心服务，不是插件）。

**为什么是核心服务而不是插件**：多个互不知情的插件（以及 `core/pipeline.py`
自己——它不是插件，没有 `ctx` 可用）都需要读写用户可调设置——RRF 两路
权重、`default_libraries`、MinerU 解释器覆盖路径……两个插件之间没法互相
询问"你的设置存哪了"，需要一个大家都认识的中立入口，同 GPU 仲裁/写权限
门禁"是否需要在互不知情的插件间做裁判"的理由一样（见
docs/ARCHITECTURE.md 2.1/2.2 节）。

**这次补的是哪个坑**：2026-09-23 全面功能审计对照 obsidian-rag/config.py
发现，`core/context.py::PluginContext` 此前只有 `data_dir`，完全没有
"用户可调、可持久化"的设置入口——RRF 权重写死在调用处、`default_libraries`
没法配、MinerU 解释器只能靠环境变量硬覆盖没有真正的设置入口……这些看似
分散的小缺口根子都在"没有这层"，见 docs/ROADMAP.md 该轮审计的说明。

**这不是照抄 obsidian-rag/config.py 的 `CFG` 全局字典**：obsidian-rag 把
全部约60个配置项的默认值集中写在一份 `DEFAULTS` 里，任何模块 `from
config import CFG` 就能读任意键——这在插件互相隔离、"同一件事只能在一处
定义"的架构下不合适（RRF 权重的默认值该由 `official-fusion-rrf`/
`core/pipeline.py` 自己决定和维护，不该让一个无关的核心模块替它们背书）。
这里反过来：`SettingsStore` 只提供"存一个具名值、读回来、活过重启"这个
通用能力，完全不知道、也不关心任何一个具体键的含义/类型/默认值——那些
由每个调用方自己在 `get(key, default)` 调用点声明（`default` 参数本身
就是"这个设置项属于谁、默认值是什么"的权威声明），同
`core/resource_arbiter.py`"不懂 GPU 是什么，只认具名资源租约"是同一个
哲学（见该模块 docstring）。

**类型安全**：`get()` 按调用方传入的 `default` 的类型校验磁盘上存的值——
类型不匹配就回退给 `default` 并记一条 warning，不是静默接受错误类型、
在很远的地方才炸出一个费解的 TypeError——对齐 obsidian-rag/config.py
`_coerce` 的教训（同一个理由，换成不需要预先声明全局 DEFAULTS 的写法）。

**为什么不路由进 `core/datastore.py::DataStore`**：设置存储是核心服务，
不是插件私有数据；DataStore 负责插件存储 handle 和跨插件契约权限，设置
仍由 SettingsStore 直接管理自身文件。规则见 ../AGENTS.md"两个核心组件"节。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any


def _type_compatible(value: Any, default: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return True  # default 是 None 或其他类型时不做类型收窄，交给调用方自己判断


class SettingsStore:
    """具名键值对，持久化到 `path` 指向的 JSON 文件。插件/core 通过
    `ctx.settings`（见 core/context.py）或直接持有这个实例来读写。"""

    def __init__(self, path: Path, *, logger: logging.Logger | None = None) -> None:
        self._path = path
        self._logger = logger if logger is not None else logging.getLogger("rag_redo.core.settings")
        self._lock = threading.Lock()
        self._values: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning("设置文件读取失败（%s），本次以空设置启动，不影响其他数据", exc)
            return {}
        if not isinstance(data, dict):
            self._logger.warning("设置文件顶层不是 JSON 对象，本次以空设置启动")
            return {}
        return data

    def get(self, key: str, default: Any = None) -> Any:
        """读一个设置项。`key` 不存在、或存的值类型和 `default` 对不上时
        都回退 `default`——调用方永远拿到"类型对的值"，不需要自己再校验
        一遍。"""
        with self._lock:
            if key not in self._values:
                return default
            value = self._values[key]
        if not _type_compatible(value, default):
            self._logger.warning(
                "设置项 %s 存储值类型异常（%r，期望类型同 %r），本次回退默认值", key, value, default
            )
            return default
        return value

    def set(self, key: str, value: Any) -> None:
        """写一个设置项并立即落盘——设置改动是低频、用户驱动的操作，不需要
        为写入频率做批量优化，简单直接优先。"""
        with self._lock:
            self._values[key] = value
            self._save_locked()

    def unset(self, key: str) -> None:
        """删掉一个设置项，恢复成"未设置"（调用方的 `get(key, default)`
        之后会拿回 default）——GUI 设置面板"恢复默认值"按钮用得上，幂等：
        key 本来就不存在也不报错。"""
        with self._lock:
            if key in self._values:
                del self._values[key]
                self._save_locked()

    def all(self) -> dict[str, Any]:
        """只读快照——GUI 设置面板列出当前全部已存值时用，返回的是拷贝，
        调用方改了不会污染这个 store 内部状态。"""
        with self._lock:
            return dict(self._values)

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._values, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            self._logger.warning("设置写盘失败（本次改动只在内存生效，进程重启后会丢失）：%s", exc)
