from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from unittest.mock import patch

from core import atomic
from core.index_progress import IndexProgress, IndexWorkerManager, StatusUnreadableError, _read_json
from core.singleton import pid_alive


PLUGIN_SOURCE = textwrap.dedent(
    """
    from __future__ import annotations

    import json
    import os
    import sys
    import time
    from pathlib import Path
    from types import SimpleNamespace

    from core.contracts import Chunk, EmbeddingVector, ExtractedDocument


    class TestPlugin:
        def on_load(self, ctx):
            self.store = self
            self._data_dir = ctx.storage.directory("worker", legacy=".")
            roots = json.loads((self._data_dir / "worker_roots.json").read_text(encoding="utf-8"))
            self.roots = roots
            self.mode = (self._data_dir / "worker_mode.txt").read_text(encoding="utf-8").strip()
            (self._data_dir / "worker_utf8.txt").write_text(str(sys.flags.utf8_mode), encoding="utf-8")
            with (self._data_dir / "worker_loads.txt").open("a", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}" + chr(10))  # 插件（真机上 = 模型）在哪个进程里加载了几次
            if self.mode == "early_crash":
                os._exit(24)
            if self.mode == "foreign":
                time.sleep(2.0)

        def get(self, library_id):
            root = self.roots.get(library_id)
            return SimpleNamespace(root_path=root) if root is not None else None

        def resolve_included_files(self, library_id, *, format_allowlist=None):
            cfg = self.get(library_id)
            if cfg is None:
                raise KeyError(library_id)
            (self.store._data_dir / "worker_allowlist.json").write_text(
                json.dumps(format_allowlist),
                encoding="utf-8",
            )
            root = Path(cfg.root_path)
            return [
                (str(path.relative_to(root)).replace("\\\\", "/"), True, "test")
                for path in sorted(root.glob("*.md"))
            ]

        def extract(self, library_id, path, root):
            try:
                text = (Path(root) / path).read_text(encoding="utf-8")
            except OSError as exc:
                return ExtractedDocument(
                    library_id=library_id,
                    path=path,
                    text=None,
                    failure_reason=str(exc),
                    extracted_by="test",
                    extractor_version="1",
                    content_hash="",
                )
            return ExtractedDocument(
                library_id=library_id,
                path=path,
                text=text,
                failure_reason=None,
                extracted_by="test",
                extractor_version="1",
                content_hash="test",
            )

        def chunk(self, doc):
            return [
                Chunk(
                    chunk_id=f"{doc.library_id}:{doc.path}:0",
                    library_id=doc.library_id,
                    path=doc.path,
                    chunk_index=0,
                    total_chunks=1,
                    text=doc.text,
                    heading_breadcrumb="",
                    chunked_by="test",
                    chunker_version="1",
                )
            ]

        def embed_chunks(self, chunks):
            if self.mode == "long":
                time.sleep(2.0)
            if self.mode == "crash":
                os._exit(23)
            if self.mode == "fail":
                raise RuntimeError("worker exploded")
            if self.mode == "fail_lib1" and chunks and chunks[0].library_id == "lib1":
                raise RuntimeError("lib1 exploded")
            if self.mode == "crash_lib1" and chunks and chunks[0].library_id == "lib1":
                os._exit(23)
            if self.mode == "long_lib1" and chunks and chunks[0].library_id == "lib1":
                time.sleep(2.0)
            return [
                EmbeddingVector(
                    chunk_id=chunk.chunk_id,
                    vector=(1.0,),
                    model_id="test",
                    model_version="1",
                    dim=1,
                )
                for chunk in chunks
            ]

        def index_chunk(self, chunk, generation=None):
            return None

        def upsert(self, library_id, chunk_ids, vectors, documents=None, metadatas=None, generation=None):
            return None
    """
).strip()


