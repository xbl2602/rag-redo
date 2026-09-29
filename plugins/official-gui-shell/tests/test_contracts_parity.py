"""BC-15 阶段B/C 门禁：桥接层必须与 guiweb/bridge.py 的 37 个契约方法**签名一致、返回键集
合一致**，并且每个方法都被**真实调用**过。

这是量化验收装置：失败信息直接打印 `覆盖 N/37`、`签名一致 M/37`、`键集合满足 K/37`。

**为什么不能只比签名**（2026-09-28 审计 HIGH-2）：此前这份测试只看方法名和形参顺序，于是
`get_library_config` 缺 `effective/overrides/all_keys`、`read_document` 缺 `markdown/
rendered_html/...`、`wemm_status` 缺 `exists/total_pages/rows` 都是绿的——前端对应功能
拿不到数据。现在对夹具里 `required_keys` / `item_required_keys` 逐个真实调用、逐个断言。

`Api` 子类同名方法遮蔽 mixin 的问题（契约版整段成死代码）也在这里堵死：`Api` 自己不得
重定义任何契约方法名。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_TESTS_DIR = Path(__file__).parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from gui_test_env import GuiTestEnv  # noqa: E402

from official_gui_shell import contract_bridge  # noqa: E402
from official_gui_shell.api import Api  # noqa: E402

FIXTURE_PATH = _TESTS_DIR / "fixtures" / "legacy_guiweb_contract.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class TestContractMethodCoverage(unittest.TestCase):
    """BC-15 第(2)条：37 个契约方法 37/37 存在且签名一致，且不被子类遮蔽。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _fixture()
        cls.contract = cls.fixture["methods"]

    def test_contract_fixture_declares_37_methods(self) -> None:
        names = [item["name"] for item in self.contract]
        self.assertEqual(len(names), 37, f"契约方法数应恒为 37：{names}")
        self.assertEqual(len(set(names)), 37, "契约方法名重复")

    def test_every_contract_method_is_implemented(self) -> None:
        missing = [item["name"] for item in self.contract if not hasattr(Api, item["name"])]
        covered = len(self.contract) - len(missing)
        self.assertEqual(missing, [], f"契约方法覆盖 {covered}/{len(self.contract)}，缺：{missing}")

    def test_every_contract_method_has_matching_signature(self) -> None:
        import inspect

        mismatched: list[str] = []
        matched = 0
        for item in self.contract:
            method = getattr(Api, item["name"], None)
            if method is None:
                mismatched.append(f"{item['name']}: 缺失")
                continue
            params = [
                p.name
                for p in inspect.signature(method).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            ]
            if params and params[0] == "self":
                params = params[1:]
            if params == list(item["params"]):
                matched += 1
            else:
                mismatched.append(f"{item['name']}: 期望 {item['params']} 实际 {params}")
        self.assertEqual(mismatched, [], f"签名一致 {matched}/{len(self.contract)}，不符：{mismatched}")

    def test_api_subclass_never_shadows_a_contract_method(self) -> None:
        """契约方法只定义在 `_LegacyContractMixin`。`Api` 自己再定义同名方法就会遮蔽它——
        契约版成了死代码，前端拿到的是另一套形状。"""
        contract_names = {item["name"] for item in self.contract}
        shadowing = sorted(contract_names & {n for n in vars(Api) if not n.startswith("_")})
        self.assertEqual(shadowing, [], f"Api 遮蔽了 mixin 的契约方法：{shadowing}")
        for name in contract_names:
            self.assertIn(name, vars(contract_bridge._LegacyContractMixin), f"{name} 不在 mixin 里")

    def test_bridge_exposes_window_binding_for_push_and_dialogs(self) -> None:
        self.assertTrue(hasattr(Api, "bind_window"))
        self.assertTrue(hasattr(Api, "get_snapshot"))


class TestImportConfirmationGate(unittest.TestCase):
    """导入必须逐字确认，防误触（contracts.md 导入/导出门禁）。"""

    def test_confirm_text_is_frozen(self) -> None:
        self.assertEqual(_fixture()["import_confirm_text"], "我确认导入")
        self.assertEqual(contract_bridge.IMPORT_CONFIRM_TEXT, "我确认导入")


