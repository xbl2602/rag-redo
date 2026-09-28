"""提取试验台生命周期测试用的子进程桩（不是测试文件）。

Windows 的 `spawn` 要求子进程入口是**可导入的模块级函数**，而测试模块本身的模块名
（`plugin_test__plugins__...`）子进程导不到——所以这几个桩放在独立的、只依赖标准库的
小模块里，子进程启动也快。它们与 `official_gui_shell.preview_job.preview_job` 同签名。
"""
from __future__ import annotations

import os
import time


def sleep_job(queue, plugins_dir, data_dir, plugin_ids, active_choices, path, backend):
    """模拟一个"卡住"的云端/GPU 提取：睡很久，永远不往队列放结果。"""
    time.sleep(600)


def crash_job(queue, plugins_dir, data_dir, plugin_ids, active_choices, path, backend):
    """模拟子进程崩溃：直接退出，没有任何结果。"""
    os._exit(3)
