"""见 ../../../AGENTS.md 测试纪律。覆盖 docs/LESSONS.md 第2条"一件事的判定
逻辑只能有一处实现"背后的真实场景：路径级选择的优先级规则。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_library_manager.selection import collect_included_files, decide_included  # noqa: E402


class TestDecideIncluded(unittest.TestCase):
    def test_default_include_when_no_rules(self):
        included, reason = decide_included(
            "notes/a.md", selection_in=[], selection_out=[], new_file_default="include"
        )
        self.assertTrue(included)

    def test_default_exclude_when_no_rules(self):
        included, _ = decide_included(
            "notes/a.md", selection_in=[], selection_out=[], new_file_default="exclude"
        )
        self.assertFalse(included)

    def test_explicit_dir_exclude(self):
        included, reason = decide_included(
            "private/secret.md",
            selection_in=[],
            selection_out=["private"],
            new_file_default="include",
        )
        self.assertFalse(included)
        self.assertIn("private", reason)

    def test_file_pick_pierces_ancestor_dir_exclude(self):
        """文件点名可以穿透继承的目录级排除——这是旧项目问题47附记2明确
        拍板过的规则，本次重写必须保留同样的行为。"""
        included, reason = decide_included(
            "private/keep-this.md",
            selection_in=["private/keep-this.md"],
            selection_out=["private"],
            new_file_default="include",
        )
        self.assertTrue(included)
        self.assertIn("keep-this.md", reason)

    def test_more_specific_exclude_beats_less_specific_include(self):
        included, reason = decide_included(
            "docs/drafts/wip.md",
            selection_in=["docs"],
            selection_out=["docs/drafts"],
            new_file_default="include",
        )
        self.assertFalse(included)
        self.assertIn("docs/drafts", reason)

    def test_deeper_ancestor_wins_over_shallower(self):
        included, reason = decide_included(
            "a/b/c/file.md",
            selection_in=["a/b/c"],
            selection_out=["a"],
            new_file_default="exclude",
        )
        self.assertTrue(included)
        self.assertIn("a/b/c", reason)

    def test_same_path_in_both_lists_excludes(self):
        """同一路径同时出现在纳入和排除名单里（"同位置打架"）——排除赢，
        这是唯一的平局打破规则。"""
        included, reason = decide_included(
            "weird.md",
            selection_in=["weird.md"],
            selection_out=["weird.md"],
            new_file_default="include",
        )
        self.assertFalse(included)
        self.assertIn("排除", reason)

    def test_extension_filter_blocks_default_include(self):
        included, reason = decide_included(
            "notes/a.pdf",
            selection_in=[],
            selection_out=[],
            new_file_default="include",
            enabled_extensions=[".md"],
        )
        self.assertFalse(included)
        self.assertIn("格式", reason)

    def test_explicit_pick_bypasses_extension_filter(self):
        """格式规则是最弱的一层，显式路径命中可以静默穿透它——即使 .pdf
        不在启用格式列表里，只要用户显式点名要这个文件，就该收。"""
        included, _ = decide_included(
            "notes/special.pdf",
            selection_in=["notes/special.pdf"],
            selection_out=[],
            new_file_default="include",
            enabled_extensions=[".md"],
        )
        self.assertTrue(included)

    def test_root_level_file_no_ancestor_match_still_default(self):
        included, _ = decide_included(
            "readme.md", selection_in=[], selection_out=["other"], new_file_default="include"
        )
        self.assertTrue(included)


class TestCollectIncludedFiles(unittest.TestCase):
    def test_returns_full_decision_for_every_path(self):
        results = collect_included_files(
            ["a.md", "b.md", "excluded/c.md"],
            selection_in=[],
            selection_out=["excluded"],
            new_file_default="include",
        )
        self.assertEqual(len(results), 3)
        decisions = {path: included for path, included, _ in results}
        self.assertTrue(decisions["a.md"])
        self.assertTrue(decisions["b.md"])
        self.assertFalse(decisions["excluded/c.md"])


if __name__ == "__main__":
    unittest.main()
