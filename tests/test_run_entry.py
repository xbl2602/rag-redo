"""统一测试入口自己的测试：几套同时跑（tests/run.py）与按改动挑套件（tests/related_suites.py）。

2026-10-01 操作者确认：全量回归默认几套同时跑、用例一条不少；开发中途可以只跑相关的几套，
提交前仍跑全量（BC-13）。这里钉住两件最容易悄悄坏掉的事——“同时跑”真的是同时、“挑套件”
不会漏掉经由插件 id 或字符串路径接上的依赖。"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

import related_suites  # noqa: E402
import run  # noqa: E402

GATE = related_suites.GATE_SUITE


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


class TestSuitesRunSideBySide(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _suite(self, name: str, body: str) -> run.Suite:
        path = _write(self.tmp, f"test_{name}.py", "import unittest\n" + textwrap.dedent(body))
        return run.Suite(name, path)

    def _waiting_suite(self, name: str, other: str) -> run.Suite:
        """先留个记号，再等另一套的记号——只有两套真的同时在跑才都能过。"""
        return self._suite(name, f"""
            import time
            from pathlib import Path
            class T(unittest.TestCase):
                def test_meet(self):
                    Path({str(self.tmp / name)!r}).write_text("x")
                    deadline = time.time() + 30
                    while not Path({str(self.tmp / other)!r}).exists() and time.time() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(Path({str(self.tmp / other)!r}).exists())
        """)

    def test_two_suites_really_run_at_the_same_time(self):
        suites = [self._waiting_suite("a", "b"), self._waiting_suite("b", "a")]
        results = run.run_suites(suites, jobs=2, report=lambda line: None)
        self.assertEqual([r.ok for r in results], [True, True], [r.output for r in results])

    def test_a_failing_suite_is_reported_with_its_whole_output(self):
        good = self._suite("good", """
            class T(unittest.TestCase):
                def test_ok(self):
                    pass
        """)
        bad = self._suite("bad", """
            class T(unittest.TestCase):
                def test_broken(self):
                    self.assertEqual(1, 2, "独一无二的失败说明")
        """)
        lines: list[str] = []
        results = {r.suite.label: r for r in run.run_suites([good, bad], jobs=2, report=lines.append)}
        self.assertTrue(results["good"].ok)
        self.assertEqual(results["good"].tests_run, 1)
        self.assertFalse(results["bad"].ok)
        report = "\n".join(lines)
        self.assertIn("PASS good (1 用例, ", report)
        self.assertIn("FAIL bad (1 用例, ", report)
        self.assertIn("独一无二的失败说明", report)
        self.assertNotIn("test_ok", report)  # 过了的套件只报一行，不灌满屏幕

    def test_slowest_suites_start_first(self):
        log = self.tmp / "order.txt"
        body = """
            class T(unittest.TestCase):
                def test_log(self):
                    with open({log!r}, "a", encoding="utf-8") as f:
                        f.write({name!r} + "\\n")
        """
        suites = [self._suite(n, body.format(log=str(log), name=n)) for n in ("quick", "slow", "medium")]
        run.run_suites(suites, jobs=1, estimates={"quick": 1.0, "slow": 90.0, "medium": 20.0}, report=lambda line: None)
        self.assertEqual(log.read_text(encoding="utf-8").split(), ["slow", "medium", "quick"])

    def test_a_slow_suite_is_split_but_every_test_runs_exactly_once(self):
        ran = self.tmp / "ran"  # 每条用例留一个记号文件（几份同时往一个文件里追加，Windows 上会互相覆盖）
        ran.mkdir()
        source: list[str] = []
        for c in "ab":  # 两个类各 3 条，拆成 4 份后同一个类会散到几份里，各份都得先跑 setUpClass
            source += [
                f"class T{c}(unittest.TestCase):",
                "    @classmethod",
                "    def setUpClass(cls):",
                "        cls.ready = True",
            ]
            for i in range(3):
                source += [
                    f"    def test_{c}{i}(self):",
                    "        assert self.ready",
                    f"        open({str(ran / f'{c}{i}')!r}, 'x').close()",
                ]
        suite = self._suite("big", "\n".join(source) + "\n")
        lines: list[str] = []
        results = run.run_suites([suite], jobs=4, estimates={"big": 1000.0}, report=lines.append)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok, results[0].output)
        self.assertEqual(results[0].tests_run, 6)
        self.assertEqual(sorted(p.name for p in ran.iterdir()), ["a0", "a1", "a2", "b0", "b1", "b2"])
        self.assertIn("分 4 份同时跑", "\n".join(lines))

    def test_running_one_at_a_time_never_splits(self):
        self.assertEqual(run._shard_count(1000.0, jobs=1), 1)
        self.assertEqual(run._shard_count(10.0, jobs=6), 1)
        self.assertEqual(run._shard_count(1000.0, jobs=6), run.MAX_SHARDS)

    def test_a_leftover_child_still_holding_the_output_does_not_hang_the_run(self):
        """测试拉起的服务万一残留、还攥着继承来的输出句柄：用管道收输出会一直等到它退出
        （这里是 60 秒）；收进文件只等套件本身。"""
        pid_file = self.tmp / "child.pid"
        suite = self._suite("leaky", f"""
            import subprocess, sys
            from pathlib import Path
            class T(unittest.TestCase):
                def test_leave_a_child_behind(self):
                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=1, stderr=2)
                    Path({str(pid_file)!r}).write_text(str(child.pid))
        """)
        self.addCleanup(self._kill_leftover, pid_file)
        start = time.time()
        results = run.run_suites([suite], jobs=1, report=lambda line: None)
        self.assertTrue(results[0].ok, results[0].output)
        self.assertLess(time.time() - start, 30)

    @staticmethod
    def _kill_leftover(pid_file: Path) -> None:
        if not pid_file.exists():
            return
        pid = pid_file.read_text().strip()
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True, check=False)
        else:
            import psutil

            try:
                psutil.Process(int(pid)).kill()
            except psutil.Error:
                pass


class TestRunnerBookkeeping(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_durations_round_trip_and_merge(self):
        path = self.tmp / "durations.json"
        suite = run.Suite("core/test_x", self.tmp / "test_x.py")
        run.save_durations(path, [run.SuiteResult(suite, True, 3, 12.345, "")])
        other = run.Suite("core/test_y", self.tmp / "test_y.py")
        run.save_durations(path, [run.SuiteResult(other, False, 1, 1.0, "")])
        self.assertEqual(run.load_durations(path), {"core/test_x": 12.35, "core/test_y": 1.0})

    def test_unreadable_durations_just_mean_no_history(self):
        path = self.tmp / "durations.json"
        path.write_text("{坏掉的", encoding="utf-8")
        self.assertEqual(run.load_durations(path), {})
        self.assertEqual(run.load_durations(self.tmp / "missing.json"), {})

    def test_core_test_files_left_out_of_the_list_are_pointed_out(self):
        trimmed = [name for name in run.CORE_SUITES if name != "test_atomic"]
        with patch.object(run, "CORE_SUITES", trimmed):
            self.assertIn("test_atomic", run.unregistered_core_suites())
        self.assertNotIn("test_atomic", run.unregistered_core_suites())


class TestPickingRelatedSuites(unittest.TestCase):
    """一个最小的假仓库：核心模块链、带相对导入的插件、按 id 加载插件的端到端测试、
    字符串里的 patch 目标、测试辅助模块、示例目录、文档。"""

    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        files = {
            "core/__init__.py": "",
            "core/a.py": "X = 1\n",
            "core/b.py": "from core.a import X\n",
            "core/c.py": "Y = 2\n",
            "core/lonely.py": "Z = 3\n",
            "plugins/official-x/plugin.toml": "id = 'official-x'\n",
            "plugins/official-x/official_x/__init__.py": "",
            "plugins/official-x/official_x/plugin.py": "from core.b import X\nfrom .extract import f\n",
            "plugins/official-x/official_x/extract.py": "def f():\n    pass\n",
            "plugins/official-x/official_x/assets/app.js": "// js\n",
            "plugins/official-x/tests/test_x.py": "import official_x.plugin\n",
            "plugins/official-x/tests/fixtures/data.json": "{}\n",
            "plugins/official-y/official_y/__init__.py": "",
            "plugins/official-y/tests/test_y.py": "import unittest\n",
            "tests/test_agents_contract.py": "import unittest\n",
            "tests/test_b.py": "from core.b import X\n",
            "tests/test_e2e.py": "PLUGINS = ['official-x']\n",
            "tests/test_patch.py": "TARGET = 'core.c.Y'\n",
            "tests/helper.py": "from core.c import Y\n",
            "tests/test_h.py": "import helper\n",
            "tests/test_unrelated.py": "import unittest\n",
            "tests/test_gone.py": "import core.gone\n",
            "tests/test_examples.py": "ROOT = 'examples'\n",
            "tests/test_gui.py": "PATH = 'gui_main.py'\n",
            "tests/test_doc.py": '"""这里只是顺嘴提到 official-x 和 core.a，不是依赖。"""\nimport unittest\n',
            "gui_main.py": "IDS = ['official-y']\n",
            "examples/demo/thing.txt": "x\n",
            "docs/x.md": "# doc\n",
            "README.md": "# readme\n",
            "AGENTS.md": "# rules\n",
        }
        for rel, text in files.items():
            _write(self.repo, rel, text)
        self.suites = sorted(
            p.relative_to(self.repo).as_posix()
            for p in [*self.repo.glob("tests/test_*.py"), *self.repo.glob("plugins/*/tests/test_*.py")]
        )

    def _pick(self, *changed: str) -> set[str]:
        return set(related_suites.select_suites(self.repo, self.suites, list(changed)).suites)

    def test_a_core_module_change_reaches_everything_that_depends_on_it(self):
        self.assertEqual(
            self._pick("core/a.py"),
            {GATE, "tests/test_b.py", "plugins/official-x/tests/test_x.py", "tests/test_e2e.py"},
        )

    def test_a_relative_import_inside_a_plugin_is_followed(self):
        self.assertEqual(
            self._pick("plugins/official-x/official_x/extract.py"),
            {GATE, "plugins/official-x/tests/test_x.py", "tests/test_e2e.py"},
        )

    def test_any_file_of_a_plugin_reaches_suites_that_load_it_by_id(self):
        for rel in ("plugins/official-x/plugin.toml", "plugins/official-x/official_x/assets/app.js"):
            self.assertEqual(self._pick(rel), {GATE, "plugins/official-x/tests/test_x.py", "tests/test_e2e.py"}, rel)

    def test_a_plugin_id_named_in_an_entry_script_is_followed(self):
        self.assertEqual(
            self._pick("plugins/official-y/official_y/__init__.py"),
            {GATE, "plugins/official-y/tests/test_y.py", "tests/test_gui.py"},
        )

    def test_patch_targets_and_helper_modules_count_as_dependencies(self):
        self.assertEqual(self._pick("core/c.py"), {GATE, "tests/test_patch.py", "tests/test_h.py"})

    def test_a_deleted_module_still_finds_who_imports_it(self):
        self.assertEqual(self._pick("core/gone.py"), {GATE, "tests/test_gone.py"})

    def test_fixtures_and_other_folders(self):
        self.assertEqual(
            self._pick("plugins/official-x/tests/fixtures/data.json"),
            {GATE, "plugins/official-x/tests/test_x.py"},
        )
        self.assertEqual(self._pick("examples/demo/thing.txt"), {GATE, "tests/test_examples.py"})

    def test_documents_only_run_the_gate(self):
        self.assertEqual(self._pick("docs/x.md", "README.md", "AGENTS.md"), {GATE})

    def test_a_changed_suite_runs_itself(self):
        self.assertEqual(self._pick("tests/test_unrelated.py"), {GATE, "tests/test_unrelated.py"})

    def test_nothing_changed_or_nothing_covering_is_said_out_loud(self):
        selection = related_suites.select_suites(self.repo, self.suites, [])
        self.assertEqual(selection.suites, (GATE,))
        self.assertTrue(selection.notes)
        selection = related_suites.select_suites(self.repo, self.suites, ["core/lonely.py"])
        self.assertEqual(selection.suites, (GATE,))
        self.assertIn("没有测试覆盖：core/lonely.py", selection.notes)

    def test_changed_files_come_from_git_including_new_and_renamed_ones(self):
        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                cwd=str(self.repo), check=True, capture_output=True,
            )

        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        (self.repo / "core/a.py").write_text("X = 2\n", encoding="utf-8")
        _write(self.repo, "core/new.py", "N = 1\n")
        git("mv", "core/c.py", "core/c2.py")
        changed = set(related_suites.changed_files_from_git(self.repo))
        self.assertTrue({"core/a.py", "core/new.py", "core/c.py", "core/c2.py"} <= changed, changed)


class TestPickingInThisRepository(unittest.TestCase):
    """在真仓库上看一眼：挑法既不能漏掉明显相关的套件，也不能退化成“什么都挑”。"""

    def test_a_chunker_change_picks_its_own_and_the_pipeline_suites_but_not_the_page_index(self):
        suites = [s.rel for s in run.all_suites()]
        picked = set(related_suites.select_suites(
            REPO_ROOT, suites, ["plugins/official-chunker/official_chunker/chunk.py"],
        ).suites)
        self.assertIn("plugins/official-chunker/tests/test_chunk.py", picked)
        self.assertIn("tests/test_pipeline_e2e.py", picked)
        self.assertIn(GATE, picked)
        self.assertNotIn("plugins/official-visual-wemm/tests/test_plugin.py", picked)
        self.assertLess(len(picked), len(suites))


if __name__ == "__main__":
    unittest.main()
