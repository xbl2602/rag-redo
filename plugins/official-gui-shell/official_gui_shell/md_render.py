"""Markdown → HTML 迷你渲染器（GUI 呈现层，移植自 obsidian-rag，2026-09-30 起在其上扩展）。

为什么在 GUI 插件层而不是 core：`core` 不该知道"HTML"这个概念——它产出的是
Markdown 原文，渲染成什么形态是操作方式的事（同一份 core 数据将来也能给 CLI
纯文本、MCP 结构化返回用）。这与 guiweb/contracts.md "所有列表/字段中文文案由
后端给全，前端只做渲染"是同一个分层。

**三个调用点必须共用这一个渲染器**（旧 contracts.md 明确要求，检索命中/试验台/
正文查看的渲染结果要一致）：
- `Api.search` 的每条结果 `rendered_html`
- `Api.read_document` 的 `rendered_html`
- `Api.preview_poll` 的 `result.rendered_html`

**2026-09-30 的扩展（BC-15，操作者批准）**：旧项目的渲染器只认 GFM 竖线表格，而本项目
用 MinerU 识别出来的内容里，表格是 HTML（`<table><tr><td rowspan=1 …>`）、公式是
`$$…$$` / `$…$` 的 LaTeX、图片是本机根本不存在的 `![](images/…)`。旧渲染器把 HTML 表格
当成一串带尖括号的文字、把公式里的 `*` `_` 当成斜体/粗体标记吃掉。现在：
- HTML 表格 → 真表格（白名单：只放行 table/tr/td/th 结构、`sup`/`sub`/`br`、整数的
  `colspan`/`rowspan`；其余标签一律丢弃，文字保留并转义）；
- `$$…$$` 公式块 → 等宽原文块（`<pre class="md-math">`），**不做数学排版**（排版要往前端
  加公式库，是另一个需要单独批准的决定）；行内 `$…$` 被保护起来，不再被斜体/粗体规则改坏；
- 竖线表格的单元格：粗体等行内格式、`<br>`、`\\|` 转义竖线都正常。
图片不动：仍是"图片"占位符（图片文件从未导出，见 `official-ocr-mineru-local` 的 `return_images`）。

XSS-safe：文本先 `html.escape` 再套标签，不信任任何原始内容，也不插第三方。HTML 表格
的输出完全由"解析出的行列 + 固定标签 + 限幅整数"重新拼成，原始标签和属性一个都不透传。
"""
from __future__ import annotations

import html as _h
import re

from core import html_table as _ht

#: 行内公式（沿用 Pandoc 的判定规则，避免把"$5 涨到 $10"这样的金额当公式）：开头的 `$`
#: 右边必须紧跟非空白，结尾的 `$` 左边必须是非空白、右边不能紧跟数字。`$$…$$` 先于单个
#: `$…$` 处理，否则行内的 `$$a*b$$` 会被拆成两个空公式。
_INLINE_DISPLAY_MATH_RE = re.compile(r"\$\$(.+?)\$\$")
_INLINE_MATH_RE = re.compile(r"(?<![\\$])\$(?=[^\s$])((?:\\.|[^$\n\\])+?)(?<=\S)\$(?![\d$])")

#: `$$` 公式块向后最多找多少行的结尾 `$$`。找不到就当普通段落——一个孤零零的 `$$`
#: 不能把后面整篇文档吞成一个公式。
_MAX_MATH_LINES = 60

#: 单元格里的行内占位符换回固定标签。占位符来自 `core.html_table`：单元格文字先转义、套完行内
#: 格式，最后才换成标签，所以 `<sup>`/`<br>` 的内容仍会走一遍转义，不会有原始标签混进输出。
_SENTINEL_HTML = {
    _ht.BR: "<br/>",
    _ht.SUP_OPEN: "<sup>",
    _ht.SUP_CLOSE: "</sup>",
    _ht.SUB_OPEN: "<sub>",
    _ht.SUB_CLOSE: "</sub>",
}


