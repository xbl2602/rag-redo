#!/usr/bin/env python3
"""official-visual-wemm 的子进程服务端——单独进程运行。

WeMM-Embedding 是"看图"的多模态嵌入模型：把一整页 PDF 渲染出来的图编码成
一个向量，也把一段文字查询编码到同一向量空间，跨模态算余弦相似度实现
"页级视觉导航"——不依赖文字提取/OCR，扫描件、图表、公式密集的 PDF 也能
按页找到内容，这正是它相对纯文字检索的核心价值。行为对齐旧项目
obsidian-rag 的 wemm_server.py（同一份模型、同一套 transformers+
qwen_vl_utils 调用方式），实现按 rag-redo 插件化原则重写。

**为什么是独立子进程**：这个模型真正跑起来要占几个GB显存/内存，依赖
`torch`+`transformers`+`qwen_vl_utils`——和 official-ocr-mineru-local 的
道理一样：不该把这些重依赖悄悄装进核心 `.venv`，子进程崩溃也不该带崩
核心进程或其他插件，架构红线4/7都是这个理由。真正调用模型的分支懒导入，
没装就折叠成清楚的失败原因，不是启动就炸；`RAG_REDO_FAKE_WEMM` 环境
变量存在时用确定性假实现，只给测试/架构验证用。

**GPU 生命周期管理（2026-09-23 补齐，按 obsidian-rag/wemm_server.py 真实
行为移植）**：懒加载 + 两级空闲释放——①空闲 `WEMM_UNLOAD_AFTER_SECONDS`
（默认300s/5分钟）卸载模型释放显存，子进程本身继续监听（下次请求重新
加载，代价是几十秒冷加载，比显存溢出整机卡死划算）；②卸载之后再空闲
`WEMM_IDLE_EXIT_SECONDS`（默认1800s/30分钟），子进程自己退出——"用完
即关"，下次真正需要时由插件按需重新拉起（见 plugin.py::_ensure_alive，
对应旧项目 gpu_arbiter.ensure_server 的"已死则重拉、已活则复用"幂等
语义）。加载模型前用 `_wait_for_vram()` 等其他 GPU 消费者让路；`/evict`
端点供资源仲裁器的抢占回调主动请求"立即卸载"（检索侧优先，见
plugin.py 的 GPU_PRIORITY 说明）。

**为什么这几个探测/等待函数是自包含的、不 import core.gpu_arbiter**：
这个子进程运行在完全隔离的独立解释器里（未来 `env_bootstrap` 落地后是
插件自己的 venv），import 不到核心包——同 plugin.py 模块 docstring 里
"为什么 subprocess_service 的 server.py 不直接 import core.*"的说明，
这里的几个函数是 core/gpu_arbiter.py 对应函数的自包含副本（stdlib-only，
故意重复，不是遗漏）。

**本进程的输出去哪**：由父进程决定——`core/subprocess_service.py::
SubprocessServiceHandle` 在 `Popen` 时就把本进程的 fd 1/2 指向插件在
DATA_ROOT 下的真实日志文件（`data/visual_wemm/wemm_server.log`，父进程
那侧是 `plugin.py::_start_handle` 传进去的 `log_path`），所以下面所有
`print(..., file=sys.stderr)`、以及 `socketserver` 打到 stderr 的
traceback，都会落在那个文件里而不是一个没人读的管道（对齐 LEGACY
obsidian-rag/gpu_arbiter.py:241-252 的 `Popen(stdout=logf, stderr=logf)`
——真实文件不会被"管道写满 64KB"卡死，这是本文件真机调试时踩过的坑的
同一个根因）。父进程那一侧用 `plugin.py::status()` 的 `log_file` 字段把
路径报给用户/AI（同 LEGACY `wemm_status` 指向 data/wemm_server.log）。
"""
from __future__ import annotations

import base64
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