OPTIONAL_BROKEN_TOML = """
id = "optional-broken-plugin"
name = "Optional broken plugin"
version = "0.1.0"
api_version = ">=0.1,<0.2"

[provides]
optional_feature = "multi"

[requires]

[runtime]
kind = "in_process"
entry = "broken_plugin:BrokenPlugin"

[permissions]
filesystem = []
network = false
gpu = false
"""


PLUGIN_TOML = """
id = "test-index-plugin"
name = "Index worker test plugin"
version = "0.1.0"
api_version = ">=0.1,<0.2"

[provides]
library_manager = "singleton"
chunker = "singleton"
embedder = "singleton"
lexical_index = "singleton"
vector_store = "singleton"
"extractor:md" = "multi"

[requires]

[runtime]
kind = "in_process"
entry = "index_test_plugin:TestPlugin"

[permissions]
filesystem = ["vault_read", "data_write"]
network = false
gpu = false
"""


def _wait_until(predicate, timeout_s: float = 12.0, poll_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return bool(predicate())


class TestIndexWorkerManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_dir = self.tmp / "data"
        self.plugin_dir = self.tmp / "plugins"
        plugin_path = self.plugin_dir / "test-index-plugin"
        plugin_path.mkdir(parents=True)
        self.data_dir.mkdir()
        self.lib1 = self.tmp / "lib1"
        self.lib2 = self.tmp / "lib2"
        self.lib1.mkdir()
        self.lib2.mkdir()
        (self.lib1 / "one.md").write_text("alpha", encoding="utf-8")
        (self.lib2 / "two.md").write_text("beta", encoding="utf-8")
        (self.data_dir / "worker_roots.json").write_text(
            json.dumps({"lib1": str(self.lib1), "lib2": str(self.lib2)}),
            encoding="utf-8",
        )
        self.mode_path = self.data_dir / "worker_mode.txt"
        self._set_mode("done")
        (plugin_path / "plugin.toml").write_text(PLUGIN_TOML, encoding="utf-8")
        (plugin_path / "index_test_plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
        broken_path = self.plugin_dir / "optional-broken-plugin"
        broken_path.mkdir()
        (broken_path / "plugin.toml").write_text(OPTIONAL_BROKEN_TOML, encoding="utf-8")
        (broken_path / "broken_plugin.py").write_text(
            "class BrokenPlugin:\n    def on_load(self, ctx):\n        raise RuntimeError('optional broken')\n",
            encoding="utf-8",
        )
        self.manager = IndexWorkerManager(
            self.plugin_dir,
            self.data_dir,
            ["test-index-plugin", "optional-broken-plugin"],
            heartbeat_interval=0.05,
            heartbeat_timeout=0.3,
            stall_timeout=0.12,
            ack_timeout=10.0,
        )
        self.addCleanup(self.manager.shutdown)

    def _set_mode(self, mode: str) -> None:
        self.mode_path.write_text(mode, encoding="utf-8")

    def _wait_stage(self, library_id: str, stage: str, timeout_s: float = 12.0) -> dict:
        self.assertTrue(
            _wait_until(
                lambda: (self.manager.status(library_id) or {}).get("stage") == stage,
                timeout_s,
            ),
            self.manager.status(library_id),
        )
        return self.manager.status(library_id) or {}

    def _manual_progress(
        self,
        *,
        heartbeat_at: float,
        progress_at: float,
        worker_pid: int | None = None,
        launcher_pid: int | None = None,
        grace_until: float | None = None,
    ) -> IndexProgress:
        now = time.time()
        return IndexProgress(
            library_id="manual",
            run_id="manual-run",
            source="test",
            full=False,
            launcher_pid=os.getpid() if launcher_pid is None else launcher_pid,
            worker_pid=os.getpid() if worker_pid is None else worker_pid,
            stage="running",
            phase="embedding",
            files_done=1,
            files_total=2,
            chunks_done=1,
            chunks_total=None,
            started_at=now - 10,
            heartbeat_at=heartbeat_at,
            progress_at=progress_at,
            stall_grace_until=grace_until,
        )

    def test_format_allowlist_crosses_process_boundary(self):
        result = self.manager.start(
            "lib1",
            "test",
            format_allowlist=(".md", ".txt"),
        )
        self.assertTrue(result.started, result.message)
        self.assertEqual(self._wait_stage("lib1", "done")["stage"], "done")
        allowlist = json.loads(
            (self.data_dir / "worker_allowlist.json").read_text(encoding="utf-8")
        )
        self.assertEqual(allowlist, [".md", ".txt"])

    def test_real_worker_done_and_library_can_reopen(self):
        result = self.manager.start("lib1", source="test")
        started, message = result
        self.assertTrue(started, message)
        self.assertTrue(result.run_id)
        self.assertTrue(result.worker_pid)
        status = self._wait_stage("lib1", "done")
        self.assertEqual(status["succeeded"], 1)
        self.assertEqual(status["failed"], 0)
        self.assertEqual(status["chunks_total"], 1)
        self.assertEqual(status["percent"], 100.0)
        self.assertEqual(status["eta_s"], 0.0)
        self.assertEqual(status["owner"], "self")
        self.assertFalse(status["active"])
        self.assertTrue(_wait_until(lambda: not pid_alive(result.worker_pid), timeout_s=5.0))
        second = self.manager.start("lib1")
        self.assertTrue(second.started, second.message)
        self._wait_stage("lib1", "done")
        second_status = self.manager.status("lib1") or {}
        self.assertEqual(second_status.get("unchanged"), 1)
        self.assertEqual(second_status.get("added"), 0)

    def test_same_library_second_worker_is_rejected(self):
        self._set_mode("long")
        first = self.manager.start("lib1")
        self.assertTrue(first.started, first.message)
        second = self.manager.start("lib1")
        started, message = second
        self.assertFalse(started)
        self.assertEqual(second.run_id, "")
        self.assertIsNone(second.worker_pid)
        self.assertIn("已经有一个索引任务在跑", message)
        self._wait_stage("lib1", "done")

    def test_different_libraries_run_in_parallel(self):
        # 只说 `start()` 本身：各自独立的单库调用（如 MCP 的 reindex_knowledge）互不排队。
        # "一次点击重建多个库"走 `start_batch`，那条是串行的（见下面的批次测试）。
        self._set_mode("long")
        first = self.manager.start("lib1")
        second = self.manager.start("lib2")
        self.assertTrue(first.started, first.message)
        self.assertTrue(second.started, second.message)
        self.assertNotEqual(first.worker_pid, second.worker_pid)
        self.assertTrue(pid_alive(first.worker_pid))
        self.assertTrue(pid_alive(second.worker_pid))
        self._wait_stage("lib1", "done")
        self._wait_stage("lib2", "done")

    def test_heartbeat_advances_during_embedding_and_grace_prevents_stall(self):
        self._set_mode("long")
        result = self.manager.start("lib1")
        self.assertTrue(result.started, result.message)
        self.assertTrue(
            _wait_until(
                lambda: (self.manager.status("lib1") or {}).get("phase") == "embedding",
                5.0,
            )
        )
        before = self.manager.status("lib1") or {}
        time.sleep(0.2)
        after = self.manager.status("lib1") or {}
        self.assertGreater(after["heartbeat_at"], before["heartbeat_at"])
        self.assertEqual(after["progress_at"], before["progress_at"])
        self.assertIsNotNone(after["stall_grace_until"])
        self.assertEqual(after["health"], "healthy")
        self._wait_stage("lib1", "done")

    def test_embedding_phase_percent_counts_chunks_and_gives_no_file_based_eta(self):
        """索引先全部转换切块、再连续向量化：向量化阶段文件数已走满，percent 只能按块数算
        （旧 gui/store.py::progress_ratio），按文件外推的 eta 没有意义。"""
        now = time.time()
        progress = self._manual_progress(heartbeat_at=now, progress_at=now)
        progress.phase = "embedding"
        progress.files_done = 12
        progress.files_total = 12
        progress.chunks_done = 30
        progress.chunks_total = 120
        self.assertTrue(self.manager._write(progress))
        status = self.manager.status("manual")
        self.assertAlmostEqual(status["percent"], 25.0)
        self.assertIsNone(status["eta_s"])

        progress.phase = "extracting"
        progress.files_done = 3
        progress.files_total = 12
        progress.chunks_done = 0
        progress.chunks_total = None
        self.assertTrue(self.manager._write(progress))
        status = self.manager.status("manual")
        self.assertAlmostEqual(status["percent"], 25.0)
        self.assertIsNotNone(status["eta_s"])

    def test_manual_progress_without_grace_is_stalled(self):
        now = time.time()
        self.assertTrue(self.manager._write(self._manual_progress(heartbeat_at=now, progress_at=now - 5)))
        status = self.manager.status("manual")
        self.assertEqual(status["health"], "stalled_no_progress")

    def test_heartbeat_timeout_has_priority_over_progress_health(self):
        now = time.time()
        self.assertTrue(
            self.manager._write(
                self._manual_progress(heartbeat_at=now - 5, progress_at=now)
            )
        )
        status = self.manager.status("manual")
        self.assertEqual(status["health"], "stalled_no_heartbeat")

    def test_dead_worker_pid_is_orphaned_before_timeout_checks(self):
        now = time.time()
        self.assertTrue(
            self.manager._write(
                self._manual_progress(
                    heartbeat_at=now - 5,
                    progress_at=now - 5,
                    worker_pid=99999999,
                )
            )
        )
        status = self.manager.status("manual")
        self.assertEqual(status["health"], "orphaned")
        self.assertFalse(status["active"])

    def test_post_ack_worker_crash_is_reaped_as_failed_and_can_reopen(self):
        self._set_mode("crash")
        result = self.manager.start("lib1")
        self.assertTrue(result.started, result.message)
        status = self._wait_stage("lib1", "failed")
        self.assertIn("异常退出", status["error"])
        self._set_mode("done")
        reopened = self.manager.start("lib1")
        self.assertTrue(reopened.started, reopened.message)
        self._wait_stage("lib1", "done")

    def test_worker_exception_writes_failed_and_releases_lock(self):
        self._set_mode("fail")
        result = self.manager.start("lib1")
        self.assertTrue(result.started, result.message)
        status = self._wait_stage("lib1", "failed")
        self.assertIn("worker exploded", status["error"])
        time.sleep(0.1)
        self._set_mode("done")
        reopened = self.manager.start("lib1")
        self.assertTrue(reopened.started, reopened.message)
        self._wait_stage("lib1", "done")

    def test_self_stop_writes_cancelled_kills_worker_and_reopens(self):
        self._set_mode("long")
        result = self.manager.start("lib1")
        self.assertTrue(result.started, result.message)
        self.assertTrue(
            _wait_until(
                lambda: (self.manager.status("lib1") or {}).get("stage") == "running",
                5.0,
            )
        )
        stopped, message = self.manager.stop("lib1", result.run_id)
        self.assertTrue(stopped, message)
        status = self._wait_stage("lib1", "cancelled")
        self.assertEqual(status["run_id"], result.run_id)
        self.assertFalse(pid_alive(result.worker_pid))
        self._set_mode("done")
        reopened = self.manager.start("lib1")
        self.assertTrue(reopened.started, reopened.message)
        self._wait_stage("lib1", "done")

    def test_foreign_stop_is_rejected(self):
        self._set_mode("foreign")
        self.manager._heartbeat_interval = 10.0
        result = self.manager.start("lib1")
        self.assertTrue(result.started, result.message)
        status = self.manager.status("lib1") or {}
        status["launcher_pid"] = os.getpid() + 1
        self.assertTrue(self.manager._write(status))
        stopped, message = self.manager.stop("lib1", result.run_id)
        self.assertFalse(stopped)
        self.assertIn("其他启动进程", message)
        self.manager.shutdown()
        self.assertFalse(pid_alive(result.worker_pid))

    def test_unknown_and_corrupt_status_are_none(self):
        self.assertIsNone(self.manager.status("unknown"))
        status_path = self.manager._status_path("corrupt")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(self.manager.status("corrupt"))

    # ---- 一次重建多个库：依次串行（旧项目 index.py `__main__` 的逐库循环）--------
    #
    # 此前 GUI 对每个库各起一个 worker，4 个库 = 4 份模型同时占显卡/CPU（2026-09-29
    # 真机复现）。`start_batch`：第一个立刻起，其余排队，前一个跑完才轮到下一个。
    # 2026-09-30 起（操作者确认，BC-15）整批在**同一个** worker 进程里依次跑，模型只加载
    # 一次——与旧项目"一次点击一个索引进程、按库循环"一致；某个库失败或进程崩了，
    # 后面的库换一个新进程接着跑。

    def _loads(self) -> list[str]:
        path = self.data_dir / "worker_loads.txt"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

    def _batch_idle(self, timeout_s: float = 8.0) -> bool:
        return _wait_until(lambda: not self.manager._batches, timeout_s)

    def test_batch_runs_libraries_one_after_another_never_together(self):
        self._set_mode("long_lib1")
        result = self.manager.start_batch(["lib1", "lib2"], source="test")
        self.assertTrue(result.started, result.message)
        self.assertTrue(result.run_id)
        # lib1 在跑（要 2 秒），lib2 还在排队：没有 worker、没有进度文件，读模型合成"排队中"
        self.assertTrue(
            _wait_until(lambda: (self.manager.status("lib1") or {}).get("stage") == "running", 5.0)
        )
        queued = self.manager.status("lib2") or {}
        self.assertEqual((queued.get("stage"), queued.get("phase")), ("starting", "queued"))
        self.assertTrue(queued["active"])
        self.assertTrue(queued["can_stop"])
        self.assertEqual(queued["owner"], "self")
        self.assertEqual(queued["run_id"], "")
        self.assertIn("排队", queued["message"])
        self.assertIsNone(_read_json(self.manager._status_path("lib2")), "排队的库不该已经有 worker 的进度文件")
        first = self._wait_stage("lib1", "done")
        second = self._wait_stage("lib2", "done")
        self.assertGreaterEqual(
            second["started_at"], first["finished_at"], "lib2 必须在 lib1 结束之后才开始，不能同时跑"
        )
        self.assertEqual(first["worker_pid"], second["worker_pid"], "整批在同一个 worker 进程里跑")
        self.assertTrue(self._batch_idle(), "整批结束后批次线程应退出、队列清空")
        self.assertTrue(_wait_until(lambda: not pid_alive(first["worker_pid"]), 8.0), "整批跑完 worker 要退出")

    def test_batch_loads_plugins_and_models_only_once(self):
        """真机：每个库各起一个进程时，每个库都要重新载入深度学习库、重新加载模型（每次 10~35 秒）。"""
        result = self.manager.start_batch(["lib1", "lib2"], source="test")
        self.assertTrue(result.started, result.message)
        self._wait_stage("lib1", "done")
        self._wait_stage("lib2", "done")
        self.assertTrue(self._batch_idle())
        self.assertEqual(len(self._loads()), 1, f"整批只加载一次插件：{self._loads()}")

    def test_after_a_worker_crash_the_rest_of_the_batch_runs_in_a_new_process(self):
        self._set_mode("crash_lib1")
        result = self.manager.start_batch(["lib1", "lib2"], source="test")
        self.assertTrue(result.started, result.message)
        crashed = self._wait_stage("lib1", "failed")
        self.assertIn("异常退出", crashed["message"])
        done = self._wait_stage("lib2", "done")
        self.assertNotEqual(crashed["worker_pid"], done["worker_pid"], "崩了的进程不能再用，后面的库换新进程")
        self.assertEqual(done["succeeded"], 1)
        self.assertTrue(self._batch_idle())

    def test_batch_continues_with_the_next_library_after_one_fails(self):
        self._set_mode("fail_lib1")
        result = self.manager.start_batch(["lib1", "lib2"], source="test")
        self.assertTrue(result.started, result.message)
        failed = self._wait_stage("lib1", "failed")
        self.assertIn("lib1 exploded", failed["error"])
        done = self._wait_stage("lib2", "done")  # 旧项目：索引失败（继续下一库）
        self.assertEqual(done["succeeded"], 1)
        self.assertNotEqual(failed["worker_pid"], done["worker_pid"], "出过错的进程状态不可信，后面的库换新进程")

    def test_stopping_the_running_library_cancels_the_rest_of_the_batch(self):
        self._set_mode("long_lib1")
        result = self.manager.start_batch(["lib1", "lib2"], source="test")
        self.assertTrue(result.started, result.message)
        self.assertTrue(
            _wait_until(lambda: (self.manager.status("lib1") or {}).get("stage") == "running", 5.0)
        )
        stopped, message = self.manager.stop("lib1", result.run_id)
        self.assertTrue(stopped, message)
        self._wait_stage("lib1", "cancelled")
        self.assertTrue(self._batch_idle())
        time.sleep(1.0)  # 给"错误地把下一个库起起来"留出足够时间
        self.assertIsNone(self.manager.status("lib2"), "停止 = 整批取消，lib2 不许被起起来")
        self.assertFalse(self.manager._status_path("lib2").exists())

    def test_stopping_a_queued_library_only_removes_it_from_the_queue(self):
        self._set_mode("long_lib1")
        result = self.manager.start_batch(["lib1", "lib2"], source="test")
        self.assertTrue(result.started, result.message)
        stopped, message = self.manager.stop("lib2")
        self.assertTrue(stopped, message)
        self.assertIn("队列", message)
        self.assertIsNone(self.manager.status("lib2"))
        self._wait_stage("lib1", "done")  # 在跑的那个不受影响
        self.assertTrue(self._batch_idle())
        time.sleep(0.5)
        self.assertFalse(self.manager._status_path("lib2").exists(), "被取消的库不许再被起起来")

    def test_a_queued_library_cannot_be_started_or_queued_again(self):
        self._set_mode("long_lib1")
        result = self.manager.start_batch(["lib1", "lib2"], source="test")
        self.assertTrue(result.started, result.message)
        again = self.manager.start("lib2")
        self.assertFalse(again.started)
        self.assertIn("排队", again.message)
        batch_again = self.manager.start_batch(["lib2"], source="test")
        self.assertFalse(batch_again.started)
        self.assertIn("排队", batch_again.message)
        self._wait_stage("lib2", "done")

    def test_batch_whose_first_library_cannot_start_does_not_begin(self):
        self._set_mode("long")
        first = self.manager.start("lib1")
        self.assertTrue(first.started, first.message)
        refused = self.manager.start_batch(["lib1", "lib2"], source="test")
        self.assertFalse(refused.started)
        self.assertIn("已经有一个索引任务在跑", refused.message)
        self.assertFalse(self.manager._batches, "起不来的批次不能留下幽灵队列")
        self.assertIsNone(self.manager.status("lib2"))
        self._wait_stage("lib1", "done")

    def test_single_library_batch_is_just_start(self):
        result = self.manager.start_batch(["lib1"], source="test")
        self.assertTrue(result.started, result.message)
        self.assertFalse(self.manager._batches)
        self._wait_stage("lib1", "done")

    def test_shutdown_drops_the_queue(self):
        self._set_mode("long_lib1")
        result = self.manager.start_batch(["lib1", "lib2"], source="test")
        self.assertTrue(result.started, result.message)
        self.manager.shutdown()
        self.assertIsNone(self.manager.status("lib2"))
        self.assertTrue(self._batch_idle())
        time.sleep(0.5)
        self.assertFalse(self.manager._status_path("lib2").exists())

    # ---- worker 输出与崩溃留痕 ------------------------------------------------

    def test_worker_starts_in_utf8_mode_and_the_launcher_environment_is_left_alone(self):
        """worker 以 UTF-8 模式启动（默认编码也是 UTF-8，不只是标准流）；起完进程后发起方环境还原。"""
        before = os.environ.get("PYTHONUTF8")
        result = self.manager.start("lib1", source="test")
        self.assertTrue(result.started, result.message)
        self._wait_stage("lib1", "done")
        marker = (self.data_dir / "worker_utf8.txt").read_text(encoding="utf-8")
        self.assertEqual(marker, "1", "worker 进程应在 UTF-8 模式下启动")
        self.assertEqual(os.environ.get("PYTHONUTF8"), before, "起进程只是临时设置，不能污染发起进程的环境")

    def _read_log(self) -> str:
        path = self.data_dir / "index_worker.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def test_worker_failure_leaves_a_library_line_and_the_traceback_in_the_log(self):
        """崩了不能只剩进度文件里的一句摘要：日志里要有"哪个库、什么错"和完整堆栈。"""
        self._set_mode("fail")
        result = self.manager.start("lib1")
        self.assertTrue(result.started, result.message)
        self._wait_stage("lib1", "failed")
        self.assertTrue(
            _wait_until(lambda: "Traceback" in self._read_log(), 5.0), self._read_log()
        )
        log = self._read_log()
        self.assertIn("[lib1] 索引失败：RuntimeError: worker exploded", log)
        self.assertIn('raise RuntimeError("worker exploded")', log)

    # ---- 读侧瞬时失败（审计 M-1：停止按钮间歇性被拒）------------------------
    #
    # 根因：Windows 上读者恰好撞上另一方的 `os.replace`，会得到瞬时的
    # `PermissionError`；旧 `_read_json` 吞掉全部 OSError，把它当成"没有状态"，
    # `stop()` 于是回"没有可停止的索引任务"。下面用**注入的**瞬时失败做确定性验证，
    # 不靠碰运气跑几十遍。

    def _write_running_status(self, library_id: str = "lib1") -> Path:
        path = self.manager._status_path(library_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        progress = self._manual_progress(heartbeat_at=time.time(), progress_at=time.time())
        progress.library_id = library_id
        path.write_text(json.dumps(progress.__dict__), encoding="utf-8")
        return path

    def test_read_json_retries_transient_permission_errors(self):
        path = self._write_running_status()
        real_read_text = Path.read_text
        attempts = {"n": 0}

        def flaky(self_path, *args, **kwargs):
            if self_path == path and attempts["n"] < 3:
                attempts["n"] += 1
                raise PermissionError(13, "Access is denied")  # 与 os.replace 撞车的瞬间
            return real_read_text(self_path, *args, **kwargs)

        with patch.object(Path, "read_text", flaky):
            data = _read_json(path)
        self.assertIsNotNone(data, "瞬时的 PermissionError 必须被重试掉，而不是当成'没有状态'")
        self.assertEqual(data["run_id"], "manual-run")
        self.assertEqual(attempts["n"], 3)

    def test_read_json_missing_file_is_none_immediately_without_retrying(self):
        path = self.data_dir / "nope" / "missing.json"
        started = time.monotonic()
        with patch.object(atomic.time, "sleep") as sleep:
            self.assertIsNone(_read_json(path))
            self.assertIsNone(_read_json(path, strict=True))
        sleep.assert_not_called()
        self.assertLess(time.monotonic() - started, 0.5)

    def test_read_json_persistent_failure_is_none_when_lenient_and_raises_when_strict(self):
        path = self._write_running_status()

        def always_denied(self_path, *args, **kwargs):
            raise PermissionError(13, "Access is denied")

        with patch.object(atomic, "_REPLACE_RETRY_DELAYS_S", (0.0, 0.0)), \
                patch.object(Path, "read_text", always_denied):
            self.assertIsNone(_read_json(path))
            with self.assertRaises(StatusUnreadableError):
                _read_json(path, strict=True)

    def test_non_busy_os_errors_are_not_retried(self):
        path = self._write_running_status()
        calls = {"n": 0}

        def broken_disk(self_path, *args, **kwargs):
            calls["n"] += 1
            raise OSError(5, "I/O error")  # 不是句柄占用：重试没有意义

        with patch.object(Path, "read_text", broken_disk):
            self.assertIsNone(_read_json(path))
        self.assertEqual(calls["n"], 1)

    def test_stop_does_not_call_an_unreadable_status_no_task_or_changed(self):
        """读不出来 ≠ 没有任务 ≠ 状态已变化：`stop()` 要把真实情况说出来。"""
        self._write_running_status("lib1")

        def always_denied(self_path, *args, **kwargs):
            raise PermissionError(13, "Access is denied")

        with patch.object(atomic, "_REPLACE_RETRY_DELAYS_S", (0.0, 0.0)), \
                patch.object(Path, "read_text", always_denied):
            stopped, message = self.manager.stop("lib1", "manual-run")
        self.assertFalse(stopped)
        self.assertIn("无法读取索引进度状态", message)
        self.assertNotIn("没有可停止的索引任务", message)
        self.assertNotIn("状态已经变化", message)
        # 真的没有状态文件时，仍然是原来的说法
        stopped, message = self.manager.stop("never-started", "x")
        self.assertFalse(stopped)
        self.assertIn("没有可停止的索引任务", message)


class TestWorkerOutputEncoding(unittest.TestCase):
    """worker 输出必须是 UTF-8：中文写不进系统编码（Windows cp1252）时不许把整轮索引带崩。

    2026-09-29 真机：4 个库的后台索引全都在中途崩于
    `UnicodeEncodeError: 'charmap' codec can't encode characters`，用户看到"没转成向量，
    也没报错"。子进程里 stdout 按系统区域编码严格编码，中文一写就抛。
    """

    def _run(self, code: str, *args: str) -> "subprocess.CompletedProcess[bytes]":
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONUTF8", "PYTHONIOENCODING"}}
        env["PYTHONIOENCODING"] = "cp1252"  # 复现系统区域编码（Windows 中文版之外的默认）
        return subprocess.run(
            [sys.executable, "-c", code, *args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            timeout=60,
        )

    def test_control_plain_print_of_chinese_crashes_under_cp1252(self):
        """对照：不做处理时，同样的环境里 print 中文确实会崩——证明下面的测试有意义。"""
        proc = self._run("print('中文输出')")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"UnicodeEncodeError", proc.stderr)

    def test_redirect_output_makes_chinese_print_and_stderr_safe_and_utf8_on_disk(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        log = tmp / "worker.log"
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from pathlib import Path\n"
            "from core.index_progress import _redirect_output\n"
            "_redirect_output(Path(sys.argv[1]))\n"
            "print('中文输出：模型已加载')\n"
            "print('emoji 😀 也不许崩')\n"
            "sys.stderr.write('错误：堆栈\\n')\n"
        )
        proc = self._run(code, str(log))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        text = log.read_text(encoding="utf-8")
        self.assertIn("中文输出：模型已加载", text)
        self.assertIn("错误：堆栈", text)
        self.assertIn("emoji", text)


if __name__ == "__main__":
    unittest.main()
