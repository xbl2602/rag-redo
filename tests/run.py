"""统一测试入口，风格对齐旧 obsidian-rag 项目 tests/run.py：核心套件在
同一进程运行，插件套件各自在隔离子进程运行，避免 Chroma/原生扩展的进程级
句柄在几十个测试 runtime 之间累积，打印“结果：N/M 套通过”。

覆盖三类测试：
- 核心测试（本目录下的 test_*.py，测 core/ 里的核心组件）
- 插件测试（plugins/*/tests/test_*.py，随插件自己走——每个插件自带测试，
  方便将来独立分发时测试也跟着走，不用依赖仓库中心 tests/ 目录）
- 工具脚本测试（tools/tests/test_*.py，比如迁移脚本）
每个测试模块用它自己的完整相对路径生成唯一模块名，避免"两处都有一个
test_config.py"这种同名冲突。
"""
from __future__ import annotations

import gc
import importlib.util
import subprocess
import sys
import time
import unittest
from pathlib import Path

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
    "test_note_relations",
    "test_graph",
    "test_singleton",
    "test_runtime",
    "test_subprocess_service",
    "test_pipeline_e2e",
    "test_demo_vault",
]


def _discover_extra_test_files() -> list[Path]:
    found: list[Path] = []
    plugins_dir = REPO_ROOT / "plugins"
    if plugins_dir.exists():
        found.extend(plugins_dir.glob("*/tests/test_*.py"))
    tools_tests_dir = REPO_ROOT / "tools" / "tests"
    if tools_tests_dir.exists():
        found.extend(tools_tests_dir.glob("test_*.py"))
    return sorted(found)


def _load_module_from_path(path: Path):
    module_name = "plugin_test__" + "__".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    loader = unittest.TestLoader()
    total_ok = 0
    total = 0
    timings: list[tuple[str, float]] = []

    for name in CORE_SUITES:
        total += 1
        module = __import__(name)
        suite = loader.loadTestsFromModule(module)
        start = time.time()
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        elapsed = time.time() - start
        timings.append((f"core/{name}", elapsed))
        ok = result.wasSuccessful()
        gc.collect()
        total_ok += 1 if ok else 0
        print(f"{'PASS' if ok else 'FAIL'} core/{name} ({result.testsRun} 用例, {elapsed:.2f}s)")

    runner = Path(__file__).resolve()
    for path in _discover_extra_test_files():
        label = str(path.relative_to(REPO_ROOT))
        total += 1
        start = time.time()
        completed = subprocess.run(
            [sys.executable, str(runner), "--suite", str(path.resolve())],
            cwd=str(REPO_ROOT),
            check=False,
        )
        elapsed = time.time() - start
        timings.append((label, elapsed))
        ok = completed.returncode == 0
        total_ok += 1 if ok else 0
        print(f"{'PASS' if ok else 'FAIL'} {label} ({elapsed:.2f}s)")

    print(f"\n结果：{total_ok}/{total} 套通过")
    timings.sort(key=lambda t: -t[1])
    print("耗时前5：", ", ".join(f"{n}={t:.2f}s" for n, t in timings[:5]))
    return 0 if total_ok == total else 1


def _run_single_suite(path: Path) -> int:
    module = _load_module_from_path(path)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--suite":
        sys.exit(_run_single_suite(Path(sys.argv[2])))
    sys.exit(main())
