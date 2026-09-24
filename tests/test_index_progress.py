from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.index_progress import IndexProgress, IndexWorkerManager
from core.singleton import pid_alive


PLUGIN_SOURCE = textwrap.dedent(
    """
    from __future__ import annotations

    import json
    import os
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


if __name__ == "__main__":
    unittest.main()
