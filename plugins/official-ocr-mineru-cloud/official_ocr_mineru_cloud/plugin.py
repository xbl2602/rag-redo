"""official-ocr-mineru-cloud 插件：生命周期钩子的薄封装，真实逻辑在
extract.py/ocr.py。"""
from __future__ import annotations

from pathlib import Path

from .extract import MineruCloudExtractor


class MineruCloudOcrPlugin:
    def __init__(self) -> None:
        self.extractor = MineruCloudExtractor()

    def on_load(self, ctx):
        ctx.logger.info("MinerU云端OCR已加载")

    def on_enable(self, ctx):
        ctx.logger.info("MinerU云端OCR已启用")

    def on_disable(self, ctx):
        ctx.logger.info("MinerU云端OCR已禁用")

    def on_unload(self, ctx):
        pass

    def extract(self, library_id: str, path: str, root: Path):
        return self.extractor.extract(library_id, path, root)
