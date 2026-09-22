"""official-fusion-rrf 插件：生命周期钩子的薄封装，真实逻辑在 rrf.py。"""
from __future__ import annotations

from .rrf import reciprocal_rank_fusion


class FusionRRFPlugin:
    def on_load(self, ctx):
        ctx.logger.info("RRF融合已加载")

    def on_enable(self, ctx):
        ctx.logger.info("RRF融合已启用")

    def on_disable(self, ctx):
        ctx.logger.info("RRF融合已禁用")

    def on_unload(self, ctx):
        pass

    def fuse(self, ranked_lists: list[list[str]], weights: list[float] | None = None) -> list[tuple[str, float]]:
        return reciprocal_rank_fusion(ranked_lists, weights=weights)
