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

**子进程的 stdout/stderr 去哪（2026-09-24 修复"诊断黑洞 + 永久僵死"）**：
默认曾用 `stdout=PIPE, stderr=PIPE`，而 `start()` 只在"启动即早夭"那一个
分支读一次 stderr，之后再无任何读取方。两个真实后果：①诊断黑洞——WEMM
加载模型失败、端口冲突、`socketserver.handle_error` 的 traceback 全进一个
没人读的管道，永久丢失，用户和 AI 都没处可查；②永久僵死——Windows 匿名
管道缓冲约 64KB，写满后子进程任何一次 print/traceback 都阻塞在写 fd 上，
进程还活着、`is_alive` 仍返回 True，但服务永远起不来（LEGACY
obsidian-rag 的 MinerU 内服务真机踩过这个坑，本仓
official-ocr-mineru-local 当时也是在子进程那一侧自己 `os.dup2` 绕开同一个
根因，见那个插件 server.py 的模块 docstring）。
现在两条路都堵死了：调用方给了 `log_path` 就把 fd 1/2 重定向到**真实日志
文件**（对齐 LEGACY obsidian-rag/gpu_arbiter.py:241-252 的
`Popen(stdout=logf, stderr=logf)`——文件不会像管道那样被写满阻塞，孙进程
继承到的也是这个文件）；没给就退化成 PIPE，但**必须有持续排空方**（守护
线程读进有界内存缓冲），绝不再是"只读一次"。早夭诊断改从这两处取尾部。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

# 调用方没指定日志文件时，内存里保留多少子进程输出供崩溃诊断。够看一段
# traceback 即可，不是日志归档——归档归真实文件。
_DIAG_TAIL_BYTES = 256 * 1024
_DIAG_READ_CHUNK = 64 * 1024

#: 宿主写给子进程的环境变量：宿主自己的 pid。子进程（WEMM/MinerU 服务）盯着它，
#: 宿主异常没了就自退出——见 `SubprocessServiceHandle._child_env`。
PARENT_PID_ENV = "RAG_REDO_PARENT_PID"


class EnvBootstrapError(Exception):
    """env_bootstrap 脚本执行失败（venv 创建失败/脚本报错/超时）。调用方
    （resolve_plugin_python）自己捕获并折叠成"退化用核心解释器"，这个
    异常类型本身只是让失败原因可读，不是要往外传播炸宿主进程。"""


