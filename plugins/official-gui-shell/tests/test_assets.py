from __future__ import annotations

import re
import unittest
from pathlib import Path


PLUGIN_DIR = Path(__file__).parent.parent
ASSET_PATH = PLUGIN_DIR / "official_gui_shell" / "assets" / "index.html"
PLUGIN_PATH = PLUGIN_DIR / "plugin.toml"


class TestGraphAssets(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.asset = ASSET_PATH.read_text(encoding="utf-8")

    def test_graph_api_calls_use_selected_library_scope(self) -> None:
        self.assertIn("api().graph(libraryScope())", self.asset)
        self.assertIn("api().graph_semantic_edges(libraryScope(), state.graph.threshold)", self.asset)
        self.assertIn('value="0.62"', self.asset)

    def test_gui_exposes_multi_library_and_document_controls(self) -> None:
        for text in (
            "selectedLibraryIds",
            "api().search(scope, query, 10)",
            "api().read_document",
            "api().note_relations",
            "api().index_failures",
            "open_source",
            "库摘要",
            "设置",
        ):
            self.assertIn(text, self.asset)

    def test_semantic_edges_are_opt_in_and_surface_response_error(self) -> None:
        self.assertIn('<input id="graph-semantic-toggle" type="checkbox" />', self.asset)
        self.assertIn("semanticVisible: false", self.asset)
        self.assertIn("state.graph.semanticEdges = result.edges", self.asset)
        self.assertIn('state.graph.semanticError = String(result.error || "")', self.asset)
        self.assertIn("setGraphAlert(state.graph.semanticError, true)", self.asset)

    def test_page_ownership_is_inferred_from_node_types(self) -> None:
        self.assertIn('node.type === "page" || node.type === "pagegroup"', self.asset)
        self.assertIn('node.type === "pdf"', self.asset)
        self.assertIn("endpoints.some((node) => node.id === parent.id)", self.asset)
        self.assertIn('edge.kind === "page" && graphEdgeVisible(edge) && pageEdgeChild(edge)', self.asset)

    def test_page_and_pagegroup_share_one_visibility_layer(self) -> None:
        self.assertIn("return !graphNodeIsPage(node) || state.graph.showPages", self.asset)
        self.assertIn("return state.graph.showPages", self.asset)
        self.assertIn('id="graph-pages-toggle" type="checkbox" checked', self.asset)

    def test_graph_asset_has_no_cache_topology_or_external_urls(self) -> None:
        lowered = self.asset.lower()
        self.assertNotIn("cache", lowered)
        self.assertNotIn("缓存层", self.asset)
        self.assertIsNone(re.search(r"pdf\s*(?:→|->|=>)\s*md", lowered))
        urls = re.findall(r"https?://[^\s\"'<>]+", self.asset)
        self.assertEqual([url for url in urls if url != "http://www.w3.org/2000/svg"], [])

    def test_graph_exposes_terminal_state_and_legacy_threshold_range(self) -> None:
        self.assertIn('max="0.85"', self.asset)
        self.assertIn("function graphNodeState(node)", self.asset)
        self.assertIn("st-${nodeState}", self.asset)
        self.assertIn('["状态", graphNodeState(node)]', self.asset)

    def test_plugin_version_was_bumped(self) -> None:
        self.assertIn('version = "0.2.0"', PLUGIN_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
