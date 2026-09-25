"""见 ../../../AGENTS.md 测试纪律。覆盖路径级勾选裁决：逐字对齐旧
obsidian-rag library.py::decide_included（问题44/47 用户拍板）+
index.py::collect_md_files 中性分支的语义，用例镜像旧项目
tests/test_selection.py 的同名场景（窄例外/深度裁决/同位置打架/前缀排除）。
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


class TestNeutralDefaults(unittest.TestCase):
    def test_default_include_supported_format(self):
        # include = 受支持格式（md/txt/pdf/docx）一律纳入，可穿透 extensions 白名单
        included, _ = decide_included(
            "notes/a.md", selection_in=[], selection_out=[], new_file_default="include"
        )
        self.assertTrue(included)

    def test_default_include_pierces_extension_whitelist(self):
        # 旧语义：include 分支只看 SUPPORTED_EXTS，不看库 extensions 列表
        included, reason = decide_included(
            "notes/a.pdf", selection_in=[], selection_out=[], new_file_default="include",
            enabled_extensions=[".md"],
        )
        self.assertTrue(included)
        self.assertIn("include", reason)

    def test_default_include_unsupported_format_rejected(self):
        included, _ = decide_included(
            "notes/data.xyz", selection_in=[], selection_out=[], new_file_default="include"
        )
        self.assertFalse(included)

    def test_default_follow_uses_library_extensions(self):
        # follow（旧默认）= 按该库格式开关判定
        self.assertTrue(decide_included(
            "notes/a.md", selection_in=[], selection_out=[], new_file_default="follow",
            enabled_extensions=[".md"],
        )[0])
        self.assertFalse(decide_included(
            "notes/a.pdf", selection_in=[], selection_out=[], new_file_default="follow",
            enabled_extensions=[".md"],
        )[0])

    def test_default_exclude_when_no_rules(self):
        included, _ = decide_included(
            "notes/a.md", selection_in=[], selection_out=[], new_file_default="exclude"
        )
        self.assertFalse(included)


class TestExplicitRules(unittest.TestCase):
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
        """文件点名穿透继承的目录排除（旧问题47：窄例外静默生效）——镜像旧
        用例 decide_included(["课件/青苹果菜单.pdf"], [], ["课件"], ...) == in。"""
        included, reason = decide_included(
            "课件/青苹果菜单.pdf",
            selection_in=["课件/青苹果菜单.pdf"],
            selection_out=[],
            new_file_default="follow",
            exclude_dirs=["课件"],
            enabled_extensions=[".md", ".pdf"],
        )
        self.assertTrue(included)

    def test_deeper_dir_exclude_beats_shallower_include(self):
        """更深的单段目录排除压过较浅的目录级纳入（问题47 用户拍板）：
        勾了 docs 但 drafts 子目录被排除 → drafts 里的文件不收。"""
        included, reason = decide_included(
            "docs/drafts/wip.md",
            selection_in=["docs"],
            selection_out=[],
            new_file_default="include",
            exclude_dirs=["drafts"],
        )
        self.assertFalse(included)
        self.assertIn("排除", reason)

    def test_deeper_ancestor_wins_over_shallower(self):
        included, reason = decide_included(
            "a/b/c/file.md",
            selection_in=["a/b/c"],
            selection_out=["a"],
            new_file_default="exclude",
        )
        self.assertTrue(included)
        self.assertIn("a/b/c", reason)

    def test_multi_segment_exclude_entry_is_inert(self):
        """镜像旧用例：目录排除是"单部件子串"语义，含斜杠的多段条目在深度
        裁决里天然不命中（旧代码如此，两侧一致），显式纳入照常生效。"""
        included, _ = decide_included(
            "课件/子/f.md",
            selection_in=["课件"],
            selection_out=[],
            new_file_default="follow",
            exclude_dirs=["课件/子"],
        )
        self.assertTrue(included)

    def test_same_place_conflict_exclusion_stands(self):
        """镜像旧用例：纳入目标本身在目录排除名单（字符串相等）——排除站住。"""
        included, reason = decide_included(
            "课件/a.md",
            selection_in=["课件"],
            selection_out=[],
            new_file_default="follow",
            exclude_dirs=["课件"],
        )
        self.assertFalse(included)
        self.assertIn("同位置", reason)

    def test_same_path_in_both_lists_excludes(self):
        """同一路径同时出现在纳入和排除名单里——读侧 out 优先（宁可少索引；
        写路径已拒绝新建此类状态）。"""
        included, reason = decide_included(
            "weird.md",
            selection_in=["weird.md"],
            selection_out=["weird.md"],
            new_file_default="include",
        )
        self.assertFalse(included)
        self.assertIn("排除", reason)

    def test_explicit_pick_bypasses_extension_filter(self):
        """格式规则是最弱的一层，显式路径命中静默穿透它。"""
        included, _ = decide_included(
            "notes/special.pdf",
            selection_in=["notes/special.pdf"],
            selection_out=[],
            new_file_default="follow",
            enabled_extensions=[".md"],
        )
        self.assertTrue(included)

    def test_explicit_pick_pierces_weak_exclude_rules(self):
        self.assertTrue(
            decide_included(
                "private/keep.md",
                selection_in=["private/keep.md"],
                selection_out=[],
                new_file_default="exclude",
                exclude_dirs=["private"],
                exclude_patterns=["keep"],
            )[0]
        )


class TestWeakRulesOnNeutralFiles(unittest.TestCase):
    def test_dir_substring_semantics(self):
        """目录排除是子串语义（旧 collect 漏斗逐字行为）：条目 'TEMP' 命中
        文件名部件含 TEMP 的位置（'MyTEMP notes.md'）。"""
        self.assertFalse(decide_included(
            "subdir/MyTEMP notes.md",
            selection_in=[], selection_out=[], new_file_default="include",
            exclude_dirs=["TEMP"],
        )[0])

    def test_prefix_pattern_exclusion(self):
        """exclude_patterns 旧语义是文件名前缀匹配（startswith），不是通配。"""
        self.assertFalse(decide_included(
            "logs/session-2024.md",
            selection_in=[], selection_out=[], new_file_default="include",
            exclude_patterns=["session-"],
        )[0])
        # 通配式模式在旧语义下不生效（startswith 按字面比较）
        self.assertTrue(decide_included(
            "drafts/note.md",
            selection_in=[], selection_out=[], new_file_default="include",
            exclude_patterns=["*.md"],
        )[0])

    def test_exclude_files_exact_name(self):
        self.assertFalse(decide_included(
            "deep/nest/secret.txt",
            selection_in=[], selection_out=[], new_file_default="include",
            exclude_files=["secret.txt"],
        )[0])

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

    def test_funnel_mirrors_old_collect_fixture(self):
        """镜像旧 test_narrow_exception_and_funnel_safety 的漏斗用例：目录
        排除 + 文件点名窄例外 + follow 中性文件按格式跟随。"""
        results = collect_included_files(
            ["私人/账单.pdf", "笔记.md", "课件/青苹果菜单.pdf"],
            selection_in=["课件/青苹果菜单.pdf"],
            selection_out=[],
            new_file_default="follow",
            enabled_extensions=[".md", ".pdf"],
            exclude_dirs=["课件"],
        )
        decisions = {path: included for path, included, _ in results}
        self.assertTrue(decisions["私人/账单.pdf"])
        self.assertTrue(decisions["笔记.md"])
        self.assertTrue(decisions["课件/青苹果菜单.pdf"], "点名穿透目录排除")


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

    def test_same_place_conflict_refused(self):
        """镜像旧 test_gate_refuses_same_place：MCP 提案侧事前拦截（问题47
        用户拍板）——确认码不花在注定无效的提案上；错误信息指引先清排除。"""
        with self.assertRaises(ValueError) as ctx:
            normalize_selection_changes(
                [{"path": "私人", "action": "in"}], exclude_dirs=["私人"]
            )
        self.assertIn("exclude_dirs", str(ctx.exception))
        # 非打架照常：同位置 out 方向 + 点名其下具体文件（个别例外静默生效）
        result = normalize_selection_changes(
            [{"path": "私人", "action": "out"}, {"path": "私人/账单.pdf", "action": "in"}],
            exclude_dirs=["私人"],
        )
        self.assertEqual(len(result), 2)


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
