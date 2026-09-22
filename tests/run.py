"""统一测试入口，风格对齐旧 obsidian-rag 项目 tests/run.py：单进程跑完全部
套件，打印"结果：N/M 套通过"。见 ../AGENTS.md 测试纪律一节。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent))

SUITES = [
    "test_manifest",
    "test_registry",
    "test_datastore",
    "test_resource_arbiter",
    "test_runtime",
]


def main() -> int:
    loader = unittest.TestLoader()
    total_ok = 0
    timings: list[tuple[str, float]] = []
    for name in SUITES:
        module = __import__(name)
        suite = loader.loadTestsFromModule(module)
        start = time.time()
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        elapsed = time.time() - start
        timings.append((name, elapsed))
        ok = result.wasSuccessful()
        total_ok += 1 if ok else 0
        print(f"{'PASS' if ok else 'FAIL'} {name} ({result.testsRun} 用例, {elapsed:.2f}s)")

    print(f"\n结果：{total_ok}/{len(SUITES)} 套通过")
    timings.sort(key=lambda t: -t[1])
    print("耗时前5：", ", ".join(f"{n}={t:.2f}s" for n, t in timings[:5]))
    return 0 if total_ok == len(SUITES) else 1


if __name__ == "__main__":
    sys.exit(main())
