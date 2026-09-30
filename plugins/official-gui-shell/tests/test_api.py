"""Api 类是纯 Python，不需要真的渲染一个窗口就能测——这里覆盖的是
"GUI后端调用Pipeline/library-manager对不对、失败会不会被折叠成
{"ok": False, ...}而不是让调用方（js_api桥）异常"。渲染层与启动路径的验证见
test_boot.py（真实运行时加载 + 真实 gui_main.main()）与 test_contracts_parity.py
（37 个契约方法逐个真实调用）。

**两套入口各自的覆盖**（2026-09-28 审计 M-7：BC-15 阶段B 把精确 API 改名后这里没有迁移，
33 条里 22 条报错）：

- 本文件测**精确 API**（库 id 在前，供 MCP/CLI/脚本用）：`register_library` /
  `search_scoped` / `stop_index_run` / `get_settings_values` / `dedup_run_library` /
  `document_text` …；
- 同一批行为经**契约 API**（前端真正调用的名字与参数顺序）的对应用例也在这里：
  `add_library(path, name)` / `search(query, top_k, libraries, include_body)` /
  `stop_index()` / `get_settings()` / `dedup_run(threshold)` / `read_document(lib, rel)` …
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_TESTS_DIR = Path(__file__).parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from gui_test_env import GuiTestEnv  # noqa: E402


class TestApi(unittest.TestCase):
    def setUp(self) -> None:
        self.env = GuiTestEnv()
        self.addCleanup(self.env.close)
        self.tmp = self.env.tmp
        self.vault = self.env.vault
        self.fake_llm = self.env.fake_llm
        self.runtime = self.env.runtime
        self.pipeline = self.env.pipeline
        self.api = self.env.api

    # ---- 库 -------------------------------------------------------------

    def test_list_libraries_empty_initially(self):
        self.assertEqual(self.api.list_libraries(), [])

    def test_register_library_then_list(self):
        result = self.api.register_library("lib1", "测试库", str(self.vault))
        self.assertTrue(result["ok"])
        libs = self.api.list_libraries()
        self.assertEqual(len(libs), 1)
        self.assertEqual(libs[0]["collection"], "lib1")  # 库 id
        self.assertEqual(libs[0]["name"], "测试库")  # 前端看到的是显示名

    def test_contract_add_library_uses_the_name_as_identity(self):
        """契约 `add_library(path, name)`：旧项目里名字就是库身份，库 id 取名字本身。"""
        result = self.api.add_library(str(self.vault), "测试库")
        self.assertEqual(result, {"ok": True, "library_id": "测试库"})
        self.assertEqual(self.api.list_libraries()[0]["collection"], "测试库")

    def test_add_duplicate_library_returns_error_not_exception(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        result = self.api.register_library("lib1", "重复", str(self.vault))
        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        duplicate = self.api.add_library(str(self.vault), "另一个名字")
        self.assertFalse(duplicate["ok"], "同一目录不能注册两次")

    # ---- 索引 -----------------------------------------------------------

    def test_index_and_search_round_trip(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        report = self.pipeline.index_library("lib1")
        self.assertEqual(report.succeeded, 1)

        search_result = self.api.search_scoped("lib1", "插件 架构")
        self.assertTrue(search_result["ok"])
        self.assertGreater(len(search_result["results"]), 0)
        self.assertEqual(search_result["results"][0]["path"], "notes.md")
        self.assertIn("confidence_tier", search_result["results"][0])

    def test_contract_search_has_the_legacy_shape_and_argument_order(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        result = self.api.search("插件 架构", 5, "lib1", True)
        self.assertIsNone(result["error"])
        hits = [r for r in result["results"] if not r.get("notice")]
        self.assertGreater(len(hits), 0)
        first = hits[0]
        self.assertEqual((first["lib"], first["rel"]), ("测试库", "notes.md"))
        self.assertTrue(first["body"])
        self.assertIn("<", first["rendered_html"])
        self.assertIn("confidence", first)
        # 块序号来自 core，不再是写死的"块 1/1"
        self.assertEqual(first["chunk_total"], self.pipeline.search("lib1", "插件 架构")[0].total_chunks)
        self.assertEqual(self.api.search("   ", 5, "lib1", True), {"results": [], "error": "请输入问题"})

    def test_reindex_library_starts_background_index_from_gui(self):
        start_result = Mock(
            started=True,
            message="已开始后台重建索引",
            run_id="run-1",
            worker_pid=1234,
        )
        with patch.object(
            self.pipeline, "start_index_library", return_value=start_result
        ) as start_index:
            result = self.api.reindex_library("lib1")
        self.assertEqual(
            result,
            {
                "ok": True,
                "started": True,
                "message": "已开始后台重建索引",
                "run_id": "run-1",
                "worker_pid": 1234,
                "full": False,
            },
        )
        start_index.assert_called_once_with("lib1", source="gui")

    def test_reindex_library_can_force_full_rebuild(self):
        start_result = Mock(started=True, message="started", run_id="run-2", worker_pid=2345)
        with patch.object(self.pipeline, "start_index_library", return_value=start_result) as start_index:
            result = self.api.reindex_library("lib1", full=True)
        self.assertTrue(result["full"])
        start_index.assert_called_once_with("lib1", source="gui", full=True)

    def test_index_status_returns_none_or_pipeline_dict(self):
        status = {"stage": "running", "files_done": 1}
        with patch.object(
            self.pipeline, "index_status", side_effect=[None, status]
        ) as index_status:
            self.assertEqual(self.api.index_status("lib1"), {"ok": True, "status": None})
            self.assertEqual(self.api.index_status("lib1"), {"ok": True, "status": status})
        self.assertEqual(index_status.call_count, 2)
        index_status.assert_called_with("lib1")

    def test_stop_index_run_returns_pipeline_result(self):
        for stopped, message in (
            (True, "索引任务已取消"),
            (False, "拒绝停止：索引任务 run_id 不匹配"),
        ):
            with self.subTest(stopped=stopped):
                with patch.object(
                    self.pipeline, "stop_index_library", return_value=(stopped, message)
                ) as stop_index:
                    result = self.api.stop_index_run("lib1", "run-1")
                self.assertEqual(
                    result,
                    {"ok": True, "stopped": stopped, "message": message},
                )
                stop_index.assert_called_once_with("lib1", "run-1")

    def test_index_api_folds_unknown_library_errors(self):
        error = "未知库: no-such-lib"
        expected_error = str(KeyError(error))
        calls = (
            ("start_index_library", lambda: self.api.reindex_library("no-such-lib")),
            ("index_status", lambda: self.api.index_status("no-such-lib")),
            ("stop_index_library", lambda: self.api.stop_index_run("no-such-lib", "run-1")),
        )
        for method_name, call in calls:
            with self.subTest(method_name=method_name):
                with patch.object(self.pipeline, method_name, side_effect=KeyError(error)):
                    result = call()
                self.assertEqual(result, {"ok": False, "error": expected_error})

    # ---- 检索 -----------------------------------------------------------

    def test_search_unknown_library_returns_error_not_exception(self):
        """GUI 是零侵入观察者：Pipeline 抛出的任何异常（比如查询一个不存在
        的库）都必须在 Api 这一层被折叠成 {"ok": False}，绝不能让异常
        原样冒泡到 js_api 桥、把整个窗口炸掉（AGENTS.md 架构红线4的GUI
        层体现）。"""
        result = self.api.search_scoped("no-such-lib", "随便查点什么")
        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        contract = self.api.search("随便查点什么", 5, "no-such-lib", True)
        self.assertEqual(contract["results"], [])
        self.assertTrue(contract["error"])

    def test_search_empty_query_returns_empty_results_not_error(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        result = self.api.search_scoped("lib1", "   ")
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"], [])

    def test_search_marks_small_to_big_parent_backfill(self):
        parent = "\n\n".join(f"细节第{i}段。" + "工程内容" * 60 for i in range(8))
        (self.vault / "long.md").write_text(f"# 长父节\n\n{parent}", encoding="utf-8")
        self.api.register_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        result = self.api.search_scoped("lib1", "细节", top_k=3)
        self.assertTrue(result["ok"])
        self.assertIn("advice", result)
        self.assertTrue(result["results"])
        self.assertTrue(result["results"][0]["backfilled"])
        self.assertGreater(len(result["results"][0]["text"]), 300)

    # ---- 图谱 -----------------------------------------------------------

    def test_graph_serializes_read_model_and_uses_all_for_empty_scope(self):
        node = Mock(
            node_id="lib1|a.md",
            library_id="lib1",
            path="a.md",
            node_type="md",
            chunks=2,
            updated_ns=1_500_000_000,
            failure_reason=None,
            theme="general",
            extraction_state="done",
            visual_state="none",
            page_number=None,
            page_count=None,
            is_hub=True,
        )
        edge = Mock(source="lib1|a.md", target="lib1|b.md", kind="link")
        response = Mock(
            nodes=(node,),
            edges=(edge,),
            library_ids=("lib1",),
            stats=Mock(nodes=2, edges=1),
        )
        with patch.object(self.pipeline, "graph", return_value=response) as graph:
            result = self.api.graph("")
        graph.assert_called_once_with("all")
        self.assertEqual(result["nodes"][0]["id"], "lib1|a.md")
        self.assertEqual(result["nodes"][0]["lib"], "lib1")  # 库未注册时退回 id
        self.assertEqual(result["nodes"][0]["pipeline"], {"mineru": "done", "wemm": "none"})
        self.assertEqual(result["edges"], [{"a": "lib1|a.md", "b": "lib1|b.md", "kind": "link"}])
        self.assertEqual(result["stats"], {"nodes": 2, "edges": 1})
        self.assertEqual(result["libs"], ["lib1"])

    def test_graph_nodes_carry_the_display_name_not_the_library_id(self):
        self.api.register_library("lib1", "显示名", str(self.vault))
        self.pipeline.index_library("lib1")
        result = self.api.graph("")
        self.assertTrue(result["nodes"])
        self.assertEqual({n["lib"] for n in result["nodes"]}, {"显示名"})
        self.assertEqual(result["libs"], ["显示名"])

    def test_graph_semantic_edges_exposes_error_without_raising(self):
        with patch.object(
            self.pipeline,
            "graph_semantic_edges",
            return_value=Mock(edges=(), error="模型不可用"),
        ) as semantic_edges:
            result = self.api.graph_semantic_edges("lib1", 0.7)
        semantic_edges.assert_called_once_with("lib1", threshold=0.7)
        self.assertEqual(result, {"ok": True, "edges": [], "error": "模型不可用"})

    def test_graph_api_folds_pipeline_errors(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        with patch.object(self.pipeline, "graph", side_effect=ValueError("bad graph")):
            result = self.api.graph("lib1")
        self.assertEqual(result["error"], "bad graph")
        self.assertEqual((result["nodes"], result["edges"]), ([], []))
        unknown = self.api.graph("bad")
        self.assertIn("库不存在", unknown["error"])

    # ---- 打开 / 正文 / 关系 ------------------------------------------------

    def test_open_source_resolves_only_inside_library(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        with patch("os.startfile", create=True) as startfile:
            result = self.api.open_source("lib1", "notes.md")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["opened"])
        startfile.assert_called_once_with(str((self.vault / "notes.md").resolve()))
        outside = self.api.open_source("lib1", "../outside.txt")
        self.assertFalse(outside["ok"])
        self.assertIn("越出", outside["error"])

    def test_open_source_uses_the_obsidian_uri_for_a_vault(self):
        """含 `.obsidian` 的目录是 Obsidian vault，走 obsidian:// URI（旧 open_source）。"""
        (self.vault / ".obsidian").mkdir()
        (self.vault / "中文 笔记.md").write_text("# 甲\n\n乙", encoding="utf-8")
        self.api.register_library("lib1", "测试库", str(self.vault))
        with patch("os.startfile", create=True) as startfile:
            result = self.api.open_source("lib1", "中文 笔记.md")
        self.assertTrue(result["ok"], result)
        url = startfile.call_args[0][0]
        self.assertTrue(url.startswith("obsidian://open?vault="), url)
        self.assertIn("file=%E4%B8%AD%E6%96%87%20%E7%AC%94%E8%AE%B0.md", url)

    def test_index_failures_read_document_and_relations_delegate(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        failures = {"succeeded": 1, "failures": [{"path": "bad.md", "reason": "提取失败"}]}
        document = Mock(path="notes.md", text="正文", source="源文件直读")
        relations = {"resolved": True, "file": "notes.md", "outlinks": ["a.md"], "inlinks": []}
        with patch.object(self.pipeline, "index_failures", return_value=failures) as index_failures:
            self.assertEqual(
                self.api.index_failures("lib1"),
                {"ok": True, "failures": failures["failures"]},
            )
            index_failures.assert_called_once_with("lib1")
        with patch.object(self.pipeline, "read_document", return_value=document):
            self.assertEqual(
                self.api.document_text("lib1", "notes.md"),
                {"ok": True, "path": "notes.md", "text": "正文", "source": "源文件直读"},
            )
            contract = self.api.read_document("lib1", "notes.md")
            self.assertEqual(
                (contract["ok"], contract["markdown"], contract["chars"], contract["route"], contract["error"]),
                (True, "正文", 2, "源文件", None),
            )
            self.assertFalse(contract["truncated"])
        with patch.object(self.pipeline, "note_relations", return_value=relations):
            self.assertEqual(self.api.note_relations("lib1", "notes.md"), relations)

    def test_read_document_truncates_very_long_text_and_explains_unindexed_files(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        long_text = "字" * 250_000
        (self.vault / "long.md").write_text(long_text, encoding="utf-8")
        result = self.api.read_document("lib1", "long.md")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["chars"], 200_000)
        # pdf/docx 没索引过：零触发只读，不后台提取，给出可执行的提示
        (self.vault / "x.docx").write_bytes(b"not a real docx")
        self.pipeline.index_library("lib1")
        with patch.object(self.pipeline, "read_document",
                          side_effect=ValueError("「x.docx」还没有被成功索引过，先调用 index_library 建好索引再重试")):
            missing = self.api.read_document("lib1", "x.docx")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"], "该文件尚未被索引/提取，请先增量重建后再查看")

    # ---- 导出 / 导入 ------------------------------------------------------

    def test_export_then_import_round_trip_preserves_search_results(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        before = self.api.search_scoped("lib1", "插件 架构")

        dest = str(self.tmp / "lib1.ragexport.zip")
        export_result = self.api.export_library("lib1", dest)
        self.assertTrue(export_result["ok"], export_result)
        self.assertTrue(Path(dest).exists())

        import_result = self.api.import_library(dest, "/new/machine/vault", "lib1-restored")
        self.assertTrue(import_result["ok"], import_result)
        self.assertEqual(import_result["library_id"], "lib1-restored")

        after = self.api.search_scoped("lib1-restored", "插件 架构")
        self.assertEqual(
            [r["path"] for r in before["results"]],
            [r["path"] for r in after["results"]],
        )

    def test_export_unknown_library_returns_error_not_exception(self):
        result = self.api.export_library("no-such-lib", str(self.tmp / "out.zip"))
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_import_missing_file_returns_error_not_exception(self):
        result = self.api.import_library(str(self.tmp / "does-not-exist.zip"), "/some/path")
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_import_run_requires_the_exact_confirmation_text(self):
        self.assertEqual(
            self.api.import_run("确认"),
            {"ok": False, "error": "确认文本不匹配，未执行导入"},
        )

    # ---- 库简介 -----------------------------------------------------------

    def test_get_library_summary_blank_by_default(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        result = self.api.get_library_summary("lib1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["source"], "none")

    def test_set_library_summary_writes_directly_without_gate(self):
        """用户在 GUI 手写简介：无条件生效，不需要写权限门禁确认——
        即使覆盖的是用户自己之前写的内容也一样（用户改自己的东西不需要
        向自己确认）。"""
        self.api.register_library("lib1", "测试库", str(self.vault))
        self.api.set_library_summary("lib1", "第一版手写简介")
        result = self.api.set_library_summary("lib1", "第二版手写简介")
        self.assertTrue(result["ok"])
        summary = self.api.get_library_summary("lib1")
        self.assertEqual(summary["text"], "第二版手写简介")
        self.assertEqual(summary["source"], "user")
        # 库列表里的来源标注如实反映（此前只要有文字就写死成 "ai"）
        row = self.api.list_libraries()[0]
        self.assertEqual((row["summary"]["text"], row["summary"]["source"]), ("第二版手写简介", "user"))

    def test_refresh_library_summary_calls_llm_and_writes_directly(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        result = self.api.refresh_library_summary("lib1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["text"], self.fake_llm.response)
        self.assertEqual(result["provider"], "official-llm-openai-compatible")
        summary = self.api.get_library_summary("lib1")
        self.assertEqual(summary["source"], "ai")

    def test_refresh_library_summary_needs_confirm_when_user_authored_and_not_forced(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        self.api.set_library_summary("lib1", "用户手写的简介")
        result = self.api.refresh_library_summary("lib1")
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("needs_confirm"))
        # 没有真的生成/覆盖——用户手写内容原封不动
        self.assertEqual(self.api.get_library_summary("lib1")["text"], "用户手写的简介")

    def test_refresh_library_summary_force_overwrites_user_authored(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        self.api.set_library_summary("lib1", "用户手写的简介")
        result = self.api.refresh_library_summary("lib1", force=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.api.get_library_summary("lib1")["text"], self.fake_llm.response)

    def test_refresh_library_summary_on_unindexed_library_returns_error(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        result = self.api.refresh_library_summary("lib1")
        self.assertFalse(result["ok"])

    def test_batch_summary_refresh_background_and_poll(self):
        """对齐旧 guiweb/bridge.py::refresh_library_summaries_batch+poll：后台线程逐库生成，
        轮询读进度与结果，手写库不传 force 时标 needs_confirm 而不阻塞批次；结果按**显示名**
        归位（前端按名字找库卡片）。"""
        # lib2 必须用自己的目录：库注册表按 resolve 后的根路径拒绝重复注册
        # （对齐 obsidian-rag/library.py:497-498，同一目录只能注册成一个库）。
        vault2 = self.env.make_vault("vault2", {"notes2.md": "# 厨房笔记\n\n食谱记录。"})
        self.api.register_library("lib1", "测试库", str(self.vault))
        self.api.register_library("lib2", "第二库", str(vault2))
        self.pipeline.index_library("lib2")
        self.pipeline.set_library_summary_direct("lib1", "手写简介", source="user")
        started = self.api.refresh_library_summaries_batch(["lib1", "第二库"])
        self.assertEqual(started, {"ok": True, "total": 2})
        self.assertFalse(self.api.refresh_library_summaries_batch(["lib2"])["ok"], "已有任务在跑时应拒绝")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            poll = self.api.refresh_library_summaries_poll()
            if not poll["running"]:
                break
            time.sleep(0.05)
        self.assertFalse(poll["running"])
        self.assertEqual(poll["done"], poll["total"])
        self.assertIsNone(poll["current"])
        self.assertEqual(poll["results"]["测试库"], {"ok": False, "needs_confirm": True})
        # lib2 生成失败的详细原因（假 LLM 环境下的可诊断性）
        self.assertTrue(
            poll["results"]["第二库"]["ok"],
            poll["results"].get("第二库", {}).get("error", "no error field"),
        )

    def test_batch_summary_refresh_with_no_matching_library_is_an_error(self):
        result = self.api.refresh_library_summaries_batch(["不存在"])
        self.assertFalse(result["ok"])
        self.assertIn("没有可刷新的库", result["error"])

    # ---- 设置 -------------------------------------------------------------

    def test_get_settings_values_starts_empty(self):
        result = self.api.get_settings_values()
        self.assertEqual(result["values"], {})
        self.assertIn("fusion_dense_weight", result["meta"])

    def test_set_setting_then_get_settings_values_round_trips(self):
        result = self.api.set_setting("fusion_dense_weight", 2.0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.get_settings_values()["values"], {"fusion_dense_weight": 2.0})

    def test_set_setting_takes_effect_immediately_without_restart(self):
        """对齐架构红线8"切换实现是配置层面操作，不需要重启"——写了设置
        之后不重建 Api/Pipeline，直接再查一次就该看到新值。"""
        self.api.set_setting("default_libraries", ["lib1"])
        self.assertEqual(self.api._pipeline.runtime.settings.get("default_libraries", []), ["lib1"])

    def test_unset_setting_restores_default_on_next_read(self):
        self.api.set_setting("k", "v")
        result = self.api.unset_setting("k")
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.get_settings_values()["values"], {})
        self.assertEqual(self.api._pipeline.runtime.settings.get("k", "default"), "default")

    def test_unset_setting_missing_key_is_ok_not_error(self):
        result = self.api.unset_setting("never-set")
        self.assertTrue(result["ok"])

    def test_settings_meta_marks_api_keys_secret(self):
        """对齐 obsidian-rag/gui/config_editor.py:168/278（secret 标志）与
        guiweb/bridge.py:813（元信息随值一起返回）：API key 类设置必须带
        secret=True 让前端按密码框渲染，且规则兜底覆盖未登记的 key 类新键。"""
        self.api.set_setting("hyde_llm_api_key", "sk-something")
        self.api.set_setting("custom_provider_token", "tok-something")
        self.api.set_setting("fusion_dense_weight", 1.5)
        meta = self.api.get_settings_values()["meta"]
        self.assertTrue(meta["hyde_llm_api_key"]["secret"])
        self.assertTrue(meta["custom_provider_token"]["secret"], "未登记的 *_token 键按规则兜底")
        self.assertFalse(meta["fusion_dense_weight"]["secret"])
        self.assertTrue(meta["hyde_llm_api_key"]["label"])
        self.assertTrue(meta["fusion_dense_weight"]["hint"], "已知键必须带中文说明")

    # ---- 库管理 -----------------------------------------------------------

    def test_library_management_methods(self):
        """库管理契约方法：get/set_library_config、dedup、wemm_status、open_path、remove_library。"""
        self.api.register_library("lib1", "测试库", str(self.vault))
        cfg = self.api.get_library_config("lib1")
        self.assertEqual(sorted(cfg), ["all_keys", "effective", "overrides"])
        self.assertEqual(cfg["effective"]["extensions"], ["md", "pdf", "docx"])
        self.assertEqual(cfg["overrides"], {}, "全默认的库没有任何覆盖项")
        # 逐键校验：非法键报错，合法键照常生效（旧 guiweb 桥逐键各自写）
        result = self.api.set_library_config("lib1", {"exclude_dirs": ".trash", "bad_key": 1})
        self.assertFalse(result["ok"], "非法键必须报错")
        self.assertIn("bad_key", result["errors"])
        self.assertEqual(self.api._lib_mgr.store.get("lib1").exclude_dirs, [".trash"])
        self.assertEqual(self.api.get_library_config("lib1")["overrides"], {"exclude_dirs": [".trash"]})
        # 空字符串 = 恢复默认
        self.assertEqual(self.api.set_library_config("lib1", {"exclude_dirs": ""}), {"ok": True, "errors": {}})
        self.assertEqual(self.api.get_library_config("lib1")["overrides"], {})
        self.api._pipeline.index_library("lib1")
        dedup = self.api.dedup_run_library("lib1")
        self.assertTrue(dedup["ok"])
        contract_dedup = self.api.dedup_run(0.8)
        self.assertEqual(sorted(contract_dedup["stats"]), ["clusters", "files", "seconds"])
        wemm = self.api.wemm_status("lib1")
        self.assertEqual((wemm["exists"], wemm["total_pages"], wemm["rows"]), (False, 0, []))
        with patch("os.startfile", create=True) as startfile:
            opened = self.api.open_path(str(self.vault))
        self.assertTrue(opened["ok"])
        startfile.assert_called_once_with(str(self.vault))
        missing = self.api.open_path(str(self.tmp / "no-such-dir"))
        self.assertFalse(missing["ok"])
        removed = self.api.remove_library("lib1")
        self.assertTrue(removed["ok"])
        self.assertIsNone(self.api._lib_mgr.store.get("lib1"))

    def test_library_config_agent_formats_and_extensions_follow_the_legacy_rules(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        # 关掉 pdf 后再授权 pdf → 旧规则："以下格式未在该库 extensions 中启用，无法授权"
        self.assertEqual(self.api.set_library_config("lib1", {"extensions": "md,docx"}), {"ok": True, "errors": {}})
        denied = self.api.set_library_config("lib1", {"agent_formats": "pdf"})
        self.assertFalse(denied["ok"])
        self.assertIn("agent_formats", denied["errors"])
        self.assertEqual(self.api.set_library_config("lib1", {"agent_formats": "docx"}), {"ok": True, "errors": {}})
        cfg = self.api.get_library_config("lib1")
        self.assertEqual(cfg["effective"]["agent_formats"], ["docx"])
        self.assertEqual(cfg["overrides"]["extensions"], ["md", "docx"])
        # 不支持的扩展名 / 空扩展名
        self.assertIn("extensions", self.api.set_library_config("lib1", {"extensions": "md,exe"})["errors"])
        # 前端保存时会把切块粒度/collection 也带回；rag-redo 没有按库覆盖，空值无害、非空值明确报错
        harmless = self.api.set_library_config("lib1", {"chunk_char_limit": "", "collection": ""})
        self.assertEqual(harmless, {"ok": True, "errors": {}})
        refused = self.api.set_library_config("lib1", {"chunk_char_limit": "800"})
        self.assertIn("chunk_char_limit", refused["errors"])
        self.assertIn("暂不支持", refused["errors"]["chunk_char_limit"])
        # 撤销授权 = 清空；unset 把整项恢复默认
        self.assertEqual(self.api.set_library_config("lib1", {"agent_formats": ""}), {"ok": True, "errors": {}})
        self.assertEqual(self.api.unset_library_config("lib1", ["extensions"]), {"ok": True})
        self.assertEqual(self.api.get_library_config("lib1")["overrides"], {})

    def test_remove_library_with_drop_clears_index_data(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        self.assertTrue(self.pipeline.has_index("lib1"))
        result = self.api.remove_library("lib1", True)
        self.assertTrue(result["ok"], result)
        self.assertIn("索引数据已清理", result["note"])
        self.assertIsNone(self.api._lib_mgr.store.get("lib1"))
        self.assertTrue((self.vault / "notes.md").exists(), "笔记文件永远保留")

    # ---- GPU（精确 API：新能力，旧项目没有对应按钮，见 BC-16）--------------------------

    def test_release_gpu_memory_delegates_to_pipeline(self):
        expected = {"released": ["official-embedder-bge-m3"], "skipped": [], "errors": {}}
        with patch.object(self.pipeline, "release_gpu_memory", return_value=expected) as mocked:
            result = self.api.release_gpu_memory()
        mocked.assert_called_once_with()
        self.assertEqual(result, expected)

    def test_release_gpu_memory_folds_pipeline_exception_instead_of_crashing(self):
        """见模块 docstring：绝不让一次操作失败带崩整个窗口——就算编排层
        本身抛异常，前端也该拿到一个可展示的结构，不是一个未捕获异常。"""
        with patch.object(self.pipeline, "release_gpu_memory", side_effect=RuntimeError("boom")):
            result = self.api.release_gpu_memory()
        self.assertEqual(result["released"], [])
        self.assertIn("boom", result["errors"]["_pipeline"])

    # ---- 总览星图（精确 API：新能力，旧项目没有，见 BC-18）-----------------------------

    def test_overview_map_returns_column_arrays_keyed_by_display_name(self):
        self.api.register_library("lib1", "测试库", str(self.vault))
        (self.vault / "other.md").write_text("# 另一篇\n\n和插件无关的内容。", encoding="utf-8")
        self.pipeline.index_library("lib1")
        result = self.api.overview_map("")
        self.assertIsNone(result["error"])
        self.assertEqual([lib["lib"] for lib in result["libs"]], ["测试库"], "前端拿显示名去打开文件")
        lib = result["libs"][0]
        columns = ("rel", "type", "chunks", "pages", "state", "group", "gap", "loose", "updated", "fail")
        self.assertEqual({len(lib[c]) for c in columns}, {2}, "每一列长度一致，按下标对齐")
        self.assertEqual(set(lib["rel"]), {"notes.md", "other.md"})
        self.assertTrue(all(g >= 0 for g in lib["group"]))
        self.assertEqual(lib["points"], sum(1 + c + p for c, p in zip(lib["chunks"], lib["pages"])))
        self.assertEqual(result["stats"], {"libs": 1, "files": 2, "points": lib["points"]})
        self.assertTrue(all(s["lib"] == "测试库" for g in result["groups"] for s in g["samples"]))
        json.dumps(result)  # pywebview 用 json.dumps 序列化返回值

    def test_overview_map_folds_pipeline_exception_and_unknown_scope(self):
        with patch.object(self.pipeline, "overview_map", side_effect=RuntimeError("boom")):
            broken = self.api.overview_map("")
        self.assertEqual(broken["libs"], [])
        self.assertIn("RuntimeError", broken["error"])
        unknown = self.api.overview_map("不存在的库")
        self.assertEqual(unknown["libs"], [])
        self.assertTrue(unknown["error"])


    # ---- 转换缓存看得见（BC-19）---------------------------------------------

    def _conversion_fixture(self):
        """一个库：一份转好的 Word、一份缺正文的 PDF（页库开着但没建全）。缓存文件是真文件。"""
        from core.contracts import ConversionCacheFile, ConversionCacheLibrary

        self.api.register_library("lib1", "测试库", str(self.vault))
        text_dir = self.tmp / "extracted" / "lib1"
        (text_dir / "g1").mkdir(parents=True)
        cached = text_dir / "g1" / "abc.official-extractor-docx%3A1.0.0.txt"
        cached.write_text("转好的正文" * 3, encoding="utf-8")
        report = ConversionCacheLibrary(
            library_id="lib1",
            name="测试库",
            files=(
                ConversionCacheFile(
                    path="a.docx", extension="docx", text_state="done", text_route="official-extractor-docx",
                    text_route_version="1.0.0", text_route_name="Word 提取", text_file=str(cached),
                    text_bytes=cached.stat().st_size, text_updated=1_790_000_000.0,
                ),
                ConversionCacheFile(
                    path="scan.pdf", extension="pdf", text_state="pending", text_reason="scanned",
                    pages_state="partial", pages_reason="pages-partial", pages=(1, 2, 4), page_count=5,
                    pages_detail="部分页面编码失败",
                ),
            ),
            text_dir=str(text_dir),
            catalog_file=str(text_dir / "缓存目录.md"),
            pages_enabled=True,
            pages_dir=str(self.tmp / "visual"),
            page_bytes_estimate=3 * 4608,
            page_vram_gb=6.3,
            page_idle_unload_seconds=300,
        )
        return report, cached

    def test_conversion_caches_summarise_libraries_and_list_one_library_file_by_file(self):
        report, _cached = self._conversion_fixture()
        with patch.object(self.pipeline, "conversion_caches", return_value=(report,)) as reader:
            summary = self.api.conversion_caches("")
            detail = self.api.conversion_caches("测试库")
        self.assertEqual(reader.call_args_list[0].args, ("all",))
        self.assertEqual(reader.call_args_list[1].args, ("lib1",))
        self.assertIsNone(summary["error"])
        self.assertEqual(summary["rows"], [], "不指定库时只给汇总，库卡片用")
        lib = summary["libs"][0]
        self.assertEqual(lib["lib"], "测试库")
        self.assertEqual((lib["text_done"], lib["text_total"], lib["text_attention"]), (1, 2, 1))
        self.assertEqual((lib["pdf_total"], lib["pages_done"], lib["pages_attention"], lib["page_vectors"]), (1, 0, 1, 3))
        self.assertEqual((lib["vram_gb"], lib["idle_unload_s"]), (6.3, 300))
        rows = {row["rel"]: row for row in detail["rows"]}
        self.assertFalse(rows["a.docx"]["attention"])
        self.assertEqual(rows["a.docx"]["text"]["route_name"], "Word 提取")
        self.assertEqual(rows["a.docx"]["pages"]["state"], "n/a")
        scan = rows["scan.pdf"]
        self.assertTrue(scan["attention"])
        self.assertEqual(scan["text"]["label"], "扫描件，等文字识别")
        self.assertTrue(scan["text"]["next"], "缺了要写下一步怎么办")
        self.assertEqual(
            (scan["pages"]["have"], scan["pages"]["missing"], scan["pages"]["count"], scan["pages"]["total"]),
            ("1–2、4", "3、5", 3, 5),
        )
        self.assertEqual(scan["pages"]["label"], "有页面没编上")
        json.dumps(detail)  # pywebview 用 json.dumps 序列化返回值

    def test_conversion_caches_are_reused_briefly_and_refreshed_when_indexing_ends(self):
        report, _cached = self._conversion_fixture()
        with patch.object(self.pipeline, "conversion_caches", return_value=(report,)) as reader:
            self.api.conversion_caches("")
            self.api.conversion_caches("")
            self.assertEqual(reader.call_count, 1, "库卡片每次重画不该都把整个库枚举一遍")
            self.api._invalidate_cache()  # noqa: SLF001 - 索引结束时桥接层就是这样作废的
            self.api.conversion_caches("")
            self.assertEqual(reader.call_count, 2)

    def test_conversion_cache_file_counts_characters_and_folds_unknown_files(self):
        report, cached = self._conversion_fixture()
        with patch.object(self.pipeline, "conversion_caches", return_value=(report,)):
            got = self.api.conversion_cache_file("测试库", "a.docx")
            missing = self.api.conversion_cache_file("测试库", "不在清单里.pdf")
        self.assertTrue(got["ok"])
        self.assertEqual(got["text"]["chars"], len(cached.read_text(encoding="utf-8")))
        self.assertEqual((got["vram_gb"], got["idle_unload_s"]), (6.3, 300))
        self.assertFalse(missing["ok"])
        self.assertIn("不是这个库里需要转换的文件", missing["error"])

    def test_reveal_cache_file_only_selects_the_file_the_list_reported(self):
        import official_gui_shell.api as api_module

        report, cached = self._conversion_fixture()
        with (
            patch.object(self.pipeline, "conversion_caches", return_value=(report,)),
            patch.object(api_module.subprocess, "Popen") as popen,
        ):
            shown = self.api.reveal_cache_file("测试库", "a.docx")
            not_converted = self.api.reveal_cache_file("测试库", "scan.pdf")
        self.assertTrue(shown["ok"])
        if api_module.os.name == "nt":
            popen.assert_called_once_with(["explorer", "/select,", str(cached.resolve())])
        else:
            popen.assert_not_called()
            self.assertFalse(shown["opened"])
        self.assertFalse(not_converted["ok"])
        self.assertEqual(not_converted["error"], "扫描件，等文字识别")

    def test_open_cache_folder_refreshes_the_catalog_before_opening(self):
        import official_gui_shell.api as api_module

        self.api.register_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        with patch.object(api_module.os, "startfile", create=True) as startfile:
            result = self.api.open_cache_folder("测试库")
        self.assertTrue(result["ok"], result)
        catalog = Path(result["catalog"])
        self.assertTrue(catalog.is_file())
        self.assertEqual(catalog.parent, Path(result["path"]))
        startfile.assert_called_once_with(result["path"])
        self.assertIn("测试库", catalog.read_text(encoding="utf-8"))
        self.assertFalse(self.api.open_cache_folder("不存在的库")["ok"])

    def test_page_preview_and_try_search_fold_every_failure_into_a_message(self):
        from core.contracts import PageHit

        self.api.register_library("lib1", "测试库", str(self.vault))
        not_pdf = self.api.page_preview("测试库", "notes.md", 1)
        self.assertFalse(not_pdf["ok"])
        self.assertIn("PDF", not_pdf["error"])
        with patch.object(self.pipeline, "page_preview", return_value=b"x") as preview:
            ok = self.api.page_preview("测试库", "a.pdf", "3")
        preview.assert_called_once_with("lib1", "a.pdf", 3)
        self.assertEqual(ok, {"ok": True, "page": 3, "data_url": "data:image/png;base64,eA=="})
        self.assertEqual(self.api.page_try_search("测试库", "a.pdf", "  ")["error"], "先输入一句要找的内容")
        hits = [
            PageHit(library_id="lib1", path="a.pdf", abs_path="/x/a.pdf", page_index=4, score=0.9),
            PageHit(library_id="lib1", path="a.pdf", abs_path="/x/a.pdf", page_index=0, score=0.5),
        ]
        with patch.object(self.pipeline, "navigate", return_value=hits) as navigate:
            found = self.api.page_try_search("测试库", "a.pdf", "注意力机制", top_k=99)
        navigate.assert_called_once_with("lib1", "注意力机制", top_k=20, path="a.pdf")
        self.assertEqual(found["hits"], [{"page": 5, "rank": 1}, {"page": 1, "rank": 2}])
        with patch.object(self.pipeline, "navigate", side_effect=RuntimeError("（WEMM 视觉导航未开启）")):
            vetoed = self.api.page_try_search("测试库", "a.pdf", "注意力机制")
        self.assertEqual((vetoed["ok"], vetoed["hits"]), (False, []))
        self.assertIn("未开启", vetoed["error"])

if __name__ == "__main__":
    unittest.main()
