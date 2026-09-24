from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent.parent
PLUGIN_DIR = Path(__file__).parent.parent
for path in (REPO_ROOT, PLUGIN_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core.runtime import PluginRuntime, PluginState  # noqa: E402
from official_query_enhancer_hyde.hyde import OpenAiCompatibleHydeClient  # noqa: E402
from official_query_enhancer_hyde.plugin import PLUGIN_ID  # noqa: E402


class _FakeClient:
    def __init__(self, response: str = "假设文档正文") -> None:
        self.response = response
        self.calls: list[dict] = []

    def complete(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.response


class _FailingClient:
    def complete(self, prompt: str, **kwargs):
        raise RuntimeError("secret-token-must-not-leak")


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class TestHydePlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._environment = {
            key: os.environ.pop(key, None)
            for key in (
                "RAG_REDO_HYDE_LLM_URL",
                "RAG_REDO_HYDE_LLM_MODEL",
                "RAG_REDO_HYDE_LLM_API_KEY",
            )
        }
        self.addCleanup(self._restore_environment)
        self.runtime = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.runtime.scan()
        self.runtime.load(PLUGIN_ID)
        self.assertEqual(self.runtime.plugins[PLUGIN_ID].state, PluginState.LOADED)
        self.runtime.enable(PLUGIN_ID)
        self.assertEqual(self.runtime.plugins[PLUGIN_ID].state, PluginState.ENABLED)
        self.instance = self.runtime.plugins[PLUGIN_ID].instance
        self.addCleanup(self.runtime.disable, PLUGIN_ID)

    def _restore_environment(self) -> None:
        for key, value in self._environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_disabled_by_default(self) -> None:
        self.assertFalse(self.instance.should_enhance("能力", 0.1))

    def test_enabled_only_for_low_confidence(self) -> None:
        self.runtime.settings.set("hyde_enabled", True)
        self.runtime.settings.set("hyde_min_confidence", 0.5)
        self.assertTrue(self.instance.should_enhance("能力", 0.49))
        self.assertFalse(self.instance.should_enhance("能力", 0.5))
        self.assertTrue(self.instance.should_enhance("能力", None))

    def test_enhance_returns_contract_and_passes_old_defaults(self) -> None:
        self.runtime.settings.set("hyde_enabled", True)
        fake = _FakeClient()
        self.instance._client = fake
        expansion = self.instance.enhance("个人能力")
        self.assertEqual(expansion.query, "假设文档正文")
        self.assertEqual(expansion.expanded_by, PLUGIN_ID)
        self.assertEqual(expansion.reason, "low_confidence_hyde")
        self.assertIn("查询：个人能力", fake.calls[0]["prompt"])
        self.assertEqual(fake.calls[0]["url"], "http://localhost:1234/v1/chat/completions")
        self.assertEqual(fake.calls[0]["model"], "qwen2.5-3b-instruct")
        self.assertEqual(fake.calls[0]["timeout"], 30.0)
        self.assertEqual(fake.calls[0]["max_tokens"], 200)

    def test_failure_returns_none_without_exposing_error(self) -> None:
        self.runtime.settings.set("hyde_enabled", True)
        self.instance._client = _FailingClient()
        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(self.instance.enhance("个人能力"))
        self.assertNotIn("secret-token-must-not-leak", "\n".join(logs.output))

    def test_real_client_builds_openai_compatible_request(self) -> None:
        response = _Response({"choices": [{"message": {"content": " 假设答案 "}}]})
        with patch(
            "official_query_enhancer_hyde.hyde.urllib.request.urlopen",
            return_value=response,
        ) as urlopen:
            text = OpenAiCompatibleHydeClient().complete(
                "prompt",
                url="http://localhost:1234/v1/chat/completions",
                model="test-model",
                api_key="secret",
                timeout=30.0,
                max_tokens=200,
            )
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(text, "假设答案")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["max_tokens"], 200)
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")


if __name__ == "__main__":
    unittest.main()