WEMM_MODEL_DEFAULT = "tencent/WeMM-Embedding-2B"
WEMM_DIM_DEFAULT = 512
#: 加载前要求的最小空闲显存（GiB）。
#:
#: **2026-09-29 在本机实测重标定过，原来的 5.5 是照搬旧项目 `gpu_arbiter.py`
#: 的同名常量，从来没有在这台机器上验证过。** 实测（`tools/probe_wemm_vram.py`，
#: 用本文件 `_real_embed` 同一套调用形态，RTX 5060 Laptop / torch 2.11.0+cu128）：
#:
#:   加载前空闲 6.878 → 加载后 1.036   即加载吃掉 5.842
#:   torch 报 `memory_allocated` 5.070、`memory_reserved` 5.164
#:   再编一页额外吃 0.404（编码峰值 reserved 5.527）
#:   ── 合计 6.231 GiB
#:
#: 旧项目日志里那个 `gpu_mem=5.07GB` 是 `memory_allocated`（纯权重），**漏掉了
#: CUDA 上下文与 cuBLAS/cuDNN 句柄的 0.77 GiB**。门槛 5.5 比真实需求低 0.73 GiB，
#: 照它放行会让模型起来后差一截，触发 OOM 或 WDDM 共享内存抖动（整机变卡，
#: 正是本模块 docstring 里反复警告的那种后果）。
#:
#: 6.23 是**不可再压**的下限（不量化的话）：试过 `PYTORCH_CUDA_ALLOC_CONF=
#: expandable_segments:True`，占用 6.231 → 6.230，那 0.678 GiB 不是碎片而是上下文
#: 与句柄，换分配器无效。`WEMM_DIM` 512→256 也不省——省的是向量存储不是权重。
WEMM_MIN_VRAM_GB = 6.3
#: 等待其他 GPU 消费者让路的上限。原来 900s 长得离谱：期间界面只见「空闲显存
#: 5.2GB < 需求 5.5GB」反复刷屏，宿主 120s 超时熔断后请求早已失败，服务端还在
#: 干等。降到 60s：让路是**主动**发生的（`/evict` 软驱逐，或 bge/reranker 空闲
#: 自动卸载，都是秒级），60s 还等不到基本就是"这块卡此刻就是装不下"，此时快速
#: 失败并把所需/现有数字报回去，好过静默耗掉 15 分钟。
WEMM_VRAM_WAIT_SECONDS = 60.0
WEMM_UNLOAD_AFTER_SECONDS = 300  # 空闲卸载模型（保留子进程），0=不自动卸载
WEMM_IDLE_EXIT_SECONDS = 1800  # 卸载后再空闲这么久，子进程自退出，0=常驻不退出

_engine = None  # dict(model=, processor=, device=)
_ENGINE_LOCK = threading.Lock()
_last_use = time.time()
_active_requests = 0  # 在途请求数：空闲自退出的安全判据，不能在编码中途把进程干掉
_vram_cache: tuple[float | None, float] = (None, 0.0)


def _vram_free_gb(max_age: float = 5.0):
    """当前空闲显存（GB），探测失败返回 None（fail-open）——core/gpu_arbiter.py
    ::vram_free_gb 的自包含副本，见模块 docstring。"""
    global _vram_cache
    now = time.time()
    if _vram_cache[1] and now - _vram_cache[1] < max_age:
        return _vram_cache[0]
    val = None
    try:
        import torch

        if torch.cuda.is_available():
            val = torch.cuda.mem_get_info()[0] / 2**30
    except Exception:
        val = None
    if val is None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True,
                timeout=5,
                check=False,
                # 这个子进程本身也是被 core/subprocess_service.py 用
                # CREATE_NO_WINDOW 拉起的、没有控制台的进程——这里不加同一个
                # flag，Windows 还是会给 nvidia-smi 单独弹一个控制台窗口。
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            nums = [float(x) for x in out.stdout.decode("utf-8", "replace").split()]
            if nums:
                val = nums[0] / 1024.0
        except Exception:
            _vram_cache = (None, now)
            return None
    _vram_cache = (val, now)
    return val


