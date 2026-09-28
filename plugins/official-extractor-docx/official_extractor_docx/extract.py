"""DOCX 的提取逻辑：读文件 -> ExtractedDocument（结构化转 Markdown）。

标题样式（Heading 1/2/3...）转成对应级别的 `#`，普通段落原样输出，表格
转成 Markdown 管道表格——这样切块阶段（official-chunker）能复用同一套
"按标题分段、表格不拆开"的逻辑，不用为 DOCX 来源的文档另写一套处理。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import docx

from core.contracts import ExtractedDocument

# 标题识别规则变了（补齐 LEGACY 的 Title/中文/钳级），必须 bump：pipeline 的
# 提取缓存 route 是 f"{extracted_by}:{extractor_version}"（core/pipeline.py:834）。
EXTRACTOR_VERSION = "0.2.0"
PLUGIN_ID = "official-extractor-docx"

# 英文模板 `Heading 2` 与中文 Word 模板 `标题 2` 都要认；标题与数字之间允许
# 空白（`标题2` 也命中）。逐条对齐 obsidian-rag/extractors.py:432。
_HEADING_STYLE_RE = re.compile(r"^(?:Heading|标题)\s*(\d+)$", re.IGNORECASE)


def _heading_level(style_name: str | None) -> int:
    r"""样式名 → 标题级别（1-3）；非标题样式返回 0。

    逐条照抄 LEGACY `obsidian-rag/extractors.py:420-435 _heading_level`：
      - `Title`（不分大小写、去空白后比较）→ H1；
      - `Heading N` 或中文 `标题 N`（不分大小写）→ N，再**钳到 3 级**；
      - 其余 → 0（当普通正文原样输出）。

    钳到 3 级不是偷懒，是硬约束：切块器
    `official-chunker/chunk.py:31 _HEADING_RE = ^(#{1,3})\s+` 只认 H1-H3，
    输出 `####` 会被当成普通正文行——那样 Word 里 4~6 级标题就彻底失去
    heading_breadcrumb 语义锚点。
    """
    if not style_name:
        return 0
    name = style_name.strip()
    if name.lower() == "title":
        return 1
    m = _HEADING_STYLE_RE.match(name)
    if not m:
        return 0
    return max(1, min(3, int(m.group(1))))


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fail(
    library_id: str,
    path: str,
    reason: str,
    content_hash: str = "",
    state: str | None = None,
) -> ExtractedDocument:
    """折叠失败结果。

    `state` 必须显式给出，不能让上层靠 reason 文本猜：AGENTS.md §5 要求
    `unreadable` / `empty` / `extract-failed` 不能合并成一个无法诊断的
    `failed`（`core/pipeline.py::normalize_failure_state` 对不认识的前缀一律
    归到 `extract-failed`）。取值语义照抄 LEGACY：
      - 读不到字节 → `unreadable`（`obsidian-rag/index.py:118`）
      - docx 打开/遍历失败 → `extract-failed`（`obsidian-rag/extractors.py:475,498`）
      - 遍历成功但没有内容 → `empty`（`obsidian-rag/extractors.py:501`）
    """
    return ExtractedDocument(
        library_id=library_id,
        path=path,
        text=None,
        failure_reason=reason,
        extracted_by=PLUGIN_ID,
        extractor_version=EXTRACTOR_VERSION,
        content_hash=content_hash,
        failure_state=state,
    )


def _table_to_markdown(table: "docx.table.Table") -> str:
    rows = [[cell.text.strip().replace("\n", " ") for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join("---" for _ in rows[0]) + " |"]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _document_to_markdown(document: "docx.Document") -> str:
    """按 body 的实际元素顺序遍历段落和表格，不能简单先取全部段落再取
    全部表格——那样会打乱"表格出现在哪两段文字之间"的原始顺序。"""
    from docx.oxml.ns import qn  # noqa: PLC0415 - 局部import，避免给模块顶层加不必要的内部符号依赖

    parts: list[str] = []
    body = document.element.body
    paragraphs_by_id = {p._p: p for p in document.paragraphs}
    tables_by_id = {t._tbl: t for t in document.tables}

    for child in body.iterchildren():
        if child.tag == qn("w:p") and child in paragraphs_by_id:
            para = paragraphs_by_id[child]
            text = para.text.strip()
            if not text:
                continue
            # 样式名读取本身可能抛（损坏的 styles part），照抄 LEGACY
            # obsidian-rag/extractors.py:485-488 的兜底：拿不到就当普通正文，
            # 绝不让单个段落的样式异常把整篇文档变成提取失败。
            try:
                style_name = para.style.name if para.style is not None else ""
            except Exception:  # noqa: BLE001 - 提取器绝不因样式异常抛异常
                style_name = ""
            level = _heading_level(style_name)
            parts.append(f"{'#' * level} {text}" if level else text)
        elif child.tag == qn("w:tbl") and child in tables_by_id:
            md_table = _table_to_markdown(tables_by_id[child])
            if md_table:
                parts.append(md_table)

    return "\n\n".join(parts)


def extract(library_id: str, path: str, root: Path) -> ExtractedDocument:
    full_path = root / path
    try:
        data = full_path.read_bytes()
    except OSError as exc:
        return _fail(
            library_id, path, f"读取失败: {type(exc).__name__}: {exc}", state="unreadable"
        )

    content_hash = _content_hash(data)

    # python-docx 读 zip 时是整包读进内存（`docx.opc.phys_pkg.PhysPkgReader`
    # 收 blob），不持有 OS 文件句柄，所以这里**不需要** finally/close 收尾
    # ——和 LEGACY `obsidian-rag/extractors.py:460-502` 一样开完即用。
    # 真要加清理也必须 `try/except: pass` 包一层，否则 finally 里抛的异常会
    # 覆盖下面 except 分支已产生的返回值并继续外泄，"绝不抛异常"就废了。
    try:
        document = docx.Document(str(full_path))
        text = _document_to_markdown(document)
    except Exception as exc:  # noqa: BLE001 - extractor 绝不抛异常
        return _fail(
            library_id,
            path,
            f"提取失败: {type(exc).__name__}: {exc}",
            content_hash,
            state="extract-failed",
        )

    if not text.strip():
        return _fail(library_id, path, "提取结果为空", content_hash, state="empty")

    return ExtractedDocument(
        library_id=library_id,
        path=path,
        text=text,
        failure_reason=None,
        extracted_by=PLUGIN_ID,
        extractor_version=EXTRACTOR_VERSION,
        content_hash=content_hash,
    )
