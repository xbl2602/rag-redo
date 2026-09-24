from __future__ import annotations

import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import unittest  # noqa: E402

from official_import_export.archive import (  # noqa: E402
    ARCHIVE_FORMAT_VERSION,
    ArchiveFormatError,
    pack,
    unpack,
)


class TestPackUnpackRoundTrip(unittest.TestCase):
    def test_round_trip_preserves_all_three_sections(self):
        manifest = {"library_id": "lib1", "name": "工作笔记"}
        vectors = {"c1": {"document": "文本", "metadata": {"path": "a.md"}, "embedding": [1.0, 0.0]}}
        bm25 = {"k1": 1.5, "b": 0.75, "doc_lengths": {"c1": 3}, "doc_tokens_cache": {"c1": {"文本": 1}}}

        data = pack(manifest, vectors, bm25)
        result = unpack(data)

        self.assertEqual(result["manifest"]["library_id"], "lib1")
        self.assertEqual(result["manifest"]["name"], "工作笔记")
        self.assertEqual(result["vectors"], vectors)
        self.assertEqual(result["bm25"], bm25)

    def test_pack_stamps_format_version(self):
        data = pack({}, {}, {})
        result = unpack(data)
        self.assertEqual(result["manifest"]["archive_format_version"], ARCHIVE_FORMAT_VERSION)

    def test_pack_returns_real_zip_bytes(self):
        """不是随便拼字节——确认产物本身就是一个合法 zip，用户在文件管理器
        双击应该能直接看到里面的三个 json 文件（见模块 docstring 的
        "目标用户不懂命令行"理由）。"""
        data = pack({"library_id": "lib1"}, {}, {})
        zf = zipfile.ZipFile(BytesIO(data))
        self.assertEqual(set(zf.namelist()), {"manifest.json", "vectors.json", "bm25.json"})

    def test_optional_index_state_sections_round_trip(self):
        data = pack(
            {"library_id": "lib1"},
            {},
            {},
            index_manifest={"files": {"a.md": {"status": "terminal"}}},
            extracted={"a.pdf": [{"text": "正文", "route": "ocr"}]},
            relations={"a.md": ["b.md"]},
            failures={"succeeded": 0, "failures": [{"path": "a.md", "reason": "empty"}]},
        )
        result = unpack(data)
        self.assertEqual(result["index_manifest"]["files"]["a.md"]["status"], "terminal")
        self.assertEqual(result["extracted"]["a.pdf"][0]["text"], "正文")
        self.assertEqual(result["relations"], {"a.md": ["b.md"]})
        self.assertEqual(result["failures"]["failures"][0]["reason"], "empty")

    def test_empty_vectors_and_bm25_round_trip(self):
        data = pack({"library_id": "lib1"}, {}, {})
        result = unpack(data)
        self.assertEqual(result["vectors"], {})
        self.assertEqual(result["bm25"], {})


class TestUnpackErrors(unittest.TestCase):
    def test_not_a_zip_raises_archive_format_error(self):
        with self.assertRaises(ArchiveFormatError):
            unpack(b"this is definitely not a zip file")

    def test_missing_entry_raises_archive_format_error(self):
        buf = BytesIO()
        with zipfile.ZipFile(buf, mode="w") as zf:
            zf.writestr("manifest.json", json.dumps({"archive_format_version": 1}))
            # 故意不写 vectors.json / bm25.json
        with self.assertRaises(ArchiveFormatError):
            unpack(buf.getvalue())

    def test_corrupted_json_raises_archive_format_error(self):
        buf = BytesIO()
        with zipfile.ZipFile(buf, mode="w") as zf:
            zf.writestr("manifest.json", "{not valid json")
            zf.writestr("vectors.json", "{}")
            zf.writestr("bm25.json", "{}")
        with self.assertRaises(ArchiveFormatError):
            unpack(buf.getvalue())

    def test_future_format_version_raises_clear_error(self):
        """真实场景：用户拿新版本软件导出的归档去导一份旧版本软件——不该
        读出一份错位的半成品数据，要能说清楚"版本太新，升级软件"。"""
        buf = BytesIO()
        with zipfile.ZipFile(buf, mode="w") as zf:
            zf.writestr("manifest.json", json.dumps({"archive_format_version": ARCHIVE_FORMAT_VERSION + 1}))
            zf.writestr("vectors.json", "{}")
            zf.writestr("bm25.json", "{}")
        with self.assertRaises(ArchiveFormatError):
            unpack(buf.getvalue())

    def test_missing_format_version_raises_clear_error(self):
        buf = BytesIO()
        with zipfile.ZipFile(buf, mode="w") as zf:
            zf.writestr("manifest.json", json.dumps({"library_id": "lib1"}))
            zf.writestr("vectors.json", "{}")
            zf.writestr("bm25.json", "{}")
        with self.assertRaises(ArchiveFormatError):
            unpack(buf.getvalue())


if __name__ == "__main__":
    unittest.main()