def _wait_for_vram(min_free_gb: float, timeout_s: float = WEMM_VRAM_WAIT_SECONDS, poll_s: float = 10.0) -> tuple[bool, float | None]:
    """等空闲显存 ≥ min_free_gb。返回 `(够不够, 最后一次实测的空闲显存)`。

    2026-09-29 改签名：以前只回 bool，显存不足时上层只能说"超时"，用户和 GUI
    拿不到任何数字，只能在日志里翻。现在把最后一次探测到的空闲显存带回去，
    报错信息里直接写明"需要 X GB、当前只有 Y GB"，GUI 才有东西可以显示。
    """
    deadline = time.time() + timeout_s
    while True:
        free = _vram_free_gb(max_age=0.0)
        if free is None or free >= min_free_gb:
            return True, free
        if time.time() >= deadline:
            return False, free
        print(f"[wemm] 空闲显存 {free:.2f}GB < 需求 {min_free_gb:.2f}GB，等待其他模型让路...", file=sys.stderr)
        time.sleep(poll_s)


def _hf_hub_dir() -> Path:
    """模型缓存目录：核心启动本服务时通过 `HF_HUB_CACHE` 传入用户配置的模型目录
    （core/paths.py::models_env，BC-17）；没传（单独手动跑本脚本）才退回
    HuggingFace 的默认位置。本文件与核心完全隔离、import 不到 core.*，所以这里
    只认环境变量，不重新实现路径规则。"""
    configured = os.environ.get("HF_HUB_CACHE")
    if configured:
        return Path(configured)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _resolve_model_path(model_id: str) -> str:
    """优先用本机 HuggingFace 缓存里已经下好的快照（不重新下载）——真实
    用户机器上如果已经用别的工具下过这个模型（比如旧项目本身），这里应该
    直接复用，不该傻乎乎再下一遍。找不到缓存才原样交给
    AutoModel.from_pretrained 按需下载。"""
    if not model_id:
        return model_id
    if Path(model_id).is_dir():
        return model_id
    hf_cache = _hf_hub_dir()
    safe = model_id.replace("/", "--").replace(":", "--")
    snapshots = hf_cache / f"models--{safe}" / "snapshots"
    if snapshots.is_dir():
        candidates = sorted(p for p in snapshots.iterdir() if p.is_dir())
        if candidates:
            return str(candidates[-1])
    return model_id


class InsufficientVram(RuntimeError):
    """显存不足，**并且带着具体数字**，供上层/GUI 如实转述给用户。

    2026-09-29 新增。以前这里只 `raise RuntimeError("等待空闲显存 >= 5.5GB 超时")`，
    用户在界面上看到的是「索引还在继续」，日志里是「空闲显存 5.2GB < 需求
    5.5GB」，但**没有任何地方告诉他"你需要多少、现在有多少、差多少、怎么办"**，
    只能自己翻日志猜。现在这两个数字是异常的一等公民。

    `required_gb` 用的是本机实测标定后的 6.3（见 `WEMM_MIN_VRAM_GB` 的注释：
    原来的 5.5 漏算了 CUDA 上下文与 cuBLAS 句柄，比真实需求低 0.73 GiB）。
    """

    def __init__(self, required_gb: float, free_gb: float | None) -> None:
        self.required_gb = round(float(required_gb), 2)
        self.free_gb = None if free_gb is None else round(float(free_gb), 2)
        if self.free_gb is None:
            detail = "当前空闲显存探测失败（装了 nvidia-smi 也没有），无法判断是否装得下"
        else:
            detail = f"需要 {self.required_gb} GB，当前只有 {self.free_gb} GB"
        super().__init__(
            f"显卡内存不足：{detail}。页级视觉导航（WEMM）本轮未运行，"
            "文字索引不受影响。可关闭占显存的程序后重试，"
            "或在设置里开启「强制加载页级视觉导航」再试（显存不足时可能失败）。"
        )


