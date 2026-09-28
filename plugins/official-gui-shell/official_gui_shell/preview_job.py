"""提取试验台的子进程入口（旧 `obsidian-rag/extractors.py::_preview_job` 的对应物）。

**为什么必须是独立进程**：试验台可能走云端 OCR（花钱、长轮询）或本地 MinerU/GPU。
只在守护线程里跑、"取消"只清状态的话，用户点了取消，云端任务和显存占用照样继续——
旧项目用 `multiprocessing` 子进程 + `terminate()` 就是为了让取消**真的能停**，
并且能给整个任务一个硬超时（180 秒）。这里同样：父进程拿着进程句柄，取消/超时
直接终止子进程。

子进程自己重建一个**只含提取器**的最小插件运行时（与索引 worker 的做法一致，
`core/index_progress.py::_index_worker`），跑一次 `Pipeline.preview_extract`——
该方法只读、不写提取缓存、不落 generation、不碰任何索引数据。

Windows 的 `spawn` 要求入口是模块级函数、参数可序列化，所以这里只接收字符串/字典/元组。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def preview_job(
    queue: Any,
    plugins_dir: str,
    data_dir: str,
    plugin_ids: tuple[str, ...],
    active_choices: dict[str, str],
    path: str,
    backend: str,
) -> None:
    """跑一次提取并把结果放进 `queue`。

    结果形状取自旧项目 `_preview_job` 的 payload：`{"ok", "error", "info": {md, reason,
    route, cached, elapsed, chars}}`。`ok` 表示"子进程正常交付了结果"，**不是**"提取
    出了内容"——管线级的失败（扫描件跳过、缺 Key、空文件）走 `info.reason`，前端据此
    如实显示"未产出 · 原因"，而不是把它误报成失败或"完成"。
    """
    from core.pipeline import Pipeline
    from core.runtime import PluginRuntime, PluginState

    runtime = None
    activated: list[str] = []
    try:
        runtime = PluginRuntime(Path(plugins_dir), state_file=None, data_dir=Path(data_dir))
        runtime.scan()
        for point, plugin_id in active_choices.items():
            runtime.registry.set_active(point, plugin_id)
        for plugin_id in sorted(plugin_ids):
            if plugin_id not in runtime.plugins:
                continue
            runtime.load(plugin_id)
            plugin = runtime.plugins[plugin_id]
            if plugin.instance is not None:
                activated.append(plugin_id)
            if plugin.state == PluginState.LOADED:
                runtime.enable(plugin_id)
        outcome = Pipeline(runtime).preview_extract(path, backend=backend or "")
        queue.put({
            "ok": True,
            "error": None,
            "info": {
                "md": outcome.markdown or "",
                "reason": "" if outcome.ok else (outcome.reason or "extract-failed"),
                "route": outcome.route,
                "cached": False,
                "elapsed": outcome.elapsed,
                "chars": outcome.chars,
            },
        })
    except Exception as exc:  # noqa: BLE001 - 子进程里的任何异常都折叠成结果，不能悄悄退出
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}", "info": {}})
    finally:
        if runtime is not None:
            for plugin_id in reversed(activated):
                try:
                    runtime.disable(plugin_id)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    runtime.unload(plugin_id)
                except Exception:  # noqa: BLE001
                    pass
