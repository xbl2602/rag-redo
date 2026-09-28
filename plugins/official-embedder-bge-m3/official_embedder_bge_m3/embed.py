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
检索侧的真实行为（obsidian-rag/index.py::_vram_maybe_evict_wemm 704-745 +
get_model 748）：**名额**决定"轮到谁"，**物理空闲显存**决定"现在装不装得下"，
两者都要看——拿到名额后仍要读一次 `vram_free_gb()` 和
`gpu_arbiter.BGE_MIN_VRAM_GB`（旧项目 gpu_arbiter.py:35 的 3.5GB）比对；
探测失败(None) fail-open 放行（AGENTS.md §7：探测可 fail-open），明确不足
则先让路再复核、复核仍不足就用 CPU，而不是硬上 CUDA 必然 OOM。
**不做 VRAM 阻塞等待**：旧项目里"等不到显存就放弃本条请求"的 wait 语义只属
于 WEMM/MinerU 服务端（wemm_server.py:131-134 / mineru_server.py:124-127），
检索侧是抢占式的。具体的"设备选择+空闲卸载"逻辑封装成 GpuAwareModel 基类，
和 official-reranker/rerank.py 共用同一份实现方式（因为插件之间不能互相
import，这个基类在两个插件里各自有一份，是刻意的小重复，不是遗漏——同
core/subprocess_service.py::resolve_plugin_python 的 docstring 提到的
"跨插件隔离边界的小工具函数该复制不该硬造共享桥梁"这条先例）。

**锁序（AGENTS.md §7，改这两个文件前必读）**：全局唯一顺序是
`self._lock → gpu_arbiter.GPU_LOCK → self._refs_lock → ResourceArbiter 内部锁`
（`core/resource_arbiter.py` 模块 docstring 有完整版），**绝不反向**。
而 on_preempt 回调（`self._unload`）必须按同一顺序拿锁——写成
`GPU_LOCK → self._lock` 就与 `encode()` 构成 ABBA，两把锁都无超时，
结果是冷加载线程与抢占回调线程互等到天荒地老，`encode()` 永不返回。
`self._refs_lock`（引用计数，见下）是这条链末端的**叶子锁**：只在函数内部
做几次数值加减，绝不在持有它的时候去拿任何别的锁、也绝不回调外部代码，
因此不可能构成新的锁环。

**跨进程抢占不许自锁（2026-09-27 收尾时实测出的死锁，已修）**：`GPU_LOCK` 按
`core/gpu_arbiter.py` 的持有期纪律只该包"判定+快速变更"，**绝不能包住一次
可能阻塞的跨进程名额申请**。原因：申请方阻塞在 `ResourceArbiter.acquire()`
里等对方让路时，仲裁器是在**被让方那个进程**的后台监控线程里执行
`on_preempt` 回调的（core/resource_arbiter.py `_process_preempt_requests`），
而回调要拿 `GPU_LOCK` 才能卸载模型腾显存。两个"进程"在同一台机器上同时
存在（GUI 与 MCP 同开正是这个拓扑）时，让方等 GPU_LOCK、申请方等让方
放手 = 互等到 `preempt_timeout_s` 超时，然后申请方**白白降级成 CPU**——
实测跨进程场景下 GUI 的 bge 被卸载了、MCP 那边却仍然拿不到名额，
`test_scenario_both_sides_loaded_serialize_without_hang` 钉的就是这个。
所以 `_select_device` 拆成三段：**非阻塞的显存/冷却判定持 `GPU_LOCK`** →
**名额申请在 `GPU_LOCK` 之外**（只持 `self._lock`）→ **让路后的复核持
`GPU_LOCK`**。这不是"新的锁序"，只是把一次长等待移出临界区，顺序依然是
`self._lock` 打头、绝不反向。

