"""文字层 PDF 的提取逻辑：读文件 -> ExtractedDocument。

只处理有文字层的 PDF（`pymupdf4llm.to_markdown`）；完全没有文字层的
（扫描件/图片版）折叠成 failure_reason="scanned:no-text-layer"，诚实
承认"这次没收"而不是返回空文本假装成功——继承旧项目"图片版 PDF 没开
识别时会先诚实记下'这次没收'"的原则。真正的 OCR 是另一个 extractor 插件
（official-ocr-mineru-*，Phase 2）的事，两者靠 failure_reason 里的
"scanned:"前缀分流，不是这个插件自己做 OCR。

pymupdf/pymupdf4llm 可能因为各种损坏/加密 PDF 抛出五花八门的异常类型，
这里用宽 except Exception 兜底折叠——契约是"extractor 绝不抛异常"，不是
"只处理我预料到的异常类型"。

**两种转换方式（2026-09-30 操作者确认，BC-01，设置项 `pdf_text_mode`）**：装了
pymupdf-layout 时 `pymupdf4llm.to_markdown` 默认每页跑一遍 AI 版面分析（onnx 模型）——
标题层级、表格、图里的文字都认得最好，但真机每页 0.25~0.5 秒、占满全部 CPU 核
（Y2S1 这种 7600 页的库首次转换估计 40~55 分钟，用户看到的"转换阶段 CPU 尖峰、显卡
闲着"就是它）。旧项目与本项目此前一直是这种。"快速"方式直接调经典规则式转换
（`pymupdf_rag.to_markdown`，不认表格、不分析图形）：讲义/幻灯片每页约 6~9 毫秒
（快约 30 倍），厚教材快约 6 倍，只用 1~2 个核；代价是标题层级偶尔认错（把正文当
标题）、图片里的文字不收、表格变成普通文字行。默认 `auto`：超过 `FAST_MODE_MIN_PAGES`
页的大文件用快速方式，其余照旧。

快速方式**直接调 `pymupdf_rag`，不切 `pymupdf4llm.use_layout()`**：那是进程级全局开关，
同一进程里另一份文件正在按版面分析转换时切它会串台；而 `table_strategy=None` 保证
经典转换不会走到 pymupdf 里唯一还会调版面模型的找表格那一步。

两种方式产出的 `extractor_version` 相同：提取缓存按"插件:版本"找正文，模式写进版本号
会让快速方式转好的正文下一轮找不到。切换模式只影响**之后需要转换**的 PDF（新加的、
改过的、失败待重试的），已经转好的不会被重转——模式进插件的能力签名，所以此前
转换失败的文件会在下一轮按新模式重试一次（AGENTS.md §8.5）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from typing import Callable

import pymupdf
import pymupdf4llm
from pymupdf4llm.helpers import pymupdf_rag

from core.contracts import ExtractedDocument

EXTRACTOR_VERSION = "0.3.0"
_TEXT_PAGE_MIN_CHARS = 10
#: 每一页文字都完全相同、且不超过这么多字符，就是水印/印章（如扫描 App 盖在每页的
#: "CamScanner"），不是正文。见 `_is_repeated_watermark`。
_WATERMARK_MAX_CHARS = 40
PLUGIN_ID = "official-extractor-pdf-text"

#: 设置项 `pdf_text_mode` 的取值（含义见模块 docstring）。
PDF_TEXT_MODES = ("auto", "layout", "fast")
DEFAULT_PDF_TEXT_MODE = "auto"
#: `auto` 下超过这么多页的 PDF 用快速方式（操作者确认的折中：厚教材才值得牺牲一点结构）。
FAST_MODE_MIN_PAGES = 200
#: 快速方式的参数：不找表格（找表格会调版面模型，也是经典转换里最慢的一步）、不分析矢量图形、
#: 不管图片——只要文字和按字号判断的标题。
_FAST_OPTIONS = {"table_strategy": None, "ignore_graphics": True, "ignore_images": True, "show_progress": False}


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_repeated_watermark(page_texts: list[str]) -> bool:
    """每一页的文字（折叠空白后）都是同一句短话 -> 水印，不是正文。

    2026-09-29 操作者真机反馈 + 确认的新规则（BC-01，**旧项目没有**）：扫描 App
    （如 CamScanner）会在每页盖一个文字水印，恰好达到"每页 >= 10 字符"的文字页门槛
    （"CamScanner" 正好 10 个字符），整份扫描件被当成"文字层 PDF"，转出来只有水印，
    切块清洗后为空，记成终态 empty，OCR 从未被调用。

    只在至少两页、且全部页面折叠空白后完全相同、长度不超过 `_WATERMARK_MAX_CHARS` 时
    才判水印：只有一页时没有"每一页都一样"的证据；页面文字里除水印外还有各不相同的正文，
    或者重复的是一整段长文，都仍按文字层处理。"""
    if len(page_texts) < 2:
        return False
    distinct = {" ".join(text.split()) for text in page_texts}
    return len(distinct) == 1 and len(next(iter(distinct))) <= _WATERMARK_MAX_CHARS


def _inspect_text_layer(path: Path) -> tuple[bool, int]:
    """（有没有文字层, 页数）——页数顺带给转换方式用，免得再打开一次。"""
    doc = pymupdf.open(path)
    try:
        page_texts = [page.get_text("text").strip() for page in doc]
    finally:
        doc.close()
    if not all(len(text) >= _TEXT_PAGE_MIN_CHARS for text in page_texts):
        return False, len(page_texts)
    return not _is_repeated_watermark(page_texts), len(page_texts)


def _has_text_layer(path: Path) -> bool:
    return _inspect_text_layer(path)[0]


def uses_fast_mode(mode: str, pages: int) -> bool:
    """这份 PDF 按设置该不该用快速方式转换（未知取值按默认 auto 处理）。"""
    if mode not in PDF_TEXT_MODES:
        mode = DEFAULT_PDF_TEXT_MODE
    if mode == "fast":
        return True
    if mode == "layout":
        return False
    return pages > FAST_MODE_MIN_PAGES


def _fail(library_id: str, path: str, reason: str, content_hash: str = "") -> ExtractedDocument:
    return ExtractedDocument(
        library_id=library_id,
        path=path,
        text=None,
        failure_reason=reason,
        extracted_by=PLUGIN_ID,
        extractor_version=EXTRACTOR_VERSION,
        content_hash=content_hash,
    )


def extract(
    library_id: str,
    path: str,
    root: Path,
    *,
    mode: str = DEFAULT_PDF_TEXT_MODE,
    log: Callable[[str], None] | None = None,
) -> ExtractedDocument:
    full_path = root / path
    try:
        data = full_path.read_bytes()
    except OSError as exc:
        return _fail(library_id, path, f"读取失败: {type(exc).__name__}: {exc}")

    content_hash = _content_hash(data)

    try:
        has_text, pages = _inspect_text_layer(full_path)
        if not has_text:
            return _fail(library_id, path, "scanned", content_hash)
        if uses_fast_mode(mode, pages):
            if log is not None:
                log(f"PDF 用快速方式转换（{pages} 页，模式 {mode}）：{path}")
            text = pymupdf_rag.to_markdown(str(full_path), **_FAST_OPTIONS)
        else:
            text = pymupdf4llm.to_markdown(str(full_path))
    except Exception as exc:  # noqa: BLE001 - extractor 绝不抛异常，见模块 docstring
        return _fail(library_id, path, f"提取失败: {type(exc).__name__}: {exc}", content_hash)

    if not text.strip():
        return _fail(library_id, path, "提取结果为空", content_hash)

    return ExtractedDocument(
        library_id=library_id,
        path=path,
        text=text,
        failure_reason=None,
        extracted_by=PLUGIN_ID,
        extractor_version=EXTRACTOR_VERSION,
        content_hash=content_hash,
    )
