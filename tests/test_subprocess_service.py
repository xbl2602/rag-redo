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

from core.singleton import pid_alive
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

# 往 stdout 狂写 300KB 的假子进程：Windows 匿名管道缓冲约 64KB（旧项目
# gpu_arbiter.py:241-252 与 rag-redo 重构前 subprocess_service.py:221-228 都
# 踩过"没人排空的 PIPE 写满后子进程永久阻塞在 write 上"这个坑），用来证明
# 重定向到真实日志文件之后子进程不会被写死、内容也会完整落盘。
_FLOODS_STDOUT = '''
import sys
chunk = "x" * 4096 + "\\n"
for _ in range(80):  # 80 * ~4KB = 320KB，远超任何管道的默认缓冲
    sys.stdout.write(chunk)
    sys.stdout.flush()
sys.stdout.write("FLOOD_DONE\\n")
sys.stdout.flush()
import http.server, json, time
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        body = json.dumps({"echo": payload}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
port = int(sys.argv[sys.argv.index("--port") + 1])
http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def _process_is_gone(pid: int) -> bool:
    """"这个 pid 是不是真的没了"：直接问 `core/singleton.py::pid_alive`（看进程是不是已经
    结束），测试里不另写一份判断（AGENTS.md §4.5、§7）。

    以前这里各自写成“OpenProcess 打得开就算还活着”，在 Windows 上判不准：进程被杀掉之后，
    只要别处还有人握着它的句柄，这个进程对象就还在、照样打得开，要过零点几秒才真正消失。
    2026-10-01 在整套回归里抓到过：`stop()` 之后立刻查，退出码已经是 1（被 taskkill 杀掉），
    却仍被判“还活着”，1 秒后再查就没了——“停止子进程”那条测试时好时坏就是这个原因。"""
    return not pid_alive(pid)


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


class TestSubprocessStdioRedirection(unittest.TestCase):
    """缺陷 A 的复现组：子进程 stdout/stderr 原来用 `subprocess.PIPE` 交给
    核心进程，而核心只在"启动即早夭"那一个分支读一次 stderr，之后再无任何
    读取方。两个真实后果（对齐 LEGACY obsidian-rag/gpu_arbiter.py:241-252
    把子进程 stdout/stderr 直接指向 `data/wemm_server.log` 真实文件的既有
    做法、以及同仓 official-ocr-mineru-local 在真机调试时抓到的内服务 64KB
    管道写满死锁）：

    ① 诊断黑洞：WEMM 加载模型失败、端口冲突、`socketserver.handle_error`
       的 traceback 全进一个没人读的管道，永久丢失；
    ② 永久僵死：管道缓冲写满后子进程任何一次 print/traceback 都阻塞在写
       fd 上，进程还活着、is_alive 仍为 True，但服务永远起不来。

    修法是重定向到真实日志文件（LEGACY 的做法），并且在没有指定日志文件
    时也必须有持续排空方，绝不能"只读一次"。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.server_script = self.tmp / "echo_server.py"
        self.server_script.write_text(_ECHO_SERVER, encoding="utf-8")
        self.flood_script = self.tmp / "flood.py"
        self.flood_script.write_text(_FLOODS_STDOUT, encoding="utf-8")
        self.log_path = self.tmp / "logs" / "service.log"
        self._handles: list[SubprocessServiceHandle] = []
        self.addCleanup(self._stop_all)

    def _stop_all(self) -> None:
        for handle in self._handles:
            handle.stop()

    def _echo_handle(self, **kwargs) -> SubprocessServiceHandle:
        handle = SubprocessServiceHandle(
            (sys.executable, str(self.server_script), "--port", "{port}"),
            health_check="http://127.0.0.1:{port}/health",
            **kwargs,
        )
        self._handles.append(handle)
        return handle

    def _flood_handle(self, **kwargs) -> SubprocessServiceHandle:
        handle = SubprocessServiceHandle(
            (sys.executable, str(self.flood_script), "--port", "{port}"),
            health_check="http://127.0.0.1:{port}/health",
            startup_timeout=20.0,
            **kwargs,
        )
        self._handles.append(handle)
        return handle

    def test_flooding_child_does_not_deadlock_and_output_lands_in_logfile(self):
        handle = self._flood_handle(log_path=self.log_path)
        handle.start()
        # 子进程在起 HTTP 服务之前先往 stdout 写满 320KB：如果这条 fd 还是
        # 那个"没人排空、缓冲约 64KB"的管道，它会永久阻塞在 write 上，
        # /health 永远不响，start() 这里就会抛"没有通过 health_check"。
        self.assertTrue(handle.is_alive)
        content = self.log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("FLOOD_DONE", content)
        self.assertGreater(len(content), 300 * 1024)

    def test_flooding_child_does_not_deadlock_even_when_no_logfile_is_given(self):
        """没给 log_path 的调用方也必须有持续排空方——PIPE 本身可以留（有人
        读就不存在写满死锁），但不能"只在早夭时读一次"。"""
        handle = self._flood_handle()
        handle.start()
        self.assertTrue(handle.is_alive)
        self.assertEqual(handle.call("echo", {"who": "flood"})["echo"], {"who": "flood"})

    def test_early_exit_reason_comes_from_the_logfile_not_only_memory(self):
        broken = self.tmp / "broken.py"
        broken.write_text(_EXITS_IMMEDIATELY, encoding="utf-8")
        handle = SubprocessServiceHandle(
            (sys.executable, str(broken)),
            health_check="http://127.0.0.1:{port}/health",
            startup_timeout=5.0,
            log_path=self.log_path,
        )
        self._handles.append(handle)
        with self.assertRaises(SubprocessServiceError) as ctx:
            handle.start()
        self.assertIn("boom", str(ctx.exception))
        # 关键：崩溃原因落进了可被用户/AI 事后翻查的日志文件，而不是只留在
        # 一个已经随异常丢掉的内核对象里。
        self.assertIn("boom", self.log_path.read_text(encoding="utf-8", errors="replace"))
        self.assertFalse(handle.is_alive)

    def test_early_exit_reason_is_captured_even_without_logfile(self):
        broken = self.tmp / "broken_nolog.py"
        broken.write_text(_EXITS_IMMEDIATELY, encoding="utf-8")
        handle = SubprocessServiceHandle(
            (sys.executable, str(broken)),
            health_check="http://127.0.0.1:{port}/health",
            startup_timeout=5.0,
        )
        self._handles.append(handle)
        with self.assertRaises(SubprocessServiceError) as ctx:
            handle.start()
        self.assertIn("boom", str(ctx.exception))

    def test_unwritable_log_path_degrades_instead_of_preventing_startup(self):
        """写不了日志绝不能让子进程起不来（插件装在只读目录、或 data 根不可
        写都会触发）——降级成"不落文件"继续跑。"""
        blocker = self.tmp / "blocker"
        blocker.write_text("这是一个文件，不是目录", encoding="utf-8")
        handle = self._echo_handle(log_path=blocker / "logs" / "service.log")
        handle.start()
        self.assertTrue(handle.is_alive)
        self.assertEqual(handle.call("echo", {"ok": 1})["echo"], {"ok": 1})

    def test_stop_closes_the_log_file_handle(self):
        """架构红线 §7"任何子进程/文件句柄都必须有停止、等待和异常收口路径"：
        stop() 之后日志文件必须可以被删掉——Windows 上仍被打开的文件删不掉，
        这条断言在 Windows 上才是真断言。"""
        handle = self._echo_handle(log_path=self.log_path)
        handle.start()
        self.assertTrue(self.log_path.is_file())
        handle.stop()
        self.log_path.unlink()
        self.assertFalse(self.log_path.exists())

    def test_log_file_property_exposes_the_path_for_diagnostics(self):
        """日志路径要能被查询到（对齐 LEGACY obsidian-rag/server.py:965 的
        `wemm_status` 明确把 `data/wemm_server.log` 指给用户/AI）。"""
        handle = self._echo_handle(log_path=self.log_path)
        self.assertEqual(handle.log_file, str(self.log_path))
        self.assertIsNone(SubprocessServiceHandle((sys.executable, "-c", "pass")).log_file)


class TestChildKnowsItsParent(unittest.TestCase):
    """宿主把自己的 pid 通过 RAG_REDO_PARENT_PID 告诉子进程，子进程据此在宿主异常没了时自退出
    （2026-09-29：实测抓到过一个宿主没了 29 分钟还活着的 WEMM 服务孤儿）。"""

    _SCRIPT = "import os; print('PARENT=' + os.environ.get('RAG_REDO_PARENT_PID', '') + ' FOO=' + os.environ.get('FOO', ''))"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _child_output(self, env=None) -> str:
        import time

        log = self.tmp / "child.log"
        handle = SubprocessServiceHandle([sys.executable, "-c", self._SCRIPT], log_path=log, env=env)
        handle.start()
        deadline = time.monotonic() + 15
        text = ""
        while time.monotonic() < deadline:
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if "PARENT=" in text:
                break
            time.sleep(0.05)
        handle.stop()
        return text

    def test_child_is_told_the_hosts_pid(self):
        self.assertIn(f"PARENT={os.getpid()} ", self._child_output())

    def test_callers_env_is_kept_and_still_gets_the_parent_pid(self):
        text = self._child_output(env=dict(os.environ, FOO="bar"))
        self.assertIn(f"PARENT={os.getpid()} FOO=bar", text)

    def test_default_env_is_still_inherited(self):
        with patch.dict(os.environ, {"FOO": "inherited"}):
            text = self._child_output()
        self.assertIn(f"PARENT={os.getpid()} FOO=inherited", text)

    def test_callers_env_dict_is_not_modified(self):
        env = dict(os.environ, FOO="bar")
        before = dict(env)
        self._child_output(env=env)
        self.assertEqual(env, before)


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

    def test_frozen_build_uses_packaged_portable_python_for_bootstrap(self):
        from core import subprocess_service

        app = self.tmp / "app"
        portable = app / "runtime" / "python" / "python.exe"
        portable.parent.mkdir(parents=True)
        portable.write_text("portable", encoding="utf-8")
        script = self.tmp / "env_bootstrap.py"
        script.write_text("pass\n", encoding="utf-8")
        created = self._venv_python_path()

        def fake_run(command, **kwargs):
            created.parent.mkdir(parents=True, exist_ok=True)
            created.write_text("created", encoding="utf-8")
            return subprocess_service.subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(subprocess_service.sys, "frozen", True, create=True):
            with patch.object(subprocess_service.sys, "executable", str(app / "gui" / "rag-redo.exe")):
                with patch.dict(os.environ, {}, clear=True):
                    with patch.object(subprocess_service.subprocess, "run", side_effect=fake_run) as run:
                        result = subprocess_service.resolve_plugin_python(self.tmp, env_bootstrap="env_bootstrap.py")
        self.assertEqual(result, str(created))
        self.assertEqual(run.call_args.args[0][0], str(portable))

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


class TestNoConsoleWindowOnWindows(unittest.TestCase):
    """2026-09-29 真实反馈：桌面双击 GUI 后先黑屏几秒才出界面，之后每隔几秒
    还会再闪一下——根因是本模块三处 `subprocess.Popen`/`subprocess.run` 调用
    都没带 `creationflags=CREATE_NO_WINDOW`：宿主是 pythonw.exe/冻结 GUI exe
    时没有控制台，Windows 会给子进程现开一个、用完即关，看起来就是"黑色
    弹窗一闪"。

    这里特意对 `subprocess` 打 mock（本文件其余测试刻意不这么做，见模块
    docstring）——"Windows 有没有弹出一个控制台窗口"这件事本身没有可移植、
    可在无头测试环境里断言的观测点，唯一能钉住的是"调用点确实传了这个
    flag"，所以只对这一条窗口相关的编排逻辑做例外。
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_start_passes_creationflags_to_popen(self):
        import core.subprocess_service as svc

        with patch.object(svc, "subprocess") as fake_sp:
            fake_sp.PIPE = "PIPE"
            fake_sp.Popen.return_value.stdout = None  # 让 _start_drain 干净地 no-op
            fake_sp.Popen.return_value.stderr = None
            handle = SubprocessServiceHandle((sys.executable, "-c", "pass"), cwd=self.tmp)
            handle.start()
        self.assertIs(
            fake_sp.Popen.call_args.kwargs["creationflags"],
            fake_sp.CREATE_NO_WINDOW,
        )

    def test_kill_process_tree_taskkill_passes_creationflags(self):
        import core.subprocess_service as svc

        handle = SubprocessServiceHandle((sys.executable, "-c", "pass"), cwd=self.tmp)
        handle.start()
        self.addCleanup(handle.stop)
        with patch.object(svc, "subprocess") as fake_sp, patch.object(sys, "platform", "win32"):
            fake_sp.run.return_value = None
            fake_sp.TimeoutExpired = Exception
            handle._kill_process_tree(grace_period=1.0)
        self.assertIs(
            fake_sp.run.call_args.kwargs["creationflags"],
            fake_sp.CREATE_NO_WINDOW,
        )

    def test_env_bootstrap_run_passes_creationflags(self):
        import core.subprocess_service as svc

        script = self.tmp / "env_bootstrap.py"
        script.write_text("pass\n", encoding="utf-8")
        with patch.object(svc, "subprocess") as fake_sp:
            fake_sp.run.return_value.returncode = 0
            svc._run_env_bootstrap(self.tmp, "env_bootstrap.py", timeout=30.0, logger=_FakeLogger())
        self.assertIs(
            fake_sp.run.call_args.kwargs["creationflags"],
            fake_sp.CREATE_NO_WINDOW,
        )


if __name__ == "__main__":
    unittest.main()
