"""见 ../../../AGENTS.md 测试纪律。本文件覆盖库注册表（libraries.json）的
读写两侧行为，逐条对齐 obsidian-rag/library.py：

- 缺陷A：库 id/名称的非法字符（: 是块 id 的字段分隔符，出现在 id 里会让
  检索装配把块归错库）；
- 缺陷B：注册路径必须 resolve + 必须是已存在的目录 + 不许重复注册同一目录；
- 缺陷C：libraries.json 损坏先改名 .bak 再按空注册表处理；
- 缺陷D：读侧逐条防御（未知键/缺字段/类型错），一条坏数据不许拖垮整个
  注册表；
- 缺陷E：勾选两表写侧归一 + 越界校验，读侧再归一一次（防手工编辑）；
- 缺陷F：格式开关变更迁移文件级勾选条目（set_policy 是唯一收口点）；
- 缺陷G/H：待授权格式只数"会被纳入"的文件；Agent 授权与格式开关取交集。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.runtime import PluginRuntime, PluginState  # noqa: E402
from core.settings import SettingsStore  # noqa: E402
from official_library_manager.config import (  # noqa: E402
    AGENT_BINARY_FORMATS,
    LibraryConfigStore,
)
from official_library_manager.plugin import LibraryManagerPlugin  # noqa: E402

_LOGGER_NAME = "rag_redo.library_manager.config"


class VaultMixin:
    def make_vault(self, name: str = "vault", files: tuple[str, ...] = ()) -> Path:
        """在临时目录下造一个真实存在的库根目录。

        缺陷B 起 `add_library` 要求根路径 resolve 后确实是已存在的目录
        （对齐 obsidian-rag/library.py:489-491），所以测试不能再拿
        "/vaults/lib1" 这种假路径注册——那样测的就不是真实约束了。
        """
        path = self.tmp / name  # type: ignore[attr-defined]
        path.mkdir(parents=True, exist_ok=True)
        for rel in files:
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"内容 {rel}", encoding="utf-8")
        return path


class TestLibraryConfigStore(VaultMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "libraries.json"
        self.vault = self.make_vault("vault", ("notes.md", "docs/a.md"))

    # ---- 基本读写 ------------------------------------------------------

    def test_add_and_list(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        self.assertEqual([c.library_id for c in store.list_libraries()], ["lib1"])

    def test_invalid_library_identity_is_rejected(self):
        store = LibraryConfigStore(self.path)
        with self.assertRaises(ValueError):
            store.add_library("../escape", "坏 id", str(self.vault))
        with self.assertRaises(ValueError):
            store.add_library("lib2", " ", str(self.vault))
        with self.assertRaises(ValueError):
            store.add_library("lib3", "坏路径", "")
        with self.assertRaises(ValueError):
            store.add_library("lib4", "首尾空格 ", str(self.vault))

    def test_duplicate_id_raises(self):
        store = LibraryConfigStore(self.path)
        other = self.make_vault("other")
        store.add_library("lib1", "我的库", str(self.vault))
        with self.assertRaises(ValueError):
            store.add_library("lib1", "重复", str(other))

    def test_persists_across_instances(self):
        store1 = LibraryConfigStore(self.path)
        store1.add_library("lib1", "我的库", str(self.vault))
        store2 = LibraryConfigStore(self.path)
        self.assertEqual(len(store2.list_libraries()), 1)
        self.assertEqual(store2.get("lib1").name, "我的库")

    def test_set_selection_persists(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_selection("lib1", selection_in=["a.md"], selection_out=["b.md"])
        store2 = LibraryConfigStore(self.path)
        cfg = store2.get("lib1")
        self.assertEqual(cfg.selection_in, ["a.md"])
        self.assertEqual(cfg.selection_out, ["b.md"])

    def test_set_selection_unknown_library_raises(self):
        store = LibraryConfigStore(self.path)
        with self.assertRaises(KeyError):
            store.set_selection("nope", selection_in=["a.md"])

    def test_set_policy_persists_excludes(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_policy(
            "lib1",
            exclude_dirs=["private"],
            exclude_files=["secret.txt"],
            exclude_patterns=["*.tmp"],
        )
        cfg = LibraryConfigStore(self.path).get("lib1")
        self.assertEqual(cfg.exclude_dirs, ["private"])
        self.assertEqual(cfg.exclude_files, ["secret.txt"])
        self.assertEqual(cfg.exclude_patterns, ["*.tmp"])

    def test_set_policy_persists(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_policy("lib1", new_file_default="exclude", enabled_extensions=[".pdf", ".docx"])
        store2 = LibraryConfigStore(self.path)
        cfg = store2.get("lib1")
        self.assertEqual(cfg.new_file_default, "exclude")
        self.assertEqual(cfg.enabled_extensions, [".pdf", ".docx"])

    def test_set_policy_partial_update_leaves_other_field_untouched(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_policy("lib1", new_file_default="exclude")
        cfg = store.get("lib1")
        self.assertEqual(cfg.new_file_default, "exclude")
        self.assertEqual(cfg.enabled_extensions, [".md", ".pdf", ".docx"])

    def test_set_policy_rejects_unknown_default_value(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        with self.assertRaises(ValueError):
            store.set_policy("lib1", new_file_default="maybe")
        self.assertEqual(store.get("lib1").new_file_default, "follow")

    def test_set_policy_normalizes_extension_case_and_dot(self):
        """对齐 obsidian-rag/library.py:558-566 的小写归一 + 去重 + 保序：
        批量勾选语义与裁决侧都按"小写带点"比较，手滑写出的 .PDF / PDF 必须
        落到同一个格式上，否则关格式时漏掉它们。"""
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_policy("lib1", enabled_extensions=["MD", "pdf", ".PDF", "docx"])
        self.assertEqual(store.get("lib1").enabled_extensions, [".md", ".pdf", ".docx"])

    def test_set_selection_refuses_same_place_conflict(self):
        """同位置打架最后兜底（旧 library.py::set_selection 问题47）：纳入
        目标本身在 exclude_dirs 里 → 拒绝落盘，注册表原样不动（内存里的
        配置也一样原样不动，否则一次被拒的改动会跟着下一次无关写入一起
        落盘）。"""
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_policy("lib1", exclude_dirs=["私人"])
        before = self.path.read_bytes()
        with self.assertRaises(ValueError) as ctx:
            store.set_selection("lib1", selection_in=["私人"], selection_out=[])
        self.assertIn("exclude_dirs", str(ctx.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(store.get("lib1").selection_in, [])
        # 非打架照常：out 方向 + 其下具体文件点名不受影响
        store.set_selection("lib1", selection_in=["私人/账单.pdf"], selection_out=["私人"])
        cfg = store.get("lib1")
        self.assertEqual(cfg.selection_in, ["私人/账单.pdf"])
        self.assertEqual(cfg.selection_out, ["私人"])

    def test_set_policy_unknown_library_raises(self):
        store = LibraryConfigStore(self.path)
        with self.assertRaises(KeyError):
            store.set_policy("nope", new_file_default="exclude")

    def test_remove_library(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.remove_library("lib1")
        self.assertEqual(store.list_libraries(), [])

    # ---- 缺陷A：库 id/名称的非法字符 ------------------------------------

    def test_colon_in_library_id_is_rejected(self):
        """冒号是块 id 的字段分隔符（core/pipeline.py:106/115
        `chunk_id.split(":", 1)`），库 id 里出现它，库 a:b 的块
        "a:b:notes/x.md:0" 会被解析成 library='a'、path='b:notes/x.md'
        ——检索结果张冠李戴。旧项目在结构上不可能：
        obsidian-rag/library.py:41/353-357 直接把 ':' 拒掉。"""
        store = LibraryConfigStore(self.path)
        for bad in ("a:b", "a:b:c", "a*", "a?", 'a"b', "a<b", "a>b", "a|b"):
            with self.subTest(library_id=bad), self.assertRaises(ValueError) as ctx:
                store.add_library(bad, "库", str(self.vault))
            message = str(ctx.exception)
            self.assertIn(bad, message)
            self.assertIn("非法字符", message)

    def test_colon_in_library_name_is_rejected(self):
        """旧项目 add_library:438 查 name（库名非法时退化成 "default"），
        REDO 直接拒绝——同一个字符集，不给 name 开口子。"""
        store = LibraryConfigStore(self.path)
        with self.assertRaises(ValueError) as ctx:
            store.add_library("lib1", "坏:名", str(self.vault))
        self.assertIn("非法字符", str(ctx.exception))

    def test_backslash_in_library_id_is_rejected(self):
        store = LibraryConfigStore(self.path)
        with self.assertRaises(ValueError):
            store.add_library("a\\b", "库", str(self.vault))

    def test_control_character_is_rejected_with_readable_message(self):
        store = LibraryConfigStore(self.path)
        with self.assertRaises(ValueError) as ctx:
            store.add_library("a\x1fb", "库", str(self.vault))
        self.assertIn("U+001F", str(ctx.exception))

    def test_library_name_longer_than_64_chars_is_rejected(self):
        store = LibraryConfigStore(self.path)
        with self.assertRaises(ValueError):
            store.add_library("lib1", "名" * 65, str(self.vault))

    # ---- 缺陷B：注册路径校验 -------------------------------------------

    def test_nonexistent_root_directory_is_rejected(self):
        """路径打错必须在**创建时**报错，而不是拖到之后每次检索的 freshness
        诊断里才报 missing（对齐 obsidian-rag/library.py:489-491）。"""
        store = LibraryConfigStore(self.path)
        with self.assertRaises(ValueError) as ctx:
            store.add_library("lib1", "库", str(self.tmp / "no-such-dir"))
        self.assertIn("不存在", str(ctx.exception))
        self.assertEqual(store.list_libraries(), [])

    def test_file_instead_of_directory_is_rejected(self):
        store = LibraryConfigStore(self.path)
        with self.assertRaises(ValueError):
            store.add_library("lib1", "库", str(self.vault / "notes.md"))

    def test_require_existing_dir_opt_out_allows_import_target(self):
        """从归档导入时目标目录可能稍后才由用户摆文件（旧 obsidian-rag 是
        import.py:248 先 mkdir 再 add_library，所以它不需要这个开关）。"""
        store = LibraryConfigStore(self.path)
        cfg = store.add_library(
            "lib1", "库", str(self.tmp / "later"), require_existing_dir=False
        )
        self.assertEqual(cfg.library_id, "lib1")

    def test_root_path_is_resolved_and_normalized(self):
        """"D:\\Vault" / "D:\\Vault\\." / 尾斜杠 / 大小写差异 必须归一到
        同一个值——否则同一个目录能注册成两个库，各建一份索引，跨库检索
        把同一批内容重复返回两遍。resolve() 在 Windows 上还会把
        \\USERS\\... 这类大小写还原成磁盘真名（Python 3.14 实测），并消掉
        尾斜杠、`.`、`..` 与 junction 别名。"""
        store = LibraryConfigStore(self.path)
        cfg = store.add_library(
            "lib1", "库", str(self.vault) + "\\.\\"
        )
        self.assertEqual(cfg.root_path, str(self.vault.resolve()))
        with self.assertRaises(ValueError) as ctx:
            store.add_library("lib2", "库2", str(self.vault).upper())
        self.assertIn("已注册", str(ctx.exception))

    def test_same_directory_cannot_be_registered_twice(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "库1", str(self.vault))
        variants = [
            str(self.vault) + "/",
            str(self.vault) + "\\.",
            str(self.vault) + "/./",
            str(self.vault / ".." / self.vault.name),
            str(self.vault).upper(),
        ]
        for index, raw in enumerate(variants):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as ctx:
                store.add_library(f"lib{index + 2}", f"库{index + 2}", raw)
            self.assertIn("已注册", str(ctx.exception))

    def test_two_libraries_may_share_a_display_name(self):
        """**有意不移植旧项目"库名已存在"那条拒绝**（obsidian-rag/library.py:
        495-496）。旧项目的 name 就是库身份（resolve_entries 按 name 索引），
        同名必然撞车；REDO 把身份拆成 library_id 之后 name 只是展示标签，
        检索范围 / 选库参数 / 块归属全走 library_id。硬要唯一会打死"从归档
        导入成新 id 的库"这条正当流程（import 把归档里的 name 原样带过来，
        源库还在注册表里就会撞名）。库里允许同名，真正的约束是 id 唯一 +
        根路径唯一 + selection 归一。"""
        store = LibraryConfigStore(self.path)
        other = self.make_vault("other")
        store.add_library("lib1", "同名", str(self.vault))
        cfg = store.add_library("lib2", "同名", str(other))
        self.assertEqual(cfg.library_id, "lib2")
        self.assertEqual({c.library_id for c in store.list_libraries()}, {"lib1", "lib2"})

    def test_library_id_defaults_to_name(self):
        store = LibraryConfigStore(self.path)
        cfg = store.add_library("", "自动命名", str(self.vault))
        self.assertEqual(cfg.library_id, "自动命名")

    def test_junction_alias_is_treated_as_same_directory(self):
        """junction（mklink /J）是 Windows 上不占软链符号链接的等价手段；
        实测 Path.resolve() 会跟到 junction 目标，所以经 junction 注册同一
        个目录必须被识别成重复。建不出 junction 就跳过（本机权限不足时）。"""
        vault = self.make_vault("vault-j", ("a.md",))
        link = self.tmp / "vault-link"
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(vault)],
            capture_output=True, text=True,
        )
        if done.returncode != 0 or not link.is_dir():
            self.skipTest("本机无法创建 junction（权限不足），跳过")
        self.assertEqual(str(link.resolve()), str(vault.resolve()))
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "库1", str(vault))
        with self.assertRaises(ValueError):
            store.add_library("lib2", "库2", str(link))
        # 反向：先注册 junction，再注册它的目标目录，同样撞车
        store2 = LibraryConfigStore(self.path.with_name("libraries2.json"))
        store2.add_library("lib1", "库1", str(link))
        with self.assertRaises(ValueError):
            store2.add_library("lib2", "库2", str(vault))

    # ---- 缺陷C：损坏注册表先备份 ---------------------------------------

    def test_corrupted_file_degrades_to_empty_not_crash(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        store = LibraryConfigStore(self.path)
        self.assertEqual(store.list_libraries(), [])

    def test_corrupted_file_is_backed_up_before_it_can_be_overwritten(self):
        """对齐 obsidian-rag/library.py:399-408：解析失败先改名 .bak 再按空
        注册表处理。没有这一步，一次手滑写坏 JSON 之后，用户随便建一个库
        就用 {}+新条目把原文件原子覆盖掉，全部库的勾选/排除/格式开关一起
        永久丢失。"""
        broken = '{"lib1": {"name": "我的库", "root_path": "D:/Vault", "chunks": '
        self.path.write_text(broken, encoding="utf-8")
        store = LibraryConfigStore(self.path)
        self.assertEqual(store.list_libraries(), [])
        backup = self.path.with_name("libraries.json.bak")
        self.assertTrue(backup.exists(), "损坏的原文件必须先备份成 .bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), broken)
        # 备份之后原文件已不存在；此时新建库会写出一个新的合法注册表，
        # 但备份里的原始内容仍可手工恢复。
        store.add_library("lib1", "新库", str(self.vault))
        self.assertEqual(backup.read_text(encoding="utf-8"), broken)
        self.assertEqual([c.library_id for c in store.list_libraries()], ["lib1"])

    def test_non_object_root_is_also_backed_up(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        store = LibraryConfigStore(self.path)
        self.assertEqual(store.list_libraries(), [])
        self.assertTrue(self.path.with_name("libraries.json.bak").exists())

    def test_backup_failure_keeps_original_file_untouched(self):
        """备份失败必须只记日志，不能把原文件删了/动了（fail-open 到"原样
        保留"，用户还有机会手工抢救）。"""
        blocked = self.tmp / "blocked"
        blocked.mkdir()
        self.path.write_text("{not valid json", encoding="utf-8")
        # 把备份目标变成一个已存在的目录 → os.replace 必然失败
        self.path.with_name("libraries.json.bak").mkdir()
        store = LibraryConfigStore(self.path)
        self.assertEqual(store.list_libraries(), [])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not valid json")

    # ---- 缺陷D：读侧逐条防御 -------------------------------------------

    def test_unknown_keys_in_entry_are_ignored_with_warning(self):
        """旧注册表里有 collection / chunk_char_limit 这类 REDO 没有对应
        字段的键（迁移自 obsidian-rag 时会遇到）。过去 LibraryConfig(**data)
        直接 TypeError → 插件 on_load 失败 → 宿主 GUI 整个起不来。对齐
        obsidian-rag/library.py:410-418"跳过非法条目并记日志"，改成容忍 +
        告警。"""
        self.path.write_text(
            json.dumps(
                {
                    "lib1": {
                        "name": "我的库",
                        "root_path": str(self.vault),
                        "collection": "kb_vault",
                        "chunk_char_limit": 600,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
            store = LibraryConfigStore(self.path)
        self.assertEqual([c.library_id for c in store.list_libraries()], ["lib1"])
        self.assertIn("chunk_char_limit", "\n".join(captured.output))
        self.assertIn("collection", "\n".join(captured.output))

    def test_entry_with_missing_required_field_is_skipped_but_siblings_survive(self):
        self.path.write_text(
            json.dumps(
                {
                    "good": {"name": "好库", "root_path": str(self.vault)},
                    "no-path": {"name": "缺路径"},
                    "no-name": {"root_path": str(self.vault)},
                    "not-a-dict": ["坏数据"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertLogs(_LOGGER_NAME, level="ERROR"):
            store = LibraryConfigStore(self.path)
        self.assertEqual([c.library_id for c in store.list_libraries()], ["good"])

    def test_entry_with_illegal_name_is_skipped(self):
        self.path.write_text(
            json.dumps(
                {
                    "ok": {"name": "好库", "root_path": str(self.vault)},
                    "bad": {"name": "坏:名", "root_path": str(self.vault)},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertLogs(_LOGGER_NAME, level="ERROR"):
            store = LibraryConfigStore(self.path)
        self.assertEqual([c.library_id for c in store.list_libraries()], ["ok"])

    def test_illegal_library_id_is_kept_but_loudly_reported(self):
        """库 id 含非法字符时**保留**条目（连同 ERROR 日志），而不是像库名
        那样跳过：id 非法意味着这个库的块在检索装配时会归错库，但把整个库
        从 GUI 里抹掉会让用户"库凭空消失"且更难排查，id 又是可以手工改回来
        的字段。"""
        self.path.write_text(
            json.dumps(
                {"a:b": {"name": "旧库", "root_path": str(self.vault)}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertLogs(_LOGGER_NAME, level="ERROR") as captured:
            store = LibraryConfigStore(self.path)
        self.assertEqual([c.library_id for c in store.list_libraries()], ["a:b"])
        self.assertIn("归到错误的库", "\n".join(captured.output))

    def test_non_list_field_falls_back_to_default(self):
        self.path.write_text(
            json.dumps(
                {"lib1": {"name": "库", "root_path": str(self.vault), "exclude_dirs": "私人"}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertLogs(_LOGGER_NAME, level="WARNING"):
            store = LibraryConfigStore(self.path)
        self.assertIn("TEMP", store.get("lib1").exclude_dirs)

    def test_invalid_new_file_default_falls_back_to_follow(self):
        self.path.write_text(
            json.dumps(
                {
                    "lib1": {
                        "name": "库",
                        "root_path": str(self.vault),
                        "new_file_default": "whatever",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertLogs(_LOGGER_NAME, level="WARNING"):
            store = LibraryConfigStore(self.path)
        self.assertEqual(store.get("lib1").new_file_default, "follow")

    # ---- 缺陷E：勾选两表归一 + 越界校验 --------------------------------

    def test_selection_paths_are_normalized_on_write(self):
        """写侧必须归一：裁决侧 selection.py:82-91 做的是精确集合比对，落盘
        成 "docs\\a.md" / "x/" 的话规则永远命不中，用户看到的是"我明明勾了，
        怎么没生效"。Windows 上手写/迁移产生的反斜杠与尾斜杠极常见。"""
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_selection(
            "lib1",
            selection_in=["docs\\a.md", "笔记/"],
            selection_out=["私人\\"],
        )
        cfg = store.get("lib1")
        self.assertEqual(cfg.selection_in, ["docs/a.md", "笔记"])
        self.assertEqual(cfg.selection_out, ["私人"])

    def test_selection_paths_are_normalized_on_read(self):
        """读侧再归一一次（双保险）：文件可能被手工编辑或从旧项目迁移过来。
        对齐 obsidian-rag/library.py:225-238 _sel_entry_lists。"""
        self.path.write_text(
            json.dumps(
                {
                    "lib1": {
                        "name": "库",
                        "root_path": str(self.vault),
                        "selection_in": ["docs\\a.md", "../坏路径", 42, "x/"],
                        "selection_out": ["私人"],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
            store = LibraryConfigStore(self.path)
        cfg = store.get("lib1")
        self.assertEqual(cfg.selection_in, ["docs/a.md", "x"])
        self.assertEqual(cfg.selection_out, ["私人"])
        self.assertIn("静默丢弃", "\n".join(captured.output))

    def test_normalized_selection_actually_hits_in_decide_included(self):
        """归一不是为了好看：归一后裁决必须真的命中（端到端钉住）。"""
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_selection("lib1", selection_in=["docs\\a.md"], selection_out=[])
        plugin = LibraryManagerPlugin()
        plugin.store = store
        decisions = {p: inc for p, inc, _ in plugin.resolve_included_files("lib1")}
        self.assertTrue(decisions["docs/a.md"])

    def test_escape_paths_are_rejected_on_write(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        before = self.path.read_bytes()
        for bad in ("../逃逸.md", "/etc/passwd", "C:/Windows/System32", "~/x.md", "a//b"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                store.set_selection("lib1", selection_in=[bad])
        self.assertEqual(self.path.read_bytes(), before)

    def test_selection_path_escaping_root_via_junction_is_rejected(self):
        """词法校验（norm_selection_path）挡不住符号链接/junction 越界——
        "link/secret.md" 词法上完全在库内，实际指向库外。resolve 之后必须
        仍在库根内（对齐 obsidian-rag/library.py:261-263）。实测 junction
        会被 Path.resolve() 跟到目标，所以这道校验对 junction 有效。"""
        vault = self.make_vault("vault-e", ("notes.md",))
        outside = self.make_vault("outside-e", ("secret.md",))
        link = vault / "link"
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True, text=True,
        )
        if done.returncode != 0 or not link.is_dir():
            self.skipTest("本机无法创建 junction（权限不足），跳过")
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "库", str(vault))
        with self.assertRaises(ValueError) as ctx:
            store.set_selection("lib1", selection_in=["link/secret.md"])
        self.assertIn("越出库范围", str(ctx.exception))

    def test_selection_escape_via_dotdot_inside_root_is_fine(self):
        """.`` 段在 norm_selection_path 阶段就被拒了（词法层），这里钉的是
        "库内深层相对路径必须放行"——越界校验不能连正常路径一起拦。"""
        vault = self.make_vault("vault-deep", ("a/b/c/deep.md",))
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "库", str(vault))
        store.set_selection("lib1", selection_in=["a/b/c/deep.md"], selection_out=[])
        self.assertEqual(store.get("lib1").selection_in, ["a/b/c/deep.md"])

    # ---- 缺陷F：格式开关变更迁移文件级勾选条目 ------------------------

    def test_format_switch_off_cancels_file_level_selection(self):
        """镜像 obsidian-rag tests/test_selection.py:146-166 test_format_bulk：
        关格式 = 把该格式的**文件级**显式勾选移入 selection_out，文件夹级
        条目永不被触碰（用户拍板③：全局格式开关就是对该格式文件的批量
        勾/取消，没有"保护显式勾选"的概念）。"""
        vault = self.make_vault("vault-f", ("课件/青苹果菜单.pdf", "课件/锅包肉配方.pdf", "笔记.md"))
        store = LibraryConfigStore(self.path)
        store.add_library("t", "库", str(vault))
        store.set_selection(
            "t",
            selection_in=["课件/青苹果菜单.pdf", "课件/锅包肉配方.pdf", "课件"],
            selection_out=[],
        )
        store.set_policy("t", enabled_extensions=[".md"])
        cfg = store.get("t")
        self.assertEqual(cfg.enabled_extensions, [".md"])
        self.assertEqual(cfg.selection_in, ["课件"], "文件夹级条目不被触碰")
        self.assertEqual(
            sorted(cfg.selection_out),
            ["课件/锅包肉配方.pdf", "课件/青苹果菜单.pdf"],
            "子目录里的文件同样是文件级（不能以路径有没有 / 来区分）",
        )

    def test_format_switch_on_restores_follow_for_explicitly_removed_files(self):
        """镜像 obsidian-rag tests/test_selection.py:172-186：开格式 =
        selection_out 里该格式的文件条目移除（恢复"跟随"=纳入），而
        selection_in 原样（显式勾选不凭空复活）。"""
        vault = self.make_vault("vault-g", ("课件/青苹果菜单.pdf", "笔记.md"))
        store = LibraryConfigStore(self.path)
        store.add_library("t", "库", str(vault))
        store.set_selection("t", selection_in=["课件/青苹果菜单.pdf"], selection_out=[])
        store.set_policy("t", enabled_extensions=["md"])  # 关 pdf
        self.assertEqual(store.get("t").selection_out, ["课件/青苹果菜单.pdf"])
        self.assertEqual(store.get("t").selection_in, [])
        store.set_policy("t", enabled_extensions=["md", "pdf"])  # 开 pdf
        cfg = store.get("t")
        self.assertEqual(cfg.selection_out, [])
        self.assertEqual(cfg.selection_in, [])

    def test_format_switch_ignores_folder_entries_that_look_like_extensions(self):
        vault = self.make_vault("vault-h", ("资料.pdf/a.md", "笔记.md"))
        store = LibraryConfigStore(self.path)
        store.add_library("t", "库", str(vault))
        store.set_selection("t", selection_in=["资料.pdf"], selection_out=[])
        # 判据是扩展名而不是"有没有 /"，所以这个**文件夹**条目同样会被批量
        # 迁移——与旧项目逐字一致（旧注释：文件夹条目极少以 .ext 结尾，
        # 误伤面可忽略）。
        store.set_policy("t", enabled_extensions=[".md"])
        self.assertEqual(store.get("t").selection_in, [])
        self.assertEqual(store.get("t").selection_out, ["资料.pdf"])

    def test_format_switch_bulk_is_persisted(self):
        vault = self.make_vault("vault-i", ("课件/a.pdf",))
        store = LibraryConfigStore(self.path)
        store.add_library("t", "库", str(vault))
        store.set_selection("t", selection_in=["课件/a.pdf"], selection_out=[])
        store.set_policy("t", enabled_extensions=[".md"])
        reloaded = LibraryConfigStore(self.path).get("t")
        self.assertEqual(reloaded.selection_out, ["课件/a.pdf"])

    # ---- 缺陷H：Agent 授权与格式开关取交集 ----------------------------

    def test_agent_allowlist_cannot_be_pierced_by_explicit_selection(self):
        vault = self.make_vault(
            "vault-agent", ("notes.md", "scan.pdf", "sub/deep.pdf")
        )
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(vault))
        store.set_selection("lib1", selection_in=["scan.pdf"])
        plugin = LibraryManagerPlugin()
        plugin.store = store
        self.assertEqual(plugin.agent_allowed_extensions("lib1"), (".md", ".txt"))
        decisions = {
            path: included
            for path, included, _reason in plugin.resolve_included_files(
                "lib1",
                format_allowlist=plugin.agent_allowed_extensions("lib1"),
            )
        }
        self.assertTrue(decisions["notes.md"])
        self.assertFalse(decisions["scan.pdf"])
        store.set_agent_formats("lib1", [".pdf"])
        decisions = {
            path: included
            for path, included, _reason in plugin.resolve_included_files(
                "lib1",
                format_allowlist=plugin.agent_allowed_extensions("lib1"),
            )
        }
        self.assertTrue(decisions["scan.pdf"])

    def test_agent_formats_persist_and_reject_unknown_binary(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_agent_formats("lib1", ["pdf", ".docx", "pdf"])
        self.assertEqual(store.get("lib1").agent_formats, [".pdf", ".docx"])
        with self.assertRaises(ValueError):
            store.set_agent_formats("lib1", [".exe"])

    def test_agent_formats_rejects_format_not_enabled(self):
        """对齐 obsidian-rag/library.py:578-590 + server.py 的 set_config
        拦截：未在格式开关里启用的格式无法授权。少了这条，授权就成了一句
        永远不会生效的空话（用户看到"已授权 .pdf"但 .pdf 根本不在库里）。"""
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_policy("lib1", enabled_extensions=[".md"])
        with self.assertRaises(ValueError) as ctx:
            store.set_agent_formats("lib1", [".pdf"])
        self.assertIn("enabled_extensions", str(ctx.exception))
        self.assertEqual(store.get("lib1").agent_formats, [])

    def test_agent_formats_revocation_by_empty_list_allowed(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(self.vault))
        store.set_agent_formats("lib1", [".pdf"])
        store.set_agent_formats("lib1", [])
        self.assertEqual(store.get("lib1").agent_formats, [])

    def test_disabling_format_revokes_agent_authorization(self):
        """单一事实来源是格式开关：用户在格式开关里关掉 .pdf，历史上授权过
        也自动失效（obsidian-rag/library.py:473 的注释原话）。配合中性默认
        include（该态穿透格式白名单），少了这一步 Agent 触发的索引会把用户
        已经关掉的 .pdf 照收不误。"""
        vault = self.make_vault("vault-revoke", ("notes.md", "a.pdf"))
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(vault))
        store.set_policy("lib1", new_file_default="include")
        store.set_agent_formats("lib1", [".pdf"])
        plugin = LibraryManagerPlugin()
        plugin.store = store
        self.assertEqual(plugin.agent_allowed_extensions("lib1"), (".md", ".pdf", ".txt"))
        store.set_policy("lib1", enabled_extensions=[".md"])
        self.assertEqual(plugin.agent_allowed_extensions("lib1"), (".md", ".txt"))
        decisions = {
            path: included
            for path, included, _reason in plugin.resolve_included_files(
                "lib1", format_allowlist=plugin.agent_allowed_extensions("lib1")
            )
        }
        self.assertFalse(decisions["a.pdf"])

    # ---- 缺陷G：待授权格式只数会被纳入的文件 ----------------------------

    def test_pending_agent_formats_ignores_excluded_files(self):
        """对齐 obsidian-rag/server.py:120-137：走漏斗（collect_md_files）
        计数，而不是裸 glob。把被 exclude_dirs/selection_out/exclude_files
        挡掉的 .pdf 算进"待授权"，横幅会长期挂着一个用户授权了也不可能变化
        的数字——那是"授权了但没生效"的静默不一致，比不提示更糟。"""
        vault = self.make_vault(
            "vault-g", ("笔记.md", "课件/a.pdf", "私人/b.pdf", "名单内/c.pdf", "模板/d.pdf")
        )
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(vault))
        store.set_policy(
            "lib1",
            exclude_dirs=["私人"],
            exclude_files=["d.pdf"],
        )
        store.set_selection("lib1", selection_out=["名单内"])
        plugin = LibraryManagerPlugin()
        plugin.store = store
        self.assertEqual(plugin.pending_agent_formats("lib1"), {".pdf": 1})

    def test_pending_agent_formats_empty_when_authorized(self):
        vault = self.make_vault("vault-g2", ("笔记.md", "a.pdf"))
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(vault))
        store.set_agent_formats("lib1", [".pdf"])
        plugin = LibraryManagerPlugin()
        plugin.store = store
        self.assertEqual(plugin.pending_agent_formats("lib1"), {})

    def test_pending_agent_formats_ignores_disabled_format(self):
        """格式开关里已经关掉的格式不在待授权清单里（它压根不会被索引，
        授权它没有意义）。"""
        vault = self.make_vault("vault-g3", ("笔记.md", "a.pdf"))
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", str(vault))
        store.set_policy("lib1", enabled_extensions=[".md"])
        plugin = LibraryManagerPlugin()
        plugin.store = store
        self.assertEqual(plugin.pending_agent_formats("lib1"), {})

    def test_pending_agent_formats_unknown_library_raises(self):
        plugin = LibraryManagerPlugin()
        plugin.store = LibraryConfigStore(self.path)
        with self.assertRaises(KeyError):
            plugin.pending_agent_formats("nope")


class TestBinaryFormatAllowlistShared(unittest.TestCase):
    def test_binary_allowlist_matches_plugin_constant(self):
        from official_library_manager import plugin as plugin_module

        self.assertEqual(tuple(AGENT_BINARY_FORMATS), plugin_module.AGENT_BINARY_EXTENSIONS)


class TestResolveLibraries(VaultMixin, unittest.TestCase):
    """`LibraryManagerPlugin.resolve_libraries` 是多库检索选库语法的唯一
    权威实现（对齐 obsidian-rag/library.py::resolve_entries），这里直接
    测这个方法本身——不需要走完整 PluginRuntime/Pipeline，`resolve_libraries`
    只碰 `self.store`，直接赋值即可，比端到端测试更聚焦。跨库检索的真实
    端到端行为（真的搜到两个库的内容）在 tests/test_pipeline_e2e.py。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plugin = LibraryManagerPlugin()
        self.plugin.store = LibraryConfigStore(self.tmp / "libraries.json")
        self.plugin.store.add_library("lib-a", "库A", str(self.make_vault("a")))
        self.plugin.store.add_library("lib-b", "库B", str(self.make_vault("b")))
        self.plugin.store.add_library("lib-c", "库C", str(self.make_vault("c")))
        self.plugin._settings = SettingsStore(self.tmp / "settings.json")

    def test_empty_libraries_returns_all(self):
        result = self.plugin.resolve_libraries("")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-b", "lib-c"})

    def test_all_keyword_case_insensitive_returns_all(self):
        result = self.plugin.resolve_libraries("ALL")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-b", "lib-c"})

    def test_comma_separated_list_preserves_order_and_dedups(self):
        result = self.plugin.resolve_libraries("lib-b,lib-a,lib-b")
        self.assertEqual([c.library_id for c in result], ["lib-b", "lib-a"])

    def test_exclude_subtracts_from_selection(self):
        result = self.plugin.resolve_libraries("all", exclude="lib-b")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-c"})

    def test_unknown_library_name_raises_and_lists_available(self):
        with self.assertRaises(ValueError) as ctx:
            self.plugin.resolve_libraries("lib-a,no-such-lib")
        message = str(ctx.exception)
        self.assertIn("no-such-lib", message)
        self.assertIn("lib-a", message)
        self.assertIn("lib-b", message)
        self.assertIn("lib-c", message)

    def test_exclude_everything_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.plugin.resolve_libraries("lib-a", exclude="lib-a")

    def test_no_registered_libraries_raises_value_error(self):
        empty_plugin = LibraryManagerPlugin()
        empty_plugin.store = LibraryConfigStore(self.tmp / "empty.json")
        with self.assertRaises(ValueError):
            empty_plugin.resolve_libraries("")

    def test_empty_libraries_uses_default_libraries_setting_when_configured(self):
        """对齐 obsidian-rag/config.py 的 default_libraries：libraries 留空
        时优先用这份设置里的范围，而不是不由分说查全部库。"""
        self.plugin._settings.set("default_libraries", ["lib-a", "lib-c"])
        result = self.plugin.resolve_libraries("")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-c"})

    def test_all_keyword_ignores_default_libraries_setting(self):
        self.plugin._settings.set("default_libraries", ["lib-a"])
        result = self.plugin.resolve_libraries("all")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-b", "lib-c"})

    def test_default_libraries_with_deleted_library_silently_skips_it(self):
        self.plugin._settings.set("default_libraries", ["lib-a", "no-longer-exists"])
        result = self.plugin.resolve_libraries("")
        self.assertEqual({c.library_id for c in result}, {"lib-a"})

    def test_default_libraries_all_invalid_falls_back_to_all(self):
        """对齐 obsidian-rag resolve_entries："默认库全部失效→回退全部库
        （旧行为）"——不是报错，是安静地退回"全部库"这个更宽松的默认。"""
        self.plugin._settings.set("default_libraries", ["no-longer-exists"])
        result = self.plugin.resolve_libraries("")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-b", "lib-c"})

    def test_explicit_libraries_param_overrides_default_libraries_setting(self):
        self.plugin._settings.set("default_libraries", ["lib-a"])
        result = self.plugin.resolve_libraries("lib-b")
        self.assertEqual([c.library_id for c in result], ["lib-b"])


class TestSelectionWriteGate(VaultMixin, unittest.TestCase):
    """get_selection/propose_selection_changes/apply_selection_changes——
    对齐 obsidian-rag 的同名三件套（2026-09-23 全面功能审计B类缺口）：
    路径级勾选变更永远走写权限门禁确认，没有"此前非用户手写就直接生效"
    的快捷分支（同 official-library-summary::propose() 不一样，见
    plugin.py 里这三个方法的说明）。真实通过 PluginRuntime 走一遍完整
    生命周期，拿到真实 ctx.write_gate，不是假的（同
    official-library-summary/tests/test_plugin.py 的验证方式）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.rt = PluginRuntime(
            _REPO_ROOT / "plugins", state_file=self.tmp / "state.json", data_dir=self.tmp / "data"
        )
        self.rt.scan()
        self.rt.load("official-library-manager")
        self.rt.enable("official-library-manager")
        self.assertEqual(
            self.rt.plugins["official-library-manager"].state,
            PluginState.ENABLED,
            self.rt.plugins["official-library-manager"].error,
        )
        self.instance = self.rt.plugins["official-library-manager"].instance
        self.vault = self.make_vault("lib1", ("a.md", "private/keep.md"))
        self.instance.store.add_library("lib1", "库1", str(self.vault))

    def test_get_selection_on_fresh_library_is_empty(self):
        result = self.instance.get_selection("lib1")
        self.assertEqual(result, {"selection_in": [], "selection_out": []})

    def test_get_selection_unknown_library_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.instance.get_selection("no-such-lib")

    def test_propose_never_applies_directly(self):
        """硬性确认门禁：不管此前状态如何，propose 永远只生成提案，绝不
        直接生效——这是与库简介 propose() 最大的行为差异，必须显式钉住。"""
        result = self.instance.propose_selection_changes("lib1", [{"path": "private/", "action": "out"}])
        self.assertTrue(result["ok"])
        self.assertIn("proposal_id", result)
        self.assertIn("confirmation_code", result)
        # 还没 apply，get_selection 应该看不到任何变化
        self.assertEqual(self.instance.get_selection("lib1"), {"selection_in": [], "selection_out": []})

    def test_propose_then_apply_with_correct_code_takes_effect(self):
        propose_result = self.instance.propose_selection_changes(
            "lib1", [{"path": "private", "action": "out"}, {"path": "private/keep.md", "action": "in"}]
        )
        apply_result = self.instance.apply_selection_changes(
            "lib1", propose_result["proposal_id"], propose_result["confirmation_code"]
        )
        self.assertTrue(apply_result["ok"])
        self.assertEqual(apply_result["selection_in"], ["private/keep.md"])
        self.assertEqual(apply_result["selection_out"], ["private"])
        self.assertEqual(
            self.instance.get_selection("lib1"),
            {"selection_in": ["private/keep.md"], "selection_out": ["private"]},
        )

    def test_apply_with_wrong_code_is_rejected_and_does_not_take_effect(self):
        propose_result = self.instance.propose_selection_changes("lib1", [{"path": "a.md", "action": "out"}])
        apply_result = self.instance.apply_selection_changes("lib1", propose_result["proposal_id"], "000000")
        self.assertFalse(apply_result["ok"])
        self.assertEqual(self.instance.get_selection("lib1"), {"selection_in": [], "selection_out": []})

    def test_apply_proposal_twice_second_time_rejected(self):
        """一次性有效——同 core/write_gate.py 的一次性提案纪律，这里只是
        确认它真的接上了。"""
        propose_result = self.instance.propose_selection_changes("lib1", [{"path": "a.md", "action": "out"}])
        first = self.instance.apply_selection_changes(
            "lib1", propose_result["proposal_id"], propose_result["confirmation_code"]
        )
        self.assertTrue(first["ok"])
        second = self.instance.apply_selection_changes(
            "lib1", propose_result["proposal_id"], propose_result["confirmation_code"]
        )
        self.assertFalse(second["ok"])

    def test_propose_with_illegal_path_raises_before_creating_proposal(self):
        with self.assertRaises(ValueError):
            self.instance.propose_selection_changes("lib1", [{"path": "../escape.md", "action": "in"}])

    def test_propose_unknown_library_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.instance.propose_selection_changes("no-such-lib", [{"path": "a.md", "action": "in"}])

    def test_apply_with_mismatched_library_id_is_rejected(self):
        self.instance.store.add_library("lib2", "库2", str(self.make_vault("lib2")))
        propose_result = self.instance.propose_selection_changes("lib1", [{"path": "a.md", "action": "out"}])
        apply_result = self.instance.apply_selection_changes(
            "lib2", propose_result["proposal_id"], propose_result["confirmation_code"]
        )
        self.assertFalse(apply_result["ok"])


if __name__ == "__main__":
    unittest.main()
