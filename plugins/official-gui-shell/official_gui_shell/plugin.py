"""official-gui-shell 插件：生命周期钩子很薄——真正"打开一个窗口"是仓库
根目录 gui_main.py（胶水脚本）的事，这个插件只负责声明"这个能力存在"以及
构造 Api（js_api 桥）、启动 1 秒状态推送线程供 gui_main.py 使用，理由同 official-mcp-server。
"""
from __future__ import annotations

from .api import Api
from .push import start_push_loop


class GuiShellPlugin:
    def on_load(self, ctx):
        ctx.logger.info("GUI壳已加载")

    def on_enable(self, ctx):
        ctx.logger.info("GUI壳已启用")

    def on_disable(self, ctx):
        ctx.logger.info("GUI壳已禁用")

    def on_unload(self, ctx):
        pass

    def make_api(self, pipeline, lib_mgr) -> Api:
        return Api(pipeline, lib_mgr)

    def start_push_loop(self, api: Api, window):
        """窗口创建并 `bind_window` 之后调用：起 1 秒状态推送线程，返回 (线程, 停止事件)。"""
        return start_push_loop(api, window)
