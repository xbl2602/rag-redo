"""official-ocr-mineru-local 插件：真正的重依赖/模型跑在独立子进程里
（server.py），这个类本身很薄，只负责三件事：
①向 GPU 资源仲裁器申请一个名额（这是"名额"层面的协商，不是真实GPU
硬件探测——探测发生在真正调用真实模型那一刻，见
core/resource_arbiter.py 模块docstring"探测失败fail-open"）；
②用 core.subprocess_service 启动/终止自己声明的子进程；
③把 extract() 转发成对子进程的本机HTTP调用。

**这个沙盒环境刻意不下载真实OCR模型**：用户明确要求——虚拟机磁盘空间
有限，且插件架构本身就该是"模型无关"的，具体模型选型/权重下载应该在
用户真正启用这个插件、真正需要本机OCR能力时才发生，不该为了验证"这个
插件的架构能不能跑起来"就强绑一个真实模型下载（同
official-embedder-bge-m3/official-reranker"真实依赖懒加载、测试注入假
实现"的纪律，这里的"重依赖"从模型权重换成了整个OCR子进程）。
server.py 里真正调用模型的分支懒导入，没装就折叠成清楚的失败原因；
测试用 RAG_REDO_FAKE_OCR=1 环境变量注入确定性假OCR结果，验证的是"子
进程真的启动了、本机HTTP协议真的通了、chain-try 真的把内容喂进主链路
了"，不是"真的识别对了文字"——后者要等真机器（有GPU/磁盘空间的用户
环境）装好真实依赖后验证。

**已知的、刻意的简化**：`env_bootstrap`（首次启用时拉起独立venv）尚未
被核心真正执行——`resolve_plugin_python()` 找不到约定路径下的独立venv
时会退化用核心自己的解释器，见 core/subprocess_service.py 该函数的
docstring。对于这个只用标准库的参考实现，这个简化本身没有问题；一旦
真的要接入需要重依赖的真实OCR模型，`env_bootstrap` 的执行逻辑必须先
落地。

**GPU 资源仲裁（2026-09-23 补齐，和 official-visual-wemm 是同一套协议）**：
和 official-visual-wemm 处于同一优先级层级——两者都是"按需占用"的
subprocess_service GPU 消费者，谁刚需要谁能把对方挤开（`preempt_equal`，
对应旧项目 WEMM/MinerU 互相抢占显存的真实行为）；`on_preempt` 回调只
请求子进程"软驱逐"（`/evict`），不整个杀掉子进程；`_ensure_alive()` 在
真正调用前按需重新拉起（子进程接入真实模型后会有空闲自退出机制，见
server.py TODO 说明，这里先把协议对称打通）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from core.contracts import ExtractedDocument
from core.subprocess_service import SubprocessServiceError, SubprocessServiceHandle, resolve_plugin_python

EXTRACTOR_VERSION = "0.1.0"
PLUGIN_ID = "official-ocr-mineru-local"
GPU_RESOURCE_ID = "gpu:0"
GPU_PRIORITY = 10  # 和 official-visual-wemm 同一层级，互相抢占（preempt_equal）


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MineruLocalOcrPlugin:
    def __init__(self) -> None:
        self._handle: SubprocessServiceHandle | None = None
        self._logger = None
        self._enabled = False
        self._plugin_dir: Path | None = None
        self._runtime_health_check: str | None = None
        self._runtime_command: tuple[str, ...] | None = None

    def on_load(self, ctx):
        self._logger = ctx.logger
        ctx.logger.info("MinerU本机OCR已加载")

    def on_enable(self, ctx):
        # 名额协商，不是真实GPU探测——见模块docstring。
        ctx.resource_arbiter.acquire(
            GPU_RESOURCE_ID, ctx.plugin_id, priority=GPU_PRIORITY, on_preempt=self._soft_evict, preempt_equal=True
        )
        self._plugin_dir = Path(__file__).parent
        self._runtime_health_check = ctx.runtime.health_check
        self._runtime_command = ctx.runtime.command
        self._enabled = True
        self._start_handle()

    def on_disable(self, ctx):
        self._enabled = False
        self._stop_handle()
        ctx.resource_arbiter.release(GPU_RESOURCE_ID, ctx.plugin_id)

    def on_unload(self, ctx):
        self._enabled = False
        self._stop_handle()

    def _start_handle(self) -> None:
        assert self._plugin_dir is not None and self._runtime_command is not None
        python = resolve_plugin_python(self._plugin_dir)
        command = tuple(arg.replace("{python}", python) for arg in self._runtime_command)
        self._handle = SubprocessServiceHandle(command, health_check=self._runtime_health_check, cwd=self._plugin_dir)
        self._handle.start()
        self._logger.info("MinerU本机OCR子进程已启动（端口=%d）", self._handle.port)

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
            return self._fail(library_id, path, "本机OCR子进程未运行")

        try:
            data = full_path.read_bytes()
        except OSError as exc:
            return self._fail(library_id, path, f"读取失败: {type(exc).__name__}: {exc}")
        content_hash = _content_hash(data)

        try:
            result = self._handle.call("extract", {"path": path, "root": str(root)}, timeout=120.0)
        except SubprocessServiceError as exc:
            return self._fail(library_id, path, f"本机OCR调用失败: {exc}", content_hash)

        text = result.get("text")
        if text is None:
            return self._fail(library_id, path, result.get("failure_reason") or "本机OCR未返回文本", content_hash)
        if not text.strip():
            return self._fail(library_id, path, "OCR结果为空", content_hash)

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
