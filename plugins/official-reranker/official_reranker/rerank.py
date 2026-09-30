"""Cross-Encoder 重排器。懒加载契约和 official-embedder-bge-m3 完全同一
套理由，见该插件 embed.py 的模块 docstring——这里不重复展开。

**重排加载失败的闩锁（对齐 obsidian-rag/retriever.py:249-264）**：旧项目
`_get_reranker()` 第一次加载失败就置 `_reranker_failed = True` 并**本会话不再
重试**（docstring 明写"本次会话不再重试，降级纯融合"），失败计数
`rerank_failures` 供 eval 检测静默降级。rag-redo 此前失败后 `self._model`
仍是 None，于是**每一次检索都重试一次完整模型加载**（冷加载数十秒，其中还有
联网回退），只留一条 debug 级 info 日志——用户在 MCP 侧表现为"每次搜索都卡住
几十秒"。现在用 `self._failed` 闩锁 + `reset_load_failure()` 复位入口（插件
on_disable/on_unload 会复位，用户修好环境后重启插件即可重试）。

**设备选择与 GPU 生命周期**：和 official-embedder-bge-m3 是同一个"检索侧"GPU
消费群体——用同一个 `GPU_HOLDER_ID` 向资源仲裁器申请"gpu:0"名额（同一个
holder_id 意味着两边互相不冲突：谁先加载谁申请到，另一边后来加载时是
"同一持有者再次申请"的幂等续期，不会互相抢占/驱逐），同一套设备选择+
空闲卸载逻辑（详细设计理由见 embed.py 模块 docstring，这里不重复展开，
两个插件各自维护一份小实现，是插件互相隔离原则下的刻意小重复）。

**GPU 租约语义 = "模型此刻真的在显存里"（2026-09-27 语义修正，与
official-embedder-bge-m3 同步）**：改造前名额代表"这个插件启用期间可能会用
GPU"——加载时 acquire、只在插件 `on_disable` 时 release，模块 docstring 曾
把它写成设计。后果是：重排器只要被加载过一次，本进程就永久以优先级 100 占着
"gpu:0"，WEMM（`priority=10 + preempt_equal`）永远抢不到 →
`navigate_knowledge` 只能一直回"GPU 忙"。这与 AGENTS.md §7「锁不能代替真实
的跨进程 holder 协调」冲突，也偏离旧项目"按需占用"的语义（LEGACY
obsidian-rag/retriever.py:231 `release_reranker` 只在**真要卸模型**时调用，
且它唯一的生产调用点是 `index.py:1784-1788` 的 `before_serve`——"真的要
渲染页面才把检索侧让开"）。

现在名额与真实驻留严格配对：真装进显存才 acquire、真离开显存（含空闲卸载/慢批
降级/被抢占挪内存/插件停用）就 release，落到 CPU 的三条降级路径不留名额。并发/
连续使用同一个模型用**进程内引用计数**收敛成一次 acquire、归零才 release
（实现与 embed.py 同款，跨插件禁止 import，两边各有一份是刻意的）。

**共享 holder_id 下"只有真拿到名额的那个对象才归还"**：进场先读一次当前
持有者——若名额已被同进程另一个检索侧模型拿着（同 holder_id 的幂等续期），
本对象禁用时就**不**release，避免"关掉其中一个插件把另一个仍驻留的模型的名额
还掉"（这正是此前 docstring 里记的那条已知简化，现在关掉了）。反方向的口子
（持名额的那个先空闲卸载、而同进程另一个模型还驻留着）只由物理显存判据兜底，
与旧项目等价（旧项目那里同样没有名额拦着，WEMM/MinerU 服务端自己等显存）；
要彻底解决需要给"检索侧 GPU 消费群体"加一层核心才能提供的进程级引用计数
服务，超出本轮范围，如实记录不假装解决。

**2026-09-27 与 embedder 同步修掉的两处同款缺陷**（本文件与
official-embedder-bge-m3/embed.py 是刻意的小重复，两边必须同款，否则同一个
业务判断出现两份实现，AGENTS.md §4.5 不允许）：

1. **名额申请绝不在持 `GPU_LOCK` 时做**。申请方阻塞在 `acquire()` 里等对方
   让路时，仲裁器是在**被让方那个进程**的后台监控线程里跑 `on_preempt` 回
   调的，而回调要拿 `GPU_LOCK` 才能卸载模型。GUI 与 MCP 同开（两个进程各自
   都在跑检索侧）就是这个拓扑：让方等 GPU_LOCK、申请方等让方放手 = 互等到
   `preempt_timeout_s` 超时，然后申请方白白降级成 CPU。所以 `_select_device`
   同样拆成三段：非阻塞判定持 `GPU_LOCK` → 名额申请在 `GPU_LOCK` 之外 →
   让路后的复核持 `GPU_LOCK`（详见 embed.py 模块 docstring 的"跨进程抢占不许
   自锁"一节）。
2. **引用归零只归还名额，绝不卸载模型**。模型在 CPU 内存里时没有"驻留"引用，
   于是每一次 `score()` 结束引用都会归零；如果这里图省事调用收尾用的
   `release_gpu_slot()`（它无条件卸模型），降级到 CPU 之后就变成"每次检索都
   重新加载一遍几 GB 的重排模型"。见 `_drop_user_ref`。

**被抢显卡时挪到内存，而不是扔掉（2026-09-30 操作者确认，BC-11，与 embedder 同款）**：
`on_preempt` 走 `_park`（模型搬到内存，显存全部让出、名额归还），下次用时 `_unpark`
搬回显卡（实测约 0.4 秒），不再重新加载。只有被抢这一条挪内存；空闲满时限（暂存在内存里
的也算）、手动「释放显存」、慢批降级、插件停用仍整个卸载。理由与场景见 embed.py 模块
docstring 同名一节。
"""
from __future__ import annotations

