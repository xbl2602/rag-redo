"""HF 模型离线优先加载——对齐 obsidian-rag/index.py::_load_pretrained
（问题59 B1），embedder 与 reranker 两个插件共用这一份实现（跨插件禁止
import，放 core 是因为"模型加载策略"属于资源/模型生命周期的公共基础设施，
见 AGENTS.md §7；本模块不 import 任何重依赖，factory 由调用方传入）。

SentenceTransformer / CrossEncoder 传 repo id 时，默认每次加载都要先向
huggingface.co 核对 commit 新鲜度，即使模型早已下载——实测这一步占冷加载
约 8 秒。local_files_only=True 直接跳过整轮联网核对。本地失败（缺文件
OSError；损坏快照 ValueError/RuntimeError）一律回退联网加载，首次使用
行为不变；联网也失败则抛带清理指引的 RuntimeError。
"""
from __future__ import annotations

from typing import Any, Callable


def load_pretrained(
    factory: Callable[..., Any], model_id: str, *, log=None, **kwargs: Any
) -> Any:
    """离线优先加载 HF 模型：先只读本地缓存，本地缓存不可用再回退联网。"""
    try:
        return factory(model_id, local_files_only=True, **kwargs)
    except (OSError, ValueError, RuntimeError) as exc:
        if log is not None:
            log(f"{model_id} 本地缓存不可用（{type(exc).__name__}），回退联网加载…")
    try:
        return factory(model_id, local_files_only=False, **kwargs)
    except Exception as exc:
        raise RuntimeError(
            f"{model_id} 加载失败：本地缓存不可用且联网加载也失败（{exc}）。"
            "若反复出现，请删除本地快照后重试"
        ) from exc


def param_dtype_mixed(model: Any) -> bool:
    """fp16 加载后参数 dtype 是否混搭（Half 与 Float 并存）——对齐旧
    index.py::_param_dtype_mixed：混搭即异常状态，推理时激活值与权重
    dtype 不匹配必报错；防御性检查，混搭由调用方回退 fp32 整体重建。"""
    seen: set[Any] = set()
    for p in model.parameters():
        seen.add(p.dtype)
        if len(seen) > 1:
            return True
    return False
