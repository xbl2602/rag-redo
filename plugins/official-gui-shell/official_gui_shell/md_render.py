"""Markdown → HTML 迷你渲染器（GUI 呈现层，逐字移植自 obsidian-rag）。

为什么在 GUI 插件层而不是 core：`core` 不该知道"HTML"这个概念——它产出的是
Markdown 原文，渲染成什么形态是操作方式的事（同一份 core 数据将来也能给 CLI
纯文本、MCP 结构化返回用）。这与 guiweb/contracts.md "所有列表/字段中文文案由
后端给全，前端只做渲染"是同一个分层。

**三个调用点必须共用这一个渲染器**（旧 contracts.md 明确要求，检索命中/试验台/
正文查看的渲染结果要一致）：
- `Api.search` 的每条结果 `rendered_html`
- `Api.read_document` 的 `rendered_html`
- `Api.preview_poll` 的 `result.rendered_html`

XSS-safe：文本先 `html.escape` 再套标签，不信任任何原始内容，也不插第三方。
"""
from __future__ import annotations

import html as _h
import re


def _md_inline(s: str) -> str:
    """行内 Markdown → HTML（输入已是纯文本，只需加壳与标签）。

    顺序：代码块 → 图片 → 链接 → 加粗/斜体/删除线/下划线 → 还原占位符。
    """
    holders: dict[str, str] = {}

    def _hold(fragment: str) -> str:
        holders["\x00%d\x00" % len(holders)] = fragment
        return "\x00%d\x00" % (len(holders) - 1)

    s = re.sub(r"`([^`\n]+)`", lambda m: _hold("<code>%s</code>" % m.group(1)), s)
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


def md_to_html(md: str) -> str:
    """Markdown → HTML（离线 mini 渲染，检索命中/试验台/正文查看三处共用）。

    支持：h1-h4、围栏代码块、引用、ul/ol、GFM 表格、分隔线、行内样式
    （见 `_md_inline`）。XSS-safe。
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

            def _cells(row: str) -> list[str]:
                row = row.strip()
                if row.startswith("|"):
                    row = row[1:]
                if row.endswith("|"):
                    row = row[:-1]
                return [_h.escape(c.strip()) for c in row.split("|")]

            head = _cells(s)
            out.append(
                "<table><thead><tr>%s</tr></thead><tbody>"
                % "".join("<th>%s</th>" % c for c in head)
            )
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                out.append(
                    "<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in _cells(lines[i].strip()))
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
