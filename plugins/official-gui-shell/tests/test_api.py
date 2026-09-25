"""Api 类是纯 Python，不需要真的渲染一个窗口就能测——这里覆盖的是
"GUI后端调用Pipeline/library-manager对不对、失败会不会被折叠成
{"ok": False, ...}而不是让调用方（js_api桥）异常"，渲染层的验证见
scripts/smoke_gui.py（手动冒烟，不进自动化回归，风格同旧项目
tests/smoke_gui.py）。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
for other_plugin_dir in (_REPO_ROOT / "plugins").glob("*"):
    if other_plugin_dir.is_dir() and str(other_plugin_dir) not in sys.path:
        sys.path.insert(0, str(other_plugin_dir))

from official_gui_shell.api import Api  # noqa: E402

from core.pipeline import Pipeline  # noqa: E402
from core.runtime import PluginRuntime  # noqa: E402

REQUIRED_PLUGINS = [
    "official-extractor-text",
    "official-chunker",
    "official-library-manager",
    "official-lexical-bm25",
    "official-embedder-bge-m3",
    "official-vector-store-chroma",
    "official-fusion-rrf",
    "official-reranker",
    "official-import-export",
    "official-library-summary",
    "official-llm-openai-compatible",
    "official-result-advisor",
]


class _FakeEncoder:
    KEYWORDS = ["插件", "架构"]

    def encode(self, texts):
        return [[float(t.count(k)) for k in self.KEYWORDS] for t in texts]


class _FakeReranker:
    def score(self, query, texts):
        # query.split()，不是逐字符遍历——见 tests/test_demo_vault.py 的
        # 同类注释，逐字符统计在更长的真实文本上容易被噪声干扰。
        terms = query.split()
        return [sum(text.count(term) for term in terms) for text in texts]


class _FakeLlmClient:
    def __init__(self, response: str = "这是一个关于插件架构的知识库。") -> None:
        self.response = response
        self.calls: list[dict] = []

    def complete(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user})
        return self.response


class TestApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "notes.md").write_text("# 插件架构\n\n插件系统笔记。", encoding="utf-8")

        self.runtime = PluginRuntime(
            _REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.runtime.scan()
        for plugin_id in REQUIRED_PLUGINS:
            self.runtime.load(plugin_id)
            self.runtime.enable(plugin_id)

        from official_embedder_bge_m3.embed import BGEM3Embedder

        self.runtime.plugins["official-embedder-bge-m3"].instance.embedder = BGEM3Embedder(
            encoder=_FakeEncoder()
        )
        from official_reranker.rerank import RerankerEngine

        self.runtime.plugins["official-reranker"].instance.engine = RerankerEngine(reranker=_FakeReranker())

        from official_llm_openai_compatible.llm import OpenAiCompatibleClient

        self.fake_llm = _FakeLlmClient()
        self.runtime.plugins["official-llm-openai-compatible"].instance._client = OpenAiCompatibleClient(  # noqa: SLF001
            http_client=self.fake_llm
        )

        lib_mgr = self.runtime.plugins["official-library-manager"].instance
        self.pipeline = Pipeline(self.runtime)
        self.api = Api(self.pipeline, lib_mgr)

    def test_list_libraries_empty_initially(self):
        self.assertEqual(self.api.list_libraries(), [])

    def test_add_library_then_list(self):
        result = self.api.add_library("lib1", "测试库", str(self.vault))
        self.assertTrue(result["ok"])
        libs = self.api.list_libraries()
        self.assertEqual(len(libs), 1)
        self.assertEqual(libs[0]["library_id"], "lib1")

    def test_add_duplicate_library_returns_error_not_exception(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        result = self.api.add_library("lib1", "重复", str(self.vault))
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_index_and_search_round_trip(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        report = self.pipeline.index_library("lib1")
        self.assertEqual(report.succeeded, 1)

        search_result = self.api.search("lib1", "插件 架构")
        self.assertTrue(search_result["ok"])
        self.assertGreater(len(search_result["results"]), 0)
        self.assertEqual(search_result["results"][0]["path"], "notes.md")
        self.assertIn("confidence_tier", search_result["results"][0])

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

    def test_stop_index_returns_pipeline_result(self):
        for stopped, message in (
            (True, "索引任务已取消"),
            (False, "拒绝停止：索引任务 run_id 不匹配"),
        ):
            with self.subTest(stopped=stopped):
                with patch.object(
                    self.pipeline, "stop_index_library", return_value=(stopped, message)
                ) as stop_index:
                    result = self.api.stop_index("lib1", "run-1")
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
            ("stop_index_library", lambda: self.api.stop_index("no-such-lib", "run-1")),
        )
        for method_name, call in calls:
            with self.subTest(method_name=method_name):
                with patch.object(self.pipeline, method_name, side_effect=KeyError(error)):
                    result = call()
                self.assertEqual(result, {"ok": False, "error": expected_error})

    def test_search_unknown_library_returns_error_not_exception(self):
        """GUI 是零侵入观察者：Pipeline 抛出的任何异常（比如查询一个不存在
        的库）都必须在 Api 这一层被折叠成 {"ok": False}，绝不能让异常
        原样冒泡到 js_api 桥、把整个窗口炸掉（AGENTS.md 架构红线4的GUI
        层体现）。"""
        result = self.api.search("no-such-lib", "随便查点什么")
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_search_empty_query_returns_empty_results_not_error(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        result = self.api.search("lib1", "   ")
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"], [])

    def test_search_marks_small_to_big_parent_backfill(self):
        parent = "\n\n".join(f"细节第{i}段。" + "工程内容" * 60 for i in range(8))
        (self.vault / "long.md").write_text(f"# 长父节\n\n{parent}", encoding="utf-8")
        self.api.add_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        result = self.api.search("lib1", "细节", top_k=3)
        self.assertTrue(result["ok"])
        self.assertIn("advice", result)
        self.assertTrue(result["results"])
        self.assertTrue(result["results"][0]["backfilled"])
        self.assertGreater(len(result["results"][0]["text"]), 300)

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
        self.assertEqual(result["nodes"][0]["pipeline"], {"mineru": "done", "wemm": "none"})
        self.assertEqual(result["edges"], [{"a": "lib1|a.md", "b": "lib1|b.md", "kind": "link"}])
        self.assertEqual(result["stats"], {"nodes": 2, "edges": 1})

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
        with patch.object(self.pipeline, "graph", side_effect=ValueError("bad graph")):
            self.assertEqual(self.api.graph("bad"), {"ok": False, "error": "bad graph"})

    def test_open_source_resolves_only_inside_library(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        with patch("os.startfile") as startfile:
            result = self.api.open_source("lib1", "notes.md")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["opened"])
        startfile.assert_called_once_with(str(self.vault / "notes.md"))
        outside = self.api.open_source("lib1", "../outside.txt")
        self.assertFalse(outside["ok"])
        self.assertIn("越出", outside["error"])

    def test_index_failures_read_document_and_relations_delegate(self):
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
                self.api.read_document("lib1", "notes.md"),
                {"ok": True, "path": "notes.md", "text": "正文", "source": "源文件直读"},
            )
        with patch.object(self.pipeline, "note_relations", return_value=relations):
            self.assertEqual(self.api.note_relations("lib1", "notes.md"), {"ok": True, **relations})

        self.api.add_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        before = self.api.search("lib1", "插件 架构")

        dest = str(self.tmp / "lib1.ragexport.zip")
        export_result = self.api.export_library("lib1", dest)
        self.assertTrue(export_result["ok"], export_result)
        self.assertTrue(Path(dest).exists())

        import_result = self.api.import_library(dest, "/new/machine/vault", "lib1-restored")
        self.assertTrue(import_result["ok"], import_result)
        self.assertEqual(import_result["library_id"], "lib1-restored")

        after = self.api.search("lib1-restored", "插件 架构")
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

    def test_get_library_summary_blank_by_default(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        result = self.api.get_library_summary("lib1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["source"], "none")

    def test_set_library_summary_writes_directly_without_gate(self):
        """用户在 GUI 手写简介：无条件生效，不需要写权限门禁确认——
        即使覆盖的是用户自己之前写的内容也一样（用户改自己的东西不需要
        向自己确认）。"""
        self.api.add_library("lib1", "测试库", str(self.vault))
        self.api.set_library_summary("lib1", "第一版手写简介")
        result = self.api.set_library_summary("lib1", "第二版手写简介")
        self.assertTrue(result["ok"])
        summary = self.api.get_library_summary("lib1")
        self.assertEqual(summary["text"], "第二版手写简介")
        self.assertEqual(summary["source"], "user")

    def test_refresh_library_summary_calls_llm_and_writes_directly(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        result = self.api.refresh_library_summary("lib1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["text"], self.fake_llm.response)
        self.assertEqual(result["provider"], "official-llm-openai-compatible")
        summary = self.api.get_library_summary("lib1")
        self.assertEqual(summary["source"], "ai")

    def test_refresh_library_summary_needs_confirm_when_user_authored_and_not_forced(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        self.api.set_library_summary("lib1", "用户手写的简介")
        result = self.api.refresh_library_summary("lib1")
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("needs_confirm"))
        # 没有真的生成/覆盖——用户手写内容原封不动
        self.assertEqual(self.api.get_library_summary("lib1")["text"], "用户手写的简介")

    def test_refresh_library_summary_force_overwrites_user_authored(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        self.pipeline.index_library("lib1")
        self.api.set_library_summary("lib1", "用户手写的简介")
        result = self.api.refresh_library_summary("lib1", force=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.api.get_library_summary("lib1")["text"], self.fake_llm.response)

    def test_refresh_library_summary_on_unindexed_library_returns_error(self):
        self.api.add_library("lib1", "测试库", str(self.vault))
        result = self.api.refresh_library_summary("lib1")
        self.assertFalse(result["ok"])

    def test_get_settings_starts_empty(self):
        result = self.api.get_settings()
        self.assertEqual(result["values"], {})
        self.assertIn("fusion_dense_weight", result["meta"])

    def test_set_setting_then_get_settings_round_trips(self):
        result = self.api.set_setting("fusion_dense_weight", 2.0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.get_settings()["values"], {"fusion_dense_weight": 2.0})

    def test_set_setting_takes_effect_immediately_without_restart(self):
        """对齐架构红线8"切换实现是配置层面操作，不需要重启"——写了设置
        之后不重建 Api/Pipeline，直接再查一次就该看到新值。"""
        self.api.set_setting("default_libraries", ["lib1"])
        self.assertEqual(self.api._pipeline.runtime.settings.get("default_libraries", []), ["lib1"])

    def test_unset_setting_restores_default_on_next_read(self):
        self.api.set_setting("k", "v")
        result = self.api.unset_setting("k")
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.get_settings()["values"], {})
        self.assertEqual(self.api._pipeline.runtime.settings.get("k", "default"), "default")

    def test_unset_setting_missing_key_is_ok_not_error(self):
        result = self.api.unset_setting("never-set")
        self.assertTrue(result["ok"])

    def test_batch_summary_refresh_background_and_poll(self):
        """对齐旧 guiweb/bridge.py::refresh_library_summaries_batch+poll：
        后台线程逐库生成，轮询读进度与结果，手写库跳过不阻塞批次。"""
        self.api.add_library("lib1", "测试库", str(self.vault))
        self.api.add_library("lib2", "第二库", str(self.vault))
        self.api._pipeline.index_library("lib2")
        self.api._pipeline.set_library_summary_direct("lib1", "手写简介", source="user")
        result = self.api.refresh_library_summaries_batch(["lib1", "lib2"])
        self.assertTrue(result["ok"])
        deadline = __import__("time").monotonic() + 30
        while __import__("time").monotonic() < deadline:
            poll = self.api.refresh_library_summaries_poll()
            if not poll["running"]:
                break
            __import__("time").sleep(0.05)
        self.assertFalse(poll["running"])
        self.assertEqual(poll["done"], poll["total"])
        self.assertIn("lib1", poll["skipped"], "手写简介在 force=False 时跳过")
        self.assertIn("lib2", poll["results"])
        # lib2 生成失败的详细原因（假 LLM 环境下的可诊断性）
        self.assertTrue(
            poll["results"]["lib2"]["ok"],
            poll["results"].get("lib2", {}).get("error", "no error field"),
        )

    def test_settings_meta_marks_api_keys_secret(self):
        """对齐 obsidian-rag/gui/config_editor.py:168/278（secret 标志）与
        guiweb/bridge.py:813（元信息随值一起返回）：API key 类设置必须带
        secret=True 让前端按密码框渲染，且规则兜底覆盖未登记的 key 类新键。"""
        self.api.set_setting("hyde_llm_api_key", "sk-something")
        self.api.set_setting("custom_provider_token", "tok-something")
        self.api.set_setting("fusion_dense_weight", 1.5)
        meta = self.api.get_settings()["meta"]
        self.assertTrue(meta["hyde_llm_api_key"]["secret"])
        self.assertTrue(meta["custom_provider_token"]["secret"], "未登记的 *_token 键按规则兜底")
        self.assertFalse(meta["fusion_dense_weight"]["secret"])
        self.assertTrue(meta["hyde_llm_api_key"]["label"])
        self.assertTrue(meta["fusion_dense_weight"]["hint"], "已知键必须带中文说明")


if __name__ == "__main__":
    unittest.main()
