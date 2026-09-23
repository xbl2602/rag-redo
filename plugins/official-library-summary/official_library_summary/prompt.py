"""库简介的写作规范 + prompt 拼装——纯函数，零副作用，对齐旧项目
library_summary.py 的 `_PROMPT_INSTRUCTIONS`/`build_prompt`（同一套写作
要求，逐字迁移，不重新发明"简介该怎么写"这个已经调过的产品判断）。

`INSTRUCTIONS` 单独成一段、不掺进 `build_prompt` 拼出来的内容里——旧
项目原文说明：这样本地 llama.cpp/LM Studio 这类服务端的 prompt 前缀
缓存能安全地把这段不变的规范缓存住，只对每次都不同的库名/片段内容
重新计算，批量连续刷新多个库时能明显提速，调用方（core/pipeline.py
编排 llm_provider 调用时）应该把这两个值分别作为 system/user 两条
消息发送，不要拼成一条。
"""
from __future__ import annotations

from core.contracts import SampledChunk

from .summary_store import SUMMARY_MAX_CHARS

INSTRUCTIONS = (
    "你在给一个个人知识库写一段简介，供另一个 AI agent 在检索前快速判断"
    '"这个库大致讲什么、值不值得往这查"。这段话必须同时做到两件事——'
    "先画范围、再给判断依据，不能只做其中一个：\n"
    "1. 先用一两句给出总体定位（这库大致是什么性质/服务于什么），再概括库内"
    '主要覆盖哪几类主题或板块（口语化提及即可，比如"主要是……和……"，'
    "不要用编号或项目符号罗列成清单）；\n"
    '2. 接着明确写清楚"适合来这库查什么类型的问题"，以及"大概率查不到'
    '什么"（正反两面都要有），直接服务于 agent "值不值得查"这个决策，'
    "而不是让 agent 看完主题范围自己再去猜；\n"
    "3. 禁止逐字摘抄下面给的片段原文，必须用自己的话概括转写；\n"
    "4. 不点名任何一篇具体笔记的细节，只讲库整体范围；\n"
    f"5. 100~{SUMMARY_MAX_CHARS} 字，一段话，不用 markdown、不分点、不用标题；\n"
    "6. 直接输出这段简介正文，不要任何解释、引导语或包装。"
)


def build_prompt(library_name: str, samples: list[SampledChunk]) -> str:
    files = sorted({s.path for s in samples if s.path})
    lines = [
        f"库名：{library_name}",
        f"涉及文件（部分，共 {len(files)} 份采样命中）：" + "、".join(files[:15]),
        "",
        "代表性片段（仅供你概括主题用，不要摘抄）：",
    ]
    for s in samples:
        head = s.heading
        lines.append(f"- [{s.path}{(' · ' + head) if head else ''}] {s.text}")
    return "\n".join(lines)
