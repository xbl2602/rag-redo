"""单测全程注入假 encoder，绝不碰真实模型——同旧项目测试纪律。

**真实踩过的坑**：这份文件曾经靠"本开发环境没装 sentence_transformers"
这个环境事实，让"encode() 应该抛 ModuleNotFoundError"这条断言显得成立。
后来因为要做一次端到端真实验证（用真模型跑一遍 demo-vault），临时装了
sentence_transformers/torch——这条测试当场从"几毫秒的轻量断言"变成了
"真的触发几GB模型下载"，把整个测试套件的运行时间从几秒拖到十几分钟，
内存涨到几个GB，还会真的发网络请求。用真实环境状态当测试断言成立的
前提是脆弱的：环境一变，测试的意图跟着悄悄改变，且不会有任何提示。
现在改用 `unittest.mock.patch.dict(sys.modules, ...)` 强制模拟"这个包
不存在"，不管开发机上实际装没装 sentence_transformers，这条测试的行为
都是确定的、和真实环境解耦的。

**GPU 名额/租约那组用例（`TestGpuLease*`）同样绝不加载真模型**：它们要用
到 `_ensure_loaded` 的完整装载路径（选设备→申请名额→装载→重算引用），
所以和 official-reranker/tests/test_rerank.py 用的是同一手法——
`patch.dict(sys.modules, {"sentence_transformers": 假模块})` 把真包整个
换掉，而不是 patch 真包的属性。理由同上，而且更硬：那句
`from sentence_transformers import SentenceTransformer` 会把真的
sentence_transformers 连带 torch 拉起来，冷启动在开发机实测接近 30s，
还会顺手发一次 HF 联网请求。假模块只提供 `parameters()`/`encode()` 两个
最小接口，够走完 fp16 装载与批量编码这两段逻辑。
"""
from __future__ import annotations

import collections
import logging
import shutil
import sys
import tempfile
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
from official_embedder_bge_m3.embed import GPU_HOLDER_ID, GPU_RESOURCE_ID, BGEM3Embedder, _RealEncoder  # noqa: E402
from official_embedder_bge_m3.plugin import EmbedderPlugin  # noqa: E402


class _FakeEncoder:
    """确定性假向量：不碰任何真实模型，只依赖文本长度，纯为了让测试可
    重复、可断言。"""

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t) % 7 + 1)] * self.dim for t in texts]


class TestBGEM3EmbedderWithFakeEncoder(unittest.TestCase):
    def test_empty_input_returns_empty_without_calling_encoder(self):
        fake = _FakeEncoder()
        embedder = BGEM3Embedder(encoder=fake)
        self.assertEqual(embedder.embed([]), [])
        self.assertEqual(fake.calls, [])

    def test_embed_returns_one_vector_per_text(self):
        embedder = BGEM3Embedder(encoder=_FakeEncoder(dim=4))
        vectors = embedder.embed(["abc", "de", ""])
        self.assertEqual(len(vectors), 3)
        self.assertTrue(all(len(v) == 4 for v in vectors))

    def test_encoder_receives_exact_texts_passed(self):
        fake = _FakeEncoder()
        embedder = BGEM3Embedder(encoder=fake)
        embedder.embed(["hello", "world"])
        self.assertEqual(fake.calls, [["hello", "world"]])


class TestRealEncoderLazyLoading(unittest.TestCase):
    def test_construction_does_not_touch_model(self):
        encoder = _RealEncoder()  # 不应该报错，即使 sentence_transformers 没装
        self.assertIsNone(encoder._model)

    def test_encode_without_dependency_raises_clear_import_error(self):
        """强制模拟 sentence_transformers 不存在（不依赖开发机实际有没有
        装这个包，见模块 docstring）——真正调用 encode() 时应该是清清楚楚
        的 ImportError，而不是模块导入期就挂掉、也不是某种更晦涩的错误。
        这个测试本身能跑到断言这一步，就已经证明了"构造 _RealEncoder
        不需要这个依赖"（懒加载）。"""
        encoder = _RealEncoder()
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            with self.assertRaises(ImportError):
                encoder.encode(["test"])


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


class _StubRecordingGate:
    """记录 cooldown/report 调用的冷却门替身（ready 恒 True）。"""

    def __init__(self):
        self.cooldowns: list[str] = []
        self.reports: list[str] = []

    def ready(self):
        return True

    def cooldown(self, reason):
        self.cooldowns.append(reason)

    def report_device(self, device, note=""):
        self.reports.append(device)


