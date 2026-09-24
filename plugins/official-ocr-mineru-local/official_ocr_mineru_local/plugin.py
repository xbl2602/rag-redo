"""official-ocr-mineru-local 插件：真正的重依赖/模型跑在独立子进程里
（server.py），这个类本身很薄，只负责四件事：
①向 GPU 资源仲裁器申请一个名额（这是"名额"层面的协商，不是真实GPU
硬件探测——探测发生在真正调用真实模型那一刻，见
core/resource_arbiter.py 模块docstring"探测失败fail-open"）；
②解析出能跑子进程的 MinerU 解释器路径（见 `_resolve_mineru_python`）；
③用 core.subprocess_service 启动/终止自己声明的子进程；
④把 extract() 转发成对子进程的本机HTTP调用。

**MinerU 解释器是"检测复用外部工具环境"，不是 env_bootstrap 建独立venv
（2026-09-23 真机接入真实模型时的架构决策，按 obsidian-rag/gpu_arbiter.py
`_resolve_mineru_python` 真实行为照做）**：MinerU 官方发行的是一个独立
命令行工具（`uv tool install --python 3.12 -U "mineru[all]"`），装的时候
自带一整套 torch/transformers 环境，用户很可能已经在别处（比如旧项目
obsidian-rag，或者单纯早前就自己装过）装过一份——这里的策略是"探测已有
安装、直接复用"，不是像 official-visual-wemm 那样用 env_bootstrap 每个
插件建一份隔离 venv 重新 pip install 一遍。原因：MinerU 的模型权重+
torch 依赖体积以GB计，重复安装既浪费磁盘又会触发没必要的重新下载
（用户明确反馈过"已经有本地MinerU了，不要重复下载"）；而 uv tool 是
MinerU 官方文档推荐的标准安装方式，落点路径是可预测的既定规范，值得
专门探测，不需要每个插件都发明一套"帮用户装环境"的逻辑。

**这个参考实现此前刻意不下载真实OCR模型**（虚拟机磁盘空间有限阶段的
过渡状态，2026-09-23 已因用户本机已具备完整 MinerU 环境而结束）：
server.py 里真正调用模型的分支仍然懒导入，找不到依赖时折叠成清楚的
失败原因；`RAG_REDO_FAKE_OCR=1` 环境变量继续存在，供测试/无GPU机器用
——验证"子进程真的启动了、本机HTTP协议真的通了、chain-try 真的把内容
喂进主链路了"，不需要真机器/真模型（同 official-embedder-bge-m3/
official-reranker"真实依赖懒加载、测试注入假实现"的纪律）。

**GPU 资源仲裁（和 official-visual-wemm 是同一套协议）**：和
official-visual-wemm 处于同一优先级层级——两者都是"按需占用"的
subprocess_service GPU 消费者，谁刚需要谁能把对方挤开（`preempt_equal`，
对应旧项目 WEMM/MinerU 互相抢占显存的真实行为）；`on_preempt` 回调只
请求子进程"软驱逐"（`/evict`），不整个杀掉子进程；`_ensure_alive()` 在
真正调用前按需重新拉起。

**单文件超时随页数缩放（对齐 obsidian-rag/extractors.py::_mineru_local_
timeout）**：300s + 30s×页数（200页封顶约105min，pipeline GPU 尚无基准
数据，这是防卡死的 backstop 不是性能承诺）——页数在核心解释器这一侧
（这里，用 pymupdf，本来就是 official-extractor-pdf-text 的既有依赖）先
数一遍算出这次调用该给多长的HTTP超时，server.py 那一侧对同一份文件会
独立再数一遍页数（用于它自己的页数上限把关），两次数页各司其职，不是
重复劳动——对齐 obsidian-rag caller 端预先算超时、server 端独立把关
上限的既有分工。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from core.contracts import ExtractedDocument
from core.subprocess_service import SubprocessServiceError, SubprocessServiceHandle

EXTRACTOR_VERSION = "0.2.0"
PLUGIN_ID = "official-ocr-mineru-local"
GPU_RESOURCE_ID = "gpu:0"
GPU_PRIORITY = 10  # 和 official-visual-wemm 同一层级，互相抢占（preempt_equal）
_MINERU_INSTALL_HINT = 'uv tool install --python 3.12 -U "mineru[all]"'


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_mineru_python(explicit: str | None = None, settings=None) -> str | None:
    """MinerU tool 环境的 python.exe。测试/无GPU机器（RAG_REDO_FAKE_OCR）
    直接用核心自己的解释器——不需要真装 MinerU，server.py 的假OCR分支
    只用标准库。覆盖优先级：显式参数 > `RAG_REDO_MINERU_PYTHON` 环境变量
    （一次性/CI场景用，不持久化）> `mineru_python` 设置项（2026-09-23接入
    core/settings.py 通用设置存储后补齐，对齐 obsidian-rag/config.py 的
    `mineru_python` 项——GUI/设置面板可持久化改，不用每次都设环境变量）
    > 按 `uv tool install` 的标准落点自动探测（对齐
    obsidian-rag/gpu_arbiter.py `_resolve_mineru_python` 的探测路径），
    都找不到返回 None，调用方负责报出清楚的安装提示，不是静默退化用不
    认识 mineru 包的核心解释器。"""
    if os.environ.get("RAG_REDO_FAKE_OCR"):
        return sys.executable
    override = explicit or os.environ.get("RAG_REDO_MINERU_PYTHON")
    if not override and settings is not None:
        override = settings.get("mineru_python", "") or None
    if override and Path(override).is_file():
        return str(override)
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "uv" / "tools" / "mineru" / "Scripts" / "python.exe")
    home = Path.home()
    candidates.append(home / ".local" / "share" / "uv" / "tools" / "mineru" / "bin" / "python")
    candidates.append(home / ".local" / "share" / "uv" / "tools" / "mineru" / "Scripts" / "python.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _count_pages(full_path: Path) -> "int | None":
    try:
        import pymupdf

        doc = pymupdf.open(full_path)
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except Exception:
        return None


def _mineru_local_timeout(pages) -> float:
    try:
        n = int(pages or 0)
    except (TypeError, ValueError):
        n = 0
    return 300.0 + 30.0 * max(0, n)


class MineruLocalOcrPlugin:
    def __init__(self) -> None:
        self._handle: SubprocessServiceHandle | None = None
        self._logger = None
        self._enabled = False
        self._plugin_dir: Path | None = None
        self._runtime_health_check: str | None = None
        self._runtime_command: tuple[str, ...] | None = None
        self._settings = None
        self._resource_arbiter = None
        self._plugin_id = ""

    def on_load(self, ctx):
        self._logger = ctx.logger
        self._settings = ctx.settings
        ctx.logger.info("MinerU本机OCR已加载")

    def on_enable(self, ctx):
        self._resource_arbiter = ctx.resource_arbiter
        self._plugin_id = ctx.plugin_id
        self._plugin_dir = Path(__file__).parent
        self._runtime_health_check = ctx.runtime.health_check
        self._runtime_command = ctx.runtime.command
        self._enabled = True
        if not self.is_active():
            return
        acquired = ctx.resource_arbiter.acquire(
            GPU_RESOURCE_ID,
            ctx.plugin_id,
            priority=GPU_PRIORITY,
            on_preempt=self._soft_evict,
            preempt_equal=True,
        )
        if acquired:
            self._start_handle()

    def on_disable(self, ctx):
        self._enabled = False
        self._stop_handle()
        ctx.resource_arbiter.release(GPU_RESOURCE_ID, ctx.plugin_id)
        self._resource_arbiter = None
        self._plugin_id = ""

    def on_unload(self, ctx):
        self._enabled = False
        self._stop_handle()
        if self._resource_arbiter is not None and self._plugin_id:
            self._resource_arbiter.release(GPU_RESOURCE_ID, self._plugin_id)
        self._resource_arbiter = None
        self._plugin_id = ""

    def is_active(self) -> bool:
        selected = self._settings.get("pdf_scan_backend", "none") if self._settings is not None else "none"
        return selected == "mineru-local"

    def index_signature(self) -> str:
        selected = self._settings.get("pdf_scan_backend", "none") if self._settings is not None else "none"
        readiness = "ready" if _resolve_mineru_python(settings=self._settings) else "noready"
        return f"selected:{selected}:{readiness}"

    def _start_handle(self) -> None:
        assert self._plugin_dir is not None and self._runtime_command is not None
        python = _resolve_mineru_python(settings=self._settings)
        if python is None:
            raise SubprocessServiceError(
                f"找不到 MinerU tool 环境的 Python（mineru_python 设置项/RAG_REDO_MINERU_PYTHON "
                f"环境变量均未配且自动探测失败）：请先跑 {_MINERU_INSTALL_HINT} 装好本机 MinerU，"
                "或设置 mineru_python 指向已有安装的 python.exe"
            )
        command = tuple(arg.replace("{python}", python) for arg in self._runtime_command)
        self._handle = SubprocessServiceHandle(command, health_check=self._runtime_health_check, cwd=self._plugin_dir)
        self._handle.start()
        self._logger.info("MinerU本机OCR子进程已启动（端口=%d，解释器=%s）", self._handle.port, python)

    def _stop_handle(self) -> None:
        if self._handle is not None:
            self._handle.stop()
            self._handle = None

    def _soft_evict(self) -> None:
        """见 official-visual-wemm/plugin.py 同名方法的说明——只请求软
        驱逐，不整个杀掉子进程；HTTP 失败 fail-open，不阻塞抢占方。"""
        if self._handle is not None and self._handle.is_alive:
            try:
                self._handle.call("evict", {}, timeout=15.0)
            except SubprocessServiceError:
                pass

    def _ensure_alive(self) -> bool:
        if not self._enabled:
            return False
        if self._resource_arbiter is not None and self._resource_arbiter.holder_of(GPU_RESOURCE_ID) != self._plugin_id:
            acquired = self._resource_arbiter.acquire(
                GPU_RESOURCE_ID,
                self._plugin_id,
                priority=GPU_PRIORITY,
                on_preempt=self._soft_evict,
                preempt_equal=True,
            )
            if not acquired:
                return False
        if self._handle is not None and self._handle.is_alive:
            return True
        try:
            self._start_handle()
            return True
        except SubprocessServiceError as exc:
            self._logger.warning("MinerU本机OCR子进程重新拉起失败：%s", exc)
            return False

    def extract(self, library_id: str, path: str, root: Path) -> ExtractedDocument:
        full_path = root / path
        if full_path.suffix.lower() != ".pdf":
            return self._fail(library_id, path, "不是PDF，本机OCR跳过")
        if not self._ensure_alive():
            return self._fail(library_id, path, "deferred")

        try:
            data = full_path.read_bytes()
        except OSError as exc:
            return self._fail(library_id, path, f"读取失败: {type(exc).__name__}: {exc}")
        content_hash = _content_hash(data)

        # 客户端这一侧的HTTP超时要覆盖住 server.py 那一侧的单文件处理预算
        # （300s+30s×页数）再加60s网络余量，否则大文件会在还没解析完时就被
        # 这一跳掐断——对齐 obsidian-rag/extractors.py 的 caller 端算超时。
        timeout = _mineru_local_timeout(_count_pages(full_path)) + 60.0
        try:
            result = self._handle.call("extract", {"path": path, "root": str(root)}, timeout=timeout)
        except SubprocessServiceError as exc:
            return self._fail(library_id, path, f"extract-failed: {type(exc).__name__}", content_hash)

        text = result.get("text")
        if text is None:
            reason = str(result.get("failure_reason") or "本机OCR未返回文本")
            category = "scanned" if reason.startswith("too-many-pages:") else "extract-failed"
            return self._fail(library_id, path, f"{category}:{reason}", content_hash)
        if not text.strip():
            return self._fail(library_id, path, "empty", content_hash)

        return ExtractedDocument(
            library_id=library_id,
            path=path,
            text=text,
            failure_reason=None,
            extracted_by=PLUGIN_ID,
            extractor_version=EXTRACTOR_VERSION,
            content_hash=content_hash,
        )

    @staticmethod
    def _fail(library_id: str, path: str, reason: str, content_hash: str = "") -> ExtractedDocument:
        return ExtractedDocument(
            library_id=library_id,
            path=path,
            text=None,
            failure_reason=reason,
            extracted_by=PLUGIN_ID,
            extractor_version=EXTRACTOR_VERSION,
            content_hash=content_hash,
        )
