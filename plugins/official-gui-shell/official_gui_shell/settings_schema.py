"""设置页的数据层：哪些设置键、分成哪几组、每个键什么类型/文案/默认值。

**分组与文案对齐旧项目** `obsidian-rag/gui/config_editor.py::GROUPS/FIELD_META`
（前端是逐字节复刻的旧前端，`get_settings()` 的返回形状 `{groups, missing_keys}`
就是它消费的）。但**只列 rag-redo 真实读取的键**：旧项目有 13 个分组、几十个键，
其中 `vault`/`collection_name`/`model_name`（模型由插件决定）/`chunk_char_limit`
（切块粒度目前是固定值）/锁与心跳/导出导入等，在 rag-redo 里要么不存在、要么不
是"设置"。把它们画在设置页上会是"改了没有任何效果"的假开关，比少一个分组更糟，
所以没有可调键的分组整组省略——前端按 `groups` 动态渲染，分组数不是写死的。

**默认值的出处**：展示"当前生效值"必须能回答"没设过时是多少"，否则设置页会把
所有未设置的键显示成空，用户一保存（前端保存时会把页面上**所有**字段一起发回）
就把空串写成了值。默认值取自各个消费方自己的常量：core 的直接 import
`core.pipeline`；插件里的（HyDE/WEMM/重排冷却…）插件之间不许互相 import，这里抄
一份**展示用**的默认值，并由 `tests/test_settings_schema.py` 用真实消费方的常量
对账——漂移会让测试变红，而不是让设置页悄悄说谎。

**保存语义**（`coerce_setting` + 桥接层 `save_settings`）：按键声明的类型转换，转换
失败进 `errors` 且**整批不落盘**（旧 `config_editor.apply_updates` 同一语义："任一
失败即整体中止"）；值等于默认值时清除该键而不是写死——这样将来默认值调整时，没被
用户主动改过的键会跟着走，不会被这次"保存"钉死在旧默认上。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from core.pipeline import (
    DEFAULT_CONFIDENCE_DROP_THRESHOLD,
    DEFAULT_CONFIDENCE_WARN_THRESHOLD,
    DEFAULT_DENSE_CANDIDATE_FACTOR,
    DEFAULT_DENSE_MIN_CANDIDATES,
    DEFAULT_FUSION_BM25_WEIGHT,
    DEFAULT_FUSION_DENSE_WEIGHT,
    DEFAULT_MAX_CHUNKS_PER_FILE,
    DEFAULT_RERANK_CANDIDATES,
    DEFAULT_RETURN_CHUNK_LIMIT,
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class SettingField:
    key: str
    kind: str  # "str" | "int" | "float" | "bool" | "list"
    label: str
    hint: str
    default: Any
    rebuild: bool = False
    secret: bool = False
    #: 封闭枚举：(值, 说明) —— GUI 渲染成下拉而不是手输魔法字符串
    choices: tuple[tuple[str, str], ...] = ()
    #: 推荐候选芯片（开放值仍可手输）
    suggest: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SettingGroup:
    title: str
    level: str  # "basic"（常用，排前面）| "advanced"（开发者）
    desc: str
    fields: tuple[SettingField, ...]


GROUPS: tuple[SettingGroup, ...] = (
    SettingGroup(
        "模型", "basic",
        "两阶段检索的重排开关，以及模型文件存放在哪。嵌入与重排用哪个模型由插件决定，不在设置里选。",
        (
            SettingField(
                "rerank_enabled", "bool", "两阶段重排开关",
                "检索两步走：向量+关键词融合先粗筛候选，重排模型再对「查询-块」逐对精排取 top_k；"
                "关闭 = 只用粗筛排序",
                True,
            ),
            SettingField(
                "models_dir", "str", "模型存放路径",
                "嵌入/重排/看图/本机OCR 的模型文件夹（里面是 models--BAAI--bge-m3 这类子文件夹）；"
                "留空 = 项目内的 models 文件夹（没有就首次使用时自动下载到那里）；"
                "改后检索模型下次加载时生效，看图/本机OCR 服务下次重新启动时生效",
                "",
            ),
        ),
    ),
    SettingGroup(
        "PDF 与云端 OCR", "basic",
        "扫描件 OCR 的提取后端；切到 mineru-cloud 会上传原始文件，mineru-local 不出内网"
        "（需先装 mineru 环境）。",
        (
            SettingField(
                "pdf_scan_backend", "str", "扫描件 OCR 后端",
                "无文字层 PDF 的处理方式；切换后下轮索引自动重试存量扫描件",
                "none",
                choices=(
                    ("none", "不做 OCR，扫描件跳过（默认）"),
                    ("mineru-cloud", "MinerU 云端 OCR（上传原始文件）"),
                    ("mineru-local", "MinerU 本地解析（不出内网，需先装 mineru 环境）"),
                ),
            ),
            SettingField(
                "pdf_text_mode", "str", "PDF 文字转换方式",
                "有文字层的 PDF 怎么转成文字：精细 = 每页做 AI 版面分析（标题、表格、图里的字最准，"
                "但每页约 0.3 秒、会占满 CPU）；快速 = 规则式转换（快约 6~30 倍，标题偶尔认错、"
                "图里的字不收、表格变成普通文字行）；自动 = 超过 200 页的大文件用快速，其余用精细。"
                "只影响之后新转换的 PDF，已经转好的不会重转",
                "auto",
                choices=(
                    ("auto", "自动：超过 200 页的用快速，其余精细（默认）"),
                    ("layout", "精细：全部做 AI 版面分析（最慢，结构最准）"),
                    ("fast", "快速：全部规则式转换（最快，结构略差）"),
                ),
            ),
            SettingField(
                "mineru_api_key", "str", "MinerU API Key",
                "云端 OCR 用：mineru.net → 个人中心 → API Token；也可用环境变量 MINERU_API_KEY，"
                "这里填了以这里为准。敏感信息，不进任何日志；补上 Key 后下轮索引自动重试此前因没 Key 跳过的扫描件",
                "",
                secret=True,
            ),
            SettingField(
                "mineru_model_version", "str", "MinerU 云端模型版本",
                "仅影响送 MinerU 云端时用哪个模型解析；本地直提与本机解析不受影响",
                "vlm",
                choices=(
                    ("vlm", "视觉语言模型（默认，官方推荐，精度更高）"),
                    ("pipeline", "传统流水线（更快更省配额，精度稍低）"),
                ),
            ),
            SettingField(
                "mineru_python", "str", "MinerU 环境 Python",
                "uv tool 装好的 py3.12 全路径；空=自动探测。别填 .venv/全局3.14",
                "",
            ),
        ),
    ),
    SettingGroup(
        "检索输出", "basic",
        "返回内容的形状与低置信护栏，全部实时生效。",
        (
            SettingField(
                "return_chunk_limit", "int", "单块返回字符上限",
                "超出截断并附标记；直接影响回答注入的 token 量",
                DEFAULT_RETURN_CHUNK_LIMIT,
            ),
            SettingField(
                "max_chunks_per_file", "int", "同文件最多块数",
                "防单文件霸屏 top_k；想看更多可临时调到 3–5",
                DEFAULT_MAX_CHUNKS_PER_FILE,
            ),
            SettingField(
                "default_libraries", "list", "默认检索库",
                "逗号分隔库 id；检索的 libraries 参数留空时先收窄到这些库，留空 = 全部注册库",
                [],
            ),
            SettingField(
                "confidence_warn_threshold", "float", "低置信标注阈值",
                "命中置信度低于此值 → 来源标注「仅供参考」（0~1）",
                DEFAULT_CONFIDENCE_WARN_THRESHOLD,
            ),
            SettingField(
                "confidence_drop_threshold", "float", "低置信丢弃阈值",
                "低于此值直接不输出该来源，宁缺毋滥（0~1）；0 = 关闭（给满 top_k，只标注不丢弃）",
                DEFAULT_CONFIDENCE_DROP_THRESHOLD,
            ),
        ),
    ),
    SettingGroup(
        "视觉导航（WEMM）", "basic",
        "把 PDF 每页渲染成图，用本机 GPU 的 WeMM 模型做成「每页一向量」导航库，告诉 AI "
        "内容在哪个 PDF 哪页。看图服务按需自动拉起、用完自动退出，与 bge-m3 显存互斥。",
        (
            SettingField(
                "wemm_backend", "str", "视觉导航开关",
                "WEMM 页级视觉导航后端；开着时导航/页索引会自动拉起看图服务，显存与 bge-m3 互斥自动错峰",
                "on",
                choices=(
                    ("on", "开启（默认：服务按需自动拉起、用完自动退出）"),
                    ("local", "开启（同 on，兼容旧取值）"),
                    ("off", "关闭（不建视觉库不占显存）"),
                ),
            ),
        ),
    ),
    SettingGroup(
        "融合与排序调优", "advanced",
        "RRF 权重 / 候选池 / 重排预算。拿不准就保持默认。",
        (
            SettingField(
                "fusion_dense_weight", "float", "语义权重（dense）",
                "调大偏语义检索；1.0/1.0 即经典等权 RRF",
                DEFAULT_FUSION_DENSE_WEIGHT,
            ),
            SettingField(
                "fusion_bm25_weight", "float", "关键词权重（bm25）",
                "调大偏关键词/专名检索；两项均实时生效",
                DEFAULT_FUSION_BM25_WEIGHT,
            ),
            SettingField(
                "dense_candidate_factor", "int", "候选池系数",
                "候选池 = top_k × 此系数，越大越准越慢",
                DEFAULT_DENSE_CANDIDATE_FACTOR,
            ),
            SettingField(
                "dense_min_candidates", "int", "候选池下限",
                "候选池保底数量，保证小 top_k 时融合质量",
                DEFAULT_DENSE_MIN_CANDIDATES,
            ),
            SettingField(
                "rerank_candidates", "int", "重排候选数",
                "送重排的融合候选数，建议 30–80；越大越慢",
                DEFAULT_RERANK_CANDIDATES,
            ),
        ),
    ),
    SettingGroup(
        "切块粒度", "advanced",
        "切块大小目前是固定值（单块 600 字符、整篇收录阈值 200 字符），这里只有父节回填开关。",
        (
            SettingField(
                "small_to_big", "bool", "父节回填（small-to-big）",
                "命中小块时回填父节全文补偿上下文；与小块切块配套",
                True,
            ),
        ),
    ),
    SettingGroup(
        "排除规则", "advanced",
        "排除目录/文件名/前缀是按库配置的（库管理 → 库配置）；这里是全局的占位过滤。",
        (
            SettingField(
                "tbd_exclude_ratio", "float", "TBD 占位过滤",
                "[TBD] 行占比 ≥ 此值的半成品文件跳过索引；0 = 关闭；仅全局生效（不可按库覆盖）",
                0.1,
                rebuild=True,
            ),
        ),
    ),
    SettingGroup(
        "HyDE 查询增强", "advanced",
        "提问用词与笔记差太远导致检索落空时，先让本地 LLM 按问题写一段「假设答案」，拿它去检索"
        "（术语更接近笔记原文）。默认关；触发才多花一跳。",
        (
            SettingField(
                "hyde_enabled", "bool", "启用 HyDE",
                "开启后才可能触发；触发时多跑一轮检索 + 一次本地 LLM 调用（不触发零开销）；"
                "需 LM Studio 类服务在运行",
                False,
            ),
            SettingField(
                "hyde_llm_url", "str", "HyDE 服务地址",
                "OpenAI 兼容 chat/completions 接口（如 LM Studio 默认 localhost:1234）",
                "http://localhost:1234/v1/chat/completions",
            ),
            SettingField(
                "hyde_llm_model", "str", "HyDE 模型名",
                "填本地服务里已加载的模型名（如 qwen2.5-3b-instruct）",
                "qwen2.5-3b-instruct",
            ),
            SettingField(
                "hyde_llm_api_key", "str", "HyDE LLM API Key",
                "留空=本地服务免鉴权；填了=云端走 Bearer 认证，敏感信息不进任何日志",
                "",
                secret=True,
            ),
            SettingField(
                "hyde_min_confidence", "float", "HyDE 触发阈值",
                "首轮 top1 置信度低于此值才触发（0~1）；调大更爱触发",
                0.5,
            ),
            SettingField(
                "hyde_llm_timeout_seconds", "float", "HyDE 请求超时（秒）",
                "单次生成假设文档的等待上限",
                30.0,
            ),
            SettingField(
                "hyde_llm_max_tokens", "int", "HyDE 生成上限（token）",
                "假设文档的最大长度",
                200,
            ),
        ),
    ),
    SettingGroup(
        "性能与硬件", "advanced",
        "CUDA 失败冷却；运行时会自动按显存收紧批次。",
        (
            SettingField(
                "cuda_cooldown_seconds", "int", "CUDA 冷却秒数",
                "CUDA 失败（OOM 等）后进入冷却，到期轻量探测自动切回，避免反复崩（默认 300）",
                300,
            ),
        ),
    ),
)

FIELDS: dict[str, SettingField] = {f.key: f for g in GROUPS for f in g.fields}

#: 精确 API（`get_settings_values`）用的展示元信息：label/hint/secret。
SETTING_FIELD_META: dict[str, dict[str, Any]] = {
    f.key: {"label": f.label, "hint": f.hint, **({"secret": True} if f.secret else {})}
    for f in FIELDS.values()
}


def setting_is_secret(key: str) -> bool:
    """敏感键：登记为 secret 的，以及 `*_api_key` / `*_token` 后缀的（对齐旧
    config_editor.py 对 mineru_api_key 等的 secret 标记；规则兜底让将来新增的
    key 类设置不需要记得回来登记）。"""
    field = FIELDS.get(key)
    return bool(field and field.secret) or key.lower().endswith(("_api_key", "_token"))


def coerce_setting(key: str, raw: Any) -> Any:
    """把 UI 传来的值按该键声明的类型转成真值；失败抛 `ValueError`。

    只认已登记的键（未登记的键抛 `KeyError`，调用方决定跳过还是报错）。语义取自旧
    `config_editor._value_to_json`：int/float 严格解析、list 按逗号切、bool 认
    1/true/yes/on。与旧实现不同的一处：bool 遇到**不认识的字面值**报错，而不是
    悄悄当成 False——"保存成功但值没变"正是这次要消灭的一类静默失败。
    """
    field = FIELDS[key]
    kind = field.kind
    if kind == "str":
        value: Any = "" if raw is None else str(raw)
    elif kind == "bool":
        if isinstance(raw, bool):
            value = raw
        else:
            text = str(raw).strip().lower()
            if text in _TRUE:
                value = True
            elif text in _FALSE:
                value = False
            else:
                raise ValueError(f"不是合法的开关值：{raw!r}（用 true/false）")
    elif kind == "int":
        if isinstance(raw, bool):
            raise ValueError(f"不是整数：{raw!r}")
        if isinstance(raw, float) and raw.is_integer():
            value = int(raw)
        else:
            try:
                value = int(str(raw).strip())
            except ValueError as exc:
                raise ValueError(f"不是整数：{raw!r}") from exc
    elif kind == "float":
        if isinstance(raw, bool):
            raise ValueError(f"不是数字：{raw!r}")
        try:
            value = float(str(raw).strip())
        except ValueError as exc:
            raise ValueError(f"不是数字：{raw!r}") from exc
        if not math.isfinite(value):
            raise ValueError(f"不是有限的数字：{raw!r}")
    elif kind == "list":
        if isinstance(raw, (list, tuple)):
            value = [str(item).strip() for item in raw if str(item).strip()]
        else:
            value = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    else:  # 表里写错了类型：登记时就该被测试拦住，这里兜底不静默
        raise ValueError(f"未知的设置类型：{kind}")
    if field.choices and value not in {choice[0] for choice in field.choices}:
        allowed = "、".join(choice[0] for choice in field.choices)
        raise ValueError(f"不在候选值内：{value!r}（可选：{allowed}）")
    return value


def format_setting_value(kind: str, value: Any) -> str:
    """配置值 → 设置页字符串（契约约定：bool→true/false，list→a,b，其余 str；
    对齐旧 `fmt_setting_value`）。"""
    if kind == "bool":
        return "true" if value else "false"
    if kind == "list":
        return ",".join(str(item) for item in (value or []))
    return "" if value is None else str(value)
