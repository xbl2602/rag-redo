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

**2026-09-30 的调整（BC-07，操作者批准；0.4.0）**。起因是对本机真实提取缓存（348 份、
23,423 块）的体检：短于 30 字的块占 21%，单独一个公式的块占 14%，还有 32 块只含半个公式。
在上面五条之外新增：

6. `$$…$$` 公式块与表格一样"宁大勿断"：块内的空行不是段落边界，块内的 `#` 不是标题，超长
   也不按句子切。找不到配对的结尾 `$$`（向后 `_MAX_MATH_LINES` 行内）就当普通段落，不吞后文。
7. 公式块不再单独成块：并入紧挨它的上文（引出句）；上文若是超长段，只借最后几句；紧随其后的
   "式中…/where…"说明或不超过 `_SHORT_TAIL_CHARS` 字的短行（"(7 marks)"）一并带上；连续的
   公式（推导链）拼在一起，最多拼到 `_MATH_CHAIN_RATIO`×上限。做法参考 RAGFlow 把前后文字窗口
   并进表格/图片块、Docling 把标题与图注拼进嵌入文本；不用大模型，不违背本地优先。
8. 同一节里的碎小段落合并到接近上限再成块（参考 Docling 的 merge_peers、Unstructured 的
   combine_text_under_n_chars）；有一侧是碎小段（< `_TINY_CHARS` 字）时允许略超上限
   （≤ `_SOFT_PACK_RATIO`×），免得在上限边缘留下孤零零的碎块。表格、公式这类不可切的块只通过
   第 2、7 条并入自己的上下文，不与无关段落合并；合并不跨节。

表格"整张不切"是操作者确认的设计（避免丢上下文），本次调整不改。它的已知代价是：很大的表格
在重排阶段只有开头参与打分（重排模型的最大长度是 512）。

HTML 表格不在这里处理：索引文本清洗（`core/text_cleaning.py::flatten_html_tables`）已把它摊平成
竖线表格，切块器只需认竖线表格。

面包屑分隔符用 " / "（对齐旧 split_by_headings 的输出格式）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CHUNKER_VERSION = "0.4.0"

MAX_CHARS = 600  # 对齐旧 config.py::chunk_char_limit 默认值（问题22-F17 之前的基础值）
SHORT_DOC_CHAR_LIMIT = 200  # 对齐旧 config.py::short_doc_char_limit

#: `$$` 公式块向后最多找多少行的结尾 `$$`。找不到就当普通段落——一个孤零零的 `$$`
#: 不能把后面整篇文档吞成一个公式。（GUI 渲染器 `md_render` 用同一个数。）
_MAX_MATH_LINES = 60
#: 公式后面紧跟的这类段落是公式的说明（符号含义、适用条件），并进公式块。
_WHERE_CLAUSE_RE = re.compile(
    r"^\s*(?:式中|其中|符号说明|注[:：]|where\b|here\b|with\b|in which\b|note\b|given\b|let\b)",
    re.IGNORECASE,
)
#: 公式后面不超过这么多字的短行（"(7 marks)"、"解得 x=2"）也带上。
_SHORT_TAIL_CHARS = 40
#: 连续公式（推导链）最多拼到 max_chars 的几倍；再长就另起一块，免得整页推导拼成一个巨块。
_MATH_CHAIN_RATIO = 2
#: 碎小段落的字数线，以及合并时相对 max_chars 允许超出的比例（见模块文档第 8 条）。
_TINY_CHARS = 60
_SOFT_PACK_RATIO = 1.25

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


def _find_display_math_end(lines: list[str], i: int) -> int | None:
    """第 i 行若以 `$$` 开头且能找到配对的结尾 `$$`，返回结尾所在行的下标（同一行闭合就是 i）；
    否则返回 None（当普通文字，不当公式块）。"""
    s = lines[i].strip()
    if not s.startswith("$$"):
        return None
    if "$$" in s[2:]:
        return i
    for j in range(i + 1, min(len(lines), i + 1 + _MAX_MATH_LINES)):
        if "$$" in lines[j]:
            return j
    return None


def _has_display_math(text: str) -> bool:
    lines = text.splitlines()
    return any(_find_display_math_end(lines, i) is not None for i in range(len(lines)))


