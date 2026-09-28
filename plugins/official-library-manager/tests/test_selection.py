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
    bulk_for_extensions,
    collect_included_files,
    decide_included,
    dir_self_blocked,
    explicit_verdict,
    format_selection_bulk,
    normalize_selection_changes,
    norm_selection_path,
    selection_explicit,
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


# ---------------------------------------------------------------------------
# 格式批量勾选语义（obsidian-rag 问题44 用户拍板③）——镜像旧
# tests/test_selection.py:146-166 test_format_bulk / :172-186
# test_extensions_change_triggers_bulk。落盘收口在 config.set_policy
# （见 plugins/official-library-manager/tests/test_config.py 的对应用例），
# 这里直接测纯函数本身。
# ---------------------------------------------------------------------------
class TestFormatSelectionBulk(unittest.TestCase):
    def test_switching_off_moves_file_level_entries_to_out(self):
        sin, sout, n = format_selection_bulk(
            ["pdf"], False,
            ["课件/青苹果菜单.pdf", "课件/锅包肉配方.pdf", "课件", "笔记.md"],
            [],
        )
        self.assertEqual(n, 2)
        self.assertEqual(sin, ["课件", "笔记.md"], "文件夹级条目永不被批量触碰")
        self.assertEqual(sorted(sout), ["课件/锅包肉配方.pdf", "课件/青苹果菜单.pdf"])

    def test_switching_on_removes_matching_entries_from_out(self):
        sin, sout, n = format_selection_bulk(
            ["pdf"], True, ["课件"], ["课件/青苹果菜单.pdf", "课件/锅包肉配方.pdf"]
        )
        self.assertEqual(n, 2)
        self.assertEqual(sout, [])
        self.assertEqual(sin, ["课件"], "显式勾选不凭空复活")

    def test_file_level_is_decided_by_extension_not_by_slash(self):
        """旧项目在 TASK_LOG.md 问题44 里明确记过这个坑：子目录里的文件同样
        含斜杠，不能以"路径里有没有 /"区分文件/文件夹。判据只能是扩展名
        （因此一个恰好叫 '资料.pdf' 的**文件夹**也会被批量迁移——与旧项目
        逐字一致，旧注释：文件夹条目极少以 .ext 结尾，误伤面可忽略）。"""
        sin, sout, n = format_selection_bulk(["pdf"], False, ["资料.pdf", "a/b.pdf"], [])
        self.assertEqual(n, 2)
        self.assertEqual(sin, [])
        self.assertEqual(sorted(sout), ["a/b.pdf", "资料.pdf"])

    def test_extension_matching_is_case_insensitive_and_dot_optional(self):
        sin, _, n = format_selection_bulk(["PDF", ".docx"], False, ["A.PDF", "b.docx", "c.md"], [])
        self.assertEqual(n, 2)
        self.assertEqual(sin, ["c.md"])

    def test_no_match_returns_zero_and_copies_inputs(self):
        sin, sout, n = format_selection_bulk(["docx"], True, ["a.md"], ["b.md"])
        self.assertEqual((n, sin, sout), (0, ["a.md"], ["b.md"]))

    def test_empty_ext_list_is_a_no_op(self):
        self.assertEqual(format_selection_bulk([], False, ["a.pdf"], []), (["a.pdf"], [], 0))

    def test_bulk_for_extensions_drive_off_the_set_difference(self):
        sin, sout, n = bulk_for_extensions(
            ["md", "pdf", "docx"], ["md"],
            ["课件/a.pdf", "课件/b.docx", "笔记.md"], ["旧/c.pdf"],
        )
        self.assertEqual(n, 2, "pdf 与 docx 各被移出 in 一条（out 里原有的不动）")
        self.assertEqual(sin, ["笔记.md"])
        self.assertEqual(sorted(sout), ["旧/c.pdf", "课件/a.pdf", "课件/b.docx"])

        sin, sout, n = bulk_for_extensions(
            ["md"], ["md", "pdf"], sin, sout,
        )
        self.assertEqual(n, 2, "新增 pdf：out 里的 pdf 条目被摘掉")
        self.assertEqual(sin, ["笔记.md"])
        self.assertEqual(sout, ["课件/b.docx"])

    def test_bulk_for_extensions_ignores_case_and_dot_differences(self):
        sin, sout, n = bulk_for_extensions([".MD", "PDF"], ["md", ".pdf"], ["a.pdf"], [])
        self.assertEqual((n, sin, sout), (0, ["a.pdf"], []))


