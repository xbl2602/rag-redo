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
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pymupdf4llm

from core.contracts import ExtractedDocument

EXTRACTOR_VERSION = "0.3.0"
_TEXT_PAGE_MIN_CHARS = 10
#: 每一页文字都完全相同、且不超过这么多字符，就是水印/印章（如扫描 App 盖在每页的
#: "CamScanner"），不是正文。见 `_is_repeated_watermark`。
_WATERMARK_MAX_CHARS = 40
PLUGIN_ID = "official-extractor-pdf-text"


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


def _has_text_layer(path: Path) -> bool:
    doc = pymupdf.open(path)
    try:
        page_texts = [page.get_text("text").strip() for page in doc]
    finally:
        doc.close()
    if not all(len(text) >= _TEXT_PAGE_MIN_CHARS for text in page_texts):
        return False
    return not _is_repeated_watermark(page_texts)


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


def extract(library_id: str, path: str, root: Path) -> ExtractedDocument:
    full_path = root / path
    try:
        data = full_path.read_bytes()
    except OSError as exc:
        return _fail(library_id, path, f"读取失败: {type(exc).__name__}: {exc}")

    content_hash = _content_hash(data)

    try:
        if not _has_text_layer(full_path):
            return _fail(library_id, path, "scanned", content_hash)
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
