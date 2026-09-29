"""真实通过 PluginRuntime 走一遍 official-ocr-mineru-local 的完整生命
周期：真的 Popen 子进程、真的发本机HTTP、真的申请/释放GPU资源租约、
真的在 disable 时把子进程杀干净。

`RAG_REDO_FAKE_OCR=1` 让子进程内部用确定性假OCR结果，不需要真实模型——
验证的是"这条子进程+HTTP+chain-try链路本身通不通"，不是"识别准不准"，
见 official_ocr_mineru_local/plugin.py 模块 docstring。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.runtime import PluginRuntime, PluginState  # noqa: E402


def _process_is_gone(pid: int) -> bool:
    """跨平台的"这个 pid 是不是真的没了"检查，理由同
    tests/test_runtime.py 里同名函数——POSIX 的 os.kill(pid, 0) 信号-0
    探测语义在 Windows 上不成立（直接抛 OSError 而不是
    ProcessLookupError），得走 Win32 OpenProcess API。"""
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class TestMineruLocalOcrPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env_backup = os.environ.get("RAG_REDO_FAKE_OCR")
        os.environ["RAG_REDO_FAKE_OCR"] = "1"
        self.addCleanup(self._restore_env)

        self.rt = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.rt.scan()
        self.rt.settings.set("pdf_scan_backend", "mineru-local")
        self.assertIn("official-ocr-mineru-local", self.rt.plugins)
        self.rt.load("official-ocr-mineru-local")
        self.assertEqual(self.rt.plugins["official-ocr-mineru-local"].state, PluginState.LOADED)
        self.rt.enable("official-ocr-mineru-local")
        self.assertEqual(
            self.rt.plugins["official-ocr-mineru-local"].state,
            PluginState.ENABLED,
            self.rt.plugins["official-ocr-mineru-local"].error,
        )
        self.instance = self.rt.plugins["official-ocr-mineru-local"].instance
        self.plugin_module = sys.modules[type(self.instance).__module__]
        self.addCleanup(lambda: self.rt.disable("official-ocr-mineru-local"))

    def _restore_env(self) -> None:
        if self._env_backup is None:
            os.environ.pop("RAG_REDO_FAKE_OCR", None)
        else:
            os.environ["RAG_REDO_FAKE_OCR"] = self._env_backup

    def test_default_none_does_not_start_ocr_subprocess(self):
        runtime = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "default-none-state.json",
            data_dir=self.tmp / "default-none-data",
        )
        runtime.scan()
        runtime.load("official-ocr-mineru-local")
        runtime.enable("official-ocr-mineru-local")
        self.addCleanup(runtime.disable, "official-ocr-mineru-local")
        plugin = runtime.plugins["official-ocr-mineru-local"].instance
        self.assertFalse(plugin.is_active())
        self.assertIsNone(plugin._handle)
        self.assertIsNone(runtime.resource_arbiter.holder_of("gpu:0"))

    def test_enable_acquires_gpu_lease(self):
        self.assertEqual(self.rt.resource_arbiter.holder_of("gpu:0"), "official-ocr-mineru-local")

    def test_worker_process_takes_over_and_parent_can_reacquire_later(self):
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from pathlib import Path\n"
            "from core.runtime import PluginRuntime\n"
            f"runtime = PluginRuntime(Path({str(REPO_ROOT / 'plugins')!r}), "
            f"state_file=Path({str(self.tmp / 'worker_state.json')!r}), "
            f"data_dir=Path({str(self.tmp / 'data')!r}))\n"
            "runtime.scan()\n"
            "runtime.load('official-ocr-mineru-local')\n"
            "runtime.enable('official-ocr-mineru-local')\n"
            "plugin = runtime.plugins['official-ocr-mineru-local']\n"
            "print('WORKER_ENABLED', plugin.instance._enabled, flush=True)\n"
            "runtime.disable('official-ocr-mineru-local')\n"
            "runtime.unload('official-ocr-mineru-local')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WORKER_ENABLED True", result.stdout)
        self.assertIsNone(self.rt.resource_arbiter.holder_of("gpu:0"))
        self.assertTrue(self.instance._handle.is_alive)
        (self.tmp / "after-worker.pdf").write_bytes(b"%PDF-fake-scanned-content")
        document = self.instance.extract("lib-after-worker", "after-worker.pdf", self.tmp)
        self.assertIn("fake-ocr", document.text)
        self.assertEqual(self.rt.resource_arbiter.holder_of("gpu:0"), "official-ocr-mineru-local")

    def test_extract_real_pdf_via_subprocess_returns_fake_text(self):
        (self.tmp / "scan.pdf").write_bytes(b"%PDF-fake-scanned-content")
        doc = self.instance.extract("lib1", "scan.pdf", self.tmp)
        self.assertIsNotNone(doc.text)
        self.assertIn("fake-ocr", doc.text)
        self.assertIn("scan.pdf", doc.text)
        self.assertIsNone(doc.failure_reason)

    def test_extract_non_pdf_skipped(self):
        (self.tmp / "notes.txt").write_text("纯文本", encoding="utf-8")
        doc = self.instance.extract("lib1", "notes.txt", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIn("不是PDF", doc.failure_reason)

    def test_extract_missing_file_folds_to_failure(self):
        doc = self.instance.extract("lib1", "does-not-exist.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIsNotNone(doc.failure_reason)

    def test_disable_releases_gpu_lease_and_kills_subprocess(self):
        pid = self.instance._handle._process.pid  # noqa: SLF001 - 直接问操作系统这个pid还在不在
        self.rt.disable("official-ocr-mineru-local")
        self.assertIsNone(self.rt.resource_arbiter.holder_of("gpu:0"))
        self.assertTrue(_process_is_gone(pid))

    def test_release_gpu_soft_evicts_without_killing_subprocess(self):
        """手动"释放显存"按钮用（core/pipeline.py::release_gpu_memory，
        2026-09-29 新能力，BC-16）——同 official-visual-wemm 同名测试：子
        进程（同一个 pid）继续存活，只是模型被请求卸载，下次真正提取时
        子进程自己按需重新加载模型，不需要用户重新开启本机OCR或重启GUI。"""
        pid_before = self.instance._handle._process.pid  # noqa: SLF001
        self.instance.release_gpu()
        self.assertIsNotNone(self.instance._handle)  # noqa: SLF001
        self.assertEqual(self.instance._handle._process.pid, pid_before)  # noqa: SLF001
        self.assertTrue(self.instance._handle.is_alive)
        (self.tmp / "after-release.pdf").write_bytes(b"%PDF-fake-scanned-content")
        doc = self.instance.extract("lib-after-release", "after-release.pdf", self.tmp)
        self.assertIn("fake-ocr", doc.text)

    def test_release_gpu_is_a_noop_when_subprocess_never_started(self):
        """本机OCR后端本来就关着/子进程还没拉起来时，点"释放显存"必须是
        安全的空操作，不能抛异常。"""
        self.rt.disable("official-ocr-mineru-local")
        self.instance.release_gpu()  # 不抛异常即通过
        self.assertIsNone(self.instance._handle)  # noqa: SLF001

    def test_subprocess_is_pointed_at_the_default_project_models_folder(self):
        """BC-17：没配 models_dir 时，本机OCR服务去项目内的 models/ 找/下模型。"""
        from core.paths import models_dir

        env = self.instance._handle._env  # noqa: SLF001
        self.assertEqual(env["HF_HUB_CACHE"], str(models_dir("")))
        self.assertEqual(Path(env["HF_HUB_CACHE"]).name, "models")
        self.assertIn("PATH", env, "子进程必须继承当前环境，丢 PATH 会直接起不来")

    def test_subprocess_uses_the_configured_models_dir_after_restart(self):
        """用户在设置页改了模型路径：本机OCR服务下次（重新）启动时用新路径。"""
        target = self.tmp / "my-models"
        self.rt.settings.set("models_dir", str(target))
        self.instance._stop_handle()  # noqa: SLF001
        self.instance._start_handle()  # noqa: SLF001
        self.assertEqual(self.instance._handle._env["HF_HUB_CACHE"], str(target))  # noqa: SLF001
        self.assertTrue(self.instance._handle.is_alive)  # noqa: SLF001

    def test_extract_after_disable_folds_to_failure_not_crash(self):
        self.rt.disable("official-ocr-mineru-local")
        doc = self.instance.extract("lib1", "whatever.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "deferred")

    def test_mineru_local_timeout_scales_with_pages(self):
        f = self.plugin_module._mineru_local_timeout
        self.assertEqual(f(None), 300.0)
        self.assertEqual(f(0), 300.0)
        self.assertEqual(f(1), 330.0)
        self.assertEqual(f(200), 300.0 + 30.0 * 200)

    def test_resolve_mineru_python_short_circuits_to_core_interpreter_when_faking(self):
        # RAG_REDO_FAKE_OCR=1 已经在 setUp 里设了——不管有没有装真实 MinerU
        # 工具环境，都不该去探测/要求它，测试机器不该被强制装几个GB的依赖。
        self.assertEqual(self.plugin_module._resolve_mineru_python(), sys.executable)


class TestMineruLocalLogLocation(unittest.TestCase):
    """缺陷 D 的复现组：子进程日志原来写在**插件源码目录**下
    （`official_ocr_mineru_local/data/mineru_local_server.log`，且 mkdir 没有
    try 保护——插件装在只读目录时子进程在 import 阶段就死）。这既违反
    "所有数据落在 data/ 目录"（主程序 DATA_ROOT 锚定在
    %LOCALAPPDATA%\\RAG-Redo\\data，见 gui_main.py:38-45 / mcp_stdio.py:39-45），
    又让卸载便携包会连带删掉诊断日志（实测那份日志已累计 4.6MB 并被
    installer/build_windows.py 打进便携包）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env_backup = os.environ.get("RAG_REDO_FAKE_OCR")
        os.environ["RAG_REDO_FAKE_OCR"] = "1"
        self.addCleanup(self._restore_env)
        self.data_dir = self.tmp / "data"
        self.rt = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.data_dir,
        )
        self.rt.scan()
        self.rt.settings.set("pdf_scan_backend", "mineru-local")
        self.rt.load("official-ocr-mineru-local")
        self.rt.enable("official-ocr-mineru-local")
        self.instance = self.rt.plugins["official-ocr-mineru-local"].instance
        self.addCleanup(lambda: self.rt.disable("official-ocr-mineru-local"))

    def _restore_env(self) -> None:
        if self._env_backup is None:
            os.environ.pop("RAG_REDO_FAKE_OCR", None)
        else:
            os.environ["RAG_REDO_FAKE_OCR"] = self._env_backup

    def test_log_file_lives_under_the_runtime_data_root(self):
        log_file = self.instance.log_file()
        self.assertTrue(log_file, "插件必须能报出子进程日志路径（对齐 LEGACY gpu_arbiter.py:273-283）")
        self.assertTrue(
            log_file.startswith(str(self.data_dir)),
            f"日志必须落在 DATA_ROOT 之下，实际落在 {log_file}",
        )

    def test_subprocess_output_actually_lands_in_that_log_file(self):
        log_file = Path(self.instance.log_file())
        self.assertTrue(log_file.is_file(), f"子进程启动后日志文件应已创建：{log_file}")
        content = log_file.read_text(encoding="utf-8", errors="replace")
        self.assertIn("mineru-local", content)

    def test_plugin_source_directory_never_gets_a_data_dir(self):
        """真实跑一轮 extract 之后，插件源码目录底下不能多出 data/——
        之前子进程一启动就 mkdir 一个，日志还跟着进了便携包。"""
        (self.tmp / "scan.pdf").write_bytes(b"%PDF-fake-scanned-content")
        self.instance.extract("lib1", "scan.pdf", self.tmp)
        self.assertFalse(
            (REPO_ROOT / "plugins" / "official-ocr-mineru-local" / "official_ocr_mineru_local" / "data").exists()
        )


