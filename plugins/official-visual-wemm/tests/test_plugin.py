"""真实通过 PluginRuntime 走一遍 official-visual-wemm 的完整生命周期：
真的 Popen 子进程、真的发本机HTTP、真的申请/释放GPU资源租约、真的用
pymupdf 渲染PDF页面、真的写/查自己的 Chroma collection、真的在 disable
时把子进程杀干净。

`RAG_REDO_FAKE_WEMM=1` 让子进程内部用确定性假向量（按页图字节内容哈希
撒向量，不同页会得到不同向量），不需要真实模型——验证的是"子进程+HTTP+
pymupdf渲染+Chroma读写这条链路本身通不通、检索排序对不对"，不是"识别
准不准"，见 official_visual_wemm/server.py 模块 docstring 同名说明。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pymupdf  # noqa: E402

from core.singleton import pid_alive  # noqa: E402
from core.runtime import PluginRuntime, PluginState  # noqa: E402
from core.subprocess_service import SubprocessServiceError  # noqa: E402

# 插件包本身不在 sys.path 上（tests/run.py 按文件路径加载测试模块），沿用本文件
# TestVisualWemmServerModelLookup 的同一惯例按路径加载，只为拿输出向量维度。
_WEMM_PLUGIN_PATH = REPO_ROOT / "plugins" / "official-visual-wemm" / "official_visual_wemm" / "plugin.py"
_spec = importlib.util.spec_from_file_location("wemm_plugin_under_test", _WEMM_PLUGIN_PATH)
_wemm_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wemm_plugin)
WEMM_DIM = _wemm_plugin.WEMM_DIM


def _process_is_gone(pid: int) -> bool:
    """"这个 pid 是不是真的没了"：直接问 `core/singleton.py::pid_alive`（看进程是不是已经
    结束），测试里不另写一份判断（AGENTS.md §4.5、§7）。

    以前这里各自写成“OpenProcess 打得开就算还活着”，在 Windows 上判不准：进程被杀掉之后，
    只要别处还有人握着它的句柄，这个进程对象就还在、照样打得开，要过零点几秒才真正消失。
    2026-10-01 在整套回归里抓到过：`stop()` 之后立刻查，退出码已经是 1（被 taskkill 杀掉），
    却仍被判“还活着”，1 秒后再查就没了——“停止子进程”那条测试时好时坏就是这个原因。"""
    return not pid_alive(pid)


def _make_pdf(path: Path, page_texts: list[str]) -> None:
    """造一份真实多页 PDF——每页画一段不同的文字，只是为了让每页渲染出的
    PNG 字节不同（假向量按字节哈希撒，页与页之间要能被区分开）。"""
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=24)
    doc.save(str(path))
    doc.close()


class TestVisualWemmPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env_backup = os.environ.get("RAG_REDO_FAKE_WEMM")
        os.environ["RAG_REDO_FAKE_WEMM"] = "1"
        # 这个插件现在声明了真实的 env_bootstrap（会真的 pip install
        # torch 等几GB重依赖）——测试纪律要求不碰真实网络/不拖成几分钟
        # （见 core/subprocess_service.py::resolve_plugin_python 的
        # RAG_REDO_SKIP_ENV_BOOTSTRAP 说明），这里显式跳过，测试只关心
        # 子进程+HTTP+仲裁这条架构链路，不关心真实依赖装没装。
        self._skip_bootstrap_backup = os.environ.get("RAG_REDO_SKIP_ENV_BOOTSTRAP")
        os.environ["RAG_REDO_SKIP_ENV_BOOTSTRAP"] = "1"
        self.addCleanup(self._restore_env)

        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        _make_pdf(self.vault / "doc.pdf", ["第一页的内容 alpha", "第二页的内容 beta"])

        self.rt = PluginRuntime(REPO_ROOT / "plugins", state_file=self.tmp / "plugins_state.json", data_dir=self.tmp / "data")
        self.rt.scan()
        self.assertIn("official-visual-wemm", self.rt.plugins)
        self.rt.load("official-visual-wemm")
        self.assertEqual(self.rt.plugins["official-visual-wemm"].state, PluginState.LOADED)
        self.rt.enable("official-visual-wemm")
        self.assertEqual(
            self.rt.plugins["official-visual-wemm"].state,
            PluginState.ENABLED,
            self.rt.plugins["official-visual-wemm"].error,
        )
        self.instance = self.rt.plugins["official-visual-wemm"].instance

        def _cleanup_runtime() -> None:
            if self.rt.plugins["official-visual-wemm"].state.value == "enabled":
                self.rt.disable("official-visual-wemm")
            if self.rt.plugins["official-visual-wemm"].state.value == "disabled":
                self.rt.unload("official-visual-wemm")

        self.addCleanup(_cleanup_runtime)

    def _restore_env(self) -> None:
        if self._env_backup is None:
            os.environ.pop("RAG_REDO_FAKE_WEMM", None)
        else:
            os.environ["RAG_REDO_FAKE_WEMM"] = self._env_backup
        if self._skip_bootstrap_backup is None:
            os.environ.pop("RAG_REDO_SKIP_ENV_BOOTSTRAP", None)
        else:
            os.environ["RAG_REDO_SKIP_ENV_BOOTSTRAP"] = self._skip_bootstrap_backup

    def test_enable_acquires_gpu_lease(self):
        self.assertEqual(self.rt.resource_arbiter.holder_of("gpu:0"), "official-visual-wemm")

    def test_worker_process_takes_over_and_parent_can_reacquire_later(self):
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from pathlib import Path\n"
            "from core.runtime import PluginRuntime\n"
            f"runtime = PluginRuntime(Path({str(REPO_ROOT / 'plugins')!r}), "
            f"state_file=Path({str(self.tmp / 'worker_state.json')!r}), "
            f"data_dir=Path({str(self.tmp / 'data')!r}))\n"
            "runtime.scan()\n"
            "runtime.load('official-visual-wemm')\n"
            "runtime.enable('official-visual-wemm')\n"
            "plugin = runtime.plugins['official-visual-wemm']\n"
            "print('WORKER_ENABLED', plugin.instance._enabled, flush=True)\n"
            "runtime.disable('official-visual-wemm')\n"
            "runtime.unload('official-visual-wemm')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WORKER_ENABLED True", result.stdout)
        self.assertIsNone(self.rt.resource_arbiter.holder_of("gpu:0"))
        self.assertTrue(self.instance._handle.is_alive)
        self.instance.index_library("lib-after-worker", self.vault, ["doc.pdf"])
        self.assertEqual(self.instance._collection("lib-after-worker").count(), 2)
        self.assertEqual(self.rt.resource_arbiter.holder_of("gpu:0"), "official-visual-wemm")

    def test_index_library_writes_one_vector_per_page(self):
        self.instance.index_library("lib1", self.vault, ["doc.pdf"])
        collection = self.instance._collection("lib1")  # noqa: SLF001 - 测试直接确认底层写库结果
        self.assertEqual(collection.count(), 2)
        ids = set(collection.get(include=[])["ids"])
        self.assertEqual(ids, {"doc.pdf::0", "doc.pdf::1"})

    def test_navigate_returns_page_hits_with_correct_metadata(self):
        self.instance.index_library("lib1", self.vault, ["doc.pdf"])
        hits = self.instance.navigate("lib1", "随便什么查询", top_k=5)
        self.assertEqual(len(hits), 2)
        pages = {h.page_index for h in hits}
        self.assertEqual(pages, {0, 1})
        for hit in hits:
            self.assertEqual(hit.library_id, "lib1")
            self.assertEqual(hit.path, "doc.pdf")
            self.assertTrue(hit.abs_path.endswith("doc.pdf"))
            self.assertIsInstance(hit.score, float)

    def test_status_reports_subprocess_alive_and_page_counts(self):
        """对齐 obsidian-rag 的 wemm_status 诊断工具——2026-09-23 全面
        功能审计发现的缺口，见 tools.py::wemm_status。"""
        self.instance.index_library("lib1", self.vault, ["doc.pdf"])
        status = self.instance.status()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["subprocess_alive"])
        self.assertEqual(status["libraries"], {"lib1": {"page_count": 2, "pdf_count": 1}})

    def test_graph_page_states_are_typed_and_read_generation_without_starting_worker(self):
        self.instance.index_library("lib1", self.vault, ["doc.pdf"], generation="g1")
        self.rt.disable("official-visual-wemm")
        states = self.instance.graph_page_states("lib1", "g1")
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].library_id, "lib1")
        self.assertEqual(states[0].path, "doc.pdf")
        self.assertEqual(states[0].provider_id, "official-visual-wemm")
        self.assertEqual(states[0].status, "indexed")
        self.assertEqual(states[0].pages, (1, 2))
        self.assertFalse(self.rt.plugins["official-visual-wemm"].instance._handle)

    # ---- 转换缓存看得见（BC-19）-----------------------------------------------

    def test_page_states_carry_the_total_page_count_and_the_round_that_built_them(self):
        """清单要写“28/36 页”、列出缺哪几页，还要分清页向量是这轮新建的还是沿用上一轮的。"""
        self.instance.index_library("lib1", self.vault, ["doc.pdf"], generation="g1")
        self.assertTrue(self.instance._generations.commit("lib1", "g1"))  # noqa: SLF001
        first = self.instance.graph_page_states("lib1", "g1")
        self.assertEqual((first[0].page_count, first[0].built_in), (2, "g1"))
        self.instance.index_library(
            "lib1", self.vault, ["doc.pdf"], generation="g2", changed_paths=[], previous_generation="g1",
        )
        again = self.instance.graph_page_states("lib1", "g2")
        # 没重编：即使压缩把页向量搬进了新段，也还记着是 g1 编的
        self.assertEqual((again[0].page_count, again[0].built_in), (2, "g1"))

    def test_navigate_can_be_limited_to_one_pdf(self):
        """“试搜”只在这一份 PDF 的页里找，别的 PDF 的页一律不出现。"""
        _make_pdf(self.vault / "other.pdf", ["另一份第一页 gamma", "另一份第二页 delta", "另一份第三页"])
        self.instance.index_library("lib1", self.vault, ["doc.pdf", "other.pdf"])
        everything = self.instance.navigate("lib1", "查询", top_k=10)
        self.assertEqual({hit.path for hit in everything}, {"doc.pdf", "other.pdf"})
        only = self.instance.navigate("lib1", "查询", top_k=10, path="other.pdf")
        self.assertEqual([hit.path for hit in only], ["other.pdf"] * 3)
        self.assertEqual({hit.page_index for hit in only}, {0, 1, 2})
        scores = [hit.score for hit in only]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # 同一页单独查和全库查，分数是同一把尺子
        full = {(hit.path, hit.page_index): hit.score for hit in everything}
        for hit in only:
            self.assertAlmostEqual(hit.score, full[(hit.path, hit.page_index)], places=3)
        self.assertEqual(self.instance.navigate("lib1", "查询", top_k=10, path="不存在.pdf"), [])

    def test_render_page_png_draws_one_page_on_the_cpu_without_the_service(self):
        self.rt.disable("official-visual-wemm")
        png = self.instance.render_page_png(self.vault / "doc.pdf", 2, max_side=200)
        self.assertTrue(png.startswith(b"\x89PNG"))
        pixmap = pymupdf.Pixmap(png)
        self.assertLessEqual(max(pixmap.width, pixmap.height), 200)
        self.assertGreaterEqual(max(pixmap.width, pixmap.height), 190)
        with self.assertRaises(ValueError):
            self.instance.render_page_png(self.vault / "doc.pdf", 3)
        with self.assertRaises(ValueError):
            self.instance.render_page_png(self.vault / "doc.pdf", 0)
        with self.assertRaises(ValueError):
            self.instance.render_page_png(self.vault / "没有这个文件.pdf", 1)
        self.assertFalse(self.instance._handle)  # noqa: SLF001 - 画小图不拉起看图服务

    def test_cache_info_says_where_the_page_library_lives_and_what_it_costs(self):
        info = self.instance.cache_info()
        self.assertTrue(Path(info["dir"]).is_dir())
        self.assertEqual(info["bytes_per_page"], WEMM_DIM * 4 * 2 + 512)
        server = TestVramGateIsReportedTruthfully._load_server_module()
        self.assertEqual(info["vram_gb"], server.WEMM_MIN_VRAM_GB)
        # 插件对外说的“闲多久自动卸载”必须等于服务端真实计时，否则用户被告知错的时间
        self.assertEqual(info["idle_unload_seconds"], server.WEMM_UNLOAD_AFTER_SECONDS)

    def test_status_before_any_indexing_has_no_libraries(self):
        status = self.instance.status()
        self.assertEqual(status["libraries"], {})

    def test_status_does_not_start_subprocess(self):
        """只读诊断不该有副作用——禁用后子进程已经不在了，status() 不该
        把它重新拉起来（对齐 obsidian-rag"只读，不启动服务"的承诺）。"""
        self.rt.disable("official-visual-wemm")
        status = self.instance.status()
        self.assertFalse(status["subprocess_alive"])
        self.assertFalse(status["enabled"])

    def test_navigate_on_library_with_no_pdf_index_returns_empty_not_error(self):
        # 从没调用过 index_library，对应 collection 压根不存在——不该报错，
        # 应该折叠成空结果（同 wemm_retriever.py "页库不存在只记错误不阻断"
        # 的简化版：这里更简单，直接判定为"这个库没有可视觉导航的内容"）。
        hits = self.instance.navigate("lib-never-indexed", "query", top_k=5)
        self.assertEqual(hits, [])

    def test_reindex_removes_stale_pages_for_deleted_pdf(self):
        second_pdf = self.vault / "second.pdf"
        _make_pdf(second_pdf, ["only page"])
        self.instance.index_library("lib1", self.vault, ["doc.pdf", "second.pdf"])
        collection = self.instance._collection("lib1")  # noqa: SLF001
        self.assertEqual(collection.count(), 3)

        # second.pdf 不再在本轮传入的 pdf_paths 里（模拟文件被移出检索范围/
        # 被删除）——重跑 index_library 应该清掉它的旧页向量，不留幽灵页。
        self.instance.index_library("lib1", self.vault, ["doc.pdf"])
        self.assertEqual(collection.count(), 2)
        ids = set(collection.get(include=[])["ids"])
        self.assertEqual(ids, {"doc.pdf::0", "doc.pdf::1"})

    def test_incremental_generation_reuses_unchanged_pdf_segments(self):
        self.instance.index_library("lib1", self.vault, ["doc.pdf"], generation="g1")
        self.assertTrue(self.instance._generations.commit("lib1", "g1"))
        original_call = self.instance._handle.call
        with patch.object(self.instance._handle, "call", wraps=original_call) as call_mock:
            self.instance.index_library(
                "lib1",
                self.vault,
                ["doc.pdf"],
                generation="g2",
                changed_paths=[],
                previous_generation="g1",
            )
        self.assertEqual(call_mock.call_count, 0)
        self.assertTrue(self.instance._generations.commit("lib1", "g2"))
        state = self.instance._read_state("lib1", "g2")
        self.assertEqual(state["segments"], ["g1"])
        self.assertEqual(len(self.instance.navigate("lib1", "query", top_k=5)), 2)

    def test_unchanged_pdf_is_not_reencoded_when_text_stage_rebuild_marks_it_changed(self):
        self.instance.index_library("lib1", self.vault, ["doc.pdf"], generation="g1")
        self.assertTrue(self.instance._generations.commit("lib1", "g1"))
        original_call = self.instance._handle.call
        with patch.object(self.instance._handle, "call", wraps=original_call) as call_mock:
            self.instance.index_library(
                "lib1",
                self.vault,
                ["doc.pdf"],
                generation="g2",
                changed_paths=["doc.pdf"],
                previous_generation="g1",
            )
        self.assertEqual(call_mock.call_count, 0)
        self.assertTrue(self.instance._generations.commit("lib1", "g2"))
        self.assertEqual(self.instance._read_state("lib1", "g2")["segments"], ["g1"])

    def test_changed_pdf_uses_new_segment_and_masks_old_pages(self):
        second_pdf = self.vault / "second.pdf"
        _make_pdf(second_pdf, ["temporary"])
        self.instance.index_library("lib1", self.vault, ["doc.pdf", "second.pdf"], generation="g1")
        self.assertTrue(self.instance._generations.commit("lib1", "g1"))
        second_pdf.unlink()
        _make_pdf(self.vault / "doc.pdf", ["changed first", "changed second"])
        self.instance.index_library(
            "lib1",
            self.vault,
            ["doc.pdf"],
            generation="g2",
            changed_paths=["doc.pdf"],
            previous_generation="g1",
        )
        self.assertTrue(self.instance._generations.commit("lib1", "g2"))
        state = self.instance._read_state("lib1", "g2")
        self.assertEqual(state["segments"], ["g1", "g2"])
        self.assertEqual(set(state["files"]), {"doc.pdf"})
        hits = self.instance.navigate("lib1", "query", top_k=5)
        self.assertEqual({hit.path for hit in hits}, {"doc.pdf"})
        self.assertEqual(len(hits), 2)

    # ---- 2026-09-29 真机：WEMM 整轮没被调用（BC-11/BC-15）-----------------------
    # 4 个库的日志全是"WEMM子进程未运行，页级索引本轮跳过"：文字向量模型做完后还占着
    # 显卡名额，WEMM 抢不到；70 个 PDF 的页库因此全被记成失败，而失败记录只要 PDF 没变
    # 就永远不会重试——图谱里就永远没有 WEMM 页节点。旧项目 wemm_indexer.py 的做法：
    # ①真要拉看图服务（要占显存）前才调 before_serve 让 bge/reranker 让路，无活可干时
    # 零拉起零开销（:260-278）；②失败终态绝不走快速路径，每轮都重试（:311-312）。

    def test_before_serve_runs_before_the_service_is_needed_and_only_when_there_is_work(self):
        order: list[str] = []
        real_ensure = self.instance._ensure_alive  # noqa: SLF001

        def _recording_ensure():
            order.append("ensure")
            return real_ensure()

        with patch.object(self.instance, "_ensure_alive", side_effect=_recording_ensure):
            self.instance.index_library(
                "lib1", self.vault, ["doc.pdf"], generation="g1",
                before_serve=lambda: order.append("release"),
            )
        self.assertEqual(order, ["release", "ensure"])
        self.assertTrue(self.instance._generations.commit("lib1", "g1"))  # noqa: SLF001

        order.clear()
        with patch.object(self.instance, "_ensure_alive", side_effect=_recording_ensure):
            self.instance.index_library(
                "lib1", self.vault, ["doc.pdf"], generation="g2", changed_paths=[],
                previous_generation="g1", before_serve=lambda: order.append("release"),
            )
        self.assertEqual(order, [], "没有页要渲染时，既不该让文字模型让路，也不该去抢显卡/拉起看图服务")

    def test_a_failing_before_serve_never_breaks_page_indexing(self):
        def _boom():
            raise RuntimeError("release failed")

        self.instance.index_library("lib1", self.vault, ["doc.pdf"], before_serve=_boom)
        self.assertEqual(self.instance._collection("lib1").count(), 2)  # noqa: SLF001

    def test_a_failed_page_index_is_retried_next_round_even_when_the_pdf_is_unchanged(self):
        with patch.object(self.instance, "_ensure_alive", return_value=False):
            self.instance.index_library("lib1", self.vault, ["doc.pdf"], generation="g1")
        self.assertTrue(self.instance._generations.commit("lib1", "g1"))  # noqa: SLF001
        first = self.instance._read_state("lib1", "g1")["files"]["doc.pdf"]  # noqa: SLF001
        self.assertEqual(first["status"], "failed")

        self.instance.index_library(
            "lib1", self.vault, ["doc.pdf"], generation="g2", changed_paths=[], previous_generation="g1",
        )
        self.assertTrue(self.instance._generations.commit("lib1", "g2"))  # noqa: SLF001
        second = self.instance._read_state("lib1", "g2")["files"]["doc.pdf"]  # noqa: SLF001
        self.assertEqual(second["status"], "indexed")
        self.assertEqual(len(second["page_ids"]), 2)
        self.assertEqual(len(self.instance.navigate("lib1", "query", top_k=5)), 2)

    def test_a_successfully_indexed_pdf_is_still_never_reencoded(self):
        """重试只针对失败/部分成功的记录；已成功且没变的 PDF 仍然零渲染零编码。"""
        self.instance.index_library("lib1", self.vault, ["doc.pdf"], generation="g1")
        self.assertTrue(self.instance._generations.commit("lib1", "g1"))  # noqa: SLF001
        with patch.object(self.instance._handle, "call", wraps=self.instance._handle.call) as call_mock:  # noqa: SLF001
            self.instance.index_library(
                "lib1", self.vault, ["doc.pdf"], generation="g2", changed_paths=[], previous_generation="g1",
            )
        self.assertEqual(call_mock.call_count, 0)

    def test_disable_releases_gpu_lease_and_kills_subprocess(self):
        pid = self.instance._handle._process.pid  # noqa: SLF001 - 直接问操作系统这个pid还在不在
        self.rt.disable("official-visual-wemm")
        self.assertIsNone(self.rt.resource_arbiter.holder_of("gpu:0"))
        self.assertTrue(_process_is_gone(pid))

    def test_index_library_when_subprocess_not_running_folds_to_warning_not_crash(self):
        self.rt.disable("official-visual-wemm")
        # 不该抛异常——同 extractor "绝不抛异常"的纪律，见 plugin.py 模块 docstring
        self.instance.index_library("lib1", self.vault, ["doc.pdf"])

    def test_ensure_alive_restarts_subprocess_after_it_exits(self):
        """子进程可能因为空闲自退出（server.py 的 idle-exit 机制）而不在
        了——模拟这个场景（直接把子进程停掉，不经过 on_disable，插件本身
        仍处于 enabled 状态），下一次真正调用必须透明地重新拉起，不是永久
        瘫痪（否则"空闲自退出省资源"这个优化会变成用户遇到的真实回归）。"""
        old_pid = self.instance._handle._process.pid  # noqa: SLF001
        self.instance._handle.stop()  # noqa: SLF001 - 模拟子进程自己没了，不走 on_disable
        self.assertFalse(self.instance._handle.is_alive)  # noqa: SLF001
        self.instance.index_library("lib1", self.vault, ["doc.pdf"])
        self.assertIsNotNone(self.instance._handle)  # noqa: SLF001
        self.assertNotEqual(self.instance._handle._process.pid, old_pid)  # noqa: SLF001
        collection = self.instance._collection("lib1")  # noqa: SLF001
        self.assertEqual(collection.count(), 2)

    def test_ensure_alive_does_not_restart_after_explicit_disable(self):
        """区别于上一条：真正被 disable 之后，直接调用这个实例的方法不该
        意外把子进程又偷偷拉起来——disable 就是 disable，不是"暂时睡着"。"""
        self.rt.disable("official-visual-wemm")
        self.instance.navigate("lib1", "query")
        self.assertIsNone(self.instance._handle)  # noqa: SLF001

    def test_resource_arbiter_soft_evict_unloads_model_without_killing_subprocess(self):
        """同一优先级层级的另一个 GPU 消费者申请"gpu:0"时，WEMM 应该只被
        软驱逐（子进程收到 /evict 请求、继续存活），不是被整个杀掉——软
        驱逐比整个重启轻，重新可用只需模型冷加载。"""
        pid_before = self.instance._handle._process.pid  # noqa: SLF001
        acquired = self.rt.resource_arbiter.acquire(
            "gpu:0", "some-other-gpu-consumer", priority=10, preempt_equal=True
        )
        self.assertTrue(acquired)
        self.addCleanup(
            self.rt.resource_arbiter.release,
            "gpu:0",
            "some-other-gpu-consumer",
        )
        self.assertEqual(self.rt.resource_arbiter.holder_of("gpu:0"), "some-other-gpu-consumer")
        # 子进程本身还活着、还是同一个 pid——只是模型被卸载了，不是整个被杀掉
        self.assertIsNotNone(self.instance._handle)  # noqa: SLF001
        self.assertEqual(self.instance._handle._process.pid, pid_before)  # noqa: SLF001
        self.assertTrue(self.instance._handle.is_alive)
        self.instance.index_library("lib-after-preempt", self.vault, ["doc.pdf"])
        self.assertEqual(self.rt.resource_arbiter.holder_of("gpu:0"), "official-visual-wemm")

    def test_release_gpu_soft_evicts_without_killing_subprocess(self):
        """手动"释放显存"按钮用（core/pipeline.py::release_gpu_memory，
        2026-09-29 新能力，BC-16）——走的是和上一条测试同一条软驱逐路径，
        区别只是触发方是用户主动点按钮，不是被别的消费者抢占：子进程本身
        （同一个 pid）继续存活，只是模型被请求卸载，下次真正查询/索引时
        子进程自己按需重新加载模型，不需要用户重新开启 WEMM 或重启 GUI。"""
        pid_before = self.instance._handle._process.pid  # noqa: SLF001
        self.instance.release_gpu()
        self.assertIsNotNone(self.instance._handle)  # noqa: SLF001
        self.assertEqual(self.instance._handle._process.pid, pid_before)  # noqa: SLF001
        self.assertTrue(self.instance._handle.is_alive)
        # 插件仍然可用：不需要重新 on_enable，子进程按需重新加载模型。
        self.instance.index_library("lib-after-release", self.vault, ["doc.pdf"])
        collection = self.instance._collection("lib-after-release")  # noqa: SLF001
        self.assertEqual(collection.count(), 2)

    def test_release_gpu_is_a_noop_when_subprocess_never_started(self):
        """WEMM 后端本来就关着/子进程还没拉起来时，点"释放显存"必须是
        安全的空操作，不能抛异常（用户不知道、也不需要知道内部有没有子
        进程在跑）。"""
        self.rt.disable("official-visual-wemm")
        self.instance.release_gpu()  # 不抛异常即通过
        self.assertIsNone(self.instance._handle)  # noqa: SLF001

    def test_subprocess_is_pointed_at_the_default_project_models_folder(self):
        """BC-17：没配 models_dir 时，看图服务去项目内的 models/ 找/下模型。"""
        from core.paths import models_dir

        env = self.instance._handle._env  # noqa: SLF001
        self.assertEqual(env["HF_HUB_CACHE"], str(models_dir("")))
        self.assertEqual(Path(env["HF_HUB_CACHE"]).name, "models")
        self.assertIn("PATH", env, "子进程必须继承当前环境，丢 PATH 会直接起不来")

    def test_subprocess_uses_the_configured_models_dir_after_restart(self):
        """用户在设置页改了模型路径：看图服务下次（重新）启动时用新路径。"""
        target = self.tmp / "my-models"
        self.rt.settings.set("models_dir", str(target))
        self.instance._stop_handle()  # noqa: SLF001
        self.instance._start_handle()  # noqa: SLF001
        self.assertEqual(self.instance._handle._env["HF_HUB_CACHE"], str(target))  # noqa: SLF001
        self.assertTrue(self.instance._handle.is_alive)  # noqa: SLF001


class TestVisualWemmServerModelLookup(unittest.TestCase):
    """BC-17：看图服务（与核心完全隔离的子进程脚本）只认核心传进来的 `HF_HUB_CACHE`。"""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        path = REPO_ROOT / "plugins" / "official-visual-wemm" / "official_visual_wemm" / "server.py"
        spec = importlib.util.spec_from_file_location("wemm_server_under_test", path)
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _env(self, **values: str):
        cleaned = {k: v for k, v in os.environ.items() if k not in ("HF_HUB_CACHE", "HF_HOME")}
        cleaned.update(values)
        return patch.dict(os.environ, cleaned, clear=True)

    def test_hf_hub_cache_variable_wins(self):
        with self._env(HF_HUB_CACHE=str(self.tmp / "a"), HF_HOME=str(self.tmp / "b")):
            self.assertEqual(self.server._hf_hub_dir(), self.tmp / "a")

    def test_hf_home_is_used_when_no_explicit_cache_variable(self):
        with self._env(HF_HOME=str(self.tmp / "b")):
            self.assertEqual(self.server._hf_hub_dir(), self.tmp / "b" / "hub")

    def test_falls_back_to_the_huggingface_default_location(self):
        with self._env():
            self.assertEqual(self.server._hf_hub_dir(), Path.home() / ".cache" / "huggingface" / "hub")

    def test_a_downloaded_snapshot_in_the_configured_folder_is_reused_not_redownloaded(self):
        snapshot = self.tmp / "models--tencent--WeMM-Embedding-2B" / "snapshots" / "abc123"
        snapshot.mkdir(parents=True)
        with self._env(HF_HUB_CACHE=str(self.tmp)):
            self.assertEqual(self.server._resolve_model_path("tencent/WeMM-Embedding-2B"), str(snapshot))

    def test_missing_model_falls_through_to_the_model_id_for_on_demand_download(self):
        with self._env(HF_HUB_CACHE=str(self.tmp)):
            self.assertEqual(
                self.server._resolve_model_path("tencent/WeMM-Embedding-2B"), "tencent/WeMM-Embedding-2B"
            )


class TestVisualWemmBackendGate(unittest.TestCase):
    """缺陷 B 的复现组：`on_enable` 曾经在**没有任何开关判断**的情况下直接
    抢 "gpu:0" 租约并拉起 WEMM 子进程——双击一次 GUI 就等于启动一个 5.1GB
    常驻模型子进程，即使用户从没打开过任何 PDF、也没开过 WEMM。同一个
    REQUIRED_PLUGINS 列表里另一个 subprocess_service 插件
    (official-ocr-mineru-local) 就有 `is_active()` 门禁，两个门禁不一致。
    门禁条件对齐 LEGACY obsidian-rag：`wemm_backend` 设置项取值
    on/local 才算开（obsidian-rag/wemm_indexer.py:132、
    obsidian-rag/wemm_retriever.py:37），默认 "on"
    （obsidian-rag/config.py:122）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env_backup = os.environ.get("RAG_REDO_FAKE_WEMM")
        os.environ["RAG_REDO_FAKE_WEMM"] = "1"
        self._skip_backup = os.environ.get("RAG_REDO_SKIP_ENV_BOOTSTRAP")
        os.environ["RAG_REDO_SKIP_ENV_BOOTSTRAP"] = "1"
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, backup in (("RAG_REDO_FAKE_WEMM", self._env_backup),
                            ("RAG_REDO_SKIP_ENV_BOOTSTRAP", self._skip_backup)):
            if backup is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = backup

    def _runtime(self, name: str) -> PluginRuntime:
        runtime = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / f"{name}-state.json",
            data_dir=self.tmp / f"{name}-data",
        )
        runtime.scan()
        runtime.load("official-visual-wemm")
        return runtime

    def test_backend_off_does_not_acquire_lease_and_does_not_start_subprocess(self):
        runtime = self._runtime("off")
        runtime.settings.set("wemm_backend", "off")
        runtime.enable("official-visual-wemm")
        self.addCleanup(runtime.disable, "official-visual-wemm")
        instance = runtime.plugins["official-visual-wemm"].instance
        self.assertFalse(instance.is_active())
        self.assertIsNone(instance._handle)  # noqa: SLF001 - 确认没有子进程被拉起
        self.assertIsNone(runtime.resource_arbiter.holder_of("gpu:0"))

    def test_backend_local_counts_as_enabled(self):
        runtime = self._runtime("local")
        runtime.settings.set("wemm_backend", "local")
        runtime.enable("official-visual-wemm")
        self.addCleanup(runtime.disable, "official-visual-wemm")
        instance = runtime.plugins["official-visual-wemm"].instance
        self.assertTrue(instance.is_active())
        self.assertEqual(runtime.resource_arbiter.holder_of("gpu:0"), "official-visual-wemm")

    def test_turning_backend_on_takes_effect_without_restarting_the_app(self):
        """用户在设置里把 WEMM 打开后**不重启**也必须能生效——"重启才生效"
        会让用户以为功能坏了。门禁只在 on_enable 拦一次驻留，真正的
        拉起发生在每次真正使用前（`_ensure_alive`/`_ensure_query_service`）
        读的是设置存储的当前值。"""
        runtime = self._runtime("toggle")
        runtime.settings.set("wemm_backend", "off")
        runtime.enable("official-visual-wemm")
        self.addCleanup(runtime.disable, "official-visual-wemm")
        instance = runtime.plugins["official-visual-wemm"].instance
        self.assertIsNone(instance._handle)  # noqa: SLF001
        self.assertIsNone(runtime.resource_arbiter.holder_of("gpu:0"))

        runtime.settings.set("wemm_backend", "on")
        self.assertTrue(instance.is_active())
        self.assertTrue(instance._ensure_alive())  # noqa: SLF001
        self.assertIsNotNone(instance._handle)  # noqa: SLF001
        self.assertTrue(instance._handle.is_alive)  # noqa: SLF001
        self.assertEqual(runtime.resource_arbiter.holder_of("gpu:0"), "official-visual-wemm")

    def test_index_library_is_skipped_while_backend_is_off(self):
        """LEGACY wemm_backend=off 时页索引是"静默跳过（零开销）"
        （obsidian-rag/index.py:1842、docs/legacy/TASK_LOG.md:1818）——不
        能因为用户在设置里关了 WEMM 就把 5.1GB 子进程又拉起来。"""
        runtime = self._runtime("index-off")
        runtime.settings.set("wemm_backend", "off")
        runtime.enable("official-visual-wemm")
        self.addCleanup(runtime.disable, "official-visual-wemm")
        instance = runtime.plugins["official-visual-wemm"].instance
        vault = self.tmp / "vault"
        vault.mkdir()
        _make_pdf(vault / "doc.pdf", ["第一页", "第二页"])
        instance.index_library("lib1", vault, ["doc.pdf"])
        self.assertIsNone(instance._handle)  # noqa: SLF001
        self.assertIsNone(runtime.resource_arbiter.holder_of("gpu:0"))
        self.assertEqual(instance._read_state("lib1", "legacy")["files"], {})  # noqa: SLF001


