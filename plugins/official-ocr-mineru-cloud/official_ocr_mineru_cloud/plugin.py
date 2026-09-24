"""official-ocr-mineru-cloud 插件：生命周期钩子的薄封装，真实逻辑在
extract.py/ocr.py。"""
from __future__ import annotations

import os
from pathlib import Path

from .extract import MineruCloudExtractor


class MineruCloudOcrPlugin:
    def __init__(self) -> None:
        self.extractor = MineruCloudExtractor()
        self._settings = None

    def on_load(self, ctx):
        self._settings = ctx.settings
        self.extractor = MineruCloudExtractor(
            pending_path=ctx.storage.file("mineru_pending.json", legacy="mineru_pending.json"),
            sidecar_dir=ctx.storage.directory("mineru_sidecars", legacy="mineru_sidecars"),
            quota_path=ctx.storage.file("mineru_quota.json", legacy="mineru_quota.json"),
        )
        ctx.logger.info("MinerU云端OCR已加载")

    def on_enable(self, ctx):
        ctx.logger.info("MinerU云端OCR已启用")

    def on_disable(self, ctx):
        ctx.logger.info("MinerU云端OCR已禁用")

    def on_unload(self, ctx):
        self._settings = None

    def is_active(self) -> bool:
        selected = self._settings.get("pdf_scan_backend", "none") if self._settings is not None else "none"
        return selected == "mineru-cloud"

    def index_signature(self) -> str:
        selected = self._settings.get("pdf_scan_backend", "none") if self._settings is not None else "none"
        key_state = "key" if os.environ.get("MINERU_API_KEY") else "nokey"
        return f"selected:{selected}:{key_state}"

    def extract(self, library_id: str, path: str, root: Path):
        return self.extractor.extract(library_id, path, root)
