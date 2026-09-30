"""WeMM 页级编码性能探针：回答三个只能实测的问题（2026-09-29）。

**为什么必须实测**：真机一次全量跑了 7645 页、约 30 分钟、显卡利用率只有 30~55%、
功耗 53W。三个候选原因——串行渲染、base64/临时文件浪费、fla 内核缺失（18/24 层
GatedDeltaNet 跑参考实现）——都可以解释这个现象，但**各自占多少时间、batch 能开
多大、余量够不够**，全靠猜就只能瞎优化，甚至把"能跑"改"跑不起来"。

本脚本只读不写：加载模型 → 渲染真实 PDF → 编码 → 报数 → 退出。不碰 data-real，
不建索引，不改任何持久化状态。跑完显存被进程退出回收。

两件事必须用它的输出决定，不能拍脑袋：

1. **fla 装不装**（`--golden` / `--compare`）。已知 fla 版本与 transformers 不匹配时
   会让 Qwen3-Next 输出乱码（上游 issue #792）。对嵌入模型来说"乱码"是**静默产出错误
   向量**——检索悄悄变差、没有任何报错。所以先存 golden 向量，装完再比：余弦低于
   `--min-cos` 直接判失败，不许"看起来能跑"就上。

2. **batch 开多大**（`--batches`）。批处理是最大的提速杠杆（GPU 一次算 N 页），但显存
   刚好卡在门槛上（实测 6.231 GiB vs 门槛 6.3 GiB），开大了就是 CUDA OOM，而 WDDM 下
   的 OOM 会拖垮整机。所以必须先量出每个 batch 的峰值，再选一个有实测余量的值。

用法（Windows）::

    $env:PYTHONIOENCODING = "utf-8"
    .\\plugins\\official-visual-wemm\\official_visual_wemm\\.venv\\Scripts\\python.exe tools\\probe_wemm_perf.py --golden data-real\\wemm_golden.json
    # 装完 fla 再比
    .\\plugins\\official-visual-wemm\\official_visual_wemm\\.venv\\Scripts\\python.exe tools\\probe_wemm_perf.py --compare data-real\\wemm_golden.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

#: 与 server.py / plugin.py 的 WEMM_RENDER_DPI、WEMM_DIM 保持一致。改了这两个数，
#: 签名会变、整个视觉索引失效，这里测出来的数也不再对应线上行为。
RENDER_DPI = 60
DIM = 512

#: fla 一致性闸：装错版本会静默产出错误向量（不是报错），所以要卡死余弦下界。
#: 0.999 意味着 0.1% 以内才算同一个模型的不同内核实现；掉到 0.99 就说明向量几何
#: 已经变了，全库视觉索引必须重建——那时不能靠"导航用途，精度无所谓"糊弄过去，
#: 因为它会悄悄改变用户看到的页级命中结果（BC-07/BC-09）。
MIN_COS = 0.999


def _resolve_model() -> Path:
    """照抄 probe_wemm_vram.py / server.py::_resolve_model_path 的本地快照定位，不下载。"""
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


def _free_gb() -> float:
    import torch

    free, _total = torch.cuda.mem_get_info()
    return free / 2**30


def _total_gb() -> float:
    import torch

    _free, total = torch.cuda.mem_get_info()
    return total / 2**30


def _find_pdf(explicit: str | None) -> Path | None:
    """找一份**真实** PDF 来测渲染耗时。

    先用用户指定的；否则读 `data-real/libraries.json`，挑 PDF 最多的那个库。

    为什么不在 data-real 里 rglob：那里有个 `mutation-vault` 测试库，里面是
    pymupdf 打不开的假 PDF（`no objects found`），盲搜会挑中它，白跑一趟模型加载
    （10 秒 + 5GB 显存）。按"库里 PDF 最多"来挑，既避开了测试库，又落在用户真实
    数据上——渲染耗时对页面类型敏感（讲义 vs 论文 vs 扫描件），得用真页才量得准。
    只读，就是系统平时做的事，不新增任何隐私面。
    """
    # Windows 上 PowerShell 5.1 往原生程序传带 `&`/空格的路径会出问题（用户资料目录
    # 常叫 "USM COURSE RELATED" 这种），命令行传参不可靠，所以先看环境变量。
    if explicit is None:
        explicit = (os.environ.get("WEMM_PROBE_PDF") or "").strip() or None
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    config = Path("data-real") / "libraries.json"
    if not config.is_file():
        return None
    try:
        libraries = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    best: tuple[int, Path] | None = None
    for entry in libraries.values():
        if not isinstance(entry, dict):
            continue
        raw_root = entry.get("root_path")
        if not raw_root:
            continue
        root = Path(raw_root)
        if not root.is_dir():
            continue
        try:
            count = sum(1 for p in root.rglob("*.pdf") if p.is_file() and p.stat().st_size > 0)
        except OSError:
            continue
        if count and (best is None or count > best[0]):
            # 取第一份就够测渲染；确定性优先按排序，免得每次跑挑到不同文件、
            # 导致 golden 向量前后不可比。
            for candidate in sorted(root.rglob("*.pdf")):
                if candidate.is_file() and candidate.stat().st_size > 0:
                    best = (count, candidate)
                    break
    return best[1] if best else None


def _kernels_report() -> dict:
    """报告 fla / causal_conv1d 内核到底有没有真的用上。

    这是"提速是否生效"的直接证据，比墙钟时间可靠：换了内核如果余弦没变但时间变了，
    那就是内核换了；如果时间变了而余弦也变了（超过 MIN_COS），那就是内核算错了。
    """
    report = {"fla": False, "causal_conv1d": False}
    for name in ("fla", "causal_conv1d"):
        try:
            __import__(name)
            report[name] = True
        except Exception:  # noqa: BLE001 - 没装就是没装
            report[name] = False
    return report


def _embed_batch(model, processor, pngs: list[bytes], dim: int) -> list[list[float]]:
    """按 batch 编码若干页，返回归一化后的向量列表。

    调用形态必须和 server.py::_real_embed 一致：`process_vision_info(...,
    image_patch_size=16, ...)` + `model.embedding(**inputs)`（不是 `model(**inputs)`，
    那个返回 CausalLM 输出、没有 embedding 头）。形态不一致量出来的数没有意义。
    """
    import torch
    from qwen_vl_utils import process_vision_info

    messages = [
        [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": f"data:image/png;base64,{base64.b64encode(p).decode('ascii')}"},
                    {"type": "text", "text": "<embedding>"},
                ],
            }
        ]
        for p in pngs
    ]
    prompts = [
        processor.apply_chat_template(m, tokenize=False, add_generation_prompt=False) for m in messages
    ]
    images, videos, video_kwargs = process_vision_info(
        messages, image_patch_size=16, return_video_kwargs=True, return_video_metadata=True
    )
    video_metadata = None
    if videos is not None:
        videos, video_metadata = (list(part) for part in zip(*videos))
        videos, video_metadata = list(videos), list(video_metadata)
    inputs = processor(
        text=prompts, images=images, videos=videos, video_metadata=video_metadata,
        return_tensors="pt", **(video_kwargs or {}),
    ).to("cuda")
    with torch.inference_mode():
        emb = model.embedding(**inputs).float()
    emb = torch.nn.functional.normalize(emb[..., :dim], dim=-1)
    return emb.cpu().tolist()


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    return num / (da * db) if da and db else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="WeMM 页级编码性能与一致性探针")
    ap.add_argument("--model", default=None, help="模型目录，默认按 HF 缓存布局找")
    ap.add_argument("--pdf", default=None, help="用来测渲染耗时的真实 PDF，默认在 data-real 里找")
    ap.add_argument("--pages", type=int, default=4, help="取几页来测（默认 4）")
    ap.add_argument("--dpi", type=int, default=None,
                    help="覆盖渲染 DPI（默认 60，与线上签名一致）。注意：DPI 在视觉签名里，"
                         "改了会让已建索引全部失效——这里只是量速度，不改代码。")
    ap.add_argument("--batches", default="1,2,4", help="要测的 batch 档位，逗号分隔")
    ap.add_argument("--repeat", type=int, default=3, help="每档重复几次取中位数")
    ap.add_argument("--golden", default=None, help="把当前向量写到这个文件（装 fla 之前跑）")
    ap.add_argument("--compare", default=None, help="和这个文件里的向量比余弦（装 fla 之后跑）")
    ap.add_argument("--min-cos", type=float, default=MIN_COS, help="余弦下界，默认 0.999")
    args = ap.parse_args()

    model_dir = Path(args.model) if args.model else _resolve_model()
    print(f"模型: {model_dir}")
    if not model_dir.is_dir():
        print("模型目录不存在", file=sys.stderr)
        return 1

    pdf = _find_pdf(args.pdf)
    if pdf is None:
        print("找不到可用的 PDF（--pdf 指定，或在 data-real 下放一份）", file=sys.stderr)
        return 1
    print(f"测速 PDF: {pdf}")

    import torch

    print(f"内核: {_kernels_report()}")
    before_free = _free_gb()
    total_gb = _total_gb()
    print(f"加载前空闲显存: {before_free:.3f} / {total_gb:.3f} GiB")
    # 这一段是踩过的坑：2026-09-29 第一次跑，屏上明明有一个 `Obsidian RAG 2.0` 窗口
    # （**旧项目 obsidian-rag**）开着，它的两个 server.py 子进程占着 2GB 显存，于是
    # "加载后空闲"只剩 0.704 GiB，一度让人以为模型突然变大了、批处理没戏了。
    # 显卡是 WDDM，nvidia-smi 的进程级归因不可信，所以这里只能看总量并**如实说
    # 存疑**——让人自己决定要不要先去关掉别的程序，而不是让探针默默给一份被污染的数。
    if before_free < total_gb - 6.5:
        print(
            f"⚠️ 警告：加载前空闲只剩 {before_free:.2f} GiB，很可能还有别的 GPU 程序在占"
            f"（旧项目 / 浏览器 / 其它客户端）。下面的 batch 峰值显存会偏小、"
            f"编码耗时也可能被抢。测批处理前请先关掉占显存的程序。",
            file=sys.stderr,
        )

    from transformers import AutoModel, AutoProcessor

    t0 = time.time()
    processor = AutoProcessor.from_pretrained(str(model_dir), trust_remote_code=True)
    model = AutoModel.from_pretrained(str(model_dir), trust_remote_code=True)
    model = model.to("cuda", dtype=torch.bfloat16).eval()
    print(f"加载耗时: {time.time() - t0:.1f}s   加载后空闲显存: {_free_gb():.3f} GiB")
    after_load_free = _free_gb()

    # ── 第一段：渲染（#3 要重叠的就是它）──
    import pymupdf

    dpi = args.dpi or RENDER_DPI
    print(f'  DPI={dpi}')
    document = pymupdf.open(str(pdf))
    page_count = min(int(args.pages), int(document.page_count))
    render_times: list[float] = []
    pngs: list[bytes] = []
    for i in range(page_count):
        t = time.time()
        pngs.append(document.load_page(i).get_pixmap(dpi=dpi).tobytes("png"))
        render_times.append(time.time() - t)
    document.close()
    avg_render = statistics.median(render_times)
    avg_png_kb = statistics.median(len(p) for p in pngs) / 1024
    print("")
    print(f"渲染 {page_count} 页: 中位 {avg_render * 1000:.0f} ms/页   PNG {avg_png_kb:.0f} KB/页")

    # ── 第二段：#4 省掉的那部分（base64 + JSON 转义 + 落盘）──
    t = time.time()
    for p in pngs:
        base64.b64encode(p).decode("ascii")
    b64_s = (time.time() - t) / len(pngs)
    disk_s = 0.0
    for p in pngs:
        fd, tmp = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as fh:
            fh.write(p)
        with open(tmp, "rb") as fh:
            fh.read()
        os.unlink(tmp)
    disk_s /= len(pngs)
    print(f"#4 可省: base64 {b64_s * 1000:.0f} ms/页 + 落盘回读 {disk_s * 1000:.0f} ms/页")

    # ── 第三段：batch 曲线（决定批处理开多大）──
    print("")
    print(f"{'batch':>6} {'ms/页':>9} {'相对b1':>8} {'峰值显存':>10}")
    results: dict[str, dict] = {}
    base_ms = None
    for b in [int(x) for x in args.batches.split(",") if x.strip()]:
        pool = (pngs * ((b // len(pngs)) + 1))[:b]
        samples: list[float] = []
        peak = 0.0
        ok = True
        for _ in range(max(1, args.repeat)):
            try:
                torch.cuda.reset_peak_memory_stats()
                torch.cuda.synchronize()
                t = time.time()
                _embed_batch(model, processor, pool, DIM)
                torch.cuda.synchronize()
                dt = time.time() - t
                samples.append(dt)
                peak = max(peak, torch.cuda.memory_reserved())
            except torch.cuda.OutOfMemoryError:
                ok = False
                break
        if not ok or not samples:
            print(f"{b:>6} {'OOM':>9} {'-':>8} {'-':>10}")
            results[str(b)] = {"ok": False}
            break
        ms_per_page = statistics.median(samples) / b * 1000
        if base_ms is None:
            base_ms = ms_per_page
        results[str(b)] = {
            "ok": True,
            "ms_per_page": ms_per_page,
            "peak_reserved_gb": peak / 2**30,
            "total_free_after_gb": _free_gb(),
        }
        print(f"{b:>6} {ms_per_page:>9.0f} {base_ms / ms_per_page:>7.2f}x {peak / 2**30:>9.3f}G")

    # ── 第四段：golden 向量 / 一致性比对 ─
    vectors = _embed_batch(model, processor, pngs[:min(2, len(pngs))], DIM)
    payload = {
        "model": str(model_dir),
        "dpi": dpi,
        "dim": DIM,
        "kernels": _kernels_report(),
        "vectors": vectors,
    }

    exit_code = 0
    if args.golden:
        Path(args.golden).parent.mkdir(parents=True, exist_ok=True)
        Path(args.golden).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"\ngolden 向量已存: {args.golden}")

    if args.compare:
        golden_path = Path(args.compare)
        if not golden_path.is_file():
            print(f"golden 文件不存在: {golden_path}", file=sys.stderr)
            return 1
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        if len(golden.get("vectors") or []) != len(vectors):
            print("页数对不上，无法比对", file=sys.stderr)
            return 1
        worst = min(_cos(a, b) for a, b in zip(golden["vectors"], vectors))
        print(f"\ngolden 基准内核: {golden.get('kernels')}")
        print(f"当前内核:       {payload['kernels']}")
        print(f"最差余弦: {worst:.6f}   阈值 {args.min_cos}")
        if worst < args.min_cos:
            print(
                "\n结论：**拒绝**。余弦低于阈值说明向量几何变了——多半是 fla 版本与 "
                "transformers 不匹配（上游 issue #792：Qwen3-Next 会输出乱码）。"
                "对嵌入模型这就是静默产出错误向量、检索悄悄变差。"
                "此时要么钉死 fla 版本，要么卸载 fla。",
                file=sys.stderr,
            )
            exit_code = 3
        else:
            print("结论：**通过**。余弦在阈值内，内核换对了。")

    del model
    import gc

    gc.collect()
    torch.cuda.empty_cache()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
