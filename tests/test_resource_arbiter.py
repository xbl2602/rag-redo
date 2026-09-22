from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.resource_arbiter import ResourceArbiter


class TestResourceArbiter(unittest.TestCase):
    def test_first_acquire_succeeds(self):
        arb = ResourceArbiter()
        self.assertTrue(arb.acquire("gpu:0", "holder-a", priority=1))
        self.assertEqual(arb.holder_of("gpu:0"), "holder-a")

    def test_lower_priority_cannot_preempt(self):
        arb = ResourceArbiter()
        arb.acquire("gpu:0", "holder-a", priority=5)
        self.assertFalse(arb.acquire("gpu:0", "holder-b", priority=1))
        self.assertEqual(arb.holder_of("gpu:0"), "holder-a")

    def test_higher_priority_preempts_and_calls_callback(self):
        arb = ResourceArbiter()
        preempted = []
        arb.acquire("gpu:0", "holder-a", priority=1, on_preempt=lambda: preempted.append("a"))
        self.assertTrue(arb.acquire("gpu:0", "holder-b", priority=5))
        self.assertEqual(arb.holder_of("gpu:0"), "holder-b")
        self.assertEqual(preempted, ["a"])

    def test_release_frees_resource(self):
        arb = ResourceArbiter()
        arb.acquire("gpu:0", "holder-a", priority=1)
        arb.release("gpu:0", "holder-a")
        self.assertIsNone(arb.holder_of("gpu:0"))

    def test_release_by_non_holder_is_noop(self):
        arb = ResourceArbiter()
        arb.acquire("gpu:0", "holder-a", priority=1)
        arb.release("gpu:0", "holder-b")
        self.assertEqual(arb.holder_of("gpu:0"), "holder-a")

    def test_reacquire_by_same_holder_is_idempotent(self):
        arb = ResourceArbiter()
        arb.acquire("gpu:0", "holder-a", priority=1)
        self.assertTrue(arb.acquire("gpu:0", "holder-a", priority=1))

    def test_probe_fail_open_on_exception(self):
        def broken_check():
            raise RuntimeError("探测失败")

        self.assertTrue(ResourceArbiter.probe(broken_check))

    def test_probe_returns_actual_result_when_no_exception(self):
        self.assertFalse(ResourceArbiter.probe(lambda: False))
        self.assertTrue(ResourceArbiter.probe(lambda: True))

    def test_full_acquire_preempt_release_flow(self):
        """完整流程：A拿到→B抢占A→B释放→资源空闲，对应 ROADMAP.md Phase 0
        验收标准"资源仲裁器能演示一次申请锁→抢占→释放的完整流程"。"""
        arb = ResourceArbiter()
        preempted = []
        self.assertTrue(
            arb.acquire("gpu:0", "plugin-a", priority=1, on_preempt=lambda: preempted.append("a-evicted"))
        )
        self.assertTrue(arb.acquire("gpu:0", "plugin-b", priority=9))
        self.assertEqual(preempted, ["a-evicted"])
        self.assertEqual(arb.holder_of("gpu:0"), "plugin-b")
        arb.release("gpu:0", "plugin-b")
        self.assertIsNone(arb.holder_of("gpu:0"))


if __name__ == "__main__":
    unittest.main()