def _md_inline(s: str) -> str:
    """行内 Markdown → HTML（输入已是纯文本，只需加壳与标签）。

    顺序：代码块 → 公式 → 图片 → 链接 → 加粗/斜体/删除线/下划线 → 还原占位符。
    公式必须在图片/链接/加粗斜体之前被保护起来，否则 `$x_1 * y_2 * z$` 里的星号
    会被当成斜体标记吃掉、公式就被改坏了。
    """
    holders: dict[str, str] = {}

    def _hold(fragment: str) -> str:
        holders["\x00%d\x00" % len(holders)] = fragment
        return "\x00%d\x00" % (len(holders) - 1)

    s = re.sub(r"`([^`\n]+)`", lambda m: _hold("<code>%s</code>" % m.group(1)), s)
    s = _INLINE_DISPLAY_MATH_RE.sub(
        lambda m: _hold('<code class="md-math">$$%s$$</code>' % m.group(1)), s
    )
    s = _INLINE_MATH_RE.sub(
        lambda m: _hold('<code class="md-math">$%s$</code>' % m.group(1)), s
    )
    s = re.sub(
        r"!\[([^\]]*)\]\([^)]*\)",
        lambda m: _hold('<span class="md-img">🖼 %s</span>' % (m.group(1) or "图片")),
        s,
    )

    def _link(m: re.Match[str]) -> str:
        text, url = m.group(1), (m.group(2) or "").strip()
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if url.startswith("#") or scheme in ("http", "https", "obsidian", "mailto", ""):
            return _hold(
                '<a href="%s" target="_blank" rel="noreferrer">%s</a>'
                % (_h.escape(url, quote=True), text)
            )
        return _hold("%s（%s）" % (text, _h.escape(url)))

    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__([^_]+)__", r"<b>\1</b>", s)
    s = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", s)
    s = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<i>\1</i>", s)
    for k, v in holders.items():
        s = s.replace(k, v)
    return s


# ---------------------------------------------------------------------------
# 竖线（GFM）表格
# ---------------------------------------------------------------------------


