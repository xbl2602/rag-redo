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
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


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
        self._process = subprocess.Popen(
            self._command,
            cwd=str(self._cwd) if self._cwd else None,
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
        """不留游离进程（架构红线6）：先礼后兵——terminate() 给子进程
        机会自己清理，grace_period 内没退出再 kill()，最后 wait() 确认
        真的没了，不是发了信号就假装完事。真实踩过的坑：只 wait() 进程
        退出不够，Popen(stdout=PIPE, stderr=PIPE) 打开的管道文件描述符
        不会因为子进程退出就自动关闭，得手动 close()，不然每 start/stop
        一轮就泄漏两个文件描述符（测试里用 ResourceWarning 抓到的）。"""
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=grace_period)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=grace_period)
        if self._process.stdout is not None:
            self._process.stdout.close()
        if self._process.stderr is not None:
            self._process.stderr.close()
        self._process = None
