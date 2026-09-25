"""切块策略：文本 -> 一组 (heading_breadcrumb, chunk_text)。

对齐 obsidian-rag/index.py 的两级切块管线（问题8/9/18、审计 F8 的既有决策），
语义逐条对应旧实现 `_store_chunks` + `split_by_headings`/`split_paragraphs`/
`is_list_block`/`split_list_block`/`split_sentences`：

1. 标题切分只认 H1-H3（旧 heading_re `#{1,3}`——H4-H6 是正文，不是分节点），
   维护嵌套标题路径；``` / ~~~ 围栏代码块内的 `#` 行不算标题（审计 F8：
   代码注释混进嵌入文本的教训）。
2. 表格"宁大勿断"：表格段并入直接上文、结尾段吞入下一段（整组绑定上下文）；
   含表格的超长段整体保留，绝不在表格行中间切断。
3. 连续列表块跨空行合并；超长列表按列表项边界切（永不从列表项中间剪断）。
4. 普通长段按句子边界切：中文句末标点（。！？）；英文 .!? 后必须紧跟空格 +
   大写/数字（避免 Mr./e.g./3.14 被误切），常见缩写先保护再切；单句超长宁长
   勿断。
5. 无 overlap——旧项目没有跨块重叠（重叠会把上一话题的尾巴混进下一话题，
   且违反"边界即语义边界"的设计）。

面包屑分隔符用 " / "（对齐旧 split_by_headings 的输出格式）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CHUNKER_VERSION = "0.3.0"

MAX_CHARS = 600  # 对齐旧 config.py::chunk_char_limit 默认值（问题22-F17 之前的基础值）
SHORT_DOC_CHAR_LIMIT = 200  # 对齐旧 config.py::short_doc_char_limit

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])\s*|(?<=[.!?])\s+(?=[A-Z0-9])")
_ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "prof.", "e.g.", "i.e.", "etc.", "vs.", "st.", "no.", "al."}


@dataclass(frozen=True)
class ChunkPiece:
    heading_breadcrumb: str
    text: str
    section_id: str
    section_text: str


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def is_list_block(text: str) -> bool:
    """段落整体是否为列表体：非空行中列表项行占 ≥ 一半（旧 is_list_block，
    单行列表项也算列表体——列表项常因空行被拆成单行段落，需跨段落合并）。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    items = sum(1 for line in lines if _LIST_ITEM_RE.match(line))
    return items >= 1 and items * 2 >= len(lines)


def split_paragraphs(text: str) -> list[str]:
    """按空行切段落，去首尾空白（旧 split_paragraphs）。"""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def split_sentences(text: str, max_len: int) -> list[str]:
    """按句子边界切块，永不从句子中间剪断（旧 split_sentences）。

    英文边界要求 .!? 后紧跟空格+大写/数字；常见缩写先保护再切；单句超长
    宁长勿断；还原被消费的分隔空格避免 "dollars.This" 粘连。"""
    protected = text
    for abbr in list(_ABBREVIATIONS) + [a.capitalize() for a in _ABBREVIATIONS]:
        protected = protected.replace(" " + abbr, " " + abbr.replace(".", "\x00"))
    parts = [p.replace("\x00", ".") for p in _SENTENCE_SPLIT_RE.split(protected)]
    chunks: list[str] = []
    current = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if current and len(current) + len(part) > max_len:
            chunks.append(current)
            current = ""
        current += part
        current += " "
    if current:
        chunks.append(current.strip())
    return chunks


def split_list_block(text: str, max_len: int) -> list[str]:
    """按列表项边界切分列表块：新块只在列表项行处开启，非列表行跟随当前项
    （旧 split_list_block）。"""
    chunks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            current.append(line)
            continue
        if _LIST_ITEM_RE.match(line) and current and len("\n".join(current)) + len(line) > max_len:
            chunks.append("\n".join(current).strip())
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    return [c for c in chunks if c]


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按 H1-H3 标题切大段，维护嵌套标题路径（旧 split_by_headings，
    含围栏内 `#` 不算标题的 F8 修复）。返回 [(heading_path, body)]。"""
    chunks: list[tuple[str, str]] = []
    path: list[tuple[int, str]] = []
    current_lines: list[str] = []
    fence_char = ""
    fence_len = 0

    def flush() -> None:
        if current_lines:
            body = "\n".join(current_lines).strip()
            if body:
                chunks.append((" / ".join(t for _, t in path), body))

    for line in text.splitlines():
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if not fence_char:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char, fence_len = "", 0
            current_lines.append(line)
            continue
        if fence_char:
            current_lines.append(line)  # 围栏内一律当正文
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            htext = m.group(2).strip()
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, htext))
            current_lines = []
        else:
            current_lines.append(line)
    flush()
    return chunks


def _split_long_section(text: str, max_chars: int) -> list[str]:
    """超长标题段的结构化降级切分（旧 _store_chunks 的段落段逻辑）：
    表格绑定上下文、连续列表合并、表格整体保留、列表按项边界切、
    普通文本按句子边界切。"""
    paras: list[str] = []
    prev: str | None = None
    for p in split_paragraphs(text):
        if p.lstrip().startswith("|") and prev is not None:
            prev = prev + "\n\n" + p  # 表格并入上文（表格绑定）
        elif is_list_block(p) and prev is not None and is_list_block(prev):
            prev = prev + "\n\n" + p  # 连续列表项跨空行合并，避免拆散
        else:
            if prev is not None:
                paras.append(prev)
            prev = p
    if prev is not None:
        paras.append(prev)
    # 表格结尾段吞入下一段（下文绑定）
    final: list[str] = []
    j = 0
    while j < len(paras):
        p = paras[j]
        lines = p.splitlines()
        if lines and lines[-1].strip().startswith("|") and j + 1 < len(paras):
            final.append(p + "\n\n" + paras[j + 1])
            j += 2
        else:
            final.append(p)
            j += 1
    out: list[str] = []
    for p in final:
        if len(p) <= max_chars:
            out.append(p)
        elif any(line.strip().startswith("|") for line in p.splitlines()):
            out.append(p)  # 含表格整体保留（宁大勿断）
        elif is_list_block(p):
            out.extend(split_list_block(p, max_chars))
        else:
            out.extend(split_sentences(p, max_chars))
    return out


def chunk_document(text: str, *, max_chars: int = MAX_CHARS) -> list[ChunkPiece]:
    sections = _split_sections(text)
    pieces: list[ChunkPiece] = []
    section_index = 0
    for crumb, section_text in sections:
        section_id = f"s{section_index}"
        section_index += 1
        if len(section_text) <= max_chars:
            pieces.append(
                ChunkPiece(
                    heading_breadcrumb=crumb,
                    text=section_text,
                    section_id=section_id,
                    section_text=section_text,
                )
            )
            continue
        for part in _split_long_section(section_text, max_chars):
            pieces.append(
                ChunkPiece(
                    heading_breadcrumb=crumb,
                    text=part,
                    section_id=section_id,
                    section_text=section_text,
                )
            )
    return pieces
