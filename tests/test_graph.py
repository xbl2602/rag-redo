from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.contracts import GraphEdge, GraphNode, VisualPageState
from core.graph import build_graph, classify_theme, mark_hubs, select_semantic_edges


class TestGraphReadModel(unittest.TestCase):
    def test_classify_theme_uses_legacy_priority(self):
        self.assertEqual(classify_theme("WEMM/检索 chunk.md"), "wemm")
        self.assertEqual(classify_theme("MinerU/检索 chunk.md"), "mineru")
        self.assertEqual(classify_theme("检索 chunk.md"), "chunk")
        self.assertEqual(classify_theme("会议记录.md"), "daily")
        self.assertEqual(classify_theme("设置 config.md"), "config")
        self.assertEqual(classify_theme("普通.md"), "general")

    def test_build_graph_uses_included_files_relations_and_direct_page_ownership(self):
        manifest = {
            "files": {
                "a.md": {
                    "status": "indexed",
                    "mtime_ns": 1_000_000_000,
                    "chunk_ids": ["lib1:a.md:0", "lib1:a.md:1"],
                },
                "b.md": {"status": "indexed", "mtime_ns": 2_000_000_000, "chunk_ids": []},
                "broken.pdf": {"status": "failed", "failure_reason": "extract-failed", "chunk_ids": []},
                "new.pdf": None,
            }
        }
        page_state = VisualPageState(
            library_id="lib1",
            path="new.pdf",
            provider_id="official-visual-wemm",
            status="indexed",
            failure_reason=None,
            pages=(1, 2),
        )
        response = build_graph(
            library_ids=("lib1",),
            manifests={"lib1": manifest},
            relation_edges={"lib1": (("a.md", "b.md"),)},
            included_files={
                "lib1": (
                    ("a.md", True, "included"),
                    ("b.md", True, "included"),
                    ("broken.pdf", True, "included"),
                    ("new.pdf", True, "included"),
                    ("excluded.md", False, "excluded"),
                )
            },
            page_states={"lib1": (page_state,)},
        )
        by_id = {node.node_id: node for node in response.nodes}
        self.assertEqual(response.library_ids, ("lib1",))
        self.assertEqual((response.stats.nodes, response.stats.edges), (6, 3))
        self.assertEqual(by_id["lib1|a.md"].chunks, 2)
        self.assertEqual(by_id["lib1|broken.pdf"].extraction_state, "failed")
        self.assertEqual(by_id["lib1|broken.pdf"].chunks, 0)
        self.assertEqual(by_id["lib1|broken.pdf"].failure_reason, "extract-failed")
        self.assertEqual(by_id["lib1|new.pdf"].extraction_state, "none")
        self.assertEqual(by_id["lib1|new.pdf"].visual_state, "done")
        self.assertEqual(by_id["lib1|new.pdf"].page_count, 2)
        self.assertIn("lib1|wemm|new.pdf|p1", by_id)
        self.assertIn("lib1|wemm|new.pdf|p2", by_id)
        self.assertNotIn("lib1|excluded.md", by_id)
        self.assertIn(
            GraphEdge("lib1|a.md", "lib1|b.md", "link"),
            response.edges,
        )
        self.assertIn(
            GraphEdge("lib1|new.pdf", "lib1|wemm|new.pdf|p1", "page"),
            response.edges,
        )

    def test_more_than_24_pages_becomes_pagegroup(self):
        state = VisualPageState(
            library_id="lib1",
            path="long.pdf",
            provider_id="official-visual-wemm",
            status="indexed",
            failure_reason=None,
            pages=tuple(range(1, 26)),
        )
        response = build_graph(
            library_ids=("lib1",),
            manifests={
                "lib1": {
                    "files": {
                        "long.pdf": {
                            "status": "indexed",
                            "mtime_ns": 1,
                            "chunk_ids": ["lib1:long.pdf:0"],
                        }
                    }
                }
            },
            relation_edges={},
            included_files={"lib1": (("long.pdf", True, "included"),)},
            page_states={"lib1": (state,)},
        )
        group = next(node for node in response.nodes if node.node_type == "pagegroup")
        self.assertEqual(group.node_id, "lib1|wemm|long.pdf|grp")
        self.assertEqual(group.page_number, 25)
        self.assertEqual(group.page_count, 25)
        self.assertEqual(
            [edge for edge in response.edges if edge.kind == "page"],
            [GraphEdge("lib1|long.pdf", group.node_id, "page")],
        )

    def test_hubs_use_structural_degree_and_stable_tie_break(self):
        nodes = [GraphNode(f"n{i}", "lib", f"{i}.md", "md") for i in range(7)]
        edges = [GraphEdge("n0", f"n{i}", "link") for i in range(1, 7)]
        marked = mark_hubs(nodes, edges)
        hubs = [node.node_id for node in marked if node.is_hub]
        self.assertEqual(hubs, ["n0", "n1", "n2", "n3", "n4"])


class TestSemanticEdgeSelection(unittest.TestCase):
    def test_threshold_top_four_deduplication_and_no_self_edges(self):
        ids = ["n0", "n1", "n2", "n3", "n4", "n5"]
        vectors = [
            (1.0, 0.0),
            (0.99, 0.01),
            (0.98, 0.02),
            (0.97, 0.03),
            (0.96, 0.04),
            (0.95, 0.05),
            (0.0, 1.0),
        ][: len(ids)]
        edges = select_semantic_edges(ids, vectors, threshold=0.9, per_node=4, cap=3)
        self.assertEqual(len(edges), 3)
        self.assertTrue(all(edge.source != edge.target for edge in edges))
        self.assertTrue(all(edge.source < edge.target for edge in edges))
        self.assertTrue(all(edge.similarity >= 0.9 for edge in edges))


if __name__ == "__main__":
    unittest.main()
