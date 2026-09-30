"""单测注入假 reranker，不碰真实模型——理由同 official-embedder-bge-m3
的测试文件（含"为什么用 mock.patch.dict 而不是依赖开发机没装依赖"的
真实踩坑记录，这里不重复展开）。"""
from __future__ import annotations

import collections
import logging
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.resource_arbiter import ResourceArbiter  # noqa: E402
from official_reranker.plugin import RerankerPlugin  # noqa: E402
from official_reranker.rerank import (  # noqa: E402
    GPU_HOLDER_ID,
    GPU_RESOURCE_ID,
    RerankerEngine,
    RerankUnavailable,
    _RealReranker,
)


class _FakeReranker:
    """假打分：谁包含查询词，谁分高——足够验证排序逻辑，不需要真模型。"""

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [1.0 if query in t else 0.0 for t in texts]


class TestRerankerEngineWithFake(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        engine = RerankerEngine(reranker=_FakeReranker())
        self.assertEqual(engine.rerank("q", []), [])

    def test_matching_text_ranks_first(self):
        engine = RerankerEngine(reranker=_FakeReranker())
        results = engine.rerank(
            "插件",
            [("c1", "这段话和天气无关"), ("c2", "这段话提到了插件系统")],
        )
        self.assertEqual(results[0][0], "c2")

    def test_top_k_limits_results(self):
        engine = RerankerEngine(reranker=_FakeReranker())
        pairs = [(f"c{i}", "插件") for i in range(5)]
        results = engine.rerank("插件", pairs, top_k=2)
        self.assertEqual(len(results), 2)


class TestRealRerankerLazyLoading(unittest.TestCase):
    def test_construction_does_not_touch_model(self):
        reranker = _RealReranker()
        self.assertIsNone(reranker._model)

    def test_score_without_dependency_raises_clear_import_error(self):
        reranker = _RealReranker()
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            with self.assertRaises(ImportError):
                reranker.score("q", ["a", "b"])


class _StubGateReady:
    """冷却门替身：始终 ready——真实 CudaCooldownGate.probe 在无 GPU 的测试机
    上必然失败（64MB CUDA 分配），需要 CUDA 分支的测试用替身注入。"""

    def ready(self):
        return True

    def cooldown(self, reason):
        pass

    def report_device(self, device, note=""):
        pass


def _stub_gate_ready():
    return _StubGateReady()


class TestRealRerankerGpuArbitration(unittest.TestCase):
    """GPU 生命周期管理回归测试，同 official-embedder-bge-m3/tests/
    test_embed.py::TestRealEncoderGpuArbitration 的覆盖点，这里不重复
    展开设计理由。

    **"期望走 CUDA"的用例必须 mock 掉 vram_free_gb**：设备选择会读一次真实
    空闲显存（缺陷 B 修复），拿开发机的实际显存当断言前提就是让测试意图随
    环境悄悄改变。"""

    def test_no_cuda_selects_cpu_without_touching_arbiter(self):
        arb = ResourceArbiter()
        reranker = _RealReranker(resource_arbiter=arb)
        with patch("torch.cuda.is_available", return_value=False):
            self.assertEqual(reranker._select_device(), "cpu")
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID))

    def test_cuda_shares_holder_id_with_embedder_without_self_preempting(self):
        """embedder 和 reranker 共用同一个 GPU_HOLDER_ID——先后各自加载
        不该互相驱逐（见 rerank.py 模块 docstring 的共享 holder_id 设计）。"""
        arb = ResourceArbiter()
        arb.acquire(GPU_RESOURCE_ID, GPU_HOLDER_ID, priority=100)  # 模拟 embedder 先加载过了
        reranker = _RealReranker(resource_arbiter=arb, cooldown_gate=_stub_gate_ready())
        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0), patch(
            "torch.cuda.is_available", return_value=True
        ), patch(
            "official_reranker.rerank.gpu_arbiter.wait_for_vram",
            side_effect=AssertionError("检索侧不得阻塞等待VRAM（对齐旧项目：wait 语义只属于 WEMM/MinerU 服务端）"),
        ) as wait_spy:
            device = reranker._select_device()
        self.assertEqual(device, "cuda")
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        wait_spy.assert_not_called()

    def test_release_gpu_slot_returns_slot_to_arbiter(self):
        """on_disable 的名额归还（rerank.py 模块 docstring 一直声称这个行为，
        此前代码没实现——现在补齐并对齐）：停用后名额回到空闲状态。"""
        arb = ResourceArbiter()
        reranker = _RealReranker(resource_arbiter=arb, cooldown_gate=_stub_gate_ready())
        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0), patch(
            "torch.cuda.is_available", return_value=True
        ):
            self.assertEqual(reranker._select_device(), "cuda")
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        reranker.release_gpu_slot()
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID))

    def test_idle_check_unloads_model_after_timeout(self):
        reranker = _RealReranker()
        reranker._model = object()
        reranker._last_use = time.time() - 10_000
        reranker.idle_check()
        self.assertIsNone(reranker._model)


