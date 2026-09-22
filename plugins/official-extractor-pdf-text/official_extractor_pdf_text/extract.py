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

EXTRACTOR_VERSION = "0.1.0"
PLUGIN_ID = "official-extractor-pdf-text"


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _has_text_layer(path: Path) -> bool:
    doc = pymupdf.open(path)
    try:
        return any(page.get_text().strip() for page in doc)
    finally:
        doc.close()


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
            return _fail(library_id, path, "scanned:no-text-layer——没有文字层，交给 OCR 插件处理", content_hash)
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