class TestRealEncoderGpuArbitration(unittest.TestCase):
    """GPU 生命周期管理回归测试（2026-09-23 补，按 obsidian-rag 真实行为
    移植，见 embed.py 模块 docstring）：设备选择、检索侧高优先级抢占、
    空闲卸载。这台开发机上 torch 是真实装了的（CPU-only），用 mock 强制
    走 CUDA 分支，不依赖真实有没有 GPU 硬件。

    **所有"期望走 CUDA"的用例都必须 mock 掉 `vram_free_gb`**：设备选择现在
    会读一次真实空闲显存（缺陷 B 修复后，见同文件
    TestRealEncoderVramCriterion），而开发机/用户机的实际空闲显存是环境事实
    （本机 8GB 卡上常年只有 4GB 上下），拿环境事实当断言前提就是让测试
    意图随环境悄悄改变——同一个坑，embed.py 模块 docstring 记过一次。"""

    def test_no_cuda_selects_cpu_without_touching_arbiter(self):
        arb = ResourceArbiter()
        encoder = _RealEncoder(resource_arbiter=arb)
        with patch("torch.cuda.is_available", return_value=False):
            self.assertEqual(encoder._select_device(), "cpu")
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID))

    def test_cuda_preempts_lower_priority_wemm_style_holder(self):
        """检索侧优先抢占（对齐旧项目行为）：bge-m3 要用 CUDA 时，如果
        WEMM/OCR-local 这类低优先级消费者正占着"gpu:0"，应该被挤开。"""
        arb = ResourceArbiter()
        preempted = []
        arb.acquire("gpu:0", "official-visual-wemm", priority=10, on_preempt=lambda: preempted.append("wemm"), preempt_equal=True)
        encoder = _RealEncoder(resource_arbiter=arb, cooldown_gate=_stub_gate_ready())
        with patch("official_embedder_bge_m3.embed.gpu_arbiter.vram_free_gb", return_value=8.0), patch(
            "torch.cuda.is_available", return_value=True
        ), patch(
            "official_embedder_bge_m3.embed.gpu_arbiter.wait_for_vram",
            side_effect=AssertionError("检索侧不得阻塞等待VRAM（对齐旧项目：wait 语义只属于 WEMM/MinerU 服务端）"),
        ) as wait_spy:
            device = encoder._select_device()
        self.assertEqual(device, "cuda")
        self.assertEqual(preempted, ["wemm"])
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        wait_spy.assert_not_called()

    def test_cuda_load_failure_enters_cooldown_and_degrades_to_cpu(self):
        """对齐旧项目 get_model：CUDA 初始化失败 → 冷却 + 降级 CPU 重试一次
        （CPU 再失败才真的抛出）；诊断里记录失败原因。加载契约（2026-09-25
        fp16 对齐后）：每设备先试 fp16、失败回退 fp32（旧 _load_model 的
        两段尝试），CUDA 两段都失败才进冷却降 CPU。"""
        gate = _StubRecordingGate()
        encoder = _RealEncoder(resource_arbiter=ResourceArbiter(), cooldown_gate=gate)

        calls = []

        def _fake_st(model_name, **kwargs):
            device = kwargs["device"]
            calls.append(device)
            if device == "cuda":
                raise RuntimeError("CUDA out of memory")
            return object()

        with (
            patch("torch.cuda.is_available", return_value=True),
            patch("official_embedder_bge_m3.embed.gpu_arbiter.vram_free_gb", return_value=8.0),
            patch("sentence_transformers.SentenceTransformer", side_effect=_fake_st),
        ):
            model = encoder._ensure_loaded()
        self.assertIsNotNone(model)
        # 每设备至多 4 次尝试（离线优先 local→online × fp16→fp32，旧
        # _load_pretrained 与 _load_model 两级回退的忠实展开），CUDA 全部
        # 失败才进冷却降 CPU；CPU 上 fp16 加载成功但 object() 无 parameters
        # → 混搭检查异常 → fp32 回退（旧 _load_model 对 fp32 回退不再检查）。
        self.assertEqual(calls, ["cuda", "cuda", "cuda", "cuda", "cpu", "cpu"])
        # 冷却原因携带加载失败的完整诊断（含根因），同 load_pretrained 的
        # RuntimeError 包装格式
        self.assertEqual(len(gate.cooldowns), 1)
        self.assertIn("CUDA out of memory", gate.cooldowns[0])
        self.assertEqual(gate.reports, ["cpu"])

    def test_release_gpu_slot_returns_slot_to_arbiter(self):
        """on_disable 的名额归还（对齐 rerank.py 模块 docstring 声明的既有
        设计）：停用后名额必须回到仲裁器的"空闲"状态，否则被禁用的检索侧
        会以高优先级永久占位，WEMM/OCR-local 再也抢不到。"""
        arb = ResourceArbiter()
        encoder = _RealEncoder(resource_arbiter=arb, cooldown_gate=_stub_gate_ready())
        with patch("official_embedder_bge_m3.embed.gpu_arbiter.vram_free_gb", return_value=8.0), patch(
            "torch.cuda.is_available", return_value=True
        ):
            self.assertEqual(encoder._select_device(), "cuda")
        self.assertEqual(arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        encoder.release_gpu_slot()
        self.assertIsNone(arb.holder_of(GPU_RESOURCE_ID))

    def test_idle_check_unloads_model_after_timeout(self):
        encoder = _RealEncoder()
        encoder._model = object()  # 假装模型已加载，不需要真的加载一遍
        encoder._last_use = time.time() - 10_000  # 远超默认 300s 空闲阈值
        encoder.idle_check()
        self.assertIsNone(encoder._model)

    def test_idle_check_does_not_unload_recently_used_model(self):
        encoder = _RealEncoder()
        encoder._model = object()
        encoder._last_use = time.time()
        encoder.idle_check()
        self.assertIsNotNone(encoder._model)


class _StubGateNotReady:
    """冷却门替身：恒不 ready（模拟"CUDA 失败进入冷却期"）。"""

    def ready(self):
        return False

    def cooldown(self, reason):
        pass

    def report_device(self, device, note=""):
        pass


class _TracingLock:
    """记录加锁顺序的 RLock 替身（用真实 RLock 承载，只把 enter/exit 记进
    共享 trace）。用来钉死"先拿哪把锁"这种结构事实。"""

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


def _held_locks(trace: list[str]) -> "collections.Counter[str]":
    """从 `_TracingLock` 的 enter/exit 轨迹还原"此刻真正持有哪些锁"。

    两个坑让这件事不能想当然：
    - `enter:gpu_lock` 出现过 ≠ 现在还持着它（判据段取完就放掉了），所以要
      按 enter/exit 配对还原嵌套状态，而不是直接看轨迹里有没有出现过；
    - 两把锁都是 RLock（可重入），必须按**计数**而不是集合还原——同一把锁
      嵌套两层时，内层的 exit 不能把外层那一份也抹掉，否则"外层还持着锁"
      这个事实会被算成"没持锁"，测试就成了同义反复。
    """
    held: collections.Counter[str] = collections.Counter()
    for entry in trace:
        kind, _, name = entry.partition(":")
        if kind == "enter":
            held[name] += 1
        else:
            held[name] -= 1
    return held


_VRAM = "official_embedder_bge_m3.embed.gpu_arbiter.vram_free_gb"
_SECRET_MARKERS = ("key", "token", "password", "secret", "bearer", "http://", "https://")


class TestRealEncoderVramCriterion(unittest.TestCase):
    """物理显存判据 + 降级日志（缺陷 B / C）。

    对齐 obsidian-rag/index.py:704-745 `_vram_maybe_evict_wemm` 的前置判定
    与 obsidian-rag/gpu_arbiter.py:34-35 的门槛值：空闲显存低于 BGE_MIN_VRAM_GB
    就先让路、让路后复核、复核仍不足就别硬上 CUDA；探测不到（None）则
    fail-open 放行（AGENTS.md §7"探测可 fail-open"）。
    """

    def setUp(self) -> None:
        self.logger = logging.getLogger("rag_redo.test.embed.vram")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        # 挂一个 NullHandler：没有 handler 时 logging 的"最后兜底"会把
        # WARNING 直接打到 stderr，污染测试输出。
        self.logger.addHandler(logging.NullHandler())

    def _encoder(self, *, arb=None, gate=None, logger=None):
        return _RealEncoder(
            resource_arbiter=arb if arb is not None else ResourceArbiter(),
            cooldown_gate=gate if gate is not None else _stub_gate_ready(),
            logger=logger if logger is not None else self.logger,
        )

    def test_sufficient_vram_goes_cuda_with_single_probe(self):
        enc = self._encoder()
        with patch(_VRAM, return_value=7.0) as probe:
            self.assertEqual(enc._select_device(), "cuda")
        self.assertEqual(probe.call_count, 1, "显存充足时不必让路，也不必复核第二次")

    def test_insufficient_vram_rechecked_after_evict_then_degrades_to_cpu(self):
        """显存不足 → 名额仲裁请求让路 → 复核仍不足 → 降级 CPU（而不是硬上
        CUDA 必然 OOM）。"""
        enc = self._encoder()
        with patch(_VRAM, side_effect=[1.0, 2.0]) as probe:
            self.assertEqual(enc._select_device(), "cpu")
        self.assertEqual(probe.call_count, 2, "让路后必须复核一次真实显存")
        self.assertEqual(probe.call_args_list[-1].kwargs, {"max_age": 0.0}, "复核必须绕过探测缓存")

    def test_insufficient_vram_that_clears_after_evict_goes_cuda(self):
        enc = self._encoder()
        with patch(_VRAM, side_effect=[1.0, 6.0]):
            self.assertEqual(enc._select_device(), "cuda")

    def test_recheck_probe_failure_also_fails_open_to_cuda(self):
        """让路后的复核探测又失败(None)同样是"判断不了"→ fail-open 放行，
        不能因为第一次说不够就锁死 CPU（日志里也不能写出 NoneGB 这种脏字）。"""
        enc = self._encoder()
        with patch(_VRAM, side_effect=[1.0, None]), self.assertLogs(self.logger, level="INFO") as captured:
            self.assertEqual(enc._select_device(), "cuda")
        text = "\n".join(captured.output)
        self.assertNotIn("NoneGB", text)
        self.assertIn("探测失败", text)

    def test_probe_failure_fails_open_to_cuda(self):
        """探测失败(None) 必须 fail-open 放行到 CUDA——绝不能让"判断不了显存"
        变成"永远用 CPU"（旧项目 gpu_arbiter.py fail-open 铁律 /
        AGENTS.md §7）。"""
        enc = self._encoder()
        with patch(_VRAM, return_value=None) as probe:
            self.assertEqual(enc._select_device(), "cuda")
        self.assertEqual(probe.call_count, 1, "探测失败时不该再复核一次")

    def test_cooldown_path_skips_probe_and_logs_reason(self):
        enc = self._encoder(gate=_StubGateNotReady())
        with patch(_VRAM, side_effect=AssertionError("冷却期内不得再探测显存")) as probe, \
                self.assertLogs(self.logger, level="WARNING") as captured:
            self.assertEqual(enc._select_device(), "cpu")
        probe.assert_not_called()
        self.assertIn("冷却期", "\n".join(captured.output))

    def test_slot_contention_path_logs_reason(self):
        """名额抢不到（更高优先级持有者）→ 降级 CPU 且有日志（缺陷 C）。"""
        arb = ResourceArbiter()
        arb.acquire(GPU_RESOURCE_ID, "somebody-else", priority=1000)
        enc = self._encoder(arb=arb)
        with patch(_VRAM, return_value=8.0), self.assertLogs(self.logger, level="WARNING") as captured:
            self.assertEqual(enc._select_device(), "cpu")
        text = "\n".join(captured.output)
        self.assertIn("名额抢占失败", text)
        self.assertIn("somebody-else", text, "日志要带上当前名额持有者，便于定位谁占着 GPU")
        self.assertIn("8.0GB", text, "日志要带上探测到的显存值")

    def test_insufficient_vram_logs_reason_and_carries_no_credentials(self):
        enc = self._encoder()
        with patch(_VRAM, side_effect=[1.0, 1.0]), self.assertLogs(self.logger, level="WARNING") as captured:
            self.assertEqual(enc._select_device(), "cpu")
        text = "\n".join(captured.output).lower()
        self.assertIn("用 cpu", text)
        self.assertIn("1.0gb", text)
        for marker in _SECRET_MARKERS:
            self.assertNotIn(marker, text, f"降级日志里不得出现 {marker}（AGENTS.md §7 敏感值不进日志）")

    def test_unload_takes_plugin_lock_before_gpu_lock(self):
        """锁序（缺陷 A 的另一半）：`_unload` 是 on_preempt 回调的实现，必须
        按 self._lock → GPU_LOCK 的顺序拿锁；写成 GPU_LOCK → self._lock 会与
        `encode → _ensure_loaded`（self._lock → GPU_LOCK）构成 ABBA。"""
        trace: list[str] = []
        enc = self._encoder()
        enc._lock = _TracingLock("self_lock", trace)  # type: ignore[assignment]
        enc._model = object()
        with patch("official_embedder_bge_m3.embed.gpu_arbiter.GPU_LOCK", _TracingLock("gpu_lock", trace)):
            enc._unload()
        self.assertIsNone(enc._model)
        self.assertEqual(trace[:2], ["enter:self_lock", "enter:gpu_lock"], f"锁序错误：{trace}")

    def test_slot_request_never_runs_while_holding_gpu_lock(self):
        """设备选择里唯一会阻塞的一步（跨进程名额申请）绝不能包在 `GPU_LOCK`
        里面——这一条是实测踩出来的死锁，不是风格偏好。

        跨进程抢占的时序：申请方阻塞在 `ResourceArbiter.acquire()` 里等对方
        让路，而让路是在**被让方那个进程**的后台监控线程里跑 `on_preempt`
        回调（core/resource_arbiter.py `_process_preempt_requests`，纪律 1
        明确要求回调在锁外执行）——回调要拿 `GPU_LOCK` 才能卸载模型腾显存。
        申请方持着 `GPU_LOCK` 等 = 让方等 GPU_LOCK、申请方等让方放手，互等到
        `preempt_timeout_s` 超时。实测症状：GUI 侧模型已经被卸载了，MCP 侧
        5s 超时后仍然拿不到名额，只能白白降级 CPU（`TestGpuLease
        CrossProcess` 的场景一就是复现它的用例）。

        `core/gpu_arbiter.py` 里 `GPU_LOCK` 的持有期纪律本来就写着"只包判定+
        快速变更，绝不包长等待"，这里只是把它落实到代码上。"""
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

        enc = self._encoder(arb=_Arbiter())
        enc._lock = _TracingLock("self_lock", trace)  # type: ignore[assignment]
        with patch(_VRAM, return_value=8.0), patch(
            "official_embedder_bge_m3.embed.gpu_arbiter.GPU_LOCK", _TracingLock("gpu_lock", trace)
        ):
            with enc._lock:  # 生产路径上 encode() 本来就持着它
                self.assertEqual(enc._select_device(), "cuda")
        self.assertIn("enter:gpu_lock", trace, "非阻塞的显存判据仍应由 GPU_LOCK 护住")
        held = _held_locks(seen_at_acquire)
        self.assertEqual(held["gpu_lock"], 0, f"名额申请必须在 GPU_LOCK 之外执行（此刻持锁 {dict(held)}）：{trace}")
        self.assertEqual(held["self_lock"], 1, "生产路径上 encode() 本来就持着 self._lock")


class TestAutoBatchSize(unittest.TestCase):
    """显存自适应批次（问题59-B4，逐字对齐旧 index.py::_auto_batch_size）。"""

    def _encoder(self):
        return _RealEncoder(resource_arbiter=ResourceArbiter())

    def test_desired_within_cap_passthrough_on_cpu(self):
        enc = self._encoder()
        self.assertEqual(enc._auto_batch_size(8), 8)
        self.assertEqual(enc._auto_batch_size(32), 32)

    def test_cuda_caps_at_8_and_tightens_to_4_when_vram_low(self):
        enc = self._encoder()
        enc._device = "cuda"
        fake_info = lambda: (int(6.0 * 1024 ** 3), 8 * 1024 ** 3)  # noqa: E731
        with patch("torch.cuda.mem_get_info", side_effect=lambda: fake_info()):
            self.assertEqual(enc._auto_batch_size(32), 8)
        low_info = lambda: (int(3.0 * 1024 ** 3), 8 * 1024 ** 3)  # noqa: E731
        with patch("torch.cuda.mem_get_info", side_effect=lambda: low_info()):
            self.assertEqual(enc._auto_batch_size(32), 4)

    def test_probe_failure_falls_back_to_conservative_cap(self):
        """探针失败按"显存未知"处理取保守上限 8（fail-open 铁律）。"""
        enc = self._encoder()
        enc._device = "cuda"
        with patch("torch.cuda.mem_get_info", side_effect=RuntimeError("probe boom")):
            self.assertEqual(enc._auto_batch_size(32), 8)


# ===========================================================================
# GPU 名额（租约）语义：名额 = 模型此刻真的在显存里
# （见 embed.py 模块 docstring 的"GPU 租约语义"一节；AGENTS.md §7
#  「锁不能代替真实的跨进程 holder 协调」）
# ===========================================================================


class _FakeSentenceTransformerModel:
    """假 SentenceTransformer：只提供 embed.py 装载/编码路径真正用到的两个
    接口——`parameters()` 给 fp16 dtype 混搭检查、`encode()` 给批量编码。"""

    def __init__(self, device: str = "cuda") -> None:
        self.device = device
        self.moves: list[str] = []  # `.to()` 搬过哪些设备（被抢显卡挪内存 / 再用时搬回）
        self.fail_to: set[str] = set()  # 往这些设备搬时模拟失败（显存不够等）

    def parameters(self):
        return [types.SimpleNamespace(dtype="torch.float16")]

    def to(self, device):
        if device in self.fail_to:
            raise RuntimeError(f"CUDA out of memory（模拟搬到 {device} 失败）")
        self.moves.append(device)
        self.device = device
        return self

    def encode(self, texts, normalize_embeddings=False, batch_size=0):
        if normalize_embeddings is not True:
            raise AssertionError("编码必须归一化（与生产路径一致）")
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def _fake_sentence_transformers(factory=None) -> types.ModuleType:
    """伪造 sentence_transformers 模块（理由见本文件模块 docstring：真包会把
    torch 冷启动拖进来，实测接近 30s，还会发 HF 联网请求）。"""
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = factory or (lambda model_id, **kw: _FakeSentenceTransformerModel(kw.get("device", "cpu")))  # type: ignore[attr-defined]
    return module


class _CountingArbiter:
    """ResourceArbiter 的计数代理：行为全部转发给真仲裁器，只额外数
    acquire/release 次数——"两次加载只 acquire 一次、归零才 release"必须能被
    数出来，不能靠人肉读代码确认。"""

    def __init__(self, inner: ResourceArbiter) -> None:
        self._inner = inner
        self.acquires = 0
        self.releases = 0

    def acquire(self, *args, **kwargs):
        self.acquires += 1
        return self._inner.acquire(*args, **kwargs)

    def release(self, *args, **kwargs):
        self.releases += 1
        return self._inner.release(*args, **kwargs)

    def holder_of(self, resource_id):
        return self._inner.holder_of(resource_id)


class _Ctx:
    """最小 PluginContext 替身（embedder 插件 on_load/on_enable/on_disable 只用到
    settings/storage/logger/resource_arbiter）。"""

    def __init__(self, logger: logging.Logger, arbiter) -> None:
        self.settings: dict = {}
        self.logger = logger
        self.resource_arbiter = arbiter
        self.storage = types.SimpleNamespace(file=lambda *a, **k: None)
        self.plugin_id = "official-embedder-bge-m3"


class _LeaseTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger(f"rag_redo.test.embed.lease.{self.id()}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(logging.NullHandler())
        self.arb = ResourceArbiter()
        self.counting = _CountingArbiter(self.arb)

    def _encoder(self, **kwargs) -> _RealEncoder:
        kwargs.setdefault("resource_arbiter", self.counting)
        kwargs.setdefault("cooldown_gate", _stub_gate_ready())
        kwargs.setdefault("logger", self.logger)
        return _RealEncoder(**kwargs)


class TestGpuLeaseFollowsRealResidency(_LeaseTestBase):
    """租约必须与模型真实驻留/卸载严格配对。"""

    def _loaded_encoder(self) -> _RealEncoder:
        enc = self._encoder()
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers()}), patch(
            _VRAM, return_value=8.0
        ):
            vectors = enc.encode(["你好世界"])
        self.assertEqual(len(vectors), 1)
        return enc

    def test_enable_without_loading_holds_no_lease(self):
        """插件启用只是"待命"：此刻没有任何模型在显存里，绝不能占名额
        （否则 WEMM/OCR-local 在显存空着的时候也抢不到"gpu:0"）。"""
        plugin = EmbedderPlugin()
        ctx = _Ctx(self.logger, self.counting)
        plugin.on_load(ctx)
        plugin.on_enable(ctx)
        self.addCleanup(plugin.on_disable, ctx)
        self.assertIsNone(plugin.embedder._encoder._model)
        self.assertEqual(self.counting.acquires, 0)
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID))

    def test_loaded_model_holds_lease_and_unload_returns_it(self):
        enc = self._loaded_encoder()
        self.assertEqual(self.arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID, "真在显存里就必须持名额")
        enc._unload()
        self.assertIsNone(enc._model)
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID), "真卸载了必须归还名额")

    def test_idle_unload_also_returns_the_lease(self):
        """空闲卸载守护线程是生产环境里最常见的卸载入口，它同样必须归还名额
        ——修复前模型早就不在显存里了，bge 却仍以优先级 100 永久占着名额，
        `navigate_knowledge` 只能一直回"GPU 忙"。"""
        enc = self._loaded_encoder()
        self.assertEqual(self.arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        enc._last_use = time.time() - 10_000
        enc.idle_check()
        self.assertIsNone(enc._model)
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID))

    def test_two_embeds_acquire_once_and_release_once(self):
        """引用计数：连续两次 encode 只 acquire 一次（模型并没有被重新加载），
        卸载归零时才 release 一次。"""
        enc = self._encoder()
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers()}), patch(
            _VRAM, return_value=8.0
        ):
            enc.encode(["a"])
            enc.encode(["b"])
        self.assertEqual(self.counting.acquires, 1, "第二次 encode 不该重复申请名额")
        self.assertEqual(self.counting.releases, 0)
        enc._unload()
        self.assertEqual(self.counting.releases, 1, "卸载归零后必须归还一次")
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID))

    def test_concurrent_embeds_hold_two_refs_but_still_one_lease(self):
        """并发 encode 的引用计数必须是真的并发计数（引用在 `self._lock` 之外
        取），且两个用户共用同一份名额。"""
        enc = self._encoder()
        model_entered = threading.Event()
        release_model = threading.Event()
        seen_refs: list[int] = []

        def _slow_encode(*args, **kwargs):
            seen_refs.append(enc._lease_refs)
            model_entered.set()
            self.assertTrue(release_model.wait(timeout=10), "测试自身的等待被超时打断")
            return [[1.0, 0.0, 0.0, 0.0]]

        model = _FakeSentenceTransformerModel()
        model.encode = _slow_encode  # type: ignore[method-assign]
        factory = lambda model_id, **kw: model  # noqa: E731

        errors: list[BaseException] = []

        def _encode(tag: str) -> None:
            try:
                enc.encode([tag])
            except BaseException as exc:  # noqa: BLE001 - 收集到断言里报告
                errors.append(exc)

        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(factory)}), patch(
            _VRAM, return_value=8.0
        ):
            first = threading.Thread(target=_encode, args=("a",), name="lease-t1")
            first.start()
            self.assertTrue(model_entered.wait(timeout=10), "第一个 encode 没进到模型")
            second = threading.Thread(target=_encode, args=("b",), name="lease-t2")
            second.start()
            # 第二个线程在 self._lock 之外取到自己的引用后阻塞在 self._lock 上，
            # 等它取到才能观察到"两个引用并存"这个事实。
            deadline = time.time() + 10
            while enc._lease_refs < 2 and time.time() < deadline:
                time.sleep(0.01)
            concurrent_refs = enc._lease_refs
            release_model.set()
            first.join(timeout=10)
            second.join(timeout=10)
        self.assertEqual(errors, [], f"并发 encode 抛了异常：{errors!r}")
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertGreaterEqual(concurrent_refs, 2, "两个并发 encode 必须同时持有引用")
        self.assertEqual(seen_refs[0], 2, "模型被使用的那一刻，两个引用都在")
        self.assertEqual(self.counting.acquires, 1, "并发也只申请一次名额")
        self.assertEqual(enc._lease_refs, 1, "两个用户都结束后只剩'模型驻留'这一个引用")
        self.assertEqual(self.arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)

    def test_sibling_holder_does_not_return_lease_it_never_took(self):
        """embedder 与 reranker 共用一个 holder_id：后者是幂等续期、并没有
        "真拿到"名额，所以它停用时不能把前者（仍驻留显存）的名额还掉。"""
        owner = self._loaded_encoder()
        self.assertTrue(owner._slot_owner)
        sibling = self._encoder()
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers()}), patch(
            _VRAM, return_value=8.0
        ):
            sibling.encode(["b"])
        self.assertFalse(sibling._slot_owner, "同 holder_id 的幂等续期不算真拿到名额")
        sibling.release_gpu_slot()
        self.assertIsNone(sibling._model)
        self.assertEqual(
            self.arb.holder_of(GPU_RESOURCE_ID),
            GPU_HOLDER_ID,
            "bge 还在显存里，同 holder_id 的兄弟模型禁用不能把名额还掉",
        )
        self.assertIsNotNone(owner._model)

    def test_preempted_side_keeps_lease_bookkeeping_consistent(self):
        """被更高优先级抢占（on_preempt 卸载）之后，本对象不得再以为自己
        持有名额——否则禁用时的收口会把新持有者的名额误还。"""
        enc = self._loaded_encoder()
        self.assertTrue(
            self.arb.acquire(
                GPU_RESOURCE_ID, "somebody-else", priority=1000, on_preempt=enc._unload
            )
        )
        # 在位者被抢时跑的是它自己登记的回调（2026-09-30 起是挪内存 `_park`，BC-11）
        self.assertFalse(enc._gpu_resident)
        self.assertTrue(enc._parked)
        self.assertFalse(enc._slot_owner)
        enc.release_gpu_slot()
        self.assertEqual(self.arb.holder_of(GPU_RESOURCE_ID), "somebody-else")