class _StubGateNotReady:
    """冷却门替身：恒不 ready（模拟"CUDA 失败进入冷却期"）。"""

    def ready(self):
        return False

    def cooldown(self, reason):
        pass

    def report_device(self, device, note=""):
        pass


class _TracingLock:
    """记录加锁顺序的 RLock 替身（真 RLock 承载，只把 enter/exit 记进 trace）。"""

    def __init__(self, name: str, trace: list[str]) -> None:
        self._real = threading.RLock()
        self._name = name
        self._trace = trace

    def __enter__(self):
        self._real.acquire()
        self._trace.append(f"enter:{self._name}")
        return self

    def __exit__(self, *exc):
        self._trace.append(f"exit:{self._name}")
        self._real.release()
        return False


_VRAM = "official_reranker.rerank.gpu_arbiter.vram_free_gb"
_SECRET_MARKERS = ("key", "token", "password", "secret", "bearer", "http://", "https://")


class TestRealRerankerVramCriterion(unittest.TestCase):
    """物理显存判据 + 降级日志 + 卸载锁序（缺陷 A/B/C 在重排器这一侧的对应
    覆盖点，判据与 embedder 完全同款——两者是同一个"检索侧 GPU 消费群体"，
    共用 GPU_HOLDER_ID，冷却/显存状态理应一致，见 rerank.py 模块 docstring）。"""

    def setUp(self) -> None:
        self.logger = logging.getLogger("rag_redo.test.rerank.vram")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(logging.NullHandler())

    def _reranker(self, *, arb=None, gate=None):
        return _RealReranker(
            resource_arbiter=arb if arb is not None else ResourceArbiter(),
            cooldown_gate=gate if gate is not None else _stub_gate_ready(),
            logger=self.logger,
        )

    def test_sufficient_vram_goes_cuda_with_single_probe(self):
        rr = self._reranker()
        with patch(_VRAM, return_value=7.0) as probe:
            self.assertEqual(rr._select_device(), "cuda")
        self.assertEqual(probe.call_count, 1, "显存充足时不必让路，也不必复核第二次")

    def test_insufficient_vram_rechecked_after_evict_then_degrades_to_cpu(self):
        rr = self._reranker()
        with patch(_VRAM, side_effect=[1.0, 2.0]) as probe:
            self.assertEqual(rr._select_device(), "cpu")
        self.assertEqual(probe.call_count, 2, "让路后必须复核一次真实显存")
        self.assertEqual(probe.call_args_list[-1].kwargs, {"max_age": 0.0})

    def test_insufficient_vram_that_clears_after_evict_goes_cuda(self):
        rr = self._reranker()
        with patch(_VRAM, side_effect=[1.0, 6.0]):
            self.assertEqual(rr._select_device(), "cuda")

    def test_probe_failure_fails_open_to_cuda(self):
        rr = self._reranker()
        with patch(_VRAM, return_value=None) as probe:
            self.assertEqual(rr._select_device(), "cuda")
        self.assertEqual(probe.call_count, 1)

    def test_cooldown_path_skips_probe_and_logs_reason(self):
        rr = self._reranker(gate=_StubGateNotReady())
        with patch(_VRAM, side_effect=AssertionError("冷却期内不得再探测显存")) as probe, \
                self.assertLogs(self.logger, level="WARNING") as captured:
            self.assertEqual(rr._select_device(), "cpu")
        probe.assert_not_called()
        self.assertIn("冷却期", "\n".join(captured.output))

    def test_slot_contention_path_logs_reason(self):
        arb = ResourceArbiter()
        arb.acquire(GPU_RESOURCE_ID, "somebody-else", priority=1000)
        rr = self._reranker(arb=arb)
        with patch(_VRAM, return_value=8.0), self.assertLogs(self.logger, level="WARNING") as captured:
            self.assertEqual(rr._select_device(), "cpu")
        text = "\n".join(captured.output)
        self.assertIn("名额抢占失败", text)
        self.assertIn("somebody-else", text)
        self.assertIn("8.0GB", text)

    def test_degrade_logs_carry_no_credentials(self):
        rr = self._reranker()
        with patch(_VRAM, side_effect=[1.0, 1.0]), self.assertLogs(self.logger, level="WARNING") as captured:
            self.assertEqual(rr._select_device(), "cpu")
        text = "\n".join(captured.output).lower()
        for marker in _SECRET_MARKERS:
            self.assertNotIn(marker, text, f"降级日志里不得出现 {marker}（AGENTS.md §7）")

    def test_unload_takes_plugin_lock_before_gpu_lock(self):
        """锁序（缺陷 A 的另一半）：`_unload` 是 on_preempt 回调的实现，必须
        self._lock → GPU_LOCK；写反即与 `score → _ensure_loaded` 构成 ABBA。"""
        trace: list[str] = []
        rr = self._reranker()
        rr._lock = _TracingLock("self_lock", trace)  # type: ignore[assignment]
        rr._model = object()
        with patch("official_reranker.rerank.gpu_arbiter.GPU_LOCK", _TracingLock("gpu_lock", trace)):
            rr._unload()
        self.assertIsNone(rr._model)
        self.assertEqual(trace[:2], ["enter:self_lock", "enter:gpu_lock"], f"锁序错误：{trace}")