def split_paragraphs(text: str) -> list[str]:
    """按空行切段落，去首尾空白（旧 split_paragraphs）。`$$…$$` 公式块内部的空行不算段落
    边界（公式块整体留在一个段落里）；围栏代码块内的 `$$` 不算公式。"""
    lines = text.splitlines()
    paragraphs: list[str] = []
    current: list[str] = []
    fence_char = ""
    fence_len = 0
    i = 0

    def flush() -> None:
        para = "\n".join(current).strip()
        if para:
            paragraphs.append(para)
        current.clear()

    while i < len(lines):
        line = lines[i]
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if not fence_char:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char, fence_len = "", 0
            current.append(line)
            i += 1
            continue
        if not fence_char:
            end = _find_display_math_end(lines, i)
            if end is not None:
                current.extend(lines[i : end + 1])
                i = end + 1
                continue
        if not line.strip():
            flush()
        else:
            current.append(line)
        i += 1
    flush()
    return paragraphs


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
    含围栏内 `#` 不算标题的 F8 修复；`$$…$$` 公式块内的 `#` 同理）。
    返回 [(heading_path, body)]。"""
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

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if not fence_char:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char, fence_len = "", 0
            current_lines.append(line)
            i += 1
            continue
        if fence_char:
            current_lines.append(line)  # 围栏内一律当正文
            i += 1
            continue
        end = _find_display_math_end(lines, i)
        if end is not None:
            current_lines.extend(lines[i : end + 1])  # 公式块内一律当正文
            i = end + 1
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
        i += 1
    flush()
    return chunks


@dataclass
class _Unit:
    """一个待成块的单元。`atomic`：含表格或公式块，不再切分、也不与无关段落合并。"""

    text: str
    kind: str  # "text" | "list" | "table" | "math"
    atomic: bool = False
    tail_done: bool = False  # 表格的下文（结尾段）是否已经并入
    where_open: bool = False  # 刚收进公式，下一段若是"式中…"说明或短行就并入


def _ends_with_table_row(text: str) -> bool:
    lines = text.rstrip().splitlines()
    return bool(lines) and lines[-1].strip().startswith("|")


def _split_plain(text: str, kind: str, max_chars: int) -> list[str]:
    """超长的普通单元：列表按项边界切，其余按句子边界切（旧 _store_chunks 的降级切分）。"""
    if kind == "list":
        return split_list_block(text, max_chars)
    return split_sentences(text, max_chars)


def _bind_paragraphs(paragraphs: list[str], max_chars: int) -> list[_Unit]:
    """把段落绑成单元：表格绑上下文（既有规则）、连续列表合并（既有规则）、公式绑上下文（新）。"""
    chain_cap = _MATH_CHAIN_RATIO * max_chars
    units: list[_Unit] = []
    for p in paragraphs:
        prev = units[-1] if units else None
        # ① 表格并入直接上文（表格绑定，含连续的几个表格）
        if p.lstrip().startswith("|") and prev is not None:
            prev.text += "\n\n" + p
            prev.kind, prev.atomic, prev.where_open = "table", True, False
            continue
        # ② 表格结尾段吞入下一段（下文绑定；无条件，只吞一次）
        if prev is not None and prev.atomic and not prev.tail_done and _ends_with_table_row(prev.text):
            prev.text += "\n\n" + p
            prev.tail_done, prev.where_open = True, False
            continue
        # ③ 公式块：并入上文；上文是超长段时只借它的最后几句；连续公式拼成推导链
        if _has_display_math(p):
            if prev is not None:
                if not prev.atomic:
                    units.pop()
                    if len(prev.text) > max_chars:
                        pieces = _split_plain(prev.text, prev.kind, max_chars)
                        units.extend(_Unit(t, prev.kind) for t in pieces[:-1])
                        head = pieces[-1] if pieces else ""
                    else:
                        head = prev.text
                    units.append(_Unit(head + "\n\n" + p if head else p, "math", True, where_open=True))
                    continue
                if len(prev.text) + 2 + len(p) <= chain_cap:
                    prev.text += "\n\n" + p
                    prev.where_open = True
                    continue
            units.append(_Unit(p, "math", True, where_open=True))
            continue
        # ④ 公式后面的"式中…"说明或短行
        if prev is not None and prev.where_open:
            prev.where_open = False
            if _WHERE_CLAUSE_RE.match(p) or len(p) <= _SHORT_TAIL_CHARS:
                prev.text += "\n\n" + p
                continue
        # ⑤ 连续列表项跨空行合并，避免拆散
        kind = "list" if is_list_block(p) else "text"
        if kind == "list" and prev is not None and prev.kind == "list" and not prev.atomic:
            prev.text += "\n\n" + p
            continue
        # 含表格行的普通段（表格不在段首）同样整体保留（宁大勿断）
        atomic = any(line.strip().startswith("|") for line in p.splitlines())
        units.append(_Unit(p, kind, atomic))
    return units


def _pack_small(units: list[_Unit], max_chars: int) -> list[_Unit]:
    """把相邻的碎小普通单元合并到接近上限；不可切的块（表格/公式）不参与。"""
    soft_limit = int(max_chars * _SOFT_PACK_RATIO)
    packed: list[_Unit] = []
    for unit in units:
        prev = packed[-1] if packed else None
        if (
            prev is not None
            and not prev.atomic
            and not unit.atomic
            and len(prev.text) <= max_chars
            and len(unit.text) <= max_chars
        ):
            total = len(prev.text) + 2 + len(unit.text)
            tiny = min(len(prev.text), len(unit.text)) < _TINY_CHARS
            if total <= max_chars or (tiny and total <= soft_limit):
                prev.text += "\n\n" + unit.text
                prev.kind = "text"
                continue
        packed.append(unit)
    return packed


def _split_long_section(text: str, max_chars: int) -> list[str]:
    """超长标题段的结构化降级切分：表格/公式绑定上下文且整体保留，连续列表合并，超长的普通
    段按列表项/句子边界切，最后把碎小段落合并到接近上限。"""
    units: list[_Unit] = []
    for unit in _bind_paragraphs(split_paragraphs(text), max_chars):
        if unit.atomic or len(unit.text) <= max_chars:
            units.append(unit)
        else:
            kind = "list" if unit.kind == "list" else "text"
            units.extend(_Unit(t, kind) for t in _split_plain(unit.text, kind, max_chars))
    return [unit.text for unit in _pack_small(units, max_chars)]


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
