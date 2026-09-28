"""纯文本/Markdown 的提取逻辑：读文件 -> ExtractedDocument。

契约：extract() 绝不抛异常——任何失败都折叠成 ExtractedDocument(text=None,
failure_reason=...)，这是 docs/LESSONS.md 第1条"失败必须折叠成诚实的终态"
在最简单的提取器上的体现。连最简单的文本文件都要遵守这条纪律，作为其他
（更复杂的）extractor 插件的示范。

⚠️ 终态纪律（AGENTS.md §5 + BC-04）：**编码问题绝不能落终态**。
LEGACY 侧 `obsidian-rag/index.py:100` 是
`text = raw.decode("utf-8", errors="replace")`——永不抛错，坏字节换成
U+FFFD 后照常入库。REDO 曾用严格解码 + `failure_state="extract-failed"`，
让 GBK/Shift-JIS 等老编码的纯文本笔记彻底消失；更糟的是
`core/pipeline.py:687-701` 的 `stable_terminal` 会把它判成稳定终态，
**索引不会自愈**，用户必须自己转码。本文件的解码必须保持"永不失败"。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from core.contracts import ExtractedDocument

# 提取逻辑变了（非 UTF-8 降级 + 换行归一），必须 bump：pipeline 的提取缓存
# route 是 f"{extracted_by}:{extractor_version}"（core/pipeline.py:834/845/869），
# 版本一变旧缓存自然不再被当作当前提取器产物。
EXTRACTOR_VERSION = "0.2.0"

_logger = logging.getLogger("rag_redo.plugin.official-extractor-text")
_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    """同一类告警每进程只打一次（几百个 GBK 老文件不该刷几百行同样的提示）。

    沿用 LEGACY `obsidian-rag/extractors.py:150-155 _warn_once` 的做法：
    key 按"告警种类"而非按文件取，所以集合大小有界，不会随库增长。
    """
    if key in _warned:
        return
    _warned.add(key)
    _logger.warning(msg)


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

    # 内容哈希必须取「原始字节」——LEGACY `obsidian-rag/index.py:97` 的 bhash
    # 语义：二进制源（pdf/docx）也用它做指纹，kb_stale 据此实现"真没变就绝不
    # 跑转换"的零成本比对。降级解码不得影响这个指纹。
    content_hash = _content_hash(data)

    # ---- 解码：唯一一次、绝不失败的尝试（对齐 obsidian-rag/index.py:100）----
    #
    # `errors="replace"` 保证 UnicodeDecodeError 在这条路径上不可能发生，
    # 所以下面**不需要** try/except 兜底：非 UTF-8 内容（GBK 存档、跨设备同步
    # 留下的老编码、Shift-JIS 笔记）会被逐个坏字节换成 U+FFFD 后照常产出内容
    # ——"可搜、不会消失"，与 LEGACY 逐条一致。
    #
    # 两个有意偏离 LEGACY 的改进（都不是缺陷，需登记进行为契约）：
    #   1. `utf-8-sig`：有 BOM 就剥掉。LEGACY 用裸 "utf-8"，BOM 会以 U+FEFF
    #      留在结果串开头一路带进索引污染检索；而 bytes.decode("utf-8") 对带
    #      BOM 的内容**不会**抛 UnicodeDecodeError，所以"失败才回退"的写法在
    #      这里永远不触发。utf-8-sig 是 utf-8 的严格超集，无 BOM 时结果完全相同。
    #   2. 换行归一化 \r\n|\r -> \n：LEGACY 不做，Windows 上 Obsidian/记事本
    #      写出的 .md 每行都带一个看不见的 \r，会被切块按行切分时带进块文本；
    #      且同一篇笔记在 Windows/Linux 之间换行符不同会被误判成"内容变了"。
    #      这是用户可见的行为差异（块文本哈希不同 → 迁移后首轮全量重建），
    #      保留改进，但必须显式登记。
    text = data.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 替换字符只做**日志级**诊断，绝不改判成失败（AGENTS.md §5：终态必须
    # 可诊断，但"打不满的猜测"不是失败理由；LEGACY 也从不因此丢文件）。
    if "\ufffd" in text:
        bad = text.count("\ufffd")
        _warn_once(
            "non-utf8",
            f"检测到 {bad} 个 U+FFFD 替换字符，疑似非 UTF-8 编码（已按 LEGACY "
            f"行为降级入库、不落终态，样例：{path}）。如需修复请自行转码为 UTF-8。",
        )

    if not text.strip():
        # LEGACY `obsidian-rag/index.py:101`：text 若为空/纯空白归一为 None，
        # 上层落 `empty` 终态。注意 U+FFFD 不是空白字符，所以"全是坏字节"的
        # 文件在 LEGACY 里是**被索引**的（内容全是 U+FFFD），不是 empty。
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
