from __future__ import annotations

from core.contracts import SearchAdviceInput, SearchResult

PLUGIN_ID = "official-result-advisor"
STRONG_DEFAULT = 0.75
WARN_DEFAULT = 0.30
MAX_LINES = 2
NON_NOTE_HINTS = ("agents", "skills", "test")


def _norm_title(value: str) -> str:
    return "".join(str(value or "").split()).lower()


def _stem(path: str) -> str:
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _extension(path: str) -> str:
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _looks_keywordish(query: str) -> bool:
    value = (query or "").strip()
    if not value or "?" in value or "？" in value or len(value) > 12:
        return False
    return not any(word in value for word in ("怎么", "如何", "为什么", "哪些", "什么", "是否", "能不能"))


class ResultAdvisorPlugin:
    def __init__(self) -> None:
        self._settings = None

    def on_load(self, ctx):
        self._settings = ctx.settings
        ctx.logger.info("自适应结果建议已加载")

    def on_enable(self, ctx):
        ctx.logger.info("自适应结果建议已启用")

    def on_disable(self, ctx):
        ctx.logger.info("自适应结果建议已禁用")

    def on_unload(self, ctx):
        self._settings = None

    def advise(self, request: SearchAdviceInput) -> tuple[str, ...]:
        if self._settings is None or not request.results:
            return ()
        max_lines = max(0, int(self._settings.get("advice_max_lines", MAX_LINES)))
        if max_lines == 0:
            return ()
        hits: tuple[SearchResult, ...] = request.results
        scores = [result.confidence for result in hits]
        top = max(scores) if scores else None
        strong_count = sum(1 for score in scores if score >= request.strong_threshold)
        delivered = len(hits)
        output: list[str] = []

        if top is not None and top < request.warn_threshold:
            output.append(
                f"整批命中的相关度都偏低（最高 {top:.2f}）：以下结果仅供参考，库里可能没有直接答案。"
                "建议改用笔记里出现的原始术语重搜，或先传 include_body=false 枚举候选文件。"
            )
        elif strong_count == 1 and delivered > 1:
            output.append(
                f"只有 1 条真正相关（其余低于 {request.warn_threshold:.2f}）：请只引用这一条；"
                "要更多上下文可用 read_document 读该篇全文。"
            )
        elif top is not None and top < request.strong_threshold and strong_count == 0:
            output.append(
                f"最高一条属中相关（{top:.2f}）：沾边但不是直答。建议用 read_document 看它的完整上下文，"
                "或把问题问得更具体再搜。"
            )

        if len(output) < max_lines:
            by_title: dict[str, set[tuple[str, str]]] = {}
            for result in hits:
                title = result.heading_breadcrumb
                key = _norm_title(title if title and title != "(无标题)" else _stem(result.path))
                by_title.setdefault(key, set()).add((result.library_id, result.path))
            duplicate = next((paths for paths in by_title.values() if len(paths) > 1), None)
            if duplicate:
                rendered = "；".join(f"{path}（{library}）" for library, path in sorted(duplicate)[:2])
                output.append(
                    f"注意同名不同目录：「{_stem(sorted(duplicate)[0][1])}」下有 {rendered} 是两篇不同笔记，"
                    "引用与打开请用完整路径区分。"
                )

        if len(output) < max_lines and request.default_libraries:
            defaults = set(request.default_libraries)
            foreign = [
                library
                for library in dict.fromkeys(result.library_id for result in hits)
                if library not in defaults and any(hint in library.lower() for hint in NON_NOTE_HINTS)
            ]
            if foreign:
                output.append(
                    f"命中含非笔记库（{'、'.join(foreign)}）：那是 AI 配置/技能内容，不是你的笔记。"
                    f"只要笔记请传 libraries=\"{request.default_libraries[0]}\"。"
                )

        if len(output) < max_lines and top is not None and (
            strong_count >= 3
            or (strong_count >= 1 and top >= 0.90 and delivered >= request.top_k)
        ):
            output.append(
                f"多条高置信命中（{strong_count} 条 ≥{request.strong_threshold:.2f}，最高 {top:.2f}）"
                "说明该主题内容集中：把 top_k 调大（如 15~20）可拿到同一主题的更多小节与笔记。"
            )

        if len(output) < max_lines and delivered >= 3:
            per_file: dict[tuple[str, str], int] = {}
            for result in hits:
                key = (result.library_id, result.path)
                per_file[key] = per_file.get(key, 0) + 1
            (library, path), count = max(per_file.items(), key=lambda item: item[1])
            if count >= max(2, round(delivered * 0.6)):
                # 经操作者确认的偏离（2026-09-25）：旧项目 advice.py:148-150
                # 同一场景建议 `exclude="<文件路径>"`，但两边检索入口的 exclude
                # 实际语义都是库 ID（library.resolve_entries 对未知名直接
                # raise），AI 照做只会让整次检索报错——旧文案是继承的既有
                # bug。改为指向可执行的 read_document 精读入口。
                output.append(
                    f"命中集中在《{_stem(path)}》（{count}/{delivered} 条）："
                    f"想精读这篇可调 read_document(library_id=\"{library}\", path=\"{path}\")，"
                    "想横向比较其它笔记把 top_k 调大。"
                )

        if len(output) < max_lines and any(_extension(result.path) in {"pdf", "docx"} for result in hits):
            output.append(
                "命中含 PDF/Word：read_document 可拿已提取的 Markdown 全文，图表或扫描页内容用 navigate_knowledge 看页。"
            )

        if len(output) < max_lines and any(result.backfilled for result in hits):
            output.append(
                "命中正文已按小节回填、且同一小节只交付一次：要看原文全文用 read_document，"
                "要看它连到哪些笔记用 note_relations。"
            )

        if len(output) < max_lines and request.mode == "list":
            output.append("这是候选清单（只有来源行、无正文）：挑 1~3 条再 read_document 精读，不要把整清单都读进来。")

        if len(output) < max_lines and delivered <= 2 and (top is None or top >= request.warn_threshold):
            output.append(
                f"命中很少（{delivered} 条）：可去掉 folder/exclude 限制、把 libraries 放宽到默认库或 \"all\"，或换同义词再搜。"
            )

        if len(output) < max_lines and _looks_keywordish(request.query):
            output.append("关键词式查询命中精确但覆盖窄：若想问\"怎么做/为什么\"这类，换成完整问句（一句自然语言的问题）结果会更全。")

        return tuple(output[:max_lines])
