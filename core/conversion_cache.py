"""core/conversion_cache.py — “转换缓存”清单（BC-19）。

**补的是哪个坑**：2026-09-30 操作者提出，用户和开发者都没法一眼确认“PDF/Word 到底转成文字
了没有、WEMM 页库到底建了没有、存在哪、多大”。两份缓存本来都在——转文字缓存由核心写进
`data/extracted/<库>/<generation>/<路径指纹>.<提取器>.txt`（`core/extract_cache.py`），页库由
`visual_index` 插件写进它自己的数据库——但文件名是一串指纹，资源管理器里认不出哪个是哪个；
页库只在诊断页最下面有张要选库、要往下翻的表。

**这个模块只做“读出来、摆在一起”**：输入是清单记录、正文缓存的实际位置、页库逐 PDF 状态，
输出 `ConversionCacheLibrary`。不改任何存法，不触发任何转换，不加载任何模型。界面（库卡片、
诊断页清单、文件详情）、命令行、MCP 工具、每轮日志和缓存文件夹里的 `缓存目录.md` 全部吃这一份
结果；“缺了是什么原因、下一步怎么办”的人话也只在这里写一份（`reason_text`），入口不各写各的。
"""
from __future__ import annotations

import time
import urllib.parse
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .contracts import (
    ConversionCacheFile,
    ConversionCacheLibrary,
    ConversionRoundSummary,
    VisualPageState,
)

#: 不需要“转换”的纯文字格式：正文就是源文件本身，没有转文字缓存，也不进清单。
#: 与索引里“纯文本格式不进转换暂存”是同一个判断（`core/pipeline.py` 复用本常量）。
PLAIN_TEXT_EXTENSIONS = frozenset({"md", "txt", "markdown"})

#: 清单结构版本：`ConversionCacheLibrary` 的字段含义变了就升，入口据此判断能不能直接用。
CONVERSION_CACHE_REPORT_VERSION = "1"

#: 缓存文件夹里那份人能看懂的目录（每个库一份，放在该库转文字缓存文件夹的根上）。
CATALOG_FILE_NAME = "缓存目录.md"

#: 清单记录里“还在等、会自动补上”的两种失败终态：扫描件等文字识别、转换服务这轮没起来。
_TEXT_PENDING_STATES = frozenset({"scanned", "deferred"})

#: 原因代码 → （一句话说明，下一步怎么办）。界面、命令行、目录文件、MCP 工具都用这一份。
_REASONS: dict[str, tuple[str, str]] = {
    "not-indexed": (
        "还没索引到",
        "新加的文件或索引还没跑完：等下一轮自动同步，或在库页点「增量更新」。",
    ),
    "scanned": (
        "扫描件，等文字识别",
        "这份没有文字层：在设置里开启 MinerU（本机或云端）后，下一轮会自动转；已经开着就等下一轮。",
    ),
    "deferred": (
        "转换服务这轮没起来",
        "MinerU 这一轮没能用上，下一轮会自动重试；一直这样就去诊断页看 MinerU 的日志。",
    ),
    "extract-failed": (
        "转换失败",
        "多半是当时的环境问题（缺 Key、额度用完、网络断了、本机解析出错），修好后下一轮自动重试。",
    ),
    "unreadable": (
        "文件打不开",
        "文件可能损坏或加了密码：换一份能正常打开的文件，下一轮会自动重新转。",
    ),
    "empty": (
        "转出来是空的",
        "文件里没有能提取的文字；如果它其实是扫描件，开启 MinerU 后会自动重试。",
    ),
    "tbd": (
        "占位内容太多",
        "文件里大部分是 [TBD] 之类的占位，按设置跳过；补上内容后会自动重新转。",
    ),
    "cache-missing": (
        "正文文件不见了",
        "索引记着已经转好，但缓存文件被删了或挪走了：在库页点「全量重建」重新转一遍。",
    ),
    "pages-off": (
        "页库没开",
        "在设置里打开「页级视觉导航（WEMM）」，下一轮索引会自动为 PDF 建页库。",
    ),
    "pages-not-built": (
        "页库还没建",
        "等下一轮索引自动建；页库开着却一直不建，去诊断页看页库服务的状态。",
    ),
    "pages-partial": (
        "有页面没编上",
        "缺的页下一轮会自动重试；一直缺，去诊断页看页库服务的日志。",
    ),
    "pages-failed": (
        "页库没建成",
        "常见原因是显存不够或页库服务没起来：关掉占显卡的程序，或在设置里开「强制加载」，下一轮自动重试。",
    ),
}