class TestContractReturnShapes(unittest.TestCase):
    """对夹具里的每个方法做一次**真实调用**，逐个断言 `required_keys`。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = GuiTestEnv()
        cls.api = cls.env.api
        env = cls.env
        assert cls.api.add_library(str(env.vault), "测试库")["ok"]
        vault_b = env.make_vault("vault_b", {"b.md": "# 厨房\n\n食谱笔记。"})
        assert cls.api.register_library("lib-b", "显示名B", str(vault_b))["ok"]
        env.pipeline.index_library("测试库")
        env.pipeline.index_library("lib-b")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.close()

    def _calls(self) -> dict:
        env, api = self.env, self.api

        def add_and_remove():
            added = api.add_library(str(env.make_vault("v_rm", {"a.md": "x"})), "待移除")
            assert added["ok"], added
            return api.remove_library("待移除", False)

        def batch_summaries():
            started = api.refresh_library_summaries_batch(["显示名B"], False)
            assert started["ok"], started
            deadline = time.monotonic() + 30
            while api.refresh_library_summaries_poll()["running"] and time.monotonic() < deadline:
                time.sleep(0.05)
            return started

        def start_index():
            fake = Mock(started=True, message="ok", run_id="r1", worker_pid=1)
            with patch.object(env.pipeline, "start_index_libraries", return_value=fake):
                return api.start_index(False, "测试库")

        def open_source():
            with patch("os.startfile", create=True):
                return api.open_source("测试库", "notes.md", "")

        def open_path():
            with patch("os.startfile", create=True):
                return api.open_path(str(env.vault))

        return {
            "get_snapshot": api.get_snapshot,
            "list_libraries": api.list_libraries,
            "get_library_config": lambda: api.get_library_config("测试库"),
            "add_library": lambda: api.add_library(str(env.make_vault("v_add", {"a.md": "x"})), "新增库"),
            "remove_library": add_and_remove,
            "set_library_config": lambda: api.set_library_config("测试库", {"exclude_dirs": ".trash,.git"}),
            "unset_library_config": lambda: api.unset_library_config("测试库", ["exclude_dirs"]),
            "set_library_summary": lambda: api.set_library_summary("测试库", "手写简介"),
            "refresh_library_summaries_batch": batch_summaries,
            "refresh_library_summaries_poll": api.refresh_library_summaries_poll,
            "selection_tree": lambda: api.selection_tree("测试库", ""),
            "selection_format_bulk": lambda: api.selection_format_bulk("测试库", "md", True),
            "selection_update": lambda: api.selection_update(
                "测试库", [{"path": "notes.md", "action": "in"}]
            ),
            "selection_resolve_conflict": lambda: api.selection_resolve_conflict("测试库", "no-such-dir"),
            "start_index": start_index,
            "stop_index": api.stop_index,
            "search": lambda: api.search("插件 架构", 5, "测试库", True),
            "read_document": lambda: api.read_document("测试库", "notes.md"),
            "note_relations": lambda: api.note_relations("测试库", "notes.md"),
            "open_source": open_source,
            "open_path": open_path,
            "pick_path": lambda: api.pick_path("dir", ""),
            "get_static_path": lambda: api.get_static_path("root"),
            "get_settings": api.get_settings,
            "save_settings": lambda: api.save_settings({"fusion_dense_weight": "1.5"}),
            "graph": lambda: api.graph(""),
            "semantic_edges": lambda: api.semantic_edges("", 0.62),
            "dedup_run": lambda: api.dedup_run(0.8),
            "failures": lambda: api.failures(""),
            "wemm_status": lambda: api.wemm_status("测试库"),
            "wemm_probe": api.wemm_probe,
            "preview_start": lambda: api.preview_start(str(env.tmp / "no-such.md"), None),
            "preview_poll": api.preview_poll,
            "preview_cancel": api.preview_cancel,
            "log_tail": lambda: api.log_tail(None),
            "export_run": api.export_run,
            "import_run": lambda: api.import_run("我确认导入"),
        }

    def test_every_contract_method_returns_its_frozen_key_set(self) -> None:
        fixture = _fixture()["methods"]
        calls = self._calls()
        self.assertEqual(
            sorted(calls), sorted(m["name"] for m in fixture),
            "夹具里的每个契约方法都必须在这里被真实调用一次（新增契约方法要同步加调用）",
        )
        problems: list[str] = []
        satisfied = 0
        for item in fixture:
            name = item["name"]
            try:
                result = calls[name]()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{name}: 调用抛异常 {type(exc).__name__}: {exc}")
                continue
            if item["kind"] == "array":
                if not isinstance(result, list):
                    problems.append(f"{name}: 应返回列表，实际 {type(result).__name__}")
                    continue
                if not result:
                    problems.append(f"{name}: 列表为空，无法核对 item_required_keys（夹具里应有库）")
                    continue
                missing = [k for k in item.get("item_required_keys", []) if k not in result[0]]
            else:
                if not isinstance(result, dict):
                    problems.append(f"{name}: 应返回对象，实际 {type(result).__name__}")
                    continue
                missing = [k for k in item.get("required_keys", []) if k not in result]
            if missing:
                problems.append(f"{name}: 缺键 {missing}（实际键 {sorted(result if isinstance(result, dict) else result[0])}）")
            else:
                satisfied += 1
            try:
                json.dumps(result)  # pywebview 用 json.dumps 序列化返回值
            except TypeError as exc:
                problems.append(f"{name}: 返回值不能 JSON 序列化：{exc}")
        self.assertEqual(problems, [], f"键集合满足 {satisfied}/{len(fixture)}：\n" + "\n".join(problems))


class TestDisplayNameVersusLibraryId(unittest.TestCase):
    """库的"显示名"与 `library_id` 不再混用：前端拿到什么名字就回传什么名字，都能解析；
    未知名字明确报错，不假装成功。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = GuiTestEnv()
        cls.api = cls.env.api
        env = cls.env
        # 显示名 ≠ id：由脚本/迁移工具注册的库常见
        vault = env.make_vault("vault_b", {"b.md": "# 厨房\n\n食谱笔记。", "docs/c.md": "# 文档\n\n正文"})
        assert cls.api.register_library("lib-b", "显示名B", str(vault))["ok"]
        env.pipeline.index_library("lib-b")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.close()

    def test_snapshot_and_list_use_display_names(self) -> None:
        self.assertEqual([lib["name"] for lib in self.api.get_snapshot()["libs"]], ["显示名B"])
        row = self.api.list_libraries()[0]
        self.assertEqual((row["name"], row["collection"]), ("显示名B", "lib-b"))

    def test_every_name_taking_method_resolves_display_name_and_id_identically(self) -> None:
        api = self.api
        by_name = api.selection_tree("显示名B", "")
        by_id = api.selection_tree("lib-b", "")
        self.assertIsNone(by_name["error"])
        self.assertEqual(by_name, by_id)
        self.assertEqual(api.get_library_config("显示名B"), api.get_library_config("lib-b"))
        self.assertTrue(api.read_document("显示名B", "b.md")["ok"])
        self.assertTrue(api.read_document("lib-b", "b.md")["ok"])
        self.assertTrue(api.set_library_summary("显示名B", "简介")["ok"])
        self.assertEqual(api.failures("显示名B")["error"], None)
        self.assertEqual(api.wemm_status("显示名B")["error"], None)
        self.assertEqual(api.set_library_config("显示名B", {"exclude_files": "x.md"}), {"ok": True, "errors": {}})
        self.assertEqual(api.unset_library_config("显示名B", ["exclude_files"]), {"ok": True})
        # 检索范围：前端传显示名，结果里的库标注也是显示名
        result = api.search("食谱", 5, "显示名B", True)
        self.assertIsNone(result["error"], result)
        real = [r for r in result["results"] if not r.get("notice")]
        self.assertTrue(real)
        self.assertEqual({r["lib"] for r in real}, {"显示名B"})
        self.assertEqual(api.graph("显示名B")["libs"], ["显示名B"])

    def test_unknown_library_is_an_error_never_a_fake_success(self) -> None:
        api = self.api
        self.assertTrue(api.selection_tree("不存在", "")["error"])
        self.assertTrue(api.get_library_config("不存在")["error"])
        self.assertFalse(api.set_library_config("不存在", {"exclude_dirs": "x"})["ok"])
        # 此前对着不存在的库也返回 {"ok": True}
        unset = api.unset_library_config("不存在", ["exclude_dirs"])
        self.assertFalse(unset["ok"])
        self.assertTrue(unset["error"])
        self.assertFalse(api.read_document("不存在", "a.md")["ok"])
        self.assertFalse(api.set_library_summary("不存在", "x")["ok"])
        # 此前把未知库当"无失败"
        self.assertTrue(api.failures("不存在")["error"])
        self.assertTrue(api.wemm_status("不存在")["error"])
        self.assertFalse(api.selection_update("不存在", [{"path": "a", "action": "in"}])["ok"])
        self.assertFalse(api.start_index(False, "不存在")["ok"])
        self.assertTrue(api.search("x", 5, "不存在", True)["error"])

    def test_add_library_makes_the_name_the_identity_like_the_legacy_project(self) -> None:
        """旧项目里名字就是库的身份：目录名带空格也照原样当 id，不再由目录名"推导"出
        另一个 id（那正是显示名≠id 的一大来源）。"""
        env = self.env
        vault = env.make_vault("My Vault 2", {"a.md": "# 甲\n\n乙"})
        added = self.api.add_library(str(vault), None)
        self.addCleanup(env.lib_mgr.store.remove_library, "My Vault 2")  # 共享环境：别污染其他用例
        self.assertEqual(added, {"ok": True, "library_id": "My Vault 2"})
        self.assertIn("My Vault 2", [lib["name"] for lib in self.api.list_libraries()])
        self.assertTrue(self.api.selection_tree("My Vault 2", "")["error"] is None)
        # 名字与已有库的 id/显示名都不能重（旧"库名已存在"）
        dup = self.api.add_library(str(env.make_vault("dup", {"a.md": "x"})), "显示名B")
        self.assertFalse(dup["ok"])
        self.assertIn("已存在", dup["error"])
        dup_id = self.api.add_library(str(env.make_vault("dup2", {"a.md": "x"})), "lib-b")
        self.assertFalse(dup_id["ok"])

    def test_ambiguous_display_name_is_reported_with_the_candidate_ids(self) -> None:
        env = self.env
        env.lib_mgr.store.add_library("dup-1", "同名", str(env.make_vault("s1", {"a.md": "x"})))
        env.lib_mgr.store.add_library("dup-2", "同名", str(env.make_vault("s2", {"a.md": "x"})))
        self.addCleanup(env.lib_mgr.store.remove_library, "dup-1")  # 共享环境：别污染其他用例
        self.addCleanup(env.lib_mgr.store.remove_library, "dup-2")
        result = self.api.selection_tree("同名", "")
        self.assertIn("dup-1", result["error"])
        self.assertIn("dup-2", result["error"])
        self.assertIsNone(self.api.selection_tree("dup-1", "")["error"])


