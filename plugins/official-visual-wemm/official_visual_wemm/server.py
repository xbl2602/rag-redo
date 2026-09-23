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

**已知的、刻意的简化**（相对旧项目）：不做旧项目那套"空闲卸载/空闲自
退出/显存等待/主动 evict 抢占"的精细 GPU 生命周期管理——rag-redo 的
`core/resource_arbiter.py` 本身是比旧项目 `gpu_arbiter.py` 更简单的粗粒度
"具名资源租约"模型（插件 enable 时申请、disable 时释放，不做每请求级别
的协商），这里跟着这个更简单的层级走，模型只在第一次真正被调用时懒加载，
一直留在显存/内存里直到子进程被 on_disable 杀掉——同 official-ocr-mineru-
local 子进程的生命周期粒度一致，不是遗漏。
"""
from __future__ import annotations

import base64
import http.server
import io
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

WEMM_MODEL_DEFAULT = "tencent/WeMM-Embedding-2B"
WEMM_DIM_DEFAULT = 512

_engine = None  # dict(model=, processor=, device=)
_ENGINE_LOCK = threading.Lock()


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
    global _engine
    with _ENGINE_LOCK:
        if _engine is not None and _engine["model_id"] == model_id:
            return _engine
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
        return _engine


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
        if self.path != "/embed":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        kind = payload.get("kind")
        content = payload.get("content")
        dim = int(payload.get("dim") or WEMM_DIM_DEFAULT)
        if kind not in ("image", "text") or content is None:
            self._json(400, {"ok": False, "error": "kind 必须是 image|text，content 必填"})
            return
        try:
            if kind == "image":
                content = self._decode_image_to_tmpfile(content)
            if os.environ.get("RAG_REDO_FAKE_WEMM"):
                embedding = _fake_embed(kind, content, dim)
            else:
                embedding = _real_embed(kind, content, dim)
            self._json(200, {"ok": True, "embedding": embedding, "dim": dim})
        except Exception as exc:  # noqa: BLE001 - 子进程这一侧也不能让异常直接炸掉HTTP响应
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
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
    port = int(sys.argv[sys.argv.index("--port") + 1])
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
