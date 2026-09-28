"""core/index_integrity.py — "当前 generation 还是不是可信的"这一个判断。

**补的是哪个坑**：`obsidian-rag` 有两条自愈，rag-redo 一条都没有：

1. **一致性自愈**（`obsidian-rag/index.py:1903-1910` 增量路径 /
   `index.py:1592-1597` `kb_stale` 检索前扫描）——meta 里记的期望块数与
   向量库实际块数不符时，增量路径"指纹全命中 → 无新块可写"根本修不了缺失的
   块，必须按全量重建处理。LEGACY 注释点名的两类场景：a) `--full` 中途被杀
   在清库窗口（count==0）；b) 进程被杀致 Chroma WAL 段未持久化、或被外部
   工具/磁盘故障破坏——b) 此前会陷入「每轮判 stale → 每轮修不了」的死循环。
2. **版本升级自愈**（`obsidian-rag/index.py:1518-1519`，LEGACY 的
   `META_VERSION`）——切块/清洗/提取逻辑升级后旧索引文本与当前逻辑不一致，
   检索前的自动同步必须自己发现并重建，而不是等用户察觉后手动 `--full`。

**为什么是核心服务**：判断"要不要整库重建"要知道 manifest（核心的持久化
状态）又要问向量库（插件），两头都是核心编排层自己的事，没有第三方插件需要
中立裁判，放扩展点只会制造两份实现。

**AGENTS.md §4.5「同一业务判断只能有一个权威实现」**：`index_library()` 与
`library_freshness()` 是这条判断的两个入口（LEGACY 也是两处），两处都必须
调本模块的 `rebuild_reason()`，谁都不许自己再写一遍比大小。

**一处必须写清楚的语义收窄（`!=` → `actual < expected`）**：LEGACY 写的是
`_actual != _expected`，看起来更严，但两边的"实际块数"含义并不相同：

- LEGACY 单 collection、无 active 过滤，每轮按 id 精确清理幽灵块
  （`index.py:2281-2293`），所以实际块数只可能"少于"期望；
- rag-redo 是**多 segment + active_chunk_ids 过滤**（见
  `core/pipeline.py::_query_vector_segments`）：文件改了/删了之后，旧块
  物理上还留在上一代的集合里，只是在查询时被 `active_ids` 滤掉。这不是
  损坏，是压缩（compaction）之前的设计内中间态。拿 `!=` 去判，每轮都会
  误报漂移 → 每轮强制全量重建，正好制造出 LEGACY 注释里要消灭的那个死循环。

LEGACY 这条检查要治的病只有一个，注释里写得很直白（`index.py:1897`
"增量无法凭空补出**缺失**的块"），所以这里只判**缺块**这一个方向：检索
侧真正会疼的是"该有的块取不到"，多出来的幽灵块由 `active_ids` 挡住、
由压缩回收，对用户零影响。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

#: 漂移原因 token（稳定可机读，GUI/MCP/日志都按它分支，不要改成自由文本）。
REBUILD_SIGNATURE_UPGRADE = "signature-upgrade"
REBUILD_MISSING_CHUNKS = "missing-chunks"


def manifest_files(manifest: Mapping[str, Any] | None) -> dict[str, dict]:
    """manifest 的有效文件条目（`files` 不是 dict 时视作空）。"""
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    if not isinstance(files, dict):
        return {}
    return {
        str(path): record
        for path, record in files.items()
        if isinstance(path, str) and isinstance(record, dict)
    }


def expected_chunk_count(manifest: Mapping[str, Any] | None) -> int:
    """期望块数 = Σ 非终态条目记录的 `chunk_ids` 长度。

    逐条对齐 LEGACY `index.py:1904-1905`
    （`sum(i.get("chunks", 0) for i in meta.values() if not _skipped(i))`）：
    只数**真正产出了块**的条目——xfail/tbd/empty/scanned 这些终态条目在
    LEGACY 里 `_skipped()` 为真、chunks 记 0，在 rag-redo 里就是
    `status != "indexed"` 且 `chunk_ids` 为空。判定基准取自**真实条目**
    （`files` 里值是 dict 的那些），不是 `files` 这个 dict 本身——LEGACY
    特别强调过这一点（meta 里混着 `_version` 这类非 dict 键，用原始 dict
    判空会得出"有条目"的错误结论）。
    """
    total = 0
    for record in manifest_files(manifest).values():
        if record.get("status") != "indexed":
            continue
        chunk_ids = record.get("chunk_ids")
        if isinstance(chunk_ids, list):
            total += sum(1 for chunk_id in chunk_ids if chunk_id)
    return total


def count_store_chunks(
    count_one: Callable[[str], int],
    segments: Iterable[str],
) -> int | None:
    """向量库实际块数 = Σ 各 segment 的 `count()`；**任何一步探测失败返回
    `None`**（= 不可判定，调用方必须 fail-open 放行）。

    fail-open 的理由同 LEGACY `obsidian-rag/index.py::_chroma_count`：它 catch
    住异常返回 `None`，`kb_stale` 见到 `None` 就不判漂移（`index.py:1595`
    `if actual is not None and ...`）。AGENTS.md §7「探测失败」与「所有权未知」
    必须区分——探测失败只能少判一次自愈，绝不能把整轮索引搞崩，更不能反过来
    宣称"索引是好的"。
    """
    total = 0
    try:
        for segment in segments:
            value = count_one(segment)
            if value is None:
                return None
            total += int(value)
    except Exception:  # noqa: BLE001 - 探测一律 fail-open（LEGACY 同纪律）
        return None
    return total


def consistency_drift(expected: int, actual: int | None) -> str | None:
    """缺块判定：`actual < expected` → `REBUILD_MISSING_CHUNKS`，否则 `None`。

    - `actual is None`（探测失败）→ `None`，降级放行；
    - `expected <= 0`（全终态库，0 个非终态块）→ `None`，**0==0 不误伤**
      （LEGACY `index.py:1902` 显式排除的那一类）；
    - `actual > expected`（幽灵块，`active_ids` 会滤掉）→ `None`，理由见模块
      docstring 的语义收窄说明。
    """
    if actual is None or expected <= 0:
        return None
    if actual < expected:
        return REBUILD_MISSING_CHUNKS
    return None


def signature_drift(
    manifest: Mapping[str, Any] | None,
    signatures: Mapping[str, Any],
) -> bool:
    """当前能力签名与 manifest 记录的是否已经不一致。

    对齐 LEGACY `index.py:1518-1519`（`meta["_version"] != META_VERSION` →
    `version_upgrade`）：LEGACY 只有一个全局 `META_VERSION`，rag-redo 把它拆成
    `_pipeline_signatures()` 逐扩展点的签名（切块器/嵌入器/词法/向量库/文本
    管线/各格式提取器），"任一签名变了"就等价于"旧索引是旧逻辑产的"。
    重建粒度仍然是受控的：`index_library()` 内部按
    `force_chunks`/`force_embed`/`force_lexical` 逐层判断，只重做受影响的部分。
    """
    if not isinstance(manifest, Mapping):
        return False
    recorded = manifest.get("signatures")
    if not isinstance(recorded, Mapping):
        return True  # 没有签名段的 manifest（外来/被裁剪过）不可信，按升级处理
    return dict(recorded) != dict(signatures)


def rebuild_reason(
    manifest: Mapping[str, Any] | None,
    *,
    signatures: Mapping[str, Any] | None = None,
    actual_chunk_count: int | None = None,
) -> str | None:
    """**唯一权威判定**：这个 manifest 还可信吗？不可信就返回原因 token。

    `manifest is None`（从未索引过 / 库是空的）返回 `None`——没有可判定的
    东西，也就没有"需要重建"的理由。签名漂移优先于缺块判定返回：逻辑升级
    之后的旧索引里"少块"往往是升级本身的正常结果，先按升级重建更贴近
    LEGACY（`kb_stale` 也是 `version_upgrade` 先返回）。
    """
    if manifest is None:
        return None
    if signatures is not None and signature_drift(manifest, signatures):
        return REBUILD_SIGNATURE_UPGRADE
    return consistency_drift(expected_chunk_count(manifest), actual_chunk_count)