**GPU 租约语义 = "模型此刻真的在显存里"（2026-09-27 语义修正）**：改造前
"gpu:0"名额代表的是"**这个插件启用期间可能会用 GPU**"——第一次加载时
`acquire`，只在插件 `on_disable` 时 `release`，`release_gpu_slot` 的
docstring 把这写成了设计。后果比"显存够时多余 evict 一次 WEMM"严重得多：
bge-m3 一旦被加载过，本进程就永久以最高优先级（100）占着名额，而 WEMM 是
`priority=10 + preempt_equal`（见 official-visual-wemm/plugin.py）**永远抢不
到** → `navigate_knowledge` 只能一直回"GPU 忙"，页级视觉导航在 GUI+MCP 同开
时基本不可用。更根本的问题是：模型根本不在显存里，名额却在对外声明"我占着
GPU"——这与 AGENTS.md §7「锁不能代替真实的跨进程 holder 协调」直接冲突，也
偏离了旧项目"按需占用"的真实语义（obsidian-rag/index.py:748 `get_model`
只在**真要装模型时**才 evict 让路，:795 `release_model` 只在**真把模型卸掉**
时才释放；旧项目里根本没有"插件启用即占位"这回事）。

现在改为**与真实驻留严格配对**：

- 模型真的装进显存 → `acquire`（"选好设备"到"装载完成"之间是唯一一段
  "已声明但尚未驻留"的窗口，这是合理的：我们此刻正在装，而且这正是旧项目
  "先 evict 再加载"的同款时序）；
- 模型真的从显存卸载（空闲卸载 / 慢批降级 / 被抢占 / on_disable 强制收尾）
  → `release`；
- 最终落在 CPU 的三条路径（冷却期 / 名额抢不到 / 显存不足让路后仍不足）以及
  加载彻底失败，**绝不允许留下名额**——改造前第 3、4 条是真的会泄漏的
  （抢到了名额、决定用 CPU、然后再也不归还，WEMM 从此永远抢不到）。

"多处要用同一个模型"（两个并发检索请求同时要 embed）用**进程内引用计数**
处理：每次 `encode()` 持有一个"使用中"引用（`encode` 期间有效），模型真的
驻留显存再持有一个"驻留"引用；名额只在**第一个**引用出现时 acquire、**最后**
一个引用消失时 release，中间多少次 encode 都只 acquire 一次。

**引用归零只归还名额，绝不卸载模型**（这条是本轮修掉的一个真实缺陷）：模型
在 CPU 内存里时它压根没有"驻留"引用，于是**每一次** `encode()` 结束引用归零
都是"名额可以还了"的时刻。如果这里顺手把模型也卸掉（收尾期的写法），用户
看到的现象就是"每次检索都重新加载一遍几 GB 的模型"——降级到 CPU 之后索引
慢到不可用。模型什么时候真的离开内存，只有三个入口：空闲超时
（`idle_check`）、被别人抢占（`on_preempt`）、插件停用（`on_disable`/
`on_unload`）。名额归零和模型卸载是**两件事**，各走各的入口。

"只有真拿到名额的那个对象才归还名额"：embedder 与 reranker 共用一个
`GPU_HOLDER_ID`（同一次检索里两个模型本来就要一起住显存，旧项目也是 bge-m3 +
reranker 共存），仲裁器对同一 holder_id 的重复申请是幂等的——如果本对象这次
并不是"从别人手里真抢到"（进场时名额已经被同进程另一个模型拿着），那它禁用
时就**不**去 release，避免"一个插件禁用把另一个仍驻留的模型的名额还掉"。
反方向的口子（拿名额的那个先空闲卸载，而同进程另一个模型还驻留着）只由物理
显存判据兜底，这与旧项目等价（旧项目那里同样没有名额拦着，WEMM/MinerU 服务端
自己等显存），要彻底解决需要给"检索侧 GPU 消费群体"加一层核心才能提供的进程级
引用计数服务，超出本文件范围，如实记录不假装解决。

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
from core.model_loading import load_pretrained, param_dtype_mixed