class _FakeCrossEncoderModel:
    """假 cross-encoder：只要求 predict 能返回可排序的分数；`.to()` 记下搬去过哪些设备。"""

    def __init__(self, device: str = "cuda") -> None:
        self.device = device
        self.moves: list[str] = []
        self.fail_to: set[str] = set()

    def predict(self, pairs, batch_size=16):
        return [0.5] * len(pairs)

    def to(self, device):
        if device in self.fail_to:
            raise RuntimeError(f"CUDA out of memory（模拟搬到 {device} 失败）")
        self.moves.append(device)
        self.device = device
        return self


def _held_locks(trace: list[str]) -> "collections.Counter[str]":
    """从 `_TracingLock` 的 enter/exit 轨迹还原"此刻真正持有哪些锁"。

    两个坑让这件事不能想当然：`enter:gpu_lock` 出现过 ≠ 现在还持着它
    （判据段取完就放掉了）；两把锁都是 RLock 可重入，必须按**计数**还原，
    否则内层的 exit 会把"外层还持着锁"这个事实抹掉。详见
    official-embedder-bge-m3/tests/test_embed.py 同名 helper 的注释。"""
    held: collections.Counter[str] = collections.Counter()
    for entry in trace:
        kind, _, name = entry.partition(":")
        if kind == "enter":
            held[name] += 1
        else:
            held[name] -= 1
    return held


def _fake_sentence_transformers(cross_encoder) -> types.ModuleType:
    """伪造 sentence_transformers 模块。

    为什么不直接 patch 真实模块的属性：那句 import 会把真的
    sentence_transformers（连带 torch）拉起来，冷启动在本机实测接近 30s，
    而且"这台机器装没装这个包"会变成测试耗时/行为的隐含前提——同
    official-embedder-bge-m3/tests/test_embed.py 模块 docstring 记过的那个坑。
    """
    module = types.ModuleType("sentence_transformers")
    module.CrossEncoder = cross_encoder  # type: ignore[attr-defined]
    return module