#: 全部原因代码（测试据此确认“人话只在核心写一份”，前端、命令行不各写各的）。
REASON_CODES: tuple[str, ...] = tuple(_REASONS)


def reason_text(code: str | None) -> tuple[str, str]:
    """原因代码 → （一句话说明，下一步怎么办）。不认识的代码原样返回、下一步留空。"""
    if not code:
        return "", ""
    return _REASONS.get(code, (code, ""))


def needs_conversion(path: str) -> bool:
    """这份文件要不要“转文字”：不是纯文字格式的都算（现在是 PDF、Word）。"""
    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return bool(extension) and extension not in PLAIN_TEXT_EXTENSIONS


def _failure_code(record: Mapping[str, object]) -> str:
    """清单记录里的失败状态 → 稳定原因代码（与图谱读模型同一套判断口径）。"""
    for key in ("failure_state", "failure_reason"):
        value = str(record.get(key) or "").strip().lower()
        if value in _REASONS:
            return value
    return "extract-failed"


def _text_entry(
    path: str,
    record: Mapping[str, object] | None,
    locate_text: Callable[[str], Path | None],
    route_names: Mapping[str, str],
) -> dict:
    if record is None:
        return {"text_state": "pending", "text_reason": "not-indexed"}
    if record.get("status") != "indexed":
        code = _failure_code(record)
        return {"text_state": "pending" if code in _TEXT_PENDING_STATES else "failed", "text_reason": code}
    located = locate_text(path)
    if located is None:
        return {"text_state": "missing", "text_reason": "cache-missing"}
    try:
        stat = located.stat()
    except OSError:
        return {"text_state": "missing", "text_reason": "cache-missing"}
    route = str(record.get("extractor_id") or "") or None
    return {
        "text_state": "done",
        "text_route": route,
        "text_route_version": str(record.get("extractor_version") or "") or None,
        "text_route_name": route_names.get(route or "") or route,
        "text_file": str(located),
        "text_bytes": int(stat.st_size),
        "text_updated": float(stat.st_mtime),
    }


def _pages_entry(
    extension: str,
    state: VisualPageState | None,
    pages_enabled: bool,
    generation: str | None,
) -> dict:
    if extension != "pdf":
        return {"pages_state": "n/a"}
    pages = tuple(sorted({page for page in (state.pages if state else ()) if page > 0}))
    common = {
        "pages": pages,
        "page_count": state.page_count if state is not None else None,
        "pages_rebuilt": bool(state is not None and generation and state.built_in == generation),
    }
    if not pages_enabled:
        return {"pages_state": "off", "pages_reason": "pages-off", **common}
    if state is None:
        return {"pages_state": "none", "pages_reason": "pages-not-built", **common}
    detail = state.failure_reason or None
    if state.status == "indexed" and pages:
        return {"pages_state": "done", **common}
    if state.status in {"indexed", "partial"} and pages:
        return {"pages_state": "partial", "pages_reason": "pages-partial", "pages_detail": detail, **common}
    return {"pages_state": "failed", "pages_reason": "pages-failed", "pages_detail": detail, **common}