MODEL_VERSION = "BAAI/bge-m3"
GPU_RESOURCE_ID = "gpu:0"
GPU_HOLDER_ID = "official-text-retrieval-gpu"  # 和 official-reranker 共用同一个 holder_id，见模块 docstring
GPU_PRIORITY = 100  # 检索侧优先，压过 WEMM/OCR-local 的10（对齐旧项目"检索优先抢占"）
IDLE_UNLOAD_SECONDS = 300  # 空闲卸载模型释放显存，对齐旧项目默认 gpu_idle_unload_seconds
SLOW_BATCH_SECONDS = 30.0  # CUDA 单批编码超过此值视为疑似共享显存溢出（对齐旧项目 encode_safe）
EMBED_BATCH_SIZE = 8     # 索引嵌入外层分批（旧 config.embed_batch_size 默认 8；逐批进度心跳+降低显存峰值）
ENCODE_BATCH_SIZE = 32   # model.encode 的期望批次（旧 config.encode_batch_size；实际值经 _auto_batch_size 按显存收紧）


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
        self._last_batch_cap: int | None = None  # 上次自动批次收紧值（仅变化时打日志，避免刷屏）
        self._device = "cpu"
        self._lock = threading.RLock()
        # ---- GPU 名额（租约）状态：语义 = "模型此刻真的在显存里"（模块 docstring）
        # _refs_lock 是引用计数的叶子锁：只做数值加减，持有期间绝不调任何外部
        # 代码、不再拿任何别的锁，因此不可能构成锁环（模块 docstring 的锁序一节）。
        self._refs_lock = threading.Lock()
        self._lease_refs = 0      # "gpu:0" 的在用引用数（每次 encode 一个 + 模型驻留一个）
        self._gpu_resident = False  # 手上这个模型是不是真的在显存里
        self._slot_owner = False    # 本对象是不是"真拿到名额"的那个（同进程另一个模型拿着时不归还）

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(message)

    def _log_degrade(self, reason: str, free_gb: float | None) -> None:
        """降级到 CPU 的原因必须可见（对齐 obsidian-rag/index.py:727/740 每次
        让路都有 log 的做法）：旧项目降级路径每次都打日志，用户看到"索引忽然
        变慢"至少知道是显存不够被别的模型挤了；rag-redo 此前两条 cpu 分支
        静默返回，用户无从判断，只能看到速度掉若干倍。

        **日志里只写显存数值、原因、holder_id 这类非敏感信息**，绝不写 API
        Key/凭据（AGENTS.md §7 敏感值不得进入日志）。"""
        if self._logger is None:
            return
        free_text = "探测失败(未知)" if free_gb is None else f"{free_gb:.1f}GB"
        self._logger.warning(
            "BGE-M3 本次用 CPU 编码：%s（空闲显存 %s，GPU 名额持有者 %s）",
            reason, free_text, self._resource_arbiter.holder_of(GPU_RESOURCE_ID)
            if self._resource_arbiter is not None else "无仲裁器",
        )

    def _ensure_loaded(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415 - 故意懒加载，见模块 docstring

            # 选设备里含唯一一次可能阻塞的名额申请，它**必须**在 GPU_LOCK 之外
            # （见模块 docstring 的"跨进程抢占不许自锁"一节：持着 GPU_LOCK 等
            # 别人让路，而让路的 on_preempt 回调又要 GPU_LOCK 才能卸载模型 =
            # 互等到超时，然后本进程白白降级 CPU）。
            device = self._select_device()
            try:
                with gpu_arbiter.GPU_LOCK:
                    try:
                        self._model = self._load_for_device(SentenceTransformer, device)
                    except Exception as exc:
                        # 对齐旧项目 get_model：CUDA 初始化失败 → 冷却 + 降级 CPU
                        # 重试一次（CPU 再失败才真的抛出）
                        if device != "cuda":
                            raise
                        self._cooldown_gate.cooldown(str(exc))
                        self._log(f"CUDA 初始化失败（{exc}），降级 CPU")
                        device = "cpu"
                        self._model = self._load_for_device(SentenceTransformer, device)
                    self._log(f"BGE-M3模型已加载（device={device}）")
                    self._device = device
                    self._cooldown_gate.report_device(device)
                    if self._model is not None and device == "cuda":
                        # 模型真的进了显存 → 持一个"驻留"引用（名额生命周期从此
                        # 与真实驻留严格配对，见模块 docstring）。
                        self._mark_gpu_resident()
            finally:
                # 落到 CPU / 加载彻底失败（含 CPU 重试再抛）时绝不能把名额
                # 留在手里：`finally` 保证异常路径也收口。改造前这条路径会
                # 泄漏名额——模型在 CPU 上跑、却对外声明占着 "gpu:0"。
                with gpu_arbiter.GPU_LOCK:
                    self._drop_slot_if_nothing_resident()
        self._last_use = time.time()
        return self._model

    def _load_for_device(self, factory, device: str):
        """对齐旧 index.py::_load_model：CUDA/CPU 均优先 fp16 减半显存
        （8GB 卡防 Windows 共享显存溢出——fp32 双模型常驻必顶满 8GB 触发
        WDDM 溢出，实测单批编码掉进分钟级），失败或参数 dtype 混搭回退
        fp32。加载本身走 core.model_loading.load_pretrained 的离线优先
        （问题56），不再触发 hub 联网核对。"""
        try:
            model = load_pretrained(
                factory, self._model_name, log=self._log,
                device=device, model_kwargs={"torch_dtype": "float16"},
            )
            if param_dtype_mixed(model):
                self._log(f"{device} fp16 加载后参数 dtype 混搭（Half/Float 并存），回退 fp32")
                raise RuntimeError("fp16 参数 dtype 混搭")
            return model
        except Exception as exc:
            self._log(f"{device} fp16 加载失败，回退 fp32：{exc}")
            return load_pretrained(factory, self._model_name, log=self._log, device=device)

    def _select_device(self) -> str:
        """有 CUDA 就优先用（大幅提速），但**先用物理显存判据筛一遍**，再向
        资源仲裁器申请"gpu:0"名额（高优先级，可能挤走正占着的 WEMM/OCR-local
        子进程）。对齐 obsidian-rag 检索侧（index.py::_vram_maybe_evict_wemm
        704-745 + get_model）的真实行为：

        1. 冷却期状态机取代裸 is_available（对齐旧项目 _cuda_ready）。
        2. 读一次真实空闲显存（core.gpu_arbiter.vram_free_gb，失败返回 None）：
           - 探测失败(None) → **fail-open 放行到 cuda**（AGENTS.md §7"探测可
             fail-open"；旧项目 gpu_arbiter.py 的 fail-open 铁律同款——判断不
             了就不阻塞、不放弃，绝不让仲裁本身卡死正常路径）。
           - 已知不足（< BGE_MIN_VRAM_GB，取自旧项目 gpu_arbiter.py:35）→ 先
             走名额仲裁请求让路（在位者会跑自己的 on_preempt 卸载模型），让路
             后**复核**一次；复核仍不足就降级 CPU，而不是硬上 CUDA 让它必然
             OOM（WDDM 共享显存溢出实测会把单批编码拖进分钟级）。
        3. 名额抢不到（更高优先级的在位者、15s 等不到让路）→ 降级 CPU。

        **仍然不做 VRAM 阻塞等待**：旧项目里 wait_for_vram 的"等不到就放弃
        本条请求"语义只属于低优先级的 WEMM/MinerU 服务端
        （wemm_server.py:131-134 / mineru_server.py:124-127），检索侧是抢占式
        的；core/gpu_arbiter.py::wait_for_vram 保留给那些服务端路径。

        与旧项目的差异（如实记录，AGENTS.md §3.3 要求偏离可见）：旧项目在
        evict 失败/服务不在时"静默放行"照旧硬上 CUDA，靠 OOM 后进冷却期兜底；
        rag-redo 在"明确探测到装不下"时直接用 CPU，理由是 AGENTS.md §7 把
        "探测失败"和"所有权未知"分开要求——fail-open 只适用于探测失败，不
        适用于探测已经给出确定答案的情形。

        **三段式：非阻塞判定持 GPU_LOCK → 名额申请在 GPU_LOCK 之外 → 让路
        后的复核持 GPU_LOCK**（锁纪律见模块 docstring 的"跨进程抢占不许自锁"）。
        中间那一段是全流程唯一会阻塞的步骤（最多 `preempt_timeout_s`），持着
        `GPU_LOCK` 等对方让路就是自锁：让方的 `on_preempt` 回调要拿
        `GPU_LOCK` 才能卸载模型。

        **名额的归属（`_slot_owner`）**：进场先读一次当前持有者，用来区分
        "这次是我真抢到了"和"同进程另一个检索侧模型已经拿着（共享
        GPU_HOLDER_ID 的幂等续期）"。只有前者才在归还时 release，避免一个
        插件禁用把另一个仍驻留的模型的名额还掉（rerank.py 模块 docstring 里
        记的同一条已知简化，现在关掉了一半）。`holder_of` 只是读仲裁器状态
        （仲裁器自己的 `_guard`），不涉及 GPU_LOCK，锁序仍然向前不回头。"""
        with gpu_arbiter.GPU_LOCK:
            if not self._cooldown_gate.ready():
                self._log_degrade("CUDA 处于冷却期（失败后自动探测恢复）", None)
                return "cpu"
            # 物理显存判据：探测失败(None) 与"明确不足"必须区分，不能混为一谈。
            free_gb = gpu_arbiter.vram_free_gb()
            need_room = free_gb is not None and free_gb < gpu_arbiter.BGE_MIN_VRAM_GB
        if self._resource_arbiter is not None:
            already_ours = self._resource_arbiter.holder_of(GPU_RESOURCE_ID) == GPU_HOLDER_ID
            # 刻意不持 GPU_LOCK：这一步会等对方（可能是同机另一个进程）的
            # on_preempt 回调卸完模型才返回。
            acquired = self._resource_arbiter.acquire(
                GPU_RESOURCE_ID,
                GPU_HOLDER_ID,
                priority=GPU_PRIORITY,
                on_preempt=self._unload,
            )
            if not acquired:
                self._log_degrade("GPU 名额抢占失败（有更高优先级持有者）", free_gb)
                return "cpu"
            self._slot_owner = not already_ours
        if need_room:
            # 名额拿到了，在位者已经被要求让路；复核真实显存（max_age=0 跳过
            # 缓存，拿到让路后的新鲜数字）。仍不足 → 降级，绝不硬上 CUDA。
            with gpu_arbiter.GPU_LOCK:
                free_after = gpu_arbiter.vram_free_gb(max_age=0.0)
            if free_after is not None and free_after < gpu_arbiter.BGE_MIN_VRAM_GB:
                # evict 失败/让路方没放手：旧项目这里是"静默放行硬上 CUDA"，
                # rag-redo 选择降级 CPU（理由见 docstring 末段），两种行为都
                # 必须留日志，否则用户只看到速度莫名掉下来。
                self._log_degrade(
                    f"空闲显存不足且让路后仍不足（需 ≥{gpu_arbiter.BGE_MIN_VRAM_GB}GB）",
                    free_after,
                )
                # 决定用 CPU 就绝不能把名额攥在手里：改造前这条分支拿完名额就
                # 直接 return，名额一路挂到插件 on_disable，WEMM/OCR-local 从此
                # 永远抢不到（AGENTS.md §7：名额必须等于真实占用）。注意此刻
                # 当前这条 encode 仍持有一个"使用中"引用，所以真正归还发生在
                # encode 收尾的引用归零处（_drop_user_ref），语义一样。
                self._drop_slot_if_nothing_resident()
                return "cpu"
            # 复核探测本身又失败(None)同样 fail-open 放行，只是没法报具体数字。
            self._log(
                f"让路后空闲显存 "
                + ("探测失败，按无法判断放行" if free_after is None else f"{free_after:.1f}GB")
                + f"（需求 {gpu_arbiter.BGE_MIN_VRAM_GB}GB），使用 CUDA"
            )
        return "cuda"

    # ---- GPU 名额引用计数（语义与锁序见模块 docstring）----

    def _mark_gpu_resident(self) -> None:
        """模型真的进了显存 → 持一个"驻留"引用。必须在 `self._lock` +
        `GPU_LOCK` 之内调用（它只在 `_ensure_loaded` 的装载成功分支出现）。"""
        with self._refs_lock:
            self._lease_refs += 1
        self._gpu_resident = True

    def _hold_user_ref(self) -> None:
        """一次 encode 取一个"使用中"引用。

        刻意在 `self._lock` **之外**取：这样两个并发 encode 的引用计数是真的
        并发计数（而不是被 `self._lock` 串行化成永远 1），名额"只 acquire
        一次、归零才 release"这条规则才是被真正验证过的事实而不是同义反复。"""
        with self._refs_lock:
            self._lease_refs += 1

    def _drop_user_ref(self) -> None:
        """归还一个"使用中"引用。

        归零时**只把名额收干净，绝不卸载模型**（这是本轮修掉的真实缺陷）：
        模型在 CPU 内存里时没有"驻留"引用，于是每一次 `encode()` 结束引用
        都会归零；如果这里图省事调用 `release_gpu_slot()`（它是"插件停用"
        的收口入口，无条件卸模型），降级到 CPU 之后就会变成"每次检索都重新
        加载一遍几 GB 模型"。模型什么时候真的离开内存，只由空闲超时、被抢占
        和插件停用三个入口决定（见 `idle_check` / `_unload` /
        `release_gpu_slot`）。
        """
        with self._refs_lock:
            if self._lease_refs > 0:
                self._lease_refs -= 1
            remaining = self._lease_refs
        if remaining:
            return
        # 此刻既没有使用者、也没有模型真的驻留显存 → 名额绝不该继续挂着
        # （降级路径的收口）。持 `self._lock` 调，和其它收口入口同一把锁。
        with self._lock:
            self._drop_slot_if_nothing_resident()

    def _force_reset_refs(self) -> int:
        """把引用计数清零，返回清零前的残留数量（on_disable 收口用：先看清还
        有多少人在用，再强制卸载 + 归还名额，绝不因为"还有人引用"就漏掉收尾）。"""
        with self._refs_lock:
            leftover = self._lease_refs
            self._lease_refs = 0
        return leftover

    def _drop_slot_if_nothing_resident(self) -> None:
        """没有任何引用、也没有模型真的驻留显存 → 把名额还回去。

        调用方一律已持 `self._lock`（加载路径还要持 `GPU_LOCK`）：归还动作要
        走 ResourceArbiter（`self._lock → GPU_LOCK → _refs_lock → 仲裁器`，
        绝不反向）。`_gpu_resident` 为真时这里什么都不做——模型还在显存里，
        名额本来就该留着。"""
        with self._refs_lock:
            busy = self._lease_refs > 0
        if not busy and not self._gpu_resident:
            self._release_slot_locked()

    def _release_slot_locked(self) -> None:
        """真正归还名额。**幂等**，且只归还"本对象真拿到过"的那一份——仲裁器
        侧还会再按 holder_id 复核一次（不是持有者时 release 是空操作），这里
        的标志位是为了不误伤同进程另一个检索侧模型持有的名额。"""
        if not self._slot_owner:
            return
        self._slot_owner = False
        if self._resource_arbiter is not None:
            self._resource_arbiter.release(GPU_RESOURCE_ID, GPU_HOLDER_ID)

    def release_gpu_slot(self) -> None:
        """**插件生命周期收口入口**（`on_disable` / `on_unload` 专用）：先把模型
        真的从显存里卸掉，再把"gpu:0"名额还给仲裁器。

        - **幂等**：重复调用是安全空操作（`_slot_owner` 标志位 + 仲裁器侧按
          holder_id 复核，两层幂等）。
        - **哪怕还有残留引用也照样收尾**：插件都要被停用了，留下一个占着最高
          优先级名额、模型却已经不在的幽灵 holder，比"提前抢跑一条 encode"
          严重得多；有残留引用时先记一条日志说明当时还有多少人在用。
        - **请求路径（`encode`）绝不许调它**：它无条件卸载模型，而"没人用了"
          和"该卸载模型"是两件事——引用归零只该归还名额，见 `_drop_user_ref`。
          （本轮实测踩过：这里曾被当成"通用清理"复用，结果模型在 CPU 上时
          每次 `encode()` 结束就被卸掉，索引慢到不可用。）
        - 只归还"本对象真拿到过"的那一份名额：同进程另一个检索侧模型
          （embedder/reranker 共用 `GPU_HOLDER_ID`）若仍驻留显存，本插件停用
          不能把它的名额还掉，见 `_release_slot_locked`。

        锁序 `self._lock → GPU_LOCK`（模块 docstring 第一节），和 `on_preempt`
        回调走的是同一条路，绝不反向。"""
        with self._lock, gpu_arbiter.GPU_LOCK:
            leftover = self._force_reset_refs()
            if leftover:
                self._log(
                    f"停用时仍有 {leftover} 个在用引用，先卸载模型再归还 GPU 名额"
                )
            self._unload_locked()
            self._release_slot_locked()

    def encode(self, texts: list[str]) -> list[list[float]]:
        self._hold_user_ref()
        try:
            with self._lock:
                model = self._ensure_loaded()
                out: list[list[float]] = []
                # 外层按 embed_batch_size 分批（旧 index.py:2249：提供逐批进度
                # 心跳并降低显存峰值），每批内 batch_size 走 _auto_batch_size
                # 按当前可用显存二次收紧（问题59-B4）——慢批检测因此逐批生效，
                # 与旧 encode_safe 的检测粒度一致。
                for start in range(0, len(texts), EMBED_BATCH_SIZE):
                    sub = texts[start:start + EMBED_BATCH_SIZE]
                    started = time.time()
                    result = model.encode(
                        sub, normalize_embeddings=True,
                        batch_size=self._auto_batch_size(ENCODE_BATCH_SIZE),
                    )
                    elapsed = time.time() - started
                    self._check_slow_batch(elapsed)
                    out.extend(result.tolist() if hasattr(result, "tolist") else list(result))
                self._last_use = time.time()
                return out
        finally:
            self._drop_user_ref()

    def _auto_batch_size(self, desired: int) -> int:
        """按当前可用显存自动收紧批次（防共享显存溢出），配置值仅作上限——
        逐字对齐旧 index.py::_auto_batch_size（问题59-B4）。

        2026-08-13 校准教训（旧注释原文）：不要用固定线性公式猜批次。实测
        长块（~700 字符）下 attention 显存随 batch×seq² 暴涨，8GB 卡满载触发
        WDDM 溢出排入系统 RAM。固定安全上限（8）+ 可用显存二次收紧，慢批
        看门狗仍兜底。探针失败按"显存未知"处理取保守上限（fail-open 铁律：
        探测失败绝不阻塞任何路径）。"""
        if getattr(self, "_device", "cpu") != "cuda":
            return desired
        try:
            import torch

            free_gb = torch.cuda.mem_get_info()[0] / 1024 ** 3
        except Exception as exc:  # noqa: BLE001 - 见 docstring：探针失败取保守上限
            self._log(f"显存探针失败（{type(exc).__name__}），批次取保守上限：{exc}")
            return min(8, desired)
        cap = min(8, desired)  # 长块 bs=16 已近 6GB，固定 8 保安全
        if free_gb < 4.5:
            cap = min(cap, 4)  # 显存紧张再降
        if desired > cap:
            if cap != self._last_batch_cap:
                self._log(f"可用显存 {free_gb:.1f}GB，批次 {desired} 收紧为 {cap}")
            self._last_batch_cap = cap
            return cap
        self._last_batch_cap = None
        return desired

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
        有任务在跑"标记，见模块 docstring）。

        **会一并归还 GPU 名额**：名额语义是"模型此刻真的在显存里"（模块
        docstring），模型都卸载了还占着名额，就是拿"锁"冒充"真实 holder"
        （AGENTS.md §7）——那会让 WEMM/OCR-local 在 GPU 明明空着的时候一直
        抢不到"gpu:0"。引用计数归零才归还：有 encode 正在用时名额照旧持有
        （那条 encode 结束后模型已被卸载、引用归零，同一次调用里收口）。"""
        if IDLE_UNLOAD_SECONDS <= 0 or self._model is None:
            return
        if time.time() - self._last_use > IDLE_UNLOAD_SECONDS:
            with self._lock:
                if time.time() - self._last_use > IDLE_UNLOAD_SECONDS and self._model is not None:
                    self._unload_locked()

    def _unload(self) -> None:
        """卸载模型。**锁顺序：self._lock → gpu_arbiter.GPU_LOCK**（模块
        docstring 的全局锁序第一段）。

        为什么是这个顺序而不是反过来：on_preempt 回调（资源仲裁器在*锁外*
        调用，见 core/resource_arbiter.py 模块 docstring 纪律 1）走的就是
        这条路径，而正常编码路径是 `encode` 持 self._lock → `_ensure_loaded`
        持 GPU_LOCK → `arbiter.acquire()`。两把锁都是裸 acquire 无超时，
        一旦这里写成 `GPU_LOCK → self._lock`，就与编码路径构成 ABBA：
        冷加载线程持 self._lock 等 GPU_LOCK、抢占回调线程持 GPU_LOCK 等
        self._lock，两边永久互等，encode() 永不返回。"""
        with self._lock, gpu_arbiter.GPU_LOCK:
            self._unload_locked()

    def _unload_locked(self) -> None:
        """真卸载（调用方已持 `self._lock` + `GPU_LOCK`）：清模型 → 放掉"驻留"
        引用 → 引用归零就把名额还回去。

        被抢占（on_preempt）、空闲卸载、慢批降级、插件停用四条路径全都收敛到
        这里，所以"模型离开显存 ⟺ 名额被归还"是一条结构性保证，而不是每条
        调用点各自记得写一遍的约定。"""
        if self._model is None:
            # 幂等：没有模型可卸时不重复扣引用（否则会把别人的使用中引用扣成 0）。
            return
        self._model = None
        if self._gpu_resident:
            self._gpu_resident = False
            with self._refs_lock:
                if self._lease_refs > 0:
                    self._lease_refs -= 1
        try:
            import gc

            gc.collect()
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        self._log("BGE-M3模型空闲卸载，显存已释放")
        self._drop_slot_if_nothing_resident()


class BGEM3Embedder:
    def __init__(
        self,
        encoder: Encoder | None = None,
        *,
        resource_arbiter=None,
        cooldown_gate: CudaCooldownGate | None = None,
        logger=None,
    ) -> None:
        # encoder=None 时用真实的（懒加载）；测试/CI 注入假 encoder。
        self._encoder = (
            encoder
            if encoder is not None
            else _RealEncoder(resource_arbiter=resource_arbiter, cooldown_gate=cooldown_gate, logger=logger)
        )

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
        """透传给真实 encoder 的收口入口（on_disable/on_unload 用）：卸载模型 +
        归还名额，幂等。注入的假 encoder 没有这个方法时静默跳过，同 idle_check
        的宽容语义。"""
        release = getattr(self._encoder, "release_gpu_slot", None)
        if release is not None:
            release()