def _load_engine(model_id: str, *, force: bool = False):
    global _engine, _last_use
    with _ENGINE_LOCK:
        if _engine is not None and _engine["model_id"] == model_id:
            _last_use = time.time()
            return _engine
        # 显存互斥：其他 GPU 消费者在线时不硬抢——等它让路（空闲自动卸载 /
        # evict 主动抢占），等不到就报出确切数字快速失败，绝不撑爆显存导致整机卡死。
        #
        # `force`（2026-09-29 新增，对应设置项 `wemm_force_load`）：用户在 GUI
        # 明确选择"我知道风险，仍要试"时跳过这道门槛，直接尝试加载。风险是实的
        # ——低于实测需求（6.23 GiB）时很可能 CUDA OOM，或者触发 WDDM 共享内存
        # 溢出把整机拖卡（见本模块 docstring 反复警告的失败模式）。所以 force
        # 只跳门槛、**不吞异常**：真 OOM 就让调用方看到明确的 OOM 报错，而不是
        # 伪装成成功或静默截断。
        if not force:
            enough, free_now = _wait_for_vram(WEMM_MIN_VRAM_GB)
            if not enough:
                print(
                    f"[wemm] 显存不足，跳过加载：需要 {WEMM_MIN_VRAM_GB}GB，"
                    f"当前 {free_now if free_now is not None else '探测失败'}GB。"
                    "本轮页级索引记为待重试，文字索引照常发布。",
                    file=sys.stderr,
                )
                raise InsufficientVram(WEMM_MIN_VRAM_GB, free_now)
        else:
            print(
                f"[wemm] 已开启强制加载：跳过 {WEMM_MIN_VRAM_GB}GB 显存门槛直接尝试"
                "（显存不足会明确报错，不会伪装成功）",
                file=sys.stderr,
            )
        import torch
        from transformers import AutoModel, AutoProcessor

        path = _resolve_model_path(model_id)
        processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
        # dtype= 是新版 transformers 的参数名，torch_dtype= 是旧版——两个都
        # 传，未知的那个会被静默吞掉，只有对应版本认识的那个生效；只传一个
        # 在版本不对的那一侧会退化成 fp32 加载，显存/内存占用直接翻倍，这是
        # 旧项目 wemm_server.py 真实踩过、写进注释的坑，照抄过来。
        model = AutoModel.from_pretrained(
            path, trust_remote_code=True, dtype=torch.bfloat16, torch_dtype=torch.bfloat16
        )
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = model.to(device)
        model.eval()
        supported = list(getattr(model.config, "matryoshka_dimensions", None) or [])
        _engine = {"model": model, "processor": processor, "device": device, "model_id": model_id, "supported": supported}
        _last_use = time.time()
        return _engine


def _unload_engine_locked() -> None:
    """释放显存给其他 GPU 消费者让路。调用方须已持有 _ENGINE_LOCK。幂等：
    没有引擎在跑时直接返回，不是错误（/evict 空载时也该回 ok）。"""
    global _engine, _last_use
    if _engine is None:
        return
    _engine.clear()
    _engine = None
    _last_use = time.time()
    try:
        import gc

        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    print("[wemm] engine unloaded, gpu memory released", file=sys.stderr)


def _check_idle_unload() -> None:
    if WEMM_UNLOAD_AFTER_SECONDS <= 0 or _engine is None:
        return
    if time.time() - _last_use > WEMM_UNLOAD_AFTER_SECONDS:
        with _ENGINE_LOCK:
            if time.time() - _last_use > WEMM_UNLOAD_AFTER_SECONDS and _engine is not None:
                _unload_engine_locked()


def _idle_unload_daemon() -> None:
    """空闲卸载守护线程：每 30s 检查一次，不依赖"有新请求进来才检查"——
    空闲的定义恰恰是没有请求，只在请求里检查会让长时间无请求时形同虚设。"""
    interval = min(30, max(1, WEMM_UNLOAD_AFTER_SECONDS))
    while True:
        time.sleep(interval)
        try:
            _check_idle_unload()
        except Exception as exc:
            print(f"[wemm] idle check error: {type(exc).__name__}: {exc}", file=sys.stderr)


