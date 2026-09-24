from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
for plugin_dir in (REPO_ROOT / "plugins").glob("*"):
    if plugin_dir.is_dir() and str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))

from core.contracts import SearchAdviceInput, SearchResult
from core.runtime import PluginRuntime, PluginState


def _result(
    chunk_id: str,
    path: str,
    confidence: float,
    *,
    library: str = "notes",
    heading: str = "",
    backfilled: bool = False,
) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        library_id=library,
        path=path,
        heading_breadcrumb=heading,
        text="正文",
        confidence=confidence,
        backfilled=backfilled,
    )


class TestResultAdvisorPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.runtime = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.runtime.scan()
        self.runtime.load("official-result-advisor")
        self.assertEqual(
            self.runtime.plugins["official-result-advisor"].state,
            PluginState.LOADED,
        )
        self.runtime.enable("official-result-advisor")
        self.instance = self.runtime.plugins["official-result-advisor"].instance

    def _advise(
        self,
        results,
        *,
        query: str = "",
        mode: str = "body",
        top_k: int = 5,
        default_libraries=(),
    ):
        request = SearchAdviceInput(
            results=tuple(results),
            query=query,
            mode=mode,
            top_k=top_k,
            default_libraries=tuple(default_libraries),
            warn_threshold=0.30,
            strong_threshold=0.75,
        )
        return self.instance.advise(request)

    def test_empty_results_return_no_advice(self):
        self.assertEqual(self._advise([]), ())

    def test_low_confidence_and_keyword_rules(self):
        advice = self._advise(
            [_result("c1", "a.md", 0.1), _result("c2", "b.md", 0.2)],
            query="Fluent",
        )
        self.assertEqual(len(advice), 2)
        self.assertIn("相关度都偏低", advice[0])
        self.assertIn("关键词式查询", advice[1])

    def test_duplicate_heading_uses_complete_paths(self):
        advice = self._advise(
            [
                _result("c1", "a/主题.md", 0.8, heading="Fluent 配置"),
                _result("c2", "b/主题.md", 0.7, heading="fluent配置"),
            ]
        )
        self.assertTrue(any("同名不同目录" in line for line in advice))

    def test_non_default_config_library_is_called_out(self):
        advice = self._advise(
            [_result("c1", "skills/a.md", 0.8, library="skills")],
            default_libraries=["notes"],
        )
        self.assertTrue(any("非笔记库" in line for line in advice))

    def test_many_strong_hits_suggest_larger_top_k(self):
        advice = self._advise(
            [_result(f"c{i}", f"{i}.md", 0.9) for i in range(3)],
            top_k=3,
        )
        self.assertTrue(any("top_k 调大" in line for line in advice))

    def test_single_file_concentration_suggests_read_document_not_exclude(self):
        """经操作者确认的偏离（2026-09-25）：旧项目 advice.py:148-150 在"单
        文件集中"场景建议 `exclude="<文件路径>"`，但检索入口的 exclude 实际
        语义是库 ID，照做整次检索直接报错（继承的既有 bug）。建议必须指向
        可执行的入口，不得再把文件路径塞进 exclude。"""
        advice = self._advise(
            [
                _result("c1", "docs/深度专题.md", 0.8),
                _result("c2", "docs/深度专题.md", 0.7),
                _result("c3", "其他笔记.md", 0.6),
            ],
            top_k=3,
        )
        concentrated = [line for line in advice if "命中集中" in line]
        self.assertTrue(concentrated, advice)
        self.assertIn("read_document", concentrated[0])
        self.assertIn('library_id="notes"', concentrated[0])
        self.assertNotIn('exclude="', concentrated[0])

    def test_backfill_list_and_document_rules(self):
        self.runtime.settings.set("advice_max_lines", 3)
        advice = self._advise(
            [
                _result("c1", "a.pdf", 0.8, backfilled=True),
                _result("c2", "b.docx", 0.7),
            ],
            mode="body",
            top_k=2,
        )
        self.assertTrue(any("PDF/Word" in line for line in advice))
        self.assertTrue(any("按小节回填" in line for line in advice))
        list_advice = self._advise(
            [_result("c3", "c.md", 0.8), _result("c4", "d.md", 0.7)],
            mode="list",
            top_k=2,
        )
        self.assertTrue(any("候选清单" in line for line in list_advice))

    def test_max_lines_setting_is_enforced(self):
        self.runtime.settings.set("advice_max_lines", 1)
        advice = self._advise(
            [_result("c1", "a.md", 0.1), _result("c2", "b.md", 0.2)],
            query="关键词",
        )
        self.assertEqual(len(advice), 1)
        self.runtime.settings.set("advice_max_lines", 0)
        self.assertEqual(
            self._advise([_result("c1", "a.md", 0.1)]),
            (),
        )


if __name__ == "__main__":
    unittest.main()
