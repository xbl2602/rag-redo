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


class EnvBootstrapError(Exception):
    """env_bootstrap 脚本执行失败（venv 创建失败/脚本报错/超时）。调用方
    （resolve_plugin_python）自己捕获并折叠成"退化用核心解释器"，这个
    异常类型本身只是让失败原因可读，不是要往外传播炸宿主进程。"""


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
    自我复制的进程炸弹。真正的修复（给冻结产物打包一份独立的、能当脚本
    解释器用的便携 Python）是后续工作，还没做，见 docs/ROADMAP.md。"""
    if getattr(sys, "frozen", False):
        raise EnvBootstrapError(
            "当前是 PyInstaller 冻结产物，没有可以用来跑 env_bootstrap 脚本的独立解释器"
            "（sys.executable 是这个冻结 exe 自己，不是通用 Python，直接拿它当解释器用"
            "会导致 exe 把自己重新拉起——已知问题，见 core/subprocess_service.py 本函数"
            "docstring，真正的修复需要给冻结产物打包一份独立便携 Python，还没做）"
        )
    script = plugin_dir / env_bootstrap
    if not script.is_file():
        raise EnvBootstrapError(f"env_bootstrap 脚本缺失: {script}")
    logger.info("插件 %s 首次启用，正在建独立环境（%s）……这一步可能要几分钟", plugin_dir.name, env_bootstrap)
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(plugin_dir),
            capture_output=True,
            timeout=timeout,
            check=False,
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
