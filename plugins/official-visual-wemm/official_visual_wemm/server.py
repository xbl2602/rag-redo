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
WEMM_MIN_VRAM_GB = 5.5  # WeMM-2B bf16 + 激活余量，对齐旧项目 gpu_arbiter.py 同名常量
WEMM_VRAM_WAIT_SECONDS = 900.0
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
            )
            nums = [float(x) for x in out.stdout.decode("utf-8", "replace").split()]
            if nums:
                val = nums[0] / 1024.0
        except Exception:
            _vram_cache = (None, now)
            return None
    _vram_cache = (val, now)
    return val


def _wait_for_vram(min_free_gb: float, timeout_s: float = WEMM_VRAM_WAIT_SECONDS, poll_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while True:
        free = _vram_free_gb(max_age=0.0)
        if free is None or free >= min_free_gb:
            return True
        if time.time() >= deadline:
            return False
        print(f"[wemm] 空闲显存 {free:.1f}GB < 需求 {min_free_gb:.1f}GB，等待其他模型让路...", file=sys.stderr)
        time.sleep(poll_s)


def _resolve_model_path(model_id: str) -> str:
    """优先用本机 HuggingFace 缓存里已经下好的快照（不重新下载）——真实
    用户机器上如果已经用别的工具下过这个模型（比如旧项目本身），这里应该
    直接复用，不该傻乎乎再下一遍。找不到缓存才原样交给
    AutoModel.from_pretrained 按需下载。"""
    if not model_id:
        return model_id
    if Path(model_id).is_dir():
        return model_id
    hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
    safe = model_id.replace("/", "--").replace(":", "--")
    snapshots = hf_cache / f"models--{safe}" / "snapshots"
    if snapshots.is_dir():
        candidates = sorted(p for p in snapshots.iterdir() if p.is_dir())
        if candidates:
            return str(candidates[-1])
    return model_id


def _load_engine(model_id: str):
    global _engine, _last_use
    with _ENGINE_LOCK:
        if _engine is not None and _engine["model_id"] == model_id:
            _last_use = time.time()
            return _engine
        # 显存互斥：其他 GPU 消费者在线时不硬抢——等它让路（空闲自动卸载 /
        # evict 主动抢占），等不到就报错本条请求，绝不撑爆显存导致整机卡死。
        if not _wait_for_vram(WEMM_MIN_VRAM_GB):
            raise RuntimeError(f"等待空闲显存 >= {WEMM_MIN_VRAM_GB}GB 超时（其他模型占用中），本条请求未执行")
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
    global _engine
    if _engine is None:
        return
    _engine.clear()
    _engine = None
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
            print(f"[wemm] idle {WEMM_IDLE_EXIT_SECONDS}s after unload -> process exit", file=sys.stderr)
            os._exit(0)


def build_messages(kind: str, content):
    if kind == "image":
        return [{"role": "user", "content": [{"type": "image", "image": content}]}]
    if kind == "text":
        return [{"role": "user", "content": [{"type": "text", "text": content}]}]
    raise ValueError(f"未知类型: {kind}")


def _real_embed(kind: str, content, dim: int) -> list[float]:
    try:
        import torch
        import torch.nn.functional as F
        from qwen_vl_utils import process_vision_info
    except ImportError as exc:
        raise RuntimeError(
            "WEMM 依赖(torch/transformers/qwen_vl_utils)未安装——见 plugin 模块 docstring"
        ) from exc

    model_id = os.environ.get("RAG_REDO_WEMM_MODEL", WEMM_MODEL_DEFAULT)
    eng = _load_engine(model_id)
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
            self._json(200, {"ok": True})
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
        if kind not in ("image", "text") or content is None:
            self._json(400, {"ok": False, "error": "kind 必须是 image|text，content 必填"})
            return
        _active_requests += 1
        try:
            _check_idle_unload()
            if kind == "image":
                content = self._decode_image_to_tmpfile(content)
            if os.environ.get("RAG_REDO_FAKE_WEMM"):
                embedding = _fake_embed(kind, content, dim)
            else:
                embedding = _real_embed(kind, content, dim)
            _last_use = time.time()
            self._json(200, {"ok": True, "embedding": embedding, "dim": dim})
        except Exception as exc:  # noqa: BLE001 - 子进程这一侧也不能让异常直接炸掉HTTP响应
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            _active_requests -= 1
            if kind == "image" and isinstance(content, str) and content.endswith(".png"):
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
    port = int(sys.argv[sys.argv.index("--port") + 1])
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