class TestRealRerankerLoadFailureLatch(unittest.TestCase):
    """加载失败闩锁（缺陷 D）。

    对齐 obsidian-rag/retriever.py:249-264：`_get_reranker()` 第一次加载失败
    置 `_reranker_failed = True` 并**本会话不再重试**（docstring 明写"本次
    会话不再重试，降级纯融合"）。rag-redo 此前失败后 `_model` 仍是 None，
    于是**每次检索都重试一次完整模型加载**（冷加载数十秒，还可能联网），
    用户侧表现为"每次搜索都卡几十秒"，且只留一条 info 日志。"""

    def _failing_reranker(self, logger=None) -> tuple[_RealReranker, list[str]]:
        calls: list[str] = []

        def _boom(*args, **kwargs):
            calls.append("load")
            raise RuntimeError("模型文件损坏")

        patcher = patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_boom)})
        patcher.start()
        self.addCleanup(patcher.stop)
        reranker = _RealReranker(
            resource_arbiter=ResourceArbiter(), cooldown_gate=_stub_gate_ready(), logger=logger
        )
        return reranker, calls

    def test_first_failure_latches_and_second_call_does_not_reload(self):
        reranker, calls = self._failing_reranker()
        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0):
            for _ in range(3):
                with self.assertRaises(Exception):
                    reranker.score("q", ["a"])
        after_first = len(calls)
        self.assertGreater(after_first, 0)
        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0):
            for _ in range(3):
                with self.assertRaises(RerankUnavailable):
                    reranker.score("q", ["a"])
        self.assertEqual(len(calls), after_first, "闩锁之后不得再尝试加载模型")
        self.assertTrue(reranker.load_failed)

    def test_latched_reranker_raises_dedicated_unavailable_error(self):
        reranker, _ = self._failing_reranker()
        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0):
            with self.assertRaises(Exception):
                reranker.score("q", ["a"])
        with self.assertRaises(RerankUnavailable):
            reranker.score("q", ["a"])

    def test_reset_load_failure_allows_retry(self):
        """用户把模型补下载好之后要有重试入口（插件 on_disable/on_unload 会
        自动复位，见 TestRerankerPluginResetsLatch）。"""
        reranker, calls = self._failing_reranker()
        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0):
            with self.assertRaises(Exception):
                reranker.score("q", ["a"])
        after_failure = len(calls)
        reranker.reset_load_failure()
        self.assertFalse(reranker.load_failed)
        loaded: list[object] = []

        def _factory(*args, **kwargs):
            model = _FakeCrossEncoderModel()
            loaded.append(model)
            return model

        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0), \
                patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)}):
            reranker.score("q", ["a"])
        self.assertEqual(len(calls), after_failure, "只有复位前那一次是真失败")
        self.assertEqual(len(loaded), 1, "复位后必须真的重新加载出模型")

    def test_load_failure_logs_warning_without_credentials(self):
        logger = logging.getLogger("rag_redo.test.rerank.latch")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(logging.NullHandler())
        reranker, _ = self._failing_reranker(logger=logger)
        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0), \
                self.assertLogs(logger, level="WARNING") as captured:
            with self.assertRaises(Exception):
                reranker.score("q", ["a"])
        text = "\n".join(captured.output).lower()
        self.assertIn("不再重试", text)
        for marker in ("key", "token", "password", "secret", "bearer", "http://", "https://"):
            self.assertNotIn(marker, text, f"日志里不得出现 {marker}（AGENTS.md §7）")

    def test_engine_returns_empty_ranking_when_latched(self):
        """闩锁后的降级出口在引擎层：返回空列表让 core/pipeline.py 走它既有
        的"按库归一化合并"，不抛异常、不刷屏（对齐旧项目
        retriever.py:680 `if reranker is not None` 的拿不到就不重排）。"""
        reranker, calls = self._failing_reranker()
        engine = RerankerEngine(reranker=reranker)
        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0):
            # 第一次是真的加载失败：异常照旧往上抛（调用方/用户当场看得见），
            # 同时闩锁置位。
            with self.assertRaises(RuntimeError):
                engine.rerank("q", [("c1", "文本")])
            after_first = len(calls)
            self.assertGreater(after_first, 0)
            # 之后每一次都只降级、不再加载。
            self.assertEqual(engine.rerank("q", [("c1", "文本")]), [])
            self.assertEqual(engine.rerank("q", [("c1", "文本")]), [])
        self.assertEqual(len(calls), after_first, "闩锁后不得重复加载")

    def test_engine_reset_reaches_real_reranker(self):
        reranker, _ = self._failing_reranker()
        RerankerEngine(reranker=reranker).reset_load_failure()
        self.assertFalse(reranker.load_failed)

    def test_engine_reset_tolerates_fake_reranker_without_hook(self):
        """注入的假 reranker 没有这个方法时静默跳过（同 idle_check 的宽容
        语义，插件 on_disable 对任何实现都要能调）。"""
        RerankerEngine(reranker=_FakeReranker()).reset_load_failure()


