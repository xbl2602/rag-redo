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


if __name__ == "__main__":
    unittest.main()
