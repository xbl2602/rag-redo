"""统一测试入口，风格对齐旧 obsidian-rag 项目 tests/run.py：每个套件在自己的子进程里
运行，避免 Chroma/原生扩展的进程级句柄在几十个测试 runtime 之间累积，打印
“结果：N/M 套通过”。

覆盖三类测试：
- 核心测试（本目录下的 test_*.py，测 core/ 里的核心组件；必须手动登记进 CORE_SUITES）
- 插件测试（plugins/*/tests/test_*.py，随插件自己走——每个插件自带测试，
  方便将来独立分发时测试也跟着走，不用依赖仓库中心 tests/ 目录）
- 工具脚本测试（tools/tests/test_*.py，比如迁移脚本）
每个测试模块用它自己的完整相对路径生成唯一模块名，避免"两处都有一个
test_config.py"这种同名冲突。

几套同时跑（2026-10-01 操作者确认）：一遍全量原本挨个跑要 7～10 分钟，85% 的时间花在
8 套“真启动”的集成测试上（真的拉起页级视觉服务、索引进程、桌面窗口入口），大部分时间
在等进程起停，电脑 20 个线程只用着 1 个。现在默认同时跑 `DEFAULT_JOBS` 套、上次最慢的先
开跑，上次超过 `SHARD_SECONDS` 秒的大套件按用例拆成几份同时跑，用例一条不少；各套各用自己的
临时目录和随机端口，互不相干。`--jobs 1` 退回挨个跑、不拆。

`--changed`：开发中途只跑和改动有关的套件（挑法见 `related_suites.py`），提交前仍必须
跑全量（AGENTS.md §12/§13）。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).parent.parent
TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(REPO_ROOT))

# Windows 下用管道/文件重定向运行时（不是真实控制台），stdout/stderr 的
# 编码会退化成系统区域码页（比如 cp1252），print() 里的中文用例名直接
# UnicodeEncodeError 崩掉——这里统一重编码成 utf-8，真实在这台 Windows
# 机器上复现过这个崩溃才加的，不是猜的。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

CORE_SUITES = [
    "test_agents_contract",
    "test_atomic",
    "test_cli",
    "test_manifest",
    "test_plugin_manifests_sane",
    "test_registry",
    "test_datastore",
    "test_resource_arbiter",
    "test_gpu_arbiter",
    "test_write_gate",
    "test_settings",
    "test_extract_cache",
    "test_index_progress",
    "test_index_failures",
    "test_model_loading",
    "test_noise_cleaning",
    "test_note_relations",
    "test_graph",
    "test_overview_map",
    "test_conversion_cache",
    "test_singleton",
    "test_paths",
    "test_runtime",
    "test_subprocess_service",
    "test_pipeline_e2e",
    "test_pipeline_data_safety",
    "test_demo_vault",
    "test_run_entry",
]

#: 默认同时跑几套：线程数的一半、最多 6。再多也快不了多少——全量的总用时最终卡在最慢的
#: 那一套（页级视觉，约 2 分钟）上，开太多只会让对时机敏感的测试在机器忙时更容易误报。
DEFAULT_JOBS = max(1, min(6, (os.cpu_count() or 2) // 2))

_RAN_LINE = re.compile(r"^Ran (\d+) tests? in", re.MULTILINE)


@dataclass(frozen=True)
class Suite:
    label: str
    path: Path

    @property
    def rel(self) -> str:
        return self.path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


@dataclass(frozen=True)
class SuiteResult:
    suite: Suite
    ok: bool
    tests_run: int | None
    elapsed: float
    output: str


def _discover_extra_test_files() -> list[Path]:
    found: list[Path] = []
    plugins_dir = REPO_ROOT / "plugins"
    if plugins_dir.exists():
        found.extend(plugins_dir.glob("*/tests/test_*.py"))
    tools_tests_dir = REPO_ROOT / "tools" / "tests"
    if tools_tests_dir.exists():
        found.extend(tools_tests_dir.glob("test_*.py"))
    return sorted(found)


def all_suites() -> list[Suite]:
    suites = [Suite(f"core/{name}", TESTS_DIR / f"{name}.py") for name in CORE_SUITES]
    suites += [Suite(str(path.relative_to(REPO_ROOT)), path) for path in _discover_extra_test_files()]
    return suites


def unregistered_core_suites() -> list[str]:
    """tests/ 下没登记进 CORE_SUITES 的测试文件——它们不会被跑到，结尾提醒一句。"""
    registered = set(CORE_SUITES)
    return sorted(p.stem for p in TESTS_DIR.glob("test_*.py") if p.stem not in registered)


def _load_module_from_path(path: Path):
    try:
        parts = path.resolve().relative_to(REPO_ROOT.resolve()).with_suffix("").parts
    except ValueError:  # 仓库外的套件（测试入口自己的测试会在临时目录里造几套）
        parts = ("external", hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8], path.stem)
    module_name = "plugin_test__" + "__".join(parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _kill_tree(proc: subprocess.Popen) -> None:
    """Ctrl+C 中断时收掉还在跑的套件。Windows 上杀父进程不会带走子孙（AGENTS.md §7），用
    taskkill /T 连整棵树。"""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        proc.kill()


@dataclass(frozen=True)
class _Job:
    """一份活：一整套，或者大套件拆出来的第 `shard` 份（共 `shards` 份）。"""

    suite: Suite
    shard: int = 0
    shards: int = 1


@dataclass(frozen=True)
class _JobResult:
    job: _Job
    ok: bool
    tests_run: int | None
    elapsed: float
    output: str


def _run_one(job: _Job, running: dict[int, subprocess.Popen], lock: threading.Lock) -> _JobResult:
    """在子进程里跑一份活，输出先收进临时文件、跑完再整块打印，几套同时跑也不会混在一起。

    收进文件而不是管道：测试拉起的服务（页级视觉、扫描件识别）万一残留、还攥着继承来的输出
    句柄，管道就永远等不到结尾、整轮回归卡死；文件只等套件进程本身退出。"""
    command = [sys.executable, str(Path(__file__).resolve()), "--suite", str(job.suite.path.resolve())]
    if job.shards > 1:
        command += ["--shard", f"{job.shard}/{job.shards}"]
    start = time.time()
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(command, cwd=str(REPO_ROOT), stdout=out, stderr=subprocess.STDOUT)
        with lock:
            running[proc.pid] = proc
        try:
            code = proc.wait()
        finally:
            with lock:
                running.pop(proc.pid, None)
        out.seek(0)
        text = out.read().decode("utf-8", errors="replace")
    counts = _RAN_LINE.findall(text)
    return _JobResult(job, code == 0, int(counts[-1]) if counts else None, time.time() - start, text)


def _merge(suite: Suite, parts: list[_JobResult]) -> SuiteResult:
    """把一套拆出来的几份合回一套：全过才算过，用例数相加，耗时记各份之和（下次据此决定拆几份）。"""
    parts = sorted(parts, key=lambda p: p.job.shard)
    counts = [p.tests_run for p in parts]
    if len(parts) == 1:
        output = parts[0].output
    else:
        output = "\n".join(
            f"（第 {p.job.shard + 1}/{p.job.shards} 份）\n{p.output.rstrip()}" for p in parts if not p.ok
        )
    return SuiteResult(
        suite,
        all(p.ok for p in parts),
        None if None in counts else sum(counts),
        sum(p.elapsed for p in parts),
        output,
    )


def _describe(result: SuiteResult, shards: int = 1) -> str:
    count = f"{result.tests_run} 用例, " if result.tests_run is not None else ""
    split = f", 分 {shards} 份同时跑" if shards > 1 else ""
    return f"{'PASS' if result.ok else 'FAIL'} {result.suite.label} ({count}{result.elapsed:.2f}s{split})"


def _size_estimate(suite: Suite) -> float:
    """没有历史耗时时按文件大小估：大文件通常用例多、也更慢。只用来排先后。"""
    try:
        return suite.path.stat().st_size / 20000
    except OSError:
        return 0.0


#: 上次超过这么多秒的套件拆成几份同时跑（最多 `MAX_SHARDS` 份）。不拆的话，全量的总用时就是
#: 最慢那一套自己的时长——页级视觉、端到端两套各要 2～4 分钟，别的早跑完了还在等它们。
SHARD_SECONDS = 70.0
MAX_SHARDS = 4


def _shard_count(estimate: float, jobs: int) -> int:
    if jobs <= 1:
        return 1
    return max(1, min(jobs, MAX_SHARDS, math.ceil(estimate / SHARD_SECONDS)))


def run_suites(
    suites: list[Suite],
    jobs: int,
    estimates: dict[str, float] | None = None,
    report: Callable[[str], None] = print,
) -> list[SuiteResult]:
    """同时跑 `jobs` 份活，预计最慢的先开跑（全量的总用时由最慢那一份和它开跑的时间决定）；
    上次特别慢的套件按用例拆成几份。每跑完一套就报一行；没过的连同它的全部输出一起报。"""
    estimates = estimates or {}
    work: list[tuple[float, _Job]] = []
    for suite in suites:
        estimate = estimates.get(suite.label, _size_estimate(suite))
        shards = _shard_count(estimate, jobs)
        work += [(estimate / shards, _Job(suite, i, shards)) for i in range(shards)]
    work.sort(key=lambda item: -item[0])
    running: dict[int, subprocess.Popen] = {}
    lock = threading.Lock()
    results: list[SuiteResult] = []
    pending: dict[str, list[_JobResult]] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, jobs))
    try:
        futures = [pool.submit(_run_one, job, running, lock) for _, job in work]
        for future in as_completed(futures):
            part = future.result()
            parts = pending.setdefault(part.job.suite.label, [])
            parts.append(part)
            if len(parts) < part.job.shards:
                continue
            result = _merge(part.job.suite, parts)
            results.append(result)
            report(_describe(result, part.job.shards))
            if not result.ok:
                report(f"──── {result.suite.label} 的输出 ────")
                report(result.output.rstrip())
                report(f"──── {result.suite.label} 输出结束 ────")
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        with lock:
            leftovers = list(running.values())
        for proc in leftovers:
            _kill_tree(proc)
        raise
    finally:
        pool.shutdown(wait=True)
    return results


def _durations_path() -> Path:
    """上次各套的耗时，只用来排先后。放系统临时目录（不进仓库），按仓库路径区分。"""
    digest = hashlib.sha1(str(REPO_ROOT.resolve()).lower().encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"rag-redo-suite-durations-{digest}.json"


def load_durations(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, (int, float))}


def save_durations(path: Path, results: list[SuiteResult]) -> None:
    from core.atomic import atomic_write_text

    merged = load_durations(path)
    merged.update({r.suite.label: round(r.elapsed, 2) for r in results})
    try:
        atomic_write_text(path, json.dumps(merged, ensure_ascii=False, indent=1))
    except OSError:
        pass  # 只影响下次的排序，写不进去不要紧


def _select_changed(suites: list[Suite], paths: list[str]) -> list[Suite] | None:
    """按改动挑套件；拿不到改动清单时返回 None（调用方改跑全量）。"""
    from related_suites import changed_files_from_git, select_suites

    if paths:
        changed = paths
    else:
        try:
            changed = changed_files_from_git(REPO_ROOT)
        except OSError as exc:
            print(f"拿不到改动清单（{exc}），改跑全量")
            return None
    by_rel = {suite.rel: suite for suite in suites}
    selection = select_suites(REPO_ROOT, list(by_rel), changed)
    for note in selection.notes:
        print(note)
    chosen = [by_rel[rel] for rel in selection.suites]
    print(f"按改动挑出 {len(chosen)}/{len(suites)} 套（只用于开发中途；提交前必须跑全量 tests/run.py）")
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAG REDO 统一测试入口")
    parser.add_argument(
        "--jobs", "-j", type=int, default=DEFAULT_JOBS,
        help=f"同时跑几套（默认 {DEFAULT_JOBS}；1 = 挨个跑）",
    )
    parser.add_argument(
        "--changed", nargs="*", metavar="文件",
        help="只跑和改动有关的套件：不带文件名 = 工作区里所有未提交的改动；也可以直接列文件",
    )
    parser.add_argument("--list", action="store_true", help="只列出要跑哪些套件，不真跑")
    parser.add_argument("--suite", help=argparse.SUPPRESS)
    parser.add_argument("--shard", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.suite:
        shard = None
        if args.shard:
            index, _, count = args.shard.partition("/")
            shard = (int(index), int(count))
        return _run_single_suite(Path(args.suite), shard)

    suites = all_suites()
    if args.changed is not None:
        chosen = _select_changed(suites, args.changed)
        if chosen is not None:
            suites = chosen
    if args.list:
        for suite in suites:
            print(suite.label)
        return 0

    jobs = max(1, min(args.jobs, len(suites)))
    print(f"共 {len(suites)} 套，同时跑 {jobs} 套（上次最慢的先开跑）", flush=True)
    durations_path = _durations_path()
    start = time.time()
    results = run_suites(suites, jobs, load_durations(durations_path), report=lambda line: print(line, flush=True))
    wall = time.time() - start
    save_durations(durations_path, results)

    total_ok = sum(1 for r in results if r.ok)
    print(f"\n结果：{total_ok}/{len(suites)} 套通过")
    timings = sorted(results, key=lambda r: -r.elapsed)
    print("耗时前5：", ", ".join(f"{r.suite.label}={r.elapsed:.2f}s" for r in timings[:5]))
    print(f"总用时 {wall:.0f}s（各套耗时加起来 {sum(r.elapsed for r in results):.0f}s）")
    failed = [r.suite.label for r in results if not r.ok]
    if failed:
        print("没通过：" + "、".join(failed))
    missing = unregistered_core_suites()
    if missing:
        print("注意：tests/ 下这些测试文件没登记进 CORE_SUITES，不会被跑到：" + "、".join(missing))
    return 0 if total_ok == len(suites) else 1


def _flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _run_single_suite(path: Path, shard: tuple[int, int] | None = None) -> int:
    if path.resolve().parent == TESTS_DIR.resolve():
        # 核心套件按原名导入：有的核心测试会 `from test_pipeline_e2e import ...` 复用夹具，
        # 换个模块名再导一次会得到两份互不相认的类。
        module = __import__(path.stem)
    else:
        module = _load_module_from_path(path)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if shard is not None:
        # 按用例编号轮流分：同一个类的用例散到几份里，每份各自跑一遍 setUpClass。
        index, count = shard
        tests = sorted(_flatten(suite), key=lambda test: test.id())
        suite = unittest.TestSuite(t for i, t in enumerate(tests) if i % count == index)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