class TestSelectionTreeMatchesLegacySemantics(unittest.TestCase):
    """`selection_tree` 与旧 guiweb/bridge.py:434-557 的语义一致（审计 HIGH-5 的四处偏差）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = GuiTestEnv()
        cls.api = cls.env.api
        env = cls.env
        vault = env.make_vault("tree", {
            "notes.md": "# 顶层\n\n正文",
            "report.docx": "not really a docx",
            "docs/a.md": "# 甲\n\n正文",
            "docs/private/secret.md": "# 密\n\n正文",
            "private/p.md": "# 私\n\n正文",
            "drafts/x.md": "# 草\n\n正文",
            ".obsidian/app.json": "{}",
            ".gitignore": "x",
        })
        assert cls.api.add_library(str(vault), "树库")["ok"]
        env.lib_mgr.store.set_policy("树库", enabled_extensions=[".md"], exclude_dirs=["private"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.close()

    def _tree(self, sub: str = "") -> dict:
        tree = self.api.selection_tree("树库", sub)
        self.assertIsNone(tree["error"], tree)
        return tree

    def test_folder_containers_follow_their_children_not_the_file_format_rule(self) -> None:
        dirs = {d["name"]: d for d in self._tree()["dirs"]}
        # 此前所有文件夹都走扩展名判定 → 一律"排除"
        self.assertEqual((dirs["docs"]["state"], dirs["docs"]["state_text"]), ("auto_in", "入库（跟随子内容）"))
        self.assertEqual(dirs["drafts"]["state"], "auto_in")

    def test_excluded_directories_stay_visible_and_flag_self_blocked(self) -> None:
        tree = self._tree()
        dirs = {d["name"]: d for d in tree["dirs"]}
        self.assertIn("private", dirs, "被排除的目录必须照样显示，用户才有地方点它")
        private = dirs["private"]
        self.assertEqual((private["state"], private["explicit"], private["state_text"]),
                         ("out", None, "已排除（排除名单）"))
        self.assertTrue(private["self_blocked"])
        nested = {d["name"]: d for d in self._tree("docs")["dirs"]}
        self.assertEqual(nested["private"]["state"], "out")
        folder_paths = {f["path"] for f in tree["folders"]}
        self.assertTrue({"docs", "docs/private", "private", "drafts"} <= folder_paths)
        self.assertEqual(tree["folders"][0], {
            "path": "", "depth": 0, "name": "树库", "explicit": None,
            "state": "root", "state_text": "库根",
        })

    def test_files_of_disabled_formats_are_listed_as_auto_out(self) -> None:
        files = {f["name"]: f for f in self._tree()["files"]}
        self.assertIn("report.docx", files, "未启用格式的文件此前直接消失")
        self.assertEqual((files["report.docx"]["state"], files["report.docx"]["state_text"]),
                         ("auto_out", "排除（跟随格式）"))
        self.assertEqual(files["notes.md"]["state"], "auto_in")
        self.assertEqual(files["notes.md"]["state_text"], "入库（跟随格式）")
        self.assertIn(".gitignore", files, "旧项目只跳过隐藏**目录**，隐藏文件照列")
        names = {d["name"] for d in self._tree()["dirs"]}
        self.assertNotIn(".obsidian", names)

    def test_illegal_or_missing_sub_returns_an_error(self) -> None:
        for sub in ("../outside", "/abs", "C:/x", "notes.md", "no-such-dir"):
            with self.subTest(sub=sub):
                self.assertTrue(self._raw(sub)["error"], sub)

    def _raw(self, sub: str) -> dict:
        return self.api.selection_tree("树库", sub)

    def test_tree_display_agrees_with_the_indexing_funnel(self) -> None:
        """显示=实际：树上每个文件的"入库/不入库"必须与建索引用的文件漏斗同一个结论。"""
        env = self.env
        # 隐藏目录（.obsidian 等）旧项目就不进面板，所以只比较树能显示到的文件
        decisions = {
            path: included
            for path, included, _ in env.lib_mgr.resolve_included_files("树库")
            if not any(part.startswith(".") for part in path.split("/")[:-1])
        }
        seen: dict[str, bool] = {}
        for folder in self._tree()["folders"]:
            for f in self._tree(folder["path"])["files"]:
                seen[f["path"]] = f["state"] in ("in", "auto_in")
        self.assertEqual(seen, decisions)

    def test_resolve_conflict_moves_the_dir_out_of_the_exclusion_list_and_includes_it(self) -> None:
        result = self.api.selection_resolve_conflict("树库", "private")
        self.assertTrue(result["ok"], result)
        self.assertIn("private", result["selection_in"])
        cfg = self.env.lib_mgr.store.get("树库")
        self.assertNotIn("private", cfg.exclude_dirs)
        dirs = {d["name"]: d for d in self._tree()["dirs"]}
        self.assertEqual(dirs["private"]["state"], "in")
        again = self.api.selection_resolve_conflict("树库", "private")
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"], "该目录已不在排除名单里，直接勾选即可")


class TestSettingsPage(unittest.TestCase):
    """设置页：分组、当前生效值、按声明类型保存、往返一致（审计 HIGH-1）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = GuiTestEnv(plugins=["official-library-manager"])
        cls.api = cls.env.api
        cls.settings = cls.env.runtime.settings

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.close()

    def setUp(self) -> None:
        for key in list(self.settings.all()):
            self.settings.unset(key)

    def test_page_has_real_groups_with_effective_values(self) -> None:
        from official_gui_shell.settings_schema import FIELDS, GROUPS

        page = self.api.get_settings()
        self.assertEqual(page["missing_keys"], [])
        self.assertEqual([g["title"] for g in page["groups"]], [g.title for g in GROUPS])
        self.assertGreaterEqual(len(page["groups"]), 8)
        fields = {f["key"]: f for g in page["groups"] for f in g["fields"]}
        self.assertEqual(set(fields), set(FIELDS))
        # 没设过的键显示的是默认值，不是空串（前端保存会把页面上所有字段一起发回）
        self.assertEqual(fields["max_chunks_per_file"]["value"], "3")
        self.assertEqual(fields["hyde_enabled"]["value"], "false")
        self.assertEqual(fields["rerank_enabled"]["value"], "true")
        self.assertEqual(fields["default_libraries"]["value"], "")
        self.assertTrue(fields["hyde_llm_api_key"]["secret"])
        self.assertTrue(fields["pdf_scan_backend"]["choices"])
        self.assertEqual(fields["default_libraries"]["kind"], "list")

    def test_mineru_key_and_model_version_are_on_the_page_and_the_key_is_secret(self) -> None:
        """2026-09-29 操作者反馈“设置里 MinerU API 完全不见了”：旧项目设置页有这两项，云端识别读它们。"""
        page = self.api.get_settings()
        group = next(g for g in page["groups"] if g["title"] == "PDF 与云端 OCR")
        fields = {f["key"]: f for f in group["fields"]}
        self.assertTrue(fields["mineru_api_key"]["secret"])
        self.assertEqual(fields["mineru_api_key"]["value"], "")
        self.assertEqual([c[0] for c in fields["mineru_model_version"]["choices"]], ["vlm", "pipeline"])
        self.assertEqual(fields["mineru_model_version"]["value"], "vlm")
        self.assertEqual(self.api.save_settings({"mineru_api_key": "0123456789", "mineru_model_version": "pipeline"}), {"errors": {}})
        self.assertEqual(self.settings.get("mineru_api_key", ""), "0123456789")  # 全数字也不被转成整数
        self.assertEqual(self.settings.get("mineru_model_version", "vlm"), "pipeline")
        again = {f["key"]: f for g in self.api.get_settings()["groups"] for f in g["fields"]}
        self.assertEqual(again["mineru_api_key"]["value"], "0123456789")
        self.assertEqual(self.api.save_settings({"mineru_model_version": "nonsense"})["errors"].keys(), {"mineru_model_version"})

    def test_saving_the_untouched_page_pins_nothing(self) -> None:
        """前端保存时把每个字段都发回：值等于默认值就清除该键，不把默认值钉死。"""
        page = self.api.get_settings()
        updates = {f["key"]: f["value"] for g in page["groups"] for f in g["fields"]}
        self.assertEqual(self.api.save_settings(updates), {"errors": {}})
        self.assertEqual(self.settings.all(), {})

    def test_every_field_round_trips_through_save_and_read_back(self) -> None:
        from official_gui_shell.settings_schema import FIELDS

        expected: dict[str, object] = {}
        updates: dict[str, str] = {}
        for key, field in FIELDS.items():
            if field.kind == "bool":
                value: object = not field.default
                text = "true" if value else "false"
            elif field.kind == "int":
                value = int(field.default) + 7
                text = str(value)
            elif field.kind == "float":
                value = float(field.default) + 0.25
                text = str(value)
            elif field.kind == "list":
                value = ["库甲", "库乙"]
                text = "库甲,库乙"
            elif field.choices:
                value = next(c[0] for c in field.choices if c[0] != field.default)
                text = value
            elif field.secret:
                value = text = "0123456789"  # 全数字的 Key 不能被当成数字
            else:
                value = text = f"值-{key}"
            expected[key] = value
            updates[key] = text
        self.assertEqual(self.api.save_settings(updates), {"errors": {}})
        for key, want in expected.items():
            with self.subTest(key=key):
                got = self.settings.get(key, FIELDS[key].default)
                self.assertEqual(got, want)
                self.assertIs(type(got), type(want))
        # 存到磁盘的值再经页面读回，字符串形式与保存时一致
        page = {f["key"]: f["value"] for g in self.api.get_settings()["groups"] for f in g["fields"]}
        for key, text in updates.items():
            with self.subTest(page_key=key):
                self.assertEqual(page[key], text)

    def test_bad_value_rejects_the_whole_batch_and_writes_nothing(self) -> None:
        result = self.api.save_settings({
            "fusion_dense_weight": "2.5",          # 合法
            "max_chunks_per_file": "三",             # 非法整数
            "hyde_enabled": "maybe",                # 非法开关
        })
        self.assertEqual(set(result["errors"]), {"max_chunks_per_file", "hyde_enabled"})
        self.assertTrue(all("格式错误" in msg for msg in result["errors"].values()))
        self.assertEqual(self.settings.all(), {}, "任一失败即整体中止：合法的那个键也不能落盘")

    def test_choices_are_validated_and_unknown_keys_are_skipped(self) -> None:
        bad = self.api.save_settings({"pdf_scan_backend": "totally-fake"})
        self.assertIn("pdf_scan_backend", bad["errors"])
        ok = self.api.save_settings({"pdf_scan_backend": "mineru-local", "not_a_real_key": "1"})
        self.assertEqual(ok, {"errors": {}})
        self.assertEqual(self.settings.all(), {"pdf_scan_backend": "mineru-local"})

    def test_previously_dropped_values_now_stick(self) -> None:
        """审计里 4/4 探针值被丢弃或损坏：default_libraries='vault' 被忽略、全数字 API Key 变成整数。"""
        self.assertEqual(self.api.save_settings({
            "default_libraries": "vault", "hyde_llm_api_key": "0123456789", "hyde_llm_max_tokens": "300",
        }), {"errors": {}})
        self.assertEqual(self.settings.get("default_libraries", []), ["vault"])
        self.assertEqual(self.settings.get("hyde_llm_api_key", ""), "0123456789")
        self.assertEqual(self.settings.get("hyde_llm_max_tokens", 200), 300)