def build_library_report(
    *,
    library_id: str,
    name: str,
    included_files: Iterable[tuple[str, bool, str]],
    records: Mapping[str, Mapping[str, object]],
    locate_text: Callable[[str], Path | None],
    route_names: Mapping[str, str],
    page_states: Iterable[VisualPageState],
    pages_enabled: bool,
    generation: str | None,
    text_dir: Path,
    pages_dir: str | None = None,
    bytes_per_page: int = 0,
    page_vram_gb: float | None = None,
    page_idle_unload_seconds: int | None = None,
) -> ConversionCacheLibrary:
    """一个库的转换缓存清单。

    `included_files` 必须是库管理器的权威枚举（`resolve_included_files`，§5：所有界面状态复用
    同一个文件枚举入口），只列用户纳入索引的、需要转换的文件；`records` 是当前生效 generation
    的清单记录；`locate_text(path)` 返回这份文件正文缓存的**实际**位置（与读正文走同一套查找）；
    `page_states` 是页库插件报的逐 PDF 状态，多个提供者时按提供者 id 先到先得。"""
    states: dict[str, VisualPageState] = {}
    for state in sorted(page_states, key=lambda item: (item.path, item.provider_id)):
        if state.library_id == library_id:
            states.setdefault(state.path, state)
    files: list[ConversionCacheFile] = []
    for path, included, _reason in sorted(included_files, key=lambda item: item[0]):
        if not included or not needs_conversion(path):
            continue
        extension = path.rsplit(".", 1)[-1].lower()
        files.append(
            ConversionCacheFile(
                path=path,
                extension=extension,
                **_text_entry(path, records.get(path), locate_text, route_names),
                **_pages_entry(extension, states.get(path), pages_enabled, generation),
            )
        )
    page_vectors = sum(len(item.pages) for item in files if item.pages_state in {"done", "partial", "off"})
    return ConversionCacheLibrary(
        library_id=library_id,
        name=name,
        files=tuple(files),
        text_dir=str(text_dir),
        catalog_file=str(text_dir / CATALOG_FILE_NAME),
        pages_enabled=pages_enabled,
        pages_dir=pages_dir,
        page_bytes_estimate=page_vectors * max(0, int(bytes_per_page)),
        page_vram_gb=page_vram_gb,
        page_idle_unload_seconds=page_idle_unload_seconds,
        report_version=CONVERSION_CACHE_REPORT_VERSION,
    )


def round_summary(report: ConversionCacheLibrary, fresh_paths: Iterable[str]) -> ConversionRoundSummary:
    """一轮索引的“复用多少、新转多少、还缺多少”。`fresh_paths` 是这一轮真的跑了提取器的文件
    （沿用正文缓存、沿用转换暂存的都不算新转——它们没有再花 MinerU 的时间和额度）。"""
    fresh = set(fresh_paths)
    done = [item for item in report.files if item.text_state == "done"]
    text_new = sum(1 for item in done if item.path in fresh)
    built = [item for item in report.files if item.pages_state in {"done", "partial"}]
    pages_new = sum(1 for item in built if item.pages_rebuilt)
    return ConversionRoundSummary(
        text_reused=len(done) - text_new,
        text_new=text_new,
        text_missing=report.text_total - len(done),
        pages_enabled=report.pages_enabled,
        pages_reused=(len(built) - pages_new) if report.pages_enabled else 0,
        pages_new=pages_new if report.pages_enabled else 0,
        pages_missing=(report.pdf_total - len(built)) if report.pages_enabled else 0,
    )


def format_round_summary(summary: ConversionRoundSummary) -> str:
    """每轮日志里的那一行。"""
    text = f"转文字 复用 {summary.text_reused} / 新转 {summary.text_new} / 缺 {summary.text_missing}"
    if summary.pages_enabled:
        pages = f"页库 复用 {summary.pages_reused} / 新建 {summary.pages_new} / 缺 {summary.pages_missing}"
    else:
        pages = "页库 没开"
    return f"转换缓存：{text}；{pages}"


def needs_attention(item: ConversionCacheFile) -> bool:
    """“只看缺的”算不算这一份：正文没转好，或者页库开着却没建全。页库没开不算缺——
    那是用户自己的选择，库卡片上另有一句“页库 未开启”。"""
    return item.text_state != "done" or item.pages_state in {"none", "partial", "failed"}


def human_bytes(size: int) -> str:
    """字节数 → “84 KB”“3.4 MB”这种人看的写法。"""
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.0f} GB"  # pragma: no cover - 循环里已经返回


