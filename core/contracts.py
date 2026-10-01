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
    failure_state: str | None = None
    capability_signature: str | None = None
    # ---- PDF 按页分流（2026-10-01 操作者确认，BC-01）----------------------------
    #: 文字层提取器判出来的“图片页”（页码从 1 起）：这些页要送识别。文字页和图片页混着的书，
    #: `text` 是文字层转出来的整本（图片页只有它上面仅有的几个字），由编排层把图片页送识别后
    #: 按页拼回；一页有字的都没有的书是失败（scanned），这里列出全书页码，整本送识别。
    image_pages: tuple[int, ...] = ()
    #: 文字层逐页的正文（下标 0 是第 1 页），只在混合的书上有——编排层按页拼接识别结果用。
    page_texts: tuple[str, ...] | None = None
    #: 最终结果里由识别补上的页，以及是谁识别的（插件 id）。
    ocr_pages: tuple[int, ...] = ()
    ocr_by: str | None = None
    #: 图片页里没能识别的页（正文里这些页只有文字层仅有的几个字），以及原因代码：
    #: ocr-off（没开扫描件识别）/ too-many-pages（超过本机识别页数上限、又没开送云端）/
    #: ocr-deferred（识别服务暂时不可用，下一轮再试）/ ocr-failed（识别出错）。
    #: 条件变了（开了识别、调了上限、服务恢复）下一轮自动补识别（BC-04）。
    missing_pages: tuple[int, ...] = ()
    missing_reason: str | None = None
    #: 转出这份正文时用的、会改变转换结果的设置（提取器 `output_settings()` 的返回值，比如
    #: PDF 文字层的“转换方式 + 哪些页算图片页”）。核心记进清单；用户改了这些设置，下一轮把
    #: 这个提取器转的文件按新设置重转（2026-10-01 操作者确认，BC-01）。没有这类设置的提取器留 None。
    extractor_settings: str | None = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.failure_reason is None):
            raise ValueError(
                "ExtractedDocument: text 和 failure_reason 必须恰好一个有值——"
                "不允许两个都为 None（假装成功却没内容）或两个都有值（既失败又声称有内容）"
            )