class TestResolveMineruPythonWithoutFaking(unittest.TestCase):
    """不经过 PluginRuntime，直接测 `_resolve_mineru_python` 在非测试模式下
    的探测/覆盖逻辑——真实按 obsidian-rag/gpu_arbiter.py 同名函数的探测
    路径走一遍，不是纸面设计审查。"""

    def setUp(self) -> None:
        plugin_dir = REPO_ROOT / "plugins" / "official-ocr-mineru-local"
        if str(plugin_dir) not in sys.path:
            sys.path.insert(0, str(plugin_dir))
        import official_ocr_mineru_local.plugin as ocr_plugin_module  # noqa: PLC0415

        self.mod = ocr_plugin_module
        self._fake_backup = os.environ.pop("RAG_REDO_FAKE_OCR", None)
        self._override_backup = os.environ.pop("RAG_REDO_MINERU_PYTHON", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._fake_backup is not None:
            os.environ["RAG_REDO_FAKE_OCR"] = self._fake_backup
        if self._override_backup is not None:
            os.environ["RAG_REDO_MINERU_PYTHON"] = self._override_backup

    def test_explicit_env_override_wins_when_file_exists(self):
        fd, path = tempfile.mkstemp(suffix=".exe")
        os.close(fd)
        fake_python = Path(path)
        self.addCleanup(lambda: fake_python.unlink(missing_ok=True))
        os.environ["RAG_REDO_MINERU_PYTHON"] = str(fake_python)
        self.assertEqual(self.mod._resolve_mineru_python(), str(fake_python))

    def test_nonexistent_override_falls_through_to_autodetect(self):
        missing = Path(tempfile.gettempdir()) / "rag-redo-test-does-not-exist" / "python.exe"
        os.environ["RAG_REDO_MINERU_PYTHON"] = str(missing)
        result = self.mod._resolve_mineru_python()
        # 探测不到就该是 None（本机真装了 MinerU 时会探测到真实路径，两种
        # 结果都合法——这里只断言"不是那个不存在的覆盖路径"）。
        self.assertNotEqual(result, str(missing))

    def test_autodetect_finds_uv_tool_install_when_present(self):
        appdata = os.environ.get("APPDATA")
        if not appdata:
            self.skipTest("非 Windows 或 APPDATA 未设置，跳过 uv tool 落点探测")
        expected = Path(appdata) / "uv" / "tools" / "mineru" / "Scripts" / "python.exe"
        if not expected.is_file():
            self.skipTest("本机未安装 MinerU tool 环境（uv tool install mineru），跳过")
        self.assertEqual(self.mod._resolve_mineru_python(), str(expected))

    def test_settings_mineru_python_used_when_no_explicit_or_env_override(self):
        """对齐 obsidian-rag/config.py 的 mineru_python 设置项（2026-09-23
        接入 core/settings.py 通用设置存储后补齐）——没有更高优先级的
        显式参数/环境变量覆盖时，读设置里存的解释器路径。"""
        from core.settings import SettingsStore

        fd, path = tempfile.mkstemp(suffix=".exe")
        os.close(fd)
        fake_python = Path(path)
        self.addCleanup(lambda: fake_python.unlink(missing_ok=True))
        settings = SettingsStore(Path(tempfile.mkdtemp()) / "settings.json")
        settings.set("mineru_python", str(fake_python))
        self.assertEqual(self.mod._resolve_mineru_python(settings=settings), str(fake_python))

    def test_env_var_override_wins_over_settings(self):
        from core.settings import SettingsStore

        fd1, settings_path = tempfile.mkstemp(suffix=".exe")
        os.close(fd1)
        fd2, env_path = tempfile.mkstemp(suffix=".exe")
        os.close(fd2)
        self.addCleanup(lambda: Path(settings_path).unlink(missing_ok=True))
        self.addCleanup(lambda: Path(env_path).unlink(missing_ok=True))
        settings = SettingsStore(Path(tempfile.mkdtemp()) / "settings.json")
        settings.set("mineru_python", settings_path)
        os.environ["RAG_REDO_MINERU_PYTHON"] = env_path
        self.assertEqual(self.mod._resolve_mineru_python(settings=settings), env_path)


class TestMineruLocalServerAnswersWhileBusy(unittest.TestCase):
    """2026-09-29 真机复现：本机 MinerU 服务是单线程 HTTPServer，且 /health 里现场探测显存
    （首次要 import torch）。宿主只给启动检查 10 秒、每次探测 1 秒——宿主机 CPU 被打满时
    检查超时，插件“启用失败”，整轮扫描件因此被延后（Y2S1 库 47 个）。旧项目
    obsidian-rag/mineru_server.py 是 ThreadingHTTPServer，/health 不做重活。

    这里直接在进程内起服务端模块（不经子进程），验证两条：①解析卡着时 /health 仍秒回；
    ②/health 不在请求线程里做显存探测。"""

    def setUp(self) -> None:
        import importlib.util
        import threading
        import urllib.request  # noqa: F401  (确保子模块已加载)

        spec = importlib.util.spec_from_file_location(
            "mineru_local_server_under_test",
            REPO_ROOT / "plugins" / "official-ocr-mineru-local" / "official_ocr_mineru_local" / "server.py",
        )
        assert spec is not None and spec.loader is not None
        self.server_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server_mod)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env_backup = os.environ.get("RAG_REDO_FAKE_OCR")
        os.environ["RAG_REDO_FAKE_OCR"] = "1"
        self.addCleanup(self._restore_env)
        self.httpd = self.server_mod.make_server(0)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._stop_server)

    def _restore_env(self) -> None:
        if self._env_backup is None:
            os.environ.pop("RAG_REDO_FAKE_OCR", None)
        else:
            os.environ["RAG_REDO_FAKE_OCR"] = self._env_backup

    def _stop_server(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)

    def _health(self, timeout: float = 1.0) -> tuple[int, float]:
        import time
        import urllib.request

        started = time.monotonic()
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=timeout) as resp:
            resp.read()
            return resp.status, time.monotonic() - started

    def test_server_is_multithreaded(self):
        import http.server

        self.assertIsInstance(self.httpd, http.server.ThreadingHTTPServer)

    def test_health_answers_within_a_second_while_an_extract_is_stuck(self):
        import json
        import threading
        import urllib.request
        from unittest import mock

        entered = threading.Event()
        release = threading.Event()

        def _stuck_ocr(_path):
            entered.set()
            release.wait(timeout=30)
            return "ok"

        pdf = self.tmp / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        result: dict = {}

        def _post() -> None:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/extract",
                data=json.dumps({"path": "a.pdf", "root": str(self.tmp)}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=40) as resp:
                result["body"] = json.loads(resp.read())

        with mock.patch.object(self.server_mod, "_fake_ocr", _stuck_ocr):
            poster = threading.Thread(target=_post, daemon=True)
            poster.start()
            self.assertTrue(entered.wait(timeout=10), "解析请求应该已经进到卡住的假 OCR 里")
            try:
                status, elapsed = self._health(timeout=1.0)
            finally:
                release.set()
            poster.join(timeout=10)
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result["body"]["text"], "ok")

    def test_health_never_runs_the_vram_probe_on_the_request_thread(self):
        import threading
        import time
        from unittest import mock

        probe_threads: list[str] = []

        def _slow_probe(max_age: float = 5.0):
            probe_threads.append(threading.current_thread().name)
            time.sleep(2.0)  # 老实现里这 2 秒（真实是 import torch）就压在 /health 的请求上
            self.server_mod._vram_cache = (6.5, time.time())
            return 6.5

        self.server_mod._vram_cache = (None, 0.0)
        with mock.patch.object(self.server_mod, "_vram_free_gb", _slow_probe):
            status, elapsed = self._health(timeout=1.0)
            self.assertEqual(status, 200)
            self.assertLess(elapsed, 1.0)
            deadline = time.monotonic() + 10
            while not probe_threads and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(probe_threads, ["mineru-vram-probe"])
            # 等后台探测跑完，避免线程漏到别的用例里
            while self.server_mod._vram_refreshing.locked() and time.monotonic() < deadline:
                time.sleep(0.05)


