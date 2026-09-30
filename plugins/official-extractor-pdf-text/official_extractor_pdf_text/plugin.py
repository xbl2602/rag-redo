"""official-extractor-pdf-text 插件：生命周期钩子的薄封装，真实逻辑在 extract.py。"""
from __future__ import annotations

from pathlib import Path

from .extract import DEFAULT_PDF_TEXT_MODE, EXTRACTOR_VERSION, PDF_TEXT_MODES, extract


class PdfTextExtractorPlugin:
    def __init__(self) -> None:
        self._settings = None
        self._logger = None

    def on_load(self, ctx):
        self._settings = ctx.settings
        self._logger = ctx.logger
        ctx.logger.info("PDF文字层提取器已加载")

    def on_enable(self, ctx):
        ctx.logger.info("PDF文字层提取器已启用")

    def on_disable(self, ctx):
        ctx.logger.info("PDF文字层提取器已禁用")

    def on_unload(self, ctx):
        pass

    def mode(self) -> str:
        """设置项 `pdf_text_mode`（含义见 extract.py 模块 docstring）；每次现读，改了设置不用重启。"""
        if self._settings is None:
            return DEFAULT_PDF_TEXT_MODE
        value = str(self._settings.get("pdf_text_mode", DEFAULT_PDF_TEXT_MODE) or "")
        return value if value in PDF_TEXT_MODES else DEFAULT_PDF_TEXT_MODE

    def index_signature(self) -> str:
        """能力签名：转换方式变了，此前转换失败的 PDF 下一轮按新方式重试一次（AGENTS.md §8.5）；
        已经转好的正文不因此作废——那只看插件代码版本（`plugin.toml`）。"""
        return f"{EXTRACTOR_VERSION}:{self.mode()}"

    def extract(self, library_id: str, path: str, root: Path):
        log = self._logger.info if self._logger is not None else None
        return extract(library_id, path, root, mode=self.mode(), log=log)
