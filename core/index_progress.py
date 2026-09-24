"""core/index_progress.py — 索引进度报告 + 独立工作进程（核心服务，不是插件）。

GUI 和 MCP 可以是两个独立进程，所以最近一次进度、worker 状态和停止操作都
通过磁盘状态与每库文件锁协调。同一个库只允许一个 worker 持锁运行，不同库
可以并行；worker 持锁到退出，操作系统会在进程崩溃后自动释放锁。
"""
from __future__ import annotations

import atexit
import dataclasses
import hashlib
import json
import multiprocessing
import os
import re
import signal
import subprocess
import threading
import time
import uuid
import weakref
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Callable, Iterator

from .runtime import PluginRuntime, PluginState
from .singleton import FileByteLock, pid_alive


def _shutdown_index_worker_manager(manager_ref) -> None:
    manager = manager_ref()
    if manager is not None:
        manager.shutdown()


@dataclasses.dataclass
class IndexProgressEvent:
    phase: str
    files_done: int = 0
    files_total: int = 0
    current_path: str = ""
    chunks_done: int = 0
    chunks_total: int | None = None
    message: str = ""
    stall_grace_s: float = 0.0


@dataclasses.dataclass
class IndexProgress:
    library_id: str
    run_id: str
    source: str
    full: bool
    launcher_pid: int
    worker_pid: int
    stage: str
    phase: str = ""
    files_done: int = 0
    files_total: int = 0
    current_path: str = ""
    chunks_done: int = 0
    chunks_total: int | None = None
    message: str = ""
    started_at: float = 0.0
    heartbeat_at: float = 0.0
    progress_at: float = 0.0
    stall_grace_until: float | None = None
    finished_at: float | None = None
    error: str | None = None
    succeeded: int = 0
    failed: int = 0
    deferred: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    retried: int = 0


@dataclasses.dataclass
class IndexStartResult:
    started: bool
    message: str
    run_id: str = ""
    worker_pid: int | None = None

    def __iter__(self) -> Iterator[object]:
        yield self.started
        yield self.message


def _atomic_write_json(path: Path, data: object) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                return True
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01)
        return False
    except OSError:
        return False
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _redirect_output(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)


def _terminate_worker(process: BaseProcess) -> bool:
    pid = process.pid
    if not process.is_alive():
        process.join(timeout=1)
        return pid is None or not pid_alive(pid)
    if pid is None:
        return False
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            process_group = os.getpgid(pid)
            if process_group == pid:
                os.killpg(process_group, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    process.join(timeout=15)
    if process.is_alive():
        try:
            process.kill()
        except OSError:
            pass
        process.join(timeout=5)
    return not process.is_alive() and not pid_alive(pid)


def _prepare_worker_containment(launcher_pid: int) -> object | None:
    if os.name != "nt":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        pr_set_pdeathsig = 1
        if libc.prctl(pr_set_pdeathsig, signal.SIGTERM, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

        def _handle_term(signum, frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, _handle_term)
        return None

    import ctypes
    from ctypes import wintypes

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, ctypes.FormatError(error))
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, ctypes.FormatError(error))
    return job


