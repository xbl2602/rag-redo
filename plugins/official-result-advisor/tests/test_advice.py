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
        capped: bool = False,
        folded: int = 0,
        empty_reason: str | None = None,
    ):
        request = SearchAdviceInput(
            results=tuple(results),
            query=query,
            mode=mode,
            top_k=top_k,
            default_libraries=tuple(default_libraries),
            warn_threshold=0.30,
            strong_threshold=0.75,
            capped=capped,
            folded=folded,
            empty_reason=empty_reason,
        )
        return self.instance.advise(request)

    def test_empty_results_explain_why(self):
        """空结果必须说话——对齐 obsidian-rag/retriever.py:479-483 的两分支。

        之前这里断言"空结果不给任何建议"，那正是缺陷本体：把
        confidence_drop_threshold 调高后全部命中被过滤，调用方拿到的响应与
        "库里压根没有相关内容"完全一样，零信息。
        """
        no_score = self._advise([], empty_reason="no-score")
        self.assertEqual(len(no_score), 1)
        self.assertIn("未找到相关内容", no_score[0])

        filtered = self._advise([], empty_reason="all-below-drop-threshold")
        self.assertEqual(len(filtered), 1)
        self.assertIn("低于置信度下限", filtered[0])
        # 两套文案必须真的不同，否则分叉没有意义
        self.assertNotEqual(no_score, filtered)

    def test_duplicate_stem_is_reported_even_when_sections_differ(self):
        """同名不同目录的判据是**文件名 stem**，不是小节标题面包屑。

        对齐旧 advice.py:113-117（`title` 实际几乎总为空，那条规则的语义就是
        stem）。用小节标题当判据会同时漏报（两篇同名笔记命中不同小节——正是
        这条规则要解决的场景）和误报（两篇不同名笔记都命中「## 结论」，可
        文案却打印文件名「A」/「A.md」「B.md」）。
        """
        advice = self._advise(
            [
                _result("c1", "a/主题.md", 0.8, heading="安装"),
                _result("c2", "b/主题.md", 0.7, heading="配置"),
            ]
        )
        self.assertTrue(any("同名不同目录" in line for line in advice), advice)

        # 反向：不同名笔记命中同一个小节，不该报"同名"
        unrelated = self._advise(
            [
                _result("c1", "a/甲.md", 0.8, heading="结论"),
                _result("c2", "b/乙.md", 0.7, heading="结论"),
            ]
        )
        self.assertFalse(any("同名不同目录" in line for line in unrelated), unrelated)

    def test_capped_and_folded_signals_reach_the_rules(self):
        """`capped`/`folded` 两个信号以前在 `_search_once` 里算出来却无处可去，
        建议层和渲染层整体看不到它们（缺陷：封顶/折叠对用户不可见）。"""
        capped = self._advise(
            [_result("c1", "a.md", 0.9, library="notes")],
            capped=True,
        )
        self.assertTrue(any("最多展示" in line for line in capped), capped)

        folded = self._advise(
            [_result("c1", "a.md", 0.9, library="notes")],
            folded=2,
        )
        self.assertTrue(any("按小节回填" in line for line in folded), folded)

    def test_advice_max_lines_zero_still_silences_empty_advice(self):
        """关掉建议输出时，空结果也不该硬塞一行出来。"""
        self.runtime.settings.set("advice_max_lines", 0)
        self.assertEqual(self._advise([], empty_reason="no-score"), ())

    def test_low_confidence_and_keyword_rules(self):
        advice = self._advise(
            [_result("c1", "a.md", 0.1), _result("c2", "b.md", 0.2)],
            query="Fluent",
        )
        self.assertEqual(len(advice), 2)
        self.assertIn("相关度都偏低", advice[0])
        self.assertIn("关键词式查询", advice[1])

    def test_duplicate_name_advice_uses_complete_paths(self):
        """提示里必须给**完整路径**而不是只给文件名——只给文件名的话，调用方
        拿到这句提示仍然分不清该打开哪一个，这正是这条规则要解决的问题。"""
        advice = self._advise(
            [
                _result("c1", "a/主题.md", 0.8, heading="Fluent 配置"),
                _result("c2", "b/主题.md", 0.7, heading="fluent配置"),
            ]
        )
        line = next(line for line in advice if "同名不同目录" in line)
        self.assertIn("a/主题.md", line)
        self.assertIn("b/主题.md", line)


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