class TestProgressSnapshot(unittest.TestCase):
    """进度段来自 `pipeline.index_status`，不再是写死的 0。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = GuiTestEnv(plugins=["official-library-manager"])
        cls.api = cls.env.api
        vault = cls.env.make_vault("pv", {"a.md": "# a\n\nx"})
        assert cls.api.add_library(str(vault), "进度库")["ok"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.close()

    def _snapshot_with(self, status: dict | None) -> dict:
        with patch.object(self.env.pipeline, "index_status", return_value=status):
            return self.api.get_snapshot()

    @staticmethod
    def _status(**overrides) -> dict:
        now = time.time()
        base = {
            "library_id": "进度库", "run_id": "r1", "stage": "running", "phase": "extracting",
            "files_done": 3, "files_total": 12, "chunks_done": 0, "chunks_total": None,
            "started_at": now - 40, "heartbeat_at": now, "progress_at": now,
            "stall_grace_until": None, "finished_at": None, "elapsed_s": 40.0, "eta_s": 100.0,
            "percent": 25.0, "owner": "self", "active": True, "can_stop": True, "health": "healthy",
            "message": "", "error": None,
        }
        base.update(overrides)
        return base

    def test_idle_when_nothing_ever_ran(self) -> None:
        progress = self._snapshot_with(None)["progress"]
        self.assertEqual((progress["running"], progress["phase"], progress["heartbeat"], progress["task"]),
                         (False, "idle", "idle", "idle"))
        self.assertIsNone(progress["elapsed"])

    def test_running_maps_core_status_into_the_legacy_shape(self) -> None:
        snap = self._snapshot_with(self._status())
        progress = snap["progress"]
        self.assertTrue(progress["running"])
        self.assertEqual(progress["phase"], "converting")
        self.assertEqual((progress["files_done"], progress["files_total"]), (3, 12))
        self.assertAlmostEqual(progress["pct"], 25.0)  # 前端按 0~100 用
        self.assertEqual(progress["library"], "进度库")
        self.assertEqual((progress["heartbeat"], progress["task"], progress["busy"]), ("running", "ours", True))
        self.assertEqual(progress["elapsed"], 40.0)
        self.assertEqual(progress["heartbeat_note"], "文档转换中（大文件耗时属预期）")
        self.assertEqual(snap["last_elapsed"], 40.0)

    def test_progress_moves_when_core_status_changes(self) -> None:
        low = self._snapshot_with(self._status(files_done=1, phase="embedding"))["progress"]
        high = self._snapshot_with(self._status(files_done=9, phase="writing"))["progress"]
        self.assertLess(low["pct"], high["pct"])
        self.assertEqual((low["phase"], high["phase"]), ("embedding", "writing"))

    def test_foreign_process_is_reported_as_foreign(self) -> None:
        progress = self._snapshot_with(self._status(owner="foreign", can_stop=False))["progress"]
        self.assertEqual(progress["task"], "foreign")

    def test_heartbeat_dead_and_stalled_and_the_alert_fires_once_per_degradation(self) -> None:
        pushes: list[tuple[str, dict]] = []
        with patch.object(self.api, "_push", side_effect=lambda t, p: pushes.append((t, p))):
            dead = self._snapshot_with(self._status(health="stalled_no_heartbeat"))["progress"]
            self._snapshot_with(self._status(health="orphaned"))
            alerts = [p for t, p in pushes if t == "alert"]
            self.assertEqual(dead["heartbeat"], "dead")
            self.assertIsNone(dead["heartbeat_note"], "DEAD 优先：红胶囊不能配'宽限内'文案")
            self.assertEqual(len(alerts), 1, "同一次劣化只推一次")
            self.assertEqual(alerts[0]["level"], "dead")
            stalled = self._snapshot_with(self._status(health="stalled_no_progress"))["progress"]
            self.assertEqual(stalled["heartbeat"], "stalled")
            self._snapshot_with(self._status(health="stalled_no_heartbeat"))  # 恢复后再劣化 → 再响一次
            self.assertEqual(len([1 for t, _ in pushes if t == "alert"]), 2)

    def test_done_and_terminal_states(self) -> None:
        done = self._snapshot_with(self._status(
            stage="done", phase="finalizing", active=False, finished_at=time.time(), percent=100.0,
            files_done=12, elapsed_s=55.0, can_stop=False,
        ))["progress"]
        self.assertEqual((done["running"], done["phase"], done["heartbeat"], done["pct"]), (False, "done", "done", 100.0))
        self.assertEqual(done["elapsed"], 55.0)
        cancelled = self._snapshot_with(self._status(
            stage="cancelled", active=False, finished_at=time.time(), can_stop=False,
        ))["progress"]
        self.assertEqual((cancelled["running"], cancelled["phase"], cancelled["heartbeat"]), (False, "idle", "idle"))

    def test_stop_index_uses_core_ownership_not_a_private_run_registry(self) -> None:
        with patch.object(self.env.pipeline, "index_status", return_value=self._status(owner="foreign", can_stop=False)):
            foreign = self.api.stop_index()
        self.assertEqual((foreign["ok"], foreign["stopped"]), (False, False))
        self.assertIn("不是本应用启动", foreign["reason"])
        with patch.object(self.env.pipeline, "index_status", return_value=None):
            idle = self.api.stop_index()
        self.assertEqual((idle["ok"], idle["stopped"]), (True, False))
        with patch.object(self.env.pipeline, "index_status", return_value=self._status()), \
                patch.object(self.env.pipeline, "stop_index_library", return_value=(True, "索引任务已取消")) as stop:
            mine = self.api.stop_index()
        self.assertEqual((mine["ok"], mine["stopped"]), (True, True))
        stop.assert_called_once_with("进度库", "r1")

    def test_start_index_refuses_while_running_and_reports_the_real_failure(self) -> None:
        with patch.object(self.env.pipeline, "index_status", return_value=self._status()):
            busy = self.api.start_index(False, "进度库")
        self.assertEqual((busy["ok"], busy.get("already_running")), (False, True))
        fail = Mock(started=False, message="启动失败：无法获取库锁", run_id="", worker_pid=None)
        with patch.object(self.env.pipeline, "index_status", return_value=None), \
                patch.object(self.env.pipeline, "start_index_libraries", return_value=fail):
            failed = self.api.start_index(True, "")
        self.assertFalse(failed["ok"])
        self.assertNotIn("already_running", failed, "启动失败不是'已在运行'，此前一律误标成 already_running")
        self.assertIn("无法获取库锁", failed["error"])



class TestStartIndexBatch(unittest.TestCase):
    """一次重建多个库：整批交给 core 依次排队（旧项目一次点击只起一个索引进程，逐库循环）。

    此前入口层对每个库各调一次 `start_index_library`，4 个库 = 4 个 worker 同时开跑，
    同时加载模型压满显卡和 CPU（2026-09-29 真机复现）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = GuiTestEnv(plugins=["official-library-manager"])
        cls.api = cls.env.api
        for name in ("库甲", "库乙", "库丙"):
            vault = cls.env.make_vault(f"v_{name}", {"a.md": "# a\n\nx"})
            assert cls.api.add_library(str(vault), name)["ok"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.close()

    def test_start_index_hands_the_whole_batch_to_core_in_one_call(self) -> None:
        ok = Mock(started=True, message="ok", run_id="r1", worker_pid=1)
        with patch.object(self.env.pipeline, "index_status", return_value=None), \
                patch.object(self.env.pipeline, "start_index_libraries", return_value=ok) as batch, \
                patch.object(self.env.pipeline, "start_index_library") as single:
            result = self.api.start_index(False, "")
        self.assertTrue(result["ok"], result)
        batch.assert_called_once()
        ids = list(batch.call_args.args[0])
        self.assertEqual(len(ids), 3)
        self.assertEqual(result["started"], ids)
        self.assertEqual(batch.call_args.kwargs, {"source": "gui", "full": False})
        single.assert_not_called()

    def test_progress_shows_the_running_library_not_every_queued_name(self) -> None:
        """排队中的库（phase=queued）不并进进度条的库名/阶段，只有排队的空档才显示队首。"""
        now = time.time()

        def status(library_id: str) -> dict:
            queued = library_id != "库甲"
            return {
                "library_id": library_id, "run_id": "" if queued else "r1", "stage": "starting" if queued else "running",
                "phase": "queued" if queued else "embedding",
                "files_done": 0 if queued else 5, "files_total": 0 if queued else 10,
                "chunks_done": 0, "chunks_total": None,
                "started_at": now - 30 + (1 if library_id == "库丙" else 0), "heartbeat_at": now, "progress_at": now,
                "stall_grace_until": None, "finished_at": None, "elapsed_s": 30.0, "eta_s": None,
                "percent": 0.0, "owner": "self", "active": True, "can_stop": True, "health": "healthy",
                "message": "", "error": None,
            }

        with patch.object(self.env.pipeline, "index_status", side_effect=status):
            progress = self.api.get_snapshot()["progress"]
        self.assertTrue(progress["running"])
        self.assertEqual(progress["library"], "库甲")
        self.assertEqual(progress["phase"], "embedding")
        self.assertEqual((progress["files_done"], progress["files_total"]), (5, 10))

    def test_gap_between_two_libraries_still_reads_as_running(self) -> None:
        """上一个库刚结束、下一个还没起来的交接空档：只剩排队中的库，界面不能闪成"完成/空闲"。"""
        now = time.time()

        def status(library_id: str) -> dict | None:
            if library_id == "库甲":  # 刚跑完
                return {
                    "library_id": library_id, "run_id": "r1", "stage": "done", "phase": "finalizing",
                    "files_done": 10, "files_total": 10, "chunks_done": 3, "chunks_total": 3,
                    "started_at": now - 60, "heartbeat_at": now, "progress_at": now,
                    "stall_grace_until": None, "finished_at": now - 1, "elapsed_s": 59.0, "eta_s": 0.0,
                    "percent": 100.0, "owner": "self", "active": False, "can_stop": False, "health": "healthy",
                    "message": "", "error": None,
                }
            return {
                "library_id": library_id, "run_id": "", "stage": "starting", "phase": "queued",
                "files_done": 0, "files_total": 0, "chunks_done": 0, "chunks_total": None,
                "started_at": now - 60 + (1 if library_id == "库丙" else 0), "heartbeat_at": now, "progress_at": now,
                "stall_grace_until": None, "finished_at": None, "elapsed_s": 60.0, "eta_s": None,
                "percent": 0.0, "owner": "self", "active": True, "can_stop": True, "health": "healthy",
                "message": "", "error": None,
            }

        with patch.object(self.env.pipeline, "index_status", side_effect=status):
            progress = self.api.get_snapshot()["progress"]
        self.assertTrue(progress["running"], "交接空档不能被显示成已完成")
        self.assertEqual(progress["library"], "库乙", "只显示队首，不把所有排队的库名连起来")


class TestPreviewLifecycle(unittest.TestCase):
    """提取试验台：独立子进程，取消/超时**真的终止**任务（审计 M-2）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = GuiTestEnv(plugins=["official-library-manager", "official-extractor-text"])
        cls.api = cls.env.api
        cls.md = cls.env.tmp / "sample.md"
        cls.md.write_text("# 试验台\n\n一段正文。", encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.close()

    def tearDown(self) -> None:
        self.api.preview_cancel()
        self.api._pv_target = contract_bridge.preview_job

    def _poll_until_done(self, timeout: float = 90.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.api.preview_poll()
            if state["done"]:
                return state
            time.sleep(0.2)
        self.fail("试验台在限定时间内没有完成")

    def test_real_child_process_extracts_a_markdown_file(self) -> None:
        self.assertEqual(self.api.preview_start(str(self.md), None), {"ok": True})
        self.assertFalse(self.api.preview_start(str(self.md), None)["ok"], "已有预览在运行时应拒绝并发")
        state = self._poll_until_done()
        result = state["result"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "")
        self.assertIn("一段正文", result["markdown"])
        self.assertIn("<", result["rendered_html"])
        self.assertEqual(result["route"], "local")
        self.assertEqual(result["chars"], len(result["markdown"]))

    def test_missing_file_is_rejected_up_front(self) -> None:
        result = self.api.preview_start(str(self.env.tmp / "nope.md"), None)
        self.assertFalse(result["ok"])
        self.assertIn("文件不存在", result["error"])

    def test_cancel_really_terminates_the_child_process(self) -> None:
        self.api._pv_target = __import__("preview_stub_jobs").sleep_job
        self.assertTrue(self.api.preview_start(str(self.md), None)["ok"])
        proc = self.api._pv_proc
        time.sleep(1.0)
        self.assertTrue(proc.is_alive(), "桩任务应该在睡眠中")
        self.assertEqual(self.api.preview_poll(), {"running": True, "done": False, "result": None})
        self.assertEqual(self.api.preview_cancel(), {"ok": True})
        proc.join(timeout=10)
        self.assertFalse(proc.is_alive(), "取消后子进程必须真的停下（此前只清了状态）")
        self.assertTrue(self.api.preview_poll()["done"])

    def test_hard_timeout_terminates_and_reports(self) -> None:
        self.api._pv_target = __import__("preview_stub_jobs").sleep_job
        with patch.object(contract_bridge, "PREVIEW_TIMEOUT_S", 1.0):
            self.assertTrue(self.api.preview_start(str(self.md), None)["ok"])
            proc = self.api._pv_proc
            time.sleep(1.5)
            state = self.api.preview_poll()
        self.assertTrue(state["done"])
        self.assertFalse(state["result"]["ok"])
        self.assertIn("预览超时", state["result"]["error"])
        proc.join(timeout=10)
        self.assertFalse(proc.is_alive())

    def test_crashing_child_is_reported_as_abnormal_exit(self) -> None:
        self.api._pv_target = __import__("preview_stub_jobs").crash_job
        self.assertTrue(self.api.preview_start(str(self.md), None)["ok"])
        state = self._poll_until_done(timeout=30)
        self.assertFalse(state["result"]["ok"])
        self.assertIn("异常退出", state["result"]["error"])

    def test_result_shape_mapping_is_the_legacy_one(self) -> None:
        """`ok` = 子进程正常交付；是否产出内容看 `reason`（旧 `_preview_result_of`）。"""
        produced = Api._preview_result_of({
            "ok": True, "error": None,
            "info": {"md": "# 标题\n\n正文", "reason": "", "route": "extractor:official-extractor-pdf-text",
                     "elapsed": 1.5, "chars": 9},
        })
        self.assertEqual((produced["ok"], produced["reason"], produced["route"]), (True, "", "local"))
        skipped = Api._preview_result_of({
            "ok": True, "error": None,
            "info": {"md": "", "reason": "scanned", "route": "extractor:official-extractor-pdf-text"},
        })
        self.assertEqual((skipped["ok"], skipped["reason"], skipped["markdown"]), (True, "scanned", ""))
        failed = Api._preview_result_of({"ok": False, "error": "boom", "info": {}})
        self.assertEqual((failed["ok"], failed["error"], failed["route"]), (False, "boom", "-"))


class TestLogAndPush(unittest.TestCase):
    """日志：GUI 动作写进与 worker 同一份日志；推送只在不会重复/丢行时才发。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = GuiTestEnv(plugins=["official-library-manager"])
        cls.api = cls.env.api

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.close()

    def setUp(self) -> None:
        self.api._index_log_path().unlink(missing_ok=True)
        self.api._log_seen_cursor = 0

    def test_gui_actions_land_in_the_shared_log_in_the_frontend_line_format(self) -> None:
        self.api._log("添加库：甲")
        self.api._log("出错了", is_error=True)
        lines = self.api.log_tail(None)["lines"]
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[GUI\] 添加库：甲$")
        self.assertRegex(lines[1], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[GUI\] ERROR 出错了$")

    def test_cursor_is_a_byte_offset_and_returns_only_new_lines(self) -> None:
        self.api._log("第一行")
        first = self.api.log_tail(None)
        self.api._log("第二行")
        more = self.api.log_tail(first["cursor"])
        self.assertEqual(len(more["lines"]), 1)
        self.assertIn("第二行", more["lines"][0])
        self.assertEqual(self.api.log_tail(more["cursor"])["lines"], [])

    def test_log_push_only_when_it_cannot_duplicate_or_skip(self) -> None:
        window = Mock()
        self.api.bind_window(window)
        self.addCleanup(self.api.bind_window, None)
        # 前端游标与文件末尾一致 → 推送并同步游标
        self.api._log("甲")
        self.assertEqual(window.evaluate_js.call_count, 1)
        script = window.evaluate_js.call_args[0][0]
        # 旧 `Bridge._push` 的线格式：事件名用 json.dumps（双引号），载荷紧随其后
        self.assertTrue(script.startswith('window.__push && window.__push("log", '), script)
        payload = json.loads(script.split(", ", 1)[1].rstrip(")"))
        self.assertEqual(payload["cursor"], self.api._index_log_path().stat().st_size)
        # worker 悄悄追加了一行（前端不知道）→ 不推，留给轮询，避免跳过那一行
        with self.api._index_log_path().open("ab") as handle:
            handle.write("worker 输出\n".encode("utf-8"))
        self.api._log("乙")
        self.assertEqual(window.evaluate_js.call_count, 1)
        # 前端轮询追平后又恢复推送
        self.api.log_tail(None)
        self.api._log("丙")
        self.assertEqual(window.evaluate_js.call_count, 2)

    def test_static_paths(self) -> None:
        data_dir = str(Path(self.env.runtime.data_dir))
        self.assertEqual(self.api.get_static_path("logs"), {"path": data_dir})
        self.assertEqual(self.api.get_static_path("log_dir"), {"path": data_dir}, "前端实际传的是 log_dir")
        self.assertTrue(self.api.get_static_path("root")["path"])
        self.assertEqual(self.api.get_static_path("nope"), {"path": ""})

    def test_semantic_edges_pushes_a_progress_notice(self) -> None:
        window = Mock()
        self.api.bind_window(window)
        self.addCleanup(self.api.bind_window, None)
        with patch.object(self.env.pipeline, "graph_semantic_edges",
                          return_value=Mock(edges=(), error=None)):
            result = self.api.semantic_edges("", 0.62)
        self.assertEqual(result, {"edges": [], "error": None})
        script = window.evaluate_js.call_args[0][0]
        self.assertTrue(script.startswith('window.__push && window.__push("notice", '), script)
        self.assertIn("正在加载嵌入模型", script)


if __name__ == "__main__":
    unittest.main()