def _index_worker(
    plugin_dir: str,
    data_dir: str,
    enabled_plugin_ids: tuple[str, ...],
    active_choices: dict[str, str],
    library_id: str,
    run_id: str,
    source: str,
    full: bool,
    format_allowlist: tuple[str, ...] | None,
    launcher_pid: int,
    ack_path: str,
    log_path: str,
    heartbeat_interval: float,
    heartbeat_timeout: float,
    stall_timeout: float,
) -> None:
    from .pipeline import Pipeline

    ack_file = Path(ack_path)
    data_path = Path(data_dir)
    lock = FileByteLock(Path(data_dir) / "index_progress" / "locks" / f"{_status_key(library_id)}.lock")
    acquired = False
    containment = None
    try:
        try:
            _redirect_output(Path(log_path))
        except OSError as exc:
            _atomic_write_json(
                ack_file,
                {"accepted": False, "message": f"启动失败：无法打开工作进程日志：{type(exc).__name__}: {exc}"},
            )
            return
        try:
            containment = _prepare_worker_containment(launcher_pid)
        except OSError as exc:
            print(f"index worker containment degraded: {type(exc).__name__}: {exc}")
        if os.name != "nt":
            try:
                os.setsid()
            except OSError as exc:
                _atomic_write_json(
                    ack_file,
                    {"accepted": False, "message": f"启动失败：无法建立独立进程组：{type(exc).__name__}: {exc}"},
                )
                return
            if os.getppid() != launcher_pid:
                raise SystemExit(128 + signal.SIGTERM)
        try:
            acquired = lock.acquire()
        except OSError as exc:
            _atomic_write_json(
                ack_file,
                {"accepted": False, "message": f"启动失败：无法获取库锁：{type(exc).__name__}: {exc}"},
            )
            return
        if not acquired:
            _atomic_write_json(
                ack_file,
                {"accepted": False, "message": f"库「{library_id}」已经有一个索引任务在跑"},
            )
            return

        now = time.time()
        progress = IndexProgress(
            library_id=library_id,
            run_id=run_id,
            source=source,
            full=full,
            launcher_pid=launcher_pid,
            worker_pid=os.getpid(),
            stage="starting",
            phase="starting",
            message="索引工作进程已启动",
            started_at=now,
            heartbeat_at=now,
            progress_at=now,
        )
        if not _atomic_write_json(
            data_path / "index_progress" / f"{_status_key(library_id)}.json",
            dataclasses.asdict(progress),
        ):
            _atomic_write_json(ack_file, {"accepted": False, "message": "启动失败：无法写入索引进度状态"})
            return
        if not _atomic_write_json(
            ack_file,
            {"accepted": True, "message": "索引工作进程已启动", "worker_pid": os.getpid()},
        ):
            return

        progress_lock = threading.Lock()
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None

        def _heartbeat() -> None:
            while not heartbeat_stop.wait(heartbeat_interval):
                if launcher_pid > 0 and not pid_alive(launcher_pid):
                    os._exit(129)
                with progress_lock:
                    progress.heartbeat_at = time.time()
                    _atomic_write_json(
                        data_path / "index_progress" / f"{_status_key(library_id)}.json",
                        dataclasses.asdict(progress),
                    )

        def _on_progress(event: IndexProgressEvent) -> None:
            now = time.time()
            with progress_lock:
                progress.stage = "running"
                progress.phase = event.phase
                progress.files_done = event.files_done
                progress.files_total = event.files_total
                progress.current_path = event.current_path
                progress.chunks_done = event.chunks_done
                progress.chunks_total = event.chunks_total
                progress.message = event.message
                if event.stall_grace_s > 0:
                    if progress.stall_grace_until is None or now >= progress.stall_grace_until:
                        progress.stall_grace_until = now + min(float(event.stall_grace_s), 600.0)
                else:
                    progress.stall_grace_until = None
                progress.progress_at = now
                _atomic_write_json(
                    data_path / "index_progress" / f"{_status_key(library_id)}.json",
                    dataclasses.asdict(progress),
                )

        runtime = None
        activated: list[str] = []
        terminal_stage = "failed"
        terminal_error: str | None = None
        terminal_succeeded = 0
        terminal_failed = 0
        terminal_deferred = 0
        terminal_counts = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0, "retried": 0}
        heartbeat_thread = threading.Thread(
            target=_heartbeat,
            daemon=True,
            name=f"index-heartbeat-{library_id}",
        )
        heartbeat_thread.start()
        try:
            runtime = PluginRuntime(Path(plugin_dir), state_file=None, data_dir=data_path)
            runtime.scan()
            for point, plugin_id in active_choices.items():
                runtime.registry.set_active(point, plugin_id)
            for plugin_id in sorted(enabled_plugin_ids):
                if plugin_id not in runtime.plugins:
                    print(f"index worker skipped missing plugin: {plugin_id}")
                    continue
                runtime.load(plugin_id)
                plugin = runtime.plugins[plugin_id]
                if plugin.instance is not None:
                    activated.append(plugin_id)
                if plugin.state == PluginState.LOADED:
                    runtime.enable(plugin_id)
                    plugin = runtime.plugins[plugin_id]
                if plugin.state != PluginState.ENABLED:
                    print(
                        f"index worker skipped unavailable plugin: {plugin_id}: "
                        f"{plugin.state.value}"
                    )
                    try:
                        runtime.unload(plugin_id)
                    except Exception:
                        pass
                    if plugin_id in activated:
                        activated.remove(plugin_id)
            report = Pipeline(runtime).index_library(
                library_id,
                generation_id=run_id,
                full=full,
                progress_callback=_on_progress,
                format_allowlist=format_allowlist,
            )
            terminal_stage = "done"
            terminal_succeeded = getattr(report, "succeeded", 0)
            terminal_failed = getattr(report, "failed", 0)
            terminal_deferred = getattr(report, "deferred", 0)
            terminal_counts = {
                name: int(getattr(report, name, 0))
                for name in ("added", "changed", "removed", "unchanged", "retried")
            }
        except Exception as exc:
            terminal_error = f"{type(exc).__name__}: {exc}"
        finally:
            if runtime is not None:
                for plugin_id in reversed(activated):
                    try:
                        runtime.disable(plugin_id)
                    except Exception:
                        pass
                    try:
                        runtime.unload(plugin_id)
                    except Exception:
                        pass
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=max(1.0, heartbeat_timeout))
            with progress_lock:
                progress.stage = terminal_stage
                progress.error = terminal_error
                progress.succeeded = terminal_succeeded
                progress.failed = terminal_failed
                progress.deferred = terminal_deferred
                for name, value in terminal_counts.items():
                    setattr(progress, name, value)
                progress.stall_grace_until = None
                progress.finished_at = time.time()
                progress.heartbeat_at = progress.finished_at
                progress.progress_at = progress.finished_at
                if terminal_stage == "done":
                    progress.message = "索引已完成"
                else:
                    progress.message = terminal_error or "索引失败"
                snapshot = dataclasses.asdict(progress)
            _atomic_write_json(data_path / "index_progress" / f"{_status_key(library_id)}.json", snapshot)
    finally:
        if acquired:
            lock.release()


