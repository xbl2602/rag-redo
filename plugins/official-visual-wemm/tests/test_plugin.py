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


if __name__ == "__main__":
    unittest.main()