class TestPreemptedModelWaitsInRam(_LeaseTestBase):
    """被抢显卡时挪内存（BC-11，2026-09-30 操作者确认）：只有被抢这一条挪内存，其余卸载
    入口（空闲满时限 / 手动释放显存 / 插件停用）仍然整个卸掉；搬来搬去失败不能把状态搞乱。"""

    def _loaded(self, model=None):
        model = model or _FakeSentenceTransformerModel("cuda")
        enc = self._encoder()
        with patch.dict(
            sys.modules, {"sentence_transformers": _fake_sentence_transformers(lambda mid, **kw: model)}
        ), patch(_VRAM, return_value=8.0):
            enc.encode(["a"])
        self.assertTrue(enc._gpu_resident)
        return enc, model

    def test_preempt_frees_the_gpu_and_the_lease_but_keeps_the_model(self):
        enc, model = self._loaded()
        self.assertTrue(self.arb.acquire(GPU_RESOURCE_ID, "somebody-else", priority=1000, on_preempt=enc._park))
        self.assertIs(enc._model, model)
        self.assertEqual(model.device, "cpu")
        self.assertFalse(enc._gpu_resident)
        self.assertFalse(enc._slot_owner)
        self.assertEqual(enc._lease_refs, 0, "离开显存就不再持'驻留'引用")
        self.assertEqual(self.arb.holder_of(GPU_RESOURCE_ID), "somebody-else")

    def test_model_parked_in_ram_is_still_fully_unloaded_after_idle_timeout(self):
        """操作者要求：满 5 分钟空闲照旧整个卸载——暂存在内存里的也一样。"""
        enc, _model = self._loaded()
        enc._park()
        enc._last_use = time.time() - 10_000
        enc.idle_check()
        self.assertIsNone(enc._model)
        self.assertFalse(enc._parked)

    def test_manual_release_still_unloads_entirely(self):
        """操作者要求：手动「释放显存」（BC-16）维持整个卸载，不挪内存。"""
        enc, _model = self._loaded()
        enc.release_gpu_slot()
        self.assertIsNone(enc._model)
        self.assertFalse(enc._parked)
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID))

    def test_parking_failure_falls_back_to_full_unload(self):
        model = _FakeSentenceTransformerModel("cuda")
        model.fail_to.add("cpu")
        enc, _ = self._loaded(model)
        enc._park()
        self.assertIsNone(enc._model, "挪不动就整个卸载，让路本身不能失败")
        self.assertFalse(enc._gpu_resident)
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID))

    def test_failing_to_move_back_to_gpu_cools_down_and_keeps_serving_on_cpu(self):
        gate = _StubRecordingGate()
        model = _FakeSentenceTransformerModel("cuda")
        enc = self._encoder(cooldown_gate=gate)
        with patch.dict(
            sys.modules, {"sentence_transformers": _fake_sentence_transformers(lambda mid, **kw: model)}
        ), patch(_VRAM, return_value=8.0):
            enc.encode(["a"])
            enc._park()
            model.fail_to.add("cuda")
            vectors = enc.encode(["b"])
        self.assertEqual(len(vectors), 1, "搬不回显卡也照样出结果（在 CPU 上）")
        self.assertIs(enc._model, model, "不重新加载")
        self.assertEqual(model.device, "cpu")
        self.assertFalse(enc._parked)
        self.assertFalse(enc._gpu_resident)
        self.assertEqual(len(gate.cooldowns), 1, "搬回失败按 CUDA 失败进冷却期")
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID), "落在 CPU 上绝不留名额")


