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
TEXT_PIPELINE_VERSION = 2


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