def _split_pipe_cells(row: str) -> list[str]:
    """按未转义的竖线切单元格；`\\|` 是转义竖线，留在格子里并还原成 `|`。"""
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _pipe_cell_html(cell: str) -> str:
    """竖线表格单元格 → HTML：行内格式照常生效，`<br>` 换行还原成真换行。"""
    html = _md_inline(_h.escape(cell))
    return re.sub(r"&lt;br\s*/?&gt;", "<br/>", html, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# HTML 表格（MinerU 的表格输出）
# ---------------------------------------------------------------------------


def _html_cell(cell: _ht.HtmlCell) -> str:
    text = re.sub(r"[ \t\r\n]+", " ", cell.raw).strip()
    html = _md_inline(_h.escape(text))
    for sentinel, tag in _SENTINEL_HTML.items():
        html = html.replace(sentinel, tag)
    attrs = ""
    if cell.colspan > 1:
        attrs += ' colspan="%d"' % cell.colspan
    if cell.rowspan > 1:
        attrs += ' rowspan="%d"' % cell.rowspan
    return "<%s%s>%s</%s>" % (cell.tag, attrs, html, cell.tag)


def _html_table_block(raw: str) -> list[str]:
    """一段含 HTML 表格的原文 → 若干 HTML 片段（表格外的文字成段落，表格成 `<table>`）。"""
    out: list[str] = []
    for kind, payload in _ht.parse_html_table_block(raw):
        if kind == "text":
            lines = [ln.strip() for ln in str(payload).splitlines() if ln.strip()]
            if lines:
                out.append("<p>%s</p>" % "<br/>".join(_md_inline(_h.escape(ln)) for ln in lines))
        else:
            rows = "".join(
                "<tr>%s</tr>" % "".join(_html_cell(c) for c in row)
                for row in payload  # type: ignore[attr-defined]
            )
            out.append("<table><tbody>%s</tbody></table>" % rows)
    return out


# ---------------------------------------------------------------------------
# `$$…$$` 公式块
# ---------------------------------------------------------------------------


def _take_display_formula(lines: list[str], i: int) -> tuple[str, str, int] | None:
    """第 i 行以 `$$` 开头时，取出整个公式块：返回 (公式原文, 结尾 `$$` 之后的余文, 下一行下标)；
    找不到配对的 `$$` 就返回 None（当普通段落处理，不吞掉后面的内容）。"""
    first = lines[i].strip()[2:]
    if "$$" in first:
        head, _, trailing = first.partition("$$")
        return head.strip(), trailing.strip(), i + 1
    buf = [first]
    for j in range(i + 1, min(len(lines), i + 1 + _MAX_MATH_LINES)):
        if "$$" in lines[j]:
            head, _, trailing = lines[j].partition("$$")
            buf.append(head)
            return "\n".join(x.rstrip() for x in buf).strip(), trailing.strip(), j + 1
        buf.append(lines[j])
    return None


def md_to_html(md: str) -> str:
    """Markdown → HTML（离线 mini 渲染，检索命中/试验台/正文查看三处共用）。

    支持：h1-h4、围栏代码块、引用、ul/ol、GFM 表格、HTML 表格（MinerU 输出，白名单）、
    `$$` 公式块（等宽原文，不排版）、分隔线、行内样式（见 `_md_inline`）。XSS-safe。
    """
    lines = (md or "").splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    para: list[str] = []

    def _flush_para() -> None:
        if para:
            out.append(
                "<p>%s</p>" % "<br/>".join(_md_inline(_h.escape(p)) for p in para)
            )
            del para[:]

    def _flush_list(tag: str, items: list[str]) -> None:
        if items:
            out.append(
                "<%s>%s</%s>"
                % (tag, "".join("<li>%s</li>" % _md_inline(_h.escape(t)) for t in items), tag)
            )
            del items[:]

    ul: list[str] = []
    ol: list[str] = []
    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            _flush_para()
            _flush_list("ul", ul)
            _flush_list("ol", ol)
            i += 1
            continue
        if s.startswith("```"):
            _flush_para()
            _flush_list("ul", ul)
            _flush_list("ol", ol)
            lang = _h.escape(s[3:].strip().split()[0]) if s[3:].strip() else ""
            buf: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 吃掉结尾 ```
            out.append(
                '<pre><code%s>%s</code></pre>'
                % ((' class="%s"' % lang) if lang else "", _h.escape("\n".join(buf)))
            )
            continue
        # `$$` 公式块：等宽原文，不排版；找不到配对的结尾 `$$` 就落到下面当普通段落
        if s.startswith("$$"):
            formula = _take_display_formula(lines, i)
            if formula is not None:
                content, trailing, i = formula
                _flush_para()
                _flush_list("ul", ul)
                _flush_list("ol", ol)
                out.append(
                    '<pre class="md-math"><code>%s</code></pre>' % _h.escape(content)
                )
                if trailing:
                    para.append(trailing)
                continue
        # HTML 表格（MinerU）：解析成行列后重新拼，绝不透传原始标签
        if _ht.is_html_table_line(line):
            _flush_para()
            _flush_list("ul", ul)
            _flush_list("ol", ol)
            raw, i = _ht.take_html_table_block(lines, i)
            out.extend(_html_table_block(raw))
            continue
        # GFM 表格：表头行 + 分隔行（| - : 空格）+ 若干数据行
        if (
            "|" in s
            and i + 1 < n
            and re.match(r"^\s*\|?[\s|:~-]+\|?[\s|:~-]*$", lines[i + 1])
            and "-" in lines[i + 1]
        ):
            _flush_para()
            _flush_list("ul", ul)
            _flush_list("ol", ol)
            head = _split_pipe_cells(s)
            out.append(
                "<table><thead><tr>%s</tr></thead><tbody>"
                % "".join("<th>%s</th>" % _pipe_cell_html(c) for c in head)
            )
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                out.append(
                    "<tr>%s</tr>"
                    % "".join("<td>%s</td>" % _pipe_cell_html(c) for c in _split_pipe_cells(lines[i]))
                )
                i += 1
            out.append("</tbody></table>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            _flush_para()
            _flush_list("ul", ul)
            _flush_list("ol", ol)
            lv = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lv, _md_inline(_h.escape(m.group(2))), lv))
            i += 1
            continue
        if re.match(r"^([-*_]\s*){3,}$", s):
            _flush_para()
            _flush_list("ul", ul)
            _flush_list("ol", ol)
            out.append("<hr/>")
            i += 1
            continue
        if s.startswith(">"):
            _flush_para()
            _flush_list("ul", ul)
            _flush_list("ol", ol)
            quotes: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quotes.append(lines[i].strip()[1:].strip())
                i += 1
            out.append(
                "<blockquote>%s</blockquote>"
                % "<br/>".join(_md_inline(_h.escape(q)) for q in quotes)
            )
            continue
        m = re.match(r"^[-*+]\s+(.*)$", s)
        if m:
            _flush_para()
            _flush_list("ol", ol)
            ul.append(m.group(1))
            i += 1
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", s)
        if m:
            _flush_para()
            _flush_list("ul", ul)
            ol.append(m.group(1))
            i += 1
            continue
        _flush_list("ul", ul)
        _flush_list("ol", ol)
        para.append(line.strip())
        i += 1
    _flush_para()
    _flush_list("ul", ul)
    _flush_list("ol", ol)
    return "\n".join(out)
