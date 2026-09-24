"""core/note_relations.py 的单元测试：真实写盘、真实解析 wikilink 语法。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.note_relations import NoteRelationsStore, extract_wikilink_targets  # noqa: E402


class TestExtractWikilinkTargets(unittest.TestCase):
    def test_plain_link_extracts_target(self):
        self.assertEqual(extract_wikilink_targets("看 [[笔记A]] 了解详情"), ["笔记A"])

    def test_alias_link_extracts_target_not_alias(self):
        self.assertEqual(extract_wikilink_targets("[[笔记A|显示别名]]"), ["笔记A"])

    def test_folder_prefixed_link_strips_path(self):
        self.assertEqual(extract_wikilink_targets("[[20-Projects/笔记A]]"), ["笔记A"])

    def test_heading_anchor_link_strips_anchor(self):
        self.assertEqual(extract_wikilink_targets("[[笔记A#某标题]]"), ["笔记A"])

    def test_block_anchor_only_link_is_ignored(self):
        # 无目标头的锚点链接（本文件内部块引用）不计入笔记间关系
        self.assertEqual(extract_wikilink_targets("[[#^块id]]"), [])

    def test_embed_link_is_ignored(self):
        self.assertEqual(extract_wikilink_targets("![[图片.png]]"), [])

    def test_multiple_links_deduplicated_and_sorted(self):
        text = "[[笔记B]] 然后 [[笔记A]] 又提到 [[笔记A]]"
        self.assertEqual(extract_wikilink_targets(text), ["笔记A", "笔记B"])

    def test_no_links_returns_empty_list(self):
        self.assertEqual(extract_wikilink_targets("没有任何链接的普通正文"), [])

    def test_escaped_pipe_in_table_cell_is_unescaped(self):
        self.assertEqual(extract_wikilink_targets(r"[[笔记A\|表格转义]]"), ["笔记A"])


class TestNoteRelationsStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = NoteRelationsStore(self.tmp / "note_relations")

    def test_resolve_before_any_write_is_unresolved(self):
        result = self.store.resolve("lib1", "笔记A.md")
        self.assertFalse(result["resolved"])
        self.assertIsNone(result["file"])
        self.assertEqual(result["outlinks"], [])
        self.assertEqual(result["inlinks"], [])

    def test_outlinks_and_inlinks_resolved_from_mutual_links(self):
        self.store.write_library(
            "lib1",
            {
                "笔记A.md": ["笔记B"],
                "笔记B.md": ["笔记A"],
                "笔记C.md": [],
            },
        )
        a = self.store.resolve("lib1", "笔记A.md")
        self.assertTrue(a["resolved"])
        self.assertEqual(a["file"], "笔记A.md")
        self.assertEqual(a["outlinks"], ["笔记B.md"])
        self.assertEqual(a["inlinks"], ["笔记B.md"])

        c = self.store.resolve("lib1", "笔记C.md")
        self.assertTrue(c["resolved"])
        self.assertEqual(c["outlinks"], [])
        self.assertEqual(c["inlinks"], [])

    def test_resolve_by_title_without_extension(self):
        self.store.write_library("lib1", {"20-Projects/笔记A.md": ["笔记B"], "笔记B.md": []})
        result = self.store.resolve("lib1", "笔记A")  # 不含扩展名的标题
        self.assertTrue(result["resolved"])
        self.assertEqual(result["file"], "20-Projects/笔记A.md")

    def test_resolve_unknown_target_is_unresolved(self):
        self.store.write_library("lib1", {"笔记A.md": []})
        result = self.store.resolve("lib1", "不存在的笔记")
        self.assertFalse(result["resolved"])

    def test_self_link_is_not_counted_as_outlink(self):
        self.store.write_library("lib1", {"笔记A.md": ["笔记A"]})
        result = self.store.resolve("lib1", "笔记A.md")
        self.assertEqual(result["outlinks"], [])

    def test_different_libraries_are_isolated(self):
        self.store.write_library("lib1", {"笔记A.md": ["笔记B"], "笔记B.md": []})
        self.store.write_library("lib2", {"笔记A.md": []})
        lib1_result = self.store.resolve("lib1", "笔记A.md")
        lib2_result = self.store.resolve("lib2", "笔记A.md")
        self.assertEqual(lib1_result["outlinks"], ["笔记B.md"])
        self.assertEqual(lib2_result["outlinks"], [])

    def test_dangling_link_to_nonexistent_note_is_not_listed_as_outlink(self):
        """链接目标在库里根本不存在（笔记还没建/已删）——同 obsidian-rag
        resolve_note_relations 一致，outlinks 只列"确实能解析到库内某个
        文件"的链接，悬空链接不出现在结果里（不是报错，也不是原样带出
        无法解析的目标字符串）。"""
        self.store.write_library("lib1", {"笔记A.md": ["从未创建的笔记"]})
        result = self.store.resolve("lib1", "笔记A.md")
        self.assertEqual(result["outlinks"], [])

    def test_resolved_edges_are_undirected_deduplicated_and_sorted(self):
        self.store.write_library(
            "lib1",
            {
                "a.md": ["b", "子/c.md", "a"],
                "b.md": ["a"],
                "子/c.md": ["a.md"],
                "dangling.md": ["missing"],
            },
        )
        self.assertEqual(
            self.store.resolved_edges("lib1"),
            (("a.md", "b.md"), ("a.md", "子/c.md")),
        )

    def test_resolved_edges_are_generation_scoped(self):
        self.store.write_library("lib1", {"a.md": ["b"], "b.md": []}, "old")
        self.store.write_library("lib1", {"a.md": ["c"], "c.md": []}, "new")
        self.assertEqual(self.store.resolved_edges("lib1", "old"), (("a.md", "b.md"),))
        self.assertEqual(self.store.resolved_edges("lib1", "new"), (("a.md", "c.md"),))

    def test_rewriting_library_replaces_stale_links(self):
        self.store.write_library("lib1", {"笔记A.md": ["笔记B"]})
        self.store.write_library("lib1", {"笔记A.md": []})  # 全量重跑覆盖旧数据
        result = self.store.resolve("lib1", "笔记A.md")
        self.assertEqual(result["outlinks"], [])


if __name__ == "__main__":
    unittest.main()
