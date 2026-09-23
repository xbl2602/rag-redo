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

from official_library_manager.selection import (  # noqa: E402
    apply_selection_changes,
    collect_included_files,
    decide_included,
    normalize_selection_changes,
    norm_selection_path,
)


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


class TestNormSelectionPath(unittest.TestCase):
    def test_plain_relative_path_passes_through(self):
        self.assertEqual(norm_selection_path("docs/a.md"), "docs/a.md")

    def test_backslashes_normalized_to_forward_slashes(self):
        self.assertEqual(norm_selection_path("docs\\a.md"), "docs/a.md")

    def test_trailing_slash_stripped(self):
        # 前导 "/" 本身就被当成"看起来像绝对路径"直接拒绝（见下面
        # test_absolute_unix_path_rejected）——只有尾部斜杠会被剥掉。
        self.assertEqual(norm_selection_path("docs/a.md/"), "docs/a.md")

    def test_absolute_unix_path_rejected(self):
        with self.assertRaises(ValueError):
            norm_selection_path("/etc/passwd")

    def test_windows_drive_letter_path_rejected(self):
        with self.assertRaises(ValueError):
            norm_selection_path("C:/Windows/System32")

    def test_home_dir_shortcut_rejected(self):
        with self.assertRaises(ValueError):
            norm_selection_path("~/secrets.md")

    def test_parent_dir_escape_rejected(self):
        with self.assertRaises(ValueError):
            norm_selection_path("../outside.md")

    def test_empty_path_rejected(self):
        with self.assertRaises(ValueError):
            norm_selection_path("   ")

    def test_non_string_rejected(self):
        with self.assertRaises(ValueError):
            norm_selection_path(123)  # type: ignore[arg-type]


class TestNormalizeSelectionChanges(unittest.TestCase):
    def test_valid_changes_normalized(self):
        result = normalize_selection_changes([{"path": "a.md", "action": "in"}])
        self.assertEqual(result, [{"path": "a.md", "action": "in"}])

    def test_empty_changes_rejected(self):
        with self.assertRaises(ValueError):
            normalize_selection_changes([])

    def test_invalid_action_rejected(self):
        with self.assertRaises(ValueError):
            normalize_selection_changes([{"path": "a.md", "action": "delete"}])

    def test_invalid_path_in_one_of_several_changes_rejects_whole_batch(self):
        """整体通过或整体拒绝——不做部分生效，同 obsidian-rag
        normalize_changes 的"提案要么全部合法要么整体拒绝"策略一致。"""
        with self.assertRaises(ValueError):
            normalize_selection_changes([{"path": "a.md", "action": "in"}, {"path": "/etc/passwd", "action": "out"}])


class TestApplySelectionChanges(unittest.TestCase):
    def test_in_action_adds_to_selection_in(self):
        sin, sout = apply_selection_changes([], [], [{"path": "a.md", "action": "in"}])
        self.assertEqual(sin, ["a.md"])
        self.assertEqual(sout, [])

    def test_out_action_adds_to_selection_out(self):
        sin, sout = apply_selection_changes([], [], [{"path": "a.md", "action": "out"}])
        self.assertEqual(sin, [])
        self.assertEqual(sout, ["a.md"])

    def test_neutral_action_removes_from_both_lists(self):
        sin, sout = apply_selection_changes(["a.md"], ["a.md"], [{"path": "a.md", "action": "neutral"}])
        self.assertEqual(sin, [])
        self.assertEqual(sout, [])

    def test_moving_from_out_to_in_removes_stale_out_entry(self):
        sin, sout = apply_selection_changes([], ["a.md"], [{"path": "a.md", "action": "in"}])
        self.assertEqual(sin, ["a.md"])
        self.assertEqual(sout, [])

    def test_later_change_to_same_path_overrides_earlier_one(self):
        sin, sout = apply_selection_changes(
            [], [], [{"path": "a.md", "action": "in"}, {"path": "a.md", "action": "out"}]
        )
        self.assertEqual(sin, [])
        self.assertEqual(sout, ["a.md"])

    def test_unrelated_existing_entries_are_preserved(self):
        sin, sout = apply_selection_changes(["existing.md"], [], [{"path": "new.md", "action": "in"}])
        self.assertEqual(sin, ["existing.md", "new.md"])


if __name__ == "__main__":
    unittest.main()
