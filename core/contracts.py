"""核心契约：跨插件传递的数据类型，唯一权威定义。

DATA_FLOW.md 规则3："跨插件传递的数据格式只能用核心 contracts 模块里定义好
的类型，不能私下发明字段名口头约定"——这个模块就是那份唯一权威定义。插件
之间不能靠字符串 key/裸 dict 传数据，必须是这里定义的类型的实例。两份文档
（本模块 docstring 和 docs/DATA_FLOW.md）如果说法不一致，以本模块为准。

贯穿全部契约的约定：
- 全部是 frozen dataclass（不可变）——管道是单向数据流，没有谁应该回头改
  上一阶段已经产出的数据
- 数据类数据带 `*_by` / `*_version` 字段记录"谁、哪个版本产出的"，配合
  docs/LESSONS.md 第3条，为将来"插件版本变了要不要重算"的判断留钩子
"""
from __future__ import annotations

from dataclasses import dataclass


# ---- 选库阶段 ------------------------------------------------------------


@dataclass(frozen=True)
class LibrarySelection:
    """library_manager 插件对某一个文件的裁决结果——唯一权威判定，其他插件
    只能读这个结果，不能自己猜（DATA_FLOW.md 规则4）。"""

    library_id: str
    path: str  # 相对库根目录的路径，正斜杠分隔
    included: bool
    reason: str  # 人可读的裁决理由，即便 included=True 也要说清楚"为什么"


# ---- 抽取阶段 ------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedDocument:
    """extractor:<ext> 插件的输出。失败一律折叠成 text=None + failure_reason，
    绝不让提取失败变成异常传播出去（继承旧项目"extractor 绝不抛异常"的教训，
    见 docs/LESSONS.md 第1条）。"""

    library_id: str
    path: str
    text: str | None
    failure_reason: str | None  # text is None 时必须有值；text 有值时应为 None
    extracted_by: str  # 插件 id，例如 "official-extractor-pdf-text"
    extractor_version: str
    content_hash: str  # 源文件内容指纹，供增量判断是否需要重新抽取

    def __post_init__(self) -> None:
        if (self.text is None) == (self.failure_reason is None):
            raise ValueError(
                "ExtractedDocument: text 和 failure_reason 必须恰好一个有值——"
                "不允许两个都为 None（假装成功却没内容）或两个都有值（既失败又声称有内容）"
            )


# ---- 切块阶段 ------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """chunker 插件的输出，一个文档可以产出多个 Chunk。"""

    chunk_id: str  # 全局唯一，约定 f"{library_id}:{path}:{chunk_index}"
    library_id: str
    path: str
    chunk_index: int
    total_chunks: int
    text: str
    heading_breadcrumb: str  # 所在标题层级面包屑，方便检索结果展示来源
    chunked_by: str
    chunker_version: str


# ---- 向量化 / 词法化阶段 ----------------------------------------------------


@dataclass(frozen=True)
class EmbeddingVector:
    chunk_id: str
    vector: tuple[float, ...]
    model_id: str  # 插件 id，例如 "official-embedder-bge-m3"
    model_version: str
    dim: int

    def __post_init__(self) -> None:
        if len(self.vector) != self.dim:
            raise ValueError(f"EmbeddingVector: 声明维度 {self.dim} 与实际向量长度 {len(self.vector)} 不符")


@dataclass(frozen=True)
class LexicalEntry:
    chunk_id: str
    tokens: tuple[str, ...]
    indexed_by: str
    indexer_version: str


# ---- 查询阶段 ------------------------------------------------------------


@dataclass(frozen=True)
class SearchQuery:
    text: str
    library_ids: tuple[str, ...] | None = None  # None = 全部已启用的库
    top_k: int = 10


@dataclass(frozen=True)
class ScoredChunk:
    """检索/融合/重排各阶段共用的"一个块+一个分数"载体。score 的含义随
    stage 变化（lexical 分数 / 向量相似度 / RRF 融合分数 / 重排分数），
    用 stage 字段区分，不为每个阶段发明一个新类型名徒增复杂度。"""

    chunk_id: str
    score: float
    stage: str  # "lexical" | "vector" | "fused" | "reranked"


@dataclass(frozen=True)
class SearchResult:
    """最终装配、返回给调用方（GUI/CLI/MCP）的一条结果。"""

    chunk_id: str
    library_id: str
    path: str
    heading_breadcrumb: str
    text: str
    confidence: float  # 0~1，展示层按此分档（强/中/弱相关）
    advice: tuple[str, ...] = ()  # 自适应建议，继承旧项目 advice.py 的思路，Phase 3 落地


# ---- 页级视觉导航（visual_index 扩展点，比如 official-visual-wemm）--------


@dataclass(frozen=True)
class PageHit:
    """页级视觉检索（"第二检索系统"）的一条结果——一整页 PDF 图对应一条，
    不是文字 chunk。

    调查了旧项目 obsidian-rag 的 WEMM 实际实现后确认：这条检索路径**从来
    不参与** core/pipeline.py::search() 的 BM25+向量+RRF 融合排序，是完全
    独立、单独调用的"第二检索系统"（旧项目 navigate_knowledge 与
    search_knowledge 彻底分离，绝不混向量空间/绝不混分数——见
    docs/ROADMAP.md TODO 第1条的调查结论）。所以 PageHit 和 SearchResult
    刻意是两个互不相通的类型，不是同一个类型的可选字段。"""

    library_id: str
    path: str  # PDF 相对库根目录的路径
    abs_path: str  # 这台机器上的绝对路径，方便调用方直接打开看图
    page_index: int  # 0-based 页码
    score: float  # 相似度分数，量纲与 SearchResult.confidence 不同，不能混用/换算/比较


# ---- 库摘要（library_summary/llm_provider 扩展点，Phase 3）---------------


@dataclass(frozen=True)
class SampledChunk:
    """从某库已建索引的向量空间里用最远点采样挑出的一个代表性片段，供
    official-library-summary 概括主题用——按 obsidian-rag 的
    library_summary.py::sample_representative_chunks 真实行为移植（"库里
    每个块索引时已经过 embedder 编码存进向量库，是免费的副产品，不需要
    也不该为了写一段简介重新读全文"）。和 SearchResult/PageHit 一样是
    独立类型，不相通：采样不是检索，没有 query，没有排序意义上的分数。"""

    path: str
    heading: str
    text: str


@dataclass(frozen=True)
class LibrarySummary:
    """一个库当前的简介状态——official-library-summary 插件的存储对外
    暴露的唯一读出形状，对齐旧项目 library.py::get_library_summary 的
    字段（source 的三态含义见该插件模块 docstring）。"""

    library_id: str
    text: str
    source: str  # "none"（从未生成）| "ai"（AI生成，可被覆盖）| "user"（用户手写，覆盖需走写权限门禁）
    updated_at: float | None
    fingerprint: str | None  # 生成时的内容指纹，用于判断"库内容可能已变化，简介或已过时"
    model: str | None  # 生成时用的 llm_provider 插件 id，source != "ai" 时为 None