class TestServerExitsWhenItsHostIsGone(unittest.TestCase):
    """本机 MinerU 服务盯着宿主进程：宿主异常没了（崩溃/被“结束任务”/被强杀）来不及走 stop() 时，
    Windows 上子进程不会跟着走，会一直占着显存直到半小时后的空闲自退出。2026-09-29 实测抓到
    过一个这样的孤儿（宿主没了 29 分钟它还活着），也是操作者反馈“关掉 GUI 之后显存没有及时
    释放、进程没有关闭”的一条可能机制。宿主通过环境变量 RAG_REDO_PARENT_PID 告诉子进程自己的 pid。"""

    def setUp(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "mineru_local_parent_watch_under_test",
            REPO_ROOT / "plugins" / "official-ocr-mineru-local" / "official_ocr_mineru_local" / "server.py",
        )
        assert spec is not None and spec.loader is not None
        self.server_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server_mod)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def _finished_pid() -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_pid_alive_tells_a_live_process_from_a_finished_one(self) -> None:
        self.assertTrue(self.server_mod._pid_alive(os.getpid()))
        self.assertFalse(self.server_mod._pid_alive(self._finished_pid()))
        self.assertFalse(self.server_mod._pid_alive(0))

    def test_watch_parent_calls_on_gone_when_the_parent_is_already_dead(self) -> None:
        called: list[int] = []
        self.server_mod._watch_parent(self._finished_pid(), interval=0.01, on_gone=lambda: called.append(1))
        self.assertEqual(called, [1])

    def test_watch_parent_keeps_waiting_while_the_parent_lives(self) -> None:
        class _Stop(Exception):
            pass

        polls: list[float] = []
        called: list[int] = []

        def _sleep(seconds: float) -> None:
            polls.append(seconds)
            if len(polls) >= 3:
                raise _Stop

        with self.assertRaises(_Stop):
            self.server_mod._watch_parent(os.getpid(), interval=0.5, on_gone=lambda: called.append(1), sleep=_sleep)
        self.assertEqual(polls, [0.5, 0.5, 0.5])
        self.assertEqual(called, [])

    def test_without_a_parent_pid_the_watch_is_off_and_returns_at_once(self) -> None:
        from unittest import mock

        for value in (None, "", "not-a-number", "0"):
            env = {k: v for k, v in os.environ.items() if k != "RAG_REDO_PARENT_PID"}
            if value is not None:
                env["RAG_REDO_PARENT_PID"] = value
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(self.server_mod, "_watch_parent", side_effect=AssertionError("must not watch")):
                    self.server_mod._parent_watch_daemon()

    @unittest.skipUnless(sys.platform == "win32" or hasattr(os, "killpg"), "需要真实进程语义")
    def test_orphaned_server_really_exits_after_its_host_is_killed(self) -> None:
        """端到端：宿主拉起真实服务子进程后被硬杀（没机会 stop），服务必须自己退出。"""
        try:
            import psutil
        except ImportError:
            self.skipTest("需要 psutil 枚举子进程树")
        helper = self.tmp / "host.py"
        helper.write_text(
            "\n".join(
                [
                    "import os, sys, time",
                    "sys.path.insert(0, r'%s')" % REPO_ROOT,
                    "from core.subprocess_service import SubprocessServiceHandle",
                    "os.environ['RAG_REDO_FAKE_OCR'] = '1'",
                    "handle = SubprocessServiceHandle(",
                    "    [sys.executable, 'server.py', '--port', '{port}'],",
                    "    health_check='http://127.0.0.1:{port}/health',",
                    "    cwd=r'%s',"
                    % (REPO_ROOT / "plugins" / "official-ocr-mineru-local" / "official_ocr_mineru_local"),
                    "    log_path=r'%s'," % (self.tmp / "server.log"),
                    "    startup_timeout=60.0,",
                    ")",
                    "handle.start()",
                    "print('CHILD', handle._process.pid, flush=True)",
                    "time.sleep(600)",
                ]
            ),
            encoding="utf-8",
        )
        host = subprocess.Popen([sys.executable, str(helper)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: host.poll() is None and host.kill())
        line = host.stdout.readline().strip()
        self.assertTrue(line.startswith("CHILD"), line)
        child = psutil.Process(int(line.split()[1]))
        tree = [child] + child.children(recursive=True)
        self.addCleanup(lambda: [p.kill() for p in tree if p.is_running()])
        host.kill()  # TerminateProcess：宿主没有任何机会收口
        host.wait(timeout=10)
        gone, alive = psutil.wait_procs(tree, timeout=30)
        self.assertEqual([p.pid for p in alive], [], "宿主没了之后服务进程树必须自己退出")


if __name__ == "__main__":
    unittest.main()
