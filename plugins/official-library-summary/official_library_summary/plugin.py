"""official-library-summary 插件：库摘要的存储 + 写权限门禁 + prompt拼装。

**AI 生成的简介随便覆盖，用户手写的简介需要走写权限门禁确认**——
source=none/ai 时 propose() 直接写入生效；source=user 时必须先
propose()（拿到核心 write_gate 的提案号+确认码）、用户明确同意后再
apply()，这是 AGENTS.md"两个核心组件"一节"Agent 写权限门禁"服务的
第一个真实调用方（此前 core/write_gate.py 机制已实现但没有任何插件
真的调用过，见 docs/ROADMAP.md Phase 3 状态），移植自旧项目
summary_gate.py 验证过的策略（同 selection_gate.py，提案号+确认码+
TTL+一次性，两个门禁互不依赖各管各的数据面）。

**采样/生成不在这个插件里**：representative chunk 采样是 vector_store
的事（见 official-vector-store-chroma::sample），调 LLM 是 llm_provider
的事（见 official-llm-openai-compatible），这个插件既不碰向量库也不
发网络请求——只知道"prompt该怎么拼、简介该怎么存、覆盖用户手写内容
需要走门禁"，三方协调只在 core/pipeline.py 发生（架构红线1），和
official-visual-wemm/official-import-export 是同一种"编排层知道顺序、
插件互不知情"的模式。

**两条真实存在的生成路径**（对齐调查到的旧项目 obsidian-rag 行为，
server.py 的注释原话："生成绝不自动触发——只能由用户在GUI点『刷新简介』，
或在对话里向你明确提出请求后，你才调用下面这两个写入工具"）：
①MCP对话路径——AI agent 自己（不是另外调一次LLM）用 core/pipeline.py::
sample_library() 拿到的采样片段自己写一段话，再调用这个插件的 propose()
提交，省一次LLM调用；②GUI"刷新简介"按钮路径——没有对话中的agent可以
代笔，走 core/pipeline.py::generate_library_summary()，真的调一次配置好
的 llm_provider。这个插件本身对这两条路径一视同仁，不知道调用方是哪一种。

**内容指纹的简化**：旧项目 library_summary.py::content_fingerprint 聚合
"全部已索引文件的相对路径:md5"来判断简介是否可能已过时；rag-redo 目前
没有中心化暴露"某个库全部文件当前哈希"的查询接口，这里用"本次采样到的
代表片段内容"算指纹作为务实的代理信号——库内容变化到足以影响最远点
采样结果时，指纹大概率也会跟着变，效果上达到同样的"提示可能已过时"
目的，但不是与旧项目完全等价的算法，不需要为了这一处单独设计一套新的
跨插件"库全量内容哈希"查询契约——如实记录这个简化，不假装是同一个算法。
"""
from __future__ import annotations

import hashlib

from core.contracts import LibrarySummary, SampledChunk
from core.write_gate import WriteGateError

from .prompt import INSTRUCTIONS, build_prompt
from .summary_store import SUMMARY_MAX_CHARS, SummaryStore

PLUGIN_ID = "official-library-summary"


def _content_fingerprint(samples: list[SampledChunk]) -> str:
    parts = sorted(f"{s.path}:{s.heading}:{hashlib.sha256(s.text.encode('utf-8')).hexdigest()[:12]}" for s in samples)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