class TestVisualWemmNavigateVeto(unittest.TestCase):
    """缺陷 C 的复现组：`navigate` 在抢不到 GPU 租约时只 `return []` 并
    打一行 warning，调用方（plugins/official-mcp-server 的
    `navigate_knowledge`）拿到空列表照样回 `{"ok": True, "results": []}`——
    与"确实没有匹配页"完全同形。LEGACY obsidian-rag/server.py:688-723 专门
    处理过：拿不到锁/索引在跑 → 明确告诉调用方"这次没查"；**若看图服务已
    经活着则跳过一切驻留变更直接查**（:712-716 注释原文大意："服务已在：
    直接查，不拉起（拉起是驻留变更，veto 期一律不做）"）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env_backup = os.environ.get("RAG_REDO_FAKE_WEMM")
        os.environ["RAG_REDO_FAKE_WEMM"] = "1"
        self._skip_backup = os.environ.get("RAG_REDO_SKIP_ENV_BOOTSTRAP")
        os.environ["RAG_REDO_SKIP_ENV_BOOTSTRAP"] = "1"
        self.addCleanup(self._restore_env)

        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        _make_pdf(self.vault / "doc.pdf", ["第一页的内容 alpha", "第二页的内容 beta"])

        self.rt = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.rt.scan()
        self.rt.load("official-visual-wemm")
        self.rt.enable("official-visual-wemm")
        self.instance = self.rt.plugins["official-visual-wemm"].instance
        self.plugin_module = sys.modules[type(self.instance).__module__]
        self.addCleanup(lambda: self.rt.disable("official-visual-wemm"))

    def _restore_env(self) -> None:
        for key, backup in (("RAG_REDO_FAKE_WEMM", self._env_backup),
                            ("RAG_REDO_SKIP_ENV_BOOTSTRAP", self._skip_backup)):
            if backup is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = backup

    def test_veto_when_lease_is_taken_and_service_not_alive_is_distinguishable(self):
        """租约被别人占着 + 服务没活着 → 必须是**可区分**的"这次没查"，
        不是和"没有匹配页"同形的空列表。"""
        self.instance.index_library("lib1", self.vault, ["doc.pdf"])
        # 模拟"看图服务没活着"（空闲自退出后的常态）
        self.instance._handle.stop()  # noqa: SLF001
        self.assertFalse(self.instance._handle.is_alive)  # noqa: SLF001
        # 让另一个 GPU 消费者以"抢不动"的优先级占住租约
        self.assertTrue(
            self.rt.resource_arbiter.acquire("gpu:0", "busy-indexer", priority=99)
        )
        self.addCleanup(self.rt.resource_arbiter.release, "gpu:0", "busy-indexer")

        with self.assertRaises(self.plugin_module.VisualVetoError) as ctx:
            self.instance.navigate("lib1", "查询")
        self.assertEqual(ctx.exception.reason, "gpu-busy")
        self.assertIn("索引任务进行中", str(ctx.exception))

    def test_alive_service_is_queried_directly_without_any_residency_change(self):
        """服务已活着 → 直接查，**不做任何驻留变更**（既不抢租约也不重新
        拉起子进程）。LEGACY server.py:712-716 的降级路径。"""
        self.instance.index_library("lib1", self.vault, ["doc.pdf"])
        pid_before = self.instance._handle._process.pid  # noqa: SLF001
        # 租约被别人占着（veto 条件成立），但服务活着——必须照查不误
        self.assertTrue(
            self.rt.resource_arbiter.acquire("gpu:0", "busy-indexer", priority=99)
        )
        self.addCleanup(self.rt.resource_arbiter.release, "gpu:0", "busy-indexer")
        self.assertEqual(self.rt.resource_arbiter.holder_of("gpu:0"), "busy-indexer")

        with patch.object(
            self.instance._resource_arbiter, "acquire", side_effect=AssertionError("veto 期不允许抢租约")
        ):
            hits = self.instance.navigate("lib1", "查询", top_k=5)
        self.assertEqual(len(hits), 2)
        self.assertEqual(self.instance._handle._process.pid, pid_before)  # noqa: SLF001
        # 租约归属没被这次查询改动
        self.assertEqual(self.rt.resource_arbiter.holder_of("gpu:0"), "busy-indexer")

    def test_genuine_empty_result_is_still_a_plain_empty_list(self):
        """"确实没有匹配页"必须仍然是普通空列表——可区分不等于所有空结果
        都变异常。"""
        self.instance.index_library("lib1", self.vault, ["doc.pdf"])
        hits = self.instance.navigate("lib-never-indexed", "查询", top_k=5)
        self.assertEqual(hits, [])

    def test_backend_off_reports_distinguishable_reason(self):
        self.rt.settings.set("wemm_backend", "off")
        with self.assertRaises(self.plugin_module.VisualVetoError) as ctx:
            self.instance.navigate("lib1", "查询")
        self.assertEqual(ctx.exception.reason, "backend-off")
        self.assertIn("wemm_backend", str(ctx.exception))

    def test_status_reports_backend_and_log_file_path(self):
        """LEGACY 的 wemm_status（obsidian-rag/server.py:965-991）明确把
        开关状态和服务日志告诉用户/AI，navigate 拿不到租约时让人"去哪儿
        看诊断"——这两项都在 status() 里。"""
        self.instance.index_library("lib1", self.vault, ["doc.pdf"])
        status = self.instance.status()
        self.assertEqual(status["backend"], "on")
        self.assertTrue(status["log_file"])
        self.assertTrue(Path(status["log_file"]).name.endswith(".log"))
        # 日志必须落在 data 根之下（架构红线：所有数据落在 data/ 目录），
        # 不能跟着插件源码目录走（卸载便携包会连带删掉日志）。
        self.assertTrue(str(status["log_file"]).startswith(str(self.tmp / "data")))


