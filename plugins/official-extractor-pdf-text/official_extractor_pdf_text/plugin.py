"""official-extractor-pdf-text 插件：生命周期钩子的薄封装，真实逻辑在 extract.py。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .ahead import AheadConverter
from .extract import DEFAULT_PDF_TEXT_MODE, EXTRACTOR_VERSION, PDF_TEXT_MODES, extract
from .pages import DEFAULT_IMAGE_PAGE_RULE, IMAGE_PAGE_RULES
from .pages import write_page_range as _write_page_range


def output_settings_text(mode: str, image_rule: str) -> str:
    """`output_settings()` 的格式：两个取值都只含字母和短横线，拼起来不含 `:` 和 `+`
    （编排层拿它当转换暂存路由的一部分，见 core/contracts.py）。"""
    return f"mode={mode};pages={image_rule}"


class PdfTextExtractorPlugin:
    def __init__(self) -> None:
        self._settings = None
        self._logger = None
        self._ahead = AheadConverter()

    def on_load(self, ctx):
        self._settings = ctx.settings
        self._logger = ctx.logger
        ctx.logger.info("PDF文字层提取器已加载")

    def on_enable(self, ctx):
        ctx.logger.info("PDF文字层提取器已启用")

    def on_disable(self, ctx):
        self._ahead.close()
        ctx.logger.info("PDF文字层提取器已禁用")

    def on_unload(self, ctx):
        self._ahead.close()

    def mode(self) -> str:
        """设置项 `pdf_text_mode`（含义见 extract.py 模块 docstring）；每次现读，改了设置不用重启。"""
        if self._settings is None:
            return DEFAULT_PDF_TEXT_MODE
        value = str(self._settings.get("pdf_text_mode", DEFAULT_PDF_TEXT_MODE) or "")
        return value if value in PDF_TEXT_MODES else DEFAULT_PDF_TEXT_MODE

    def image_rule(self) -> str:
        """设置项 `pdf_image_page_rule`：哪些页算图片页（含义见 pages.py）；每次现读。"""
        if self._settings is None:
            return DEFAULT_IMAGE_PAGE_RULE
        value = str(self._settings.get("pdf_image_page_rule", DEFAULT_IMAGE_PAGE_RULE) or "")
        return value if value in IMAGE_PAGE_RULES else DEFAULT_IMAGE_PAGE_RULE

    def index_signature(self) -> str:
        """能力签名：转换方式、图片页的判法变了，此前转换失败的 PDF、以及有图片页没识别的 PDF
        下一轮按新设置重试一次（AGENTS.md §8.5）。已经转好的那些由 `output_settings()` 管。"""
        return f"{EXTRACTOR_VERSION}:{self.mode()}:{self.image_rule()}"

    def output_settings(self) -> str:
        """会改变转出来的正文的设置：转换方式、哪些页算图片页。

        2026-10-01 操作者确认（BC-01）：改了这两项，下一轮把已经用文字层转好的 PDF 全按新设置
        重转——此前只对之后新转的 PDF 生效，真机上改完设置看不出任何变化，像是设置没生效。
        整本扫描件不是这个插件转的，不受影响。"""
        return output_settings_text(self.mode(), self.image_rule())

    def extract(self, library_id: str, path: str, root: Path):
        log = self._logger.info if self._logger is not None else None
        # 只读一次设置：转换用的和记下来的必须是同一份（转到一半用户改了设置也不会记错）
        mode, image_rule = self.mode(), self.image_rule()
        doc = None
        job = self._ahead.take(library_id, path, Path(root), mode, image_rule)
        if job is not None:
            try:
                doc, lines = job.result()
            except Exception as exc:  # noqa: BLE001 - 提前转的子进程出事了：在本进程里照常转一遍
                if log is not None:
                    log(f"提前转换没成（{type(exc).__name__}），改在本进程转：{path}")
            else:
                for line in lines:
                    if log is not None:
                        log(line)
        if doc is None:
            doc = extract(library_id, path, root, mode=mode, image_rule=image_rule, log=log)
        if doc.text is None:
            return doc
        return replace(doc, extractor_settings=output_settings_text(mode, image_rule))

    # ---- 几本同时转（2026-10-01，BC-01；接口说明见 core/contracts.py，实现见 ahead.py）----

    def prefetch(self, library_id: str, paths: list[str], root: Path) -> list[str]:
        """编排层告诉它接下来要转哪几本：会走快速方式的在后台几本同时转，返回收下的那些。"""
        return self._ahead.submit(library_id, list(paths), Path(root), self.mode(), self.image_rule())

    def prefetch_wait(self, library_id: str, path: str, timeout: float) -> bool:
        return self._ahead.wait(library_id, path, timeout)

    def prefetch_ready(self, library_id: str) -> int:
        return self._ahead.ready(library_id)

    def prefetch_cancel(self, library_id: str, paths: list[str] | None = None) -> None:
        self._ahead.cancel(library_id, paths)

    def write_page_range(self, src: Path, first: int, last: int, dest: Path) -> None:
        """把第 first～last 页另存成一份 PDF（编排层把图片页切出来送识别，BC-01）。"""
        _write_page_range(Path(src), int(first), int(last), Path(dest))
