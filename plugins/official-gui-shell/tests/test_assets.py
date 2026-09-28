"""BC-15 阶段A 门禁：GUI 资产必须与 obsidian-rag/guiweb/ui 逐字节一致。

验收基准冻结在 tests/fixtures/legacy_guiweb_contract.json（sha256 + 字节数 +
37 个契约方法 + 5 类推送 + 7 个视图），这样本仓库的 CI 不需要旧项目仓库在场
也能独立校验"复刻"这件事，而不是靠人眼比对。

同时校验 BC-15 第(5)条零业务逻辑：前端不得出现检索算法计算，排序只能是
视图排序，且不得直连文件/网络（contracts.md 通用约定第 6 条）。
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from collections import Counter
from pathlib import Path


PLUGIN_DIR = Path(__file__).parent.parent
ASSETS_DIR = PLUGIN_DIR / "official_gui_shell" / "assets"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "legacy_guiweb_contract.json"

#: 前端绝对不允许出现的检索算法 token——这些一旦出现就说明业务判断漏到了
#: 前端，违反 AGENTS.md 第 6 节"入口不得各自实现排序、过滤或错误处理"。
FORBIDDEN_ALGORITHM_TOKENS = (
    "RRF",
    "rrf",
    "bm25",
    "BM25",
    "rerank",
    "Rerank",
    "cosine",
    "tf-idf",
    "tfidf",
    "BGE",
    "reciprocal_rank",
)

#: 允许出现的 4 处 .sort( 都是视图排序：按长度排列表 ×2、按 conf 排失败明细
#: 弹窗、按 s 排图谱节点尺寸。白名单化而不是禁掉 .sort，防止以后有人以"视图需要"
#: 为名把检索排序搬进前端。
SORT_PATTERN = r"\.sort\(function \(a, b\) \{ return ([^;]+); \}\);"
BENIGN_SORT_COMPARATORS = {
    "b.length - a.length": 2,
    "b.conf - a.conf": 1,
    "b.s - a.s": 1,
}


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class TestLegacyAssetParity(unittest.TestCase):
    """阶段A 量化指标：4/4 资产逐字节一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _fixture()
        cls.app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")

    def test_assets_directory_contains_exactly_the_legacy_ui_files(self) -> None:
        expected = sorted(item["name"] for item in self.fixture["assets"])
        actual = sorted(p.name for p in ASSETS_DIR.iterdir() if p.is_file())
        self.assertEqual(actual, expected)

    def test_every_legacy_asset_is_byte_identical(self) -> None:
        for item in self.fixture["assets"]:
            with self.subTest(asset=item["name"]):
                path = ASSETS_DIR / item["name"]
                self.assertTrue(path.is_file(), f"缺少资产 {item['name']}")
                raw = path.read_bytes()
                self.assertEqual(
                    len(raw),
                    item["bytes"],
                    f"{item['name']} 字节数与旧项目不一致",
                )
                self.assertEqual(
                    hashlib.sha256(raw).hexdigest().upper(),
                    item["sha256"].upper(),
                    f"{item['name']} sha256 与旧项目不一致——BC-15 要求逐字节复刻",
                )

    def test_index_html_still_loads_the_three_siblings(self) -> None:
        html = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
        for sibling in ("app.css", "app.js", "mock.js"):
            self.assertIn(sibling, html)


class TestLegacyPushChannel(unittest.TestCase):
    """BC-15 第(3)条：五类推送与 window.__push 必须在场。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _fixture()
        cls.app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")

    def test_app_js_defines_production_push_entry(self) -> None:
        # 旧项目问题47：生产版从未定义 window.__push，推送被守卫静默吞掉，
        # KPI 与进度永远停在启动那一刻。移植后必须保留这个定义。
        self.assertIn("window.__push = function (type, payloadJson)", self.app_js)
        self.assertIn("new CustomEvent(type, { detail: payloadJson })", self.app_js)

    def test_app_js_listens_to_snapshot_log_and_preview(self) -> None:
        for push_type in ("snapshot", "log", "preview"):
            with self.subTest(push=push_type):
                self.assertIn(
                    f"window.addEventListener('{push_type}'",
                    self.app_js,
                )

    def test_mock_js_is_gated_so_production_never_shows_fake_data(self) -> None:
        mock_js = (ASSETS_DIR / "mock.js").read_text(encoding="utf-8")
        self.assertIn("window.pywebview", mock_js)
        self.assertIn("__RAG_MOCK", mock_js)

    def test_fixture_declares_all_five_push_types(self) -> None:
        self.assertEqual(
            sorted(self.fixture["push_types"]),
            sorted(["snapshot", "log", "alert", "notice", "preview"]),
        )


class TestNoBusinessLogicInFrontend(unittest.TestCase):
    """BC-15 第(5)条：前端零业务判断。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")

    def test_no_retrieval_algorithm_tokens(self) -> None:
        for token in FORBIDDEN_ALGORITHM_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(
                    token,
                    self.app_js,
                    f"前端出现检索算法 token {token}——业务判断必须留在 core",
                )

    def test_every_sort_is_a_whitelisted_view_sort(self) -> None:
        found = re.findall(SORT_PATTERN, self.app_js)
        self.assertEqual(
            Counter(found),
            Counter(BENIGN_SORT_COMPARATORS),
            f"前端 .sort( 与基准不符：{found}",
        )

    def test_frontend_does_not_touch_files_or_network(self) -> None:
        for token in ("fetch(", "XMLHttpRequest", "require(", "import("):
            with self.subTest(token=token):
                self.assertNotIn(token, self.app_js)


if __name__ == "__main__":
    unittest.main()
