from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
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
    ArchiveEmptyLibraryError,
    ArchiveFormatError,
    import_plan,
    pack,
    unpack,
    verify,
)


def _manifest() -> dict:
    """一个最小但**合法**的库配置 manifest（verify 要求 library_id + name）。"""
    return {"library_id": "lib1", "name": "工作笔记"}


def _vectors() -> dict:
    """至少一块向量——pack 现在会对 0 块的库明确拒绝（对齐
    obsidian-rag/export.py:199-201），所以"能打成一个包"的前提就是有块。"""
    return {
        "lib1:a.md:0": {
            "document": "文本",
            "metadata": {"path": "a.md", "chunk_index": 0},
            "embedding": [1.0, 0.0],
        }
    }


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
        data = pack(_manifest(), _vectors(), {})
        result = unpack(data)
        self.assertEqual(result["manifest"]["archive_format_version"], ARCHIVE_FORMAT_VERSION)

    def test_pack_returns_real_zip_bytes(self):
        """不是随便拼字节——确认产物本身就是一个合法 zip，用户在文件管理器
        双击应该能直接看到里面的三个 json 文件（见模块 docstring 的
        "目标用户不懂命令行"理由）。"""
        data = pack(_manifest(), _vectors(), {})
        zf = zipfile.ZipFile(BytesIO(data))
        self.assertEqual(set(zf.namelist()), {"manifest.json", "vectors.json", "bm25.json"})

    def test_optional_index_state_sections_round_trip(self):
        data = pack(
            _manifest(),
            _vectors(),
            {},
            # a.md 有块（indexed）、empty.md 是终态失败：终态文件不产块，
            # 所以它不该出现在 index.json 的已索引块集合里（verify 会核对
            # vectors ↔ index.json 的块 id 互指关系）。
            index_manifest={
                "files": {
                    "a.md": {"status": "indexed", "chunk_ids": ["lib1:a.md:0"]},
                    "empty.md": {"status": "terminal"},
                }
            },
            extracted={"a.pdf": [{"text": "正文", "route": "ocr"}]},
            relations={"a.md": ["b.md"]},
            failures={"succeeded": 0, "failures": [{"path": "empty.md", "reason": "empty"}]},
        )
        result = unpack(data)
        self.assertEqual(result["index_manifest"]["files"]["empty.md"]["status"], "terminal")
        self.assertEqual(result["extracted"]["a.pdf"][0]["text"], "正文")
        self.assertEqual(result["relations"], {"a.md": ["b.md"]})
        self.assertEqual(result["failures"]["failures"][0]["reason"], "empty")

    def test_bm25_state_round_trips_with_vectors_present(self):
        data = pack(_manifest(), _vectors(), {})
        result = unpack(data)
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


class TestUnpackedSizeCap(unittest.TestCase):
    """A22：`unpack` 在内存里整块读条目，压缩炸弹必须在**读取任何条目之前**被拒绝。"""

    def _bomb(self, megabytes: int = 8) -> bytes:
        """几十 KB 的压缩包，声明解压后 `megabytes` MiB（全零，deflate 压得极小）。"""
        buf = BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps({"archive_format_version": 1}))
            zf.writestr("vectors.json", "{}")
            zf.writestr("bm25.json", "{}")
            zf.writestr("sources/huge.bin", b"\0" * (megabytes * 1024 * 1024))
        return buf.getvalue()

    def test_a_bomb_over_the_cap_is_refused_before_any_entry_is_read(self):
        from unittest.mock import patch

        from official_import_export import archive

        data = self._bomb(8)
        self.assertLess(len(data), 64 * 1024, "夹具应当是个真正的高压缩比小包")
        with patch.object(archive, "MAX_UNPACKED_BYTES", 1024 * 1024), \
                patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("超限的包不该读任何条目")):
            with self.assertRaises(ArchiveFormatError) as caught:
                unpack(data)
        error = caught.exception
        self.assertEqual(error.field, "uncompressed_size")
        self.assertGreater(error.actual, 1024 * 1024)
        self.assertIn("已在读取任何内容前拒绝", str(error))

    def test_a_normal_archive_is_unaffected_by_the_default_cap(self):
        from official_import_export import archive

        self.assertGreaterEqual(archive.MAX_UNPACKED_BYTES, 1024**3, "上限是防炸弹护栏，不能低到影响真实归档")
        result = unpack(pack(_manifest(), _vectors(), {}))
        self.assertEqual(result["manifest"]["library_id"], "lib1")

    def test_the_cap_is_on_the_declared_total_and_inclusive(self):
        from unittest.mock import patch

        from official_import_export import archive

        data = pack(_manifest(), _vectors(), {})  # 真实产物：能一路通过后面的 verify
        total = sum(i.file_size for i in zipfile.ZipFile(BytesIO(data)).infolist())
        with patch.object(archive, "MAX_UNPACKED_BYTES", total):
            self.assertEqual(unpack(data)["manifest"]["library_id"], "lib1")  # 恰好等于上限 → 放行
        with patch.object(archive, "MAX_UNPACKED_BYTES", total - 1), self.assertRaises(ArchiveFormatError) as caught:
            unpack(data)
        self.assertEqual(caught.exception.field, "uncompressed_size")