# extractor:<ext> 插件的接口：必备 `extract(library_id, path, root) -> ExtractedDocument`；
# 可选 `is_active() -> bool`（设置里没选它时跳过）、`index_signature() -> str`（能力签名）。
#
# 可选 `extract_many(library_id, paths, root) -> list[ExtractedDocument]`（2026-09-29 起，
# 目前只有本机 MinerU 实现）：一次交几份文件，返回与 `paths` 一一对应、顺序相同的结果，
# 每一份的成功/失败形状与逐份调用 `extract` 完全一样（服务暂时不可用就每份都是 "deferred"）；
# 不抛异常。编排层只在同时有 ≥2 份等着它时才用，只攒到一份照旧调 `extract`；它抛了异常或
# 条数对不上，编排层就把这几份退回逐份 `extract`。合批的分组上限与显存把关由插件自己负责。
#
# PDF 按页分流用到的可选方法（2026-10-01，BC-01；编排层见 core/pipeline.py 与 core/pdf_pages.py）：
# - 文字层提取器 `write_page_range(src, first, last, dest) -> None`：把第 first～last 页（从 1 起）
#   另存成一份 PDF，编排层把图片页切出来送识别用；
# - 识别提供者 `page_budget() -> int | None`：一本书最多在它这里识别几页（None = 不限）；
# - 识别提供者 `max_pages_per_request() -> int | None`：一次请求最多几页，编排层据此把长段切开；
# - 识别提供者 `overflow_active() -> bool`：本机识别超过页数上限时，愿不愿意接手（云端，设置里开）。
#
# 可选 `output_settings() -> str`（2026-10-01，BC-01）：会改变它转出来的正文的设置，压成一个
# 短字符串（不含 `:` 和 `+`）。成功的结果要在 `extractor_settings` 里带上转换时用的那一份。
# 编排层发现清单里记的和现在的不一样，就把这个提取器转的文件按新设置重转——与“插件代码升级
# 才作废旧正文”（`plugin.toml` 版本）是两回事：这里只牵连它自己转的文件，别的提取器转的不动。


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
    section_id: str = ""
    section_text: str = ""


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
class QueryExpansion:
    query: str
    expanded_by: str
    reason: str


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
    backfilled: bool = False
    chunk_index: int = -1  # 本块在该文件内的序号（0起），[块 k/N] 完整性标记的数据（问题10）
    total_chunks: int = 0  # 该文件被索引的总块数
    truncated: bool = False  # 正文是否因超过 return_chunk_limit 被行边界截断
    advice: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchAdviceInput:
    results: tuple[SearchResult, ...]
    query: str
    mode: str
    top_k: int
    default_libraries: tuple[str, ...]
    warn_threshold: float
    strong_threshold: float
    #: 本次交付是否触发了"同一文件最多 N 块"的封顶。对齐 obsidian-rag
    #: retriever.py:494-497 的尾注"（同一文件最多展示 N 块…完整内容请打开
    #: 源文件）"——没有这个信号，agent 看到"同一篇笔记占了 3 条"时会以为
    #: 库里只有 3 个相关内容，不知道后面还有被封顶掉的。
    capped: bool = False
    #: 本次交付有多少块因"同一小节已交付过"被折叠掉。对齐 retriever.py:489
    #: 传给 advice 的 `folded` 计数；LEGACY 的 advice.py:158 用它触发
    #: "正文已按小节回填"的提示（`folded > 0 or any(backfilled)`）。
    folded: int = 0
    #: 结果为空时的原因，决定建议给哪一套文案。None = 非空结果。对齐
    #: retriever.py:479-483 的两分支：检索到了东西但全被置信度下限过滤
    #: 掉（"all-below-drop-threshold"）与压根没检索到任何候选（"no-score"）
    #: 对用户的下一步动作完全不同，不能都报"未找到"。
    empty_reason: str | None = None


@dataclass(frozen=True)
class SearchResponse:
    results: tuple[SearchResult, ...]
    advice: tuple[str, ...]


@dataclass(frozen=True)
class VisualPageState:
    library_id: str
    path: str
    provider_id: str
    status: str
    failure_reason: str | None
    pages: tuple[int, ...]
    #: 这份 PDF 一共几页（2026-09-30 起页库记录里才有；更早建的记录为 None）。
    #: “转换缓存”清单靠它写出“28/36 页”、列出缺哪几页（BC-19）。
    page_count: int | None = None
    #: 这份 PDF 的页向量是在哪一轮（generation）编出来的（压缩搬家不算重编）。等于当前
    #: generation = 本轮新建，否则是沿用之前的——每轮日志里“页库 复用/新建”就按它数（BC-19）。
    #: 更早的记录没有这一项，为 None，按“沿用”算。
    built_in: str | None = None


@dataclass(frozen=True)
class VisualProgress:
    """`visual_index` 扩展点回报给核心的**页级进度**（2026-09-29 新增）。

    **为什么需要它**：页级视觉索引的进度条口径是文字索引的
    `files_done/files_total`——那一段跑完就已经是 78/78、100% 了。于是渲染页的
    索引跑 7645 页的这 30 分钟里，界面上**一个数字都不会变**，看起来完全像冻住
    （真机 2026-09-29 就是这样让人以为卡死）。旧项目有页级进度回调
    （`wemm_indexer.py` 问题47，文件级粒度），rag-redo 移植时漏了。

    **失败语义**：插件**只在还能正常往下走时**上报；页级调用抛异常/服务不可达时
    不再上报，由插件自己决定本轮怎么收尾（记失败终态并正常返回，见 BC-04）。
    核心侧收到任何异常都吞掉——进度上报失败绝不能把页级索引带崩。

    **上报节奏由插件自己节流**（默认 ~2 秒一次）：页级循环每页都会走完，逐页
    写进度文件会把磁盘 IO 变成瓶颈。这里记的是"已经编完多少页"，是**累计值**，
    重复上报同一个值不算错，插件不必自己算差值。
    """

    pages_done: int
    pages_total: int
    current_path: str = ""

    @property
    def text(self) -> str:
        """给界面看的一行文案（前端逐字节冻结，只能用 `heartbeat_note` 这种既有字段）。"""
        return f"页级视觉索引 {self.pages_done}/{self.pages_total} 页"


