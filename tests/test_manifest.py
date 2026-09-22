"""见 ../AGENTS.md 测试纪律：新功能必须带测试用例。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.manifest import ManifestError, load_manifest, validate_manifest, version_satisfies

VALID_TOML = """
id = "test-plugin"
name = "测试插件"
version = "0.1.0"
api_version = ">=0.1,<0.2"

[provides]
demo = "multi"

[requires]

[runtime]
kind = "in_process"
entry = "pkg.module:Cls"

[permissions]
network = false
"""


class TestVersionSatisfies(unittest.TestCase):
    def test_range_ok(self):
        self.assertTrue(version_satisfies("0.1.0", ">=0.1,<0.2"))

    def test_range_fail_upper(self):
        self.assertFalse(version_satisfies("0.2.0", ">=0.1,<0.2"))

    def test_range_fail_lower(self):
        self.assertFalse(version_satisfies("0.0.9", ">=0.1,<0.2"))

    def test_bad_spec_raises(self):
        with self.assertRaises(ManifestError):
            version_satisfies("0.1.0", "not-a-range")


class TestLoadManifest(unittest.TestCase):
    def _write(self, tmp: Path, content: str) -> Path:
        plugin_dir = tmp / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.toml").write_text(content, encoding="utf-8")
        return plugin_dir

    def test_valid_manifest_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = self._write(Path(tmp), VALID_TOML)
            manifest = load_manifest(plugin_dir)
            self.assertEqual(manifest.id, "test-plugin")
            self.assertEqual(manifest.runtime.kind, "in_process")
            self.assertEqual(manifest.provides.get("demo"), "multi")

    def test_missing_file_raises_manifest_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_dir = Path(tmp) / "empty"
            empty_dir.mkdir()
            with self.assertRaises(ManifestError):
                load_manifest(empty_dir)

    def test_malformed_toml_raises_manifest_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = self._write(Path(tmp), "not valid = = toml [[[")
            with self.assertRaises(ManifestError):
                load_manifest(plugin_dir)

    def test_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = self._write(Path(tmp), 'id = "x"\n')
            with self.assertRaises(ManifestError):
                load_manifest(plugin_dir)


class TestValidateManifest(unittest.TestCase):
    def _load(self, tmp_str: str, content: str):
        plugin_dir = Path(tmp_str) / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.toml").write_text(content, encoding="utf-8")
        return load_manifest(plugin_dir)

    def test_valid_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._load(tmp, VALID_TOML)
            self.assertEqual(validate_manifest(manifest), [])

    def test_incompatible_api_version_fails(self):
        bad = VALID_TOML.replace(">=0.1,<0.2", ">=9.0,<10.0")
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._load(tmp, bad)
            errors = validate_manifest(manifest)
            self.assertTrue(any("api_version" in e for e in errors))

    def test_in_process_without_entry_fails(self):
        bad = VALID_TOML.replace('entry = "pkg.module:Cls"', "")
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._load(tmp, bad)
            errors = validate_manifest(manifest)
            self.assertTrue(any("entry" in e for e in errors))

    def test_unknown_runtime_kind_fails(self):
        bad = VALID_TOML.replace('kind = "in_process"', 'kind = "teleport"')
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._load(tmp, bad)
            errors = validate_manifest(manifest)
            self.assertTrue(any("runtime.kind" in e for e in errors))

    def test_provides_value_must_be_singleton_or_multi(self):
        bad = VALID_TOML.replace('demo = "multi"', "demo = true")
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._load(tmp, bad)
            errors = validate_manifest(manifest)
            self.assertTrue(any("provides.demo" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
