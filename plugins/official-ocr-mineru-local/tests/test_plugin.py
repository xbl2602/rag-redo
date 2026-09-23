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

        self.rt = PluginRuntime(REPO_ROOT / "plugins", state_file=self.tmp / "plugins_state.json")
        self.rt.scan()
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

    def test_enable_acquires_gpu_lease(self):
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

    def test_extract_after_disable_folds_to_failure_not_crash(self):
        self.rt.disable("official-ocr-mineru-local")
        doc = self.instance.extract("lib1", "whatever.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIn("未运行", doc.failure_reason)

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


if __name__ == "__main__":
    unittest.main()