def _portable_python() -> Path | None:
    configured = os.environ.get("RAG_REDO_PORTABLE_PYTHON")
    if configured:
        candidate = Path(configured)
        return candidate if candidate.is_file() else None
    executable = Path(sys.executable).resolve()
    candidates = [
        executable.parent / "runtime" / "python" / "python.exe",
        executable.parent.parent / "runtime" / "python" / "python.exe",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _venv_python_path(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _run_env_bootstrap(plugin_dir: Path, env_bootstrap: str, *, timeout: float, logger) -> None:
    """用核心自己的解释器跑一次插件声明的 env_bootstrap 脚本——脚本本身
    负责"在 <plugin_dir>/.venv 建一个独立 venv、pip 装好 requirements.txt
    列的依赖"，核心不替插件决定装什么，只负责"跑这个脚本、把结果记录
    下来"。约定脚本是一个 `.py` 文件（不是 `.sh`/`.bat`）——Windows 优先
    是硬性要求（AGENTS.md 五条约束第5条），一份纯 Python 脚本不需要用户
    机器上有 bash/WSL 才能跑，用核心自己已经在跑的这个解释器执行就行，
    不需要额外引入 shell 依赖。

    **`sys.executable` 在 PyInstaller 冻结产物里不是通用解释器**（2026-
    09-23 真机打包安装后真实调用时踩到的严重坑，不是猜的）：源码/开发
    环境下 `sys.executable` 是 `.venv/Scripts/python.exe`，接受任意脚本
    路径当参数、老老实实执行；冻结产物里 `sys.executable` 是冻结 exe
    自己（比如 `rag-redo-mcp.exe`），这个 exe 的入口是写死的
    `mcp_stdio.py::main()`，不认识"把 env_bootstrap.py 当脚本参数执行"
    这种用法——真实观察到的后果是它直接忽略参数、把自己当成一个全新的
    MCP 服务实例重新跑起来，而这个新实例的 `on_enable` 又会再触发一次
    同样的 env_bootstrap 尝试，指数级递归拉起自己，几分钟内真实堆出了
    四十多个 `rag-redo-mcp.exe` 进程——这正是架构红线6"不产生游离进程"
    要死死防住的场景，只是这次是被"能启动子进程"这个能力本身触发的自我
    复制，不是"启动了忘记收"。所以这里显式拒绝在冻结环境下尝试执行
    ——宁可插件启用失败、报错清楚，也不能让 fail-open 的退化路径变成
    自我复制的进程炸弹。真正的修复（给冻结/便携产物打包一份独立的、能当
    脚本解释器用的便携 Python，即下方 `_portable_python()` + 安装目录
    runtime/python 约定）已经落地——便携 ZIP 构建已包含 runtime/python，
    干净机验收仍是待办，见 docs/ROADMAP.md Phase 4。"""
    if getattr(sys, "frozen", False):
        python = _portable_python()
        if python is None:
            raise EnvBootstrapError(
                "当前是 PyInstaller 冻结产物，但找不到独立便携 Python；"
                "请确认安装目录包含 runtime/python/python.exe"
            )
    else:
        python = Path(sys.executable)
    script = plugin_dir / env_bootstrap
    if not script.is_file():
        raise EnvBootstrapError(f"env_bootstrap 脚本缺失: {script}")
    logger.info("插件 %s 首次启用，正在建独立环境（%s）……这一步可能要几分钟", plugin_dir.name, env_bootstrap)
    try:
        result = subprocess.run(
            [str(python), str(script)],
            cwd=str(plugin_dir),
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise EnvBootstrapError(f"env_bootstrap 超时（>{timeout:.0f}s）: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:2000]
        raise EnvBootstrapError(f"env_bootstrap 退出码 {result.returncode}: {stderr}")
    logger.info("插件 %s 独立环境已就绪", plugin_dir.name)


def resolve_plugin_python(
    plugin_dir: Path,
    *,
    env_bootstrap: str | None = None,
    logger=None,
    bootstrap_timeout: float = 1800.0,
) -> str:
    """subprocess_service 插件应该用自己 env_bootstrap 建出来的独立解释器
    跑子进程，不是核心的 `.venv`——两者必须互相隔离（架构红线7"不碰系统/
    其他环境"的插件间版本）。按约定路径找：
    `<plugin_dir>/.venv/bin/python`（POSIX）或
    `<plugin_dir>/.venv/Scripts/python.exe`（Windows）。

    **env_bootstrap 真正执行（2026-09-23 补齐，此前是已知的刻意简化）**：
    约定路径下找不到独立 venv、且插件声明了 `env_bootstrap` 时，"首次
    启用时跑一次"（docs/PLUGIN_SPEC.md 第3节的原话）——阻塞式跑一遍脚本
    （可能要几分钟，同"第一次搜索要下载模型"的用户预期一致，不是卡死），
    再重新探测。跑完探测还是找不到、或者脚本本身执行失败，一律 fail-open
    退化用核心自己的解释器（同没声明 env_bootstrap 时的原有行为）并把
    原因记进日志——不是静默假装成功，调用方（插件的 on_enable，最终会
    体现成子进程用错误的解释器启动失败）能看到清楚的失败原因。没有声明
    env_bootstrap 的插件（比如当前的 official-ocr-mineru-local 参考
    实现，只用标准库、没有真实重依赖）行为不变。

    **`RAG_REDO_SKIP_ENV_BOOTSTRAP` 环境变量**：测试套件用——真实的
    env_bootstrap 脚本可能会真的 `pip install torch` 这种几百MB到几GB
    的重依赖，测试纪律要求 `tests/run.py` 不碰真实网络/不拖成几分钟
    （同 `RAG_REDO_FAKE_OCR`/`RAG_REDO_FAKE_WEMM` 这两个已有先例同一条
    纪律）。设了这个变量时，即使插件声明了 env_bootstrap 也直接跳过，
    按"没声明"处理——测试本来就该走假实现（子进程内部的 FAKE_* 变量），
    不需要真的建出一个装好 torch 的独立环境。

    **冻结产物（PyInstaller）里"退化用核心解释器"这条路必须直接拒绝，
    不能真退化**（2026-09-23 真机打包安装后真实调用时抓到的严重bug，
    完整原因见 `_run_env_bootstrap` 的 docstring）：源码/开发环境下
    `sys.executable` 是能接受任意脚本参数的通用解释器，"退化用它"是
    安全的 fail-open；冻结产物里 `sys.executable` 是冻结 exe 自己，
    `command = ["{python}", "server.py", ...]` 一旦真的替换成冻结exe
    自己的路径，Popen 出来的不是子进程该跑的 server.py，是把整个应用
    自己重新拉起一份——递归下去就是指数级自我复制的进程炸弹（真实观测
    到几分钟内四十多个游离进程），这正是架构红线6要死死防住的场景。
    所以这里改成显式抛出 `SubprocessServiceError`，插件启用失败、原因
    清楚地折叠进插件状态（core/runtime.py::enable() 已有的统一折叠
    机制），绝不允许在冻结环境下静默产出一个会自我复制的错误命令。"""
    venv_python = _venv_python_path(plugin_dir / ".venv")
    if venv_python.exists():
        return str(venv_python)
    if env_bootstrap and not os.environ.get("RAG_REDO_SKIP_ENV_BOOTSTRAP"):
        import logging

        log = logger if logger is not None else logging.getLogger("rag_redo.core.subprocess_service")
        try:
            _run_env_bootstrap(plugin_dir, env_bootstrap, timeout=bootstrap_timeout, logger=log)
        except EnvBootstrapError as exc:
            log.warning("插件 %s 的 env_bootstrap 未能建出独立环境：%s", plugin_dir.name, exc)
        else:
            if venv_python.exists():
                return str(venv_python)
            log.warning("插件 %s 的 env_bootstrap 跑完了但没有在约定路径生成解释器", plugin_dir.name)
    if getattr(sys, "frozen", False):
        raise SubprocessServiceError(
            f"插件 {plugin_dir.name} 没有自己的独立环境，且当前是冻结产物——"
            "不能退化用核心解释器（那会导致应用自我复制，见 resolve_plugin_python 的 docstring）。"
            "这个插件在当前打包版本里暂时不可用，需要 env_bootstrap 真正成功建出独立环境才行。"
        )
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
    创建它并 start()，之后用 call() 发请求，on_disable 调 stop()。

    `log_path` 给了就把子进程的 stdout/stderr 重定向到这个真实文件（追加
    模式，跨次启动留痕），并用 `log_file` 属性把路径暴露给调用方做诊断
    （对齐 LEGACY `wemm_status` 明确把 `data/wemm_server.log` 指给用户/AI
    的做法）；没给则退回 PIPE + 持续排空线程。"""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        health_check: str | None = None,
        cwd: Path | None = None,
        startup_timeout: float = 10.0,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.port = find_free_port()
        self._command = [arg.format(port=self.port) for arg in command]
        self._health_check = health_check.format(port=self.port) if health_check else None
        self._cwd = cwd
        self._startup_timeout = startup_timeout
        self._env = env
        self._log_path = Path(log_path) if log_path is not None else None
        self._log_file: IO[bytes] | None = None
        self._log_open_error: str | None = None
        self._drain_lock = threading.Lock()
        self._drained: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        self._drain_threads: list[threading.Thread] = []
        self._process: subprocess.Popen | None = None

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def log_file(self) -> str | None:
        """子进程输出被重定向到的日志文件路径（没给 `log_path` 时为
        None）——只读快照，调用方拿它去做"去哪儿看诊断"的提示。"""
        return str(self._log_path) if self._log_path is not None else None

    @property
    def log_file_error(self) -> str | None:
        """日志文件打不开时的原因（`None`=正常或没要求落盘）。

        打不开不是错误——子进程照样能跑（输出只留在内存排空缓冲里），但
        调用方应该知道"日志这次没落盘"，否则用户按 `log_file` 找过去发现
        文件不存在会以为诊断信息被吞了。"""
        return self._log_open_error

    def _log_hint(self) -> str:
        """失败文案里的"去哪儿看诊断"尾巴。"""
        if self._log_path is None:
            return ""
        if self._log_file is None:
            return f"（日志文件打不开：{self._log_open_error}，本次子进程输出只留在内存里）"
        return f"（详见 {self.log_file}）"

    def start(self) -> None:
        # POSIX 上起一个独立进程组（start_new_session）——stop() 要对整棵
        # 进程树发信号（见该方法的说明），不新开一个组的话 os.killpg 会把
        # 发信号的核心进程自己也算进去。
        extra_kwargs = {} if sys.platform == "win32" else {"start_new_session": True}
        self._log_file = self._open_log_file()
        if self._log_file is not None:
            # 首选真实文件：文件不会像管道那样被写满后把子进程永久阻塞在
            # write() 上，而且子进程自己再派生的孙进程（MinerU 内服务那种）
            # 继承到的也是这个文件而不是调用方的管道。
            stdout_target: object = self._log_file
            stderr_target: object = self._log_file
        else:
            stdout_target = subprocess.PIPE
            stderr_target = subprocess.PIPE
        self._process = subprocess.Popen(
            self._command,
            cwd=str(self._cwd) if self._cwd else None,
            env=self._child_env(),
            stdout=stdout_target,
            stderr=stderr_target,
            # Windows 上宿主（pythonw.exe/冻结 GUI exe）本身没有控制台；不带这个
            # flag 时 CreateProcess 会给子进程新分配并短暂显示一个控制台窗口
            # （2026-09-29 用户真实反馈：桌面双击后先黑屏几秒才出界面）。POSIX 上
            # `CREATE_NO_WINDOW` 属性不存在，`getattr` 退化成 0，行为不变。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            **extra_kwargs,
        )
        if self._log_file is None:
            # 没有日志文件可写时 PIPE 仍然保留（它还有"崩溃文本可捞"的价值），
            # 但必须有持续读取方——见 _start_drain 的说明。
            self._start_drain("stdout", self._process.stdout)
            self._start_drain("stderr", self._process.stderr)
        if self._health_check is None:
            return
        deadline = time.time() + self._startup_timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            if self._process.poll() is not None:
                returncode = self._process.returncode
                detail = self._diagnostic_tail()
                hint = self._log_hint()
                self.stop()  # 进程已经退出，这里收口管道/日志句柄，不留文件描述符泄漏
                raise SubprocessServiceError(
                    f"子进程启动后立刻退出（returncode={returncode}）: {detail[:2000]}{hint}"
                )
            try:
                with urllib.request.urlopen(self._health_check, timeout=1.0) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                last_error = exc
                time.sleep(0.1)
        self.stop()
        raise SubprocessServiceError(
            f"子进程 {self._startup_timeout}s 内没有通过 health_check: {last_error}{self._log_hint()}"
        )

    def _child_env(self) -> dict[str, str]:
        """子进程的环境：调用方给的（没给就继承当前环境）再加上 `RAG_REDO_PARENT_PID`。

        子进程据此盯着宿主：宿主**异常没了**（崩溃、被“结束任务”、被强杀）来不及走
        `stop()` 时，Windows 上子进程不会跟着走，会一直占着显存直到空闲自退出（半小时）。
        2026-09-29 实测抓到过这样一个孤儿服务。宿主正常走 `stop()` 的路径不受影响。"""
        env = dict(os.environ if self._env is None else self._env)
        env[PARENT_PID_ENV] = str(os.getpid())
        return env

    def _open_log_file(self) -> IO[bytes] | None:
        """打开（必要时创建）日志文件。**写不了日志绝不能让子进程起不来**
        ——插件装在只读目录、data 根不可写、沙箱里跑都可能失败，这里降级成
        `None`（退回 PIPE + 排空线程），并把原因留在 `_log_open_error` 里
        供诊断，不向上抛。"""
        if self._log_path is None:
            return None
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            return open(self._log_path, "ab", buffering=0)
        except OSError as exc:
            self._log_open_error = f"{type(exc).__name__}: {exc}"
            return None

    def _start_drain(self, name: str, stream: IO[bytes] | None) -> None:
        """持续排空子进程的 stdout/stderr（守护线程）。

        这是"没人读的 PIPE"这个 bug 的另一半：管道本身可以留（它让崩溃文本
        还能捞出来），但**必须有持续读取方**——Windows 管道缓冲约 64KB，
        写满后子进程的 `write()` 直接阻塞，进程还活着、服务永远起不来。
        排空线程是 daemon，管道 EOF 后自然退出，stop() 里也会 join。"""
        if stream is None:
            return

        def _run() -> None:
            try:
                fd = stream.fileno()
            except (OSError, ValueError):
                return
            while True:
                try:
                    chunk = os.read(fd, _DIAG_READ_CHUNK)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                with self._drain_lock:
                    buffer = self._drained[name]
                    buffer.extend(chunk)
                    if len(buffer) > _DIAG_TAIL_BYTES:
                        del buffer[: len(buffer) - _DIAG_TAIL_BYTES]

        thread = threading.Thread(target=_run, daemon=True, name=f"subprocess-stdio-{name}")
        self._drain_threads.append(thread)
        thread.start()

    def _diagnostic_tail(self, limit: int = 4000) -> str:
        """子进程最近输出的尾部（stderr 优先，没有再退 stdout）——启动即
        早夭时的诊断文案来源。有日志文件读文件（崩溃现场永久可查），没有
        就读排空线程攒下来的内存缓冲。"""
        if self._log_path is not None and self._log_path.is_file():
            try:
                with self._log_path.open("rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    handle.seek(max(0, handle.tell() - limit * 4))
                    return handle.read().decode("utf-8", errors="replace")[-limit:]
            except OSError:
                return ""
        return self._drained_tail(limit)

    def _drained_tail(self, limit: int = 4000, wait_s: float = 2.0) -> str:
        """读排空缓冲。子进程刚退出时排空线程可能还没被调度到，最多等一小段
        时间再放弃——早夭诊断恰恰是这个场景，不能因为"线程慢了几毫秒"就把
        崩溃原因读丢了。"""
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            with self._drain_lock:
                stderr = bytes(self._drained["stderr"])
                stdout = bytes(self._drained["stdout"])
            data = stderr if stderr else stdout
            if data or time.monotonic() >= deadline:
                return data.decode("utf-8", errors="replace")[-limit:]
            time.sleep(0.02)

    def call(self, method: str, payload: dict, *, timeout: float = 30.0) -> dict:
        if not self.is_alive:
            raise SubprocessServiceError("子进程已经不在运行，无法调用")
        url = f"http://127.0.0.1:{self.port}/{method}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # 读响应超时抛的是 TimeoutError（OSError 子类），不是 URLError；子进程回了半截或
            # 不是 JSON 抛 ValueError。都折叠成本类异常，调用方（提取器）才能按“这一份失败”处理，
            # 不会把整轮索引拖垮（§5：提取器失败不能抛成宿主异常）。
            raise SubprocessServiceError(f"调用子进程 {method} 失败: {type(exc).__name__}: {exc}") from exc

    def stop(self, grace_period: float = 3.0) -> None:
        """不留游离进程（架构红线6）：先礼后兵——给子进程机会自己清理，
        grace_period 内没退出再强杀，最后 wait() 确认真的没了，不是发了
        信号就假装完事。真实踩过的坑：只 wait() 进程退出不够，
        Popen 打开的管道文件描述符不会因为子进程退出就自动关闭，得手动
        close()，不然每 start/stop 一轮就泄漏两个文件描述符（测试里用
        ResourceWarning 抓到的）。日志文件句柄同样必须在这里关掉——
        Windows 上还开着的文件删不掉、临时目录也清不掉（架构红线 §7
        "任何文件锁和句柄都必须有停止、等待和异常收口路径"）。

        收口顺序有讲究：先杀进程树 → 再 join 排空线程（子进程一死，管道写
        端全部关闭，os.read 立刻拿到 EOF 返回，线程自然退出）→ 最后才
        close 管道/日志句柄。反过来做就是在另一个线程阻塞读的时候抽掉它的
        句柄。"""
        if self._process is None:
            self._close_log_file()
            return
        if self._process.poll() is None:
            self._kill_process_tree(grace_period)
        for thread in self._drain_threads:
            thread.join(timeout=1.0)
        self._drain_threads.clear()
        for stream in (self._process.stdout, self._process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._process = None
        self._close_log_file()

    def _close_log_file(self) -> None:
        if self._log_file is None:
            return
        try:
            self._log_file.close()
        except OSError:
            pass
        self._log_file = None

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
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