class TestServerExitsWhenItsHostIsGone(unittest.TestCase):
    """WEMM 看图服务盯着宿主进程：宿主异常没了（崩溃/被“结束任务”/被强杀）来不及走 stop() 时，
    Windows 上子进程不会跟着走，会一直占着显存直到半小时后的空闲自退出。2026-09-29 实测抓到
    过一个这样的孤儿（宿主没了 29 分钟它还活着），也是操作者反馈“关掉 GUI 之后显存没有及时
    释放、进程没有关闭”的一条可能机制。宿主通过环境变量 RAG_REDO_PARENT_PID 告诉子进程自己的 pid。"""

    def setUp(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "wemm_parent_watch_under_test",
            REPO_ROOT / "plugins" / "official-visual-wemm" / "official_visual_wemm" / "server.py",
        )
        assert spec is not None and spec.loader is not None
        self.server_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server_mod)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def _finished_pid() -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_pid_alive_tells_a_live_process_from_a_finished_one(self) -> None:
        self.assertTrue(self.server_mod._pid_alive(os.getpid()))
        self.assertFalse(self.server_mod._pid_alive(self._finished_pid()))
        self.assertFalse(self.server_mod._pid_alive(0))

    def test_watch_parent_calls_on_gone_when_the_parent_is_already_dead(self) -> None:
        called: list[int] = []
        self.server_mod._watch_parent(self._finished_pid(), interval=0.01, on_gone=lambda: called.append(1))
        self.assertEqual(called, [1])

    def test_watch_parent_keeps_waiting_while_the_parent_lives(self) -> None:
        class _Stop(Exception):
            pass

        polls: list[float] = []
        called: list[int] = []

        def _sleep(seconds: float) -> None:
            polls.append(seconds)
            if len(polls) >= 3:
                raise _Stop

        with self.assertRaises(_Stop):
            self.server_mod._watch_parent(os.getpid(), interval=0.5, on_gone=lambda: called.append(1), sleep=_sleep)
        self.assertEqual(polls, [0.5, 0.5, 0.5])
        self.assertEqual(called, [])

    def test_without_a_parent_pid_the_watch_is_off_and_returns_at_once(self) -> None:
        from unittest import mock

        for value in (None, "", "not-a-number", "0"):
            env = {k: v for k, v in os.environ.items() if k != "RAG_REDO_PARENT_PID"}
            if value is not None:
                env["RAG_REDO_PARENT_PID"] = value
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(self.server_mod, "_watch_parent", side_effect=AssertionError("must not watch")):
                    self.server_mod._parent_watch_daemon()

    @unittest.skipUnless(sys.platform == "win32" or hasattr(os, "killpg"), "需要真实进程语义")
    def test_orphaned_server_really_exits_after_its_host_is_killed(self) -> None:
        """端到端：宿主拉起真实服务子进程后被硬杀（没机会 stop），服务必须自己退出。"""
        try:
            import psutil
        except ImportError:
            self.skipTest("需要 psutil 枚举子进程树")
        helper = self.tmp / "host.py"
        helper.write_text(
            "\n".join(
                [
                    "import os, sys, time",
                    "sys.path.insert(0, r'%s')" % REPO_ROOT,
                    "from core.subprocess_service import SubprocessServiceHandle",
                    "os.environ['RAG_REDO_FAKE_WEMM'] = '1'",
                    "handle = SubprocessServiceHandle(",
                    "    [sys.executable, 'server.py', '--port', '{port}'],",
                    "    health_check='http://127.0.0.1:{port}/health',",
                    "    cwd=r'%s',"
                    % (REPO_ROOT / "plugins" / "official-visual-wemm" / "official_visual_wemm"),
                    "    log_path=r'%s'," % (self.tmp / "server.log"),
                    "    startup_timeout=60.0,",
                    ")",
                    "handle.start()",
                    "print('CHILD', handle._process.pid, flush=True)",
                    "time.sleep(600)",
                ]
            ),
            encoding="utf-8",
        )
        host = subprocess.Popen([sys.executable, str(helper)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: host.poll() is None and host.kill())
        line = host.stdout.readline().strip()
        self.assertTrue(line.startswith("CHILD"), line)
        child = psutil.Process(int(line.split()[1]))
        tree = [child] + child.children(recursive=True)
        self.addCleanup(lambda: [p.kill() for p in tree if p.is_running()])
        host.kill()  # TerminateProcess：宿主没有任何机会收口
        host.wait(timeout=10)
        gone, alive = psutil.wait_procs(tree, timeout=30)
        self.assertEqual([p.pid for p in alive], [], "宿主没了之后服务进程树必须自己退出")


class TestVisualIndexStopsWhenTheServiceIsUnreachable(unittest.TestCase):
    """服务进程起来了、但页级调用打不通时，本轮必须立刻收手（2026-09-29 真机事故复现）。

    真机证据（`data-real/index_worker.log` + `data-real/visual_wemm/wemm_server.log`）：
    WEMM 子进程被成功拉起，`_ensure_alive` 返回 True，但服务端 `_wait_for_vram(5.5GB)`
    一直等不到显存（8GB 卡上 Windows 与桌面应用本身就吃掉约 2.6GB，把全部模型卸干净
    后空闲也只有约 5.1GB < 5.5GB），随后子进程连接被拒。此时页级 `embed` 每页都抛
    `SubprocessServiceError`，而旧代码只 `continue` 换下一页——78 份 PDF × 每份几十页
    ＝ 几千次注定失败的调用，索引进程假活半小时；用户以为卡死而中断，整轮 generation
    从未发布（`core/pipeline.py` 的 manifest 写入与 commit 都在视觉阶段之后），
    已经算好的文字索引全部丢失。

    恢复旧项目 `obsidian-rag/wemm_indexer.py:257-278`（问题46「单轮单次」）的语义：
    **服务不可达不是"这一页不行"，而是"本轮服务不可用"**——立刻终止本轮页级索引，
    把剩余文件记成可重试的失败终态，让文字索引照常发布，下轮自动重试。
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._fake_backup = os.environ.get("RAG_REDO_FAKE_WEMM")
        os.environ["RAG_REDO_FAKE_WEMM"] = "1"
        self._skip_backup = os.environ.get("RAG_REDO_SKIP_ENV_BOOTSTRAP")
        os.environ["RAG_REDO_SKIP_ENV_BOOTSTRAP"] = "1"
        self.addCleanup(self._restore_env)

        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        # 三份各 2 页：逐页空转的话是 6 次 embed 调用，本轮就该在第 1 次后收手。
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            _make_pdf(self.vault / name, [f"{name} 第一页", f"{name} 第二页"])

        self.rt = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.rt.scan()
        self.rt.load("official-visual-wemm")
        self.rt.enable("official-visual-wemm")
        self.instance = self.rt.plugins["official-visual-wemm"].instance

        def _cleanup_runtime() -> None:
            if self.rt.plugins["official-visual-wemm"].state.value == "enabled":
                self.rt.disable("official-visual-wemm")
            if self.rt.plugins["official-visual-wemm"].state.value == "disabled":
                self.rt.unload("official-visual-wemm")

        self.addCleanup(_cleanup_runtime)

    def _restore_env(self) -> None:
        if self._fake_backup is None:
            os.environ.pop("RAG_REDO_FAKE_WEMM", None)
        else:
            os.environ["RAG_REDO_FAKE_WEMM"] = self._fake_backup
        if self._skip_backup is None:
            os.environ.pop("RAG_REDO_SKIP_ENV_BOOTSTRAP", None)
        else:
            os.environ["RAG_REDO_SKIP_ENV_BOOTSTRAP"] = self._skip_backup

    def test_an_unreachable_service_stops_the_round_instead_of_failing_every_page(self) -> None:
        """服务不可达时只该试一次，不是每页试一次。"""
        with patch.object(
            self.instance._handle,  # noqa: SLF001 - 复现子进程被拒的宿主视角
            "call",
            side_effect=SubprocessServiceError("connection refused"),
        ) as call_mock:
            self.instance.index_library(
                "lib1", self.vault, ["a.pdf", "b.pdf", "c.pdf"], generation="g1"
            )
        self.assertEqual(
            call_mock.call_count,
            1,
            f"服务不可达后仍发起了 {call_mock.call_count} 次页级调用（3 份 × 2 页 = 6），"
            "会把整库页数乘以失败次数空转，正是 2026-09-29 假活半小时的成因",
        )

    def test_every_remaining_file_becomes_a_retryable_failed_entry(self) -> None:
        """本轮没轮到的文件必须留下失败终态，下轮才会自动重试（不是静默消失）。"""
        with patch.object(
            self.instance._handle,  # noqa: SLF001
            "call",
            side_effect=SubprocessServiceError("connection refused"),
        ):
            self.instance.index_library(
                "lib1", self.vault, ["a.pdf", "b.pdf", "c.pdf"], generation="g1"
            )
        state = self.instance._read_state("lib1", "g1")["files"]  # noqa: SLF001
        self.assertEqual(set(state), {"a.pdf", "b.pdf", "c.pdf"})
        for path, record in state.items():
            self.assertEqual(record["status"], "failed", path)
            self.assertIn("WEMM", str(record["failure_reason"]), path)
            self.assertEqual(record["page_ids"], [], path)

    def test_a_page_that_merely_fails_to_encode_still_moves_to_the_next_page(self) -> None:
        """区分两件事：服务健康、只是这一页编码不出来 → 继续下一页，不熔断。

        这是熔断的边界：把"单页失败"也当成"服务挂了"会让一次手抖毁掉整轮页级索引。
        """
        calls: list[int] = []

        def _first_page_fails_only(method: str, payload: dict, timeout: float = 0.0) -> dict:
            calls.append(1)
            if len(calls) == 1:
                return {"ok": False, "error": "这一页渲染炸了"}
            return {"ok": True, "embedding": [0.0] * WEMM_DIM}

        with patch.object(self.instance._handle, "call", side_effect=_first_page_fails_only):  # noqa: SLF001
            self.instance.index_library("lib1", self.vault, ["a.pdf", "b.pdf"], generation="g1")

        state = self.instance._read_state("lib1", "g1")["files"]  # noqa: SLF001
        # a.pdf 首页失败但第二页成功 → partial；b.pdf 两页都成功 → indexed
        self.assertEqual(state["a.pdf"]["status"], "partial")
        self.assertEqual(state["b.pdf"]["status"], "indexed")
        self.assertEqual(len(state["b.pdf"]["page_ids"]), 2)

    def test_pages_indexed_before_the_outage_are_kept(self) -> None:
        """已经编好的页不能因为后面服务挂了就一起判失败（宁可 partial 不要丢）。"""
        state: list[int] = []

        def _dies_on_the_third_call(method: str, payload: dict, timeout: float = 0.0) -> dict:
            state.append(1)
            if len(state) == 3:
                raise SubprocessServiceError("connection refused")
            return {"ok": True, "embedding": [0.0] * WEMM_DIM}

        with patch.object(self.instance._handle, "call", side_effect=_dies_on_the_third_call):  # noqa: SLF001
            self.instance.index_library("lib1", self.vault, ["a.pdf", "b.pdf"], generation="g1")

        files = self.instance._read_state("lib1", "g1")["files"]  # noqa: SLF001
        self.assertEqual(len(files["a.pdf"]["page_ids"]), 2, "a.pdf 两页都编好了，不该被抹掉")
        self.assertEqual(files["a.pdf"]["status"], "indexed")
        self.assertEqual(files["b.pdf"]["status"], "failed")


class TestVramGateIsReportedTruthfully(unittest.TestCase):
    """显存不足必须**带数字、可强制、且不静默**（2026-09-29 真机事故）。

    真机经过：WEMM 子进程环境装成了 CPU-only torch → 显存探测回退 nvidia-smi
    → WDDM 笔记本上 nvidia-smi 低报约 5.2 GiB → 页级索引一直卡在「空闲显存
    5.4GB < 需求 5.5GB」静默等待 → 用户中断 → 整轮 generation 从未发布。
    修好环境后实测出真实需求是 **6.231 GiB**（加载 5.842 + 编一页 0.404），
    而门槛还写着 5.5 —— 比真实需求低 0.73 GiB，照它放行反而会 OOM。
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._fake_backup = os.environ.get("RAG_REDO_FAKE_WEMM")
        os.environ["RAG_REDO_FAKE_WEMM"] = "1"
        self._skip_backup = os.environ.get("RAG_REDO_SKIP_ENV_BOOTSTRAP")
        os.environ["RAG_REDO_SKIP_ENV_BOOTSTRAP"] = "1"
        self.addCleanup(self._restore_env)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        _make_pdf(self.vault / "a.pdf", ["a 第一页", "a 第二页"])
        _make_pdf(self.vault / "b.pdf", ["b 第一页", "b 第二页"])
        self.rt = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.rt.scan()
        self.rt.load("official-visual-wemm")
        self.rt.enable("official-visual-wemm")
        self.instance = self.rt.plugins["official-visual-wemm"].instance

        def _cleanup_runtime() -> None:
            if self.rt.plugins["official-visual-wemm"].state.value == "enabled":
                self.rt.disable("official-visual-wemm")
            if self.rt.plugins["official-visual-wemm"].state.value == "disabled":
                self.rt.unload("official-visual-wemm")

        self.addCleanup(_cleanup_runtime)

    def _restore_env(self) -> None:
        if self._fake_backup is None:
            os.environ.pop("RAG_REDO_FAKE_WEMM", None)
        else:
            os.environ["RAG_REDO_FAKE_WEMM"] = self._fake_backup
        if self._skip_backup is None:
            os.environ.pop("RAG_REDO_SKIP_ENV_BOOTSTRAP", None)
        else:
            os.environ["RAG_REDO_SKIP_ENV_BOOTSTRAP"] = self._skip_backup

    def test_the_gate_is_calibrated_above_the_measured_need(self):
        """门槛必须高于实测需求 6.231 GiB——照 5.5 放行会 OOM，不是保守是漏算。

        实测（tools/probe_wemm_vram.py，本机 RTX 5060 Laptop / torch 2.11.0+cu128）：
        加载吃 5.842、编一页再吃 0.404，合计 6.231 GiB。旧值 5.5 漏算了
        CUDA 上下文与 cuBLAS 句柄的 0.77 GiB。
        """
        plugin_mod = sys.modules[type(self.instance).__module__]
        server = self._load_server_module()
        self.assertGreaterEqual(
            server.WEMM_MIN_VRAM_GB,
            6.231,
            "门槛低于实测需求 6.231 GiB，会让模型起来后差一截→OOM 或 WDDM 共享内存溢出",
        )
        # 插件与 server 各写一份常量（子进程独立解释器，import 不到插件模块），
        # 但对外显示的数字必须一致，否则用户看到的"需要多少"和真实门槛会打架。
        self.assertEqual(
            plugin_mod.WEMM_MIN_VRAM_GB,
            server.WEMM_MIN_VRAM_GB,
            "插件显示用的门槛与 server 真实门槛不一致，用户会被告知错的数字",
        )

    def test_waiting_for_vram_does_not_outlive_the_host_request(self):
        """等待上限必须远短于宿主的调用超时，否则又是一轮静默假活。

        宿主 `/embed` 的调用超时是 180s（plugin.py），服务端只该等一小段：
        让路是**主动**发生的（/evict 软驱逐、空闲自动卸载，都是秒级），
        等 60s 还不动基本就是"这块卡此刻装不下"，此时快速失败并报数字，
        好过静默耗掉 15 分钟（旧的 900s 就是这么让真机干等半小时的）。
        """
        server = self._load_server_module()
        self.assertLessEqual(
            server.WEMM_VRAM_WAIT_SECONDS,
            180.0,
            "服务端等待上限必须不超过宿主调用超时，否则宿主已放弃、服务还在空等",
        )

    def test_insufficient_vram_names_the_two_numbers(self):
        """异常必须同时带着"需要多少"和"现在多少"，否则上层只能显示一句空话。"""
        server = self._load_server_module()
        exc = server.InsufficientVram(6.3, 5.2)
        self.assertEqual(exc.required_gb, 6.3)
        self.assertEqual(exc.free_gb, 5.2)
        text = str(exc)
        self.assertIn("6.3", text)
        self.assertIn("5.2", text)
        # 探测失败也是一种真实状态，不能假装有数字
        self.assertIsNone(server.InsufficientVram(6.3, None).free_gb)

    def test_status_reports_the_gate_and_the_last_block(self):
        """GUI 要能读到"需要多少 / 上次被挡时有多少 / 是否开了强制"，用不着翻日志。"""
        status = self.instance.status()
        vram = status["vram"]
        self.assertEqual(vram["required_gb"], 6.3)
        self.assertIsNone(vram["blocked"], "刚起来时没被挡过")
        self.assertFalse(vram["force_load"], "默认不强制加载")
        # 没被挡过时不能凭空编数字
        self.instance._vram_blocked = {"required_gb": 6.3, "free_gb": 5.2, "forced": False}
        again = self.instance.status()["vram"]
        self.assertEqual(again["blocked"]["free_gb"], 5.2)
        self.assertFalse(again["blocked"]["forced"])

    def test_force_load_is_off_by_default_and_reads_the_setting_live(self):
        """默认关；开设置立刻生效（长驻进程里用户中途改设置必须立刻管用）。"""
        self.assertFalse(self.instance.force_load_enabled())
        self.rt.settings.set("wemm_force_load", True)
        self.assertTrue(self.instance.force_load_enabled())
        self.rt.settings.set("wemm_force_load", False)
        self.assertFalse(self.instance.force_load_enabled())

    def test_a_vram_block_is_remembered_with_its_numbers(self):
        """服务端回 reason=vram 时，插件要把数字记下来给 GUI，不能只丢一句 error。"""
        self.instance._handle.call(  # noqa: SLF001
            "embed",
            {"kind": "image", "content": "x", "dim": 512},
            timeout=5.0,
        )
        # 让服务回显存不足
        with patch.object(
            self.instance._handle,  # noqa: SLF001
            "call",
            return_value={
                "ok": False,
                "reason": "vram",
                "required_gb": 6.3,
                "free_gb": 5.2,
                "error": "显卡内存不足",
            },
        ):
            self.instance.index_library("lib1", self.vault, ["a.pdf"], generation="g1")
        self.assertIsNotNone(self.instance._vram_blocked, "显存被挡下却没有记录")
        self.assertEqual(self.instance._vram_blocked["required_gb"], 6.3)
        self.assertEqual(self.instance._vram_blocked["free_gb"], 5.2)
        # 文字索引的页级条目仍要留下可重试终态，下一轮才会自动重试
        state = self.instance._read_state("lib1", "g1")["files"]  # noqa: SLF001
        self.assertEqual(state["a.pdf"]["status"], "failed")

    def test_page_level_progress_is_reported_so_the_ui_is_not_frozen(self) -> None:
        """页级进度必须上报（2026-09-29 真机：编 7645 页的 30 分钟界面一动不动）。

        这一段的口径是文字索引的 `files_done/files_total`，那两个数在进视觉阶段前就
        已经是 78/78、100%，所以界面上一个数都不会变——用户看到的就是"卡死"。
        旧项目有页级进度回调（`wemm_indexer.py` 问题47），移植时漏了，这里补回来。

        断言三件事：页总数数得对、编过的页会累加上报、上报回调炸了不许影响页级索引。
        """
        seen: list[tuple[int, int, str]] = []

        def _progress(pages_done: int, pages_total: int, current_path: str = "") -> None:
            seen.append((pages_done, pages_total, current_path))

        self.instance.index_library(
            "lib1", self.vault, ["a.pdf", "b.pdf"], generation="g1", progress=_progress
        )
        self.assertTrue(seen, "一次都没上报——界面会完全看不到这一段在推进")
        # 两个 PDF 各 2 页 = 4 页总数
        self.assertEqual(seen[-1][1], 4, f"页总数数错了：{seen[-1]}")
        # 已编页数是累计值且不倒退，末值应等于成功编码的页数
        done_values = [s[0] for s in seen]
        self.assertEqual(done_values, sorted(done_values), f"累计值倒退了：{done_values}")
        self.assertGreater(done_values[-1], 0, "跑完了却一页都没计入")
        # 当前文件名要带上，用户才知道在编哪一份
        self.assertTrue(any(s[2] for s in seen), "上报里没有当前文件名")

    def test_a_broken_progress_callback_never_breaks_page_indexing(self) -> None:
        """进度上报是锦上添花：回调抛异常只该丢掉这次上报，不该带崩页级索引。"""
        def _boom(*a, **kw):
            raise RuntimeError("进度回调坏了")

        self.instance.index_library(
            "lib1", self.vault, ["a.pdf"], generation="g1", progress=_boom
        )
        state = self.instance._read_state("lib1", "g1")["files"]  # noqa: SLF001
        self.assertEqual(state["a.pdf"]["status"], "indexed")

    def test_omitting_progress_still_works(self) -> None:
        """不传 progress（老插件/老调用方）不许崩——它是可选参数。"""
        self.instance.index_library("lib1", self.vault, ["a.pdf"], generation="g1")
        state = self.instance._read_state("lib1", "g1")["files"]  # noqa: SLF001
        self.assertEqual(state["a.pdf"]["status"], "indexed")

    def test_progress_is_not_written_once_per_page(self) -> None:
        """上报必须按时间节流：逐页写进度文件会让磁盘 IO 变成瓶颈。

        实测吞吐 12~15 页/秒，即每秒 12~15 次写盘；不节流的话光写进度就比编码还忙。
        """
        seen: list[tuple[int, int, str]] = []
        self.instance.index_library(
            "lib1", self.vault, ["a.pdf"], generation="g1",
            progress=lambda d, t, p="": seen.append((d, t, p)),
        )
        # a.pdf 只有 2 页，一次 _report(force=True) 就够；有 2 次以上说明没节流到位
        self.assertLessEqual(len(seen), 2, f"2 页却上报了 {len(seen)} 次，没节流")

    @staticmethod
    def _load_server_module():
        import importlib.util

        path = (
            REPO_ROOT / "plugins" / "official-visual-wemm"
            / "official_visual_wemm" / "server.py"
        )
        spec = importlib.util.spec_from_file_location("wemm_server_vram_gate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


if __name__ == "__main__":
    unittest.main()
