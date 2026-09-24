from __future__ import annotations

import math
import re
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .contracts import (
    GraphEdge,
    GraphNode,
    GraphResponse,
    GraphStats,
    SemanticGraphEdge,
    VisualPageState,
)

THEME_RULES = (
    ("wemm", ("wemm", "页级", "视觉导航")),
    ("mineru", ("mineru", "ocr", "扫描")),
    ("chunk", ("切块", "清洗", "chunk", "嵌入", "向量", "检索", "重排", "embedding")),
    ("daily", ("journal", "日记", "周会", "会议记录", "meeting")),
    ("config", ("设置", "配置", "config", "门禁", "隐私")),
)
MAX_PAGE_NODES = 24
BIG_DEGREE = 5


def classify_theme(path: str) -> str:
    lowered = path.lower()
    for name, keywords in THEME_RULES:
        if any(keyword.lower() in lowered for keyword in keywords):
            return name
    return "general"


def _node_type(path: str) -> str:
    suffix = Path(path).suffix.lstrip(".").lower()
    return suffix or "md"


def _page_node_id(library_id: str, path: str, provider_id: str, suffix: str) -> str:
    if provider_id == "official-visual-wemm":
        return f"{library_id}|wemm|{path}|{suffix}"
    safe_provider = re.sub(r"[^\w.-]", "_", provider_id)
    return f"{library_id}|{safe_provider}|{path}|{suffix}"


def _manifest_files(manifest: Mapping[str, object] | None) -> dict[str, dict]:
    if not manifest:
        return {}
    files = manifest.get("files")
    if not isinstance(files, dict):
        return {}
    return {
        str(path): dict(record)
        for path, record in sorted(files.items())
        if isinstance(path, str) and isinstance(record, dict)
    }


def _document_nodes(
    library_id: str,
    manifest: Mapping[str, object] | None,
    included_paths: set[str],
) -> list[GraphNode]:
    records = _manifest_files(manifest)
    nodes: list[GraphNode] = []
    for path in sorted(included_paths):
        record = records.get(path)
        if record is None:
            if not path.lower().endswith(".pdf"):
                continue
            nodes.append(
                GraphNode(
                    node_id=f"{library_id}|{path}",
                    library_id=library_id,
                    path=path,
                    node_type="pdf",
                    theme=classify_theme(path),
                    extraction_state="none",
                )
            )
            continue
        status = str(record.get("status") or "failed")
        chunk_ids = record.get("chunk_ids", [])
        chunks = len(chunk_ids) if isinstance(chunk_ids, list) else 0
        if status == "indexed":
            extraction_state = "done"
        elif record.get("failure_state") in {"scanned", "deferred"}:
            extraction_state = "queued"
        else:
            extraction_state = "failed"
        if _node_type(path) == "pdf" and extraction_state != "done":
            chunks = 0
        mtime_ns = record.get("mtime_ns")
        updated_ns = int(mtime_ns) if isinstance(mtime_ns, (int, float)) else None
        nodes.append(
            GraphNode(
                node_id=f"{library_id}|{path}",
                library_id=library_id,
                path=path,
                node_type=_node_type(path),
                chunks=chunks,
                updated_ns=updated_ns,
                failure_reason=str(record["failure_reason"]) if record.get("failure_reason") else None,
                theme=classify_theme(path),
                extraction_state=extraction_state,
            )
        )
    return nodes


