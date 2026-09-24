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

from core.runtime import PluginRuntime, PluginState  # noqa: E402


def _process_is_gone(pid: int) -> bool:
    """跨平台的"这个 pid 是不是真的没了"检查，理由同
    tests/test_runtime.py 里同名函数——POSIX 的 os.kill(pid, 0) 信号-0
    探测语义在 Windows 上不成立（直接抛 OSError 而不是
    ProcessLookupError），得走 Win32 OpenProcess API。"""
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


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


if __name__ == "__main__":
    unittest.main()
