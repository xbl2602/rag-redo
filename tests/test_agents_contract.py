from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
CONTRACT_PATH = REPO_ROOT / "docs" / "behavior_contract.json"
AGENTS_PATH = REPO_ROOT / "AGENTS.md"
CLAUDE_PATH = REPO_ROOT / "CLAUDE.md"
EXPECTED_IDS = [f"BC-{index:02d}" for index in range(1, 15)]
ALLOWED_STATUSES = {"blocked", "partial", "pass"}


def _body(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("## 1."):
            return "\n".join(lines[index:])
    raise AssertionError(f"找不到规范正文起始位置: {path}")


class TestAgentsContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract_data = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        cls.contracts = cls.contract_data["contracts"]
        cls.agents_body = _body(AGENTS_PATH)
        cls.claude_body = _body(CLAUDE_PATH)

    def test_agents_and_claude_body_are_synchronized(self) -> None:
        self.assertEqual(self.agents_body, self.claude_body)

    def test_contract_manifest_shape(self) -> None:
        self.assertEqual(self.contract_data["format_version"], 1)
        self.assertEqual(self.contract_data["policy"], "strict-behavior-compatibility")
        self.assertEqual([item["id"] for item in self.contracts], EXPECTED_IDS)

    def test_contracts_have_unique_ids_and_explicit_status(self) -> None:
        ids = [item["id"] for item in self.contracts]
        self.assertEqual(len(ids), len(set(ids)))
        for item in self.contracts:
            self.assertIn(item["status"], ALLOWED_STATUSES)
            if item["status"] == "blocked":
                self.assertTrue(item["blocker"].strip())
            if item["status"] == "pass":
                self.assertFalse(item["blocker"].strip())

    def test_every_contract_has_legacy_redo_and_test_references(self) -> None:
        for item in self.contracts:
            with self.subTest(contract=item["id"]):
                self.assertTrue(item["legacy_refs"])
                self.assertTrue(item["redo_refs"])
                self.assertTrue(item["test_refs"])
                self.assertTrue(all(ref.startswith("obsidian-rag/") for ref in item["legacy_refs"]))
                self.assertTrue(all(ref.startswith("rag-redo/") for ref in item["redo_refs"]))
                self.assertTrue(all("::" in ref for ref in item["test_refs"]))
                self.assertTrue(any(ref.endswith(".py") or ".py:" in ref for ref in item["legacy_refs"]))
                self.assertTrue(any(ref.endswith(".py") or ".py:" in ref for ref in item["redo_refs"]))

    def test_all_contract_ids_are_referenced_by_normative_document(self) -> None:
        for contract_id in EXPECTED_IDS:
            with self.subTest(contract=contract_id):
                self.assertIn(contract_id, self.agents_body)

    def test_normative_document_does_not_contain_progress_state(self) -> None:
        self.assertNotIn("## 当前状态", self.agents_body)
        self.assertNotIn("## 当前状态", self.claude_body)
        self.assertIn("docs/ROADMAP.md", self.agents_body)
        self.assertIn("docs/behavior_contract.json", self.agents_body)

    def test_blocked_contracts_are_not_silently_marked_complete(self) -> None:
        blocked = [item for item in self.contracts if item["status"] == "blocked"]
        for item in blocked:
            self.assertIn(item["blocker"].strip(), self.agents_body + "\n" + self.claude_body + "\n" + json.dumps(self.contract_data, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
