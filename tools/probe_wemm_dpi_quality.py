"""DPI 到底能不能降？——用真实检索任务实测（2026-09-29）。

**为什么必须实测，不能靠推理**：降 DPI 在速度上非常划算（实测 141→94 ms/页，
1.5x），但 `WEMM_RENDER_DPI` 写在视觉索引签名里，改了就等于**已建的页级索引全部失效**，
而检索质量会不会塌，没有任何文档能回答——官方模型卡只字未提分辨率/DPI，
`processor_config.json` 只给出「不会缩放」的面积范围（65536~16777216），
可"不会被缩放"跟"字还看得清"完全是两件事。10pt 正文在 36 DPI 下只有约 5px 高。

**这个脚本测的正是产品在做的事**：拿页面里的真实文字当查询，看能不能检索回那一页。
不是"两张图像不像"（那个用余弦量就够了，但余弦高不代表排序对），而是**排序对不对**——
导航场景里用户问的是"这句话/这个图在哪一页"，能排对才算能用。

读数怎么解释：
- `top1@60` / `top1@36`：用页面真实文字查回自己，低 DPI 的 top1 明显低于 60 就是塌了。
- `MRR`：平均倒数排名，对"排在第 2、第 3"比"排不进前 10"更宽容。
- `rank保持`：低 DPI 下正确页的排名没有变差的查询占比。比只看 top1 更细。

只读不写：加载模型 → 渲染同一批页 → 编码 → 排序 → 报数 → 退出。
不碰 data-real，不建索引。跑完显存随进程退出回收。

用法::

    $env:PYTHONIOENCODING = "utf-8"
    $env:WEMM_PROBE_PDF = "D:\\...\\某本教材.pdf"
    .\\plugins\\official-visual-wemm\\official_visual_wemm\\.venv\\Scripts\\python.exe tools\\probe_wemm_dpi_quality.py --dpis 60,45,36 --pages 24
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

RENDER_DPI = 60   # 线上默认值（与旧项目一致）
DIM = 512         # 与 server.py WEMM_DIM 一致


def _resolve_model() -> Path:
    override = (os.environ.get("WEMM_MODEL_DIR") or "").strip()
    if override:
        return Path(override)
    hub = Path(os.environ.get("HF_HUB_CACHE") or (Path.home() / ".cache" / "huggingface" / "hub"))
    safe = "tencent--WeMM-Embedding-2B"
    root = hub / f"models--{safe}" / "snapshots"
    if not root.is_dir():
        return hub / f"models--{safe}"
    for snap in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True):
        return snap
    return root


def _encode(model, processor, items: list, kind: str, dim: int) -> list[list[float]]:
    """按 server.py::_real_embed 的形态批量编码（image 用 data URL，text 走同一模板）。"""
    import torch
    from qwen_vl_utils import process_vision_info

    messages = []
    for it in items:
        if kind == "image":
            # 传进来的就是 PNG 原始字节，data URL 要的是 base64 文本。
            content = [
                {
                    "type": "image",
                    "image": "data:image/png;base64,"
                    + __import__("base64").b64encode(it).decode("ascii"),
                }
            ]
        else:
            content = [{"type": "text", "text": it}]
        messages.append([{"role": "user", "content": content + [{"type": "text", "text": "<embedding>"}]}])
    prompts = [processor.apply_chat_template(m, tokenize=False, add_generation_prompt=False) for m in messages]
    images, videos, video_kwargs = process_vision_info(
        messages, image_patch_size=16, return_video_kwargs=True, return_video_metadata=True
    )
    video_metadata = None
    if videos is not None:
        videos, video_metadata = (list(part) for part in zip(*videos))
        videos, video_metadata = list(videos), list(video_metadata)
    # 纯文本批次长度不一，必须显式 padding——服务端 `do_POST` 一次只收一条（长度天然
    # 一致）所以从没踩过这个坑；这里一次编 24 条查询就会踩到。
    text_kwargs = {"padding": True} if kind == "text" else {}
    inputs = processor(
        text=prompts, images=images, videos=videos, video_metadata=video_metadata,
        return_tensors="pt", **text_kwargs, **(video_kwargs or {}),
    ).to("cuda")
    with torch.inference_mode():
        emb = model.embedding(**inputs).float()
    return torch.nn.functional.normalize(emb[..., :dim], dim=-1).cpu().tolist()


def _rank_of(query: list[float], pool: list[list[float]], truth: int) -> int:
    """正确页在按余弦降序里的名次（0 = 第一名）。"""
    scores = [sum(a * b for a, b in zip(query, doc)) for doc in pool]
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return order.index(truth)


def main() -> int:
    ap = argparse.ArgumentParser(description="降 DPI 到底还能不能检索：实测排序质量")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--dpis", default="60,45,36", help="要对比的 DPI 档位，第一个当作基准")
    ap.add_argument("--pages", type=int, default=24, help="取多少页做样本")
    args = ap.parse_args()

    pdf_raw = args.pdf or (os.environ.get("WEMM_PROBE_PDF") or "").strip()
    if not pdf_raw:
        print("需要 --pdf 或环境变量 WEMM_PROBE_PDF", file=sys.stderr)
        return 1
    pdf = Path(pdf_raw)
    if not pdf.is_file():
        print(f"PDF 不存在: {pdf}", file=sys.stderr)
        return 1
    dpis = [int(x) for x in args.dpis.split(",") if x.strip()]

    import pymupdf
    import torch
    from transformers import AutoModel, AutoProcessor

    document = pymupdf.open(str(pdf))
    # 跨全书均匀取样，并**跳过几乎没有文字的页**（封面/空白/纯图）——
    # 那类页的"查询"没有代表性，会把 top1 拉成一个和 DPI 无关的常数。
    total = int(document.page_count)
    stride = max(1, total // args.pages)
    picked: list[int] = []
    texts: list[str] = []
    for i in range(0, total, stride):
        if len(picked) >= args.pages:
            break
        page_text = " ".join(document.load_page(i).get_text().split())
        if len(page_text) < 200:
            continue
        picked.append(i)
        texts.append(page_text)
    if len(picked) < 6:
        print("文字页太少，样本不具代表性，换一份 PDF 或加 --pages", file=sys.stderr)
        return 1
    print(f"PDF: {pdf.name}  共 {total} 页，取样 {len(picked)} 页")
    print(f"取样页号: {picked[:12]}{' ...' if len(picked) > 12 else ''}")

    # 查询取页面正文里一句完整的话——模拟用户"记得那句话"而不是"记得整页"，
    # 这才是导航场景里真实的用法（也最吃分辨率）。
    queries: list[str] = []
    for page_text in texts:
        sentences = re.split(r"(?<=[.!?])\s+", page_text)
        pick = next((s for s in sentences if 40 <= len(s) <= 220), None)
        if pick is None:
            pick = page_text[:180]
        queries.append(pick)
    print(f"示例查询: {queries[0][:100]}...")

    model_dir = _resolve_model()
    if not model_dir.is_dir():
        print(f"模型目录不存在: {model_dir}", file=sys.stderr)
        return 1

    from transformers import AutoProcessor as _AP  # noqa: F401  (显式说明只用上面那个)

    processor = _AP.from_pretrained(str(model_dir), trust_remote_code=True)
    model = AutoModel.from_pretrained(str(model_dir), trust_remote_code=True)
    model = model.to("cuda", dtype=torch.bfloat16).eval()

    # 查询向量与 DPI 无关：同一批查询向量比各个 DPI，才知道差别来自页面而不是查询。
    qvecs = _encode(model, processor, queries, "text", DIM)
    print(f"查询已编码: {len(qvecs)} 条")

    results: dict[int, dict] = {}
    for dpi in dpis:
        pngs = [document.load_page(i).get_pixmap(dpi=dpi).tobytes("png") for i in picked]
        # 必须先预热再计时：Triton 内核是 JIT 的，第一次遇到某个 shape 要现编译，
        # 能把单页从 90ms 拖到几秒。不预热的话这个脚本第一档 DPI 会报出
        # "4396 ms/页"这种明显荒谬的数（踩过一次）。
        _encode(model, processor, pngs[:2], "image", DIM)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        t0 = time.time()
        pvecs = _encode(model, processor, pngs, "image", DIM)
        enc_ms = (time.time() - t0) / len(pngs) * 1000
        ranks = [_rank_of(q, pvecs, idx) for idx, q in enumerate(qvecs)]
        results[dpi] = {
            "ranks": ranks,
            "top1": sum(1 for r in ranks if r == 0) / len(ranks),
            "mrr": sum(1 / (r + 1) for r in ranks) / len(ranks),
            "ms_per_page": enc_ms,
        }

    base = dpis[0]
    print("")
    print(f"{'DPI':>5} {'ms/页':>8} {'top1':>7} {'MRR':>7} {'中位名次':>9} {'速度':>8}")
    base_ms = results[base]["ms_per_page"]
    for dpi in dpis:
        r = results[dpi]
        print(
            f"{dpi:>5} {r['ms_per_page']:>8.0f} {r['top1']:>7.1%} {r['mrr']:>7.3f} "
            f"{statistics.median(r['ranks']) + 1:>9.0f} {base_ms / r['ms_per_page']:>7.2f}x"
        )

    print("")
    print(f"以 DPI {base} 为基准的退化：")
    for dpi in dpis[1:]:
        b, r = results[base], results[dpi]
        worse = sum(1 for x, y in zip(b["ranks"], r["ranks"]) if y > x)
        print(
            f"  DPI {dpi}: top1 {b['top1']:.1%} -> {r['top1']:.1%}"
            f"（差 {b['top1'] - r['top1']:+.1%}），MRR {b['mrr']:.3f} -> {r['mrr']:.3f}，"
            f"{worse}/{len(r['ranks'])} 个查询排名变差"
        )

    del model
    import gc

    gc.collect()
    torch.cuda.empty_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())
