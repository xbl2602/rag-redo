"""Phase 0 示例插件：on_enable 故意抛异常。

用来证明核心能把这种失败折叠成 FAILED 状态而不崩溃、不牵连其他插件——
见 ../../../docs/ROADMAP.md Phase 0 验收标准第2条、AGENTS.md 架构红线4。
"""


class BrokenPlugin:
    def on_load(self, ctx):
        ctx.logger.info("故障演示插件已加载")

    def on_enable(self, ctx):
        raise RuntimeError("故意炸的——用来验证核心的失败隔离")

    def on_disable(self, ctx):
        pass

    def on_unload(self, ctx):
        pass