class TestZeroVectorLibraryRefused(unittest.TestCase):
    """缺陷 4：对"从未索引/0 块"的库必须明确报错。

    旧项目 obsidian-rag/export.py:199-201 是
    `if not ids: log("索引为空（0 块），跳过导出"); return None`，
    计数进失败数并让 main() 以退出码 1 结束——用户不会把一个空包当备份
    存下来。REDO 原来的 pack() 照收不误，CLI/MCP 于是回 ok=True。
    """

    def test_pack_refuses_library_with_zero_vectors(self):
        with self.assertRaises(ArchiveFormatError) as ctx:
            pack({"library_id": "lib-empty", "name": "空库"}, {}, {})
        self.assertIn("0 块", str(ctx.exception))
        self.assertIn("空库", str(ctx.exception))

    def test_refusal_is_a_distinct_exception_type_callers_can_tell_apart(self):
        """必须是独立异常类型（子类关系向下兼容）：调用方要能区分
        "这个库没东西可导"和"这个包/这份数据坏了"，不能都退化成一句
        "导出失败"。"""
        with self.assertRaises(ArchiveEmptyLibraryError):
            pack({"library_id": "lib-empty", "name": "空库"}, {}, {})
        self.assertTrue(issubclass(ArchiveEmptyLibraryError, ArchiveFormatError))

    def test_refusal_mentions_how_to_fix(self):
        with self.assertRaises(ArchiveFormatError) as ctx:
            pack({"library_id": "lib-empty", "name": "空库"}, {}, {})
        self.assertIn("index_library", str(ctx.exception))


class TestSourceFileInventory(unittest.TestCase):
    """缺陷 1：归档必须至少带上"这个库原本有哪些源文件 + 各自校验和"，
    并且把"归档不含笔记正文"写进自己产出的 manifest——MCP/GUI 的
    import_library 文档至今只说"归档不带路径"，从没说归档也不带正文，
    调用方没有任何依据提示用户。
    """

    _INDEX_MANIFEST = {
        "files": {
            "notes/a.md": {
                "size": 12,
                "mtime_ns": 1700000000,
                "content_hash": "a" * 64,
                "status": "indexed",
                "chunk_ids": ["lib1:notes/a.md:0"],
            },
            "empty.md": {
                "size": 0,
                "mtime_ns": 1700000001,
                "content_hash": "e" * 64,
                "status": "terminal",
                "failure_state": "empty",
                "chunk_ids": [],
            },
        }
    }

    def _pack(self):
        return pack(
            {"library_id": "lib1", "name": "工作笔记"},
            {"lib1:notes/a.md:0": {"embedding": [0.1, 0.2], "document": "正文", "metadata": {}}},
            {},
            index_manifest=self._INDEX_MANIFEST,
        )

    def test_manifest_records_every_source_file_with_sha256_and_size(self):
        result = unpack(self._pack())
        inventory = result["manifest"]["source_files"]
        self.assertEqual(set(inventory), {"notes/a.md", "empty.md"})
        self.assertEqual(inventory["notes/a.md"]["sha256"], "a" * 64)
        self.assertEqual(inventory["notes/a.md"]["size"], 12)
        self.assertEqual(inventory["notes/a.md"]["status"], "indexed")
        self.assertEqual(inventory["empty.md"]["status"], "terminal")

    def test_manifest_says_whether_bodies_are_actually_inside(self):
        """`source_files` 只是清单，`source_files_included` 才回答"正文在不在
        包里"。两者必须分开：清单永远有，正文默认没有——把这两件事混成
        一个字段就是缺陷 1 最初的样子（谁都读不出缺了什么）。"""
        manifest = unpack(self._pack())["manifest"]
        self.assertIs(manifest["source_files_included"], False)
        self.assertEqual(manifest["source_file_count"], 2)

    def test_manifest_carries_a_human_readable_notice_about_missing_bodies(self):
        """给调用方（MCP/GUI/CLI）一个可以直接展示给用户的句子——缺陷 1 的
        "任何一层都没有提示"，提示必须先存在于归档里。"""
        manifest = unpack(self._pack())["manifest"]
        notice = manifest["source_files_notice"]
        self.assertIn("正文", notice)
        self.assertIn("2", notice)

    def test_notice_is_recomputed_on_unpack_not_just_trusted_from_manifest(self):
        """unpack 必须按自己读到的清单重算提示，否则用户手改 manifest 就能
        把"包里没有正文"这条提示抹掉。"""
        import zipfile as _zipfile
        from io import BytesIO as _BytesIO

        data = self._pack()
        src = _zipfile.ZipFile(_BytesIO(data))
        entries = {n: src.read(n) for n in src.namelist()}
        entries["manifest.json"] = json.dumps(
            {**json.loads(entries["manifest.json"]), "source_files_notice": "包内一切正常"},
            ensure_ascii=False,
        ).encode("utf-8")
        buf = _BytesIO()
        with _zipfile.ZipFile(buf, mode="w", compression=_zipfile.ZIP_DEFLATED) as zf:
            for name, blob in entries.items():
                zf.writestr(name, blob)
        result = unpack(buf.getvalue())
        self.assertNotEqual(result["notices"][0], "包内一切正常")
        self.assertIn("正文", result["notices"][0])


