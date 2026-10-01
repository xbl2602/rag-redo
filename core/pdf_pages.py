"""PDF 按页分流的纯规则：图片页连成段、长段切开、识别结果按页码拼回（BC-01，2026-10-01）。

操作者 2026-10-01 的决定：一本书里文字页和图片页混着时，**文字页直接用文字层转，只把图片页
切出来送识别，再按页码拼回原来的位置**——不再因为有一页没字就整本送识别（厚教材因此撞上
本机识别 200 页的上限、整本一个字都进不了索引）。图片页送不了识别（没开识别、超过页数上限、
识别服务出错）时，文字页照转，图片页先用它上面仅有的几个字，并把这些页记下来，条件变了
下一轮自动补识别。

这里只有纯计算（不碰文件、不调插件），编排在 `core/pipeline.py`；“一页算不算图片页”由
文字层提取器插件判定（official-extractor-pdf-text），这里只拿它给的页码。
"""
from __future__ import annotations

#: 相邻两段图片页之间只隔这么几页文字页时，并成一段一起送识别（中间的文字页也一并识别，
#: 结果以识别的为准）——少发几次请求，识别出来的内容不会比文字层少。
MERGE_GAP_PAGES = 2

#: 图片页没能识别的原因代码（写进索引清单与转换缓存清单，人话在 core/conversion_cache.py）。
MISSING_OCR_OFF = "ocr-off"  # 没开扫描件识别
MISSING_TOO_MANY_PAGES = "too-many-pages"  # 要识别的页超过本机上限，又没开“超过上限送云端”
MISSING_OCR_DEFERRED = "ocr-deferred"  # 识别服务暂时不可用：下一轮自动再试
MISSING_OCR_FAILED = "ocr-failed"  # 识别出错：条件变了（换后端、补 Key……）才再试
MISSING_REASONS = (MISSING_OCR_OFF, MISSING_TOO_MANY_PAGES, MISSING_OCR_DEFERRED, MISSING_OCR_FAILED)
#: 这几种原因不用等条件变化，下一轮就再试一次（与失败终态里的 deferred 同一语义，BC-04）。
RETRY_EVERY_ROUND = frozenset({MISSING_OCR_DEFERRED})

Run = tuple[int, int]  # （第一页, 最后一页），页码从 1 起，两端都含


def page_runs(pages: tuple[int, ...] | list[int], merge_gap: int = MERGE_GAP_PAGES) -> list[Run]:
    """把要识别的页码连成段：连续的并在一起，中间只隔不超过 `merge_gap` 页的也并在一起。"""
    runs: list[Run] = []
    for page in sorted(set(int(p) for p in pages if int(p) >= 1)):
        if runs and page - runs[-1][1] <= merge_gap + 1:
            runs[-1] = (runs[-1][0], page)
        else:
            runs.append((page, page))
    return runs


def split_runs(runs: list[Run], max_pages: int | None) -> list[Run]:
    """一次请求最多 `max_pages` 页（云端每份最多 200 页）：超长的段按顺序切成几段。"""
    if not max_pages or max_pages <= 0:
        return list(runs)
    out: list[Run] = []
    for first, last in runs:
        start = first
        while start <= last:
            end = min(last, start + max_pages - 1)
            out.append((start, end))
            start = end + 1
    return out


def run_page_count(runs: list[Run]) -> int:
    return sum(last - first + 1 for first, last in runs)


def splice_pages(
    page_texts: tuple[str, ...],
    image_pages: tuple[int, ...],
    run_texts: dict[Run, str | None],
) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    """按页码把识别结果拼回原位，返回（整本正文, 由识别补上的页, 没能识别的图片页）。

    `run_texts` 里某一段是 None 表示这段没识别出来：这段的页照旧用文字层（`page_texts`），
    其中的图片页记为“没识别”（段里顺带并进来的文字页不算缺）。识别出来的段整段替换掉这些页
    的文字层——识别结果里已经有这些页的全部内容。"""
    total = len(page_texts)
    wanted = set(image_pages)
    starts = {first: (first, last) for first, last in run_texts}
    pieces: list[str] = []
    recognized: list[int] = []
    missing: list[int] = []
    page = 1
    while page <= total:
        run = starts.get(page)
        if run is None:
            pieces.append(page_texts[page - 1])
            page += 1
            continue
        first, last = run
        last = min(last, total)
        text = run_texts[run]
        if text is not None and text.strip():
            pieces.append(text.rstrip("\n") + "\n\n")
            recognized.extend(range(first, last + 1))
        else:
            pieces.extend(page_texts[first - 1 : last])
            missing.extend(p for p in range(first, last + 1) if p in wanted)
        page = last + 1
    return "".join(pieces), tuple(recognized), tuple(missing)


def missing_reason_for(failures: list[str]) -> str:
    """几段识别失败的原因合成一个：有一段是“暂时不可用”就按暂时不可用（下一轮就再试）。"""
    if any(state == "deferred" for state in failures):
        return MISSING_OCR_DEFERRED
    return MISSING_OCR_FAILED