def _status_key(library_id: str) -> str:
    safe = re.sub(r"[^\w.-]", "_", library_id)[:64] or "library"
    return f"{safe}-{hashlib.sha256(library_id.encode('utf-8')).hexdigest()[:16]}"


class IndexWorkerManager:
    HEARTBEAT_TIMEOUT_S = 15.0
    STALL_TIMEOUT_S = 60.0
    ACK_TIMEOUT_S = 15.0
    HEARTBEAT_INTERVAL_S = 5.0

    def __init__(
        self,
        plugin_dir: Path,
        data_dir: Path,
        enabled_plugin_ids: list[str] | tuple[str, ...],
        *,
        active_choices: dict[str, str] | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
        heartbeat_timeout: float = HEARTBEAT_TIMEOUT_S,
        stall_timeout: float = STALL_TIMEOUT_S,
        ack_timeout: float = ACK_TIMEOUT_S,
    ) -> None:
        self._plugin_dir = Path(plugin_dir)
        self._data_dir = Path(data_dir)
        self._enabled_plugin_ids = tuple(enabled_plugin_ids)
        self._active_choices = dict(active_choices or {})
        self._heartbeat_interval = max(float(heartbeat_interval), 0.01)
        self._heartbeat_timeout = float(heartbeat_timeout)
        self._stall_timeout = float(stall_timeout)
        self._ack_timeout = float(ack_timeout)
        self._lock = threading.Lock()
        self._workers: dict[str, BaseProcess] = {}
        self._worker_libraries: dict[str, str] = {}
        self._cleanup_callback: Callable[[str, str], None] | None = None
        atexit.register(_shutdown_index_worker_manager, weakref.ref(self))

    def set_active_choices(self, active_choices: dict[str, str]) -> None:
        with self._lock:
            self._active_choices = dict(active_choices)

    def _status_path(self, library_id: str) -> Path:
        return self._data_dir / "index_progress" / f"{_status_key(library_id)}.json"

    def _forget(self, run_id: str, process: BaseProcess | None = None) -> None:
        with self._lock:
            if process is None or self._workers.get(run_id) is process:
                self._workers.pop(run_id, None)
                self._worker_libraries.pop(run_id, None)

    def _reap_finished(self) -> None:
        with self._lock:
            workers = list(self._workers.items())
        for run_id, process in workers:
            if not process.is_alive():
                process.join(timeout=0)
                library_id = self._worker_libraries.get(run_id, "")
                if library_id:
                    current = _read_json(self._status_path(library_id))
                    needs_cleanup = bool(
                        current
                        and str(current.get("run_id")) == run_id
                        and current.get("stage") in {"starting", "running"}
                    )
                    exit_code = process.exitcode
                    message = "索引工作进程异常退出"
                    self._mark_terminal(
                        library_id,
                        run_id,
                        "failed",
                        message,
                        f"{message}（退出码 {exit_code}）",
                    )
                    if needs_cleanup:
                        self._cleanup_failed(library_id, run_id)
                self._forget(run_id, process)

    def set_cleanup_callback(self, callback: Callable[[str, str], None]) -> None:
        self._cleanup_callback = callback

    def _cleanup_failed(self, library_id: str, run_id: str) -> None:
        if self._cleanup_callback is not None:
            try:
                self._cleanup_callback(library_id, run_id)
            except Exception:
                pass

    def _write(self, progress: IndexProgress | dict) -> bool:
        data = dataclasses.asdict(progress) if isinstance(progress, IndexProgress) else progress
        return _atomic_write_json(self._status_path(str(data["library_id"])), data)

    def start(
        self,
        library_id: str,
        source: str = "api",
        full: bool = False,
        *,
        format_allowlist: tuple[str, ...] | None = None,
    ) -> IndexStartResult:
        self._reap_finished()
        run_id = uuid.uuid4().hex
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return IndexStartResult(False, f"启动失败：无法创建数据目录：{type(exc).__name__}: {exc}")
        ack_path = self._data_dir / "index_progress" / "acks" / f"{run_id}.json"
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_index_worker,
            args=(
                str(self._plugin_dir),
                str(self._data_dir),
                self._enabled_plugin_ids,
                self._active_choices,
                library_id,
                run_id,
                source,
                full,
                format_allowlist,
                os.getpid(),
                str(ack_path),
                str(self._data_dir / "index_worker.log"),
                self._heartbeat_interval,
                self._heartbeat_timeout,
                self._stall_timeout,
            ),
            daemon=False,
        )
        try:
            process.start()
        except Exception as exc:
            return IndexStartResult(False, f"启动失败：无法创建索引工作进程：{type(exc).__name__}: {exc}")
        with self._lock:
            self._workers[run_id] = process
            self._worker_libraries[run_id] = library_id

        deadline = time.monotonic() + self._ack_timeout
        while True:
            ack = _read_json(ack_path)
            if ack is not None and "accepted" in ack:
                accepted = bool(ack.get("accepted"))
                message = str(ack.get("message") or "")
                try:
                    ack_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if not accepted:
                    process.join(timeout=2)
                    if process.is_alive():
                        _terminate_worker(process)
                    self._forget(run_id, process)
                    self._mark_terminal(library_id, run_id, "failed", message, message)
                    return IndexStartResult(False, message)
                return IndexStartResult(True, "已开始后台重建索引", run_id, process.pid)
            if not process.is_alive():
                process.join(timeout=1)
                exit_code = process.exitcode
                self._forget(run_id, process)
                message = f"启动失败：索引工作进程提前退出（退出码 {exit_code}）"
                self._mark_terminal(library_id, run_id, "failed", message, message)
                self._cleanup_failed(library_id, run_id)
                try:
                    ack_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return IndexStartResult(False, message)
            if time.monotonic() >= deadline:
                terminated = _terminate_worker(process)
                message = (
                    "启动失败：等待索引工作进程确认超时"
                    if terminated
                    else "启动失败：确认超时且无法清理索引工作进程"
                )
                self._mark_terminal(library_id, run_id, "failed", message, message)
                self._cleanup_failed(library_id, run_id)
                self._forget(run_id, process)
                try:
                    ack_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return IndexStartResult(False, message)
            time.sleep(0.02)

    def status(self, library_id: str) -> dict | None:
        self._reap_finished()
        data = _read_json(self._status_path(library_id))
        if data is None:
            return None
        required = {
            "library_id",
            "run_id",
            "source",
            "full",
            "launcher_pid",
            "worker_pid",
            "stage",
            "phase",
            "files_done",
            "files_total",
            "current_path",
            "chunks_done",
            "chunks_total",
            "message",
            "started_at",
            "heartbeat_at",
            "progress_at",
            "stall_grace_until",
            "finished_at",
            "error",
            "succeeded",
            "failed",
            "added",
            "changed",
            "removed",
            "unchanged",
            "retried",
        }
        if not required.issubset(data) or data.get("library_id") != library_id:
            return None
        if data.get("stage") not in {"starting", "running", "done", "failed", "cancelled"}:
            return None
        try:
            started_at = float(data["started_at"])
            heartbeat_at = float(data["heartbeat_at"])
            progress_at = float(data["progress_at"])
            finished_at = float(data["finished_at"]) if data.get("finished_at") is not None else None
            files_done = int(data["files_done"])
            files_total = int(data["files_total"])
            launcher_pid = int(data["launcher_pid"])
            worker_pid = int(data["worker_pid"])
            grace_until = (
                float(data["stall_grace_until"]) if data.get("stall_grace_until") is not None else None
            )
        except (TypeError, ValueError):
            return None

        now = time.time()
        elapsed_s = max(0.0, (finished_at if finished_at is not None else now) - started_at)
        if data["stage"] == "done":
            percent = 100.0
        elif data["stage"] in {"starting", "running"} and data["phase"] in {"visual", "finalizing"}:
            percent = None
        elif files_total > 0:
            percent = max(0.0, min(100.0, files_done / files_total * 100.0))
        else:
            percent = 0.0
        if data["stage"] == "done":
            eta_s = 0.0
        elif data["stage"] in {"failed", "cancelled"}:
            eta_s = None
        elif percent is not None and files_total > 0 and files_done > 0:
            eta_s = max(0.0, elapsed_s * (files_total - files_done) / files_done)
        else:
            eta_s = None

        health = "healthy"
        stage_active = data["stage"] in {"starting", "running"}
        worker_alive = pid_alive(worker_pid)
        active = stage_active and worker_alive
        if stage_active:
            if not worker_alive:
                health = "orphaned"
            elif now - heartbeat_at > self._heartbeat_timeout:
                health = "stalled_no_heartbeat"
            elif now - progress_at > self._stall_timeout and not (
                grace_until is not None and now <= grace_until
            ):
                health = "stalled_no_progress"

        owner = "self" if launcher_pid == os.getpid() else "foreign"
        with self._lock:
            process = self._workers.get(str(data["run_id"]))
        can_stop = (
            owner == "self"
            and active
            and process is not None
            and process.pid == worker_pid
        )
        result = dict(data)
        result["elapsed_s"] = elapsed_s
        result["eta_s"] = eta_s
        result["percent"] = percent
        result["owner"] = owner
        result["active"] = active
        result["can_stop"] = can_stop
        result["health"] = health
        return result

    def _mark_terminal(
        self,
        library_id: str,
        run_id: str,
        stage: str,
        message: str,
        error: str | None = None,
    ) -> None:
        data = _read_json(self._status_path(library_id))
        if data is None or str(data.get("run_id")) != run_id or data.get("stage") not in {
            "starting",
            "running",
        }:
            return
        now = time.time()
        data["stage"] = stage
        data["message"] = message
        data["error"] = error
        data["heartbeat_at"] = now
        data["progress_at"] = now
        data["stall_grace_until"] = None
        data["finished_at"] = now
        _atomic_write_json(self._status_path(library_id), data)

    def stop(self, library_id: str, run_id: str = "") -> tuple[bool, str]:
        data = _read_json(self._status_path(library_id))
        if data is None:
            return False, f"库「{library_id}」没有可停止的索引任务"
        if data.get("launcher_pid") != os.getpid():
            return False, "拒绝停止：由其他启动进程拥有的索引任务"
        target_run_id = run_id or str(data.get("run_id") or "")
        if not target_run_id or target_run_id != str(data.get("run_id") or ""):
            return False, "拒绝停止：索引任务 run_id 不匹配"
        if data.get("stage") not in {"starting", "running"}:
            return False, "拒绝停止：索引任务已经结束"
        with self._lock:
            process = self._workers.get(target_run_id)
        if process is None:
            return False, "拒绝停止：当前管理器没有持有该 run_id 的工作进程"
        if process.pid != data.get("worker_pid"):
            return False, "拒绝停止：工作进程句柄已经陈旧"
        if not process.is_alive():
            exit_code = process.exitcode
            self._mark_terminal(
                library_id,
                target_run_id,
                "failed",
                "索引工作进程已经退出",
                f"索引工作进程异常退出（退出码 {exit_code}）",
            )
            self._cleanup_failed(library_id, target_run_id)
            self._forget(target_run_id, process)
            return False, "拒绝停止：工作进程已经退出"

        current = _read_json(self._status_path(library_id))
        if (
            current is None
            or str(current.get("run_id")) != target_run_id
            or current.get("launcher_pid") != os.getpid()
            or current.get("stage") not in {"starting", "running"}
        ):
            return False, "拒绝停止：索引进度状态已经变化"

        if not _terminate_worker(process):
            return False, "停止失败：索引工作进程或其进程树仍在运行"
        self._mark_terminal(library_id, target_run_id, "cancelled", "索引任务已取消")
        self._cleanup_failed(library_id, target_run_id)
        self._forget(target_run_id, process)
        return True, "索引任务已取消"

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._workers.items())
        for run_id, process in workers:
            library_id = self._worker_libraries.get(run_id, "")
            if library_id and process.is_alive():
                try:
                    stopped, _ = self.stop(library_id, run_id)
                    if stopped:
                        continue
                except Exception:
                    pass
            if process.is_alive():
                _terminate_worker(process)
            self._forget(run_id, process)
