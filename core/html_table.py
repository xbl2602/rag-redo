"""core/html_table.py — HTML 表格（MinerU 的表格输出）的宽容解析，渲染器与索引清洗共用。

为什么放在 core：**"哪一段文字是 HTML 表格、单元格里是什么"只能有一个判断**（AGENTS.md
§4.5、§10.5）。GUI 渲染器要把它画成真表格（`official_gui_shell/md_render.py`），索引清洗要把它
摊平成竖线表格再交给切块器（`core/text_cleaning.py::flatten_html_tables`）；两边各写一份解析，
迟早对"什么算表格"给出不同答案。这里只做**解析**，不产出任何 HTML 或 Markdown——怎么呈现
是调用方的事。

真实数据（2026-09-30 对本机 196 份 MinerU 本机识别结果的体检）里的 HTML 表格：整张表一行，
只用 `table/tr/td`、`sup/sub`、`rowspan/colspan`（几乎都是 1）；还有被切断成半截的表格块
（开头是 `Sat.</td></tr><tr>…`、结尾缺 `</table>`）。所以解析必须宽容：遇到 `<tr>`/`<td>`
就隐式开表，文字不丢；表格外的文字按原样交还。

**解析结果不含任何原始标签与属性**：单元格只留文字和几个私有区占位符（换行、上下标），
`colspan`/`rowspan` 只留限幅后的整数。调用方拿这份结构重新拼输出，`onclick`、`<script>`
这类东西没有任何路径能走到输出里。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

#: colspan/rowspan 的上限。浏览器本身允许到 1000，但真实表格远用不到，限幅是为了让恶意输入
#: 撑不爆布局，也让摊平时"按 rowspan 重复文字"不会无限膨胀。
MAX_SPAN = 100

#: 单元格里的行内标签先换成这几个私有区字符，调用方在文字处理完之后再换成自己的表示（HTML
#: 标签、纯文本记号……）。原文里本来就有的私有区字符会被先删掉，不会与占位符混淆。
BR = ""
SUP_OPEN = ""
SUP_CLOSE = ""
SUB_OPEN = ""
SUB_CLOSE = ""
_SENTINEL_STRIP_RE = re.compile("[-]")

_TABLE_STRUCT_TAGS = frozenset({"table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"})

#: 判定一行是不是 HTML 表格（碎片）。只认三种，避免把"用 <td> 表示单元格"这样的说明文字
#: 当成表格：行首就是表格标签；同一行里 `<table` 后面跟着 `<tr`/`<td`；含有闭合的
#: `</td>` `</tr>` `</table>`（被切断的表格块只剩后半截，行首是普通文字）。
_LINE_START_RE = re.compile(
    r"^\s*</?\s*(?:table|thead|tbody|tfoot|tr|td|th|caption)\b", re.IGNORECASE
)
_LINE_MID_RE = re.compile(r"<\s*table\b.*<\s*(?:tr|td)\b", re.IGNORECASE)
_LINE_CLOSE_RE = re.compile(r"</\s*(?:td|th|tr|table)\s*>", re.IGNORECASE)
_TABLE_OPEN_RE = re.compile(r"<\s*table\b", re.IGNORECASE)
_TABLE_END_RE = re.compile(r"</\s*table\s*>", re.IGNORECASE)


@dataclass
class HtmlCell:
    """一个单元格：标签（td/th）、限幅后的跨度、文字片段（含占位符）。"""

    tag: str
    colspan: int
    rowspan: int
    parts: list[str] = field(default_factory=list)

    @property
    def raw(self) -> str:
        return "".join(self.parts)


#: `("text", 表格之外的文字)` 或 `("table", 行列表，每行是 HtmlCell 列表)`。
Segment = tuple[str, object]


def _span_value(attrs: list[tuple[str, str | None]], name: str) -> int:
    """取 colspan/rowspan：只认 1~3 位纯数字，限幅到 `MAX_SPAN`；其余一律当 1。"""
    for key, value in attrs:
        if key == name and value is not None:
            m = re.fullmatch(r"\s*(\d{1,3})\s*", value)
            if m:
                return max(1, min(int(m.group(1)), MAX_SPAN))
    return 1


class _HtmlTableParser(HTMLParser):
    """把一段含 HTML 表格的文本拆成 `Segment` 列表。嵌套表格只保留最外层结构，内层的文字并入
    当前单元格。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[Segment] = []
        self._rows: list[list[HtmlCell]] | None = None
        self._row: list[HtmlCell] | None = None
        self._cell: HtmlCell | None = None
        self._depth = 0

    # -- 结构维护 --------------------------------------------------------
    def _open_table(self) -> None:
        if self._rows is None:
            self._rows = []

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(self._cell)
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row:
            if self._rows is None:
                self._rows = []
            self._rows.append(self._row)
        self._row = None

    def _new_row(self) -> None:
        self._open_table()
        self._close_row()
        self._row = []

    def _new_cell(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open_table()
        if self._row is None:
            self._row = []
        self._close_cell()
        self._cell = HtmlCell(
            tag=tag,
            colspan=_span_value(attrs, "colspan"),
            rowspan=_span_value(attrs, "rowspan"),
        )

    def _flush_table(self) -> None:
        self._close_row()
        if self._rows:
            self.segments.append(("table", self._rows))
        self._rows = None
        self._depth = 0

    def _emit_inline(self, token: str) -> None:
        """行内标签（换行/上下标/图片）：在单元格里就记进去，表格外只保留图片占位。"""
        if self._cell is not None:
            self._cell.parts.append(token)
        elif self._rows is None and token.startswith("!["):
            self._append_text(token)

    def _append_text(self, data: str) -> None:
        if self.segments and self.segments[-1][0] == "text":
            self.segments[-1] = ("text", str(self.segments[-1][1]) + data)
        else:
            self.segments.append(("text", data))

    # -- HTMLParser 回调 ---------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            if self._depth == 0:
                self._flush_table()
                self._open_table()
                self._depth = 1
            else:
                self._depth += 1
            return
        if self._depth > 1 and tag in _TABLE_STRUCT_TAGS:
            return  # 嵌套表格：只认最外层结构
        if tag == "tr":
            self._new_row()
        elif tag in ("td", "th"):
            self._new_cell(tag, attrs)
        elif tag == "br":
            self._emit_inline(BR)
        elif tag == "sup":
            self._emit_inline(SUP_OPEN)
        elif tag == "sub":
            self._emit_inline(SUB_OPEN)
        elif tag == "img":
            alt = dict(attrs).get("alt") or ""
            self._emit_inline("![%s](img)" % re.sub(r"[\[\]]", "", alt).strip())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() in ("td", "th", "tr"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            if self._depth > 1:
                self._depth -= 1
            else:
                self._flush_table()
            return
        if self._depth > 1 and tag in _TABLE_STRUCT_TAGS:
            return
        if tag == "tr":
            self._close_row()
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "sup":
            self._emit_inline(SUP_CLOSE)
        elif tag == "sub":
            self._emit_inline(SUB_CLOSE)

    def handle_data(self, data: str) -> None:
        data = _SENTINEL_STRIP_RE.sub("", data)
        if not data:
            return
        if self._rows is None:
            self._append_text(data)  # 表格之外的文字（例如切断块开头的 "Sat."）
        elif self._cell is not None:
            self._cell.parts.append(data)
        elif data.strip():
            # 在表格里、但不在任何单元格中的文字（例如 <caption>）：不丢，放进临时单元格
            self._new_cell("td", [])
            if self._cell is not None:
                self._cell.parts.append(data)

    def close(self) -> None:
        super().close()
        self._flush_table()


def parse_html_table_block(raw: str) -> list[Segment]:
    """一段含 HTML 表格的原文 → `Segment` 列表。**不抛异常**：畸形输入退回"整段当普通文字"。"""
    parser = _HtmlTableParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001 - 畸形输入不能让渲染/索引清洗整体失败，退回当普通文字
        return [("text", raw)]
    return parser.segments


def is_html_table_line(line: str) -> bool:
    return bool(
        _LINE_START_RE.match(line) or _LINE_MID_RE.search(line) or _LINE_CLOSE_RE.search(line)
    )


def take_html_table_block(lines: list[str], i: int) -> tuple[str, int]:
    """从第 i 行起取出一个 HTML 表格块：接着往下并入"表还没闭合"或"本身也是表格碎片"的
    连续非空行。返回 (原文, 下一行下标)。"""
    buf = [lines[i]]
    i += 1
    n = len(lines)
    while i < n and lines[i].strip():
        joined = "\n".join(buf)
        unclosed = len(_TABLE_OPEN_RE.findall(joined)) > len(_TABLE_END_RE.findall(joined))
        if not (unclosed or is_html_table_line(lines[i])):
            break
        buf.append(lines[i])
        i += 1
    return "\n".join(buf), i