class TestGpuLeaseNotLeakedOnDegradePaths(_LeaseTestBase):
    """三条降级路径（冷却期 / 名额抢不到 / 显存不足）加上加载彻底失败，
    都不许留下未释放的名额——留着等于对外声明"我占着显存"而实际没占
    （AGENTS.md §7）。"""

    def test_cooldown_path_never_acquires(self):
        enc = self._encoder(cooldown_gate=_StubGateNotReady())
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers()}), patch(
            _VRAM, return_value=8.0
        ):
            enc.encode(["a"])
        self.assertEqual(self.counting.acquires, 0, "冷却期内根本不碰名额")
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID))
        self.assertIsNotNone(enc._model, "降级 CPU 仍要正常加载出模型（降级不是失败）")
        self.assertFalse(enc._gpu_resident)

    def test_slot_contention_path_never_owns_the_lease(self):
        self.assertTrue(self.arb.acquire(GPU_RESOURCE_ID, "busy-indexer", priority=1000))
        enc = self._encoder()
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers()}), patch(
            _VRAM, return_value=8.0
        ):
            enc.encode(["a"])
        self.assertEqual(self.arb.holder_of(GPU_RESOURCE_ID), "busy-indexer", "抢不到就不许动别人的名额")
        self.assertFalse(enc._slot_owner)
        enc.release_gpu_slot()
        self.assertEqual(self.arb.holder_of(GPU_RESOURCE_ID), "busy-indexer")

    def test_insufficient_vram_after_evict_returns_the_lease(self):
        """显存不足→让路→复核仍不足→降级 CPU：名额拿到了却不用，必须当场
        归还。修复前这条路径一路挂到插件 on_disable，WEMM 从此永远抢不到。"""
        enc = self._encoder()
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers()}), patch(
            _VRAM, side_effect=[1.0, 1.0]
        ):
            enc.encode(["a"])
        self.assertEqual(self.arb.holder_of(GPU_RESOURCE_ID), None, "降级 CPU 后绝不能占着名额")
        self.assertIsNotNone(enc._model, "CPU 上模型照常加载（降级不是失败）")

    def test_cuda_load_failure_degrading_to_cpu_returns_the_lease(self):
        calls: list[str] = []

        def _factory(model_id, **kwargs):
            device = kwargs.get("device")
            calls.append(str(device))
            if device == "cuda":
                raise RuntimeError("CUDA out of memory")
            return _FakeSentenceTransformerModel(device)

        enc = self._encoder()
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)}), patch(
            _VRAM, return_value=8.0
        ):
            enc.encode(["a"])
        self.assertIn("cuda", calls)
        self.assertIsNotNone(enc._model)
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID), "模型在 CPU 上，名额必须已归还")

    def test_total_load_failure_returns_the_lease(self):
        def _factory(model_id, **kwargs):
            raise RuntimeError("模型文件损坏")

        enc = self._encoder()
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)}), patch(
            _VRAM, return_value=8.0
        ), self.assertLogs(self.logger, level="INFO"):
            with self.assertRaises(RuntimeError):
                enc.encode(["a"])
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID), "加载彻底失败也绝不能留下名额")

    def test_cpu_degraded_model_is_not_reloaded_on_every_call(self):
        """降级到 CPU 之后模型必须**留在内存里**接着用，绝不能"每次调用重载一遍"。

        这条是引用计数收尾逻辑写错时最隐蔽的表现：模型在 CPU 上没有"驻留"
        引用，于是每次 `encode()` 结束引用归零，如果那一刻顺手把模型卸了，
        用户侧没有任何报错，只是"检索/索引慢得离谱"——每查一次就重新加载
        几 GB 权重。名额该还的还得还（上一条已经钉住），但模型不能动。"""
        loads: list[str] = []

        def _factory(model_id, **kwargs):
            device = kwargs.get("device", "cpu")
            loads.append(str(device))
            return _FakeSentenceTransformerModel(device)

        enc = self._encoder()
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)}), patch(
            _VRAM, side_effect=[1.0, 1.0]
        ):
            enc.encode(["a"])
            enc.encode(["b"])
        self.assertEqual(loads, ["cpu"], "模型只应装载一次：引用归零不是卸载模型的许可")
        self.assertIsNotNone(enc._model)
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID))


