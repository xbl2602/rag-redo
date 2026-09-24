"""纯文本/Markdown 的提取逻辑：读文件 -> ExtractedDocument。

契约：extract() 绝不抛异常——任何失败都折叠成 ExtractedDocument(text=None,
failure_reason=...)，这是 docs/LESSONS.md 第1条"失败必须折叠成诚实的终态"
在最简单的提取器上的体现。连最简单的文本文件都要遵守这条纪律，作为其他
（更复杂的）extractor 插件的示范。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from core.contracts import ExtractedDocument

EXTRACTOR_VERSION = "0.1.0"


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract(library_id: str, path: str, root: Path) -> ExtractedDocument:
    full_path = root / path
    try:
        data = full_path.read_bytes()
    except OSError as exc:
        return ExtractedDocument(
            library_id=library_id,
            path=path,
            text=None,
            failure_reason=f"读取失败: {type(exc).__name__}: {exc}",
            extracted_by="official-extractor-text",
            extractor_version=EXTRACTOR_VERSION,
            content_hash="",
            failure_state="unreadable",
        )

    content_hash = _content_hash(data)
    try:
        # "utf-8-sig" 是 "utf-8" 的严格超集行为：有 BOM 就剥掉，没有 BOM 时
        # 解码结果和 "utf-8" 完全一样——用它一次到位，不要先试 "utf-8" 再
        # 试 "utf-8-sig"：bytes.decode("utf-8") 对带 BOM 的内容根本不会抛
        # UnicodeDecodeError，只会把 BOM 悄悄留在结果字符串开头，那样"失败
        # 才回退"的逻辑永远不会被触发，BOM 会一路带进索引污染检索。
        text = data.decode("utf-8-sig")
        # Windows 上 Obsidian/记事本等编辑器写出来的 .md 大概率是 \r\n
        # 换行——内容哈希用的是解码前的原始字节（不受影响），但 text 要
        # 统一成 \n，不然切块按行切分时每行末尾都带一个看不见的 \r，且
        # 同一篇笔记在 Windows/Linux 之间换行符不同会被误判成内容变了。
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        return ExtractedDocument(
            library_id=library_id,
            path=path,
            text=None,
            failure_reason=f"不是有效的 UTF-8 文本: {exc}",
            extracted_by="official-extractor-text",
            extractor_version=EXTRACTOR_VERSION,
            content_hash=content_hash,
            failure_state="extract-failed",
        )

    if not text.strip():
        return ExtractedDocument(
            library_id=library_id,
            path=path,
            text=None,
            failure_reason="文件为空或只有空白字符",
            extracted_by="official-extractor-text",
            extractor_version=EXTRACTOR_VERSION,
            content_hash=content_hash,
            failure_state="empty",
        )

    return ExtractedDocument(
        library_id=library_id,
        path=path,
        text=text,
        failure_reason=None,
        extracted_by="official-extractor-text",
        extractor_version=EXTRACTOR_VERSION,
        content_hash=content_hash,
    )
