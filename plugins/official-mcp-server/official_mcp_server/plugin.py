"""official-mcp-server 插件：生命周期钩子很薄——这个插件本身不在核心
runtime 里"运行"什么持续服务，它只是把"注册哪些MCP工具"这件事声明成一个
插件（呼应 AGENTS.md"MCP也是插件"的确认决定）。真正把这些工具跑起来、
对外提供 stdio 服务的入口在仓库根目录 mcp_stdio.py。
"""
from __future__ import annotations

from .tools import register_tools


class McpServerPlugin:
    def on_load(self, ctx):
        ctx.logger.info("MCP工具注册器已加载")

    def on_enable(self, ctx):
        ctx.logger.info("MCP工具注册器已启用")

    def on_disable(self, ctx):
        ctx.logger.info("MCP工具注册器已禁用")

    def on_unload(self, ctx):
        pass

    def register_tools(self, server, pipeline, lib_mgr) -> None:
        register_tools(server, pipeline, lib_mgr)