class TestEmbedderModelsDir(_LeaseTestBase):
    """BC-17：模型存放目录可配置（默认项目内 models/），每次加载模型时现读设置。"""

    def _load_recording_cache_folders(self, enc_or_plugin_call, folders: list) -> None:
        def _factory(model_id, **kwargs):
            folders.append(kwargs.get("cache_folder"))
            return _FakeSentenceTransformerModel(kwargs.get("device", "cpu"))

        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)}), patch(
            _VRAM, return_value=8.0
        ):
            enc_or_plugin_call()

    def test_configured_models_dir_is_passed_to_the_factory_as_cache_folder(self):
        import tempfile
        from pathlib import Path

        target = Path(tempfile.gettempdir()) / "rag_redo_models_a"
        enc = self._encoder(models_dir=lambda: target)
        folders: list = []
        self._load_recording_cache_folders(lambda: enc.encode(["a"]), folders)
        self.assertEqual(folders, [str(target)], "模型必须从用户配置的目录加载")

    def test_without_a_models_dir_reader_nothing_extra_is_passed(self):
        """旧调用方式（不传读取函数）行为不变：不带 cache_folder，走 HuggingFace 默认缓存。"""
        enc = self._encoder()
        folders: list = []
        self._load_recording_cache_folders(lambda: enc.encode(["a"]), folders)
        self.assertEqual(folders, [None])

    def test_plugin_reads_the_setting_afresh_on_every_model_load(self):
        """用户在设置页改了路径，下一次重新加载模型就用新路径，不用重启。"""
        import tempfile
        from pathlib import Path

        first = Path(tempfile.gettempdir()) / "rag_redo_models_first"
        second = Path(tempfile.gettempdir()) / "rag_redo_models_second"
        plugin = EmbedderPlugin()
        ctx = _Ctx(self.logger, self.counting)
        ctx.settings["models_dir"] = str(first)
        plugin.on_load(ctx)
        plugin.on_enable(ctx)
        self.addCleanup(plugin.on_disable, ctx)
        folders: list = []
        self._load_recording_cache_folders(lambda: plugin.embed_texts(["a"]), folders)
        plugin.release_gpu()
        ctx.settings["models_dir"] = str(second)
        self._load_recording_cache_folders(lambda: plugin.embed_texts(["b"]), folders)
        self.assertEqual(folders, [str(first), str(second)])

    def test_plugin_defaults_to_the_project_models_folder(self):
        from core.paths import models_dir as default_models_dir

        plugin = EmbedderPlugin()
        ctx = _Ctx(self.logger, self.counting)  # settings 为空 = 没配置
        plugin.on_load(ctx)
        plugin.on_enable(ctx)
        self.addCleanup(plugin.on_disable, ctx)
        folders: list = []
        self._load_recording_cache_folders(lambda: plugin.embed_texts(["a"]), folders)
        self.assertEqual(folders, [str(default_models_dir(""))])
        self.assertEqual(default_models_dir("").name, "models", "默认必须是项目内的 models 文件夹")