class _Ctx:
    """最小 PluginContext 替身（重排器插件 on_load/on_disable 只用到
    settings/storage/logger/resource_arbiter）。"""

    def __init__(self, logger: logging.Logger) -> None:
        self.settings: dict = {}
        self.logger = logger
        self.resource_arbiter = ResourceArbiter()
        self.storage = types.SimpleNamespace(file=lambda *a, **k: None)
        self.plugin_id = "official-reranker"


class TestRerankerPluginResetsLatch(unittest.TestCase):
    """插件生命周期必须复位闩锁——否则用户"停用再启用重排器"这个唯一
    无需重启进程的重试入口就废了（旧项目靠重启进程清 `_reranker_failed`）。"""

    def setUp(self) -> None:
        self.logger = logging.getLogger("rag_redo.test.rerank.plugin")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(logging.NullHandler())

    def test_on_disable_resets_load_failure_latch(self):
        plugin = RerankerPlugin()
        ctx = _Ctx(self.logger)
        plugin.on_load(ctx)
        engine = plugin.engine
        self.assertIsNotNone(engine)
        engine._reranker._failed = True  # 模拟已闩锁
        plugin.on_disable(ctx)
        self.assertFalse(engine._reranker._failed)

    def test_on_unload_resets_load_failure_latch(self):
        plugin = RerankerPlugin()
        ctx = _Ctx(self.logger)
        plugin.on_load(ctx)
        engine = plugin.engine
        self.assertIsNotNone(engine)
        engine._reranker._failed = True
        plugin.on_disable(ctx)
        engine._reranker._failed = True
        plugin.on_unload(ctx)
        self.assertFalse(engine._reranker._failed)
        self.assertIsNone(plugin.engine)


class TestRerankerPluginReleaseGpu(unittest.TestCase):
    """手动"释放显存"按钮用（core/pipeline.py::release_gpu_memory，
    2026-09-29 新能力，BC-16）——同 official-embedder-bge-m3/tests/
    test_embed.py::TestEmbedderPluginLeaseLifecycle 的同名测试，这里不重复
    展开设计理由：跟 on_disable 一样真卸载模型、归还名额，但不禁用插件、
    不复位加载失败闩锁，下次重排透明重新加载。"""

    def setUp(self) -> None:
        self.logger = logging.getLogger("rag_redo.test.rerank.plugin.release_gpu")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(logging.NullHandler())

    def test_release_gpu_unloads_model_but_keeps_plugin_usable(self):
        def _factory(*args, **kwargs):
            return _FakeCrossEncoderModel()

        plugin = RerankerPlugin()
        ctx = _Ctx(self.logger)
        plugin.on_load(ctx)
        arb = ctx.resource_arbiter
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)}), \
                patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0), \
                patch("torch.cuda.is_available", return_value=True):
            plugin.engine.rerank("q", [("c1", "text")])
        real = plugin.engine._reranker
        self.assertIsNotNone(real._model)
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)

        plugin.release_gpu()
        self.assertIsNone(real._model, "释放显存必须真的把模型卸掉")
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID), "释放显存必须归还GPU名额")

        # 插件本身仍然可用：不需要重新 on_enable，下次重排透明重新加载。
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)}), \
                patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0), \
                patch("torch.cuda.is_available", return_value=True):
            plugin.engine.rerank("q", [("c1", "text again")])
        self.assertIsNotNone(real._model, "释放显存之后插件必须还能正常工作，不需要重新启用")


