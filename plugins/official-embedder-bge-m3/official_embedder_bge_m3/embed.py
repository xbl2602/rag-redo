"""BGE-M3 向量化（sentence-transformers）。

**懒加载契约**：`import sentence_transformers` 和真实下载/加载 BGE-M3
权重（几GB），都推迟到第一次真正需要编码文本时才发生，绝不在模块导入期
或插件 `on_load`/`on_enable` 阶段触发。理由两条：

1. 插件的发现/加载/启用生命周期本身不应该等价于"把几GB模型吃进内存"——
   这两件事没有必然联系，用户只是想让这个插件"待命"，不代表现在就要付出
   模型加载的时间/显存代价。
2. 单测可以注入假 encoder（同旧 obsidian-rag 项目"索引集成测试用假编码器
   numpy 零向量，绝不碰真实模型"的纪律——见旧 AGENTS.md 测试纪律一节），
   不需要在开发机/CI 上背几GB的模型下载就能验证这层逻辑本身对不对。

真实设备上第一次调用 embed() 才会触发下载+加载，和旧项目 README"第一次
搜索要下载模型，等几十秒到几分钟"的用户预期一致，这不是回归，是延续。

**GPU 生命周期管理（2026-09-23 补齐，按 obsidian-rag/index.py 真实行为
移植）**：旧项目里 bge-m3/reranker 是"检索侧"——有 CUDA 就优先用 CUDA
（大幅提速），且相对 WEMM/MinerU 这类"按需视觉/OCR"消费者有更高的调度
优先级（8GB 卡上没法共存时，WEMM/MinerU 让路，不是反过来）；同时检索侧
自己也会在空闲一段时间后主动卸载模型释放显存（默认300s，任何新调用会
刷新这个计时，不需要额外的"是否有任务在跑"判定——见 IdleUnloadMixin 的
用法说明）。这里用 `core.gpu_arbiter`（fail-open 的 VRAM 探测）+
`core.resource_arbiter`（"gpu:0"名额仲裁，高优先级=100，压过
official-visual-wemm/official-ocr-mineru-local 的10）实现。对齐 obsidian-rag
检索侧的真实行为：拿到名额后直接加载，**不做 VRAM 阻塞等待**——旧项目里
"等不到显存就放弃本条请求"的 wait 语义只属于 WEMM/MinerU 服务端
（wemm_server.py:131-134 / mineru_server.py:124-127），检索侧是抢占式的
（index.py::_vram_maybe_evict_wemm 是主动驱逐检查，不是阻塞等待）。
具体的"设备选择+空闲卸载"逻辑封装成 GpuAwareModel 基类，和
official-reranker/rerank.py 共用同一份实现方式（因为插件之间不能互相
import，这个基类在两个插件里各自有一份，是刻意的小重复，不是遗漏——同
core/subprocess_service.py::resolve_plugin_python 的 docstring 提到的
"跨插件隔离边界的小工具函数该复制不该硬造共享桥梁"这条先例）。

**CUDA 冷却期状态机（2026-09-25 按旧项目原行为补齐，消除此前"单次降级
永不切回"的已知简化）**：CUDA 失败（初始化异常/连续慢批疑似共享显存溢出）
→ 进入冷却期（默认 300s，设置项 `cuda_cooldown_seconds`，对齐旧项目
config.py 同名项）+ 诊断落盘 device_state.json；冷却期内直接用 CPU，到期后
毫秒级轻量探测（64MB 显存分配）通过才尝试 CUDA——显存恢复后最多等一个
冷却期自动切回。状态机实现在 `core.gpu_arbiter.CudaCooldownGate`（embedder
和 reranker 是同一个"检索侧 GPU 消费群体"，冷却/恢复状态理应一致），各
语义对应旧项目 index.py 的 _cuda_probe/_cuda_ready/_cooldown_cuda/
_report_device/fallback_to_cpu。慢批检测（单批 >30s 疑似 Windows WDDM
共享显存溢出，连续两批主动降级）同样对齐旧项目 encode_safe。
"""
from __future__ import annotations

