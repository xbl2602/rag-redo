"""official-extractor-text 插件：生命周期钩子的薄封装，真实逻辑在 extract.py。"""
from __future__ import annotations

from pathlib import Path

from .extract import extract


class TextExtractorPlugin:
    def on_load(self, ctx):
        ctx.logger.info("文本提取器已加载")

    def on_enable(self, ctx):
        ctx.logger.info("文本提取器已启用")

    def on_disable(self, ctx):
        ctx.logger.info("文本提取器已禁用")

    def on_unload(self, ctx):
        pass

    def extract(self, library_id: str, path: str, root: Path):
        return extract(library_id, path, root)