# ---------------------------------------------------------------------------
# 中性文件的目录排除只看**库内相对路径**——这是一处已批准的有意偏离，必须
# 钉住，防止以后有人"顺手对齐回旧项目"时无声退化。
# ---------------------------------------------------------------------------
class TestNeutralDirExcludeUsesRelativePath(unittest.TestCase):
    """**有意偏离记录（勿改）**

    obsidian-rag 的 `index.py::collect_md_files` 在判定中性文件的目录排除时
    用的是 `p.parts`（**绝对**路径，含库根以上的所有目录），而同一次调用里
    的 `decide_included` 用的是 rel（`library.py:153`）——旧项目自身两处不
    一致。后果：库根路径任一级目录名含 `TEMP` / `templates` / `.git` 时，
    旧项目会**静默排除该库的所有中性文件**（表现为"这个库明明有笔记却搜不
    到"，且没有任何提示）。

    REDO 统一成 rel 语义，与 `decide_included` 自洽，是更正确的那个；但对
    从旧项目迁移的用户它是**静默行为变化**：原本"看不见"的一批文件会突然
    进入索引。

    影响面与处置：这是一处已批准的有意偏离，需要登记进
    docs/behavior_contract.json（主代理处理）。下面的用例把差异明确钉住。
    """

    def test_library_root_above_named_exclude_dir_does_not_hide_neutral_files(self):
        import shutil
        import tempfile

        from official_library_manager.config import LibraryConfigStore
        from official_library_manager.plugin import LibraryManagerPlugin

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # 库根建在一个名字命中默认 exclude_dirs（"TEMP"）的目录下面
        container = tmp / "TEMP"
        container.mkdir()
        vault = container / "vault"
        (vault / "notes").mkdir(parents=True)
        (vault / "notes" / "a.md").write_text("x", encoding="utf-8")
        # 先确认"绝对路径"里确实含 TEMP 部件——否则下面这条断言会因为
        # tmp 路径本身不含 TEMP 而变成空断言（自证失败的用例比没用例更糟）
        self.assertIn(
            "TEMP", {part.upper() for part in vault.resolve().parts},
            "绝对路径必须真的含 TEMP 部件，这条用例才有对照意义",
        )
        store = LibraryConfigStore(tmp / "libraries.json")
        store.add_library("t", "库", str(vault))
        store.set_policy("t", exclude_dirs=[])  # 只留"TEMP 一条"这个对照前提
        plugin = LibraryManagerPlugin()
        plugin.store = store
        decisions = {
            path: included for path, included, _reason in plugin.resolve_included_files("t")
        }
        # REDO：rel = "notes/a.md"，不含 TEMP → 纳入（旧项目在这里会排除）
        self.assertTrue(decisions["notes/a.md"])


class TestExplicitVerdictIsSharedByTreeAndFunnel(unittest.TestCase):
    """`explicit_verdict` 是"谁具体听谁的"的唯一实现：建索引的文件漏斗（`decide_included`）
    和 GUI 勾选树的展示都吃它。这里固定它的口径，并断言漏斗与它在整张矩阵上永不打架
    ——勾选树显示"入库"而索引时没收录，就是"显示≠实际"的撕裂。"""

    def test_verdict_table_mirrors_the_legacy_decide_included(self):
        cases = [
            # (selection_in, selection_out, exclude_dirs, path, (verdict, tie))
            ([], [], [], "a/b.md", (None, False)),                        # 无显式无排除：中性
            ([], [], ["private"], "private/x.md", ("out", False)),        # 只有目录排除
            ([], ["a"], [], "a/b.md", ("out", False)),                    # 祖先显式排除
            (["a/b.md"], [], ["a"], "a/b.md", ("in", False)),             # 窄例外：文件自身勾选压过目录排除
            (["a"], [], ["a"], "a/b.md", ("out", True)),                  # 同位置打架：排除站住
            (["a"], [], ["a/b"], "a/b/c.md", ("in", False)),              # 排除条目含斜杠，按部件子串匹配不到
            (["a"], [], ["b"], "a/b/c.md", ("out", False)),               # 更深的目录排除压过较浅的纳入
        ]
        for sin, sout, ex_dirs, path, expected in cases:
            with self.subTest(path=path, sin=sin, sout=sout, ex_dirs=ex_dirs):
                verdict, tie, reason = explicit_verdict(sin, sout, ex_dirs, path)
                self.assertEqual((verdict, tie), expected)
                self.assertEqual(bool(reason), verdict is not None)

    def test_funnel_never_disagrees_with_the_verdict_on_a_grid(self):
        import itertools

        paths = ["notes.md", "a/x.md", "a/b/y.md", "private/z.md", "a/private/w.md"]
        selections = [[], ["a"], ["a/b"], ["a/x.md"], ["private"]]
        exclusions = [[], ["private"], ["a"], ["b"]]
        for path, sin, sout, ex_dirs in itertools.product(paths, selections, selections, exclusions):
            verdict, _tie, _reason = explicit_verdict(sin, sout, ex_dirs, path)
            included, _ = decide_included(
                path, selection_in=sin, selection_out=sout, new_file_default="follow",
                enabled_extensions=[".md"], exclude_dirs=ex_dirs,
            )
            if verdict is not None:
                self.assertEqual(included, verdict == "in", (path, sin, sout, ex_dirs))

    def test_selection_explicit_reports_the_nearest_explicit_ancestor(self):
        self.assertEqual(selection_explicit(["a"], [], "a/b/c.md"), "in")
        self.assertEqual(selection_explicit(["a"], ["a/b"], "a/b/c.md"), "out")
        self.assertIsNone(selection_explicit(["a"], ["b"], "x/y.md"))

    def test_dir_self_blocked_only_matches_the_exact_normalized_entry(self):
        self.assertTrue(dir_self_blocked(["private", ".git"], "private"))
        self.assertTrue(dir_self_blocked(["a\\b/"], "a/b"))
        self.assertFalse(dir_self_blocked(["private"], "docs/private"), "只认字符串相等")
        self.assertFalse(dir_self_blocked(["private"], "priv"))
        self.assertFalse(dir_self_blocked([], "anything"))


if __name__ == "__main__":
    unittest.main()