def _idle_exit_daemon() -> None:
    """进程自退出守护：显存已卸载（_engine is None）且再空闲
    WEMM_IDLE_EXIT_SECONDS 秒、无在途请求 → 进程自己退出——"用完即关"，
    下次需要时由插件按需重新拉起（见 plugin.py::_ensure_alive）。"""
    interval = min(30, max(1, WEMM_IDLE_EXIT_SECONDS))
    while True:
        time.sleep(interval)
        if WEMM_IDLE_EXIT_SECONDS <= 0 or _engine is not None or _active_requests > 0:
            continue
        if time.time() - _last_use > WEMM_IDLE_EXIT_SECONDS:
            print("[wemm] idle %ds after unload -> process exit" % WEMM_IDLE_EXIT_SECONDS, file=sys.stderr)
            # os._exit 不走 Python 的正常退出流程，sys.stderr 里还缓冲着的
            # 内容会被直接丢掉——那正是"为什么这个进程自己没了"这条最需要
            # 留在日志里的诊断信息。刷一次再退。
            try:
                sys.stderr.flush()
            except Exception:  # noqa: BLE001 - 刷盘失败绝不能挡住退出
                pass
            os._exit(0)


PARENT_PID_ENV = "RAG_REDO_PARENT_PID"  # 由宿主（core/subprocess_service.py）在拉起本进程时写入
_PARENT_POLL_SECONDS = 2.0


