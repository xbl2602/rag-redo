"""core/text_cleaning.py — 索引文本清洗（核心服务，切块前调用）。

逐字移植自 obsidian-rag/index.py 的 extract_frontmatter（1095-1135）与
clean_wikilinks（1138-1164），对应旧项目决策：问题15/审计 F9（wikilink
清洗——裸链接目标词是最高信号的概念词，不清洗会同时污染嵌入文本和 BM25
词表）、审计 F19（多行 tags 解析）、问题18/审计 F20（frontmatter
title/tags 作文件级锚点）。

只动索引层：提取缓存与 read_document 交付的原文不做任何清洗。
"""
from __future__ import annotations

import re

# 索引文本管线版本：清洗/锚点逻辑变化时递增，index_library 的 text_pipeline
# 签名随之变化，触发旧 generation 受控重切块/重嵌入（对齐旧项目 META_VERSION
# 机制；此版本从 2 起——隐含的"1"是加入清洗与锚点之前的管线）。
TEXT_PIPELINE_VERSION = 3  # v3: 提取噪声清洗（问题48 v10/v11 双轨）——旧 META_VERSION 11 的对应物


def _clean_scalar(s: str) -> str:
    return s.strip().strip('"').strip("'").strip()


def extract_frontmatter(text: str) -> tuple[dict, str]:
    """提取 frontmatter 元数据，返回 dict 和去掉 frontmatter 的正文。

    支持三种写法（值一律规整成逗号分隔的扁平字符串）：
        title: 火箭发动机笔记        → "火箭发动机笔记"
        aliases: [发动机, 引擎]      → "发动机, 引擎"
        tags:                        → "航天, CFD"
          - 航天
          - CFD
    （Obsidian 最常见的多行 tags 必须能解析——审计 F19。）
    """
    meta: dict[str, str] = {}
    body = text
    if not text.startswith("---"):
        return meta, body
    end = text.find("\n---", 3)
    if end == -1:
        return meta, body
    fm = text[3:end]
    body = text[end + 4:]
    cur_key = None
    for line in fm.splitlines():
        if not line.strip():
            continue  # 空行不打断当前列表
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item and cur_key:
            v = _clean_scalar(item.group(1))
            if v:
                meta[cur_key] = f"{meta[cur_key]}, {v}" if meta.get(cur_key) else v
            continue
        m = re.match(r"^([\w-]+):\s*(.*)$", line)
        if m:
            cur_key = m.group(1)
            val = _clean_scalar(m.group(2))
            if val.startswith("[") and val.endswith("]"):
                val = ", ".join(_clean_scalar(p) for p in val[1:-1].split(",")
                                if _clean_scalar(p))
            meta[cur_key] = val
        else:
            cur_key = None  # 无法识别的行：结束当前键，避免误吞后续列表项
    return meta, body


def clean_wikilinks(text: str) -> str:
    """清洗 wiki 链接（[[...]]）：保留读者实际看到的文字，剥掉路径与锚点。

    - [[目标|别名]] / [[目标\\|别名]]（表格转义管道）→ 别名
    - [[目标]]                                    → 目标
    - [[folder/目标#标题]] / [[目标#^块id]]        → 目标（去路径、去锚点）
    - [[#标题]]（本文件锚点）                      → 标题
    - ![[嵌入]]（图片/附件嵌入）                    → 去除
    在切块前调用。

    裸 [[目标]] 必须保留目标词（审计 F9 的教训：Obsidian 里裸链接是主流
    写法，而链接目标恰恰是笔记里最高信号的概念词——此前整个删掉等于把
    关键词同时从嵌入文本和 BM25 词表里抹掉）。
    """

    def _repl(m):
        if m.group(0).startswith("!"):
            return ""  # ![[...]] 是附件嵌入，不是正文
        inner = m.group(1).replace(r"\|", "|")  # 表格里 \| 是转义管道，还原为分隔符
        parts = inner.split("|")
        if len(parts) > 1:
            return parts[1].strip()
        target = parts[0].strip()
        head, _, anchor = target.partition("#")
        head = head.strip()
        if head:
            return head.rsplit("/", 1)[-1].strip()  # 去掉 folder/ 路径前缀
        anchor = anchor.strip()
        return "" if anchor.startswith("^") else anchor  # ^块id 无语义，标题保留

    return re.sub(r"!?\[\[([^\]]*)\]\]", _repl, text)


