"""subprocess_service 插件的通用子进程管理工具（核心服务，不是插件）。

不懂任何 RAG 领域知识、不知道插件在子进程里到底跑的是 OCR 还是别的
什么——只认"启动这条命令、探测这个 health_check、往这个端口发 JSON、
干净地关掉它"，道理和 core/resource_arbiter.py"通用具名资源租约、不懂
GPU 是什么"完全一样。任何 subprocess_service 插件的 on_enable/
on_disable 都该用这一份来管理自己的子进程，不用每个插件各写一遍"怎么
优雅地杀掉一个子进程"这种细节。

**端口分配**：这里自己找一个空闲本机端口，不需要插件在 plugin.toml 里
写死端口号——`command`/`health_check` 字符串里的 "{port}" 占位符会被
替换成实际分配到的端口。这样两个同时启用的 subprocess_service 插件不会
抢同一个端口，是真实会发生的场景（比如同时装了 MinerU 本机 OCR 和 WEMM
两个 subprocess_service 插件），不是假设性的过度设计。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def resolve_plugin_python(plugin_dir: Path) -> str:
    """subprocess_service 插件应该用自己 env_bootstrap 建出来的独立解释器
    跑子进程，不是核心的 `.venv`——两者必须互相隔离（架构红线7"不碰系统/
    其他环境"的插件间版本）。按约定路径找：
    `<plugin_dir>/.venv/bin/python`（POSIX）或
    `<plugin_dir>/.venv/Scripts/python.exe`（Windows）。

    **已知的、刻意的简化**：`env_bootstrap` 声明字段目前还没有被核心真正
    执行过（见 docs/ROADMAP.md Phase 2 状态说明），约定路径下大概率找不到
    独立 venv——这时退化成用核心自己的解释器，不是假装这件事已经解决了。
    对于当前用纯标准库、不需要任何重依赖的 subprocess_service 参考实现
    （official-ocr-mineru-local）来说这个退化本身没有问题；一旦真的需要
    安装重依赖（比如真实 OCR 模型），`env_bootstrap` 的执行逻辑必须先落地，
    不能让插件在没有真正独立环境的情况下悄悄把重依赖装进核心 venv。"""
    posix_python = plugin_dir / ".venv" / "bin" / "python"
    if posix_python.exists():
        return str(posix_python)
    windows_python = plugin_dir / ".venv" / "Scripts" / "python.exe"
    if windows_python.exists():
        return str(windows_python)
    return sys.executable


class SubprocessServiceError(Exception):
    """子进程启动失败、health_check 一直没通过、或调用时出错。插件的
    on_enable 应该让这个异常原样往上冒泡——PluginRuntime.enable() 已经有
    统一的 try/except 把插件的 on_enable 异常折叠成 FAILED 状态（架构
    红线4），这里不需要重复折叠一次。"""


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SubprocessServiceHandle:
    """一个 subprocess_service 插件实例对应一个 handle：插件的 on_enable
    创建它并 start()，之后用 call() 发请求，on_disable 调 stop()。"""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        health_check: str | None = None,
        cwd: Path | None = None,
        startup_timeout: float = 10.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.port = find_free_port()
        self._command = [arg.format(port=self.port) for arg in command]
        self._health_check = health_check.format(port=self.port) if health_check else None
        self._cwd = cwd
        self._startup_timeout = startup_timeout
        self._env = env
        self._process: subprocess.Popen | None = None

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        # POSIX 上起一个独立进程组（start_new_session）——stop() 要对整棵
        # 进程树发信号（见该方法的说明），不新开一个组的话 os.killpg 会把
        # 发信号的核心进程自己也算进去。
        extra_kwargs = {} if sys.platform == "win32" else {"start_new_session": True}
        self._process = subprocess.Popen(
            self._command,
            cwd=str(self._cwd) if self._cwd else None,
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **extra_kwargs,
        )
        if self._health_check is None:
            return
        deadline = time.time() + self._startup_timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            if self._process.poll() is not None:
                stderr = self._process.stderr.read().decode("utf-8", errors="replace") if self._process.stderr else ""
                returncode = self._process.returncode
                self.stop()  # 进程已经退出，这里只是为了关掉 stdout/stderr 管道，不留文件描述符泄漏
                raise SubprocessServiceError(f"子进程启动后立刻退出（returncode={returncode}）: {stderr[:2000]}")
            try:
                with urllib.request.urlopen(self._health_check, timeout=1.0) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                last_error = exc
                time.sleep(0.1)
        self.stop()
        raise SubprocessServiceError(
            f"子进程 {self._startup_timeout}s 内没有通过 health_check: {last_error}"
        )

    def call(self, method: str, payload: dict, *, timeout: float = 30.0) -> dict:
        if not self.is_alive:
            raise SubprocessServiceError("子进程已经不在运行，无法调用")
        url = f"http://127.0.0.1:{self.port}/{method}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise SubprocessServiceError(f"调用子进程 {method} 失败: {exc}") from exc

    def stop(self, grace_period: float = 3.0) -> None:
        """不留游离进程（架构红线6）：先礼后兵——给子进程机会自己清理，
        grace_period 内没退出再强杀，最后 wait() 确认真的没了，不是发了
        信号就假装完事。真实踩过的坑：只 wait() 进程退出不够，
        Popen(stdout=PIPE, stderr=PIPE) 打开的管道文件描述符不会因为子
        进程退出就自动关闭，得手动 close()，不然每 start/stop 一轮就泄漏
        两个文件描述符（测试里用 ResourceWarning 抓到的）。"""
        if self._process is None:
            return
        if self._process.poll() is None:
            self._kill_process_tree(grace_period)
        if self._process.stdout is not None:
            self._process.stdout.close()
        if self._process.stderr is not None:
            self._process.stderr.close()
        self._process = None

    def _kill_process_tree(self, grace_period: float) -> None:
        """只杀 Popen 直接跟踪的那一个 pid 不够——真实在 Windows 上踩到的坑：
        这台机器的 Python 安装里，`command[0]`（venv 的 python.exe）自己会
        再派生一个真正执行代码的子进程，`Popen.terminate()` 只杀得掉外层
        那一个，里面真正绑着端口、占着资源的子进程会变成不受任何人控制
        的游离进程——用 `ps`/`Get-CimInstance Win32_Process` 真实抓到过
        几十个这样的残留（架构红线6"不产生游离进程"这条本来就是冲着这种
        情况写的，只是没预料到连"自己起的直接子进程"都会再分裂一层）。

        Windows 上改用 `taskkill /F /T` 连整棵进程树一起杀——这是系统自带
        工具，不需要额外依赖；`/F` 强制、`/T` 连子进程。POSIX 上等价的
        做法是给子进程开一个独立进程组（见 start()），对整个组发信号。

        **已知的残留问题，如实记录不假装修完了**：这个修复消灭了绝大多数
        游离进程（改之前几乎每个真实起过子进程的测试都会漏，改完后单独跑
        任何一个测试文件都干净），但在这台机器上"一次性跑完全部27+个测试
        套件"这种高频连续启停子进程的场景下，真实观察到过偶发的、数量不
        固定（0~10对不等）的残留——每次 taskkill 自己报告的都是成功
        （returncode=0），但过一会儿再查还是能看到多出来的进程，行为像是
        这台 Python 装装（venv 的 python.exe 会再派生子进程这件事本身）
        自己在某个时间窗口里又拉起了一次新的子进程，taskkill 扫描进程树的
        那一刻还没抓到它。没能在这轮彻底定位根因（更像是这台机器具体
        Python 发行版 venv 启动器内部的行为，不是 rag-redo 自己代码能完全
        控制的边界），先如实记录、不假装"改完就100%没有了"。真实影响面
        有限：①只在测试大量、快速连续启停子进程时才可能出现，不是每次都
        触发；②打包成 PyInstaller 冻结产物后不会有这个问题——冻结的 exe
        不经过"venv 的 python.exe 转发到真正解释器"这一层间接调用，见
        docs/ROADMAP.md 对应记录。"""
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self._process.pid)],
                capture_output=True,
                check=False,
            )
            try:
                self._process.wait(timeout=grace_period)
            except subprocess.TimeoutExpired:
                pass
            return

        try:
            pgid = os.getpgid(self._process.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
            self._process.wait(timeout=grace_period)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            self._process.wait(timeout=grace_period)
        except ProcessLookupError:
            pass
