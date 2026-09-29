"""MinerU 云端 OCR 的密钥/模型版本来源（BC-15/BC-01，2026-09-29）。

旧项目 `obsidian-rag/gui/config_editor.py` 的设置页有 `mineru_api_key`（保密字段）与
`mineru_model_version` 两项，云端提取读的就是它们（`extractors.py:1239`）。rag-redo 此前
只认环境变量 `MINERU_API_KEY` / `MINERU_MODEL_VERSION`——只用界面的人没有地方填 Key。
现在：设置页的值优先，环境变量兜底（不破坏此前靠环境变量配置的用法）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for _p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.runtime import PluginRuntime, PluginState  # noqa: E402
from core.settings import SettingsStore  # noqa: E402
from official_ocr_mineru_cloud.extract import MineruCloudExtractor  # noqa: E402
from official_ocr_mineru_cloud.ocr import (  # noqa: E402
    MineruCloudError,
    _RealHttpClient,
    resolve_api_key,
    resolve_model_version,
)

SENTINEL = "sk-SETTINGS-KEY-DO-NOT-LEAK-0123456789"


class _EnvIsolated(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {k: os.environ.pop(k, None) for k in ("MINERU_API_KEY", "MINERU_MODEL_VERSION")}
        self.addCleanup(self._restore_env)
        self.settings = SettingsStore(self.tmp / "settings.json")

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestResolveKeyAndModelVersion(_EnvIsolated):
    def test_no_key_anywhere_is_empty(self) -> None:
        self.assertEqual(resolve_api_key(self.settings), "")
        self.assertEqual(resolve_api_key(None), "")

    def test_key_from_settings_needs_no_environment_variable(self) -> None:
        self.settings.set("mineru_api_key", SENTINEL)
        self.assertEqual(resolve_api_key(self.settings), SENTINEL)

    def test_environment_variable_is_the_fallback(self) -> None:
        os.environ["MINERU_API_KEY"] = "env-key"
        self.assertEqual(resolve_api_key(self.settings), "env-key")
        self.assertEqual(resolve_api_key(None), "env-key")

    def test_settings_win_over_environment_and_blank_settings_fall_through(self) -> None:
        os.environ["MINERU_API_KEY"] = "env-key"
        self.settings.set("mineru_api_key", "  settings-key  ")
        self.assertEqual(resolve_api_key(self.settings), "settings-key")
        self.settings.set("mineru_api_key", "   ")
        self.assertEqual(resolve_api_key(self.settings), "env-key")

    def test_model_version_defaults_to_vlm_and_follows_settings_then_environment(self) -> None:
        self.assertEqual(resolve_model_version(self.settings), "vlm")
        os.environ["MINERU_MODEL_VERSION"] = "pipeline"
        self.assertEqual(resolve_model_version(self.settings), "pipeline")
        del os.environ["MINERU_MODEL_VERSION"]
        self.settings.set("mineru_model_version", "pipeline")
        self.assertEqual(resolve_model_version(self.settings), "pipeline")
        os.environ["MINERU_MODEL_VERSION"] = "other"
        self.assertEqual(resolve_model_version(self.settings), "pipeline")  # 设置页显式选的优先


class TestRealClientUsesTheResolvedKey(_EnvIsolated):
    def _capture_submit(self, client: _RealHttpClient) -> dict:
        seen: dict = {}

        def fake_json_request(request, timeout):
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data.decode("utf-8"))
            return {"code": 0, "data": {"batch_id": "b1", "file_urls": ["http://upload.invalid/x"]}}

        client._json_request = fake_json_request  # type: ignore[method-assign]
        client.submit(b"%PDF-1.4", "a.pdf", is_ocr=True)
        return seen

    def test_key_only_in_settings_is_sent_as_the_bearer_token(self) -> None:
        self.settings.set("mineru_api_key", SENTINEL)
        client = _RealHttpClient(settings=self.settings, rate_per_minute=0)
        self.assertTrue(client.has_key())
        self.assertEqual(self._capture_submit(client)["auth"], f"Bearer {SENTINEL}")

    def test_settings_key_is_re_read_on_every_call_so_edits_apply_without_restart(self) -> None:
        client = _RealHttpClient(settings=self.settings, rate_per_minute=0)
        self.assertFalse(client.has_key())
        self.settings.set("mineru_api_key", SENTINEL)
        self.assertTrue(client.has_key())

    def test_model_version_from_settings_goes_into_the_request_body(self) -> None:
        self.settings.set("mineru_api_key", SENTINEL)
        self.settings.set("mineru_model_version", "pipeline")
        client = _RealHttpClient(settings=self.settings, rate_per_minute=0)
        self.assertEqual(self._capture_submit(client)["body"]["config"]["mineru_model_version"], "pipeline")

    def test_without_any_key_the_error_names_both_places_and_never_a_value(self) -> None:
        client = _RealHttpClient(settings=self.settings, rate_per_minute=0)
        with self.assertRaises(MineruCloudError) as ctx:
            client.submit(b"%PDF-1.4", "a.pdf", is_ocr=True)
        text = str(ctx.exception)
        self.assertIn("设置", text)
        self.assertIn("MINERU_API_KEY", text)

    def test_poll_uses_the_settings_key_too(self) -> None:
        self.settings.set("mineru_api_key", SENTINEL)
        client = _RealHttpClient(settings=self.settings, rate_per_minute=0)
        seen: dict = {}

        def fake_json_request(request, timeout):
            seen["auth"] = request.get_header("Authorization")
            return {"code": 0, "data": {"extract_result": [{"state": "failed", "err_msg": "x"}]}}

        client._json_request = fake_json_request  # type: ignore[method-assign]
        try:
            client.poll("b1", timeout=1.0)
        except MineruCloudError:
            pass
        self.assertEqual(seen.get("auth"), f"Bearer {SENTINEL}")


class TestExtractorWithSettingsKey(_EnvIsolated):
    def _pdf(self) -> Path:
        path = self.tmp / "scan.pdf"
        path.write_bytes(b"%PDF-1.4 fake scan")
        return path

    def test_without_a_key_the_scan_stays_scanned_and_never_touches_the_network(self) -> None:
        extractor = MineruCloudExtractor(settings=self.settings)
        with mock.patch.object(_RealHttpClient, "submit", side_effect=AssertionError("must not submit")):
            doc = extractor.extract("lib", "scan.pdf", self._pdf().parent)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "scanned")

    def test_key_only_in_settings_lets_the_extractor_reach_the_cloud(self) -> None:
        self.settings.set("mineru_api_key", SENTINEL)
        extractor = MineruCloudExtractor(settings=self.settings)
        self._pdf()
        with mock.patch.object(_RealHttpClient, "submit", side_effect=MineruCloudError("boom")) as submit:
            doc = extractor.extract("lib", "scan.pdf", self.tmp)
        submit.assert_called()
        self.assertNotEqual(doc.failure_reason, "scanned")
        self.assertNotIn(SENTINEL, doc.failure_reason or "")


class TestPluginSignatureFollowsTheKey(_EnvIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.rt = PluginRuntime(
            _REPO_ROOT / "plugins", state_file=self.tmp / "plugins_state.json", data_dir=self.tmp / "data"
        )
        self.rt.scan()
        self.rt.load("official-ocr-mineru-cloud")
        self.rt.enable("official-ocr-mineru-cloud")
        self.assertEqual(self.rt.plugins["official-ocr-mineru-cloud"].state, PluginState.ENABLED)
        self.addCleanup(self.rt.close)
        self.plugin = self.rt.plugins["official-ocr-mineru-cloud"].instance

    def test_signature_flips_from_nokey_to_key_when_the_settings_key_is_filled_in(self) -> None:
        before = self.plugin.index_signature()
        self.assertTrue(before.endswith(":nokey"), before)
        self.rt.settings.set("mineru_api_key", SENTINEL)
        after = self.plugin.index_signature()
        self.assertTrue(after.endswith(":key"), after)
        self.assertNotIn(SENTINEL, after)

    def test_the_real_client_built_by_the_plugin_reads_the_runtime_settings(self) -> None:
        self.assertFalse(self.plugin.extractor._client.has_key())
        self.rt.settings.set("mineru_api_key", SENTINEL)
        self.assertTrue(self.plugin.extractor._client.has_key())


if __name__ == "__main__":
    unittest.main()
