"""切块策略：文本 -> 一组 (heading_breadcrumb, chunk_text)。

设计目标（对应旧 obsidian-rag 项目问题8"切块切在句子中间，语义被截断"这个
真实修过的 bug——本次是全新实现，不是复制旧代码，只是不重蹈同一类问题）：

1. 优先按 Markdown 标题分段，每个 chunk 带上完整的标题面包屑，方便检索
   结果展示"这段话出自哪个标题下"。
2. 段内按"块"（段落/表格/列表）累积到接近 max_chars 就切一刀，块内部不
   切开——表格、列表这类结构一旦从中间断开就不成形了。
3. 单个块本身超过 max_chars 时（长段落），才退化到句子边界切分，绝不在
   句子中间断开（中英文句末标点都识别）。
4. 同一标题段内的相邻 chunk 有少量重叠（overlap_chars），跨标题不重叠——
   重叠是为了避免"关键信息恰好卡在切块边界、两边都不完整"，跨标题重叠会
   把上一个话题的尾巴混进下一个话题，没有意义。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CHUNKER_VERSION = "0.2.0"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE_END_RE = re.compile(r"(?<=[。！？.!?])\s*")


@dataclass(frozen=True)
class ChunkPiece:
    heading_breadcrumb: str
    text: str
    section_id: str
    section_text: str


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_list_line(line: str) -> bool:
    return bool(re.match(r"^([-*+]|\d+[.)])\s", line.lstrip()))


def _split_blocks(lines: list[str]) -> list[str]:
    """把一段标题下的正文行分成若干"块"：连续的表格行是一块、连续的列表
    行是一块、普通段落（空行分隔）是一块。块内部后续绝不会被切开（除非
    单个块自己超过 max_chars，那是调用方另外处理的路径）。"""
    blocks: list[str] = []
    current: list[str] = []
    current_kind: str | None = None

    def flush() -> None:
        if current:
            blocks.append("\n".join(current))

    for line in lines:
        if not line.strip():
            flush()
            current.clear()
            current_kind = None  # noqa: F841 - 保持和上面对称，便于阅读
            continue
        kind = "table" if _is_table_line(line) else "list" if _is_list_line(line) else "para"
        if current and kind != current_kind:
            flush()
            current.clear()
        current.append(line)
        current_kind = kind
    flush()
    return blocks


def _split_long_block_by_sentence(block: str, max_chars: int) -> list[str]:
    sentences = [s for s in _SENTENCE_END_RE.split(block) if s]
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        pieces.append(current)
    return pieces or [block]


def chunk_document(text: str, *, max_chars: int = 800, overlap_chars: int = 100) -> list[ChunkPiece]:
    lines = text.splitlines()
    heading_stack: list[str] = []
    sections: list[tuple[str, str, str]] = []
    body: list[str] = []
    section_index = 0

    def breadcrumb() -> str:
        return " > ".join(heading_stack) if heading_stack else "(无标题)"

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            if body:
                sections.append((breadcrumb(), f"s{section_index}", "\n".join(body).strip()))
                section_index += 1
                body = []
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_stack = heading_stack[: level - 1] + [title]
        else:
            body.append(line)
    if body:
        sections.append((breadcrumb(), f"s{section_index}", "\n".join(body).strip()))

    pieces: list[ChunkPiece] = []
    for crumb, section_id, section_text in sections:
        body_lines = section_text.splitlines()
        blocks: list[str] = []
        for block in _split_blocks(body_lines):
            if len(block) > max_chars:
                blocks.extend(_split_long_block_by_sentence(block, max_chars))
            else:
                blocks.append(block)

        current = ""
        for block in blocks:
            if current and len(current) + 1 + len(block) > max_chars:
                pieces.append(
                    ChunkPiece(
                        heading_breadcrumb=crumb,
                        text=current,
                        section_id=section_id,
                        section_text=section_text,
                    )
                )
                tail = current[-overlap_chars:] if overlap_chars > 0 else ""
                current = f"{tail}\n{block}" if tail else block
            else:
                current = f"{current}\n{block}" if current else block
        if current:
            pieces.append(
                ChunkPiece(
                    heading_breadcrumb=crumb,
                    text=current,
                    section_id=section_id,
                    section_text=section_text,
                )
            )

    return pieces
