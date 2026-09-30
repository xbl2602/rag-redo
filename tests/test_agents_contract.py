from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
CONTRACT_PATH = REPO_ROOT / "docs" / "behavior_contract.json"
AGENTS_PATH = REPO_ROOT / "AGENTS.md"
CLAUDE_PATH = REPO_ROOT / "CLAUDE.md"
EXPECTED_IDS = [f"BC-{index:02d}" for index in range(1, 21)]
ALLOWED_STATUSES = {"blocked", "partial", "pass"}
_DEFS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def check_test_ref(ref: str, repo_root: Path = REPO_ROOT) -> str | None:
    """校验一条 `test_refs`：`[rag-redo/]<相对路径>::<符号>` 必须指向**真实存在**的测试。

    符号可以是 `Class`、`Class.method` 或顶层 `function`：文件必须存在，符号必须在文件里
    **真的被定义**（按 AST 查，不靠字符串子串），末段必须像个测试（`Test*` / `test*`）。
    返回 `None` = 合法，否则是一句说明问题的话。

    **为什么门禁要查这个**（2026-09-28 审计 M-6）：此前只查"`test_refs` 非空且含 `::`"，于是
    BC-08 引用了写错类名的方法、BC-09 引用了一个已经被删掉的类，契约照样标 `pass`、门禁
    全绿——`pass` 只是格式合规，背后已经没有测试在守这条契约。
    """
    path_text, _, symbol = ref.partition("::")
    if not symbol:
        return "缺少 `::符号`"
    relative = path_text[len("rag-redo/"):] if path_text.startswith("rag-redo/") else path_text
    path = repo_root / relative
    if not path.is_file():
        return f"文件不存在：{relative}"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return f"无法解析 {relative}：{exc}"
    parts = symbol.split(".")
    scope: list[ast.AST] = list(tree.body)
    node: ast.AST | None = None
    walked: list[str] = []
    for name in parts:
        node = next((n for n in scope if isinstance(n, _DEFS) and n.name == name), None)
        if node is None:
            where = f"{relative} 的 {'.'.join(walked)}" if walked else relative
            return f"{where} 里没有定义 {name}"
        walked.append(name)
        scope = list(node.body) if isinstance(node, ast.ClassDef) else []
    if not (parts[-1].startswith("Test") or parts[-1].startswith("test")):
        return f"{symbol} 不像一个测试（应以 Test/test 开头）"
    return None


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

    def test_every_test_ref_points_at_a_test_that_really_exists(self) -> None:
        """`pass` 必须有真实的测试守着：每条 `test_refs` 指向的文件、类、方法都得存在。"""
        dangling: list[str] = []
        for item in self.contracts:
            for ref in item["test_refs"]:
                problem = check_test_ref(ref)
                if problem is not None:
                    dangling.append(f"{item['id']}: {ref} —— {problem}")
        self.assertEqual(dangling, [], "契约引用了不存在的测试：\n" + "\n".join(dangling))

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


class TestCheckTestRef(unittest.TestCase):
    """门禁自测：一条失效引用必须让门禁转红（否则门禁本身就是摆设）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        (self.tmp / "tests").mkdir()
        (self.tmp / "tests" / "test_sample.py").write_text(
            "import unittest\n"
            "class TestSample(unittest.TestCase):\n"
            "    def test_real(self):\n        pass\n"
            "    async def test_async_real(self):\n        pass\n"
            "    def helper(self):\n        pass\n"
            "    class TestNested:\n        def test_deep(self):\n            pass\n"
            "def test_top_level():\n    pass\n"
            "def not_a_test():\n    pass\n",
            encoding="utf-8",
        )

    def _check(self, ref: str) -> str | None:
        return check_test_ref(ref, self.tmp)

    def test_valid_refs_pass(self) -> None:
        for ref in (
            "rag-redo/tests/test_sample.py::TestSample",
            "rag-redo/tests/test_sample.py::TestSample.test_real",
            "tests/test_sample.py::TestSample.test_async_real",
            "tests/test_sample.py::TestSample.TestNested.test_deep",
            "tests/test_sample.py::test_top_level",
        ):
            with self.subTest(ref=ref):
                self.assertIsNone(self._check(ref))

    def test_dangling_refs_are_reported(self) -> None:
        cases = {
            "tests/no_such_file.py::TestSample": "文件不存在",
            "tests/test_sample.py::TestRemoved": "没有定义 TestRemoved",
            "tests/test_sample.py::TestSample.test_missing": "没有定义 test_missing",
            # 方法挂在了另一个类下面（BC-08 的真实病例：TestMcpToolsAsyncBase.test_… 实际定义在子类里）
            "tests/test_sample.py::TestOther.test_real": "没有定义 TestOther",
            "tests/test_sample.py::TestSample.test_real.inner": "没有定义 inner",
            "tests/test_sample.py::TestSample.helper": "不像一个测试",
            "tests/test_sample.py::not_a_test": "不像一个测试",
            "tests/test_sample.py": "缺少",
        }
        for ref, expected in cases.items():
            with self.subTest(ref=ref):
                problem = self._check(ref)
                self.assertIsNotNone(problem, f"失效引用没被门禁发现：{ref}")
                self.assertIn(expected, problem)

    def test_a_broken_ref_in_the_manifest_would_turn_the_gate_red(self) -> None:
        """把真实契约里的一条引用故意写坏，`check_test_ref` 必须报出来。"""
        contracts = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["contracts"]
        good = contracts[0]["test_refs"][0]
        self.assertIsNone(check_test_ref(good))
        broken = good.replace("::", "::Renamed", 1)
        self.assertIsNotNone(check_test_ref(broken))


if __name__ == "__main__":
    unittest.main()
