"""Phase 0 示例插件：什么都不做，只演示生命周期钩子怎么被核心调用。

见 ../../../docs/ROADMAP.md Phase 0 验收标准第1条。
"""


class HelloPlugin:
    def on_load(self, ctx):
        ctx.logger.info("hello 插件已加载")

    def on_enable(self, ctx):
        ctx.data_store.write(ctx.plugin_id, "hello.greeting", "你好，插件系统！", public=True)
        ctx.logger.info("hello 插件已启用")

    def on_disable(self, ctx):
        ctx.logger.info("hello 插件已禁用")

    def on_unload(self, ctx):
        ctx.logger.info("hello 插件已卸载")