import threading
import time
from typing import Protocol

from core import gpu_arbiter
from core.gpu_arbiter import CudaCooldownGate
from core.model_loading import load_pretrained

MODEL_VERSION = "BAAI/bge-reranker-v2-m3"
GPU_RESOURCE_ID = "gpu:0"
GPU_HOLDER_ID = "official-text-retrieval-gpu"  # 和 official-embedder-bge-m3 共用，见模块 docstring
GPU_PRIORITY = 100
IDLE_UNLOAD_SECONDS = 300
SLOW_BATCH_SECONDS = 30.0  # 对齐旧项目 encode_safe 的慢批阈值


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


class RerankUnavailable(RuntimeError):
    """重排器在本进程内不可用（加载失败已被闩锁，见模块 docstring）。

    单独一个异常类型，是为了让上层能把它和"打分过程真的出错了"区分开：
    前者是**已知且不再重试**的降级（走归一化合并），后者是意外（照旧向上
    抛给 core/pipeline.py 的既有 except 处理）。"""


class _RealReranker:
    def __init__(self, model_name: str = MODEL_VERSION, *, resource_arbiter=None, cooldown_gate: CudaCooldownGate | None = None, logger=None, models_dir=None) -> None:
        self._model_name = model_name
        self._model = None
        # 模型存放目录的读取函数（BC-17），语义同 embed.py::_RealEncoder：每次加载现读；
        # None = 走 HuggingFace 默认缓存（测试与旧调用方式不变）。
        self._models_dir = models_dir
        self._resource_arbiter = resource_arbiter
        self._cooldown_gate = cooldown_gate if cooldown_gate is not None else CudaCooldownGate()
        self._logger = logger
        self._last_use = time.time()
        self._slow_batch_count = 0
        self._lock = threading.RLock()
        # ---- GPU 名额（租约）状态：语义 = "模型此刻真的在显存里"（模块 docstring）
        # _refs_lock 是引用计数的叶子锁：只做数值加减，持有期间绝不调任何外部
        # 代码、不再拿任何别的锁。锁序与 embed.py 同款：
        # self._lock → GPU_LOCK → _refs_lock → ResourceArbiter，绝不反向。
        self._refs_lock = threading.Lock()
        self._lease_refs = 0       # "gpu:0" 的在用引用数（每次 score 一个 + 模型驻留一个）
        self._gpu_resident = False  # 手上这个模型是不是真的在显存里
        self._slot_owner = False    # 本对象是不是"真拿到名额"的那个（同进程另一个模型拿着时不归还）
        self._parked = False        # 被抢显卡后挪到内存暂存着（下次用先搬回显卡，见模块 docstring）
        # 加载失败闩锁：本进程内不再重试（对齐 obsidian-rag/retriever.py:249-264
        # 的 _reranker_failed）。见 reset_load_failure()。
        self._failed = False

    @property
    def load_failed(self) -> bool:
        """是否已因加载失败被闩锁（诊断/测试用，只读）。"""
        return self._failed

    def reset_load_failure(self) -> None:
        """清除加载失败闩锁，让下一次调用重新尝试加载。

        谁需要它：① 测试（要在同一个对象上验证"闩锁后不再重试、复位后可重
        试"）；② 用户修好环境（把 reranker 模型补下载完、装上
        sentence_transformers）之后不想重启整个进程——插件 on_disable/on_unload
        也会自动调它，所以"停用再启用重排器"就等于一次复位。"""
        with self._lock:
            self._failed = False

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(message)

    def _warn(self, message: str) -> None:
        if self._logger is not None:
            self._logger.warning(message)

    def _ensure_loaded(self):
        if self._failed:
            raise RerankUnavailable(
                "重排器模型此前加载失败并已闩锁，本进程内不再重试"
                "（对齐 obsidian-rag/retriever.py:249-264）"
            )
        if self._model is not None and self._parked:
            self._unpark()
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder  # noqa: PLC0415 - 故意懒加载

                # 选设备里含唯一一次可能阻塞的名额申请，必须在 GPU_LOCK 之外
                # （模块 docstring 的"2026-09-27 同步修掉的两处同款缺陷"第 1 条）
                device = self._select_device()
                with gpu_arbiter.GPU_LOCK:
                    try:
                        self._model = self._load_for_device(CrossEncoder, device)
                    except Exception as exc:
                        # 对齐旧项目 get_model：CUDA 初始化失败 → 冷却 + 降级 CPU 重试一次
                        if device != "cuda":
                            raise
                        self._cooldown_gate.cooldown(str(exc))
                        self._log(f"CUDA 初始化失败（{exc}），降级 CPU")
                        device = "cpu"
                        self._model = self._load_for_device(CrossEncoder, device)
                    self._log(f"重排器模型已加载（device={device}）")
                    self._cooldown_gate.report_device(device)
                    if self._model is not None and device == "cuda":
                        # 模型真的进了显存 → 持一个"驻留"引用（名额生命周期
                        # 从此与真实驻留严格配对，见模块 docstring）。
                        self._mark_gpu_resident()
            except Exception as exc:
                # 闩锁：包括 import 失败、CPU 也加载失败在内的一切加载失败
                # 都只试这一次（对齐旧项目 _reranker_failed=True）。日志只写
                # 异常摘要，不写任何凭据（AGENTS.md §7）。
                self._failed = True
                self._warn(
                    f"重排器模型加载失败，本次会话不再重试（降级为不重排）：{exc}"
                )
                raise
            finally:
                # 落到 CPU / 加载彻底失败时绝不能把名额留在手里：
                # 改造前这条路径会泄漏名额——模型在 CPU 上跑、却对外
                # 声明占着 "gpu:0"，WEMM/OCR-local 从此永远抢不到。
                with gpu_arbiter.GPU_LOCK:
                    self._drop_slot_if_nothing_resident()
        self._last_use = time.time()
        return self._model

    def _load_for_device(self, factory, device: str):
        """对齐旧 retriever.py::_get_reranker（2026-09-12 决策）：max_length=512
        截断 + fp16 加载（显存/读盘减半，分数扰动经旧项目真库对比确认不影响
        0.75/0.30 档位）；fp32 常驻会把 8GB 卡顶进 WDDM 共享显存溢出，实测
        单批重排掉进分钟级。加载走 core.model_loading.load_pretrained 离线
        优先（问题56），冷加载不再等 hub 联网核对。"""
        return load_pretrained(
            factory, self._model_name, log=self._log,
            device=device, max_length=512,
            model_kwargs={"torch_dtype": "float16"},
            **self._cache_kwargs(),
        )

    def _cache_kwargs(self) -> dict:
        """用户指定的模型目录 → sentence-transformers 的 `cache_folder`；没配读取
        函数就返回空（沿用 HuggingFace 默认缓存）。日志只写路径。"""
        if self._models_dir is None:
            return {}
        folder = str(self._models_dir())
        self._log(f"重排器模型目录：{folder}")
        return {"cache_folder": folder}

    def _select_device(self) -> str:
        """同 embed.py::_select_device 的对齐口径：冷却期状态机取代裸
        is_available；**先用物理显存判据筛一遍**（读不到就 fail-open 放行、
        明确不足就让路后复核、复核仍不足降级 CPU，见 embed.py 同名方法的
        docstring 逐条理由）；拿到"gpu:0"名额后直接加载，**不做 VRAM 阻塞
        等待**（旧项目检索侧从不阻塞等待，wait 语义只属于 WEMM/MinerU
        服务端）。

        同样是三段式（模块 docstring 第 1 条）：非阻塞判定持 `GPU_LOCK` →
        名额申请在 `GPU_LOCK` 之外 → 让路后的复核持 `GPU_LOCK`。

        **名额归属（`_slot_owner`）**：进场先读一次当前持有者，区分"这次是我
        真抢到了"和"同进程另一个检索侧模型已经拿着"。只有前者才归还名额
        （模块 docstring 的"共享 holder_id"一节）。"""
        with gpu_arbiter.GPU_LOCK:
            if not self._cooldown_gate.ready():
                self._log_degrade("CUDA 处于冷却期（失败后自动探测恢复）", None)
                return "cpu"
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
                on_preempt=self._park,
            )
            if not acquired:
                self._log_degrade("GPU 名额抢占失败（有更高优先级持有者）", free_gb)
                return "cpu"
            self._slot_owner = not already_ours
        if need_room:
            with gpu_arbiter.GPU_LOCK:
                free_after = gpu_arbiter.vram_free_gb(max_age=0.0)
            if free_after is not None and free_after < gpu_arbiter.BGE_MIN_VRAM_GB:
                self._log_degrade(
                    f"空闲显存不足且让路后仍不足（需 ≥{gpu_arbiter.BGE_MIN_VRAM_GB}GB）",
                    free_after,
                )
                # 决定用 CPU 就绝不能把名额攥在手里（改造前这条分支拿完名额就
                # 直接 return，一路挂到插件 on_disable）。真正归还发生在本次
                # score 收尾的引用归零处（_drop_user_ref），语义一样。
                self._drop_slot_if_nothing_resident()
                return "cpu"
        return "cuda"

    # ---- GPU 名额引用计数（语义与锁序见模块 docstring）----

    def _mark_gpu_resident(self) -> None:
        """模型真的进了显存 → 持一个"驻留"引用（必须在 `self._lock` +
        `GPU_LOCK` 之内调用）。"""
        with self._refs_lock:
            self._lease_refs += 1
        self._gpu_resident = True

    def _hold_user_ref(self) -> None:
        """一次 score 取一个"使用中"引用。刻意在 `self._lock` **之外**取：
        两个并发 score 的引用计数才是真的并发计数（而不是被 `self._lock`
        串行化成永远 1），"只 acquire 一次、归零才 release"这条规则才是被
        真正验证过的事实而不是同义反复。"""
        with self._refs_lock:
            self._lease_refs += 1

    def _drop_user_ref(self) -> None:
        """归还一个"使用中"引用。归零时**只把名额收干净，绝不卸载模型**
        （同 embed.py 同名方法，模块 docstring 第 2 条）：模型在 CPU 内存里时
        没有"驻留"引用，于是每一次 `score()` 结束引用都会归零；调收尾用的
        `release_gpu_slot()` 会把模型一起卸掉，降级到 CPU 之后就变成"每次
        检索都重新加载一遍几 GB 模型"。"""
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
        """引用计数清零，返回清零前的残留数量（on_disable 收口用）。"""
        with self._refs_lock:
            leftover = self._lease_refs
            self._lease_refs = 0
        return leftover

    def _drop_slot_if_nothing_resident(self) -> None:
        """没有任何引用、也没有模型真的驻留显存 → 把名额还回去。调用方一律已持
        `self._lock`（加载路径还要持 `GPU_LOCK`），锁序与 embed.py 同款：
        `self._lock → GPU_LOCK → _refs_lock → ResourceArbiter`，绝不反向。"""
        with self._refs_lock:
            busy = self._lease_refs > 0
        if not busy and not self._gpu_resident:
            self._release_slot_locked()

    def _release_slot_locked(self) -> None:
        """真正归还名额。**幂等**，且只归还"本对象真拿到过"的那一份。"""
        if not self._slot_owner:
            return
        self._slot_owner = False
        if self._resource_arbiter is not None:
            self._resource_arbiter.release(GPU_RESOURCE_ID, GPU_HOLDER_ID)

    def _log_degrade(self, reason: str, free_gb: float | None) -> None:
        """降级到 CPU 必须留日志（同 embed.py::_RealEncoder._log_degrade：
        对齐 obsidian-rag/index.py:727/740，让用户能判断"变慢"的原因；日志里
        只写显存数值/原因/holder_id，绝不写凭据，AGENTS.md §7）。"""
        if self._logger is None:
            return
        free_text = "探测失败(未知)" if free_gb is None else f"{free_gb:.1f}GB"
        self._logger.warning(
            "重排器本次用 CPU：%s（空闲显存 %s，GPU 名额持有者 %s）",
            reason, free_text, self._resource_arbiter.holder_of(GPU_RESOURCE_ID)
            if self._resource_arbiter is not None else "无仲裁器",
        )

    def release_gpu_slot(self) -> None:
        """**插件生命周期收口入口**（`on_disable` / `on_unload` 专用）：卸载模型
        （真的把显存还回去），再把"gpu:0"名额还给仲裁器。

        - **幂等**：重复调用是安全空操作。
        - **哪怕还有残留引用也照样收尾**：插件都要被停用了，留下一个占着最高
          优先级名额、模型却已经不在的幽灵 holder，远比提前收尾恶劣。
        - **请求路径（`score`）绝不许调它**（同 embed.py 同名方法的说明）。
        - 只归还"本对象真拿到过"的那一份名额。

        锁序 `self._lock → GPU_LOCK`，同 `on_preempt` 回调。"""
        with self._lock, gpu_arbiter.GPU_LOCK:
            leftover = self._force_reset_refs()
            if leftover:
                self._log(f"停用时仍有 {leftover} 个在用引用，先卸载模型再归还 GPU 名额")
            self._unload_locked()
            self._release_slot_locked()


    def score(self, query: str, texts: list[str]) -> list[float]:
        self._hold_user_ref()
        try:
            with self._lock:
                model = self._ensure_loaded()
                pairs = [[query, text] for text in texts]
                started = time.time()
                # batch_size=16 对齐旧 retriever.py:698 的 predict 调用
                result = list(model.predict(pairs, batch_size=16))
                elapsed = time.time() - started
                self._last_use = time.time()
                self._check_slow_batch(elapsed)
                return result
        finally:
            self._drop_user_ref()

    def _check_slow_batch(self, elapsed: float) -> None:
        """同 embed.py::_RealEncoder._check_slow_batch（对齐旧项目
        encode_safe 慢批检测），连续两批慢批主动降级 CPU。"""
        if elapsed > SLOW_BATCH_SECONDS:
            self._slow_batch_count += 1
            self._log(
                f"CUDA 单批重排耗时 {elapsed:.1f}s（>{SLOW_BATCH_SECONDS:.0f}s），"
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
        """空闲超过 IDLE_UNLOAD_SECONDS 就卸载模型，**并一并归还 GPU 名额**
        （名额语义 = 模型此刻真的在显存里，模块 docstring）：模型都卸载了还
        占着名额，就是拿"锁"冒充"真实 holder"（AGENTS.md §7），会让
        WEMM/OCR-local 在显存明明空着时一直抢不到。引用计数归零才归还。"""
        if IDLE_UNLOAD_SECONDS <= 0 or self._model is None:
            return
        if time.time() - self._last_use > IDLE_UNLOAD_SECONDS:
            with self._lock:
                if time.time() - self._last_use > IDLE_UNLOAD_SECONDS and self._model is not None:
                    self._unload_locked()

    def _park(self) -> None:
        """`on_preempt` 回调：别的进程要用显卡——模型挪到内存、显存全部让出、名额归还，
        但不扔掉（同 embed.py::_RealEncoder._park）。锁序同 `_unload`。"""
        with self._lock, gpu_arbiter.GPU_LOCK:
            self._park_locked()

    def _park_locked(self) -> None:
        if self._model is None or not self._gpu_resident:
            # 不在显存里：没有显存可让，模型原样留着，只把名额收干净。
            self._drop_slot_if_nothing_resident()
            return
        try:
            self._model.to("cpu")
        except Exception as exc:  # noqa: BLE001 - 挪不动就退回整个卸载，让路本身不能失败
            self._log(f"重排器挪到内存失败（{type(exc).__name__}），改为整个卸载")
            self._unload_locked()
            return
        self._gpu_resident = False
        self._parked = True
        with self._refs_lock:
            if self._lease_refs > 0:
                self._lease_refs -= 1
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        self._log("重排器让出显卡：模型暂存到内存，下次用时搬回（空闲满时限仍整个卸载）")
        self._drop_slot_if_nothing_resident()

    def _unpark(self) -> None:
        """把暂存在内存里的模型搬回显卡（调用方已持 `self._lock`）。设备判定、失败降级与
        embed.py::_RealEncoder._unpark 相同：判下来用 CPU 就原地用；搬回失败冷却 + 留在 CPU；
        连挪回 CPU 都失败就整个丢掉，交给 `_ensure_loaded` 重新加载。搬回失败不算"加载失败"，
        不触发本进程的加载失败闩锁。"""
        device = self._select_device()
        try:
            with gpu_arbiter.GPU_LOCK:
                if device == "cuda":
                    try:
                        self._model.to("cuda")
                    except Exception as exc:  # noqa: BLE001 - 同首次加载的 CUDA 失败降级
                        self._cooldown_gate.cooldown(str(exc))
                        self._log(f"重排器搬回显卡失败（{type(exc).__name__}），降级 CPU")
                        device = "cpu"
                        try:
                            self._model.to("cpu")
                        except Exception:  # noqa: BLE001 - 半截在显卡上的模型不能再用
                            self._model = None
                self._parked = False
                if self._model is not None:
                    self._cooldown_gate.report_device(device)
                    if device == "cuda":
                        self._mark_gpu_resident()
                    self._log(f"重排器模型已从内存搬回（device={device}）")
        finally:
            with gpu_arbiter.GPU_LOCK:
                self._drop_slot_if_nothing_resident()

    def _unload(self) -> None:
        """卸载模型。**锁顺序：self._lock → gpu_arbiter.GPU_LOCK**（同
        embed.py::_RealEncoder._unload 的理由：on_preempt 回调走这条路径，
        而 score 路径是 self._lock → GPU_LOCK；两把锁都无超时，顺序写反即
        ABBA 死锁，score() 永不返回）。"""
        with self._lock, gpu_arbiter.GPU_LOCK:
            self._unload_locked()

    def _unload_locked(self) -> None:
        """真卸载（调用方已持 `self._lock` + `GPU_LOCK`）：清模型 → 放掉"驻留"
        引用 → 引用归零就把名额还回去。空闲卸载、慢批降级、插件停用（以及挪内存
        失败时的被抢占）全都收敛到这里，所以"模型离开显存 ⟺ 名额被
        归还"是结构性保证，而不是每条调用点各自记得写一遍的约定。"""
        if self._model is None:
            # 幂等：没有模型可卸时不重复扣引用（否则会把别人的使用中引用扣成 0）。
            return
        self._model = None
        self._parked = False
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
        self._log("重排器模型空闲卸载，显存已释放")
        self._drop_slot_if_nothing_resident()


class RerankerEngine:
    def __init__(
        self,
        reranker: Reranker | None = None,
        *,
        resource_arbiter=None,
        cooldown_gate: CudaCooldownGate | None = None,
        logger=None,
        models_dir=None,
    ) -> None:
        self._reranker = (
            reranker
            if reranker is not None
            else _RealReranker(
                resource_arbiter=resource_arbiter, cooldown_gate=cooldown_gate, logger=logger,
                models_dir=models_dir,
            )
        )

    def rerank(
        self, query: str, chunk_id_text_pairs: list[tuple[str, str]], top_k: int = 10
    ) -> list[tuple[str, float]]:
        if not chunk_id_text_pairs:
            return []
        ids = [chunk_id for chunk_id, _ in chunk_id_text_pairs]
        texts = [text for _, text in chunk_id_text_pairs]
        try:
            scores = self._reranker.score(query, texts)
        except RerankUnavailable:
            # 加载失败已被闩锁，本进程内不会自己变好（对齐旧项目
            # retriever.py:680 的 `if reranker is not None`：拿不到重排器就
            # 直接走按库归一化合并，不抛异常、不刷屏）。core/pipeline.py 收到
            # 空列表后按它既有的 "if reranked:" 分支落到归一化合并。
            return []
        ranked = sorted(zip(ids, scores), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_k]

    def reset_load_failure(self) -> None:
        """清除重排器的加载失败闩锁（透传给真实 reranker；注入的假 reranker
        没有这个方法时静默跳过，同 idle_check 的宽容语义）。

        插件 on_disable/on_unload 调它：停用再启用 = 一次复位，用户修好环境
        （补下载 reranker 模型/装依赖）后不必重启整个进程就能重试——对齐旧
        项目"重启进程才清 `_reranker_failed`"的可达效果。"""
        reset = getattr(self._reranker, "reset_load_failure", None)
        if reset is not None:
            reset()

    def idle_check(self) -> None:
        check = getattr(self._reranker, "idle_check", None)
        if check is not None:
            check()

    def release_gpu_slot(self) -> None:
        """透传收口入口（on_disable/on_unload 用）：卸载模型 + 归还名额，幂等。
        注入的假 reranker 没有这个方法时静默跳过，同 idle_check 的宽容语义。"""
        release = getattr(self._reranker, "release_gpu_slot", None)
        if release is not None:
            release()
