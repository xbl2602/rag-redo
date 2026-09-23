"""云端OCR的提取逻辑：读文件 -> 调云端API -> ExtractedDocument。

只在文字层提取器认输之后才会被链式尝试到（core/pipeline.py 的 _extract
按插件id字母序链式尝试同一个 extractor:pdf 点的全部provider——
official-extractor-pdf-text 排在 official-ocr-mineru-cloud 前面，见
../official-extractor-pdf-text/official_extractor_pdf_text/extract.py 的
"scanned:"约定）。这个提取器不需要、也不应该关心"自己是不是第二个被试
的"，只管"给我一个文件，我能不能OCR出文字"这一件事。

非PDF文件直接折叠成失败——理论上核心只会按扩展名调用registered的
extractor:pdf点，不该收到非PDF文件，但"extractor 绝不抛异常"这条纪律
要求即使输入不符合预期也要折叠而不是让 Path.suffix 之外的假设炸出
未预料的异常。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from core.contracts import ExtractedDocument

from .ocr import MineruCloudError, _RealHttpClient

EXTRACTOR_VERSION = "0.1.0"
PLUGIN_ID = "official-ocr-mineru-cloud"


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


class MineruCloudExtractor:
    """http_client 可注入——默认懒加载的 `_RealHttpClient`，测试传入假
    客户端（同 BGEM3Embedder(encoder=...) 的构造注入模式，不需要另起一套
    机制）。"""

    def __init__(self, http_client=None) -> None:
        self._client = http_client if http_client is not None else _RealHttpClient()

    def extract(self, library_id: str, path: str, root: Path) -> ExtractedDocument:
        full_path = root / path
        if full_path.suffix.lower() != ".pdf":
            return _fail(library_id, path, "不是PDF，云端OCR跳过")

        try:
            data = full_path.read_bytes()
        except OSError as exc:
            return _fail(library_id, path, f"读取失败: {type(exc).__name__}: {exc}")

        content_hash = _content_hash(data)

        try:
            text = self._client.ocr(data, full_path.name)
        except MineruCloudError as exc:
            return _fail(library_id, path, f"云端OCR失败: {exc}", content_hash)
        except Exception as exc:  # noqa: BLE001 - extractor 绝不抛异常，见旧项目教训
            return _fail(library_id, path, f"云端OCR失败(未分类异常): {type(exc).__name__}", content_hash)

        if not text.strip():
            return _fail(library_id, path, "OCR结果为空", content_hash)

        return ExtractedDocument(
            library_id=library_id,
            path=path,
            text=text,
            failure_reason=None,
            extracted_by=PLUGIN_ID,
            extractor_version=EXTRACTOR_VERSION,
            content_hash=content_hash,
        )