def build_anchor_context(doc_parts: list[str], heading_breadcrumb: str, *, separator: str = " > ") -> str:
    """文件级语义锚点 + 标题路径，逐段去重（问题18/审计 F20 的 v6 决策）。

    Obsidian 里文件名往往就是概念本体，title/tags 只写进 frontmatter 的话
    完全没进向量、也没进 BM25 词表——引用标签被清洗后（clean_wikilinks），
    笔记的概念层信息必须由锚点补回嵌入文本。去重是必要的：文件名与 title
    常常相同，重复串白占 token 还会让该词在块内词频虚高、扭曲 BM25。

    doc_parts = [文件名 stem, frontmatter title, frontmatter tags]；heading
    为切块器的标题面包屑（separator 分隔）。返回拼好的锚点串（可能为空）。"""
    out: list[str] = []
    seen: set[str] = set()
    for part in doc_parts + (heading_breadcrumb.split(separator) if heading_breadcrumb else []):
        part = (part or "").strip()
        key = part.lower()
        if part and key not in seen and part != "(无标题)":
            seen.add(key)
            out.append(part)
    return " / ".join(out)


# ---------------------------------------------------------------------------
# 提取噪声清洗（问题48 v10）+ MinerU sidecar 双轨（问题48附记 v11）——逐字
# 移植旧 obsidian-rag/index.py::strip_*（1179-1311）。纯文本变换，无副作用。
# 清洗链顺序固定：死图链 → [官方 sidecar 精确删] → 页码行 → 样板行（旧
# index.py::_store_chunks 1934-1939：先剥图链避免重复图片路径行被误判样板；
# sidecar 只精确删官方标注噪声，残差交给启发式兜底）。
# ---------------------------------------------------------------------------



# 页码行：带标记的必删（第12页 / Page 12 / - 12 -）；裸数字行（"12"）只有
# "出现 ≥2 个互不相同的裸数字行"（分页信号）时才删——单个孤立数字行可能是
# 正文内容（如单独成行的年份/编号），宁可漏删不断错。
_PAGE_NUM_KEYWORD_RE = re.compile(r"^\s*(?:第\s*\d+\s*页|Page\s*\d+)\s*$",
                                   re.IGNORECASE)
_PAGE_NUM_DECORATED_RE = re.compile(r"^\s*[-–—_·•*]+\s*\d{1,4}\s*[-–—_·•*]+\s*$")
_PAGE_NUM_BARE_RE = re.compile(r"^\s*\d{1,4}\s*$")

# 样板行：同一文档内逐字重复 ≥3 次的"普通段落行"视为页眉/页脚/水印，全删。
# 保守边界（只删普通段落行，其余一律不动）：标题行（# 开头）删它会连带丢掉
# 切块标题路径；表格行（| 开头）长表头重复也保护；列表项/引用块可能是正文
# 强调；短行（<4 字符）单节内合法重复不受影响；纯标点/分隔线不动。
_BOILER_MIN_REPEATS = 3
_BOILER_MIN_LEN = 4
_BOILER_PUNCT_ONLY_RE = re.compile(r"^[\s\-\*_#>|~`]+$")
_BOILER_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s")

# 死图链：![alt](src) 里 src 为本地/相对路径的，文件根本不存在（提取器只存
# md 文本、图片全丢），留着是进向量的死引用。alt 非空留 alt 纯文本（仍是语义
# 信号），alt 为空整段删；远端 http(s)/data: 图片是活的（可渲染），不动；
# HTML <img> 同理取 alt。Obsidian ![[…]] 已由 clean_wikilinks 处理，不管。
_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_ALT_RE = re.compile(r"""alt\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""",
                           re.IGNORECASE)

# MinerU 官方块标注的噪声类型——实测 schema 含
# header/footer/page_number/table/text；只删这三类，表格与正文按官方认定保留。
_SIDECAR_NOISE_TYPES = frozenset({"header", "footer", "page_number"})


