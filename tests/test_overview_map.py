"""BC-18：总览星图读模型（core/overview_map.py）。

总览页把每个库画成一根光管，文件沿管子排开。这里守的是"数据怎么排、怎么分组"：
相近内容必须挨在一起、同一类内容同一个颜色、结果可复现、没有内容向量的文件不会丢。
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from core.contracts import FileVectorSet, GraphNode, GraphResponse, GraphStats  # noqa: E402
from core.overview_map import (  # noqa: E402
    OVERVIEW_LAYOUT_VERSION,
    build_overview,
    chunk_groups_from_manifest,
    group_count,
    kmeans_groups,
    looseness,
    neighbor_gaps,
    project,
    seriate,
)


def _clustered_vectors(n_clusters: int, per: int, dim: int = 32, noise: float = 0.08, seed: int = 7):
    """n_clusters 个方向随机的主题，每个主题 per 个文件（主题向量 + 小噪声），行已打乱。"""
    rng = np.random.default_rng(seed)
    centers = rng.standard_normal((n_clusters, dim))
    rows, labels = [], []
    for c in range(n_clusters):
        for _ in range(per):
            rows.append(centers[c] / np.linalg.norm(centers[c]) + rng.standard_normal(dim) * noise)
            labels.append(c)
    x = np.asarray(rows, dtype=np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    perm = rng.permutation(len(rows))
    return x[perm], np.asarray(labels)[perm]


def _graph(library_id: str, paths: list[str], **overrides) -> GraphResponse:
    nodes = []
    for path in paths:
        fields = dict(
            node_id=f"{library_id}|{path}", library_id=library_id, path=path,
            node_type=path.rsplit(".", 1)[-1], chunks=3, extraction_state="done",
        )
        fields.update(overrides.get(path, {}))
        nodes.append(GraphNode(**fields))
    return GraphResponse(nodes=tuple(nodes), edges=(), library_ids=(library_id,), stats=GraphStats(len(nodes), 0))


def _vector_set(library_id: str, paths: list[str], vectors) -> FileVectorSet:
    return FileVectorSet(
        library_id=library_id, generation="g1", paths=tuple(paths), vectors=vectors,
        dim=int(vectors.shape[1]), produced_by="test", store_version="t",
    )


class TestSeriation(unittest.TestCase):
    def test_similar_files_end_up_next_to_each_other(self) -> None:
        x, labels = _clustered_vectors(5, 40)
        order = seriate(project(x))
        seq = labels[order]
        switches = int(np.sum(seq[1:] != seq[:-1]))
        # 5 个主题完美排开只需要 4 次换主题；允许少量碎片，但绝不能是随机顺序（约 160 次）
        self.assertLessEqual(switches, 15, f"主题在环上被切得太碎：{switches} 次切换")

    def test_adjacent_similarity_is_far_above_random_pairs(self) -> None:
        x, _ = _clustered_vectors(6, 30, noise=0.15)
        v = x[seriate(project(x))]
        adjacent = float(np.mean(np.einsum("ij,ij->i", v[1:], v[:-1])))
        rng = random.Random(3)
        pairs = [(rng.randrange(len(v)), rng.randrange(len(v))) for _ in range(2000)]
        rand = float(np.mean([v[a] @ v[b] for a, b in pairs if a != b]))
        self.assertGreater(adjacent, rand + 0.4)

    def test_order_is_a_permutation_and_deterministic(self) -> None:
        x, _ = _clustered_vectors(3, 20)
        a, b = seriate(project(x)), seriate(project(x))
        self.assertEqual(sorted(a.tolist()), list(range(len(x))))
        self.assertEqual(a.tolist(), b.tolist())

    def test_tiny_inputs_do_not_crash(self) -> None:
        for n in (0, 1, 2):
            x = np.eye(3, dtype=np.float32)[:n]
            self.assertEqual(sorted(seriate(project(x)).tolist()), list(range(n)))

    def test_projection_reduces_high_dimensions_and_keeps_unit_length(self) -> None:
        x, _ = _clustered_vectors(2, 5, dim=300)
        p = project(x)
        self.assertEqual(p.shape, (10, 64))
        self.assertTrue(np.allclose(np.linalg.norm(p, axis=1), 1.0, atol=1e-5))


class TestGapsAndLooseness(unittest.TestCase):
    def test_gap_is_one_minus_cosine_to_the_previous_file_wrapping_around(self) -> None:
        v = np.asarray([[1, 0], [0, 1], [-1, 0]], dtype=np.float32)
        gaps = neighbor_gaps(v)
        self.assertAlmostEqual(float(gaps[1]), 1.0, places=5)   # 垂直
        self.assertAlmostEqual(float(gaps[2]), 1.0, places=5)
        self.assertAlmostEqual(float(gaps[0]), 2.0, places=5)   # 环形：第 0 个对最后一个（反向）

    def test_an_outlier_among_a_tight_run_is_the_loosest(self) -> None:
        v = np.tile(np.asarray([[1, 0, 0]], dtype=np.float32), (21, 1))
        v[10] = [0, 1, 0]
        loose = looseness(v, window=4)
        self.assertEqual(int(np.argmin(loose)), 10)
        self.assertGreater(float(loose[0]), 0.9)


class TestGroups(unittest.TestCase):
    def test_group_count_grows_with_files_and_is_capped(self) -> None:
        self.assertEqual(group_count(0), 0)
        self.assertEqual(group_count(1), 1)
        self.assertLessEqual(group_count(10_000_000), 16)
        self.assertLess(group_count(50), group_count(5000))

    def test_kmeans_recovers_clear_topics_and_numbers_groups_by_size(self) -> None:
        x, labels = _clustered_vectors(4, 25, noise=0.05)
        extra, _ = _clustered_vectors(1, 40, noise=0.05, seed=99)   # 再加一个明显更大的主题
        x = np.vstack([x, extra])
        labels = np.concatenate([labels, np.full(40, 9)])
        got, centers = kmeans_groups(x, 5)
        self.assertEqual(centers.shape[0], 5)
        for topic in set(labels.tolist()):
            members = got[labels == topic]
            self.assertEqual(len(set(members.tolist())), 1, f"主题 {topic} 被拆成了几组")
        self.assertEqual(int(np.bincount(got).argmax()), 0, "最大的一组必须是 0 号（颜色表第一个颜色）")


class TestBuildOverview(unittest.TestCase):
    def test_every_document_appears_once_in_content_order_with_its_group(self) -> None:
        x, labels = _clustered_vectors(3, 12)
        paths = [f"n{i:03d}.md" for i in range(len(x))]
        graph = _graph("lib", paths)
        resp = build_overview(graph, {"lib": _vector_set("lib", paths, x)})
        self.assertEqual(resp.layout_version, OVERVIEW_LAYOUT_VERSION)
        self.assertEqual(resp.built_by, "core.overview_map")
        (lib,) = resp.libraries
        self.assertEqual(sorted(f.path for f in lib.files), sorted(paths))
        topic = {p: int(labels[i]) for i, p in enumerate(paths)}
        seq = [topic[f.path] for f in lib.files]
        self.assertLessEqual(sum(1 for a, b in zip(seq, seq[1:]) if a != b), 6)
        by_topic: dict[int, set[int]] = {}
        for f in lib.files:
            by_topic.setdefault(topic[f.path], set()).add(f.group)
        self.assertTrue(all(len(g) == 1 for g in by_topic.values()), by_topic)
        self.assertEqual(sum(g.size for g in resp.groups), len(paths))
        for g in resp.groups:
            self.assertLessEqual(len(g.samples), 3)
            for library_id, path in g.samples:
                self.assertEqual(library_id, "lib")
                self.assertIn(path, paths)

    def test_gaps_match_the_emitted_order(self) -> None:
        x, _ = _clustered_vectors(2, 8)
        paths = [f"f{i}.md" for i in range(len(x))]
        resp = build_overview(_graph("lib", paths), {"lib": _vector_set("lib", paths, x)})
        files = resp.libraries[0].files
        vec = {p: x[i] for i, p in enumerate(paths)}
        for i, f in enumerate(files):
            prev = files[i - 1]
            expected = 1.0 - float(vec[f.path] @ vec[prev.path])
            self.assertAlmostEqual(f.gap, max(0.0, expected), places=3)

    def test_files_without_vectors_are_kept_at_the_end_in_path_order(self) -> None:
        x, _ = _clustered_vectors(1, 4)
        with_vec = ["b.md", "d.md", "f.md", "h.md"]
        without = ["a.pdf", "c.md"]
        graph = _graph(
            "lib", sorted(with_vec + without),
            **{"a.pdf": {"extraction_state": "queued", "chunks": 0},
               "c.md": {"extraction_state": "failed", "chunks": 0, "failure_reason": "unreadable"}},
        )
        resp = build_overview(graph, {"lib": _vector_set("lib", with_vec, x)})
        files = resp.libraries[0].files
        self.assertEqual([f.path for f in files[-2:]], ["a.pdf", "c.md"])
        tail = {f.path: f for f in files[-2:]}
        self.assertEqual(tail["a.pdf"].state, "ocr")
        self.assertEqual(tail["c.md"].state, "failed")
        self.assertEqual(tail["c.md"].failure_reason, "unreadable")
        for f in tail.values():
            self.assertEqual((f.group, f.gap, f.loose), (-1, 1.0, 0.0))

    def test_points_count_files_chunks_and_indexed_pages_only(self) -> None:
        graph = _graph(
            "lib", ["a.pdf", "b.md", "c.pdf"],
            **{"a.pdf": {"chunks": 4, "page_count": 7, "visual_state": "done"},
               "b.md": {"chunks": 2},
               "c.pdf": {"chunks": 5, "page_count": 9, "visual_state": "failed"}},
        )
        resp = build_overview(graph, {})
        lib = resp.libraries[0]
        pages = {f.path: f.pages for f in lib.files}
        self.assertEqual(pages, {"a.pdf": 7, "b.md": 0, "c.pdf": 0})
        self.assertEqual(lib.points, 3 + (4 + 2 + 5) + 7)
        self.assertEqual(resp.groups, ())

    def test_page_nodes_of_the_graph_are_not_files(self) -> None:
        graph = _graph("lib", ["a.pdf"], **{"a.pdf": {"page_count": 2, "visual_state": "done"}})
        page = GraphNode(node_id="lib|wemm|a.pdf|p1", library_id="lib", path="a.pdf", node_type="page")
        graph = GraphResponse(nodes=graph.nodes + (page,), edges=(), library_ids=("lib",), stats=GraphStats(2, 0))
        self.assertEqual(len(build_overview(graph, {}).libraries[0].files), 1)

    def test_groups_are_shared_across_libraries(self) -> None:
        x, labels = _clustered_vectors(2, 16, noise=0.04)
        left = [i for i in range(len(x)) if i % 2 == 0]
        right = [i for i in range(len(x)) if i % 2 == 1]
        pa = [f"a{i}.md" for i in left]
        pb = [f"b{i}.md" for i in right]
        nodes = _graph("A", pa).nodes + _graph("B", pb).nodes
        graph = GraphResponse(nodes=nodes, edges=(), library_ids=("A", "B"), stats=GraphStats(len(nodes), 0))
        resp = build_overview(graph, {"A": _vector_set("A", pa, x[left]), "B": _vector_set("B", pb, x[right])})
        topic_group: dict[int, set[int]] = {}
        for lib, idx in ((resp.libraries[0], left), (resp.libraries[1], right)):
            name_to_topic = {f"{lib.library_id.lower()}{i}.md": int(labels[i]) for i in idx}
            for f in lib.files:
                topic_group.setdefault(name_to_topic[f.path], set()).add(f.group)
        self.assertTrue(all(len(g) == 1 for g in topic_group.values()), topic_group)

    def test_same_input_gives_the_same_map(self) -> None:
        x, _ = _clustered_vectors(3, 10)
        paths = [f"n{i}.md" for i in range(len(x))]
        a = build_overview(_graph("lib", paths), {"lib": _vector_set("lib", paths, x)})
        b = build_overview(_graph("lib", paths), {"lib": _vector_set("lib", paths, x)})
        self.assertEqual(a, b)

    def test_empty_scope_gives_an_empty_map(self) -> None:
        resp = build_overview(GraphResponse(nodes=(), edges=(), library_ids=(), stats=GraphStats(0, 0)), {})
        self.assertEqual((resp.libraries, resp.groups, resp.error), ((), (), None))


class TestChunkGroupsFromManifest(unittest.TestCase):
    def test_only_indexed_files_with_chunk_ids(self) -> None:
        manifest = {"files": {
            "a.md": {"status": "indexed", "chunk_ids": ["x", "y"]},
            "b.md": {"status": "failed", "chunk_ids": ["z"]},
            "c.md": {"status": "indexed", "chunk_ids": []},
        }}
        self.assertEqual(chunk_groups_from_manifest(manifest, ["a.md", "b.md", "c.md", "d.md"]), {"a.md": ["x", "y"]})
        self.assertEqual(chunk_groups_from_manifest(None, ["a.md"]), {})


if __name__ == "__main__":
    unittest.main()
