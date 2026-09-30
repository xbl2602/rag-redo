"""实测 WeMM-Embedding-2B 在本机的真实显存占用（2026-09-29）。

**为什么必须实测而不是推算**：`server.py` 的 `WEMM_MIN_VRAM_GB = 5.5` 是照搬旧
项目 `gpu_arbiter.py` 的同名常量，两边都**没有在这台机器上重新校准过**。权重
文件 `model.safetensors` 是 5190MB，2B 参数 bf16 理论权重约 4GB，剩下的 1GB 是
CUDA 上下文、激活峰值和分配器碎片——这部分**只能实测**，因为它随页面分辨率、
批大小、torch 版本、分配器行为变化。拿旧项目的 5.07GB 当结论，等于用一个
没验证过的数去卡真机。

本脚本只读不写：加载模型 → 编码一页真实 PDF → 报数 → 退出。不碰 data-real，
不建索引，不改任何持久化状态。跑完显存会被进程退出回收。
"""
from __future__ import annotations

import gc
import json
import os
import sys
import time
from pathlib import Path

def _resolve_model() -> Path:
    """按 server.py::_resolve_model_path 同款方式找本地快照，不下载。

    照抄那套 `models--{org}--{name}/snapshots/<rev>` 布局，是为了让测出来的数
    对准服务端真正会加载的那份权重，而不是另找一份。
    """
    override = (os.environ.get("WEMM_MODEL_DIR") or "").strip()
    if override:
        return Path(override)
    hub = Path(os.environ.get("HF_HUB_CACHE") or (Path.home() / ".cache" / "huggingface" / "hub"))
    safe = "tencent--WeMM-Embedding-2B"
    root = hub / f"models--{safe}" / "snapshots"
    if not root.is_dir():
        return hub / f"models--{safe}"
    for snapshot in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True):
        return snapshot
    return root


MODEL = _resolve_model()
RENDER_DPI = 60          # 与 server.py 的 WEMM_RENDER_DPI 一致
DIM = 512               # 与 server.py 的 WEMM_DIM 一致


def _free_gb() -> float:
    import torch

    free, _total = torch.cuda.mem_get_info()
    return free / 2**30


def _used_gb() -> float:
    import torch

    return (torch.cuda.memory_allocated() + torch.cuda.memory_reserved()) / 2**30


def _make_page_png() -> bytes:
    """造一张接近真实 lecture slide 的页面（1600x1200 左右的图）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1400, 1000), (250, 250, 245))
    d = ImageDraw.Draw(img)
    for i in range(28):
        y = 90 + i * 32
        d.rectangle([60, y, 60 + 700 + (i * 37) % 500, y + 14], fill=(40, 40, 40))
    d.rectangle([60, 20, 700, 60], fill=(20, 20, 20))
    d.ellipse([900, 200, 1300, 600], outline=(200, 60, 60), width=6)
    d.line([900, 200, 1300, 600], fill=(60, 90, 200), width=4)
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    import torch

    print(json.dumps({"stage": "start", "model": str(MODEL)}, ensure_ascii=False))
    if not MODEL.is_dir():
        print(f"模型目录不存在: {MODEL}", file=sys.stderr)
        return 1

    before_free = _free_gb()
    print(f"加载前空闲显存: {before_free:.3f} GiB")

    from transformers import AutoModel, AutoProcessor

    t0 = time.time()
    processor = AutoProcessor.from_pretrained(str(MODEL), trust_remote_code=True)
    model = AutoModel.from_pretrained(str(MODEL), trust_remote_code=True)
    model = model.to("cuda", dtype=torch.bfloat16).eval()
    load_s = time.time() - t0
    after_free = _free_gb()
    print(f"加载耗时: {load_s:.1f}s")
    print(f"加载后空闲显存: {after_free:.3f} GiB")
    print(f"模型实际吃掉:  {before_free - after_free:.3f} GiB")
    print(f"  torch 分配器: allocated={torch.cuda.memory_allocated()/2**30:.3f} "
          f"reserved={torch.cuda.memory_reserved()/2**30:.3f} GiB")

    # 完全照抄 server.py::_real_embed 的调用方式：`model.embedding(**inputs)`
    # （不是 `model(**inputs)`，那个返回的是 CausalLM 输出、没有 embedding 头），
    # `process_vision_info(..., image_patch_size=16, ...)` 也是服务端的参数。
    # 调用形态不一致就量不准，测出来的数没有意义。
    import base64

    from qwen_vl_utils import process_vision_info

    png = _make_page_png()
    print(f"渲染页 PNG: {len(png)/1024:.0f} KB")
    encoded = base64.b64encode(png).decode("ascii")
    after_load_free = _free_gb()
    peak_reserved = torch.cuda.memory_reserved()
    t1 = time.time()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": f"data:image/png;base64,{encoded}"},
                {"type": "text", "text": "<embedding>"},
            ],
        }
    ]
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
    ).to("cuda")
    with torch.inference_mode():
        emb = model.embedding(**inputs).float()
    emb = torch.nn.functional.normalize(emb[..., :DIM], dim=-1)
    vec = emb.cpu().tolist()[0]
    torch.cuda.synchronize()
    enc_s = time.time() - t1
    after_enc_free = _free_gb()
    print(f"编码一页耗时: {enc_s:.2f}s   向量维度: {len(vec)}")
    print(f"编码峰值 reserved: {max(peak_reserved, torch.cuda.memory_reserved())/2**30:.3f} GiB")
    print(f"编码一页额外吃掉: {after_load_free - after_enc_free:.3f} GiB")

    total = before_free - after_enc_free
    print("")
    print("=" * 70)
    print(f"加载 {before_free - after_load_free:.3f} + 编码 {after_load_free - after_enc_free:.3f}")
    print(f"实测总占用 {total:.3f} GiB")
    print(f"当前门槛 5.5 GiB → {'偏低（模型实际要更多）' if total > 5.5 else '够，还有余量'}")
    print("=" * 70)

    del model
    gc.collect()
    torch.cuda.empty_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())