class TestEmbedderPluginLeaseLifecycle(_LeaseTestBase):
    """插件生命周期钩子：启用不占名额，停用/卸载必须收口且幂等。"""

    def _plugin_with_loaded_model(self) -> tuple[EmbedderPlugin, _Ctx, _RealEncoder]:
        plugin = EmbedderPlugin()
        ctx = _Ctx(self.logger, self.counting)
        plugin.on_load(ctx)
        plugin.on_enable(ctx)
        enc = plugin.embedder._encoder
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers()}), patch(
            _VRAM, return_value=8.0
        ):
            plugin.embed_texts(["a"])
        self.assertEqual(self.arb.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        return plugin, ctx, enc

    def test_on_unload_returns_lease_without_on_disable(self):
        """宿主热重载可能不经过 on_disable 直接 on_unload——这条路径也必须收口
        （修复前 embedder 的 on_unload 只把 embedder 置 None，名额永久挂着）。"""
        plugin, ctx, enc = self._plugin_with_loaded_model()
        plugin.on_unload(ctx)
        self.assertIsNone(enc._model)
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID))

    def test_disable_then_unload_then_disable_is_idempotent(self):
        plugin, ctx, enc = self._plugin_with_loaded_model()
        plugin.on_disable(ctx)
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID))
        plugin.on_unload(ctx)
        plugin.on_disable(ctx)  # 幂等：重复收口不许抛异常、不许误动别人的名额
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID))
        self.assertIsNone(enc._model)

    def test_release_gpu_unloads_model_but_keeps_plugin_usable(self):
        """手动"释放显存"按钮用（core/pipeline.py::release_gpu_memory，
        2026-09-29 新能力，BC-16）——跟 on_disable 一样把模型真的卸掉、名额
        归还，但**不是禁用插件**：不停空闲卸载守护线程、不用户手动重新
        enable，下一次真正编码时必须透明地把模型重新装回来（这正是它和
        on_disable 的区别——on_disable 之后这个插件在下次真正用到之前不会
        自己再工作）。"""
        plugin, ctx, enc = self._plugin_with_loaded_model()
        plugin.release_gpu()
        self.assertIsNone(enc._model, "释放显存必须真的把模型卸掉")
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID), "释放显存必须归还GPU名额")
        # 插件本身仍然可用：不需要重新 on_enable，下次编码透明重新加载。
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers()}), patch(
            _VRAM, return_value=8.0
        ):
            plugin.embed_texts(["again"])
        self.assertIsNotNone(enc._model, "释放显存之后插件必须还能正常工作，不需要重新启用")

    def test_disable_with_outstanding_refs_unloads_first_then_returns_lease(self):
        """还有人在用（残留引用）也照样收尾：先卸载模型、再归还名额，并留日志
        说明当时还有多少引用——插件都要被停用了，留下一个"占着最高优先级名额
        但模型已经不在"的幽灵 holder 远比提前收尾恶劣。"""
        plugin, ctx, enc = self._plugin_with_loaded_model()
        enc._hold_user_ref()  # 模拟一条还在跑的 encode
        with self.assertLogs(self.logger, level="INFO") as captured:
            plugin.on_disable(ctx)
        self.assertIn("引用", "\n".join(captured.output))
        self.assertIsNone(enc._model, "有残留引用也必须先把模型卸掉")
        self.assertIsNone(self.arb.holder_of(GPU_RESOURCE_ID), "卸载之后再归还名额")
        self.assertEqual(enc._lease_refs, 0)