class TestRerankerModelsDir(unittest.TestCase):
    """BC-17：模型存放目录可配置（默认项目内 models/），每次加载模型时现读设置。
    同 official-embedder-bge-m3/tests/test_embed.py::TestEmbedderModelsDir。"""

    def setUp(self) -> None:
        self.logger = logging.getLogger("rag_redo.test.rerank.models_dir")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(logging.NullHandler())

    def _score_recording_cache_folders(self, call, folders: list) -> None:
        def _factory(*args, **kwargs):
            folders.append(kwargs.get("cache_folder"))
            return _FakeCrossEncoderModel()

        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0), \
                patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)}):
            call()

    def test_configured_models_dir_is_passed_to_the_factory_as_cache_folder(self):
        import tempfile
        from pathlib import Path

        target = Path(tempfile.gettempdir()) / "rag_redo_rerank_models"
        reranker = _RealReranker(
            resource_arbiter=ResourceArbiter(), cooldown_gate=_stub_gate_ready(), models_dir=lambda: target
        )
        folders: list = []
        self._score_recording_cache_folders(lambda: reranker.score("q", ["a"]), folders)
        self.assertEqual(folders, [str(target)], "重排模型必须从用户配置的目录加载")

    def test_without_a_models_dir_reader_nothing_extra_is_passed(self):
        reranker = _RealReranker(resource_arbiter=ResourceArbiter(), cooldown_gate=_stub_gate_ready())
        folders: list = []
        self._score_recording_cache_folders(lambda: reranker.score("q", ["a"]), folders)
        self.assertEqual(folders, [None])

    def test_plugin_reads_the_setting_afresh_on_every_model_load(self):
        import tempfile
        from pathlib import Path

        first = Path(tempfile.gettempdir()) / "rag_redo_rerank_first"
        second = Path(tempfile.gettempdir()) / "rag_redo_rerank_second"
        plugin = RerankerPlugin()
        ctx = _Ctx(self.logger)
        ctx.settings["models_dir"] = str(first)
        plugin.on_load(ctx)
        folders: list = []
        self._score_recording_cache_folders(lambda: plugin.engine.rerank("q", [("c1", "t")]), folders)
        plugin.release_gpu()
        ctx.settings["models_dir"] = str(second)
        self._score_recording_cache_folders(lambda: plugin.engine.rerank("q", [("c1", "t2")]), folders)
        self.assertEqual(folders, [str(first), str(second)])

    def test_plugin_defaults_to_the_project_models_folder(self):
        from core.paths import models_dir as default_models_dir

        plugin = RerankerPlugin()
        ctx = _Ctx(self.logger)  # settings 为空 = 没配置
        plugin.on_load(ctx)
        folders: list = []
        self._score_recording_cache_folders(lambda: plugin.engine.rerank("q", [("c1", "t")]), folders)
        self.assertEqual(folders, [str(default_models_dir(""))])
        self.assertEqual(default_models_dir("").name, "models", "默认必须是项目内的 models 文件夹")


class TestRerankerPreemptedModelWaitsInRam(unittest.TestCase):
    """被抢显卡时挪到内存、再用时搬回（BC-11，2026-09-30 操作者确认，与 embedder 同款）；
    空闲满时限、手动释放、插件停用仍整个卸载。"""

    def _loaded(self):
        loads: list[_FakeCrossEncoderModel] = []

        def _factory(model_id, **kwargs):
            model = _FakeCrossEncoderModel(kwargs.get("device", "cpu"))
            loads.append(model)
            return model

        arb = ResourceArbiter()
        reranker = _RealReranker(resource_arbiter=arb, cooldown_gate=_stub_gate_ready())
        patcher = patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)})
        patcher.start()
        self.addCleanup(patcher.stop)
        vram = patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0)
        vram.start()
        self.addCleanup(vram.stop)
        reranker.score("q", ["a"])
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        return reranker, arb, loads

    def test_preempted_reranker_waits_in_ram_and_comes_back_without_reloading(self):
        reranker, arb, loads = self._loaded()
        model = loads[0]
        self.assertTrue(arb.acquire(GPU_RESOURCE_ID, "somebody-else", priority=1000))
        self.assertIs(reranker._model, model, "被抢时模型留在内存里，不扔")
        self.assertEqual(model.device, "cpu")
        self.assertTrue(reranker._parked)
        self.assertFalse(reranker._gpu_resident)
        arb.release(GPU_RESOURCE_ID, "somebody-else")
        reranker.score("q", ["b"])
        self.assertEqual(len(loads), 1, "再用时不重新加载")
        self.assertEqual(model.moves, ["cpu", "cuda"])
        self.assertTrue(reranker._gpu_resident)
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)

    def test_parked_reranker_is_fully_unloaded_after_idle_timeout(self):
        reranker, _arb, _loads = self._loaded()
        reranker._park()
        reranker._last_use = time.time() - 10_000
        reranker.idle_check()
        self.assertIsNone(reranker._model)
        self.assertFalse(reranker._parked)

    def test_parking_failure_falls_back_to_full_unload(self):
        reranker, arb, loads = self._loaded()
        loads[0].fail_to.add("cpu")
        reranker._park()
        self.assertIsNone(reranker._model)
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID))
        self.assertFalse(reranker.load_failed, "让路失败不是加载失败，不能闩锁")

    def test_park_takes_plugin_lock_before_gpu_lock(self):
        """`_park` 是 on_preempt 回调，锁序必须与 `_unload` 一样（self._lock → GPU_LOCK）。"""
        trace: list[str] = []
        rr = _RealReranker(resource_arbiter=ResourceArbiter(), cooldown_gate=_stub_gate_ready())
        rr._lock = _TracingLock("self_lock", trace)  # type: ignore[assignment]
        with patch("official_reranker.rerank.gpu_arbiter.GPU_LOCK", _TracingLock("gpu_lock", trace)):
            rr._park()
        self.assertEqual(trace[:2], ["enter:self_lock", "enter:gpu_lock"], f"锁序错误：{trace}")


