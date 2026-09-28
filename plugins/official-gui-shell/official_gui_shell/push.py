"""1 秒状态推送线程（旧 `obsidian-rag/guiweb/app.py::_push_loop` 的逐项移植）。

前端每秒靠它收到 `snapshot` 推送（KPI、进度条、心跳、门锁全靠这条通道）；没有它，
窗口就是"有界面、无数据"：只有用户主动点按钮触发的直接 API 调用才有反应。此前
`gui_main.py` 完全没有起这个线程，进度条永远停在启动那一刻。

**容错口径**（旧项目问题47 附记）：不能"任意一次异常就永久 break"——页面加载期一次
`evaluate_js` 失败（窗口还没就绪）就会终结此后全部推送。改为**连续 5 次失败才
退出**；窗口真关了 `evaluate_js` 会持续失败，照样能退出，不泄漏线程。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

#: 推送周期与失败容忍，固定值来自 `tests/fixtures/legacy_guiweb_contract.json`。
PUSH_INTERVAL_S = 1.0
PUSH_FAILURE_TOLERANCE = 5

_LOG = logging.getLogger("rag_redo.plugin.official-gui-shell.push")


def push_loop(
    api: Any,
    window: Any,
    stop: threading.Event,
    *,
    interval: float = PUSH_INTERVAL_S,
    tolerance: int = PUSH_FAILURE_TOLERANCE,
) -> None:
    """周期性把 `api.get_snapshot()` 推给前端，直到 `stop` 被置位或连续失败超限。

    快照自己失败时 `get_snapshot()` 返回 `{"error": ...}`（不抛异常）——这一轮跳过、
    不算推送失败，下一秒再来（旧行为）。
    """
    failures = 0
    while not stop.is_set():
        try:
            snapshot = api.get_snapshot()
            if "error" not in snapshot:
                window.evaluate_js(
                    "window.__push && window.__push('snapshot', %s)"
                    % json.dumps(snapshot, ensure_ascii=False)
                )
            failures = 0
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring：宁可多试几次
            failures += 1
            _LOG.warning("推送失败×%d（窗口关闭会持续失败）：%s", failures, exc)
            if failures >= tolerance:
                break
        stop.wait(interval)


def start_push_loop(
    api: Any,
    window: Any,
    *,
    interval: float = PUSH_INTERVAL_S,
    tolerance: int = PUSH_FAILURE_TOLERANCE,
) -> tuple[threading.Thread, threading.Event]:
    """起后台守护线程；返回 (线程, 停止事件)。调用方在窗口关闭后 `stop.set()`。"""
    stop = threading.Event()
    thread = threading.Thread(
        target=push_loop,
        args=(api, window, stop),
        kwargs={"interval": interval, "tolerance": tolerance},
        daemon=True,
        name="gui-push",
    )
    thread.start()
    return thread, stop