def _pid_alive(pid: int) -> bool:
    """`pid` 对应的进程还在吗（标准库实现——本进程跑在自己的解释器里，不能 import core）。
    权限不够读不到（如宿主是管理员进程）按“还在”算，宁可不退出也不误杀。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5  # ERROR_ACCESS_DENIED：进程存在只是够不着
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watch_parent(parent_pid: int, *, interval: float = _PARENT_POLL_SECONDS, on_gone=None, sleep=time.sleep) -> None:
    """阻塞到 `parent_pid` 不在了，然后调用 `on_gone()`（可注入，便于测试）。"""
    while _pid_alive(parent_pid):
        sleep(interval)
    if on_gone is not None:
        on_gone()


def _exit_because_parent_is_gone() -> None:
    print("[wemm] 启动本服务的宿主进程已经不在 -> 进程退出（释放显存）", file=sys.stderr)
    try:
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - 刷盘失败绝不能挡住退出
        pass
    os._exit(0)


def _parent_watch_daemon() -> None:
    """宿主进程（GUI/MCP/索引 worker）**异常没了**（崩溃、被“结束任务”杀掉、被强杀）时，
    它没机会走 `on_disable` 去停本服务——Windows 上父进程退出不会带走子进程，本服务会
    一直占着显存，直到 30 分钟后的空闲自退出。2026-09-29 实测就抓到过一个这样的孤儿
    （宿主没了 29 分钟它还活着）。所以自己盯着宿主：宿主一没就自退出。没有
    `RAG_REDO_PARENT_PID`（手动启动）时不启用，回到只靠空闲自退出的旧行为。"""
    try:
        parent_pid = int(os.environ.get(PARENT_PID_ENV, "") or 0)
    except ValueError:
        return
    if parent_pid <= 0:
        return
    _watch_parent(parent_pid, on_gone=_exit_because_parent_is_gone)


def build_messages(kind: str, content):
    if kind == "image":
        return [{"role": "user", "content": [{"type": "image", "image": content}]}]
    if kind == "text":
        return [{"role": "user", "content": [{"type": "text", "text": content}]}]
    raise ValueError(f"未知类型: {kind}")


def _real_embed(kind: str, content, dim: int, *, force: bool = False) -> list[float]:
    try:
        import torch
        import torch.nn.functional as F
        from qwen_vl_utils import process_vision_info
    except ImportError as exc:
        raise RuntimeError(
            "WEMM 依赖(torch/transformers/qwen_vl_utils)未安装——见 plugin 模块 docstring"
        ) from exc

    model_id = os.environ.get("RAG_REDO_WEMM_MODEL", WEMM_MODEL_DEFAULT)
    eng = _load_engine(model_id, force=force)
    if eng["supported"] and dim not in eng["supported"]:
        raise ValueError(f"不支持的维度 {dim}，支持 {eng['supported']}")

    messages = build_messages(kind, content)
    processor, model = eng["processor"], eng["model"]
    prompt = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
    images, videos, video_kwargs = process_vision_info(
        messages, image_patch_size=16, return_video_kwargs=True, return_video_metadata=True
    )
    video_metadata = None
    if videos is not None:
        videos, video_metadata = (list(part) for part in zip(*videos))
        videos = list(videos)
        video_metadata = list(video_metadata)
    inputs = processor(
        text=prompt, images=images, videos=videos, video_metadata=video_metadata,
        return_tensors="pt", **(video_kwargs or {}),
    )
    inputs = inputs.to(eng["device"])
    with _ENGINE_LOCK, torch.inference_mode():
        emb = model.embedding(**inputs).float()
    emb = F.normalize(emb[..., :dim], dim=-1)
    return emb.cpu().tolist()[0]


def _fake_embed(kind: str, content, dim: int) -> list[float]:
    """确定性假向量，只给测试/架构验证用——用内容的哈希撒一个可复现的
    向量，不同内容会得到不同向量（方便测试断言"检索能分辨不同页"），但
    绝不是真实语义嵌入。kind="image" 时 content 是 do_POST 已经落盘的
    临时文件路径——哈希文件真实字节，不是哈希路径字符串本身（临时文件名
    长度/格式高度相似，哈希路径会让不同页图撞出几乎一样的假向量，测试
    没法区分"检索到了正确的页"）。"""
    import hashlib

    key = Path(content).read_bytes() if kind == "image" else content.encode("utf-8")
    digest = hashlib.sha256(key).digest()
    vec = [((digest[i % len(digest)] / 255.0) * 2 - 1) for i in range(dim)]
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            # 不取 _ENGINE_LOCK：health 的职责是「服务活没活」，若在模型加载/
            # 编码期间被锁挡住超时，检索方会误判「服务不可用」。快照读引用即可。
            # model/dim/device 字段对齐旧 wemm_server.py /health（293-298）——
            # wemm_status 的"看图服务存活：{model}，设备 {device}"行从这里取数。
            ent = _engine
            loaded = ent is not None
            ent = ent or {}
            try:
                import torch as _torch

                device = ent.get("device") or ("cuda" if _torch.cuda.is_available() else "cpu")
            except Exception:  # noqa: BLE001 - health 绝不因诊断失败而失败
                device = "unknown"
            self._json(200, {"ok": True, "loaded": loaded,
                             "model": ent.get("model_id") or WEMM_MODEL_DEFAULT,
                             "dim": ent.get("dim") or WEMM_DIM_DEFAULT,
                             "device": device,
                             "supported_dims": ent.get("supported") or []})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/evict":
            # 检索优先抢占：立即卸载模型释放显存。正在编码时会等当前一条
            # 拿到 _ENGINE_LOCK 才卸（不会腰斩正在跑的一条），其后批次由
            # 索引侧的失败终态记账、下轮自动重试。空载时也回 ok（幂等）。
            with _ENGINE_LOCK:
                _unload_engine_locked()
            self._json(200, {"ok": True})
            return
        if self.path != "/embed":
            self._json(404, {"error": "not found"})
            return
        global _active_requests, _last_use
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        kind = payload.get("kind")
        content = payload.get("content")
        dim = int(payload.get("dim") or WEMM_DIM_DEFAULT)
        # `force` 对应设置项 wemm_force_load：用户明确选择"知道风险也要试"。
        force = bool(payload.get("force"))
        # ── 本地临时文件直传（2026-09-29 提速，见下）──
        # 宿主与服务在同一台机器。旧的走法是宿主把 PNG 做 base64 塞进 JSON，服务端再
        # 解 base64、把同样内容的 PNG 写到一个临时文件——因为模型的图像接口要的是
        # **文件路径/URL 而不是原始字节**，所以像素在磁盘上白往返了一趟，中间还被
        # `json.dumps` 转义 + `json.loads` 解析各走一遍（base64 还平白膨胀 33%）。
        #
        # 改法：宿主自己落临时文件，JSON 里只发路径字符串（几十字节）。隐私性质一字
        # 未改——还是本机临时文件、用完即删、绝不出网（见 `_decode_image_to_tmpfile`
        # 的注释）。
        #
        # 两种走法都留着：这是**长驻子进程**，宿主升级后它可能还是旧代码在跑（反之亦然），
        # 老服务不认识 `path` 会回 400，宿主据此自动退回 base64（见 plugin.py）。
        path = payload.get("path")
        caller_owned = False
        if kind == "image" and path:
            path = str(path)
            # 只认系统临时目录里的真实文件：这条 HTTP 通道本来只有宿主这一个调用方，
            # 但它是个"把任意路径交给模型读文件"的接口，值得挡一道，避免哪天被
            # 别的端口扫到就能读任意文件。
            try:
                resolved = os.path.realpath(path)
                tmp_root = os.path.realpath(tempfile.gettempdir())
                if not resolved.startswith(tmp_root + os.sep) or not os.path.isfile(resolved):
                    raise ValueError("path 必须指向系统临时目录里已存在的文件")
                content = resolved
                caller_owned = True  # 文件归宿主所有，服务端不删
            except (OSError, ValueError) as exc:
                self._json(400, {"ok": False, "error": f"path 非法: {exc}"})
                return
        if kind not in ("image", "text") or content is None:
            self._json(400, {"ok": False, "error": "kind 必须是 image|text，content 非空"})
            return
        _active_requests += 1
        try:
            _check_idle_unload()
            # 直传路径时 content 已经是那份临时文件的路径，不是 base64——再解一次
            # 会把路径字符串当图片数据，每一页都报“base64 解码失败”，页库一页也建不上。
            if kind == "image" and not caller_owned:
                content = self._decode_image_to_tmpfile(content)
            if os.environ.get("RAG_REDO_FAKE_WEMM"):
                embedding = _fake_embed(kind, content, dim)
            else:
                embedding = _real_embed(kind, content, dim, force=force)
            _last_use = time.time()
            self._json(200, {"ok": True, "embedding": embedding, "dim": dim})
        except InsufficientVram as exc:
            # 显存不足单独回一个 reason 标记 + 两个数字：宿主侧要据此把
            # 「需要多少 / 当前多少 / 差多少」原样转述给 GUI，塞进
            # snapshot.progress.heartbeat_note 显示在进度条上。只回一句
            # 错误文本的话，上层拿不到结构化数字，只能把整段文案塞进去。
            self._json(
                200,
                {
                    "ok": False,
                    "reason": "vram",
                    "required_gb": exc.required_gb,
                    "free_gb": exc.free_gb,
                    "error": str(exc),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 子进程这一侧也不能让异常直接炸掉HTTP响应
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            _active_requests -= 1
            # 只删**自己造**的临时文件。宿主直传路径时（caller_owned）文件归宿主，
            # 服务端删了会跟宿主侧的清理打架；而且宿主可能还要把它留在队列里复用
            # （见 plugin.py 的渲染线程）——那时候服务端先删就等于删掉了正在排队的页。
            if kind == "image" and not caller_owned and isinstance(content, str) and content.endswith(".png"):
                try:
                    os.unlink(content)
                except OSError:
                    pass

    def _decode_image_to_tmpfile(self, b64: str) -> str:
        """base64 页图 → 临时文件路径——模型的图像输入接口要的是文件路径/
        URL，不是原始字节；图片只在内存/临时文件里过一道，处理完立刻删，
        绝不落进任何持久化目录、绝不出网（同旧项目 wemm_server.py 的隐私
        约定）。"""
        data = base64.b64decode(b64)
        fd, tmppath = tempfile.mkstemp(suffix=".png")
        with io.open(fd, "wb") as f:
            f.write(data)
        return tmppath

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass  # 静默，避免污染核心进程的 stdout/stderr


if __name__ == "__main__":
    if WEMM_UNLOAD_AFTER_SECONDS > 0:
        threading.Thread(target=_idle_unload_daemon, daemon=True, name="wemm-idle-unload").start()
    if WEMM_IDLE_EXIT_SECONDS > 0:
        threading.Thread(target=_idle_exit_daemon, daemon=True, name="wemm-idle-exit").start()
    threading.Thread(target=_parent_watch_daemon, daemon=True, name="wemm-parent-watch").start()
    port = int(sys.argv[sys.argv.index("--port") + 1])
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
