"""core.subprocess_service 的真实子进程测试：真的 Popen 一个 Python 子
进程、真的发 HTTP 请求、真的确认进程被杀掉——不是对 subprocess.Popen 打
mock。测试用的"回声服务器"故意只用标准库 http.server（不装任何第三方
依赖），这样这份测试本身不需要网络/模型下载就能跑，同时也如实反映了
真正的 subprocess_service 插件（比如 official-ocr-mineru-local）子进程
那一侧会长什么样子。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.subprocess_service import SubprocessServiceError, SubprocessServiceHandle, find_free_port

_ECHO_SERVER = '''
import http.server, json, sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self._json(200, {"echo": payload, "path": self.path})

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

port = int(sys.argv[sys.argv.index("--port") + 1])
srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
srv.serve_forever()
'''

_EXITS_IMMEDIATELY = "import sys; sys.stderr.write('boom\\n'); sys.exit(1)"


def _process_is_gone(pid: int) -> bool:
    """跨平台的"这个 pid 是不是真的没了"检查，理由同 test_runtime.py 里
    同名函数——POSIX 的 os.kill(pid, 0) 信号-0 探测语义在 Windows 上不
    成立（直接抛 OSError 而不是 ProcessLookupError），得走 Win32
    OpenProcess API。"""
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


class TestSubprocessServiceHandle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.server_script = self.tmp / "echo_server.py"
        self.server_script.write_text(_ECHO_SERVER, encoding="utf-8")
        self._handles: list[SubprocessServiceHandle] = []

    def tearDown(self) -> None:
        for h in self._handles:
            h.stop()

    def _make_handle(self, **kwargs) -> SubprocessServiceHandle:
        handle = SubprocessServiceHandle(
            (sys.executable, str(self.server_script), "--port", "{port}"),
            health_check="http://127.0.0.1:{port}/health",
            **kwargs,
        )
        self._handles.append(handle)
        return handle

    def test_start_then_call_gets_real_response_from_subprocess(self):
        handle = self._make_handle()
        handle.start()
        self.assertTrue(handle.is_alive)

        result = handle.call("echo", {"library_id": "lib1", "x": 1})
        self.assertEqual(result["path"], "/echo")
        self.assertEqual(result["echo"], {"library_id": "lib1", "x": 1})

    def test_stop_actually_terminates_the_process_not_just_marks_it_gone(self):
        handle = self._make_handle()
        handle.start()
        pid = handle._process.pid  # noqa: SLF001 - 测试需要直接确认操作系统层面的进程状态
        self.assertTrue(handle.is_alive)

        handle.stop()
        self.assertFalse(handle.is_alive)
        # 不只是我们自己的 Popen 对象说"没了"——真的问操作系统这个 pid 还在不在，
        # 对应架构红线6"不产生游离进程"，这是这条红线唯一靠得住的验证方式。
        self.assertTrue(_process_is_gone(pid))

    def test_stop_is_idempotent(self):
        handle = self._make_handle()
        handle.start()
        handle.stop()
        handle.stop()  # 不应该抛异常

    def test_call_after_stop_raises_not_hangs(self):
        handle = self._make_handle()
        handle.start()
        handle.stop()
        with self.assertRaises(SubprocessServiceError):
            handle.call("echo", {})

    def test_two_handles_get_different_ports_and_both_work(self):
        h1 = self._make_handle()
        h2 = self._make_handle()
        h1.start()
        h2.start()
        self.assertNotEqual(h1.port, h2.port)
        self.assertEqual(h1.call("echo", {"who": "h1"})["echo"], {"who": "h1"})
        self.assertEqual(h2.call("echo", {"who": "h2"})["echo"], {"who": "h2"})

    def test_process_that_exits_immediately_raises_clear_error_not_hang(self):
        broken_script = self.tmp / "broken.py"
        broken_script.write_text(_EXITS_IMMEDIATELY, encoding="utf-8")
        handle = SubprocessServiceHandle(
            (sys.executable, str(broken_script)),
            health_check="http://127.0.0.1:{port}/health",
            startup_timeout=5.0,
        )
        self._handles.append(handle)
        with self.assertRaises(SubprocessServiceError) as ctx:
            handle.start()
        self.assertIn("boom", str(ctx.exception))
        self.assertFalse(handle.is_alive)

    def test_no_health_check_means_start_returns_immediately(self):
        """有些 subprocess_service 插件可能不声明 health_check（不常见，
        但 manifest schema 允许它是 None）——start() 不该傻等一个不存在的
        探测端点，直接返回，调用方自己保证第一次 call() 之前子进程已经
        准备好。"""
        handle = SubprocessServiceHandle((sys.executable, str(self.server_script), "--port", "{port}"))
        self._handles.append(handle)
        handle.start()
        self.assertTrue(handle.is_alive)


class TestFindFreePort(unittest.TestCase):
    def test_returns_distinct_ports(self):
        ports = {find_free_port() for _ in range(5)}
        self.assertEqual(len(ports), 5)


class _FakeLogger:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, msg, *args):
        self.infos.append(msg % args if args else msg)

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)


class TestResolvePluginPythonEnvBootstrap(unittest.TestCase):
    """env_bootstrap 真正执行的回归测试（2026-09-23 补齐，此前只有
    "已知简化"的占位）。用真实子进程跑一个自包含的临时脚本（不碰真实
    网络/pip，只是脚本自己在约定路径造一个假 python 可执行文件），验证
    的是"核心真的会跑这个脚本、真的按约定路径重新探测"这条编排逻辑本身
    对不对，不是"pip 装依赖装得对不对"——后者是插件自己 env_bootstrap
    脚本内容的责任，不是 core/subprocess_service.py 的责任。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _venv_python_path(self) -> Path:
        if sys.platform == "win32":
            return self.tmp / ".venv" / "Scripts" / "python.exe"
        return self.tmp / ".venv" / "bin" / "python"

    def test_no_venv_no_bootstrap_falls_back_to_core_interpreter(self):
        from core.subprocess_service import resolve_plugin_python

        self.assertEqual(resolve_plugin_python(self.tmp), sys.executable)

    def test_existing_venv_returned_without_running_bootstrap(self):
        from core.subprocess_service import resolve_plugin_python

        venv_python = self._venv_python_path()
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("not a real interpreter, just needs to exist", encoding="utf-8")
        # env_bootstrap 指向一个不存在的脚本——如果真的被跑了会报错，这里
        # 用来证明"已经有独立venv"这条分支绝不会真的去跑脚本（幂等）。
        self.assertEqual(
            resolve_plugin_python(self.tmp, env_bootstrap="does-not-exist.py"), str(venv_python)
        )

    def test_bootstrap_script_runs_and_new_venv_is_picked_up(self):
        """脚本真的执行（真实子进程，不是 mock）、在约定路径造出解释器
        文件后，重新探测应该真的找到它——证明"跑完脚本→重新探测"这条
        编排逻辑是真实生效的，不是纸面设计。"""
        from core.subprocess_service import resolve_plugin_python

        script = self.tmp / "env_bootstrap.py"
        script.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "venv_python = Path(__file__).parent / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')\n"
            "venv_python.parent.mkdir(parents=True, exist_ok=True)\n"
            "venv_python.write_text('fake interpreter created by bootstrap')\n",
            encoding="utf-8",
        )
        logger = _FakeLogger()
        result = resolve_plugin_python(self.tmp, env_bootstrap="env_bootstrap.py", logger=logger, bootstrap_timeout=30.0)
        self.assertEqual(result, str(self._venv_python_path()))
        self.assertTrue(any("首次启用" in msg for msg in logger.infos))

    def test_missing_bootstrap_script_folds_to_core_interpreter_with_warning(self):
        from core.subprocess_service import resolve_plugin_python

        logger = _FakeLogger()
        result = resolve_plugin_python(self.tmp, env_bootstrap="no-such-script.py", logger=logger)
        self.assertEqual(result, sys.executable)
        self.assertTrue(logger.warnings)

    def test_bootstrap_script_nonzero_exit_folds_to_core_interpreter_with_warning(self):
        from core.subprocess_service import resolve_plugin_python

        script = self.tmp / "env_bootstrap.py"
        script.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(1)\n", encoding="utf-8")
        logger = _FakeLogger()
        result = resolve_plugin_python(self.tmp, env_bootstrap="env_bootstrap.py", logger=logger, bootstrap_timeout=30.0)
        self.assertEqual(result, sys.executable)
        self.assertTrue(any("boom" in msg for msg in logger.warnings))

    def test_bootstrap_succeeds_but_no_venv_produced_folds_to_core_interpreter(self):
        """脚本本身"成功"退出（returncode=0）但没有在约定路径生成解释器
        （比如脚本写错了路径）——不该假装找到了什么，老老实实退化。"""
        from core.subprocess_service import resolve_plugin_python

        script = self.tmp / "env_bootstrap.py"
        script.write_text("pass\n", encoding="utf-8")
        logger = _FakeLogger()
        result = resolve_plugin_python(self.tmp, env_bootstrap="env_bootstrap.py", logger=logger, bootstrap_timeout=30.0)
        self.assertEqual(result, sys.executable)
        self.assertTrue(logger.warnings)

    def test_frozen_build_raises_instead_of_falling_back_to_sys_executable(self):
        """严重bug回归测试（2026-09-23 真机打包安装后真实调用时抓到）：
        `sys.executable` 在 PyInstaller 冻结产物里是冻结exe自己，不是
        通用解释器——把它当 command 里的 "{python}" 用会让 exe 把自己
        重新拉起，递归下去是指数级自我复制的进程炸弹（真实观测到几分钟
        内四十多个游离进程）。冻结环境下找不到插件专属venv必须直接
        报错，绝不能静默退化返回 sys.executable。"""
        from core.subprocess_service import SubprocessServiceError, resolve_plugin_python

        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaises(SubprocessServiceError):
                resolve_plugin_python(self.tmp)

    def test_frozen_build_with_existing_venv_still_works_normally(self):
        """冻结环境下如果插件专属venv已经真的建好了（之前手动跑过一次
        env_bootstrap，或者未来打包了便携python自动建好的），照常返回，
        不受这条新增的拒绝逻辑影响——这条只挡"没有真实独立环境、企图
        静默退化"这一种情况。"""
        from core.subprocess_service import resolve_plugin_python

        venv_python = self._venv_python_path()
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("fake interpreter", encoding="utf-8")
        with patch.object(sys, "frozen", True, create=True):
            result = resolve_plugin_python(self.tmp)
        self.assertEqual(result, str(venv_python))

    def test_frozen_build_env_bootstrap_refuses_to_run_and_folds_to_clear_error(self):
        """冻结环境下 env_bootstrap 脚本本身也不该被尝试执行——同样是
        `sys.executable` 不是通用解释器这条坑，即使脚本文件真实存在也
        不该去跑，直接折叠成清楚的失败原因。"""
        from core.subprocess_service import SubprocessServiceError, resolve_plugin_python

        script = self.tmp / "env_bootstrap.py"
        script.write_text("pass\n", encoding="utf-8")
        logger = _FakeLogger()
        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaises(SubprocessServiceError):
                resolve_plugin_python(self.tmp, env_bootstrap="env_bootstrap.py", logger=logger, bootstrap_timeout=30.0)
        self.assertTrue(any("冻结" in msg for msg in logger.warnings))


if __name__ == "__main__":
    unittest.main()
