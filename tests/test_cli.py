"""业务 CLI（core/cli.py）的端到端测试：真实插件运行时 + 假模型。

后面几组（TestPathAnchoring / TestIndexMutualExclusion / TestFreshExtract /
TestCliSingletonGuard / TestCleanErrorOutput）用桩 pipeline 断言"CLI 的接线"
本身：默认路径锚定、库锁互斥、--fresh-extract 透传、单例守卫、错误输出形状。
这些是 2026-09-27 CLI 缺陷修复波的复现用例，不去重复端到端索引的行为断言
（那是 tests/test_pipeline_e2e.py 的范围）。
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

import core.cli as cli  # noqa: E402
from core import paths  # noqa: E402
from core.cli import main  # noqa: E402
from core.index_progress import _status_key  # noqa: E402
from core.singleton import FileByteLock, ProcessSingletonGuard  # noqa: E402


class _FakeEncoder:
    KEYWORDS = ["插件", "架构", "厨房", "食谱"]

    def encode(self, texts):
        return [[float(t.count(k)) for k in self.KEYWORDS] for t in texts]


def _inject_fakes(runtime):
    from official_embedder_bge_m3.embed import BGEM3Embedder
    from official_reranker.rerank import RerankerEngine

    runtime.plugins["official-embedder-bge-m3"].instance.embedder = BGEM3Embedder(
        encoder=_FakeEncoder()
    )
    fake_reranker = type(
        "_FakeRerankerEngine",
        (),
        {
            "idle_check": lambda self: None,
            "release_gpu_slot": lambda self: None,
            "rerank": lambda self, query, pairs, top_k=10: [
                (cid, 0.9 - i * 0.01) for i, (cid, _text) in enumerate(pairs)
            ][:top_k],
        },
    )()
    runtime.plugins["official-reranker"].instance.engine = fake_reranker


class TestBusinessCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "plugin-notes.md").write_text(
            "# 插件架构笔记\n\n这篇笔记讲插件系统的架构设计。", encoding="utf-8"
        )
        (self.vault / "cooking.md").write_text(
            "# 厨房笔记\n\n这篇笔记记录了几个食谱。", encoding="utf-8"
        )
        self.data_root = self.tmp / "data"
        self.args = [
            "--plugins-dir", str(REPO_ROOT / "plugins"),
            "--state-file", str(self.tmp / "plugins_state.json"),
            "--data-root", str(self.data_root),
        ]

    def _run(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([*self.args, *argv])
        return code, buffer.getvalue()

    def test_libraries_add_list_remove_round_trip(self):
        self._run("libraries", "add", str(self.vault), "--name", "测试库", "--id", "test-lib")
        code, out = self._run("libraries", "list")
        self.assertEqual(code, 0)
        self.assertIn("test-lib", out)
        self.assertIn("测试库", out)
        code, _ = self._run("libraries", "remove", "test-lib")
        self.assertEqual(code, 0)
        _, out = self._run("libraries", "list")
        self.assertNotIn("test-lib", out)

    def test_index_and_dedup_commands(self):
        # 先注入假模型再跑 index——CLI 每次调用独立 boot，注入要落在 boot 之后
        original_boot = None
        import core.cli as cli

        original_boot = cli._boot_pipeline

        def _boot_with_fakes(args):
            runtime, pipeline = original_boot(args)
            _inject_fakes(runtime)
            return runtime, pipeline

        with patch.object(cli, "_boot_pipeline", side_effect=_boot_with_fakes):
            self._run("libraries", "add", str(self.vault), "--id", "test-lib")
            code, out = self._run("index", "--library", "test-lib")
            self.assertEqual(code, 0, out)
            self.assertIn("索引完成", out)
            self.assertIn("成功 2", out)
            code, out = self._run("dedup", "--library", "test-lib")
            self.assertEqual(code, 0, out)

    def test_fresh_extract_flag_reaches_the_real_pipeline_verbatim(self):
        """CLI ↔ **真的** `core/pipeline.py::index_library` 的接线。

        TestFreshExtract 那组全用桩，桩只能证明"CLI 内部的判断自洽"——证明不
        了 CLI 传的参数名/取值与管道真实签名对得上。这里在真管道上跑两轮
        （不带旗标 / 带旗标），把实际收到的 kwargs 记下来比对：改造前 CLI 只要
        发现管道"支持"就硬塞 `fresh_extract=True`，不带旗标的那一轮会在这里
        变成 True——`core/pipeline.py` 里 `force_extract = fresh_extract` 会连带
        清掉提取缓存正文，等于用户没要求却白烧一遍 MinerU/OCR 配额。
        """
        import core.cli as cli
        import core.pipeline

        original_boot = cli._boot_pipeline
        seen: list[dict] = []
        real_index = core.pipeline.Pipeline.index_library

        def _boot_with_fakes(args):
            runtime, pipeline = original_boot(args)
            _inject_fakes(runtime)
            return runtime, pipeline

        def _spy_index(self, *args, **kwargs):
            seen.append(dict(kwargs))
            return real_index(self, *args, **kwargs)

        with patch.object(cli, "_boot_pipeline", side_effect=_boot_with_fakes), patch.object(
            core.pipeline.Pipeline, "index_library", _spy_index
        ):
            self._run("libraries", "add", str(self.vault), "--id", "test-lib")
            code, out = self._run("index", "--library", "test-lib")
            self.assertEqual(code, 0, out)
            code, out = self._run("index", "--library", "test-lib", "--fresh-extract")
            self.assertEqual(code, 0, out)
        self.assertEqual(
            [call.get("fresh_extract") for call in seen],
            [False, True],
            "CLI 必须把 --fresh-extract 的真实取值（含 False）透传给管道",
        )

    def test_export_import_round_trip(self):
        import core.cli as cli

        original_boot = cli._boot_pipeline

        def _boot_with_fakes(args):
            runtime, pipeline = original_boot(args)
            _inject_fakes(runtime)
            return runtime, pipeline

        with patch.object(cli, "_boot_pipeline", side_effect=_boot_with_fakes):
            self._run("libraries", "add", str(self.vault), "--id", "test-lib")
            self._run("index", "--library", "test-lib")
            out_zip = self.tmp / "backup.zip"
            code, out = self._run("export", "--library", "test-lib", "--out", str(out_zip))
            self.assertEqual(code, 0, out)
            self.assertTrue(out_zip.is_file())
            # 导入目标目录必须先建好：库注册表现在对齐
            # obsidian-rag/library.py:489-491 要求库根 resolve 后确实是已存在
            # 的目录（路径打错必须在创建时报错，而不是拖到之后每次检索的
            # freshness 诊断里才报 missing）。旧项目的 import.py:248 是
            # `mkdir(parents=True, exist_ok=True)` 之后才 add_library，REDO
            # 侧这个 mkdir 应该在 core/pipeline.py::import_library 里补上。
            restored_root = self.tmp / "restored"
            restored_root.mkdir(exist_ok=True)
            code, out = self._run(
                "import", str(out_zip), "--root", str(restored_root), "--library-id", "restored-lib"
            )
            self.assertEqual(code, 0, out)
            _, listing = self._run("libraries", "list")
            self.assertIn("restored-lib", listing)

    def test_unknown_library_fails_cleanly(self):
        import io as _io
        import contextlib as _contextlib
        import core.cli as cli

        original_boot = cli._boot_pipeline

        def _boot_with_fakes(args):
            runtime, pipeline = original_boot(args)
            _inject_fakes(runtime)
            return runtime, pipeline

        with patch.object(cli, "_boot_pipeline", side_effect=_boot_with_fakes):
            buffer = _io.StringIO()
            with _contextlib.redirect_stderr(buffer):
                code = main([*self.args, "index", "--library", "no-such-lib"])
            self.assertEqual(code, 1)
            self.assertIn("错误", buffer.getvalue())


# ---- 桩 pipeline：只桩 CLI 真正碰到的那几个成员 -------------------------------


class _StubReport:
    """索引成功时的最小报告替身——CLI 的 index 分支只读这几个计数和 files。"""

    def __init__(self) -> None:
        self.added = 1
        self.changed = 0
        self.unchanged = 0
        self.removed = 0
        self.retried = 0
        self.succeeded = 1
        self.failed = 0
        self.deferred = 0
        self.files = ()


class _StubLibrary:
    def __init__(self, library_id: str) -> None:
        self.library_id = library_id
        self.name = library_id
        self.root_path = str(REPO_ROOT)


class _StubStore:
    def __init__(self, library_ids) -> None:
        self._libs = {lid: _StubLibrary(lid) for lid in library_ids}

    def get(self, library_id):
        return self._libs.get(library_id)

    def list_libraries(self):
        return list(self._libs.values())


class _StubRegistry:
    def active_of(self, point):
        return "stub-library-manager" if point == "library_manager" else None


class _StubRuntime:
    def __init__(self) -> None:
        self.registry = _StubRegistry()


class _StubPipeline:
    """最小 pipeline 替身：`_singleton()` 与 index 分支要用的就这些成员。"""

    def __init__(self, record, *, library_ids=("lib",), on_index=None) -> None:
        self._record = record
        self._on_index = on_index
        self._mgr = type("_StubLibraryManager", (), {"store": _StubStore(library_ids)})()
        self.runtime = _StubRuntime()

    def _plugin(self, plugin_id):
        return self._mgr

    def _finish(self, library_id, kwargs):
        self._record.setdefault("calls", []).append({"library_id": library_id, **kwargs})
        if self._on_index is not None:
            self._on_index()
        return _StubReport()


def _make_stub_pipeline(record, *, library_ids=("lib",), on_index=None, fresh_extract=False):
    """`fresh_extract=False` 造出的是"core/pipeline.py 还没接这个参数"的形状
    （签名里既没有 fresh_extract 也没有 **kwargs），`True` 才是接线完成后的形状。"""
    pipe = _StubPipeline(record, library_ids=library_ids, on_index=on_index)

    if fresh_extract:
        def index_library(library_id, *, full=False, fresh_extract=False):
            return pipe._finish(library_id, {"full": full, "fresh_extract": fresh_extract})
    else:
        def index_library(library_id, *, full=False):
            return pipe._finish(library_id, {"full": full})

    pipe.index_library = index_library
    return pipe


@contextlib.contextmanager
def _stub_boot(pipeline):
    with patch.object(cli, "_boot_pipeline", side_effect=lambda args: (_StubRuntime(), pipeline)):
        yield


class _CliTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_root = self.tmp / "data"
        self.state_file = self.tmp / "plugins_state.json"
        self.record = {}

    def _argv(self, *argv: str) -> list[str]:
        return [
            "--plugins-dir", str(REPO_ROOT / "plugins"),
            "--state-file", str(self.state_file),
            "--data-root", str(self.data_root),
            *argv,
        ]

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(self._argv(*argv))
        return code, out.getvalue(), err.getvalue()

    def _lock_path(self, library_id: str = "lib") -> Path:
        """worker 侧用的同一个库锁文件（core/index_progress.py:268）。"""
        return self.data_root / "index_progress" / "locks" / f"{_status_key(library_id)}.lock"


class TestPathAnchoring(_CliTestBase):
    """缺陷1：默认路径必须锚定到仓库根 / RAG_REDO_DATA_ROOT，绝不跟 CWD 走。

    旧项目 `obsidian-rag/config.py:14` 是 `DATA_DIR = Path(__file__).parent /
    "data"`——CLI 在任何目录跑都指向同一份数据。
    """

    def _parse_in_tmp_cwd(self, argv: list[str]):
        parser = cli._build_parser()
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            return parser.parse_args(argv)
        finally:
            os.chdir(cwd)

    def test_default_paths_are_anchored_not_cwd_relative(self):
        args = self._parse_in_tmp_cwd(["status"])
        self.assertEqual(args.data_root, paths.data_root())
        self.assertEqual(args.plugins_dir, paths.plugins_dir())
        self.assertEqual(args.state_file, paths.data_root() / "plugins_state.json")
        # 明确钉住"不再是旧实现那三个相对 CWD 的默认值"
        self.assertNotEqual(args.data_root, Path("data"))
        self.assertNotEqual(args.plugins_dir, Path("plugins"))
        self.assertNotEqual(args.state_file, Path("data/plugins_state.json"))
        self.assertTrue(args.data_root.is_absolute())
        self.assertTrue(args.plugins_dir.is_absolute())

    def test_data_root_env_var_wins_and_state_file_follows_it(self):
        env_root = self.tmp / "envdata"
        with patch.dict(os.environ, {paths.DATA_ROOT_ENV: str(env_root)}):
            args = self._parse_in_tmp_cwd(["status"])
        self.assertEqual(args.data_root, env_root)
        self.assertEqual(args.state_file, env_root / "plugins_state.json")

    def test_frozen_build_anchors_to_localappdata(self):
        env = {k: v for k, v in os.environ.items() if k != paths.DATA_ROOT_ENV}
        env["LOCALAPPDATA"] = str(self.tmp / "AppData")
        with patch.dict(os.environ, env, clear=True), patch.object(sys, "frozen", True, create=True):
            args = self._parse_in_tmp_cwd(["status"])
        self.assertEqual(args.data_root, self.tmp / "AppData" / "RAG-Redo" / "data")
        self.assertEqual(args.plugins_dir, Path(sys.executable).parent / "plugins")

    def test_paths_rules_match_gui_and_mcp_entries(self):
        """同一份数据只能有一处权威路径判定（docs/DATA_FLOW.md 规则4）——
        改造前 cli.py 是第三份抄写且抄错了，这里钉住与两个胶水入口一致。"""
        env = {k: v for k, v in os.environ.items() if k != paths.DATA_ROOT_ENV}
        with patch.dict(os.environ, env, clear=True):
            for module_name in ("gui_main", "mcp_stdio"):
                try:
                    module = __import__(module_name)
                except ImportError as exc:  # 该入口的依赖没装就不硬跑
                    self.skipTest(f"{module_name} 无法导入：{exc}")
                self.assertEqual(
                    Path(module.DATA_ROOT), paths.data_root(), f"{module_name} 的 DATA_ROOT 与 core.paths 不一致"
                )
                self.assertEqual(
                    Path(module.REPO_ROOT) / "plugins", paths.plugins_dir(), f"{module_name} 的 plugins 目录不一致"
                )

    def test_the_three_entries_enable_the_same_business_plugins(self):
        """CLI / GUI / MCP 三个入口启用的业务插件必须是同一份，差异只允许是各自的"门面"
        插件（GUI 壳 / MCP 服务）。此前 CLI 少了 `official-visual-wemm`：同一个库用 CLI
        建索引时静默跳过页级视觉阶段，与 GUI/MCP 触发的索引产出的派生数据不一样，
        而且没有任何提示。"""
        facades = {"gui_main": "official-gui-shell", "mcp_stdio": "official-mcp-server"}
        for module_name, facade in facades.items():
            try:
                module = __import__(module_name)
            except ImportError as exc:  # 该入口的依赖没装就不硬跑
                self.skipTest(f"{module_name} 无法导入：{exc}")
            entry = list(module.REQUIRED_PLUGINS)
            self.assertIn(facade, entry, f"{module_name} 缺自己的门面插件 {facade}")
            self.assertEqual(
                [p for p in entry if p != facade],
                cli.BUSINESS_PLUGINS,
                f"{module_name} 与 CLI 启用的业务插件（含顺序）不一致",
            )

    def test_visual_plugin_is_enabled_only_for_commands_that_touch_page_level_data(self):
        """页级视觉插件的 `on_enable` 会立刻抢 GPU 租约并拉起看图子进程，不能让
        `libraries list` / `dedup` 这类与它无关的命令也白白拉起一个 GPU 服务。"""
        for command in ("index", "export", "import"):
            self.assertIn("official-visual-wemm", cli._plugins_for(command), command)
        for command in ("libraries", "dedup"):
            self.assertNotIn("official-visual-wemm", cli._plugins_for(command), command)
        # 其余业务插件一个都不因此缺失，顺序不变
        self.assertEqual(
            [p for p in cli.BUSINESS_PLUGINS if p != "official-visual-wemm"], cli.REQUIRED_PLUGINS
        )
        self.assertEqual(cli._plugins_for("index"), cli.BUSINESS_PLUGINS)

    def test_business_commands_close_the_runtime_even_when_the_command_fails(self):
        """CLI 是短进程：插件拉起的子进程（看图服务）不会跟着父进程退出，命令结束必须收口，
        否则每条命令泄漏一对孤儿进程（这条是真实踩到的：一次测试跑出 17 对游离 WEMM 服务）。"""
        closed: list[bool] = []

        class _ClosableRuntime(_StubRuntime):
            def close(self) -> None:
                closed.append(True)

        pipeline = _make_stub_pipeline(self.record, library_ids=("lib",))
        with patch.object(cli, "_boot_pipeline", side_effect=lambda args: (_ClosableRuntime(), pipeline)):
            code, out, _err = self._run("libraries", "list")
            self.assertEqual(code, 0)
            self.assertEqual(closed, [True])
            code, _out, err = self._run("index", "--library", "no-such-lib")  # 库不存在：早退
            self.assertEqual(code, 1)
            self.assertEqual(closed, [True, True], "命令提前失败也必须收口")

        def _boom(*_args, **_kwargs):
            raise RuntimeError("插件炸了")

        pipeline.index_library = _boom
        with patch.object(cli, "_boot_pipeline", side_effect=lambda args: (_ClosableRuntime(), pipeline)):
            code, _out, err = self._run("index", "--library", "lib")
        self.assertEqual(code, 1)
        self.assertIn("插件炸了", err)
        self.assertEqual(closed, [True, True, True], "命令抛异常也必须收口")

    def test_a_failing_runtime_close_never_changes_the_command_exit_code(self):
        class _BadClose(_StubRuntime):
            def close(self) -> None:
                raise OSError("taskkill 失败")

        pipeline = _make_stub_pipeline(self.record, library_ids=("lib",))
        with patch.object(cli, "_boot_pipeline", side_effect=lambda args: (_BadClose(), pipeline)):
            code, _out, _err = self._run("libraries", "list")
        self.assertEqual(code, 0)

    def test_plugin_management_branch_forwards_data_root_to_runtime(self):
        """插件管理分支（scan/status/load/...）此前**根本不传 data_dir**，
        PluginRuntime 于是回落到自己的默认值 Path("data")（core/runtime.py:58），
        同一进程内两条分支用两个数据根。"""
        empty = self.tmp / "empty_plugins"
        empty.mkdir()
        recorded = {}
        real_runtime_cls = cli.PluginRuntime

        def spy(*args, **kwargs):
            recorded.update(kwargs)
            return real_runtime_cls(*args, **kwargs)

        with patch.object(cli, "PluginRuntime", side_effect=spy):
            code, _out, _err = self._run("status")
        self.assertEqual(code, 0)
        self.assertEqual(Path(recorded.get("data_dir", Path())), self.data_root)
        self.assertEqual(Path(recorded.get("state_file", Path())), self.state_file)


class TestIndexMutualExclusion(_CliTestBase):
    """缺陷3：CLI 的同步索引必须和 GUI/MCP 的 worker 互斥（同一把库锁）。

    旧项目 `obsidian-rag/index.py:1702-1730 write_lock()` + `:2480-2481`：
    拿不到写锁就抛 LockBusyError，CLI 遇之直接终止整轮。
    """

    def test_index_holds_the_same_library_lock_the_worker_uses(self):
        seen = {}

        def probe():
            other = FileByteLock(self._lock_path())
            acquired = other.acquire()
            seen["cli_holds_lock"] = not acquired
            other.release()

        pipe = _make_stub_pipeline(self.record, on_index=probe, fresh_extract=True)
        with _stub_boot(pipe):
            code, out, _err = self._run("index", "--library", "lib")
        self.assertEqual(code, 0, out)
        self.assertTrue(seen.get("cli_holds_lock"), "索引期间 CLI 必须持库锁（与 worker 同名同路径）")

    def test_busy_library_lock_fails_with_one_clean_line(self):
        held = FileByteLock(self._lock_path())
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)
        held.write(str(os.getpid()).encode("ascii"))
        pipe = _make_stub_pipeline(self.record, fresh_extract=True)
        with _stub_boot(pipe), patch.object(cli, "LIBRARY_LOCK_TIMEOUT_S", 0.05), patch.object(
            cli, "LIBRARY_LOCK_POLL_S", 0.01
        ):
            code, _out, err = self._run("index", "--library", "lib")
        self.assertEqual(code, 1)
        # 桩 pipeline 只在 index_library 真被调用时才往 record 里塞 "calls"
        # 键（见 _StubPipeline._finish），所以"没调用"表现为键不存在——默认值
        # 必须给 []，否则这条断言在"确实没调用"时反而失败（None != []）。
        self.assertEqual(self.record.get("calls", []), [], "拿不到锁时不得开始索引")
        self.assertIn("index_status", err)  # 与旧项目 LockBusyError 文案同一句指引
        self.assertIn("lib", err)
        self.assertNotIn("Traceback", err)

    def test_busy_lock_gives_up_at_the_timeout_configured_at_call_time(self):
        """回归：`LIBRARY_LOCK_TIMEOUT_S` / `LIBRARY_LOCK_POLL_S` 必须在**调用
        时**读模块常量。改造前它们是默认参数值（定义时就绑死），于是"把常量
        打成 0.05 秒"完全无效、实际死等 60 秒——而且不报任何错，只表现为
        "某个用例慢得离谱"，非常难查。现在直接把等待时长钉进断言。"""
        held = FileByteLock(self._lock_path())
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)
        pipe = _make_stub_pipeline(self.record, fresh_extract=True)
        started = time.monotonic()
        with _stub_boot(pipe), patch.object(cli, "LIBRARY_LOCK_TIMEOUT_S", 0.2), patch.object(
            cli, "LIBRARY_LOCK_POLL_S", 0.02
        ):
            code, _out, err = self._run("index", "--library", "lib")
        waited = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertIn("索引锁等待超时", err)
        self.assertLess(waited, 5.0, f"库锁等待没有按 0.2 秒超时收口（实际 {waited:.1f} 秒）")

    def test_second_cli_waits_for_the_lock_then_indexes(self):
        released = threading.Event()
        holding = threading.Event()

        def holder():
            held = FileByteLock(self._lock_path())
            # 拿不到锁就直接不放 holding.set()，主线程 10 秒后断言失败——
            # 线程里写 assert 的失败会被线程吞掉，用例会假绿
            if not held.acquire():
                return
            held.write(str(os.getpid()).encode("ascii"))
            holding.set()
            time.sleep(0.4)
            held.release()
            released.set()

        thread = threading.Thread(target=holder)
        thread.start()
        self.addCleanup(thread.join, 10)
        # 必须等占锁方**真的拿到锁**再让 CLI 跑：线程启动和主线程走到取锁之间
        # 谁先谁后不确定，之前的版本在这里直接开跑，于是 CLI 有时先抢到锁、
        # 全程不等待，"等待索引锁"那行提示从未打印，用例随机红。
        self.assertTrue(holding.wait(10), "占锁线程没能在 10 秒内拿到库锁")
        pipe = _make_stub_pipeline(self.record, fresh_extract=True)
        with _stub_boot(pipe), patch.object(cli, "LIBRARY_LOCK_TIMEOUT_S", 20.0), patch.object(
            cli, "LIBRARY_LOCK_POLL_S", 0.02
        ):
            code, out, err = self._run("index", "--library", "lib")
        thread.join(timeout=10)
        self.assertTrue(released.is_set())
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.record.get("calls", [])), 1, "锁释放后应当排队进去索引，而不是失败")
        self.assertIn("等待", err)

    def test_index_of_unknown_library_reports_before_touching_lock(self):
        pipe = _make_stub_pipeline(self.record, library_ids=(), fresh_extract=True)
        with _stub_boot(pipe):
            code, _out, err = self._run("index", "--library", "nope")
        self.assertEqual(code, 1)
        self.assertIn("nope", err)
        self.assertFalse(self._lock_path("nope").exists(), "库不存在时不该为它创建锁文件")


class TestFreshExtract(_CliTestBase):
    """缺陷4：--fresh-extract（忽略提取缓存强制重提），旧项目
    `obsidian-rag/index.py:2445-2447` 有，与 --full 是两个独立语义。"""

    def test_fresh_extract_flag_is_parsed(self):
        args = cli._build_parser().parse_args(["index", "--library", "lib", "--fresh-extract"])
        self.assertTrue(args.fresh_extract)
        self.assertFalse(cli._build_parser().parse_args(["index", "--library", "lib"]).fresh_extract)

    def test_fresh_extract_true_is_passed_to_index_library(self):
        pipe = _make_stub_pipeline(self.record, fresh_extract=True)
        with _stub_boot(pipe):
            code, out, _err = self._run("index", "--library", "lib", "--fresh-extract")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.record["calls"][0]["fresh_extract"], True)

    def test_absent_flag_passes_false(self):
        pipe = _make_stub_pipeline(self.record, fresh_extract=True)
        with _stub_boot(pipe):
            code, out, _err = self._run("index", "--library", "lib")
        self.assertEqual(code, 0, out)
        self.assertIs(self.record["calls"][0]["fresh_extract"], False)

    def test_unsupported_pipeline_fails_cleanly_without_traceback(self):
        """`index_library` 还不收 `fresh_extract` 时（接线前的形状），必须一行
        中文错误 + 退出码 1，而不是 TypeError traceback，更不能静默忽略旗标。

        "不支持"是用**桩**造出来的（`_make_stub_pipeline(fresh_extract=False)`
        给出一个签名里既没有 fresh_extract 也没有 **kwargs 的 index_library），
        不能靠"等真的 core/pipeline.py 不支持"——那个代理一旦落地，这条用例
        就变成永远走不到分支的假绿。下面先把前提钉死，桩形状哪天被改坏、
        或 CLI 改了能力探测方式，都会在这一行炸，而不是悄悄测成别的东西。
        """
        pipe = _make_stub_pipeline(self.record, fresh_extract=False)
        self.assertFalse(cli._fresh_extract_supported(pipe), "桩必须真的处于'不支持'状态")
        with _stub_boot(pipe):
            code, _out, err = self._run("index", "--library", "lib", "--fresh-extract")
        self.assertEqual(code, 1)
        self.assertIn("fresh-extract", err)
        self.assertNotIn("Traceback", err)
        # 同上：桩只在真被调用时建 "calls" 键，缺键即"没调用"。
        self.assertEqual(self.record.get("calls", []), [], "不支持时不得开始索引")

    def test_flag_absent_keeps_working_on_pipeline_without_the_parameter(self):
        pipe = _make_stub_pipeline(self.record, fresh_extract=False)
        with _stub_boot(pipe):
            code, out, _err = self._run("index", "--library", "lib")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.record["calls"]), 1)


class TestCliSingletonGuard(_CliTestBase):
    """缺陷2：CLI 也要有单例守卫，但只给会写共享数据的命令——
    只读诊断/插件状态命令必须仍然可以并发跑。"""

    def test_mutating_command_refuses_while_another_cli_instance_holds_lock(self):
        guard = ProcessSingletonGuard(self.data_root / "cli.pid")
        self.assertTrue(guard.acquire())
        self.addCleanup(guard.release)
        pipe = _make_stub_pipeline(self.record, fresh_extract=True)
        with _stub_boot(pipe):
            code, _out, err = self._run("index", "--library", "lib")
        self.assertEqual(code, 1)
        self.assertIn("单例守卫", err)
        self.assertEqual(self.record.get("calls", []), [], "守卫被占时不得开始索引")

    def test_readonly_libraries_list_is_not_blocked(self):
        guard = ProcessSingletonGuard(self.data_root / "cli.pid")
        self.assertTrue(guard.acquire())
        self.addCleanup(guard.release)
        pipe = _make_stub_pipeline(self.record, library_ids=("lib",))
        with _stub_boot(pipe):
            code, out, _err = self._run("libraries", "list")
        self.assertEqual(code, 0, out)
        self.assertIn("lib", out)

    def test_readonly_plugin_command_is_not_blocked(self):
        guard = ProcessSingletonGuard(self.data_root / "cli.pid")
        self.assertTrue(guard.acquire())
        self.addCleanup(guard.release)
        code, out, _err = self._run("status")
        self.assertEqual(code, 0, out)

    def test_guard_fault_fails_open_and_command_still_runs(self):
        """守卫自身故障（锁文件路径是目录 → 打不开）必须 fail-open：
        对齐 obsidian-rag/singleton.py:96-98 与 guiweb/app.py:62-66。
        同时必须说清"没拿到锁"，不能让人以为有互斥保护（AGENTS.md §7）。"""
        self.data_root.mkdir(parents=True, exist_ok=True)
        (self.data_root / "cli.pid").mkdir()
        pipe = _make_stub_pipeline(self.record, fresh_extract=True)
        with _stub_boot(pipe):
            code, _out, err = self._run("index", "--library", "lib")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.record.get("calls", [])), 1)
        self.assertIn("单例守卫失败", err)
        self.assertIn("没有拿到", err)

    def test_holds_lock_helper_distinguishes_acquired_from_fail_open(self):
        guard = ProcessSingletonGuard(self.data_root / "cli.pid")
        self.assertTrue(guard.acquire())
        self.addCleanup(guard.release)
        self.assertTrue(cli._guard_holds_lock(guard))
        guard.release()
        self.assertFalse(cli._guard_holds_lock(guard))
        with patch.object(guard._lock, "acquire", side_effect=OSError("boom")):
            with redirect_stderr(io.StringIO()):
                self.assertTrue(guard.acquire())
        self.assertFalse(cli._guard_holds_lock(guard), "fail-open 时不得宣称自己持有锁")


class TestCleanErrorOutput(_CliTestBase):
    """缺陷1 的近邻：CLI 的错误必须是一行中文，不是 Python traceback
    （旧项目 `obsidian-rag/library.py:770-772` 就是 `except (ValueError,
    RuntimeError): log(f"错误：{e}")`）。"""

    def test_systemexit_with_message_prints_one_line_not_traceback(self):
        """`raise SystemExit("错误：…")`（`_singleton()` 就是这么抛的）此前在
        `except SystemExit` 分支里被 `int(exc.code)` 再炸一次 ValueError，
        用户看到的是两层 traceback。这里用真的空插件目录触发同一条路径。"""
        empty = self.tmp / "empty_plugins"
        empty.mkdir()
        argv = [
            "--plugins-dir", str(empty),
            "--state-file", str(self.state_file),
            "--data-root", str(self.data_root),
            "libraries", "list",
        ]
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(argv)
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err.getvalue())
        self.assertIn("没有已启用的 library_manager", err.getvalue())

    def test_libraries_add_bad_path_reports_message_without_exception_type(self):
        code, _out, err = self._run("libraries", "add", str(self.tmp / "no-such-dir"))
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("ValueError", err)
        self.assertIn("路径不存在或不是目录", err)


if __name__ == "__main__":
    unittest.main()
