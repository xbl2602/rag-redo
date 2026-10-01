"""文字层 PDF 的提取逻辑：读文件 -> ExtractedDocument。

只处理有文字层的 PDF（`pymupdf4llm.to_markdown`）；完全没有文字层的
（扫描件/图片版）折叠成 failure_reason="scanned:no-text-layer"，诚实
承认"这次没收"而不是返回空文本假装成功——继承旧项目"图片版 PDF 没开
识别时会先诚实记下'这次没收'"的原则。真正的 OCR 是另一个 extractor 插件
（official-ocr-mineru-*，Phase 2）的事，两者靠 failure_reason 里的
"scanned:"前缀分流，不是这个插件自己做 OCR。

pymupdf/pymupdf4llm 可能因为各种损坏/加密 PDF 抛出五花八门的异常类型，
这里用宽 except Exception 兜底折叠——契约是"extractor 绝不抛异常"，不是
"只处理我预料到的异常类型"。

**两种转换方式（2026-09-30 操作者确认，BC-01，设置项 `pdf_text_mode`）**：装了
pymupdf-layout 时 `pymupdf4llm.to_markdown` 默认每页跑一遍 AI 版面分析（onnx 模型）——
标题层级、表格、图里的文字都认得最好，但真机每页 0.25~0.5 秒、占满全部 CPU 核
（Y2S1 这种 7600 页的库首次转换估计 40~55 分钟，用户看到的"转换阶段 CPU 尖峰、显卡
闲着"就是它）。旧项目与本项目此前一直是这种。"快速"方式直接调经典规则式转换
（`pymupdf_rag.to_markdown`，不认表格、不分析图形）：讲义/幻灯片每页约 6~9 毫秒
（快约 30 倍），厚教材快约 6 倍，只用 1~2 个核；代价是标题层级偶尔认错（把正文当
标题）、图片里的文字不收、表格变成普通文字行。默认 `auto`：超过 `FAST_MODE_MIN_PAGES`
页的大文件用快速方式，其余照旧。

快速方式**直接调 `pymupdf_rag`，不切 `pymupdf4llm.use_layout()`**：那是进程级全局开关，
同一进程里另一份文件正在按版面分析转换时切它会串台；而 `table_strategy=None` 保证
经典转换不会走到 pymupdf 里唯一还会调版面模型的找表格那一步。

两种方式产出的 `extractor_version` 相同：提取缓存按"插件:版本"找正文，模式写进版本号
会让快速方式转好的正文下一轮找不到。用的哪种方式由插件壳记在结果的 `extractor_settings`
里（plugin.py::output_settings）：2026-10-01 操作者确认，切换方式后下一轮把已经用文字层
转好的 PDF 全按新方式重转（此前只影响之后新转的，真机上改完看不出变化）；模式同时进能力
签名，此前转换失败的文件也会按新模式重试一次（AGENTS.md §8.5）。

**按页分流（2026-10-01 操作者确认，BC-01）**：以前只要有一页不到 10 个字，整本书就当扫描件
送去识别——厚教材因为封面、空白页整本被送去本机识别，又撞上 200 页上限，一个字都没进索引。
现在逐页判“文字页 / 图片页 / 空白页”（规则与设置项 `pdf_image_page_rule` 见 pages.py）：
一页够字的都没有 → 仍是整本扫描件（`scanned`，`image_pages` 列出全书页码）；有文字页也有图片页 →
逐页转出文字层（`page_texts`），`image_pages` 列出图片页，由编排层把这些页切出来送识别、按页码
拼回（core/pdf_pages.py）；只有文字页和空白页 → 照旧整本一次转。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from typing import Callable

import pymupdf4llm
from pymupdf4llm.helpers import pymupdf_rag

from core.contracts import ExtractedDocument

from .pages import DEFAULT_IMAGE_PAGE_RULE
from .pages import inspect as inspect_pages

#: 0.4.0（2026-10-01）：按页分流——图片页只列出来交编排层送识别，文字页直接转；空白页不再让整本
#: 书被当成扫描件（见 pages.py）。产出变了，升版本让存量 PDF 下一轮按新规则重转（§8.6）。
EXTRACTOR_VERSION = "0.4.0"
PLUGIN_ID = "official-extractor-pdf-text"

#: 设置项 `pdf_text_mode` 的取值（含义见模块 docstring）。
PDF_TEXT_MODES = ("auto", "layout", "fast")
DEFAULT_PDF_TEXT_MODE = "auto"
#: `auto` 下超过这么多页的 PDF 用快速方式（操作者确认的折中：厚教材才值得牺牲一点结构）。
FAST_MODE_MIN_PAGES = 200
#: 快速方式的参数：不找表格（找表格会调版面模型，也是经典转换里最慢的一步）、不分析矢量图形、
#: 不管图片——只要文字和按字号判断的标题。
_FAST_OPTIONS = {"table_strategy": None, "ignore_graphics": True, "ignore_images": True, "show_progress": False}


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uses_fast_mode(mode: str, pages: int) -> bool:
    """这份 PDF 按设置该不该用快速方式转换（未知取值按默认 auto 处理）。"""
    if mode not in PDF_TEXT_MODES:
        mode = DEFAULT_PDF_TEXT_MODE
    if mode == "fast":
        return True
    if mode == "layout":
        return False
    return pages > FAST_MODE_MIN_PAGES


def _fail(
    library_id: str, path: str, reason: str, content_hash: str = "", *, image_pages: tuple[int, ...] = ()
) -> ExtractedDocument:
    return ExtractedDocument(
        library_id=library_id,
        path=path,
        text=None,
        failure_reason=reason,
        extracted_by=PLUGIN_ID,
        extractor_version=EXTRACTOR_VERSION,
        content_hash=content_hash,
        image_pages=image_pages,
    )


def _page_texts(chunks, page_count: int) -> tuple[str, ...]:
    """`to_markdown(page_chunks=True)` 的逐页结果 → 与页码一一对应的正文（缺的页当空）。"""
    if isinstance(chunks, list) and len(chunks) == page_count:
        return tuple(str(chunk.get("text") or "") for chunk in chunks)
    texts = [""] * page_count
    for chunk in chunks if isinstance(chunks, list) else []:
        number = (chunk.get("metadata") or {}).get("page")
        if isinstance(number, int) and 1 <= number <= page_count:
            texts[number - 1] = str(chunk.get("text") or "")
    return tuple(texts)


def extract(
    library_id: str,
    path: str,
    root: Path,
    *,
    mode: str = DEFAULT_PDF_TEXT_MODE,
    image_rule: str = DEFAULT_IMAGE_PAGE_RULE,
    log: Callable[[str], None] | None = None,
) -> ExtractedDocument:
    full_path = root / path
    try:
        data = full_path.read_bytes()
    except OSError as exc:
        return _fail(library_id, path, f"读取失败: {type(exc).__name__}: {exc}")

    content_hash = _content_hash(data)

    page_texts: tuple[str, ...] | None = None
    try:
        layout = inspect_pages(full_path, image_rule)
        pages = layout.page_count
        if not layout.has_text_layer:
            # 一页够字的都没有（或整本只有水印）：整本扫描件，列出全书页码交给识别
            return _fail(library_id, path, "scanned", content_hash, image_pages=tuple(range(1, pages + 1)))
        fast = uses_fast_mode(mode, pages)
        if fast and log is not None:
            log(f"PDF 用快速方式转换（{pages} 页，模式 {mode}）：{path}")
        image_pages = layout.image_pages
        if not image_pages:
            if fast:
                text = pymupdf_rag.to_markdown(str(full_path), **_FAST_OPTIONS)
            else:
                text = pymupdf4llm.to_markdown(str(full_path))
        else:
            # 文字页和图片页混着：逐页转，编排层把图片页送识别后按页码拼回（core/pdf_pages.py）。
            # 逐页结果按顺序拼起来与整本一次转出来的正文相同。
            if fast:
                chunks = pymupdf_rag.to_markdown(str(full_path), page_chunks=True, **_FAST_OPTIONS)
            else:
                chunks = pymupdf4llm.to_markdown(str(full_path), page_chunks=True)
            page_texts = _page_texts(chunks, pages)
            text = "".join(page_texts)
            if log is not None:
                log(f"PDF 按页分流：共 {pages} 页，其中 {len(image_pages)} 页是图片页、要送识别：{path}")
    except Exception as exc:  # noqa: BLE001 - extractor 绝不抛异常，见模块 docstring
        return _fail(library_id, path, f"提取失败: {type(exc).__name__}: {exc}", content_hash)

    if not text.strip():
        if image_pages:
            # 文字层什么都没转出来：当整本扫描件，全部送识别
            return _fail(library_id, path, "scanned", content_hash, image_pages=tuple(range(1, pages + 1)))
        return _fail(library_id, path, "提取结果为空", content_hash)

    return ExtractedDocument(
        library_id=library_id,
        path=path,
        text=text,
        failure_reason=None,
        extracted_by=PLUGIN_ID,
        extractor_version=EXTRACTOR_VERSION,
        content_hash=content_hash,
        image_pages=image_pages,
        page_texts=page_texts,
    )