@dataclass(frozen=True)
class LibraryFreshness:
    """单库 freshness 扫描结果——对齐 obsidian-rag/index.py::kb_stale 返回的
    (stale, stats) 形状。missing/emptied 标志的消费方（MCP 搜索前自动同步，
    server.py:264-273）据此跳过同步并保留旧索引：库路径消失（临时挂载失败）
    和"目录在但扫不到任何文件"（2026-08-14 审计 F16，源文件没放回去）都
    不等于"用户确认删除"，而旧索引被清空是不可逆代价。"""

    library_id: str
    stale: bool
    missing: bool = False
    emptied: bool = False


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    library_id: str
    path: str
    node_type: str
    chunks: int = 0
    updated_ns: int | None = None
    failure_reason: str | None = None
    theme: str = "general"
    extraction_state: str = "none"
    visual_state: str = "none"
    page_number: int | None = None
    page_count: int | None = None
    is_hub: bool = False


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: str


@dataclass(frozen=True)
class GraphStats:
    nodes: int
    edges: int


@dataclass(frozen=True)
class GraphResponse:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    library_ids: tuple[str, ...]
    stats: GraphStats


@dataclass(frozen=True)
class SemanticGraphEdge:
    source: str
    target: str
    similarity: float


@dataclass(frozen=True)
class SemanticGraphResponse:
    edges: tuple[SemanticGraphEdge, ...]
    error: str | None = None


# ---- 总览星图读模型（BC-18）---------------------------------------------------
#
# GUI 总览页把每个库画成一根光管：文件按内容排序（相邻 = 内容相近），按内容分组上色。
# 排序和分组要用"每个文件一个内容向量"，这份向量直接从 vector_store 里已经存好的块向量
# 平均出来——只读、纯 CPU、不经过嵌入模型，所以打开总览页不会占用显存。


@dataclass(frozen=True)
class FileVectorSet:
    """vector_store 按文件汇总的内容向量：每个文件 = 它全部块向量的平均，再做 L2 归一化。

    `vectors` 是 numpy float32 数组，形状 (len(paths), dim)，行与 `paths` 一一对应；
    一个块向量都没找到的文件不出现（调用方把它当"没有内容向量"处理）。"""

    library_id: str
    generation: str | None
    paths: tuple[str, ...]
    vectors: object  # numpy.ndarray，float32，(N, dim)
    dim: int
    produced_by: str
    store_version: str


@dataclass(frozen=True)
class OverviewFile:
    """总览星图里的一个文件。`gap` 与 `loose` 只在有内容向量时有意义，否则分别为 1.0 / 0.0。"""

    path: str
    node_id: str  # 与 graph() 的节点 id 相同（库id|路径），详情面板靠它对上
    node_type: str
    chunks: int
    pages: int  # 页级视觉索引已收录的页数（没有就是 0）
    state: str  # "indexed" 已索引 / "pending" 未索引 / "ocr" 待 OCR / "failed" 提取失败
    failure_reason: str | None
    updated_ns: int | None
    group: int  # 内容分组编号（跨库统一；0 = 最大的一组）；-1 = 没有内容向量
    gap: float  # 与排序中前一个文件的内容差距（1 − 余弦相似度，0~2）
    loose: float  # 与前后邻居平均内容的余弦相似度（-1~1），越低越"零散"


@dataclass(frozen=True)
class OverviewLibrary:
    library_id: str
    files: tuple[OverviewFile, ...]  # 已排好顺序：有内容向量的按内容排，其余按路径接在后面
    points: int  # 这个库在星图里的总点数 = 文件数 + 块数 + 页数


