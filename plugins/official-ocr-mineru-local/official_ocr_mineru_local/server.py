#!/usr/bin/env python3
"""official-ocr-mineru-local 的子进程服务端——单独进程运行。

真正解析走 MinerU 官方 `mineru.cli.api_client.ReusableLocalAPIServer`（和
`mineru-api` 命令背后是同一套东西）——本服务只是一层"壳"：绑端口、做单次
串行调度（8GB卡一次只解一份，叠加会爆显存）、管这个内服务自己的懒加载/
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
——根因见 `_redirect_stdio_to_logfile` 的 docstring：MinerU 官方
`ReusableLocalAPIServer` 启动内服务子进程时没有显式指定 `stdout`/`stderr`
（见 mineru/cli/api_client.py），默认继承调用方（也就是这个壳进程）的
文件描述符；而这个壳进程本身是被
`core/subprocess_service.py::SubprocessServiceHandle` 用
`stdout=PIPE, stderr=PIPE` 启动的——那两个管道只在崩溃诊断时才读一次，
平时没人持续排空。内服务（uvicorn+loguru）启动时的日志量一旦把 Windows
管道的默认缓冲区（64KB）写满，`write()` 系统调用就会阻塞，内服务从此
卡在"进程活着、端口没监听完、/health 永远连不上"——实测跑满了 300s
就绪超时，直接构造脚本单独跑（不经过这层被吞的管道）反而几秒钟就绪，
两相对比才定位到是管道积压不是真的启动慢。修法：这个壳进程一启动就把
自己的 fd 1/2 换成一个真实日志文件（不是 Python 层面的
`sys.stdout`/`sys.stderr` 对象重新赋值，那不影响 OS 层面的 fd，内服务
的 `subprocess.Popen` 继承的是 fd 不是 Python 对象——必须用
`os.dup2` 才能让继承生效），文件不会像管道那样被写满阻塞，对齐
obsidian-rag 自己把 `mineru_server.py` 启动时 `stdout=logf, stderr=logf`
指向真实文件（而不是留给调用方管道）的既有做法。
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

_api_server = None  # mineru.cli.api_client.ReusableLocalAPIServer 实例，懒建（见 _get_api_server）
_API_LOCK = threading.Lock()  # 串行锁：一次只解一份，防止显存叠加
_INNER_LOCK = threading.Lock()  # 内服务启停锁
_inner_base_url: "str | None" = None
_last_use = time.time()
_active_requests = 0  # 在途请求数：空闲自退出/自动卸载的安全判据，不能在解析中途把内服务/壳干掉
_vram_cache: tuple[float | None, float] = (None, 0.0)


def _vram_free_gb(max_age: float = 5.0):
    """当前空闲显存（GB），探测失败返回 None（fail-open）——
    core/gpu_arbiter.py::vram_free_gb 的自包含副本，见模块 docstring。"""
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


def _multipart_body(pdf_path, fields):
    """手拼 multipart/form-data（只用标准库，不耦合 httpx 版本）。"""
    boundary = "----mineruLocal%s" % int(time.time() * 1000)
    fname = os.path.basename(pdf_path)
    with open(pdf_path, "rb") as f:
        data = f.read()
    parts = []
    for k, v in fields.items():
        parts.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8")
        )
    parts.append(
        (
            "--%s\r\nContent-Disposition: form-data; name=\"files\"; "
            "filename=\"%s\"\r\nContent-Type: application/pdf\r\n\r\n" % (boundary, fname)
        ).encode("utf-8")
    )
    parts.append(data)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
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


def _do_parse(pdf_path, timeout_s) -> "tuple[str | None, str | None]":
    """解析一份 PDF → (md|None, 短码错误|None)。调用方须已持有 _API_LOCK
    （串行锁，8GB卡一次只解一份，防显存叠加）。"""
    global _last_use
    t0 = time.time()
    base_url = _ensure_inner()
    fields = {
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
    body, boundary = _multipart_body(pdf_path, fields)
    req = urllib.request.Request(
        base_url.rstrip("/") + "/file_parse",
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            status = r.status
            raw = r.read()
    except Exception as e:
        return None, f"inner-error: {type(e).__name__}（内服务调用失败）"
    if status == 409:
        return None, "parse-failed: 内服务解析失败（文件损坏或版面异常）"
    if status != 200:
        return None, f"inner-error: HTTP {status}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "inner-error: 内服务返回非 JSON"
    md = _extract_md(payload)
    if not md:
        return None, "empty-result: 内服务成功但无正文（图片页无字或全空页）"
    secs = round(time.time() - t0, 1)
    print(f"[mineru-local] 解析成功：{Path(pdf_path).name}（{secs}s）", file=sys.stderr)
    _last_use = time.time()
    return md, None


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


def _fake_ocr(full_path: Path) -> str:
    return f"[fake-ocr] {full_path.name} 的识别结果（仅测试/架构验证用，不是真实OCR文字）"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True, "inner_loaded": _inner_alive(), "gpu_mem_gb": round(_vram_free_gb() or 0.0, 2)})
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

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass  # 静默，避免污染核心进程的 stdout/stderr


def _redirect_stdio_to_logfile() -> None:
    """把这个进程自己的 fd 1/2 换成一个真实日志文件——见模块 docstring
    "真机调试时抓到的严重坑"。必须用 `os.dup2` 操作 OS 层面的文件描述符，
    不能只重新赋值 `sys.stdout`/`sys.stderr`（那只影响 Python 自己
    print() 时用哪个对象，不影响子进程 `subprocess.Popen` 默认继承的
    OS fd——MinerU 内服务子进程继承的正是后者）。日志文件落在这个插件
    自己的 `data/` 目录下（架构红线7"数据落在插件/项目自己的目录"），
    用追加模式，方便跨次启动留痕排查。"""
    log_dir = Path(__file__).parent / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "mineru_local_server.log"
    log_file = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    os.dup2(log_file.fileno(), sys.stdout.fileno())
    os.dup2(log_file.fileno(), sys.stderr.fileno())
    sys.stdout = log_file
    sys.stderr = log_file
    print(f"\n[mineru-local] ===== 新一轮启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====", file=sys.stderr)


def _int_arg(name: str, default: int) -> int:
    if name in sys.argv:
        return int(sys.argv[sys.argv.index(name) + 1])
    return default


def _float_arg(name: str, default: float) -> float:
    if name in sys.argv:
        return float(sys.argv[sys.argv.index(name) + 1])
    return default


if __name__ == "__main__":
    _redirect_stdio_to_logfile()
    MINERU_UNLOAD_AFTER_SECONDS = _int_arg("--unload-after", MINERU_UNLOAD_AFTER_SECONDS)
    MINERU_IDLE_EXIT_SECONDS = _int_arg("--idle-exit", MINERU_IDLE_EXIT_SECONDS)
    MINERU_MIN_VRAM_GB = _float_arg("--min-vram", MINERU_MIN_VRAM_GB)
    MINERU_VRAM_WAIT_SECONDS = _float_arg("--vram-wait", MINERU_VRAM_WAIT_SECONDS)
    MINERU_MAX_PAGES = _int_arg("--max-pages", MINERU_MAX_PAGES)
    if MINERU_UNLOAD_AFTER_SECONDS > 0:
        threading.Thread(target=_idle_unload_daemon, daemon=True, name="mineru-idle-unload").start()
    if MINERU_IDLE_EXIT_SECONDS > 0:
        threading.Thread(target=_idle_exit_daemon, daemon=True, name="mineru-idle-exit").start()
    port = int(sys.argv[sys.argv.index("--port") + 1])
    print(
        f"[mineru-local] server listening on http://127.0.0.1:{port} "
        f"(lazy inner mineru-api, unload_after={MINERU_UNLOAD_AFTER_SECONDS}s, "
        f"idle_exit={MINERU_IDLE_EXIT_SECONDS}s, min_vram={MINERU_MIN_VRAM_GB}GB, "
        f"max_pages={MINERU_MAX_PAGES})",
        file=sys.stderr,
    )
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