def _apply_page_states(
    library_id: str,
    nodes: list[GraphNode],
    states: Iterable[VisualPageState],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    by_path = {node.path: index for index, node in enumerate(nodes) if node.node_type == "pdf"}
    edges: list[GraphEdge] = []
    for state in sorted(states, key=lambda item: (item.path, item.provider_id)):
        if state.library_id != library_id or state.path not in by_path:
            continue
        pages = tuple(sorted({page for page in state.pages if page > 0}))
        visual_state = "none"
        if state.status in {"indexed", "partial"} and pages:
            visual_state = "done"
        elif state.status == "failed":
            visual_state = "failed"
        parent_index = by_path[state.path]
        parent = nodes[parent_index]
        nodes[parent_index] = replace(
            parent,
            visual_state=visual_state,
            failure_reason=parent.failure_reason or state.failure_reason,
            page_count=len(pages) or None,
        )
        parent = nodes[parent_index]
        if not pages:
            continue
        if len(pages) <= MAX_PAGE_NODES:
            for page in pages:
                page_id = _page_node_id(library_id, state.path, state.provider_id, f"p{page}")
                nodes.append(
                    GraphNode(
                        node_id=page_id,
                        library_id=library_id,
                        path=state.path,
                        node_type="page",
                        theme=parent.theme,
                        extraction_state="done",
                        visual_state="done",
                        page_number=page,
                    )
                )
                edges.append(GraphEdge(parent.node_id, page_id, "page"))
        else:
            page_id = _page_node_id(library_id, state.path, state.provider_id, "grp")
            nodes.append(
                GraphNode(
                    node_id=page_id,
                    library_id=library_id,
                    path=state.path,
                    node_type="pagegroup",
                    theme=parent.theme,
                    extraction_state="done",
                    visual_state="done",
                    page_number=len(pages),
                    page_count=len(pages),
                )
            )
            edges.append(GraphEdge(parent.node_id, page_id, "page"))
    return nodes, edges


def mark_hubs(nodes: Sequence[GraphNode], edges: Sequence[GraphEdge]) -> tuple[GraphNode, ...]:
    degree: dict[str, int] = {}
    for edge in edges:
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1
    hubs = {
        node_id
        for node_id, _ in sorted(degree.items(), key=lambda item: (-item[1], item[0]))[:BIG_DEGREE]
    }
    return tuple(replace(node, is_hub=node.node_id in hubs) for node in nodes)


def build_graph(
    *,
    library_ids: Sequence[str],
    manifests: Mapping[str, Mapping[str, object] | None],
    relation_edges: Mapping[str, Sequence[tuple[str, str]]],
    included_files: Mapping[str, Sequence[tuple[str, bool, str]]],
    page_states: Mapping[str, Sequence[VisualPageState]],
) -> GraphResponse:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for library_id in library_ids:
        included_paths = {
            path
            for path, included, _reason in included_files.get(library_id, ())
            if included
        }
        library_nodes = _document_nodes(library_id, manifests.get(library_id), included_paths)
        library_nodes, page_edges = _apply_page_states(
            library_id,
            library_nodes,
            page_states.get(library_id, ()),
        )
        nodes.extend(library_nodes)
        edges.extend(page_edges)
        document_ids = {f"{library_id}|{path}" for path in included_paths}
        for source, target in relation_edges.get(library_id, ()):
            source_id, target_id = f"{library_id}|{source}", f"{library_id}|{target}"
            if source_id not in document_ids or target_id not in document_ids:
                continue
            first, second = sorted((source_id, target_id))
            edges.append(GraphEdge(first, second, "link"))
    marked = mark_hubs(nodes, edges)
    return GraphResponse(
        nodes=marked,
        edges=tuple(edges),
        library_ids=tuple(library_ids),
        stats=GraphStats(nodes=len(marked), edges=len(edges)),
    )


def select_semantic_edges(
    node_ids: Sequence[str],
    vectors: Sequence[Sequence[float]],
    *,
    threshold: float = 0.62,
    per_node: int = 4,
    cap: int = 2000,
) -> tuple[SemanticGraphEdge, ...]:
    selected: list[SemanticGraphEdge] = []
    seen: set[tuple[str, str]] = set()
    for index, vector in enumerate(vectors):
        norm = math.sqrt(sum(float(value) ** 2 for value in vector)) or 1.0
        candidates: list[tuple[float, int]] = []
        for other_index, other in enumerate(vectors):
            if index == other_index:
                continue
            other_norm = math.sqrt(sum(float(value) ** 2 for value in other)) or 1.0
            similarity = sum(float(left) * float(right) for left, right in zip(vector, other)) / (norm * other_norm)
            if similarity >= threshold:
                candidates.append((similarity, other_index))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        for similarity, other_index in candidates[:per_node]:
            first, second = sorted((node_ids[index], node_ids[other_index]))
            key = (first, second)
            if key in seen:
                continue
            seen.add(key)
            selected.append(SemanticGraphEdge(first, second, round(similarity, 3)))
            if len(selected) >= cap:
                return tuple(selected)
    return tuple(selected)
