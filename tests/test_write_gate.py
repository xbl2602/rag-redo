"""见 ../AGENTS.md 测试纪律。覆盖"提案号+确认码+TTL+一次性"两段式确认
的每一条规则，这是继承自旧项目 selection_gate.py/summary_gate.py 验证
过的模式（见 docs/ARCHITECTURE.md 2.2节、docs/LESSONS.md）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.write_gate import WriteGate, WriteGateError


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestWriteGate(unittest.TestCase):
    def test_propose_then_confirm_with_correct_code_succeeds(self):
        gate = WriteGate()
        ticket = gate.propose("覆盖库摘要", {"library_id": "lib1", "text": "新摘要"})
        payload = gate.confirm(ticket.proposal_id, ticket.confirmation_code)
        self.assertEqual(payload, {"library_id": "lib1", "text": "新摘要"})

    def test_wrong_code_raises(self):
        gate = WriteGate()
        ticket = gate.propose("action", {"x": 1})
        with self.assertRaises(WriteGateError):
            gate.confirm(ticket.proposal_id, "000000" if ticket.confirmation_code != "000000" else "111111")

    def test_confirm_is_one_shot(self):
        gate = WriteGate()
        ticket = gate.propose("action", {"x": 1})
        gate.confirm(ticket.proposal_id, ticket.confirmation_code)
        with self.assertRaises(WriteGateError):
            gate.confirm(ticket.proposal_id, ticket.confirmation_code)

    def test_unknown_proposal_id_raises(self):
        gate = WriteGate()
        with self.assertRaises(WriteGateError):
            gate.confirm("does-not-exist", "123456")

    def test_expired_proposal_raises(self):
        clock = _FakeClock()
        gate = WriteGate(clock=clock)
        ticket = gate.propose("action", {"x": 1}, ttl_seconds=60)
        clock.advance(61)
        with self.assertRaises(WriteGateError):
            gate.confirm(ticket.proposal_id, ticket.confirmation_code)

    def test_confirm_just_before_expiry_still_succeeds(self):
        clock = _FakeClock()
        gate = WriteGate(clock=clock)
        ticket = gate.propose("action", {"x": 1}, ttl_seconds=60)
        clock.advance(59)
        gate.confirm(ticket.proposal_id, ticket.confirmation_code)  # 不应该抛异常

    def test_pending_count_tracks_outstanding_proposals(self):
        gate = WriteGate()
        self.assertEqual(gate.pending_count(), 0)
        gate.propose("a", 1)
        gate.propose("b", 2)
        self.assertEqual(gate.pending_count(), 2)

    def test_confirm_removes_from_pending(self):
        gate = WriteGate()
        ticket = gate.propose("a", 1)
        gate.confirm(ticket.proposal_id, ticket.confirmation_code)
        self.assertEqual(gate.pending_count(), 0)

    def test_sweep_expired_removes_only_expired(self):
        clock = _FakeClock()
        gate = WriteGate(clock=clock)
        gate.propose("short-lived", 1, ttl_seconds=10)
        long_ticket = gate.propose("long-lived", 2, ttl_seconds=1000)
        clock.advance(11)
        removed = gate.sweep_expired()
        self.assertEqual(removed, 1)
        self.assertEqual(gate.pending_count(), 1)
        gate.confirm(long_ticket.proposal_id, long_ticket.confirmation_code)  # 仍然有效

    def test_different_proposals_get_different_codes_and_ids(self):
        gate = WriteGate()
        t1 = gate.propose("a", 1)
        t2 = gate.propose("a", 1)
        self.assertNotEqual(t1.proposal_id, t2.proposal_id)
        # 确认码理论上可能小概率相同（六位数字空间），但 proposal_id
        # 用 token_hex(8) 生成，碰撞概率可忽略，这里只断言 id 不同。

    def test_confirming_one_proposal_does_not_affect_another(self):
        gate = WriteGate()
        t1 = gate.propose("a", "payload-a")
        t2 = gate.propose("b", "payload-b")
        result1 = gate.confirm(t1.proposal_id, t1.confirmation_code)
        self.assertEqual(result1, "payload-a")
        result2 = gate.confirm(t2.proposal_id, t2.confirmation_code)
        self.assertEqual(result2, "payload-b")


if __name__ == "__main__":
    unittest.main()