class TestGpuLeaseCrossProcess(unittest.TestCase):
    """跨进程语义：两个 `ResourceArbiter` 实例共用一个 lock_dir 就是两个进程
    （各自独立的进程内状态 + 同一份文件锁/租约文件），用它复现真实的
    "GUI 与 MCP 同时跑检索侧"场景。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lock_dir = self.tmp / "resource_locks"
        self.logger = logging.getLogger("rag_redo.test.embed.lease.xproc")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(logging.NullHandler())

    def _arbiter(self) -> ResourceArbiter:
        # preempt_timeout 压到 5s：既要能等到对方让路，又不能让用例在回归时
        # 挂在默认的 15s 上；poll 收紧让"另一个进程写进来的抢占请求"能被
        # 后台监控线程很快看到（真实运行里是 0.05s）。
        return ResourceArbiter(lock_dir=self.lock_dir, preempt_timeout_s=5.0, poll_interval_s=0.01)

    def _encoder(self, arb) -> _RealEncoder:
        return _RealEncoder(resource_arbiter=arb, cooldown_gate=_stub_gate_ready(), logger=self.logger)

    def _embed(self, enc: _RealEncoder, text: str) -> None:
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers()}), patch(
            _VRAM, return_value=8.0
        ):
            enc.encode([text])

    def test_scenario_both_sides_loaded_serialize_without_hang(self):
        """场景一：两边都真的把 bge 装进了显存。第二个进程要名额时必须让第一
        个（on_preempt 卸载）让路——串行使用显存，不崩、不永久挂死。"""
        arb_gui, arb_mcp = self._arbiter(), self._arbiter()
        enc_gui = self._encoder(arb_gui)
        self._embed(enc_gui, "gui")
        self.assertEqual(arb_gui.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        enc_mcp = self._encoder(arb_mcp)
        started = time.time()
        self._embed(enc_mcp, "mcp")  # 有界等待：preempt_timeout_s=5s
        self.assertLess(time.time() - started, 20.0, "跨进程让路必须是有界的，不得永久挂死")
        # 让路 = 真的离开显存（2026-09-30 起挪到内存暂存，不再整个扔掉，BC-11）
        self.assertFalse(enc_gui._gpu_resident, "让路方必须真的让出显存")
        self.assertEqual(enc_gui._model.device, "cpu", "让路方的模型挪到了内存")
        self.assertIsNone(arb_gui.holder_of(GPU_RESOURCE_ID), "让路方同时交出名额")
        self.assertEqual(arb_mcp.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        # 反方向同样成立：GUI 下一次检索再把名额抢回来，串行而不是双份常驻。
        self._embed(enc_gui, "gui-again")
        self.assertFalse(enc_mcp._gpu_resident)
        self.assertEqual(enc_mcp._model.device, "cpu")
        self.assertEqual(arb_gui.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        self.assertIsNone(arb_mcp.holder_of(GPU_RESOURCE_ID))

    def test_preempted_model_waits_in_ram_and_comes_back_without_reloading(self):
        """2026-09-30 操作者确认（BC-11）：在界面里搜过、再点重建（索引进程要显卡）、索引完
        再搜——改造前让路 = 整个卸载，再搜要重新加载 10~40 秒。现在被抢时挪到内存，再用时
        搬回显卡，不重新加载。"""
        loads = []

        def _factory(model_id, **kw):
            model = _FakeSentenceTransformerModel(kw.get("device", "cpu"))
            loads.append(model)
            return model

        arb_gui, arb_worker = self._arbiter(), self._arbiter()
        enc_gui = self._encoder(arb_gui)
        enc_worker = self._encoder(arb_worker)
        with patch.dict(sys.modules, {"sentence_transformers": _fake_sentence_transformers(_factory)}), patch(
            _VRAM, return_value=8.0
        ):
            enc_gui.encode(["界面里搜一次"])
            gui_model = enc_gui._model
            enc_worker.encode(["索引进程要显卡"])
            self.assertIs(enc_gui._model, gui_model, "被抢时模型留在内存里，不扔")
            self.assertTrue(enc_gui._parked)
            self.assertEqual(gui_model.moves, ["cpu"])
            enc_gui.encode(["索引完再搜"])
        self.assertEqual(len(loads), 2, "两边各只加载过一次，再搜不重新加载")
        self.assertIs(enc_gui._model, gui_model)
        self.assertEqual(gui_model.moves, ["cpu", "cuda"], "再用时从内存搬回显卡")
        self.assertFalse(enc_gui._parked)
        self.assertTrue(enc_gui._gpu_resident)
        self.assertEqual(arb_gui.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        self.assertEqual(enc_worker._model.device, "cpu", "这回轮到索引进程那边挪到内存")

    def test_scenario_other_side_only_enabled_lets_wemm_take_the_lease(self):
        """场景二：另一边只是"插件启用、还没检索过"→ 它不占名额 → WEMM
        （priority=10 + preempt_equal）能正常拿到名额做页级导航。"""
        arb_gui, arb_other = self._arbiter(), self._arbiter()
        plugin = EmbedderPlugin()
        ctx = _Ctx(self.logger, arb_gui)
        plugin.on_load(ctx)
        plugin.on_enable(ctx)
        self.addCleanup(plugin.on_disable, ctx)
        self.assertIsNone(arb_other.holder_of(GPU_RESOURCE_ID))
        self.assertTrue(
            arb_other.acquire(
                GPU_RESOURCE_ID, "official-visual-wemm", priority=10, preempt_equal=True
            ),
            "本进程没加载模型就不该占名额，WEMM 必须能拿到",
        )
        self.assertEqual(arb_other.holder_of(GPU_RESOURCE_ID), "official-visual-wemm")

    def test_scenario_wemm_gets_lease_back_after_this_side_idle_unloaded(self):
        """场景二的续集：本进程加载过、随后空闲卸载了（显存真的空了），名额
        必须回到空闲状态，WEMM 才拿得到。修复前 bge 会以优先级 100 永久占着
        名额，`navigate_knowledge` 在 GUI+MCP 同开时只能一直回"GPU 忙"。"""
        arb_gui, arb_other = self._arbiter(), self._arbiter()
        enc = self._encoder(arb_gui)
        self._embed(enc, "gui")
        self.assertEqual(arb_gui.holder_of(GPU_RESOURCE_ID), GPU_HOLDER_ID)
        self.assertFalse(
            arb_other.acquire(GPU_RESOURCE_ID, "official-visual-wemm", priority=10, preempt_equal=True),
            "模型真的在显存里时，WEMM 抢不到是正确行为（检索侧优先）",
        )
        enc._last_use = time.time() - 10_000
        enc.idle_check()
        self.assertIsNone(enc._model)
        self.assertTrue(
            arb_other.acquire(GPU_RESOURCE_ID, "official-visual-wemm", priority=10, preempt_equal=True),
            "模型已卸载、显存已释放，WEMM 必须能拿到名额",
        )
        self.assertEqual(arb_other.holder_of(GPU_RESOURCE_ID), "official-visual-wemm")


if __name__ == "__main__":
    unittest.main()
