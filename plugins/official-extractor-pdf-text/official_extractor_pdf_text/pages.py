"""逐页判定“文字页 / 图片页 / 空白页”（BC-01，2026-10-01 操作者确认的新规则，旧项目没有）。

旧规则是“只要有一页不到 10 个字，整本书当扫描件送识别”。2026-10-01 真机：Y2S1 库里的
传热学（1208 页）、材料力学（901 页）、飞行原理（831 页）每本只有 2～4 页没字（封面、空白页），
却整本被送去本机识别，又撞上识别 200 页的上限被拒——三本书一个字都没进索引。操作者的要求：
“重点是看每页的文字占比多不多，不要只有一两个字就判断送去文字层导致整页信息消失了”。

判定（满足任一条就是**图片页**，只有图片页送识别，其余用文字层直接转）：
  ① 几乎没字（不到 `TEXT_PAGE_MIN_CHARS` 个）但页面上有图；
  ② 图占了这一页面积的 `IMAGE_MIN_COVERAGE`（两成）以上，并且——按设置项 `pdf_image_page_rule`：
     - `coverage-and-chars`（默认）：这一页的字**同时**不到 `IMAGE_PAGE_MAX_CHARS`（200）个；
     - `coverage-only`：不管这一页有多少字。
没字也没图的是**空白页**，跳过（不送识别、也不算缺）。其余是**文字页**。

默认要求“图多**并且**字少”：教材大量页面是“一整页正文配一张插图”，这些页的正文文字层
已经全有了，只少了图上的标注字；“只看图”会把这些页也送去识别（飞行原理每页都铺着一张
整页底图，831 页全算图片页）。两种都给用户选，说明写在设置页。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

#: 一页的文字少于这么多个字符，算“几乎没字”（沿用此前“有文字层”的门槛）。
TEXT_PAGE_MIN_CHARS = 10
#: 图占页面面积的比例达到这么多，算“图多”（操作者 2026-10-01 定为两成）。
IMAGE_MIN_COVERAGE = 0.2
#: 默认规则下，“图多”的页还要字少于这么多个，才算图片页。
IMAGE_PAGE_MAX_CHARS = 200
#: 每一页文字都完全相同、且不超过这么多字符，就是水印/印章（见 `is_repeated_watermark`）。
WATERMARK_MAX_CHARS = 40

RULE_COVERAGE_AND_CHARS = "coverage-and-chars"
RULE_COVERAGE_ONLY = "coverage-only"
IMAGE_PAGE_RULES = (RULE_COVERAGE_AND_CHARS, RULE_COVERAGE_ONLY)
DEFAULT_IMAGE_PAGE_RULE = RULE_COVERAGE_AND_CHARS

TEXT, IMAGE, BLANK = "text", "image", "blank"


@dataclass(frozen=True)
class PageInfo:
    chars: int  # 这一页文字层去掉首尾空白后的字符数
    image_coverage: float  # 图占页面面积的比例（0～1，多张图的面积相加，封顶 1）
    has_image: bool
    text: str  # 文字层原文（只用来认水印）


def classify(info: PageInfo, rule: str = DEFAULT_IMAGE_PAGE_RULE) -> str:
    """一页是 TEXT / IMAGE / BLANK（规则见模块 docstring）。未知的 rule 按默认处理。"""
    if info.chars < TEXT_PAGE_MIN_CHARS:
        return IMAGE if info.has_image else BLANK
    if info.image_coverage >= IMAGE_MIN_COVERAGE:
        if rule == RULE_COVERAGE_ONLY or info.chars < IMAGE_PAGE_MAX_CHARS:
            return IMAGE
    return TEXT


def page_info(page) -> PageInfo:
    text = page.get_text("text").strip()
    area = abs(page.rect) or 1.0
    covered = 0.0
    images = page.get_image_info()
    for item in images:
        covered += abs(pymupdf.Rect(item["bbox"]) & page.rect)
    return PageInfo(chars=len(text), image_coverage=min(1.0, covered / area), has_image=bool(images), text=text)


def is_repeated_watermark(page_texts: list[str]) -> bool:
    """每一页的文字（折叠空白后）都是同一句短话 -> 水印，不是正文。

    2026-09-29 操作者真机反馈 + 确认的规则（BC-01，**旧项目没有**）：扫描 App
    （如 CamScanner）会在每页盖一个文字水印，恰好达到“每页 >= 10 字符”的门槛
    （"CamScanner" 正好 10 个字符），整份扫描件被当成“文字层 PDF”，转出来只有水印。

    只在至少两页、且全部页面折叠空白后完全相同、长度不超过 `WATERMARK_MAX_CHARS` 时
    才判水印：只有一页时没有“每一页都一样”的证据。"""
    if len(page_texts) < 2:
        return False
    distinct = {" ".join(text.split()) for text in page_texts}
    return len(distinct) == 1 and len(next(iter(distinct))) <= WATERMARK_MAX_CHARS


@dataclass(frozen=True)
class PdfLayout:
    page_count: int
    has_text_layer: bool  # 至少有一页够字、且不是整本水印——没有就是整本扫描件
    kinds: tuple[str, ...]  # 逐页 TEXT / IMAGE / BLANK

    @property
    def image_pages(self) -> tuple[int, ...]:
        return tuple(index + 1 for index, kind in enumerate(self.kinds) if kind == IMAGE)


def inspect(path: Path, rule: str = DEFAULT_IMAGE_PAGE_RULE) -> PdfLayout:
    doc = pymupdf.open(path)
    try:
        infos = [page_info(page) for page in doc]
    finally:
        doc.close()
    kinds = tuple(classify(info, rule) for info in infos)
    enough_text = any(info.chars >= TEXT_PAGE_MIN_CHARS for info in infos)
    watermark = is_repeated_watermark([info.text for info in infos])
    return PdfLayout(page_count=len(infos), has_text_layer=enough_text and not watermark, kinds=kinds)


def write_page_range(src: Path, first: int, last: int, dest: Path) -> None:
    """把第 first～last 页（从 1 起，两端都含）另存成一份 PDF——编排层把图片页切出来送识别用。"""
    source = pymupdf.open(src)
    try:
        part = pymupdf.open()
        try:
            part.insert_pdf(source, from_page=first - 1, to_page=last - 1)
            part.save(str(dest))
        finally:
            part.close()
    finally:
        source.close()
