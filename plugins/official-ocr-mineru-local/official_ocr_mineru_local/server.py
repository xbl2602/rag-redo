#!/usr/bin/env python3
"""official-ocr-mineru-local 的子进程服务端——单独进程运行。

真正解析走 MinerU 官方 `mineru.cli.api_client.ReusableLocalAPIServer`（和
`mineru-api` 命令背后是同一套东西）——本服务只是一层"壳"：绑端口、做单次
串行调度（8GB卡一次只跑一个请求，叠加会爆显存；2026-09-29 起一个请求可以是
几份小扫描件合成的一批，上限与显存把关见 `_real_ocr_many`）、管这个内服务自己的懒加载/
两级空闲释放/`/evict` 软驱逐，真正的PDF解析逻辑完全在 MinerU 官方代码里，
这里不重新实现。第一个请求才懒拉起内服务，空闲自动停掉释放显存，壳进程
自己超时也退出——8GB 卡上与 WEMM/bge-m3 错峰，绝不共存。行为对齐旧项目
obsidian-rag 的 mineru_server.py（同一个 MinerU 版本、同一套
ReusableLocalAPIServer 调用方式、同样的单文件超时公式/页数上限/双向抢占
协议），按 rag-redo 插件化原则重写：HTTP 协议形状（/extract 收
{path,root}、回 {text,failure_reason}）跟其他 rag-redo extractor 插件
一致，不是照抄 obsidian-rag 自己的 {pdf_path}/{md} 形状。

**为什么 MinerU 相关 import 全部懒加载在函数内部**：这个子进程平时应该用
本机已经装好的 MinerU CLI 工具环境（`uv tool install --python 3.12 -U
"mineru[all]"`，见 plugin.py::_resolve_mineru_python）启动，但
`RAG_REDO_FAKE_OCR=1` 测试模式下实际是用核心自己的（没装 mineru 的）解释
器启动的——模块顶层不能有任何会立刻失败的 mineru import，同
official-visual-wemm/server.py 的道理。

**隐私**：PDF 只读本机文件、解析全程不出网；日志只有文件名与耗时摘要，
绝不含正文。

**真机调试时抓到的严重坑：内服务（mineru-api）会卡死在"启动中"永远不
就绪**（2026-09-23，真实用本机 MinerU 环境跑通整条链路时发现，不是猜的）
——根因是 MinerU 官方 `ReusableLocalAPIServer` 启动内服务子进程时没有显式
指定 `stdout`/`stderr`（见 mineru/cli/api_client.py），默认继承调用方（也就
是这个壳进程）的文件描述符；而这个壳进程当时是被
`core/subprocess_service.py::SubprocessServiceHandle` 用
`stdout=PIPE, stderr=PIPE` 启动的——那两个管道只在崩溃诊断时才读一次，平时
没人持续排空。内服务（uvicorn+loguru）启动时的日志量一旦把 Windows 管道的
默认缓冲区（64KB）写满，`write()` 系统调用就会阻塞，内服务从此卡在"进程
活着、端口没监听完、/health 永远连不上"——实测跑满了 300s 就绪超时，直接
构造脚本单独跑（不经过这层被吞的管道）反而几秒钟就绪，两相对比才定位到
是管道积压不是真的启动慢。

**修法已上移到父进程一侧（2026-09-24）**：现在由
`core/subprocess_service.py::SubprocessServiceHandle` 在 `Popen` 时就把这个
壳进程的 fd 1/2 直接指向一个**真实日志文件**（插件在 DATA_ROOT 下的数据
目录），对齐 LEGACY obsidian-rag 自己做这件事的方式
（obsidian-rag/gpu_arbiter.py:540-545 的
`Popen(..., stdout=logf, stderr=logf, ...)`）——文件不会像管道那样被写满
阻塞，而且内服务子进程继承到的也是这个文件。这样做比"在壳进程内部
`os.dup2` 换 fd"更靠前一层，好处有三：①父进程一启动就决定了落点，日志
路径对用户/AI 可查（父进程知道 DATA_ROOT，子进程不知道）；②不必在子进程
里重复实现一遍 fd 重定向，也就不存在"重定向失败把子进程搞死"这种风险；
③不再需要在插件源码目录下 mkdir 一个 data/ 目录写日志（那既违反"数据落
在 data/ 目录"，又会让日志跟着便携包一起分发/删除）。

本文件因此不再自己做 stdio 重定向——`__main__` 只打印一行启动摘要，
它会和内服务的日志一起落到父进程指定的日志文件里。
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

MINERU_MIN_VRAM_GB = 4.5  # pipeline 后端约 4GB + 0.5 余量，对齐旧项目 gpu_arbiter.py 同名常量
MINERU_VRAM_WAIT_SECONDS = 300.0
MINERU_UNLOAD_AFTER_SECONDS = 300  # 空闲卸载内服务（保留壳进程释放显存），0=不自动卸载
MINERU_IDLE_EXIT_SECONDS = 1800  # 内服务已停后再空闲这么久，壳进程自退出，0=常驻不退出
MINERU_MAX_PAGES = 200  # 单文件页数上限，超限直接拒收提示人工拆分——串行锁下大文件会卡死整轮
# ---- 合批（2026-09-29 操作者确认“尝试，但务必做好显存管理”）----------------------------
# 实测（本机 8GB 卡、pipeline 后端、8 份小扫描件共 60 页）：一份一份送 68.8 秒，合成一批
# 33.5～40.5 秒；显卡平均占用 17% → 25～29%；整卡显存峰值 4.70GB → 4.77～4.97GB。
# 显存为什么只多一点：MinerU 每次交给显卡的量（batch ratio）按显卡**总**显存定档，与一次送
# 几份无关；多份文件的页面按“处理窗口”（MINERU_PROCESSING_WINDOW_SIZE，默认 64 页）凑批。
# 这里把每批上限卡在一个窗口以内，一批占用的内存就与今天单送一份 64 页文件相同。
MINERU_BATCH_MAX_FILES = 8  # 一批最多几份（--batch-files；1 = 不合批，回到一份一份送）
MINERU_BATCH_MAX_PAGES = 64  # 一批最多几页（--batch-pages）：不超过 MinerU 的一个处理窗口
MINERU_BATCH_MIN_FREE_VRAM_GB = 1.0  # 模型装好后显卡至少还空着这么多，才合批（实测合批比单份多用 0.1～0.3GB）
_batch_off_reason: "str | None" = None  # 合批时出过显存不足：本进程余下时间只一份一份解

_api_server = None  # mineru.cli.api_client.ReusableLocalAPIServer 实例，懒建（见 _get_api_server）
_API_LOCK = threading.Lock()  # 串行锁：一次只解一份，防止显存叠加
_INNER_LOCK = threading.Lock()  # 内服务启停锁
_inner_base_url: "str | None" = None
_last_use = time.time()
_active_requests = 0  # 在途请求数：空闲自退出/自动卸载的安全判据，不能在解析中途把内服务/壳干掉
_vram_cache: tuple[float | None, float] = (None, 0.0)


def _vram_free_gb(max_age: float = 5.0):
    """当前空闲显存（GB），探测失败返回 None（fail-open）——
    core/gpu_arbiter.py::vram_free_gb 的自包含副本，见模块 docstring。

    **torch 优先、nvidia-smi 兜底**（与 official-visual-wemm/server.py 和旧项目
    `obsidian-rag/gpu_arbiter.py::vram_free_gb` 同款）。不要改成 nvidia-smi 优先：
    2026-09-29 本机实测两者相差约 5.2 GiB（RTX 5060 Laptop，总 8151 MiB）——
    torch 报空闲 6.878 GiB，nvidia-smi 报 1.681 GiB。WDDM 笔记本上 nvidia-smi 把大量
    系统内存计入显存占用，读数严重偏低；拿它当唯一判据会让 `_wait_for_vram(4.5)`
    间歇性"等不到显存"，表现为扫描件偶发转写失败。

    代价是壳进程调一次 `torch.cuda.mem_get_info()` 会建起自己的 CUDA 上下文、常驻约
    85MB（本机实测）。这 1% 的占用换来读数准确，值得；而且那 85MB 会被算进"已用"，
    读数因此略偏保守——保守方向对显存闸门是安全的。`_vram_cache` 限定 5s 内复用，
    不必反复建上下文。"""
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
            val = None
    _vram_cache = (val, now)
    return val


_vram_refreshing = threading.Lock()


def _vram_snapshot() -> float:
    """给 /health 用的显存快读（GB）——**只读缓存，绝不在这里触发探测**。

    2026-09-29 真机两轮：①本服务原先在 /health 里现场调 `_vram_free_gb()`，第一次调用
    要在请求线程里 `import torch` 并初始化 CUDA；宿主机 CPU 负载高时超出宿主 10 秒启动
    预算，表现为"启用失败"、扫描件整轮延后。②改成后台线程探测后，/health 快了，但
    **壳进程在启动时就建起自己的 CUDA 上下文并常驻**——它只负责调度调度和显示一个数字，
    却实打实占着显存，把真正要装 5GB 模型的 WEMM 挤到门槛外（真机实测：GUI 与索引
    worker 各起一个本服务，两份上下文加起来约 1.4GB，WEMM 卡在
    「空闲显存 5.5GB < 需求 5.5GB」）。

    现在：**只有真要装模型时（`_wait_for_vram`）才探测**，那时 CUDA 上下文本来就必须有。
    探测走 torch（读数准，见 `_vram_free_gb` 的 docstring）。还没探测过就回 0.0——
    与真探测失败时的 fail-open 口径一致，也对齐旧项目 `obsidian-rag/mineru_server.py`
    的 /health「多线程应答」而不做重活。"""
    value, _stamp = _vram_cache
    return float(value or 0.0)


def _wait_for_vram(min_free_gb: float, timeout_s: float = MINERU_VRAM_WAIT_SECONDS, poll_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while True:
        free = _vram_free_gb(max_age=0.0)
        if free is None or free >= min_free_gb:
            return True
        if time.time() >= deadline:
            return False
        print(f"[mineru-local] 空闲显存 {free:.1f}GB < 需求 {min_free_gb:.1f}GB，等待其他模型让路...", file=sys.stderr)
        time.sleep(poll_s)


def _get_api_server():
    global _api_server
    if _api_server is None:
        from mineru.cli.api_client import ReusableLocalAPIServer

        _api_server = ReusableLocalAPIServer()
    return _api_server


def _inner_alive() -> bool:
    """内 mineru-api 是否在跑（快照读，不阻塞）。"""
    if _api_server is None:
        return False
    srv = _api_server._server
    if srv is None or srv.base_url is None:
        return False
    try:
        from mineru.cli.api_client import _managed_process_is_running

        return bool(_managed_process_is_running(srv.process))
    except Exception:
        return False


def _ensure_inner() -> str:
    """确保内服务在跑：已跑直接返回 base_url；否则等显存→拉起→等就绪。

    显存互斥：pipeline 约 4GB，与 WEMM/bge-m3 错峰——等不到就抛错本条请求，
    绝不硬上（8GB 卡硬共存会导致显存溢出整机卡死）。
    """
    global _inner_base_url, _last_use
    with _INNER_LOCK:
        if _inner_alive() and _inner_base_url:
            _last_use = time.time()
            return _inner_base_url
        if not _wait_for_vram(MINERU_MIN_VRAM_GB):
            raise RuntimeError(f"等待空闲显存 >= {MINERU_MIN_VRAM_GB}GB 超时（{MINERU_VRAM_WAIT_SECONDS:.0f}s），本条请求未执行")
        t0 = time.time()
        server = _get_api_server()
        inner, _started = server.ensure_started()
        base_url = inner.base_url
        if not base_url:
            raise RuntimeError("内 mineru-api 拉起后无 base_url")
        deadline = time.time() + 300.0  # 冷起要 import + warming，给足 300s
        last_err = None
        while time.time() < deadline:
            try:
                req = urllib.request.Request(base_url.rstrip("/") + "/health")
                with urllib.request.urlopen(req, timeout=5) as r:
                    payload = json.loads(r.read().decode("utf-8"))
                if payload.get("ok", True):
                    _inner_base_url = base_url
                    _last_use = time.time()
                    print(f"[mineru-local] inner mineru-api ready in {time.time()-t0:.1f}s -> {base_url}", file=sys.stderr)
                    return base_url
            except Exception as e:
                last_err = e
            try:
                from mineru.cli.api_client import _managed_process_exit_code

                if _managed_process_exit_code(inner.process) is not None:
                    raise RuntimeError("内 mineru-api 进程启动后退出（依赖/端口问题）")
            except RuntimeError:
                raise
            except Exception:
                pass
            time.sleep(2.0)
        raise RuntimeError(f"内 mineru-api 300s 未就绪（{type(last_err).__name__ if last_err else '无响应'}）")


def _stop_inner_locked() -> None:
    """停内服务释显存。调用方须已持有 _INNER_LOCK。幂等：没有内服务在跑
    时也该回"已停"——/evict 空载时也该回 ok，不是错误。"""
    global _inner_base_url
    if _api_server is not None:
        try:
            _api_server.stop()
        except Exception as e:
            print(f"[mineru-local] stop inner failed: {type(e).__name__}", file=sys.stderr)
    _inner_base_url = None
    print("[mineru-local] inner stopped; gpu_mem released", file=sys.stderr)


def _check_idle_unload() -> None:
    if MINERU_UNLOAD_AFTER_SECONDS <= 0 or _active_requests > 0:
        return
    if time.time() - _last_use > MINERU_UNLOAD_AFTER_SECONDS:
        with _INNER_LOCK:
            if time.time() - _last_use > MINERU_UNLOAD_AFTER_SECONDS and _inner_alive():
                _stop_inner_locked()
                print("[mineru-local] idle timeout -> GPU released", file=sys.stderr)


def _idle_unload_daemon() -> None:
    interval = min(30, max(1, MINERU_UNLOAD_AFTER_SECONDS))
    while True:
        time.sleep(interval)
        try:
            _check_idle_unload()
        except Exception as exc:
            print(f"[mineru-local] idle check error: {type(exc).__name__}: {exc}", file=sys.stderr)


def _idle_exit_daemon() -> None:
    interval = min(30, max(1, MINERU_IDLE_EXIT_SECONDS))
    while True:
        time.sleep(interval)
        if MINERU_IDLE_EXIT_SECONDS <= 0 or _inner_alive() or _active_requests > 0:
            continue
        if time.time() - _last_use > MINERU_IDLE_EXIT_SECONDS:
            print(f"[mineru-local] idle {MINERU_IDLE_EXIT_SECONDS}s after unload -> process exit", file=sys.stderr)
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
    print("[mineru-local] 启动本服务的宿主进程已经不在 -> 停内服务并退出（释放显存）", file=sys.stderr)
    try:
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - 刷盘失败绝不能挡住退出
        pass

    def _stop_inner() -> None:
        try:
            if _api_server is not None:
                _api_server.stop()
        except Exception:  # noqa: BLE001
            pass

    # 内服务（mineru-api，真正占显存的那个）是本进程的子进程：直接 os._exit 会把它留成孤儿继续
    # 占显存。先请它停（最多等 10 秒，不能拿 _INNER_LOCK——宿主没了时可能正卡在冷启动持锁），
    # 再按进程树强杀（本进程也在树里，一并结束）。
    stopper = threading.Thread(target=_stop_inner, daemon=True, name="mineru-stop-inner")
    stopper.start()
    stopper.join(timeout=10.0)
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                capture_output=True,
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            import signal

            os.killpg(os.getpgid(0), signal.SIGTERM)
    except Exception:  # noqa: BLE001 - 兜底之后还有 os._exit
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


def _count_pages(pdf_path) -> "int | None":
    """快数页数（只读元信息，不渲染）。失败返回 None（由调用方按未知处理）。"""
    try:
        import pymupdf

        doc = pymupdf.open(pdf_path)
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except Exception:
        return None


def _mineru_local_timeout(pages) -> float:
    """单文件超时（秒）= 300 + 30×页数。诚实声明：pipeline GPU 尚无基准
    数据，这是防卡死的 backstop（200 页→约105min），不是性能承诺——对齐
    旧项目 obsidian-rag/extractors.py 同名函数。"""
    try:
        n = int(pages or 0)
    except (TypeError, ValueError):
        n = 0
    return 300.0 + 30.0 * max(0, n)


_PARSE_FIELDS = {
    "backend": "pipeline",
    "parse_method": "auto",
    "lang_list": "ch",
    "formula_enable": "true",
    "table_enable": "true",
    "return_md": "true",
    "response_format_zip": "false",
    "return_middle_json": "false",
    "return_model_output": "false",
    "return_content_list": "false",
    "return_images": "false",
}


def _multipart_body(pdf_paths, fields, upload_names=None):
    """手拼 multipart/form-data（只用标准库，不耦合 httpx 版本）。一次可带多份 PDF：
    MinerU 的 /file_parse 按上传文件名（去扩展名）分别返回结果，所以合批时用
    `upload_names` 给每份起不重名的名字（doc0.pdf、doc1.pdf……），免得两份同名文件的
    结果互相覆盖。"""
    if isinstance(pdf_paths, (str, os.PathLike)):
        pdf_paths = [pdf_paths]
    names = list(upload_names) if upload_names else [os.path.basename(p) for p in pdf_paths]
    boundary = "----mineruLocal%s" % int(time.time() * 1000)
    parts = []
    for k, v in fields.items():
        parts.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8")
        )
    for pdf_path, fname in zip(pdf_paths, names):
        with open(pdf_path, "rb") as f:
            data = f.read()
        parts.append(
            (
                "--%s\r\nContent-Disposition: form-data; name=\"files\"; "
                "filename=\"%s\"\r\nContent-Type: application/pdf\r\n\r\n" % (boundary, fname)
            ).encode("utf-8")
        )
        parts.append(data)
        parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), boundary


def _extract_md(payload) -> "str | None":
    """从 /file_parse(JSON) 响应里抠正文。实测形态（fast_api.build_result_dict）：
    按文件名 keyed 的 {pdf_name: {md_content}}；兼容裸 dict / 单元素 list /
    results 列表三种历史形态——对齐旧项目 mineru_server.py 同名函数。"""
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        return None
    md = payload.get("md_content")
    if isinstance(md, str) and md.strip():
        return md
    for v in payload.values():
        if isinstance(v, dict):
            md = v.get("md_content")
            if isinstance(md, str) and md.strip():
                return md
    results = payload.get("results")
    if isinstance(results, dict):
        for v in results.values():
            if isinstance(v, dict):
                md = v.get("md_content")
                if isinstance(md, str) and md.strip():
                    return md
    if isinstance(results, list) and results and isinstance(results[0], dict):
        md = results[0].get("md_content")
        if isinstance(md, str) and md.strip():
            return md
    return None


def _post_file_parse(pdf_paths, timeout_s, upload_names=None) -> "tuple[dict | None, str | None]":
    """把一份或几份 PDF 交给内服务 /file_parse，返回（响应 JSON，短码错误）。"""
    base_url = _ensure_inner()
    body, boundary = _multipart_body(pdf_paths, _PARSE_FIELDS, upload_names)
    req = urllib.request.Request(
        base_url.rstrip("/") + "/file_parse",
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            status = r.status
            raw = r.read()
    except urllib.error.HTTPError as e:
        # 4xx/5xx 在 urllib 里是异常：把内服务回的错误摘要带出来（显存不足要靠它认出来）
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        if e.code == 409:
            return None, "parse-failed: 内服务解析失败（文件损坏或版面异常）"
        return None, f"inner-error: HTTP {e.code} {detail}".rstrip()
    except Exception as e:
        return None, f"inner-error: {type(e).__name__}（内服务调用失败）"
    if status != 200:
        return None, f"inner-error: HTTP {status}"
    try:
        return json.loads(raw.decode("utf-8")), None
    except Exception:
        return None, "inner-error: 内服务返回非 JSON"


def _do_parse(pdf_path, timeout_s) -> "tuple[str | None, str | None]":
    """解析一份 PDF → (md|None, 短码错误|None)。调用方须已持有 _API_LOCK
    （串行锁，8GB卡一次只解一个请求，防显存叠加）。"""
    global _last_use
    t0 = time.time()
    payload, err = _post_file_parse(pdf_path, timeout_s)
    if err is not None:
        return None, err
    md = _extract_md(payload)
    if not md:
        return None, "empty-result: 内服务成功但无正文（图片页无字或全空页）"
    secs = round(time.time() - t0, 1)
    print(f"[mineru-local] 解析成功：{Path(pdf_path).name}（{secs}s）", file=sys.stderr)
    _last_use = time.time()
    return md, None


def _do_parse_many(pdf_paths, timeout_s) -> "tuple[list | None, str | None]":
    """几份 PDF 合成一个请求 → (每份的 md 或 None, 整批的短码错误)。调用方须已持有 _API_LOCK。
    某一份没出正文只把那一份记成 None（由调用方单独再解一次），不连累同批其他份。"""
    global _last_use
    t0 = time.time()
    names = ["doc%d.pdf" % i for i in range(len(pdf_paths))]
    payload, err = _post_file_parse(pdf_paths, timeout_s, names)
    if err is not None:
        return None, err
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, dict):
        return None, "inner-error: 合批响应里没有 results"
    mds = []
    for name in names:
        entry = results.get(name[: -len(".pdf")])
        md = entry.get("md_content") if isinstance(entry, dict) else None
        mds.append(md if isinstance(md, str) and md.strip() else None)
    secs = round(time.time() - t0, 1)
    print(
        f"[mineru-local] 合批解析完成：{len(pdf_paths)} 份，出正文 {sum(1 for m in mds if m)} 份（{secs}s）："
        + "、".join(Path(p).name for p in pdf_paths),
        file=sys.stderr,
    )
    _last_use = time.time()
    return mds, None


def _plan_batches(page_counts, *, max_files: int, max_pages: int) -> "list[list[int]]":
    """按原顺序把文件分组（返回下标）：每组不超过 `max_files` 份、页数合计不超过 `max_pages`。
    页数未知或单份就超过上限的文件单独一组（和今天一样一份一份送）。"""
    groups: list[list[int]] = []
    current: list[int] = []
    current_pages = 0
    for index, pages in enumerate(page_counts):
        if max_files <= 1 or pages is None or pages > max_pages:
            if current:
                groups.append(current)
                current, current_pages = [], 0
            groups.append([index])
            continue
        if current and (len(current) >= max_files or current_pages + pages > max_pages):
            groups.append(current)
            current, current_pages = [], 0
        current.append(index)
        current_pages += pages
    if current:
        groups.append(current)
    return groups


def _batch_allowed() -> bool:
    """这一批能不能合着送。调用方须已持有 _API_LOCK；这里先确保内服务（模型）已经装好，
    这样看到的是“模型装好之后还剩多少显存”，不是装之前的虚高值。"""
    if MINERU_BATCH_MAX_FILES <= 1 or _batch_off_reason:
        return False
    _ensure_inner()
    free = _vram_free_gb(max_age=0.0)
    if free is not None and free < MINERU_BATCH_MIN_FREE_VRAM_GB:
        print(
            f"[mineru-local] 空闲显存 {free:.1f}GB < {MINERU_BATCH_MIN_FREE_VRAM_GB:.1f}GB，这一批改为一份一份解",
            file=sys.stderr,
        )
        return False
    return True


def _note_batch_failure(err: str) -> None:
    """合批失败都会退回一份一份解；如果是显存不足，本进程余下时间干脆不再合批。"""
    global _batch_off_reason
    print(f"[mineru-local] 合批失败，退回一份一份解：{err}", file=sys.stderr)
    if "out of memory" in err.lower():
        _batch_off_reason = err
        print("[mineru-local] 合批时显存不足 -> 本服务余下时间只一份一份解", file=sys.stderr)


def _real_ocr(full_path: Path) -> str:
    pages = _count_pages(full_path)
    if pages is not None and pages > MINERU_MAX_PAGES:
        raise RuntimeError(f"too-many-pages: {pages} 页超过上限 {MINERU_MAX_PAGES} 页，请人工拆分后重建（串行锁下大文件会卡死整轮）")
    if pages is not None and pages <= 0:
        raise RuntimeError("empty-pdf: 无有效页面")
    timeout = _mineru_local_timeout(pages)
    global _active_requests
    _active_requests += 1
    try:
        with _API_LOCK:
            md, err = _do_parse(full_path, timeout)
    finally:
        _active_requests -= 1
    if md is None:
        raise RuntimeError(err or "unknown")
    return md


def _real_ocr_many(full_paths: "list[Path]") -> "list[tuple[str | None, str | None]]":
    """几份 PDF 一起解 → 每份（md 或 None，失败原因或 None），顺序与输入一致。

    显存管理（操作者 2026-09-29 的硬要求）：①每批不超过 MINERU_BATCH_MAX_FILES 份、
    MINERU_BATCH_MAX_PAGES 页（一个 MinerU 处理窗口）；②仍然一次只跑一个请求（_API_LOCK），
    模型装载前照旧等空闲显存 ≥ MINERU_MIN_VRAM_GB；③模型装好后空闲显存不足
    MINERU_BATCH_MIN_FREE_VRAM_GB 就不合批；④合批出任何错都把这一批退回一份一份解，
    是显存不足则本进程余下时间不再合批；⑤合批里某一份没出正文，只把那一份单独再解一次。"""
    global _active_requests
    outcomes: "list[tuple[str | None, str | None] | None]" = [None] * len(full_paths)
    page_counts: "list[int | None]" = []
    for index, full_path in enumerate(full_paths):
        pages = _count_pages(full_path)
        page_counts.append(pages)
        if pages is not None and pages > MINERU_MAX_PAGES:
            outcomes[index] = (
                None,
                f"too-many-pages: {pages} 页超过上限 {MINERU_MAX_PAGES} 页，请人工拆分后重建（串行锁下大文件会卡死整轮）",
            )
        elif pages is not None and pages <= 0:
            outcomes[index] = (None, "empty-pdf: 无有效页面")
    todo = [index for index in range(len(full_paths)) if outcomes[index] is None]
    groups = _plan_batches(
        [page_counts[index] for index in todo],
        max_files=MINERU_BATCH_MAX_FILES,
        max_pages=MINERU_BATCH_MAX_PAGES,
    )
    _active_requests += 1
    try:
        for group in groups:
            members = [todo[position] for position in group]
            if len(members) > 1:
                try:
                    with _API_LOCK:
                        if _batch_allowed():
                            timeout = sum(_mineru_local_timeout(page_counts[index]) for index in members)
                            mds, err = _do_parse_many([full_paths[index] for index in members], timeout)
                            if err is not None:
                                _note_batch_failure(err)
                            else:
                                for index, md in zip(members, mds):
                                    if md:
                                        outcomes[index] = (md, None)
                except Exception as exc:  # noqa: BLE001 - 合批这一步出任何错都退回单份，不连累整批
                    _note_batch_failure(f"{type(exc).__name__}: {exc}")
            for index in members:
                if outcomes[index] is not None:
                    continue
                try:
                    with _API_LOCK:
                        outcomes[index] = _do_parse(full_paths[index], _mineru_local_timeout(page_counts[index]))
                except Exception as exc:  # noqa: BLE001 - 单份失败折叠成这一份的原因，别的份照常
                    outcomes[index] = (None, str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}")
    finally:
        _active_requests -= 1
    return [outcome if outcome is not None else (None, "unknown") for outcome in outcomes]


def _fake_ocr(full_path: Path) -> str:
    return f"[fake-ocr] {full_path.name} 的识别结果（仅测试/架构验证用，不是真实OCR文字）"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True, "inner_loaded": _inner_alive(), "gpu_mem_gb": round(_vram_snapshot(), 2)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/evict":
            # 双向抢占：bge/WEMM 加载前调这里把 MinerU 请下显存。正在解析时
            # 会等 _API_LOCK 才停（不中断在途成果）；空载时也回 ok（幂等）。
            with _API_LOCK:
                with _INNER_LOCK:
                    had = _inner_alive()
                    if had:
                        _stop_inner_locked()
            self._json(200, {"ok": True, "evicted": had})
            return
        if self.path == "/extract_many":
            self._extract_many()
            return
        if self.path != "/extract":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        full_path = Path(payload.get("root", "")) / payload.get("path", "")
        try:
            if not full_path.exists():
                raise FileNotFoundError(str(full_path))
            if os.environ.get("RAG_REDO_FAKE_OCR"):
                text = _fake_ocr(full_path)
            else:
                text = _real_ocr(full_path)
            self._json(200, {"text": text, "failure_reason": None})
        except Exception as exc:  # noqa: BLE001 - 子进程这一侧也不能让异常直接炸掉HTTP响应
            self._json(200, {"text": None, "failure_reason": f"{type(exc).__name__}: {exc}"})

    def _extract_many(self) -> None:
        """POST /extract_many {root, paths:[...]} → {results:[{text, failure_reason}, ...]}，顺序同
        `paths`。每一份的失败形状与 /extract 相同（failure_reason 以 "RuntimeError: " 开头）。"""
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        root = Path(payload.get("root", ""))
        rel_paths = [str(p) for p in (payload.get("paths") or [])]
        results: "list[dict | None]" = [None] * len(rel_paths)
        todo: "list[int]" = []
        for index, rel in enumerate(rel_paths):
            full_path = root / rel
            if full_path.exists():
                todo.append(index)
            else:
                results[index] = {"text": None, "failure_reason": f"FileNotFoundError: {full_path}"}
        try:
            if os.environ.get("RAG_REDO_FAKE_OCR"):
                outcomes = [(_fake_ocr(root / rel_paths[index]), None) for index in todo]
            else:
                outcomes = _real_ocr_many([root / rel_paths[index] for index in todo])
            for index, (text, err) in zip(todo, outcomes):
                results[index] = (
                    {"text": text, "failure_reason": None}
                    if text is not None
                    else {"text": None, "failure_reason": f"RuntimeError: {err or 'unknown'}"}
                )
        except Exception as exc:  # noqa: BLE001 - 子进程这一侧也不能让异常直接炸掉HTTP响应
            for index in todo:
                if results[index] is None:
                    results[index] = {"text": None, "failure_reason": f"{type(exc).__name__}: {exc}"}
        self._json(200, {"results": results})

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass  # 静默，避免污染核心进程的 stdout/stderr


def make_server(port: int) -> http.server.ThreadingHTTPServer:
    """多线程 HTTP 服务（旧项目 mineru_server.py 用的就是 ThreadingHTTPServer）。

    单线程的 `HTTPServer` 一次只应答一个请求：/extract 解析一份 PDF 要几分钟，这期间
    任何进程来问 /health（宿主 `is_alive` 探测、另一个进程启用本插件时的启动检查）都会
    排队到超时。线程是 daemon（ThreadingHTTPServer 默认），进程退出时不会被它们拖住。"""
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)


def _int_arg(name: str, default: int) -> int:
    if name in sys.argv:
        return int(sys.argv[sys.argv.index(name) + 1])
    return default


def _float_arg(name: str, default: float) -> float:
    if name in sys.argv:
        return float(sys.argv[sys.argv.index(name) + 1])
    return default


if __name__ == "__main__":
    # 注意这里**没有** stdio 重定向：父进程（core/subprocess_service.py::
    # SubprocessServiceHandle）在 Popen 时已经把本进程的 fd 1/2 指向了
    # DATA_ROOT 下的真实日志文件，所以下面这行启动摘要和内服务
    # （mineru-api）的日志都会落到那里。详见模块 docstring。
    MINERU_UNLOAD_AFTER_SECONDS = _int_arg("--unload-after", MINERU_UNLOAD_AFTER_SECONDS)
    MINERU_IDLE_EXIT_SECONDS = _int_arg("--idle-exit", MINERU_IDLE_EXIT_SECONDS)
    MINERU_MIN_VRAM_GB = _float_arg("--min-vram", MINERU_MIN_VRAM_GB)
    MINERU_VRAM_WAIT_SECONDS = _float_arg("--vram-wait", MINERU_VRAM_WAIT_SECONDS)
    MINERU_MAX_PAGES = _int_arg("--max-pages", MINERU_MAX_PAGES)
    MINERU_BATCH_MAX_FILES = _int_arg("--batch-files", MINERU_BATCH_MAX_FILES)
    MINERU_BATCH_MAX_PAGES = _int_arg("--batch-pages", MINERU_BATCH_MAX_PAGES)
    if MINERU_UNLOAD_AFTER_SECONDS > 0:
        threading.Thread(target=_idle_unload_daemon, daemon=True, name="mineru-idle-unload").start()
    if MINERU_IDLE_EXIT_SECONDS > 0:
        threading.Thread(target=_idle_exit_daemon, daemon=True, name="mineru-idle-exit").start()
    threading.Thread(target=_parent_watch_daemon, daemon=True, name="mineru-parent-watch").start()
    port = int(sys.argv[sys.argv.index("--port") + 1])
    print(
        f"[mineru-local] server listening on http://127.0.0.1:{port} "
        f"(lazy inner mineru-api, unload_after={MINERU_UNLOAD_AFTER_SECONDS}s, "
        f"idle_exit={MINERU_IDLE_EXIT_SECONDS}s, min_vram={MINERU_MIN_VRAM_GB}GB, "
        f"max_pages={MINERU_MAX_PAGES}, batch={MINERU_BATCH_MAX_FILES} files/{MINERU_BATCH_MAX_PAGES} pages)",
        file=sys.stderr,
    )
    make_server(port).serve_forever()