class TestRerankerDegradeKeepsModelLoaded(unittest.TestCase):
    """降级到 CPU 之后模型必须留在内存里接着用，且名额当场归还。

    2026-09-27 与 official-embedder-bge-m3 同步补的两条（两个插件是刻意的
    小重复，同一份判断不能有两份实现，AGENTS.md §4.5）：

    - 降级不是失败：模型照常在 CPU 上装载出来，用户侧只该感觉到慢；
    - 引用计数归零**只归还名额、不卸载模型**——否则"降级"会变成"每次检索
      都重新加载一遍几 GB 权重"，且全程无任何报错。
    """

    def test_cpu_degraded_model_is_not_reloaded_on_every_call(self):
        loads: list[str] = []

        def _factory(model_id, **kwargs):
            device = kwargs.get("device", "cpu")
            loads.append(str(device))
            return _FakeCrossEncoderModel()

        arb = ResourceArbiter()
        reranker = _RealReranker(resource_arbiter=arb, cooldown_gate=_stub_gate_ready())
        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", side_effect=[1.0, 1.0]), \
                patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)}):
            reranker.score("q", ["a"])
            reranker.score("q", ["b"])
        self.assertEqual(loads, ["cpu"], "模型只应装载一次：引用归零不是卸载模型的许可")
        self.assertIsNotNone(reranker._model)
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID), "降级 CPU 后绝不能占着 gpu:0 名额")

    def test_slot_request_never_runs_while_holding_gpu_lock(self):
        """设备选择里唯一会阻塞的名额申请绝不能包在 `GPU_LOCK` 里：让方的
        `on_preempt` 回调要拿 `GPU_LOCK` 才能卸载模型，申请方持锁等 = 互等到
        超时然后白白降级 CPU。详见 official-embedder-bge-m3/embed.py 模块
        docstring 的"跨进程抢占不许自锁"一节。"""
        trace: list[str] = []
        seen_at_acquire: list[str] = []

        class _Arbiter:
            def holder_of(self, resource_id):
                return None

            def acquire(self, *args, **kwargs):
                seen_at_acquire.extend(trace)
                return True

            def release(self, *args, **kwargs):
                pass

        reranker = _RealReranker(resource_arbiter=_Arbiter(), cooldown_gate=_stub_gate_ready())
        reranker._lock = _TracingLock("self_lock", trace)  # type: ignore[assignment]
        with patch("official_reranker.rerank.gpu_arbiter.vram_free_gb", return_value=8.0), patch(
            "official_reranker.rerank.gpu_arbiter.GPU_LOCK", _TracingLock("gpu_lock", trace)
        ):
            with reranker._lock:
                self.assertEqual(reranker._select_device(), "cuda")
        self.assertIn("enter:gpu_lock", trace, "非阻塞的显存判据仍应由 GPU_LOCK 护住")
        held = _held_locks(seen_at_acquire)
        self.assertEqual(held["gpu_lock"], 0, f"名额申请必须在 GPU_LOCK 之外执行（此刻持锁 {dict(held)}）：{trace}")
        self.assertEqual(held["self_lock"], 1, "生产路径上 score() 本来就持着 self._lock")


if __name__ == "__main__":
    unittest.main()