class LibrarySummaryPlugin:
    def __init__(self) -> None:
        self._store: SummaryStore | None = None
        self._write_gate = None
        self._logger = None

    def on_load(self, ctx):
        self._store = SummaryStore(
            ctx.storage.directory("library_summary", legacy="library_summary")
            / "summaries.json"
        )
        self._write_gate = ctx.write_gate
        self._logger = ctx.logger
        ctx.logger.info("库摘要插件已加载")

    def on_enable(self, ctx):
        ctx.logger.info("库摘要插件已启用")

    def on_disable(self, ctx):
        ctx.logger.info("库摘要插件已禁用")

    def on_unload(self, ctx):
        self._store = None

    # ---- 读 ----------------------------------------------------------

    def get(self, library_id: str) -> LibrarySummary:
        assert self._store is not None
        entry = self._store.get(library_id)
        return LibrarySummary(library_id=library_id, **entry)

    def is_stale(self, library_id: str, current_fingerprint: str) -> bool:
        summary = self.get(library_id)
        if not summary.fingerprint:
            return False
        return summary.fingerprint != current_fingerprint

    # ---- prompt / 指纹（纯函数，供 core/pipeline.py 编排 llm_provider 调用前后使用）-------

    def build_prompt(self, library_name: str, samples: list[SampledChunk]) -> tuple[str, str]:
        return INSTRUCTIONS, build_prompt(library_name, samples)

    def content_fingerprint(self, samples: list[SampledChunk]) -> str:
        return _content_fingerprint(samples)

    def finalize_text(self, text: str) -> str:
        return text.strip()[:SUMMARY_MAX_CHARS]

    # ---- 写（AI生成随便覆盖，用户手写需要门禁确认）-----------------------

    def propose(self, library_id: str, text: str, *, model: str | None = None, fingerprint: str | None = None) -> dict:
        assert self._store is not None and self._write_gate is not None
        text = text.strip()
        if not text:
            return {"ok": False, "error": "简介文本不能为空"}
        if len(text) > SUMMARY_MAX_CHARS:
            return {"ok": False, "error": f"简介超长（{len(text)} 字，上限 {SUMMARY_MAX_CHARS} 字）：请精简后再提案"}

        current = self.get(library_id)
        if current.source != "user":
            entry = self._store.set(library_id, text, source="ai", fingerprint=fingerprint, model=model or "agent")
            self._logger.info("库简介已直接写入（库=%s，此前非用户手写）", library_id)
            return {"ok": True, "applied": True, "text": entry["text"]}

        ticket = self._write_gate.propose(
            f"覆盖库「{library_id}」用户手写的简介",
            {"library_id": library_id, "text": text, "fingerprint": fingerprint, "model": model or "agent"},
        )
        self._logger.info("库简介覆盖提案已生成（库=%s，提案=%s）——等待用户确认", library_id, ticket.proposal_id)
        return {
            "ok": True,
            "applied": False,
            "proposal_id": ticket.proposal_id,
            "confirmation_code": ticket.confirmation_code,
            "text": text,
        }

    def set_direct(self, library_id: str, text: str, *, source: str = "user", fingerprint: str | None = None, model: str | None = None) -> dict:
        """无条件写入，不经过写权限门禁——给"人类直接操作"这条路径用
        （同 core/write_gate.py 模块 docstring"人类在GUI里直接操作不走
        这个门禁，无条件生效"，移植自旧项目 guiweb/bridge.py::
        set_library_summary 的行为）。propose()/apply() 才是"AI经MCP对话
        想覆盖用户已写内容"那条受写权限门禁保护的路径，这个方法是完全
        独立的另一个入口，不是内部实现细节抄近路。GUI"刷新简介"按钮
        （调一次真实 llm_provider）也走这个方法写盘，同样不经过门禁——
        旧项目里"是否需要用户二次确认"在那条路径上是调用方（GUI）自己
        先探测 source=="user" 再决定要不要传 force，不是这个方法的责任。"""
        assert self._store is not None
        text = text.strip()
        if not text:
            return {"ok": False, "error": "简介文本不能为空"}
        if len(text) > SUMMARY_MAX_CHARS:
            return {"ok": False, "error": f"简介超长（{len(text)} 字，上限 {SUMMARY_MAX_CHARS} 字）：请精简后再写入"}
        entry = self._store.set(library_id, text, source=source, fingerprint=fingerprint, model=model)
        self._logger.info("库简介已直接写入（库=%s，来源=%s，未经写权限门禁）", library_id, source)
        return {"ok": True, "text": entry["text"]}

    def apply(self, library_id: str, proposal_id: str, confirmation_code: str) -> dict:
        assert self._store is not None and self._write_gate is not None
        try:
            payload = self._write_gate.confirm(proposal_id, confirmation_code)
        except WriteGateError as exc:
            self._logger.warning("AUDIT 库简介提案被拒（库=%s，提案=%s）：%s", library_id, proposal_id, exc)
            return {"ok": False, "error": str(exc)}
        if payload.get("library_id") != library_id:
            return {"ok": False, "error": "提案与库名不匹配"}
        entry = self._store.set(
            library_id, payload["text"], source="ai", fingerprint=payload.get("fingerprint"), model=payload.get("model")
        )
        self._logger.info("AUDIT 库简介已生效（库=%s，提案=%s，经用户确认）", library_id, proposal_id)
        return {"ok": True, "text": entry["text"]}
