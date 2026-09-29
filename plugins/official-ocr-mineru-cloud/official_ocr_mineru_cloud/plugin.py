"""official-ocr-mineru-cloud 插件：生命周期钩子的薄封装，真实逻辑在
extract.py/ocr.py。"""
from __future__ import annotations

from pathlib import Path

from .extract import MineruCloudExtractor
from .ocr import resolve_api_key


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
            logger=ctx.logger,
            settings=ctx.settings,
        )
        ctx.logger.info("MinerU云端OCR已加载")

    def on_enable(self, ctx):
        ctx.logger.info("MinerU云端OCR已启用")

    def on_disable(self, ctx):
        ctx.logger.info("MinerU云端OCR已禁用")

    def on_unload(self, ctx):
        self._settings = None

    def reset_token_flag(self) -> None:
        """每轮索引开始的钩子（core/pipeline.py:541-547 对每个
        extractor:pdf 插件调用一次）。LEGACY 在同一个时点做两件事：
        obsidian-rag/index.py:1878 复位 Token 失效标志、index.py:2176-2177
        清理断点簿记孤儿。少任何一件都会留下"标志永不复位"或"簿记只增不减"
        的慢性病（前者让用户补好 Key 后重跑索引毫无反应）。"""
        self.extractor.reset_token_flag()
        if self.is_active():
            # 本轮不走云端段时别去动云端簿记（清理要逐条读盘算指纹，不值这份开销）
            self.extractor.prune_pending()

    def is_active(self) -> bool:
        selected = self._settings.get("pdf_scan_backend", "none") if self._settings is not None else "none"
        return selected == "mineru-cloud"

    def index_signature(self) -> str:
        selected = self._settings.get("pdf_scan_backend", "none") if self._settings is not None else "none"
        key_state = "key" if resolve_api_key(self._settings) else "nokey"
        return f"selected:{selected}:{key_state}"

    def extract(self, library_id: str, path: str, root: Path):
        return self.extractor.extract(library_id, path, root)
