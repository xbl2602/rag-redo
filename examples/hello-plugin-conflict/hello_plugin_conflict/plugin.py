"""Phase 0 示例插件：和 example-hello 声明同一个单例扩展点 demo_singleton。

用来演示"两个已启用插件同时声明同一单例点，核心必须显式报冲突、不能静默
选一个"——见 ../../../docs/ROADMAP.md Phase 0 验收标准第3条。
"""


class HelloPluginConflict:
    def on_load(self, ctx):
        ctx.logger.info("冲突演示插件已加载")

    def on_enable(self, ctx):
        ctx.logger.info("冲突演示插件已启用")

    def on_disable(self, ctx):
        ctx.logger.info("冲突演示插件已禁用")

    def on_unload(self, ctx):
        ctx.logger.info("冲突演示插件已卸载")
