"""总览星图读模型（BC-18）：把"每个文件的内容向量"变成星图需要的排序、分组和差距。

GUI 总览页把每个库画成一根光管，文件沿管子排开：
- 顺序按内容排（一维 seriation），角度上相邻 = 内容相近；
- 颜色按内容分组（跨库统一的球面 k-means），同色 = 同一类内容；
- `gap` 是和前一个文件的内容差距，前端据此决定相邻文件挨多紧（相近的挤在一起，
  那一段点就密，光管就略微鼓起）；`loose` 是和前后邻居平均内容的相似度，越低越"零散"。

这里全是纯计算：输入是 graph() 读模型和 vector_store 汇总好的文件向量，不碰存储、
不碰嵌入模型、不碰显卡。前端只负责把这些数画出来，不做任何内容判断（BC-15 零业务逻辑）。

算法来自 demos/orbital-overview.html 的原型（操作者 2026-09-30 确认效果后要求落地），
随机数一律用固定种子，同样的数据每次得到同样的图。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from core.contracts import (
    FileVectorSet,
    GraphNode,
    GraphResponse,
    OverviewFile,
    OverviewGroup,
    OverviewLibrary,
    OverviewMapResponse,
)

#: 排序、分组、差距的算法版本；改了任何一步的结果都要升，调用方按它判断缓存是否还能用。
OVERVIEW_LAYOUT_VERSION = "1"
#: 排序和分组先把向量随机投影到这么多维再算：1024 维直接做幂迭代太慢，
#: 64 维对"谁和谁相近"的保持已经足够（Johnson–Lindenstrauss），差距 `gap` 仍用原始维度算。
PROJECT_DIM = 64
#: 幂迭代次数：小库主题间分离度高时迭代不够会让排序明显变差（demos/README 坑 #14）。
POWER_ITERS = 12
#: 内容分组最多几组——前端调色板只有 16 色。
MAX_GROUPS = 16
#: 图例里每组列几个代表文件。
GROUP_SAMPLES = 3
_SEED = 0x5EED

_DOC_TYPES_EXCLUDED = {"page", "pagegroup"}


def _np():
    import numpy as np  # 只在真正计算时才需要；numpy 随向量库一起装着（chromadb 依赖）

    return np


def project(vectors, dim: int = PROJECT_DIM):
    """固定种子的高斯随机投影，再逐行归一化。维度本来就不高时原样返回（也归一化）。"""
    np = _np()
    x = np.asarray(vectors, dtype=np.float32)
    if x.ndim != 2 or x.shape[0] == 0:
        return x.reshape(0, min(dim, x.shape[-1] if x.ndim == 2 else 0))
    if x.shape[1] > dim:
        rng = np.random.default_rng(_SEED + x.shape[1])
        basis = rng.standard_normal((x.shape[1], dim)).astype(np.float32) / math.sqrt(dim)
        x = x @ basis
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.maximum(norms, 1e-12)


def seriate(x, min_run: int | None = None):
    """一维排序：递归地沿主方向对半分，得到"相似即相邻"的顺序。返回行号数组。

    每一段先求均值、用幂迭代找主方向，按投影的中位数分左右两半，再分别往下分，
    直到段长不超过 `min_run`。和原型同一个做法（demos/orbital-overview.html::seriate）。"""
    np = _np()
    n = int(x.shape[0])
    order = np.arange(n)
    if n <= 2:
        return order
    if min_run is None:
        min_run = max(3, round(n * 0.002))
    rng = np.random.default_rng(_SEED)
    stack = [(0, n)]
    while stack:
        a, b = stack.pop()
        m = b - a
        if m <= min_run:
            continue
        seg = x[order[a:b]]
        centered = seg - seg.mean(axis=0, keepdims=True)
        u = rng.standard_normal(x.shape[1]).astype(np.float32)
        u /= max(float(np.linalg.norm(u)), 1e-12)
        for _ in range(POWER_ITERS):
            nxt = centered.T @ (centered @ u)
            norm = float(np.linalg.norm(nxt))
            if norm < 1e-12:
                break
            u = nxt / norm
        proj = centered @ u
        median = float(np.median(proj))
        left = proj <= median
        if left.all() or not left.any():
            continue
        # 左半保持原相对顺序，右半倒序接上：与原型一致（原型用双指针写回同一个缓冲区）
        idx = order[a:b]
        order[a:b] = np.concatenate([idx[left], idx[~left][::-1]])
        cut = a + int(left.sum())
        stack.append((a, cut))
        stack.append((cut, b))
    return order


def neighbor_gaps(v):
    """按给定顺序排好的行向量，第 i 个与第 i−1 个的内容差距（1 − 余弦），环形：第 0 个对最后一个。"""
    np = _np()
    if v.shape[0] == 0:
        return np.zeros(0, dtype=np.float32)
    prev = np.roll(v, 1, axis=0)
    return np.clip(1.0 - np.einsum("ij,ij->i", v, prev), 0.0, 2.0).astype(np.float32)


def looseness(v, window: int | None = None):
    """每一行与前后 `window` 行（含自身，不环绕）平均内容的余弦相似度。"""
    np = _np()
    n = int(v.shape[0])
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    if window is None:
        window = max(4, round(n / 60))
    csum = np.vstack([np.zeros((1, v.shape[1]), dtype=np.float64), np.cumsum(v, axis=0, dtype=np.float64)])
    lo = np.clip(np.arange(n) - window, 0, n)
    hi = np.clip(np.arange(n) + window + 1, 0, n)
    acc = csum[hi] - csum[lo]
    norms = np.linalg.norm(acc, axis=1)
    return np.clip(np.einsum("ij,ij->i", v, acc) / np.maximum(norms, 1e-12), -1.0, 1.0).astype(np.float32)


def group_count(n: int) -> int:
    """分几组：文件越多组越多，大约每 12 个文件的平方根一组，1~MAX_GROUPS。"""
    if n <= 1:
        return 1 if n == 1 else 0
    return max(1, min(MAX_GROUPS, round(math.sqrt(n / 12)), n))


def kmeans_groups(x, k: int, iters: int = 25):
    """球面 k-means（k-means++ 初始化，固定种子）。返回 (标签, 中心)，组号按组大小从大到小重排。"""
    np = _np()
    n = int(x.shape[0])
    if n == 0 or k <= 0:
        return np.zeros(0, dtype=np.int64), np.zeros((0, x.shape[1] if x.ndim == 2 else 0), dtype=np.float32)
    k = min(k, n)
    rng = np.random.default_rng(_SEED)
    centers = [x[int(rng.integers(n))]]
    closest = 1.0 - x @ centers[0]
    for _ in range(1, k):
        weights = np.clip(closest.astype(np.float64), 0.0, None) ** 2   # float64：概率和必须精确为 1
        total = float(weights.sum())
        pick = int(rng.integers(n)) if total <= 0 else int(rng.choice(n, p=weights / total))
        centers.append(x[pick])
        closest = np.minimum(closest, 1.0 - x @ x[pick])
    c = np.stack(centers).astype(np.float32)
    labels = np.zeros(n, dtype=np.int64)
    for _ in range(iters):
        labels_new = np.argmax(x @ c.T, axis=1)
        for j in range(k):
            members = x[labels_new == j]
            if len(members):
                mean = members.sum(axis=0)
                c[j] = mean / max(float(np.linalg.norm(mean)), 1e-12)
        if np.array_equal(labels_new, labels):
            labels = labels_new
            break
        labels = labels_new
    sizes = np.bincount(labels, minlength=k)
    rank = np.argsort(-sizes, kind="stable")          # 最大的一组编号 0，颜色表第一个颜色给它
    remap = np.empty(k, dtype=np.int64)
    remap[rank] = np.arange(k)
    return remap[labels], c[rank]


def _state_of(node: GraphNode) -> str:
    if node.extraction_state == "done":
        return "indexed"
    if node.extraction_state == "queued":
        return "ocr"
    if node.extraction_state == "failed":
        return "failed"
    return "pending"


def build_overview(
    graph: GraphResponse,
    vector_sets: Mapping[str, FileVectorSet],
) -> OverviewMapResponse:
    """由 graph() 读模型 + 每个库的文件向量，算出总览星图读模型。纯函数。"""
    np = _np()
    per_lib: list[tuple[str, list[GraphNode], list[int], object]] = []
    reduced_all: list[object] = []
    owners: list[tuple[int, int]] = []  # (库序号, 库内有向量的文件序号)
    for li, library_id in enumerate(graph.library_ids):
        docs = [
            node for node in graph.nodes
            if node.library_id == library_id and node.node_type not in _DOC_TYPES_EXCLUDED
        ]
        docs.sort(key=lambda node: node.path)
        fv = vector_sets.get(library_id)
        by_path = {path: i for i, path in enumerate(fv.paths)} if fv is not None else {}
        with_vec = [i for i, node in enumerate(docs) if node.path in by_path]
        if with_vec:
            full = np.asarray(fv.vectors, dtype=np.float32)[[by_path[docs[i].path] for i in with_vec]]
        else:
            full = np.zeros((0, 1), dtype=np.float32)
        per_lib.append((library_id, docs, with_vec, full))
        if with_vec:
            reduced = project(full)
            reduced_all.append(reduced)
            owners.extend((li, j) for j in range(len(with_vec)))

    # 跨库统一分组：同一类内容在不同库里也是同一个颜色
    labels = np.zeros(0, dtype=np.int64)
    centers = None
    stacked = None
    if reduced_all:
        dims = {r.shape[1] for r in reduced_all}
        if len(dims) == 1:
            stacked = np.vstack(reduced_all)
            labels, centers = kmeans_groups(stacked, group_count(stacked.shape[0]))
    label_of: dict[tuple[int, int], int] = {owner: int(labels[i]) for i, owner in enumerate(owners)} if len(labels) else {}

    libraries: list[OverviewLibrary] = []
    reduced_iter = iter(reduced_all)
    for li, (library_id, docs, with_vec, full) in enumerate(per_lib):
        ordered: list[OverviewFile] = []
        if with_vec:
            reduced = next(reduced_iter)
            order = seriate(reduced)
            v = full[order]
            gaps = neighbor_gaps(v)
            loose = looseness(v)
            for rank, j in enumerate(order.tolist()):
                node = docs[with_vec[j]]
                ordered.append(_file(node, label_of.get((li, j), -1), float(gaps[rank]), float(loose[rank])))
        placed = set(with_vec)
        for i, node in enumerate(docs):
            if i not in placed:
                ordered.append(_file(node, -1, 1.0, 0.0))
        points = sum(1 + f.chunks + f.pages for f in ordered)
        libraries.append(OverviewLibrary(library_id=library_id, files=tuple(ordered), points=points))

    groups: list[OverviewGroup] = []
    if stacked is not None and centers is not None:
        for g in range(len(centers)):
            members = np.nonzero(labels == g)[0]
            if len(members) == 0:
                continue
            sims = stacked[members] @ centers[g]
            best = members[np.argsort(-sims, kind="stable")[:GROUP_SAMPLES]]
            samples = []
            for row in best.tolist():
                li, j = owners[row]
                library_id, docs, with_vec, _ = per_lib[li]
                samples.append((library_id, docs[with_vec[j]].path))
            groups.append(OverviewGroup(group=g, size=int(len(members)), samples=tuple(samples)))
    return OverviewMapResponse(
        libraries=tuple(libraries),
        groups=tuple(groups),
        built_by="core.overview_map",
        layout_version=OVERVIEW_LAYOUT_VERSION,
    )


def _file(node: GraphNode, group: int, gap: float, loose: float) -> OverviewFile:
    pages = int(node.page_count or 0) if node.visual_state == "done" else 0
    return OverviewFile(
        path=node.path,
        node_id=node.node_id,
        node_type=node.node_type,
        chunks=int(node.chunks),
        pages=pages,
        state=_state_of(node),
        failure_reason=node.failure_reason,
        updated_ns=node.updated_ns,
        group=group,
        gap=round(gap, 4),
        loose=round(loose, 4),
    )


def chunk_groups_from_manifest(manifest: Mapping[str, object] | None, paths: Sequence[str]) -> dict[str, list[str]]:
    """从索引清单里取每个文件的块 id（清单是"库里实际有哪些块"的权威记录）。"""
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    if not isinstance(files, Mapping):
        return {}
    out: dict[str, list[str]] = {}
    for path in paths:
        record = files.get(path)
        if not isinstance(record, Mapping) or record.get("status") != "indexed":
            continue
        ids = record.get("chunk_ids")
        if isinstance(ids, list) and ids:
            out[path] = [str(i) for i in ids]
    return out