@dataclass(frozen=True)
class OverviewGroup:
    group: int
    size: int  # 这一组有多少个文件
    samples: tuple[tuple[str, str], ...]  # 离组中心最近的几个文件：(库id, 路径)，给图例用


@dataclass(frozen=True)
class OverviewMapResponse:
    libraries: tuple[OverviewLibrary, ...]
    groups: tuple[OverviewGroup, ...]
    built_by: str
    layout_version: str
    error: str | None = None


# ---- 转换缓存清单（BC-19，core/conversion_cache.py 产出）--------------------
#
# 用户和开发者要能一眼确认“PDF/Word 转成文字了没有、WEMM 页库建了没有、存在哪、多大”。
# 这两份缓存本来就在：转文字缓存由核心写（core/extract_cache.py），页库由 visual_index
# 插件写。这里只是把它们**读出来**摆在一起，不改任何存法，也不触发任何转换或模型加载。


@dataclass(frozen=True)
class ConversionCacheFile:
    """一份“需要转换”的文件（不是纯文字的格式：PDF、Word……）两种缓存的现状。

    `text_state`：done（有转好的正文）/ partial（转好了，但有图片页里的字没识别，见
    `text_missing_pages`）/ pending（还没轮到或在等转换服务，会自动补上）/
    failed（转换失败的终态）/ missing（索引记着转好了，正文文件却不见了）。
    `pages_state`：n/a（不是 PDF，页库只做 PDF）/ off（页库没开）/ none（还没建）/
    done / partial（部分页面没编上）/ failed。
    原因一律是稳定代码（`*_reason`），界面、命令行、目录文件都用
    `core.conversion_cache.reason_text()` 翻成人话，不各写一份。"""

    path: str
    extension: str
    text_state: str
    text_reason: str | None = None
    text_route: str | None = None  # 产出正文的提取器插件 id
    text_route_version: str | None = None
    text_route_name: str | None = None  # 该插件在 plugin.toml 里的名字（界面直接显示）
    text_file: str | None = None  # 正文缓存文件的绝对路径
    text_bytes: int = 0
    text_updated: float | None = None  # 缓存文件的修改时间（Unix 秒）
    text_ocr_pages: tuple[int, ...] = ()  # PDF 里由识别补上的图片页（页码从 1 起）
    text_ocr_by: str | None = None  # 识别这些页的插件 id
    text_ocr_by_name: str | None = None  # 该插件在 plugin.toml 里的名字（界面直接显示）
    text_missing_pages: tuple[int, ...] = ()  # 图片页里没能识别的页（原因见 text_reason）
    pages_state: str = "n/a"
    pages_reason: str | None = None
    pages_detail: str | None = None  # 页库插件记下的原始说明（例如显存不足的两个数字）
    pages: tuple[int, ...] = ()  # 已进页库的页码，从 1 起
    page_count: int | None = None  # PDF 总页数（页库记录里有才有）
    pages_rebuilt: bool = False  # 页向量是本轮新建的（False = 沿用上一轮）


@dataclass(frozen=True)
class ConversionCacheLibrary:
    library_id: str
    name: str
    files: tuple[ConversionCacheFile, ...]
    text_dir: str  # 这个库的转文字缓存文件夹
    catalog_file: str  # 缓存目录.md 的位置（每轮索引完成后刷新）
    pages_enabled: bool
    pages_dir: str | None = None  # 页库数据所在文件夹（页库插件没启用时为 None）
    page_bytes_estimate: int = 0  # 页库在硬盘上的大约大小（页向量存在数据库里，只能估）
    page_vram_gb: float | None = None  # 建页库/试搜要占的显存
    page_idle_unload_seconds: int | None = None  # 页库模型闲置多久自动卸载
    #: 这个库的清单没能读出来（例如库目录不在了）时的原因（只含类型，不透传原文）；
    #: 其余库照常出结果。
    error: str | None = None
    built_by: str = "core.conversion_cache"
    report_version: str = "1"

    @property
    def text_total(self) -> int:
        return len(self.files)

    @property
    def text_done(self) -> int:
        return sum(1 for item in self.files if item.text_state == "done")

    @property
    def text_bytes(self) -> int:
        return sum(item.text_bytes for item in self.files if item.text_state == "done")

    @property
    def pdf_total(self) -> int:
        return sum(1 for item in self.files if item.pages_state != "n/a")

    @property
    def pages_done(self) -> int:
        return sum(1 for item in self.files if item.pages_state == "done")

    @property
    def page_vectors(self) -> int:
        return sum(len(item.pages) for item in self.files if item.pages_state in {"done", "partial"})


