"""从 git HEAD 重建 behavior_contract.json，再通过 JSON API 重放本轮所有改动。

为什么用脚本而不是手改：BC-04/BC-11 的验收文案里有大量中文引号与 JSON 结构引号
混排，手工替换已经连续三次把文件改坏（`Expecting ',' delimiter` /
`Expecting property name` / `Expecting value`）。走 JSON 解析→改字段→序列化，
结构引号由序列化器保证，配对不可能错。

跑完即可删除。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

P = Path("docs/behavior_contract.json")

base = subprocess.run(
    ["git", "show", "HEAD:docs/behavior_contract.json"],
    capture_output=True, check=True,
).stdout.decode("utf-8")
data = json.loads(base)
contracts = data["contracts"] if isinstance(data, dict) and "contracts" in data else data
by_id = {c["id"]: c for c in contracts}

# ── BC-11 跨进程资源仲裁 ────────────────────────────────────────────────
bc11 = by_id["BC-11"]
for ref in (
    "obsidian-rag/index.py:703-742",
    "obsidian-rag/gpu_arbiter.py:441-455",
):
    if ref not in bc11["legacy_refs"]:
        bc11["legacy_refs"].append(ref)

bc11["redo_refs"] = [
    ("rag-redo/core/pipeline.py:_make_room_for_visual_index"
     if "_release_text_models_for_visual" in r
     else r)
    for r in bc11["redo_refs"]
]
for ref in (
    "rag-redo/core/pipeline.py:Pipeline._VISUAL_HANDOFF_TARGETS",
    "rag-redo/core/gpu_arbiter.py:request_evict",
):
    if ref not in bc11["redo_refs"]:
        bc11["redo_refs"].append(ref)

for ref in (
    "rag-redo/tests/test_pipeline_e2e.py::TestEndToEndSearchPipeline."
    "test_the_visual_handoff_also_asks_the_local_ocr_service_to_give_up_the_gpu",
    "rag-redo/tests/test_pipeline_e2e.py::TestEndToEndSearchPipeline."
    "test_the_visual_handoff_never_asks_the_visual_service_to_evict_itself",
    "rag-redo/plugins/official-visual-wemm/tests/test_plugin.py::"
    "TestVramGateIsReportedTruthfully",
    "rag-redo/plugins/official-gui-shell/tests/test_contracts_parity.py::"
    "TestProgressSnapshot.test_a_broken_vram_probe_never_empties_the_whole_snapshot",
):
    if ref not in bc11["test_refs"]:
        bc11["test_refs"].append(ref)

BC11_HANDOFF = (
    "【缺陷修复，恢复旧行为（2026-09-29）】让路必须覆盖到**独立子进程**里的模型，"
    "不只是核心自己进程里的文字模型：旧项目 index.py:703-742 `_vram_maybe_evict_wemm` "
    "在加载任何 CUDA 模型前，先按「空闲显存不足才动作、fail-open、绝不阻塞加载」的纪律，"
    "依次请求 MinerU（gpu_arbiter.py:441-455 `evict_mineru`）与 WEMM 让路；rag-redo 此前只卸 "
    "bge/reranker，占约 4GB 显存的 MinerU 子进程一直坐着。8GB 卡上本机 MinerU 最低要 4.5GB、"
    "WEMM 要 5.5GB，物理上不能共存，于是 WEMM 服务端 `_wait_for_vram(5.5GB)` 一直等不到。"
    "现由 `Pipeline._make_room_for_visual_index` 在 before_serve 时依次调用各插件的 "
    "`release_gpu()`（子进程侧是 `/evict` 软驱逐，只卸模型、本体存活，与资源仲裁器 "
    "on_preempt 同一套路子），每个插件独立隔离、失败只记 warning 不抛。调用方自己"
    "（official-visual-wemm）必须在让路名单之外，否则等于自我驱逐。"
)

BC11_TORCH = (
    "补充事实（2026-09-29 实测，纠正一个一度写错的判断）：判断显存余量**必须用 torch 的 "
    "`mem_get_info`，不能用 nvidia-smi**。本机（WDDM 笔记本，RTX 5060 Laptop 8151 MiB）"
    "两者相差约 5.2 GiB（torch 报空闲 6.878 GiB、nvidia-smi 报 1.681 GiB）——nvidia-smi 把大量"
    "系统内存计入显存占用，读数严重偏低。据此一度得出「这台机器装不下 WEMM」的错误结论并"
    "写进了 ROADMAP 与本条，均已更正。旧项目 2026-09-09 确实加载成功过"
    "（`data/wemm_server.log`：「model loaded in 8.7s -> cuda; gpu_mem=5.07GB」，"
    "`data/wemm_meta_LECTURE NOTE.json` 有 17 份 PDF 真编完页）。"
)

BC11_VRAM = (
    "【缺陷修复 + 用户可见能力（2026-09-29 晚，操作者确认）】页级视觉导航的显存门槛必须"
    "**在本机实测标定**、不足时**带数字快速失败**、并允许**用户强制加载**。"
    "①门槛原为 5.5（照搬旧项目同名常量，从未在本机验证），实测（`tools/probe_wemm_vram.py`，"
    "用 server.py `_real_embed` 同一调用形态：真实需求是 **6.231 GiB**——加载吃 5.842"
    "（其中 torch `memory_allocated` 只有 5.070，**旧项目报的 `gpu_mem=5.07GB` 漏掉了 CUDA "
    "上下文与 cuBLAS/cuDNN 句柄的 0.77 GiB**）+ 编一页再吃 0.404。门槛 5.5 比真实需求**低** "
    "0.73 GiB，照它放行会让模型起来后差一截，触发 OOM 或 WDDM 共享内存抖动（整机变卡，"
    "正是本条反复警告的失败模式）——所以是太保守的反面、是漏算。现定 6.3。该值不可再压："
    "试过 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`，占用 6.231→6.230（那 0.678 GiB "
    "是上下文与句柄而非碎片）；`WEMM_DIM` 512→256 也不省（省的是向量存储不是权重）。"
    "②等待上限从 900s 降到 60s：让路是**主动**发生的（`/evict` 软驱逐、bge/reranker 空闲"
    "自动卸载，都是秒级），60s 还等不到基本就是「这块卡此刻装不下」；原来 900s 期间界面只见"
    "「空闲显存 5.2GB < 需求 5.5GB」反复刷屏、宿主超时熔断后服务端还在干等。"
    "③显存不足时服务端回结构化错误 `{ok:false, reason:vram, required_gb, free_gb}`，插件把两个"
    "数字记进 `status()[\"vram\"]`，GUI 快照把「需要 X GB、当前 Y GB、文字索引不受影响、"
    "可以去设置里开强制加载」写进 `progress.heartbeat_note`（前端本来就会渲染这个字段，"
    "**因此不需要改逐字节冻结的前端**）。**不静默失效**是这条的要点。"
    "④新增设置项 `wemm_force_load`（布尔，默认关，设置页由后端 `groups` 动态渲染）：开着时"
    "服务端只**跳过那道门槛去试**、**不吞异常**——真 OOM 仍明确报错并把本轮页级索引记为可重试"
    "终态，绝不伪装成功、也绝不静默截断向量污染已建好的页库。**BC-15 的冻结值未改**："
    "真模态弹窗需要改前端资产（sha256 会变），按 §3.3 须操作者另行确认，"
    "本轮只做「数字可见 + 设置可开关」。"
)

acc = bc11["acceptance"]
for chunk in (BC11_HANDOFF, BC11_TORCH, BC11_VRAM):
    head = chunk[:24]
    if head not in acc:
        acc += chunk
bc11["acceptance"] = acc

# ── BC-04 失败终态与重试 ────────────────────────────────────────────────
bc04 = by_id["BC-04"]
if "obsidian-rag/wemm_indexer.py:257-278" not in bc04["legacy_refs"]:
    bc04["legacy_refs"].append("obsidian-rag/wemm_indexer.py:257-278")
if not any("official_visual_wemm" in r for r in bc04["redo_refs"]):
    bc04["redo_refs"].append(
        "rag-redo/plugins/official-visual-wemm/official_visual_wemm/plugin.py:index_library"
    )
for ref in (
    "rag-redo/tests/test_pipeline_e2e.py::TestEndToEndSearchPipeline."
    "test_the_text_index_is_still_published_when_the_visual_index_cannot_run",
    "rag-redo/plugins/official-visual-wemm/tests/test_plugin.py::"
    "TestVisualIndexStopsWhenTheServiceIsUnreachable",
):
    if ref not in bc04["test_refs"]:
        bc04["test_refs"].append(ref)

BC04_VISUAL = (
    "【缺陷修复，恢复旧行为（2026-09-29 晚，BC-11 配套）】页级视觉索引的失败必须同时满足两条，"
    "缺一条就会把整轮文字索引赔进去：①**快速失败**——子进程服务不可达（连接被拒/超时/已死）"
    "时，本轮页级索引立刻收手，不许逐页重试。旧项目 wemm_indexer.py:257-278（问题46「单轮"
    "单次」）的语义是「服务本轮不可用就记终态、不再拉起」；rag-redo 曾对每页失败只 `continue` "
    "换下一页，78 份 PDF × 每份几十页＝几千次注定失败的调用，索引进程假活（心跳线程独立照刷、"
    "视觉阶段有 300 秒停滞宽限，界面显示一切正常），用户以为卡死而中断。②**不连坐**——视觉"
    "没建成不许阻止文字索引发布。core/pipeline.py 的 manifest 写入与 generation commit 排在"
    "视觉阶段之后，视觉阶段一旦挂住或被中断，已经算好向量、切好块的文字索引一份都不落盘。"
    "真机证据：data-real/index_progress/Y2S1-*.json 停在 files_done=78/78、phase=visual，库的"
    "「最后索引时间」仍是上一轮的。视觉失败的文件记可重试终态（status=failed，下一轮自动"
    "重试），已编出页的文件保留 indexed/partial（宁可 partial 也不丢已付出的编码），文字索引"
    "照常发布、照常能搜到。熔断只针对服务级故障；服务健康、只是某一页编码不出来仍然逐页继续"
    "——把单页失败也当服务挂了会让一次手抖毁掉整轮。"
)
acc04 = bc04["acceptance"]
if "【缺陷修复，恢复旧行为（2026-09-29 晚，BC-11 配套）】" not in acc04:
    bc04["acceptance"] = acc04 + BC04_VISUAL

P.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
check = json.loads(P.read_text(encoding="utf-8"))
b = next(c for c in check["contracts"] if c["id"] == "BC-11")
print("JSON OK，契约数", len(check["contracts"]))
print("BC-11 acceptance", len(b["acceptance"]), "字；test_refs", len(b["test_refs"]))
print("BC-04 test_refs", len(next(c for c in check['contracts'] if c['id']=='BC-04')['test_refs']))