def strip_page_number_lines(text: str) -> str:
    """删页码行。返回清洗后的文本。（旧 index.py:1188）"""
    lines = text.splitlines()
    bare = {l.strip() for l in lines if _PAGE_NUM_BARE_RE.match(l)}
    pagination = len(bare) >= 2  # ≥2 个不同裸数字 = 分页，不是正文
    out = []
    for l in lines:
        if _PAGE_NUM_KEYWORD_RE.match(l) or _PAGE_NUM_DECORATED_RE.match(l):
            continue
        if pagination and _PAGE_NUM_BARE_RE.match(l):
            continue
        out.append(l)
    return "\n".join(out)


def _boiler_candidate(line: str) -> str | None:
    s = line.strip()
    if len(s) < _BOILER_MIN_LEN or _BOILER_PUNCT_ONLY_RE.match(s):
        return None
    if s.startswith("#") or s.startswith("|") or s.startswith(">"):
        return None
    if _LIST_ITEM_RE.match(line):
        return None
    return s


def strip_boilerplate_lines(text: str, min_repeats: int = _BOILER_MIN_REPEATS) -> str:
    """删逐字重复的样板行（页眉/页脚/水印）。返回清洗后的文本。（旧 1227）"""
    lines = text.splitlines()
    counts: dict[str, int] = {}
    cands: list[str | None] = []
    in_fence = False
    for l in lines:
        if _BOILER_FENCE_RE.match(l):
            in_fence = not in_fence
            cands.append(None)
            continue
        if in_fence:
            cands.append(None)
            continue
        c = _boiler_candidate(l)
        cands.append(c)
        if c is not None:
            counts[c] = counts.get(c, 0) + 1
    drop = {s for s, n in counts.items() if n >= min_repeats}
    if not drop:
        return text
    return "\n".join(l for l, c in zip(lines, cands)
                     if c is None or c not in drop)


def _md_img_repl(m: "re.Match[str]") -> str:
    alt = (m.group(1) or "").strip()
    src = (m.group(2) or "").strip()
    if src.lower().startswith(("http://", "https://", "data:")):
        return m.group(0)  # 远端/内嵌图是活的，不动
    return alt  # 本地相对路径已死：留 alt 或删整段


def _html_img_repl(m: "re.Match[str]") -> str:
    a = _HTML_ALT_RE.search(m.group(0))
    alt = next((g for g in (a.group(2), a.group(3), a.group(4))
                if g is not None), "") if a else ""
    return alt.strip()


def strip_dead_image_refs(text: str) -> str:
    """剥离指向本地图片的死引用（保留 alt 文本与远端图）。返回清洗后的文本。（旧 1277）"""
    text = _MD_IMG_RE.sub(_md_img_repl, text)
    return _HTML_IMG_RE.sub(_html_img_repl, text)


def strip_sidecar_noise(body: str, sidecar: object) -> str:
    """按 MinerU 官方块标注精确删页眉/页脚/页码行（问题48附记，第二档治本）。

    sidecar = MinerU 结果包 content_list.json 解析出的 list，每个元素
    {type, text, page_idx, bbox}。只做「整行逐字相等」匹配：官方标
    header/footer/page_number 的 text 在正文里恰好单独成行才删；标题行
    （# 前缀）即使文本撞上也不删——那是 md 结构（标题路径进切块向量）；
    表格行/正文/列表一律不动（官方没标噪声，绝不启发式越权）。只精确删、
    绝不猜测：撞不中的残差噪声交给 strip_page_number_lines/strip_boilerplate_lines
    启发式兜底。（旧 index.py:1288）
    """
    if not isinstance(sidecar, list) or not sidecar:
        return body
    noise: set[str] = set()
    for b in sidecar:
        if not isinstance(b, dict):
            continue
        if b.get("type") in _SIDECAR_NOISE_TYPES:
            t = str(b.get("text") or "").strip()
            if t:
                noise.add(t)
    if not noise:
        return body
    return "\n".join(l for l in body.splitlines() if l.strip() not in noise)