def page_ranges(pages: Iterable[int]) -> str:
    """页码 → “1–28、31–36”。"""
    ordered = sorted({int(page) for page in pages if int(page) > 0})
    parts: list[str] = []
    index = 0
    while index < len(ordered):
        start = end = ordered[index]
        while index + 1 < len(ordered) and ordered[index + 1] == end + 1:
            index += 1
            end = ordered[index]
        parts.append(str(start) if start == end else f"{start}–{end}")
        index += 1
    return "、".join(parts)


def missing_pages(item: ConversionCacheFile) -> tuple[int, ...]:
    """知道总页数时，哪几页没进页库。总页数未知（旧记录）就是空元组——不猜。"""
    if not item.page_count:
        return ()
    have = set(item.pages)
    return tuple(page for page in range(1, item.page_count + 1) if page not in have)


def pages_summary(item: ConversionCacheFile) -> str:
    """“28/36 页”或“28 页”（总页数未知时）。"""
    if item.page_count:
        return f"{len(item.pages)}/{item.page_count} 页"
    return f"{len(item.pages)} 页"


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_catalog(report: ConversionCacheLibrary, *, library_root: str, now: float | None = None) -> str:
    """缓存文件夹里的 `缓存目录.md`：一张人能看懂的对照表。

    转文字缓存的文件名是原路径的指纹，资源管理器里认不出；这张表把“原文件 ↔ 缓存文件”对上，
    并写明谁转的、多大、哪天、页库几页。链接是相对本文件的路径（百分号编码，文件名里的 `%3A`
    不会被阅读器误解成冒号）。"""
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() if now is None else now))
    text_dir = Path(report.text_dir)
    lines = [
        f"# 转换缓存目录 · {report.name}",
        "",
        f"> 自动生成，每轮索引完成后刷新（手动改动会被覆盖）。生成时间：{stamp}",
        "> 转文字缓存的文件名是原文件路径的指纹，下表告诉你每个缓存文件对应哪份原文件。",
        "",
        f"- 库目录：`{library_root}`",
        f"- 转文字：{report.text_done}/{report.text_total} 份已转好，共 {human_bytes(report.text_bytes)}（就在本文件夹里）",
    ]
    if report.pages_enabled:
        where = f"，存在 `{report.pages_dir}`" if report.pages_dir else ""
        lines.append(
            f"- 页库：{report.pages_done}/{report.pdf_total} 份 PDF 已建，共 {report.page_vectors} 页，"
            f"约 {human_bytes(report.page_bytes_estimate)}{where}（页向量存在数据库里，大小只能估）"
        )
    else:
        lines.append("- 页库：没开（设置里打开「页级视觉导航（WEMM）」后下一轮索引自动建）")
    lines += [
        "",
        "| 原文件 | 转文字 | 谁转的 | 大小 | 更新时间 | 缓存文件 | 页库 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in report.files:
        if item.text_state == "done":
            text_cell = "✅"
            route = item.text_route_name or item.text_route or ""
            if item.text_route_version:
                route = f"{route} {item.text_route_version}"
            size = human_bytes(item.text_bytes)
            updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.text_updated or 0))
            link = ""
            if item.text_file:
                try:
                    relative = Path(item.text_file).relative_to(text_dir).as_posix()
                except ValueError:
                    relative = Path(item.text_file).as_posix()
                link = f"[打开](<{urllib.parse.quote(relative, safe='/')}>)"
        else:
            text_cell = "⚠️ " + reason_text(item.text_reason)[0]
            route = size = updated = link = ""
        if item.pages_state == "n/a":
            pages_cell = "—"
        elif item.pages_state == "done":
            pages_cell = "✅ " + pages_summary(item)
        elif item.pages_state == "partial":
            pages_cell = "⚠️ " + pages_summary(item)
        else:
            pages_cell = "⚠️ " + reason_text(item.pages_reason)[0]
        lines.append(
            "| "
            + " | ".join(_md_cell(cell) for cell in (item.path, text_cell, route, size, updated, link, pages_cell))
            + " |"
        )
    if not report.files:
        lines.append("| （这个库里没有需要转换的文件） |  |  |  |  |  |  |")
    return "\n".join(lines) + "\n"