import threading
import time
from typing import Protocol

from core import gpu_arbiter
from core.gpu_arbiter import CudaCooldownGate

MODEL_VERSION = "BAAI/bge-m3"
GPU_RESOURCE_ID = "gpu:0"
GPU_HOLDER_ID = "official-text-retrieval-gpu"  # 和 official-reranker 共用同一个 holder_id，见模块 docstring
GPU_PRIORITY = 100  # 检索侧优先，压过 WEMM/OCR-local 的10（对齐旧项目"检索优先抢占"）
IDLE_UNLOAD_SECONDS = 300  # 空闲卸载模型释放显存，对齐旧项目默认 gpu_idle_unload_seconds
SLOW_BATCH_SECONDS = 30.0  # CUDA 单批编码超过此值视为疑似共享显存溢出（对齐旧项目 encode_safe）


class Encoder(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class _RealEncoder:
    """真实的 sentence-transformers 封装。构造它本身不加载任何东西，只有
    第一次调用 encode() 才会真正 import + 加载模型权重（懒加载），且加载
    前会做 GPU 名额仲裁 + 设备选择（见模块 docstring）。"""

    def __init__(
        self,
        model_name: str = MODEL_VERSION,
        *,
        resource_arbiter=None,
        cooldown_gate: CudaCooldownGate | None = None,
        logger=None,
    ) -> None:
        self._model_name = model_name
        self._model = None
        self._resource_arbiter = resource_arbiter
        self._cooldown_gate = cooldown_gate if cooldown_gate is not None else CudaCooldownGate()
        self._logger = logger
        self._last_use = time.time()
        self._slow_batch_count = 0
        self._lock = threading.RLock()

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(message)

    def _ensure_loaded(self):
        with gpu_arbiter.GPU_LOCK:
            if self._model is None:
                from sentence_transformers import SentenceTransformer  # noqa: PLC0415 - 故意懒加载，见模块 docstring

                device = self._select_device()
                try:
                    self._model = SentenceTransformer(self._model_name, device=device)
                except Exception as exc:
                    # 对齐旧项目 get_model：CUDA 初始化失败 → 冷却 + 降级 CPU
                    # 重试一次（CPU 再失败才真的抛出）
                    if device != "cuda":
                        raise
                    self._cooldown_gate.cooldown(str(exc))
                    self._log(f"CUDA 初始化失败（{exc}），降级 CPU")
                    device = "cpu"
                    self._model = SentenceTransformer(self._model_name, device=device)
                self._log(f"BGE-M3模型已加载（device={device}）")
                self._cooldown_gate.report_device(device)
            self._last_use = time.time()
            return self._model

    def _select_device(self) -> str:
        """有 CUDA 就优先用（大幅提速）；加载前先向资源仲裁器申请"gpu:0"
        名额（高优先级，可能挤走正占着的 WEMM/OCR-local 子进程）。对齐
        obsidian-rag 检索侧（index.py::_vram_maybe_evict_wemm）的真实行为：
        拿到名额后**直接加载，不做 VRAM 阻塞等待**——旧项目里 wait_for_vram
        的"等不到就放弃本条请求"语义只属于低优先级的 WEMM/MinerU 服务端
        （wemm_server.py:131-134 / mineru_server.py:124-127），检索侧是抢占
        式的；仲裁/探测拿不到明确答案时不阻塞，直接尝试用 CUDA；真的初始化
        失败再退回 CPU（单次降级，不做冷却重试，见模块 docstring"已知的
        刻意简化"）。"""
        # 冷却期状态机取代裸 is_available：冷却期内直接 CPU，到期后轻量
        # 探测通过才尝试 CUDA（对齐旧项目 _cuda_ready）
        if not self._cooldown_gate.ready():
            return "cpu"
        if self._resource_arbiter is not None:
            acquired = self._resource_arbiter.acquire(
                GPU_RESOURCE_ID,
                GPU_HOLDER_ID,
                priority=GPU_PRIORITY,
                on_preempt=self._unload,
            )
            if not acquired:
                return "cpu"
        return "cuda"

    def release_gpu_slot(self) -> None:
        """插件 on_disable 时调用——卸载模型并把"gpu:0"名额还给仲裁器。
        名额的语义是"这个插件启用期间可能会用 GPU"（见 idle_check 的
        docstring），插件停用后这个前提不再成立：被禁用的检索侧若仍以
        高优先级持有名额，WEMM/OCR-local 就再也抢不到资源。"""
        self._unload()
        if self._resource_arbiter is not None:
            self._resource_arbiter.release(GPU_RESOURCE_ID, GPU_HOLDER_ID)

    def encode(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            model = self._ensure_loaded()
            started = time.time()
            result = model.encode(texts, normalize_embeddings=True).tolist()
            elapsed = time.time() - started
            self._last_use = time.time()
            self._check_slow_batch(elapsed)
            return result

    def _check_slow_batch(self, elapsed: float) -> None:
        """Windows WDDM 显存溢出会静默排入共享显存而不报错，只能靠耗时识别：
        单批 >SLOW_BATCH_SECONDS 告警，连续两批主动降级 CPU（对齐旧项目
        encode_safe 的慢批检测）。降级 = 卸载模型 + 进入冷却，下一次调用经
        _select_device 的冷却门自然落在 CPU 上。"""
        if elapsed > SLOW_BATCH_SECONDS:
            self._slow_batch_count += 1
            self._log(
                f"CUDA 单批编码耗时 {elapsed:.1f}s（>{SLOW_BATCH_SECONDS:.0f}s），"
                f"疑似共享显存溢出（连续 {self._slow_batch_count} 次）"
            )
            if self._slow_batch_count >= 2:
                self._log("连续慢批，主动降级 CPU，避免病态运行...")
                self._cooldown_gate.cooldown("slow-batch")
                self._unload()
                self._slow_batch_count = 0
        else:
            self._slow_batch_count = 0

    def idle_check(self) -> None:
        """由插件的空闲卸载守护线程周期调用——空闲超过 IDLE_UNLOAD_SECONDS
        就卸载模型释放显存；任何新的 encode() 调用都会刷新 _last_use，
        意味着正在跑的索引/搜索任务天然不会被中途卸载（不需要额外的"是否
        有任务在跑"标记，见模块 docstring）。不影响 resource_arbiter 的
        名额持有——名额代表"这个插件启用期间可能会用 GPU"，和"模型这一刻
        是否真的常驻显存"是两回事，同 official-visual-wemm 的分层设计。"""
        if IDLE_UNLOAD_SECONDS <= 0 or self._model is None:
            return
        if time.time() - self._last_use > IDLE_UNLOAD_SECONDS:
            with self._lock:
                if time.time() - self._last_use > IDLE_UNLOAD_SECONDS and self._model is not None:
                    self._unload_locked()

    def _unload(self) -> None:
        with gpu_arbiter.GPU_LOCK, self._lock:
            self._unload_locked()

    def _unload_locked(self) -> None:
        if self._model is None:
            return
        self._model = None
        try:
            import gc

            gc.collect()
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        self._log("BGE-M3模型空闲卸载，显存已释放")


class BGEM3Embedder:
    def __init__(self, encoder: Encoder | None = None, *, resource_arbiter=None, logger=None) -> None:
        # encoder=None 时用真实的（懒加载）；测试/CI 注入假 encoder。
        self._encoder = encoder if encoder is not None else _RealEncoder(resource_arbiter=resource_arbiter, logger=logger)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._encoder.encode(texts)

    def idle_check(self) -> None:
        """透传给真实 encoder；注入的假 encoder（测试用）没有这个方法，
        静默跳过——空闲卸载对假编码器没有意义。"""
        check = getattr(self._encoder, "idle_check", None)
        if check is not None:
            check()

    def release_gpu_slot(self) -> None:
        """透传给真实 encoder 的名额归还（on_disable 用）；注入的假 encoder
        没有这个方法时静默跳过，同 idle_check 的宽容语义。"""
        release = getattr(self._encoder, "release_gpu_slot", None)
        if release is not None:
            release()