class TestEntryIntegrity(unittest.TestCase):
    """缺陷 2：unpack 必须逐条目校验并给出可定位的错误。

    旧项目两道闸：obsidian-rag/export.py:142-175（导出方写完对交付物
    自校验，失败删包 raise）+ obsidian-rag/import.py:86-108
    （接收方解压后逐文件 sha256，任一不符即"中止，目标数据未被改动"）。
    """

    def _packed_entries(self):
        """先经过 pack() 产出的真包（带 entries_sha256 校验和），再把里面的
        条目抠出来——篡改测试必须建立在"包自己声明过校验和"的前提上，否则
        测的是另一回事。"""
        data = pack(
            {"library_id": "lib1", "name": "工作笔记"},
            {"lib1:a.md:0": {"document": "文本", "metadata": {"path": "a.md", "chunk_index": 0}, "embedding": [1.0, 0.0]}},
            {"k1": 1.5, "b": 0.75},
        )
        with zipfile.ZipFile(BytesIO(data)) as zf:
            return {name: zf.read(name) for name in zf.namelist()}

    def _good_entries(self):
        return {
            "manifest.json": json.dumps(
                {"library_id": "lib1", "name": "工作笔记", "archive_format_version": ARCHIVE_FORMAT_VERSION},
                ensure_ascii=False,
            ).encode("utf-8"),
            "vectors.json": json.dumps(
                {"lib1:a.md:0": {"document": "文本", "metadata": {"path": "a.md", "chunk_index": 0}, "embedding": [1.0, 0.0]}},
                ensure_ascii=False,
            ).encode("utf-8"),
            "bm25.json": json.dumps({"k1": 1.5, "b": 0.75, "doc_lengths": {}, "doc_tokens_cache": {}}, ensure_ascii=False).encode("utf-8"),
        }

    def _rebuild(self, entries, *, stored=False):
        buf = BytesIO()
        compression = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(buf, mode="w", compression=compression) as zf:
            for name, blob in entries.items():
                zf.writestr(name, blob)
        return buf.getvalue()

    def test_one_byte_tamper_inside_vectors_is_rejected_and_names_the_entry(self):
        entries = self._packed_entries()
        # 只改一个字节，且改完之后 JSON 仍然合法——这正是 sha256 闸门存在的
        # 理由：光靠"能不能解析"抓不到这种改动。
        entries["vectors.json"] = entries["vectors.json"].replace("文本".encode("utf-8"), "文夲".encode("utf-8"), 1)
        with self.assertRaises(ArchiveFormatError) as ctx:
            unpack(self._rebuild(entries))
        message = str(ctx.exception)
        self.assertIn("vectors.json", message)
        self.assertEqual(getattr(ctx.exception, "entry", None), "vectors.json")

    def test_tampered_entry_reports_expected_and_actual_checksum(self):
        entries = self._packed_entries()
        entries["vectors.json"] = entries["vectors.json"].replace("文本".encode("utf-8"), "文夲".encode("utf-8"), 1)
        with self.assertRaises(ArchiveFormatError) as ctx:
            unpack(self._rebuild(entries))
        self.assertTrue(getattr(ctx.exception, "expected", None))
        self.assertTrue(getattr(ctx.exception, "actual", None))
        self.assertNotEqual(ctx.exception.expected, ctx.exception.actual)

    def test_half_truncated_package_is_rejected(self):
        raw = self._rebuild(self._good_entries())
        with self.assertRaises(ArchiveFormatError) as ctx:
            unpack(raw[: len(raw) // 2])
        self.assertIn("zip", str(ctx.exception))

    def test_central_directory_truncated_package_is_rejected(self):
        """砍掉尾部中央目录：zip 仍能开但读不出条目/条目不完整——必须照样
        拒绝，不能读出一份"少了一半"的半成品。"""
        raw = self._rebuild(self._good_entries())
        with self.assertRaises(ArchiveFormatError):
            unpack(raw[: len(raw) - 60])

    def test_crc_mismatch_inside_stored_entry_is_rejected(self):
        """ZIP_STORED 下手工翻一个数据位 → CRC 校验失败。这条模拟真实传输
        损坏（旧项目 verify_package 的"解压触发 CRC"就是它）。"""
        raw = bytearray(self._rebuild(self._good_entries(), stored=True))
        with zipfile.ZipFile(BytesIO(bytes(raw))) as zf:
            info = zf.getinfo("bm25.json")
        payload_start = info.header_offset + 30 + len(info.filename.encode("utf-8")) + len(info.extra)
        raw[payload_start] ^= 0xFF
        with self.assertRaises(ArchiveFormatError) as ctx:
            unpack(bytes(raw))
        self.assertIn("bm25.json", str(ctx.exception))

    def test_missing_required_entry_error_lists_every_missing_name(self):
        entries = self._good_entries()
        del entries["bm25.json"]
        with self.assertRaises(ArchiveFormatError) as ctx:
            unpack(self._rebuild(entries))
        message = str(ctx.exception)
        self.assertIn("bm25.json", message)
        self.assertEqual(getattr(ctx.exception, "entry", None), "bm25.json")

    def test_corrupted_optional_entry_names_that_entry_not_a_generic_json_error(self):
        data = pack(
            {"library_id": "lib1", "name": "工作笔记"},
            {"lib1:a.md:0": {"document": "文本", "metadata": {"path": "a.md", "chunk_index": 0}, "embedding": [1.0, 0.0]}},
            {},
            relations={"a.md": ["b.md"]},
        )
        with zipfile.ZipFile(BytesIO(data)) as zf:
            entries = {name: zf.read(name) for name in zf.namelist()}
        entries["relations.json"] = b"{not json"
        with self.assertRaises(ArchiveFormatError) as ctx:
            unpack(self._rebuild(entries))
        self.assertIn("relations.json", str(ctx.exception))
        self.assertEqual(getattr(ctx.exception, "entry", None), "relations.json")

    def test_bad_vector_row_locates_the_offending_chunk_id(self):
        """坏向量行以前在 core/pipeline.py:2524-2525 被静默 continue 掉——
        归档里必须先自己报出来，并指名是哪个块。"""
        entries = self._good_entries()
        entries["vectors.json"] = json.dumps(
            {
                "lib1:a.md:0": {"document": "文本", "metadata": {"path": "a.md"}, "embedding": [1.0, 0.0]},
                "lib1:b.md:3": {"document": "坏行", "metadata": {"path": "b.md"}, "embedding": "不是向量"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        with self.assertRaises(ArchiveFormatError) as ctx:
            unpack(self._rebuild(entries))
        message = str(ctx.exception)
        self.assertIn("vectors.json", message)
        self.assertIn("lib1:b.md:3", message)
        self.assertEqual(getattr(ctx.exception, "entry", None), "vectors.json")

    def test_vector_row_without_embedding_is_rejected(self):
        entries = self._good_entries()
        entries["vectors.json"] = json.dumps(
            {"lib1:a.md:0": {"document": "文本", "metadata": {"path": "a.md"}}},
            ensure_ascii=False,
        ).encode("utf-8")
        with self.assertRaises(ArchiveFormatError) as ctx:
            unpack(self._rebuild(entries))
        self.assertIn("embedding", str(ctx.exception))

    def test_version_ahead_error_still_mentions_upgrade(self):
        entries = self._good_entries()
        manifest = json.loads(entries["manifest.json"])
        manifest["archive_format_version"] = ARCHIVE_FORMAT_VERSION + 1
        entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        with self.assertRaises(ArchiveFormatError) as ctx:
            unpack(self._rebuild(entries))
        self.assertIn("升级", str(ctx.exception))


class TestSourceBodiesPacking(unittest.TestCase):
    """缺陷 1 的另一半：旧项目确实把 vault 源文件打进包里
    （obsidian-rag/export.py:207-218 逐文件 sha256 + zf.write(fpath,
    arcname=f"vault/{rel}")），接收端 obsidian-rag/import.py:199-208
    place_vault 再落回 vault_export/<库名>/。REDO 侧要把这个能力补齐，
    同时保留一个明确的开关——笔记正文是隐私数据，包体积也是真实约束。
    """

    _SOURCES = {"notes/a.md": "笔记 A 正文".encode("utf-8"), "b.md": "笔记 B".encode("utf-8")}

    def test_sources_are_written_under_sources_prefix_when_provided(self):
        data = pack(
            {"library_id": "lib1", "name": "工作笔记"},
            {"lib1:notes/a.md:0": {"embedding": [0.1], "document": "正文", "metadata": {}}},
            {},
            index_manifest={"files": {"notes/a.md": {"size": 10, "content_hash": "x" * 64, "status": "indexed", "chunk_ids": ["lib1:notes/a.md:0"]}}},
            sources=self._SOURCES,
        )
        names = set(zipfile.ZipFile(BytesIO(data)).namelist())
        self.assertIn("sources/notes/a.md", names)
        self.assertIn("sources/b.md", names)

    def test_included_bodies_round_trip_through_unpack(self):
        data = pack(
            {"library_id": "lib1", "name": "工作笔记"},
            {"lib1:notes/a.md:0": {"embedding": [0.1], "document": "正文", "metadata": {}}},
            {},
            index_manifest={"files": {"notes/a.md": {"size": 10, "content_hash": "x" * 64, "status": "indexed", "chunk_ids": ["lib1:notes/a.md:0"]}}},
            sources=self._SOURCES,
        )
        result = unpack(data)
        self.assertEqual(result["sources"], self._SOURCES)
        self.assertIs(result["manifest"]["source_files_included"], True)
        self.assertIn("sources/notes/a.md", zipfile.ZipFile(BytesIO(data)).namelist())

    def test_included_bodies_are_hashed_and_checked_on_the_way_back(self):
        """包内带正文时，清单里的 sha256 必须就是**包内字节**的 sha256
        （不是源文件路径上的旧值）——否则接收端比对的是另一个东西。"""
        import hashlib

        data = pack(
            {"library_id": "lib1", "name": "工作笔记"},
            {"lib1:notes/a.md:0": {"embedding": [0.1], "document": "正文", "metadata": {}}},
            {},
            index_manifest={"files": {"notes/a.md": {"size": 10, "content_hash": "x" * 64, "status": "indexed", "chunk_ids": ["lib1:notes/a.md:0"]}}},
            sources=self._SOURCES,
        )
        manifest = unpack(data)["manifest"]
        self.assertEqual(
            manifest["source_files"]["notes/a.md"]["sha256"],
            hashlib.sha256(self._SOURCES["notes/a.md"]).hexdigest(),
        )
        self.assertTrue(manifest["source_files"]["notes/a.md"]["in_archive"])

    def test_switch_off_keeps_full_inventory_but_drops_bodies(self):
        """include_source_files=False：清单与校验和**照样**写全（这样至少
        还知道"缺了哪些文件、各自该是什么校验和"），只是不带正文。"""
        data = pack(
            {"library_id": "lib1", "name": "工作笔记"},
            {"lib1:notes/a.md:0": {"embedding": [0.1], "document": "正文", "metadata": {}}},
            {},
            index_manifest={"files": {"notes/a.md": {"size": 10, "content_hash": "x" * 64, "status": "indexed", "chunk_ids": ["lib1:notes/a.md:0"]}}},
            sources=self._SOURCES,
            include_source_files=False,
        )
        names = set(zipfile.ZipFile(BytesIO(data)).namelist())
        self.assertFalse([n for n in names if n.startswith("sources/")])
        manifest = unpack(data)["manifest"]
        self.assertIs(manifest["source_files_included"], False)
        self.assertIn("notes/a.md", manifest["source_files"])

    def test_default_switch_is_on_so_legacy_parity_is_the_default(self):
        """默认按旧项目行为打包正文（obsidian-rag/export.py:207-218）——
        将来某天有人不想让笔记进备份，那是显式关掉，不是默认就悄悄没有。"""
        import inspect

        default = inspect.signature(pack).parameters["include_source_files"].default
        self.assertIs(default, True)

    def test_unpack_rejects_zip_slip_source_entry(self):
        """一旦包内带正文，接收端就会按 rel 路径把它写进用户的库目录——
        `sources/../../evil.md` 那种条目必须在 unpack 阶段就拒绝
        （对齐 obsidian-rag/import.py:59-66 _validate_zip_members）。"""
        entries = {
            "manifest.json": json.dumps({"library_id": "lib1", "name": "n", "archive_format_version": ARCHIVE_FORMAT_VERSION}).encode(),
            "vectors.json": json.dumps({"lib1:a.md:0": {"embedding": [0.1], "metadata": {}, "document": "x"}}).encode(),
            "bm25.json": b"{}",
            "sources/../../evil.md": b"pwned",
        }
        buf = BytesIO()
        with zipfile.ZipFile(buf, mode="w") as zf:
            for name, blob in entries.items():
                zf.writestr(name, blob)
        with self.assertRaises(ArchiveFormatError) as ctx:
            unpack(buf.getvalue())
        self.assertIn("非法路径", str(ctx.exception))

    def test_pack_refuses_a_source_rel_path_that_escapes(self):
        with self.assertRaises(ArchiveFormatError):
            pack(
                {"library_id": "lib1", "name": "n"},
                {"lib1:a.md:0": {"embedding": [0.1], "metadata": {}, "document": "x"}},
                {},
                sources={"../../evil.md": b"pwned"},
            )


class TestVerifyEntryPoint(unittest.TestCase):
    """缺陷 2 要求给 pipeline 一个"写库之前先校验"的入口
    （对齐 obsidian-rag/import.py:86-108 的"改动前中止"）。"""

    def _payload(self, **overrides):
        base = {
            "library_id": "lib1",
            "name": "工作笔记",
            "archive_format_version": ARCHIVE_FORMAT_VERSION,
            "index_manifest": {
                "files": {
                    "a.md": {"status": "indexed", "chunk_ids": ["lib1:a.md:0"]},
                }
            },
        }
        base.update(overrides)
        return {
            "manifest": base,
            "vectors": {"lib1:a.md:0": {"document": "文本", "metadata": {"path": "a.md", "chunk_index": 0}, "embedding": [1.0, 0.0]}},
            "bm25": {"k1": 1.5, "b": 0.75},
            "index_manifest": {
                "files": {"a.md": {"status": "indexed", "chunk_ids": ["lib1:a.md:0"]}}
            },
            "extracted": None,
            "relations": None,
            "failures": None,
            "visual": None,
            "sources": {},
        }

    def test_verify_accepts_a_healthy_payload(self):
        self.assertIsNone(verify(self._payload()))

    def test_verify_rejects_vectors_entry_referenced_by_index_but_absent(self):
        """vectors ↔ active_chunk_ids 一致性：index.json 说这个文件有 2 个块，
        向量里只有 1 个——这种"少一块"必须在这里就断掉。"""
        payload = self._payload()
        payload["index_manifest"]["files"]["a.md"]["chunk_ids"] = ["lib1:a.md:0", "lib1:a.md:1"]
        with self.assertRaises(ArchiveFormatError) as ctx:
            verify(payload)
        self.assertIn("lib1:a.md:1", str(ctx.exception))

    def test_verify_rejects_orphan_vector_not_referenced_by_index(self):
        payload = self._payload()
        payload["vectors"]["lib1:ghost.md:0"] = {"document": "幽灵", "metadata": {"path": "ghost.md"}, "embedding": [1.0]}
        with self.assertRaises(ArchiveFormatError) as ctx:
            verify(payload)
        self.assertIn("lib1:ghost.md:0", str(ctx.exception))

    def test_verify_rejects_chunk_count_that_disagrees_with_vectors(self):
        payload = self._payload()
        payload["manifest"]["chunk_count"] = 99
        with self.assertRaises(ArchiveFormatError) as ctx:
            verify(payload)
        self.assertIn("chunk_count", str(ctx.exception))

    def test_verify_rejects_payload_missing_manifest(self):
        with self.assertRaises(ArchiveFormatError):
            verify({"vectors": {}})

    def test_verify_rejects_source_blob_whose_checksum_disagrees(self):
        payload = self._payload()
        payload["manifest"]["source_files"] = {"a.md": {"sha256": "0" * 64, "size": 3}}
        payload["manifest"]["source_files_included"] = True
        payload["sources"] = {"a.md": b"abc"}
        with self.assertRaises(ArchiveFormatError) as ctx:
            verify(payload)
        self.assertIn("a.md", str(ctx.exception))

    def test_real_pack_output_passes_verify(self):
        data = pack(
            {"library_id": "lib1", "name": "工作笔记"},
            {"lib1:a.md:0": {"document": "文本", "metadata": {"path": "a.md", "chunk_index": 0}, "embedding": [1.0, 0.0]}},
            {"k1": 1.5, "b": 0.75},
            index_manifest={"files": {"a.md": {"status": "indexed", "chunk_ids": ["lib1:a.md:0"], "size": 6, "content_hash": "a" * 64}}},
        )
        self.assertIsNone(verify(unpack(data)))


class TestNoCredentialsInArchive(unittest.TestCase):
    """AGENTS.md §7：敏感值不得进入归档元数据。旧项目
    obsidian-rag/export.py:104-118 的 build_manifest 只写库名/时间/模型名/
    版本号/块数/维度/校验和/源文件清单，**没有任何凭据**——REDO 的
    config_manifest（core/pipeline.py:2405-2420）同样没有。"""

    _CREDENTIAL_HINTS = ("api_key", "apikey", "secret", "token", "password", "passwd", "credential", "bearer", "authorization")

    def test_pack_refuses_a_credential_shaped_manifest_field(self):
        with self.assertRaises(ArchiveFormatError) as ctx:
            pack(
                {"library_id": "lib1", "name": "n", "mineru_api_key": "sk-secret-value"},
                {"lib1:a.md:0": {"embedding": [0.1], "metadata": {}, "document": "x"}},
                {},
            )
        self.assertIn("mineru_api_key", str(ctx.exception))
        # 报错文本本身不能把凭据值抄出来
        self.assertNotIn("sk-secret-value", str(ctx.exception))

    def test_pack_refuses_a_credential_shaped_index_manifest_field(self):
        with self.assertRaises(ArchiveFormatError) as ctx:
            pack(
                {"library_id": "lib1", "name": "n"},
                {"lib1:a.md:0": {"embedding": [0.1], "metadata": {}, "document": "x"}},
                {},
                index_manifest={"files": {}, "ocr_api_key": "sk-secret-value"},
            )
        self.assertIn("ocr_api_key", str(ctx.exception))
        self.assertNotIn("sk-secret-value", str(ctx.exception))

    def test_normal_manifest_with_path_like_keys_is_not_false_positive(self):
        """库内可能有叫 api_key.md / notes/token.md 的笔记——校验和防御
        的是**元数据键名**而不是数据，误伤用户文件名就是自己制造 bug。"""
        data = pack(
            {"library_id": "lib1", "name": "n"},
            {"lib1:api_key.md:0": {"embedding": [0.1], "metadata": {"path": "api_key.md"}, "document": "x"}},
            {},
            index_manifest={"files": {"api_key.md": {"status": "indexed", "chunk_ids": ["lib1:api_key.md:0"]}}},
        )
        self.assertIsNone(verify(unpack(data)))


class TestImportPlan(unittest.TestCase):
    """缺陷 3：pipeline.py:2499-2522 连续四次落盘注册表之后才写向量/词法/
    提取缓存，中途异常就留下"已注册但无索引"的库 + 永久孤儿集合。
    插件侧不能改 pipeline，但必须把"导入是一个可回滚事务"所需的素材准备
    齐：一份纯函数产出的动作清单 + 回滚所需的全部信息。"""

    def _payload(self, *, with_lexical_extra=True):
        vectors = {
            "lib1:a.md:0": {"document": "文本", "metadata": {"path": "a.md", "chunk_index": 0}, "embedding": [1.0, 0.0]},
            "lib1:b.md:1": {"document": "文本2", "metadata": {"path": "b.md", "chunk_index": 1}, "embedding": [0.0, 1.0]},
        }
        bm25 = {
            "k1": 1.5,
            "b": 0.75,
            "doc_lengths": {"lib1:a.md:0": 3, "lib1:ghost.md:9": 1},
            "doc_tokens_cache": {"lib1:a.md:0": {"文本": 1}, "lib1:ghost.md:9": {"幽灵": 1}},
        }
        if not with_lexical_extra:
            bm25["doc_lengths"] = {"lib1:a.md:0": 3}
            bm25["doc_tokens_cache"] = {"lib1:a.md:0": {"文本": 1}}
        return {
            "manifest": {
                "library_id": "lib1",
                "name": "工作笔记",
                "archive_format_version": ARCHIVE_FORMAT_VERSION,
                "selection_out": ["c.md"],
                "source_files": {"a.md": {"sha256": "a" * 64, "size": 6, "status": "indexed"}},
                "source_files_included": False,
            },
            "vectors": vectors,
            "bm25": bm25,
            "index_manifest": {
                "files": {
                    "a.md": {"status": "indexed", "chunk_ids": ["lib1:a.md:0"]},
                    "b.md": {"status": "indexed", "chunk_ids": ["lib1:b.md:1"]},
                }
            },
            "extracted": {"a.md": [{"text": "正文", "route": None}]},
            "relations": {"a.md": ["b.md"]},
            "failures": None,
            "visual": None,
            "sources": {},
        }

    def test_plan_is_pure_and_writes_nothing(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = tmp / "vault"
        root.mkdir()
        plan1 = import_plan(self._payload(), "restored", root_path=root)
        plan2 = import_plan(self._payload(), "restored", root_path=root)
        self.assertEqual(list(os.listdir(root)), [])
        self.assertEqual(plan1.vector_rows, plan2.vector_rows)
        self.assertEqual(plan1.id_map, plan2.id_map)

    def test_plan_remaps_chunk_ids_to_the_target_library(self):
        plan = import_plan(self._payload(), "restored", root_path=Path("."))
        self.assertEqual(plan.id_map["lib1:a.md:0"], "restored:a.md:0")
        self.assertEqual(sorted(plan.active_chunk_ids), ["restored:a.md:0", "restored:b.md:1"])
        self.assertTrue(all(row["chunk_id"].startswith("restored:") for row in plan.vector_rows))

    def test_plan_carries_the_library_config_pipeline_must_write(self):
        plan = import_plan(self._payload(), "restored", root_path=Path("."))
        self.assertEqual(plan.library_name, "工作笔记")
        self.assertEqual(plan.library_config["selection_out"], ["c.md"])

    def test_plan_batches_vectors_like_pipeline_does_today(self):
        plan = import_plan(self._payload(), "restored", root_path=Path("."), upsert_batch=1)
        self.assertEqual(plan.vector_batches, [["restored:a.md:0"], ["restored:b.md:1"]])

    def test_plan_drops_unmapped_lexical_ids_instead_of_prefixing_them(self):
        """词法侧现在的兜底 f"{target_id}:{key}" 会把未映射的源 id 拼成
        `新库:旧库:path:0` 这种错号，而向量侧同场景是丢弃——两侧策略不一致
        （core/pipeline.py:2546-2549 vs 2599-2605）。旧项目 obsidian-rag/
        import.py:164-177 根本没有重映射，两侧天然一致，所以"丢弃"才是对齐
        旧行为的那一侧：留着一个没有向量的 BM25 posting 只会让 BM25 单路
        命中一个检索不到的块。"""
        plan = import_plan(self._payload(), "restored", root_path=Path("."))
        self.assertNotIn("restored:lib1:ghost.md:9", plan.lexical_state["doc_lengths"])
        self.assertEqual(plan.dropped_lexical_ids, ("lib1:ghost.md:9",))
        self.assertIn("restored:a.md:0", plan.lexical_state["doc_lengths"])

    def test_plan_flags_source_files_missing_from_the_target_directory(self):
        """缺陷 1 的可消费事实：目标目录是空壳时，调用方能确切知道"这个库
        缺哪些源文件"，而不是靠索引是否为空去猜。"""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        plan = import_plan(self._payload(), "restored", root_path=tmp)
        self.assertEqual(plan.missing_source_files, ("a.md",))
        self.assertTrue(any("正文" in n for n in plan.notices))

    def test_plan_reports_no_gap_once_source_files_are_actually_there(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "a.md").write_text("文本", encoding="utf-8")
        plan = import_plan(self._payload(), "restored", root_path=tmp)
        self.assertEqual(plan.missing_source_files, ())
        self.assertFalse(any("正文" in n for n in plan.notices))

    def test_plan_rollback_names_the_registry_writes_in_reverse_order(self):
        """回滚必须按"逆序撤销已落盘"执行：pipeline 现在连续四次写注册表
        （core/pipeline.py:2506/2509/2514/2522），中途失败要能一路退干净，
        否则重跑会撞上 2496-2497 的"库已存在，拒绝覆盖"，LEGACY
        import.py:13 承诺的"重跑幂等"就断了。"""
        plan = import_plan(self._payload(), "restored", root_path=Path("."))
        self.assertEqual(
            list(plan.rollback.registry_writes),
            [
                "store.set_agent_formats",
                "store.set_policy",
                "store.set_selection",
                "store.add_library",
            ],
        )
        self.assertEqual(plan.rollback.order[0], "discard_index_generation")

    def test_plan_rollback_lists_files_it_would_have_written_into_the_vault(self):
        import hashlib

        body = "文本".encode("utf-8")
        payload = self._payload()
        payload["manifest"]["source_files_included"] = True
        payload["manifest"]["source_files"]["a.md"]["in_archive"] = True
        payload["manifest"]["source_files"]["a.md"]["sha256"] = hashlib.sha256(body).hexdigest()
        payload["manifest"]["source_files"]["a.md"]["size"] = len(body)
        payload["sources"] = {"a.md": body}
        plan = import_plan(payload, "restored", root_path=Path("."))
        self.assertEqual(plan.source_blobs, {"a.md": body})
        self.assertEqual(plan.rollback.written_source_files, ("a.md",))


if __name__ == "__main__":
    unittest.main()