@dataclass(frozen=True)
class ConversionRoundSummary:
    """一轮索引结束时的“转换缓存”一行账（写进索引日志，BC-19）：多少份沿用了已有缓存、
    多少份这轮新转/新建、跑完还缺多少。只数“需要转换”的文件；页库没开时页库三项都是 0。"""

    text_reused: int
    text_new: int
    text_missing: int
    pages_enabled: bool
    pages_reused: int = 0
    pages_new: int = 0
    pages_missing: int = 0
    # ---- 2026-10-01：让用户看得出设置到底起没起作用（操作者反馈“改了设置不知道是没生效还是
    # 静默失败”，连送没送 MinerU 云端都看不出来）----------------------------------------------
    #: 这一轮新转的文件按“谁转的”分：（插件显示名，份数）——整本扫描件送了哪个识别一眼可见
    text_new_by: tuple[tuple[str, int], ...] = ()
    #: 这一轮按页分流补上的图片页，按识别者分：（插件显示名，页数）（BC-01）
    ocr_pages_by: tuple[tuple[str, int], ...] = ()
    #: 跑完还没识别的图片页总数、涉及几份 PDF（原因与补法见诊断页“转换缓存”）
    pages_unrecognized: int = 0
    files_unrecognized: int = 0
    #: 这个库里有没有 PDF：没有就不写“图片页”那一段
    has_pdf: bool = False


# ---- 页级视觉导航（visual_index 扩展点，比如 official-visual-wemm）--------
#
# `index_library(library_id, root, pdf_paths, *, generation, changed_paths,
# previous_generation, before_serve=None)`：`before_serve` 是核心传入的"让路"回调——
# 插件在**真要占显卡渲染页面之前**调用一次（无页可渲染时不调用），核心借它把文字向量/
# 重排模型从显卡卸下来（旧项目 index.py:1784-1806 的 `_release_for_wemm`）。回调不抛异常。


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


@dataclass(frozen=True)
class DocumentContent:
    """`Pipeline.read_document` 的返回形状——对齐 obsidian-rag 的
    `read_document` MCP 工具：读某文档的完整正文，用于检索命中后精读，
    不是检索结果的一部分（没有 confidence/query，和 SearchResult 不
    相通）。"""

    library_id: str
    path: str
    text: str
    source: str  # "源文件直读"（.md/.txt 现读）| "提取缓存"（pdf/docx 等，来自上一次索引的提取结果）
    abs_path: str = ""  # 源文件绝对路径——看图模型/用户要直读原 PDF 时用（旧项目抬头含绝对路径）


@dataclass(frozen=True)
class PreviewExtraction:
    """`Pipeline.preview_extract` 的返回形状——提取试验台（GUI 诊断视图
    "提取试验台"页签）对单个本地文件跑一次提取的结果。

    这是**只读诊断**产物：不写提取缓存、不落 generation、不进任何索引数据，
    也不要求该文件属于某个已注册库（`path` 是任意本地路径）。与
    `ExtractedDocument` 的区别就是"没有库上下文、没有缓存副作用"。

    `reason` 复用索引链路的终态词汇（`""`=有产出；非空 ∈
    unreadable/empty/scanned/extract-failed/deferred/无 provider），
    让 GUI 能用同一套文案解释"为什么这个文件没产出"。
    """

    path: str
    ok: bool
    markdown: str | None
    reason: str
    route: str  # 实际命中的 provider（"extractor:<plugin_id>"）或 "-"
    backend: str  # 请求的后端覆盖（""=跟随全局设置）
    elapsed: float
    chars: int
