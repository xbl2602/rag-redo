"""official-visual-wemm 插件：页级视觉导航——"第二检索系统"，不参与
core/pipeline.py::search() 的 BM25+向量+RRF 融合排序，是完全独立、单独
调用的检索面（见 core/contracts.py::PageHit 的说明、
docs/ROADMAP.md TODO 第1条的调查结论）。

这个类本身很薄，真正的模型调用在独立子进程（server.py）里，这里只负责
四件事：①向 GPU 资源仲裁器申请一个名额（同 official-ocr-mineru-local，
"名额协商"不是真实GPU探测）；②用 core.subprocess_service 启动/终止自己
声明的子进程；③把 PDF 页面渲染成图（pymupdf 是核心轻量依赖，这一步不需要
子进程隔离）后转发给子进程编码成向量；④把向量写进/查询自己独立的 Chroma
collection——**故意不复用 official-vector-store-chroma 的存储**，那个
插件只认"library_id -> 文字 chunk collection"的语义，这个插件不是
`vector_store` 扩展点的实现者，伸手进另一个插件的持久化目录违反数据流
铁律5"插件不直接读写持久化存储，一律通过 DataStore API，按扩展点类型
收窄权限"——这里走的是每个插件都有的 `ctx.data_dir/<自己的子目录>`
惯例（同 official-vector-store-chroma/official-lexical-bm25 各自的
`ctx.data_dir/chroma`、`ctx.data_dir/bm25` 用法），只是这个插件自己的
子目录叫 `visual_wemm`，物理上和文字向量库彻底分开，比旧项目"同一个
Chroma 文件、不同 collection"的隔离粒度更彻底，行为效果一致（绝不混
向量空间）。

**增量页库**：插件自己的 generation 状态记录 PDF 内容指纹、渲染/模型签名、页 id 和
segment。Pipeline 只把新增或修改的 PDF 交给本轮重渲染；删除/失败文件通过当前有效
页 id 集合屏蔽，查询跨 segment 合并。段数达到阈值时复制现有页向量到 compact segment，
不重新编码模型。

**冻结产物限制**：`env_bootstrap` 在源码环境可执行；PyInstaller 冻结主程序没有通用解释器
时仍拒绝退化到自身 exe，安装包需要携带独立便携 Python。

**GPU 生命周期管理（2026-09-23 补齐，按 obsidian-rag 真实行为移植）**：
子进程自己在 server.py 里做懒加载+两级空闲释放（空闲卸载模型/再空闲更久
整体自退出）+ VRAM 门槛等待，这个类只负责三件配合的事：①用
`preempt_equal=True` 向资源仲裁器申请"gpu:0"名额——和
official-ocr-mineru-local 是同一层级的"按需占用"消费者，谁刚需要谁能把
对方挤开（对应旧项目 WEMM/MinerU 互相抢占显存的真实行为，见
core/resource_arbiter.py::acquire 的 preempt_equal 参数说明）；②
`on_preempt` 回调只请求子进程"软驱逐"（调 `/evict` 卸载模型、不杀子进程
本身——比整个重启轻，重新可用只需模型冷加载不需要重新拉起解释器）；
③`_ensure_alive()` 在每次真正使用前按需重新拉起子进程（同旧项目
`gpu_arbiter.ensure_server` 的幂等语义）——子进程可能因为空闲自退出已经
不在了，不这样做的话"空闲自退出省资源"这个优化会变成"用久了突然不工作"
的真实回归。

**启用门禁（2026-09-24 补齐，缺陷 B）**：`on_enable` 此前**无条件**抢
"gpu:0" 租约并拉起子进程——双击一次 GUI 就等于启动一个 5.1GB 常驻模型
子进程，即使用户从没打开过任何 PDF、也没开过 WEMM。同一个
REQUIRED_PLUGINS 列表里另一个 subprocess_service 插件
（official-ocr-mineru-local/plugin.py:144-154）就有 `is_active()` 门禁，
两个门禁不一致。现在门禁条件对齐 LEGACY 的同名设置项 `wemm_backend`
（obsidian-rag/config.py:122 默认 "on"；取值 on/local 才算开，见
obsidian-rag/wemm_indexer.py:132、obsidian-rag/wemm_retriever.py:37），
读的是 `ctx.settings` 通用设置存储——**用户改了设置不需要重启**，真正的
拉起发生在每次真正使用前读当前设置值。

**用到时才开（2026-10-01 操作者确认，BC-11）**：开着 WEMM 时 `on_enable` 也不再
抢租约、拉子进程——界面、每个 agent 的服务、每次后台索引一启动就各拉一个看图服务
（后台索引启动因此多 2 秒），哪怕这一轮一页都不用编、也没人查页。现在只有真有页
要编码（`index_library` → `_ensure_alive`）或真有页可查（`navigate` →
`_ensure_query_service`）时才抢、才拉；查一个没有页库的库不拉服务。

**navigate 的 veto 期语义（2026-09-24 补齐，缺陷 C）**：抢不到 GPU 租约
时以前只 `return []`，调用方（official-mcp-server 的 `navigate_knowledge`）
拿到空列表照样回 `{"ok": True, "results": []}`，与"确实没有匹配页"完全
同形。LEGACY obsidian-rag/server.py:688-723 专门处理过：索引在跑/拿不到
锁 → 明确告诉调用方"这次没查"；**若看图服务已经活着则跳过一切驻留变更
直接查**（:712-716 注释原文大意："服务已在：直接查，不拉起（拉起是驻留
变更，veto 期一律不做）"）。现在这两种情况用 `VisualVetoError` 区分，
真正的"没有匹配页"仍然是普通空列表。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import tempfile
import threading
import time
from pathlib import Path

import chromadb

from core.atomic import atomic_write_text
from core.contracts import PageHit, VisualPageState
from core.index_generation import IndexGenerationStore
from core.paths import models_env
from core.subprocess_service import SubprocessServiceError, SubprocessServiceHandle, resolve_plugin_python

PLUGIN_ID = "official-visual-wemm"
GPU_RESOURCE_ID = "gpu:0"
GPU_PRIORITY = 10  # 和 official-ocr-mineru-local 同一层级，互相抢占（preempt_equal）
WEMM_RENDER_DPI = 60  # 页图渲染 DPI，行为对齐旧项目 wemm_indexer.py 的默认值
WEMM_DIM = 512  # 输出向量维度，行为对齐旧项目 config.py 的默认值
#: 加载页级视觉模型所需的最小空闲显存（GiB），**只用于向用户显示**。
#: 真正的门槛在 server.py（`WEMM_MIN_VRAM_GB`）——子进程是独立解释器、装在插件
#: 自己的 .venv 里，import 不到这个模块，所以只能各写一份，用
#: `tests/test_plugin.py::TestVramGateIsReportedTruthfully` 钉住两边一致。
#: 6.3 = 2026-09-29 本机实测 6.231 GiB（加载 5.842 + 编一页 0.404）向上取整到 0.1。
#: 旧值 5.5 是照搬旧项目的，从来没在本机验证过，比真实需求低 0.73 GiB。
WEMM_MIN_VRAM_GB = 6.3
#: 看图模型闲置多久自动卸载（秒），**只用于向用户显示**（“试搜会占显卡，闲 N 分钟自动释放”，
#: BC-19）。真正的计时在 server.py（`WEMM_UNLOAD_AFTER_SECONDS`），理由同上各写一份，
#: 由 `tests/test_plugin.py::TestCacheVisibility` 钉住两边一致。
WEMM_UNLOAD_AFTER_SECONDS = 300
#: 每页在页库数据库里大约占多少字节：向量本身（float32，存一份原值、一份检索索引）加上
#: 路径/页码等元数据。页向量存在数据库文件里，按库精确量不出来，“转换缓存”清单只能这样估。
_BYTES_PER_PAGE_ESTIMATE = WEMM_DIM * 4 * 2 + 512
#: 页面小图（“看页库这一页长什么样”）的最长边上限（像素）：足够认出版面，内存里一张不到 1 MB。
_PREVIEW_MAX_SIDE = 1600
VISUAL_INDEX_VERSION = "1"
# LEGACY obsidian-rag/config.py:122 的 wemm_backend 默认值；on/local=开，
# off=关（obsidian-rag/wemm_retriever.py:37 的同一套判定）。
WEMM_BACKEND_DEFAULT = "on"
WEMM_BACKEND_ON = ("on", "local")
LOG_FILE_NAME = "wemm_server.log"  # 同名同落点语义：LEGACY data/wemm_server.log

_VETO_GPU_BUSY = "gpu-busy"
_VETO_BACKEND_OFF = "backend-off"
_VETO_START_FAILED = "service-start-failed"
_VETO_CALL_FAILED = "service-unavailable"


class VisualVetoError(RuntimeError):
    """`navigate` 这一次**没能查**（而不是"查了但没命中"）。

    2026-09-24 补齐（缺陷 C）。以前这里只有一行 warning + `return []`，
    调用方（official-mcp-server 的 `navigate_knowledge`）拿到的空列表与
    "确实没有匹配页"完全同形，Agent 无从判断该重试还是该换查询。LEGACY
    obsidian-rag/wemm_retriever.py 对每一种"没查到"都另外给一条中文原因
    串（`return [], "WEMM 后端未开启（wemm_backend=off）"` 等），由
    obsidian-rag/server.py:728-734 渲染成给用户看的话——这里把同一份
    信息做成一个**带类型的异常**，`reason` 给程序判断，文案给人和 AI 看。

    `reason` 取值：
    - `backend-off`：用户在设置里关了 WEMM；
    - `gpu-busy`：GPU 租约被别人占着（多半是索引在跑）且看图服务没活着，
      本次不做任何驻留变更（LEGACY server.py:714-715）；
    - `service-start-failed`：拿到租约但子进程拉不起来（LEGACY :721）；
    - `service-unavailable`：子进程在，但这一次请求没打通（LEGACY
      wemm_retriever.py:49）。
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _collection_name(library_id: str, generation: str | None = None) -> str:
    if generation:
        key = hashlib.sha256(f"{library_id}\0{generation}".encode("utf-8")).hexdigest()[:40]
        return f"visualg_{key}"
    return f"visual_{library_id}"


def _looks_like_no_path_support(result: dict) -> bool:
    """判断"服务端不认本地路径"这种**可退让**故障。

    这条通道是长驻子进程：宿主代码升级了，跑着的服务端可能还是旧的。老服务端收到
    只有 `path` 没有 `content` 的请求会回 400 + 「content 非空」。那是**接口版本不
    对**，不是模型/显存/网络出了故障——所以可以安全地退回旧的 base64 走法。

    反过来说，别的失败（显存不足、编码失败、服务不可达）**一律不退让**：那些是真实
    故障，退回去只会把同一个错误再撞一遍、白白多花一次 180 秒超时。
    """
    if result.get("ok"):
        return False
    if result.get("reason") in {"vram"}:
        return False
    error = str(result.get("error") or "")
    return "content 非空" in error or "path 非法" in error


class _PageRenderer:
    """后台线程把 PDF 逐页渲染成临时 PNG，用有界队列交给主线程送 GPU。

    #3 提速（2026-09-29）：原来的循环把「CPU 渲染」和「GPU 编码」完全串行——渲染时
    显卡全闲，编码时 CPU 全闲。真机上 7645 页跑了 30 分钟、显卡利用率只有 30~55%、
    功耗 53W，说明有大段是在等 CPU 而不是算。

    改成流水线后总耗时接近 `max(渲染, 编码)` 而不是 `渲染 + 编码`。

    队列深度只给 2，这是**有意的**：再深只是多占几个临时文件（隐私要求"用完即删"，
    见下），对吞吐没帮助——瓶颈是两条流水线的**较慢那一条**，不是排队深度。

    两条纪律：
    - 退出时必须把**所有**自己造过的临时文件删掉，不管消费到哪一步了。不做记账、
      靠 `os.unlink` 对已删文件抛 FileNotFoundError 来兜底：隐私承诺（页图只在临时
      目录过一道、绝不落进持久化目录）不能因为异常路径破个洞。
    - 必须在 `document.close()` **之前** join 完线程。pymupdf 的 Document 不是线程
      安全的，渲染线程还在 `load_page()` 时主线程去 close 会踩出难查的崩溃。
    """

    def __init__(self, document, dpi: int, queue_size: int = 2) -> None:
        self._document = document
        self._dpi = dpi
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._created: list[str] = []
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_PageRenderer":
        self._thread = threading.Thread(
            target=self._work, name="wemm-page-render", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30.0)
        for path in self._created:
            try:
                os.unlink(path)
            except OSError:
                pass  # 已经消费掉删掉了就是我们要的结果
        self._created.clear()
        return False

    def _work(self) -> None:
        """渲染线程。**绝不抛异常**：这里抛出去没人接，队列会永远空着，
        主线程就会把"渲染失败"误判成"这份 PDF 渲染完了"。"""
        try:
            total = int(self._document.page_count)
        except Exception:  # noqa: BLE001
            return
        for page_index in range(total):
            if self._stop.is_set():
                return
            tmp_path = None
            try:
                page = self._document.load_page(page_index)
                png_bytes = page.get_pixmap(dpi=self._dpi).tobytes("png")
                fd, tmp_path = tempfile.mkstemp(prefix="wemm_page_", suffix=".png")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(png_bytes)
                self._created.append(tmp_path)
            except Exception:  # noqa: BLE001 - 渲不出这一页就跳过，不影响别的页
                continue
            # 队列满时等主线程消化。轮询 + stop 检查：卡在满队列上时也能立刻响应退出。
            while not self._stop.is_set():
                try:
                    self._queue.put((page_index, tmp_path), timeout=0.5)
                    break
                except queue.Full:
                    continue

    def __iter__(self):
        """逐页产出 `(page_index, tmp_path)`，到渲染线程收工且队列排空为止。"""
        while True:
            try:
                yield self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._thread is not None and self._thread.is_alive():
                    continue
                # 线程没了，再排一次空队列确认真的排空了（避免最后一页正卡在 put 上）
                try:
                    yield self._queue.get_nowait()
                except queue.Empty:
                    return


class VisualWemmPlugin:
    def __init__(self) -> None:
        self._handle: SubprocessServiceHandle | None = None
        self._client = None
        self._logger = None
        self._enabled = False
        self._plugin_dir: Path | None = None
        self._runtime_health_check: str | None = None
        self._runtime_command: tuple[str, ...] | None = None
        self._runtime_env_bootstrap: str | None = None
        self._generations: IndexGenerationStore | None = None
        self._state_root: Path | None = None
        self._storage_root: Path | None = None
        self._resource_arbiter = None
        self._plugin_id = ""
        self._settings = None
        self._log_path: Path | None = None
        #: 最近一次因为显存不足被挡下的记录（`{required_gb, free_gb, forced}`），
        #: 供 `status()` 与 GUI 快照如实显示"需要多少 / 现在多少"（2026-09-29 新增）。
        #: None = 没被挡过。刚起来时是 None，不代表"够用"——够不够由服务端的
        #: 门槛判断，这里只记录**被挡下**这件事，免得每次快照都去探测拉模型。
        self._vram_blocked: dict | None = None

    def on_load(self, ctx):
        storage_root = ctx.storage.directory("visual_wemm", legacy="visual_wemm")
        persist_dir = storage_root / "chroma"
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._generations = IndexGenerationStore(
            ctx.storage.directory("index_generations", legacy="index_generations")
        )
        self._state_root = storage_root / "state"
        self._storage_root = storage_root
        self._logger = ctx.logger
        self._settings = ctx.settings
        # 子进程输出（模型加载失败/端口冲突/socketserver 的 traceback）落到
        # 本插件在 DATA_ROOT 下的数据目录，而不是插件源码目录——LEGACY
        # obsidian-rag/gpu_arbiter.py:32 落 data/wemm_server.log
        # （gpu_arbiter.py:241-252 用 `Popen(stdout=logf, stderr=logf)`
        # 指向它），这里保持同一种"日志跟着数据根走、卸载便携包不会连带
        # 删掉诊断信息"的行为（架构红线：所有数据落在 data/ 目录下）。
        self._log_path = storage_root / LOG_FILE_NAME
        ctx.logger.info("WEMM页级视觉导航已加载")

    def on_enable(self, ctx):
        """只记下启动子进程要用的东西，**不抢显卡名额、不拉起子进程**：第一次真有页要编码
        （`index_library` → `_ensure_alive`）或真要查（`navigate` → `_ensure_query_service`）
        时才抢、才拉。2026-10-01 操作者确认（BC-11）：此前开着页库时，界面、每个 agent 的
        服务、每次后台索引一启动就各拉一个看图服务（后台索引启动因此多 2 秒），哪怕这一轮
        一页都不用编、也没人查页。WEMM 关着时两条路照旧什么都不做（`is_active` 门禁）。"""
        self._resource_arbiter = ctx.resource_arbiter
        self._plugin_id = ctx.plugin_id
        self._plugin_dir = Path(__file__).parent
        self._runtime_health_check = ctx.runtime.health_check
        self._runtime_command = ctx.runtime.command
        self._runtime_env_bootstrap = ctx.runtime.env_bootstrap
        self._enabled = True

    def force_load_enabled(self) -> bool:
        """用户是否开启了「强制加载页级视觉导航」（设置项 `wemm_force_load`）。

        2026-09-29 新增。WEMM 2B 在本机实测要占 6.23 GiB（见 server.py
        `WEMM_MIN_VRAM_GB` 的注释），8GB 卡在 Windows + 桌面应用占掉约 2GB 的
        情况下只剩 6.878 GiB，**余量不到 0.7 GiB**。用户关掉几个占显存的程序可能
        就够，也可能怎么都不够——所以给一个显式开关让他自己判断，而不是替他决定。

        开着时只是**跳过那道门槛去试**，不吞异常：真 OOM 照样明确报错，不会伪装成
        成功，也不会静默截断向量（那会污染已建好的页库）。

        每次现读 `ctx.settings` 不缓存（同 `backend()` 的理由：长驻进程里用户中途
        改设置必须立刻生效）。没设置过就是 False——默认仍走"显存不够就不加载"的
        安全路径。
        """
        if self._settings is None:
            return False
        return bool(self._settings.get("wemm_force_load", False))

    def backend(self) -> str:
        """当前 WEMM 后端设置值（LEGACY obsidian-rag/config.py 的同名设置项
        `wemm_backend`）。每次现读 `ctx.settings`，不缓存快照——长驻进程里
        用户中途改设置必须立刻生效（LEGACY obsidian-rag/server.py:954-961
        的 `_wemm_cfg()` 专门为此加了 `reload_config()`，同一个坑）。"""
        if self._settings is None:
            return WEMM_BACKEND_DEFAULT
        return str(self._settings.get("wemm_backend", WEMM_BACKEND_DEFAULT) or WEMM_BACKEND_DEFAULT)

    def is_active(self) -> bool:
        """WEMM 后端是否已开启（取值 on/local）。判定口径与 LEGACY
        obsidian-rag/wemm_retriever.py:37、wemm_indexer.py:132 完全一致。"""
        return self.backend() in WEMM_BACKEND_ON

    def log_file(self) -> str | None:
        """子进程日志文件路径（只读诊断用，对齐 LEGACY
        obsidian-rag/server.py:965 的 `wemm_status` 把
        `data/wemm_server.log` 明确指给用户/AI 的做法）。"""
        return str(self._log_path) if self._log_path is not None else None

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
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
        self._client = None
        self._generations = None
        self._state_root = None
        self._settings = None

    def _start_handle(self) -> None:
        assert self._plugin_dir is not None and self._runtime_command is not None
        python = resolve_plugin_python(self._plugin_dir, env_bootstrap=self._runtime_env_bootstrap, logger=self._logger)
        command = tuple(arg.replace("{python}", python) for arg in self._runtime_command)
        self._handle = SubprocessServiceHandle(
            command,
            health_check=self._runtime_health_check,
            cwd=self._plugin_dir,
            log_path=self._log_path,
            # 让看图服务去用户配置的模型目录找/下模型（BC-17）。子进程启动时定死：
            # 之后在设置页改路径，要等这个服务下次（重新）启动才生效。
            env=models_env(self._settings.get("models_dir", "") if self._settings is not None else ""),
        )
        self._handle.start()
        if self._handle.log_file_error:
            self._logger.warning(
                "WEMM子进程日志文件打不开，本次输出不会落盘（服务本身不受影响）：%s",
                self._handle.log_file_error,
            )
        self._logger.info(
            "WEMM页级视觉导航子进程已启动（端口=%d，日志=%s）", self._handle.port, self.log_file() or "无（不落盘）"
        )

    def _stop_handle(self) -> None:
        if self._handle is not None:
            self._handle.stop()
            self._handle = None

    def release_gpu(self) -> None:
        """手动立即释放显存用（core/pipeline.py::release_gpu_memory，
        2026-09-29 新能力，BC-16）——就是资源仲裁器抢占时走的同一条软驱逐
        路径：只请求子进程卸载模型，子进程本身继续存活监听，不影响
        `wemm_backend` 设置或插件启用状态。子进程没在跑/已经空闲就是
        no-op（`_soft_evict` 内部已判断）。"""
        self._soft_evict()

    def _soft_evict(self) -> None:
        """资源仲裁器的抢占回调：只请求子进程卸载模型释放显存，不杀子进程
        本身。HTTP 调用失败也绝不阻塞抢占方——fail-open，同
        core/gpu_arbiter.py::request_evict 的策略（这里直接用已经建好的
        handle 发请求，不复用那个独立函数——子进程边的 server.py 完全
        隔离，import 不到 core.*，见 server.py 模块 docstring）。"""
        if self._handle is not None and self._handle.is_alive:
            try:
                self._handle.call("evict", {}, timeout=15.0)
            except SubprocessServiceError:
                pass

    def _ensure_alive(self) -> bool:
        """索引态的"确保子进程可用"：抢不到租约/拉不起来都只返回 False，由
        调用方折叠成结构化失败终态（索引态永远不抛异常，见模块 docstring），
        下一轮自动重试。查询态不走这里——见 `_ensure_query_service`。"""
        if not self._enabled or not self.is_active():
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
            self._logger.warning("WEMM子进程重新拉起失败：%s", exc)
            return False

    def _raise_if_backend_off(self) -> None:
        if not self.is_active():
            raise VisualVetoError(
                _VETO_BACKEND_OFF,
                "（WEMM 视觉导航未开启：把设置里的 wemm_backend 设为 on/local 后重新调用本工具"
                "——看图服务会按需自动拉起，页索引随 reindex_knowledge/自动同步自动建。）",
            )

    def _ensure_query_service(self) -> None:
        """查询态的"确保看图服务可用"，**失败一律抛 `VisualVetoError`**，
        绝不静默退化成空列表（缺陷 C：调用方必须能区分"GPU 忙/服务不可用"
        和"确实没有匹配页"）。

        关键的一条降级路径（LEGACY obsidian-rag/server.py:712-716）：**服务
        已经活着就直接查，不做任何驻留变更**——既不抢租约也不重新拉起子
        进程。理由同 LEGACY 注释：拉起子进程是驻留变更，veto 期（索引正在
        跑、显存正忙）一律不做；模型真需要重新加载时，子进程那一侧自己的
        显存门槛等待会处理（server.py 的 `_wait_for_vram`）。

        `on_enable` 从不抢租约、不拉服务（BC-11，2026-10-01），所以用户中途把开关
        打开后**不重启**也能在这里被正常拉起（每次现读设置，见 `backend()`）。"""
        self._raise_if_backend_off()
        if self._handle is not None and self._handle.is_alive:
            return  # 服务已在：直接查，不拉起、不抢租约
        holder =self._resource_arbiter.holder_of(GPU_RESOURCE_ID) if self._resource_arbiter is not None else None
        # 名额没人占时也要先登记成自己的再拉服务（2026-10-01）：此前只在“别人占着”时才抢，
        # 启用时已经抢好、空着的情况只发生在服务空闲自退出之后；现在启用时不再抢，
        # 第一次查页就走这里——不登记，别的显卡用户就不知道看图服务占着显存。
        if holder != self._plugin_id and self._resource_arbiter is not None:
            acquired = self._resource_arbiter.acquire(
                GPU_RESOURCE_ID,
                self._plugin_id,
                priority=GPU_PRIORITY,
                on_preempt=self._soft_evict,
                preempt_equal=True,
            )
            if not acquired:
                raise VisualVetoError(
                    _VETO_GPU_BUSY,
                    "（索引任务进行中（或显存正忙），本次不抢占模型——"
                    "页索引随索引自动同步，稍后重试即可。）",
                )
        if self._handle is not None and self._handle.is_alive:
            return  # 抢占过程中把已停的服务顺手拉起来了（抢到了就直接用）
        try:
            self._start_handle()
        except SubprocessServiceError as exc:
            raise VisualVetoError(_VETO_START_FAILED, f"（看图服务拉起失败：{exc}）") from exc

    def _collection(self, library_id: str, generation: str | None = None):
        return self._client.get_or_create_collection(
            name=_collection_name(library_id, generation),
            metadata={"hnsw:space": "cosine", "library_id": library_id},
        )

    def _state_path(self, library_id: str, generation: str) -> Path:
        assert self._state_root is not None
        key = hashlib.sha256(library_id.encode("utf-8")).hexdigest()[:24]
        return self._state_root / key / f"{generation}.json"

    def _read_state(self, library_id: str, generation: str | None) -> dict:
        if self._state_root is None or not generation:
            return {"format_version": 1, "segments": [], "files": {}}
        try:
            data = json.loads(self._state_path(library_id, generation).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"format_version": 1, "segments": [], "files": {}}
        if not isinstance(data, dict) or data.get("format_version") != 1:
            return {"format_version": 1, "segments": [], "files": {}}
        return data

    def _write_state(self, library_id: str, generation: str, state: dict) -> None:
        if self._state_root is None:
            return
        path = self._state_path(library_id, generation)
        try:
            atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))
        except OSError:
            pass

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int, str]:
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return stat.st_size, stat.st_mtime_ns, digest.hexdigest()

    # ---- 索引态 ----------------------------------------------------------

    def index_library(
        self,
        library_id: str,
        root: Path,
        pdf_paths: list[str],
        generation: str | None = None,
        changed_paths: list[str] | None = None,
        previous_generation: str | None = None,
        before_serve=None,
        progress=None,
    ) -> None:
        """`before_serve`：真要占显卡渲染页面之前调用一次的"让路"回调（对齐旧项目
        obsidian-rag/index.py:1784-1806 `_release_for_wemm` 经 wemm_indexer.py:266-272
        传入的 `before_serve`）——调用方借它把文字向量/重排模型从显卡上卸下来。
        没有页需要渲染时不会调用（零拉起零开销，wemm_indexer.py:253-254）；回调抛异常
        也绝不影响页级索引。"""
        if not self.is_active():
            # LEGACY obsidian-rag/index.py:1842 与
            # docs/legacy/TASK_LOG.md:1818 的原话："wemm_backend off →
            # 静默跳过（零开销）"——用户没开这个能力时不能顺手把 5.1GB 子
            # 进程拉起来，也不该留下"失败"记录冒充页索引失败。
            self._logger.info(
                "WEMM后端未开启（wemm_backend=%s），跳过页级索引（不落任何状态）", self.backend()
            )
            return
        generation_key = generation or "legacy"
        if previous_generation:
            state = self._read_state(library_id, previous_generation)
        elif generation is None:
            state = self._read_state(library_id, generation_key)
        else:
            state = {"format_version": 1, "segments": [], "files": {}}
        files = {
            str(path): dict(record)
            for path, record in state.get("files", {}).items()
            if isinstance(record, dict)
        } if isinstance(state.get("files"), dict) else {}
        segments = [str(value) for value in state.get("segments", []) if value]
        signature = f"{VISUAL_INDEX_VERSION}:{WEMM_RENDER_DPI}:{WEMM_DIM}"
        old_signature = str(state.get("signature", ""))
        requested = set(changed_paths) if changed_paths is not None else set(pdf_paths)
        if old_signature and old_signature != signature:
            requested.update(pdf_paths)
        current_paths = set(pdf_paths)
        files = {path: record for path, record in files.items() if path in current_paths}
        # 失败/部分成功的记录**每轮都重试**：旧项目 wemm_indexer.py:311-312 "终态条目绝不走
        # 快速路径——失败文件（看图服务中途挂掉等）每轮都给重试机会，让「记入终态待重试」
        # 是真承诺而非死寂"。此前这里只要 PDF 没变就永远跳过失败记录：真机 70 个 PDF 因
        # "WEMM子进程未运行"被记失败后再也没有页库。没有记录、或记录的签名不是当前签名的
        # PDF 同样必须处理，否则下面的快速路径会拿它们去用一个没起的服务。
        requested.update(
            path for path in pdf_paths
            if files.get(path, {}).get("status") != "indexed"
            or files.get(path, {}).get("signature") != signature
        )
        indexed_pages = 0
        # 用户是否开了「强制加载页级视觉导航」。整轮只读一次：设置在长驻进程里
        # 中途改了不该让同一轮的前后不一致。
        force_load = self.force_load_enabled()

        # ── 页级进度上报（2026-09-29 新增，核心 contracts.py::VisualProgress）──
        # 视觉索引的进度口径是文字索引的 files_done/files_total，那两个数在进入
        # 视觉阶段前就已经是最终值（真机 Y2S1：78/78、100%），于是渲染 7645 页的
        # 这 30 分钟里界面一个数都不变、看起来完全像冻住。旧项目有页级进度回调
        # （wemm_indexer.py 问题47），移植时漏了，这里补回来。
        #
        # 两个必须自己算的量：
        # ① pages_total —— 全部 PDF 的总页数。只能在渲染前数一遍：pymupdf 的
        #    `page_count` 不打开文件就拿不到，而逐页编码已经开着文档了。
        # ② 节流 —— 页循环每页都走完，逐页写进度文件会让磁盘 IO 变成瓶颈
        #    （实测吞吐 12~15 页/秒，即每秒 12~15 次写盘）。按时间节流到 ~2 秒一次，
        #    顺带保证一定在收尾时补报一次终值。
        pages_total = 0
        if progress is not None:
            try:
                import pymupdf as _pm

                for _p in pdf_paths:
                    try:
                        with _pm.open(str(root / _p)) as _doc:
                            pages_total += int(_doc.page_count)
                    except Exception:  # noqa: BLE001 - 数不出页数只是进度不准，不该中断
                        continue
            except Exception:  # noqa: BLE001 - 同上
                pages_total = 0

        pages_done = 0
        last_report = [0.0]

        def _report(current_path: str = "", force: bool = False) -> None:
            """上报页级进度。任何异常都吞掉——进度上报失败绝不能拖垮页级索引。"""
            if progress is None:
                return
            now = time.monotonic()
            if not force and now - last_report[0] < 2.0:
                return
            last_report[0] = now
            try:
                progress(pages_done, pages_total, current_path)
            except Exception:  # noqa: BLE001 - 见上：进度是锦上添花，不是主流程
                pass

        if requested:
            # 真要渲染页面了：先让文字向量/重排模型让出显卡，再去抢名额拉服务
            if before_serve is not None:
                try:
                    before_serve()
                except Exception as exc:  # noqa: BLE001 - 让路失败绝不能拖垮页级索引
                    self._logger.warning("WEMM让路回调失败（忽略）：%s: %s", type(exc).__name__, exc)
            alive = self._ensure_alive()
        else:
            alive = True  # 没有页要渲染：不占显卡、不拉服务，只按旧记录计页
        collection = None
        if alive and requested:
            try:
                collection = self._collection(library_id, generation)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("WEMM页库创建失败：%s: %s", type(exc).__name__, exc)
                alive = False
        if alive:
            import pymupdf

            # 2026-09-29 真机事故（data-real/index_worker.log + visual_wemm/wemm_server.log）：
            # 子进程被成功拉起、`alive` 为 True，但服务端 `_wait_for_vram(5.5GB)` 等不到
            # 显存（8GB 卡上 Windows 与桌面应用本身就吃掉约 2.6GB，把全部模型卸干净后空闲
            # 也只有约 5.1GB），随后连接被拒。此时页级 embed 每页都抛
            # SubprocessServiceError，而旧代码只 `continue` 换下一页 —— 78 份 PDF × 每份
            # 几十页 = 几千次注定失败的调用，索引进程假活半小时；用户以为卡死而中断，整轮
            # generation 从未发布（manifest 写入与 commit 都在视觉阶段之后，
            # core/pipeline.py::_emit("visual") 之后），已算好的文字索引全部丢失。
            #
            # 恢复旧项目 wemm_indexer.py:257-278（问题46「单轮单次」）的语义：**服务不可达
            # 不是"这一页不行"，而是"本轮服务不可用"**——第一次撞上就终止本轮页级索引，
            # 剩余文件记可重试失败终态，由文字索引照常发布、下轮自动重试。
            # 只熔断"服务级"故障（SubprocessServiceError）；服务健康、只是这一页编码不出来
            # （`ok=False`）仍然逐页继续——把单页失败也当服务挂了会让一次手抖毁掉整轮。
            service_down: str | None = None

            # ── #4 传本地临时文件路径，而不是 base64 像素（2026-09-29 提速）──
            # 宿主和服务在同一台机器。旧走法把 PNG 做 base64 塞进 JSON（体积 +33%），
            # 服务端再解 base64、把**同样内容**的 PNG 写进一个临时文件——因为模型的
            # 图像接口只收文件路径/URL，不收原始字节。于是每页都在磁盘上白往返一趟，
            # 中间还被 json.dumps 转义、json.loads 解析各走一遍。
            #
            # 改后：宿主自己落临时文件，JSON 里只发几十字节的路径。隐私性质一字未改
            # （还是本机临时文件、用完即删、绝不出网，见 server.py 同名注释）。
            #
            # `path_mode` 会自我纠正：这是个**长驻子进程**，宿主升级后它可能还跑着旧代码，
            # 老服务端不认识 `path`、回一句"content 非空"。撞上就整轮永久退回 base64，
            # 不影响正确性，只是慢一点——宁可慢，不许因为提速改动而整轮失败。
            path_mode = [True]

            def _embed_page(tmp_path: str):
                """送一页去编码，返回服务端结果；自动在两种走法之间退让。"""
                if path_mode[0]:
                    result = self._handle.call(
                        "embed",
                        {
                            "kind": "image",
                            "path": tmp_path,
                            "dim": WEMM_DIM,
                            "force": force_load,
                        },
                        timeout=180.0,
                    )
                    if result.get("ok") or not _looks_like_no_path_support(result):
                        return result
                    path_mode[0] = False
                    self._logger.warning(
                        "WEMM子进程是旧版本（不认本地路径），本页起退回 base64 走法："
                        "会慢一些但结果一致。重启 WEMM 服务可恢复更快的走法。"
                    )
                # 退回旧走法才需要像素字节：从刚落盘的临时文件读回来。只在老服务端
                # 这条慢路径上多一次读盘，快路径上不发生。
                with open(tmp_path, "rb") as fh:
                    png_bytes = fh.read()
                return self._handle.call(
                    "embed",
                    {
                        "kind": "image",
                        "content": base64.b64encode(png_bytes).decode("ascii"),
                        "dim": WEMM_DIM,
                        "force": force_load,
                    },
                    timeout=180.0,
                )

            for path in pdf_paths:
                if service_down is not None:
                    break
                old = files.get(path, {})
                full_path = root / path
                if path not in requested and old.get("signature") == signature:
                    indexed_pages += len(old.get("page_ids", []))
                    continue
                try:
                    size, mtime_ns, content_hash = self._fingerprint(full_path)
                except OSError as exc:
                    files[path] = {
                        "signature": signature,
                        "status": "failed",
                        "failure_reason": f"{type(exc).__name__}: {exc}",
                        "page_ids": [],
                        "segment": generation_key,
                    }
                    continue
                if (
                    old.get("status") == "indexed"
                    and old.get("signature") == signature
                    and old.get("content_hash") == content_hash
                ):
                    files[path] = old
                    indexed_pages += len(old.get("page_ids", []))
                    continue
                try:
                    document = pymupdf.open(str(full_path))
                except Exception as exc:  # noqa: BLE001
                    files[path] = {
                        "size": size,
                        "mtime_ns": mtime_ns,
                        "content_hash": content_hash,
                        "signature": signature,
                        "status": "failed",
                        "failure_reason": f"{type(exc).__name__}: {exc}",
                        "page_ids": [],
                        "segment": generation_key,
                    }
                    continue
                ids: list[str] = []
                embeddings: list[list[float]] = []
                metadatas: list[dict] = []
                try:
                    # #3：渲染交给后台线程，与主线程的 GPU 编码重叠。渲染器必须在
                    # document.close() 之前退出（`with` 在这里、close 在外层 finally），
                    # 否则渲染线程可能正在 load_page 时文档被关掉。
                    with _PageRenderer(document, WEMM_RENDER_DPI) as renderer:
                        for page_index, tmp_path in renderer:
                            try:
                                result = _embed_page(tmp_path)
                            except SubprocessServiceError as exc:
                                # 服务级故障（连接被拒/超时/子进程已死），不是这一页的问题。
                                # 本轮就此收手（见上方 2026-09-29 注释），否则整库页数乘以失败
                                # 次数空转，索引假活而用户看不到任何提示。
                                service_down = f"{type(exc).__name__}: {exc}"
                                self._logger.error(
                                    "WEMM子进程不可达（%s），本轮页级索引到此终止：%s", path, service_down
                                )
                                break
                            finally:
                                # #4：这一页用完立刻删。渲染器退出时还会兜底扫一遍，
                                # 所以这里删失败（已删/被清理）不算问题。
                                try:
                                    os.unlink(tmp_path)
                                except OSError:
                                    pass
                            if not result.get("ok"):
                            # 显存不足是**用户能自己解决/决定**的一类失败（关掉占显存的
                            # 程序，或在设置里开强制加载），必须把两个数字如实记下来交给
                            # GUI 显式告知，而不是只留一句 error 让人猜。
                                if result.get("reason") == "vram":
                                    self._vram_blocked = {
                                        "required_gb": result.get("required_gb"),
                                        "free_gb": result.get("free_gb"),
                                        "forced": bool(force_load),
                                    }
                                    self._logger.error(
                                        "WEMM显存不足：需要 %s GB，当前 %s GB（%s）。"
                                        "本轮页级索引记为待重试，文字索引照常发布。",
                                        result.get("required_gb"),
                                        result.get("free_gb"),
                                        "已开强制加载仍失败" if force_load else "可关闭占显存的程序后重试，"
                                        "或在设置里开启「强制加载页级视觉导航」",
                                    )
                                else:
                                    self._logger.warning(
                                        "WEMM编码失败，跳过 %s 第%d页：%s", path, page_index, result.get("error")
                                    )
                                continue
                            ids.append(f"{path}::{page_index}")
                            embeddings.append(result["embedding"])
                            metadatas.append(
                                {
                                    "path": path,
                                    "page": page_index,
                                    "abs_path": str(full_path),
                                    "library_id": library_id,
                                }
                            )
                            pages_done += 1
                            _report(path)
                    # 注意这段在 `with _PageRenderer(...)` **之外**：渲染线程必须已经
                    # 退出（__exit__ 里 join 过）才碰 document.close() 和页库写入。
                    if ids and collection is not None:
                        collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)
                        indexed_pages += len(ids)
                    files[path] = {
                        "size": size,
                        "mtime_ns": mtime_ns,
                        "content_hash": content_hash,
                        "signature": signature,
                        "status": "indexed" if len(ids) == document.page_count else "partial" if ids else "failed",
                        "failure_reason": None if len(ids) == document.page_count else "部分页面编码失败",
                        "page_ids": ids,
                        # 总页数：“转换缓存”清单据此写“28/36 页”、列出缺哪几页（BC-19）
                        "page_count": int(document.page_count),
                        # 哪一轮真的编的这些页（压缩会改 segment，但不改这一项）：每轮日志据此
                        # 分清页库“复用”和“新建”（BC-19）
                        "built_in": generation_key,
                        "segment": generation_key,
                    }
                finally:
                    document.close()
            # 收尾强制补报一次终值：节流到 ~2 秒一次意味着最后一次可能刚好被丢掉，
            # 界面上就会停在「3186/7645」然后直接跳到结束——那正是最难解释的形态。
            _report(force=True)
            if service_down is not None:
                # 本轮提前收手：没轮到、以及本轮明确失败的文件都留下失败终态，下一轮才会
                # 自动重试（`requested` 的重建条件含 `status != "indexed"`，见本函数上方注释）。
                # 已经编出页的文件保留 indexed/partial——宁可 partial 也不丢已付出的编码。
                for path in sorted(requested):
                    record = files.get(path)
                    if record is None or record.get("status") not in {"indexed", "partial"}:
                        files[path] = {
                            "signature": signature,
                            "status": "failed",
                            "failure_reason": f"WEMM子进程不可达，本轮未执行（下轮自动重试）：{service_down}",
                            "page_ids": [],
                            "segment": generation_key,
                        }
        else:
            for path in requested:
                files[path] = {
                    "signature": signature,
                    "status": "failed",
                    "failure_reason": "WEMM子进程未运行",
                    "page_ids": [],
                    "segment": generation_key,
                }
            self._logger.warning("WEMM子进程未运行，页级索引本轮跳过")
        if collection is not None and generation_key not in segments and any(
            record.get("segment") == generation_key and record.get("page_ids")
            for record in files.values()
        ):
            segments.append(generation_key)
        active_ids = {
            page_id
            for record in files.values()
            if record.get("status") in {"indexed", "partial"}
            for page_id in record.get("page_ids", [])
        }
        if len(segments) >= 3 and active_ids:
            compact_segment = f"{generation_key}-compact"
            try:
                compact = self._collection(library_id, compact_segment)
                by_segment: dict[str, list[str]] = {}
                for record in files.values():
                    segment = str(record.get("segment", ""))
                    by_segment.setdefault(segment, []).extend(record.get("page_ids", []))
                for segment, page_ids in by_segment.items():
                    if not segment or not page_ids:
                        continue
                    source = self._collection(library_id, segment)
                    rows = source.get(ids=page_ids, include=["embeddings", "metadatas"])
                    if rows.get("ids"):
                        compact.upsert(
                            ids=rows["ids"],
                            embeddings=rows.get("embeddings"),
                            metadatas=rows.get("metadatas"),
                        )
                for record in files.values():
                    record["segment"] = compact_segment
                segments = [compact_segment]
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("WEMM页库压缩失败：%s: %s", type(exc).__name__, exc)
        if generation is None and collection is not None:
            try:
                existing_ids = collection.get(include=[])["ids"]
                stale_ids = [page_id for page_id in existing_ids if page_id not in active_ids]
                if stale_ids:
                    collection.delete(ids=stale_ids)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("WEMM清理旧页失败：%s: %s", type(exc).__name__, exc)
        self._write_state(
            library_id,
            generation_key,
            {
                "format_version": 1,
                "library_id": library_id,
                "generation": generation_key,
                "signature": signature,
                "segments": segments,
                "files": files,
            },
        )
        self._logger.info("WEMM页级索引完成：库=%s，%d页", library_id, indexed_pages)

    def export_state(self, library_id: str, generation: str) -> dict:
        state = dict(self._read_state(library_id, generation))
        collections: dict[str, list[dict]] = {}
        for segment in state.get("segments", []):
            try:
                collection = self._client.get_collection(name=_collection_name(library_id, segment))
                rows = collection.get(include=["embeddings", "documents", "metadatas"])
                collections[str(segment)] = [
                    {
                        "id": str(page_id),
                        "embedding": [float(value) for value in (embedding or [])],
                        "document": document or "",
                        "metadata": dict(metadata or {}),
                    }
                    for page_id, embedding, document, metadata in zip(
                        rows.get("ids", []),
                        rows.get("embeddings", []),
                        rows.get("documents", []),
                        rows.get("metadatas", []),
                    )
                ]
            except Exception:
                continue
        state["collections"] = collections
        return state

    def import_state(self, library_id: str, state: dict, generation: str) -> None:
        if not isinstance(state, dict):
            return
        restored = dict(state)
        restored["library_id"] = library_id
        restored["generation"] = generation
        self._write_state(library_id, generation, restored)
        for segment, rows in (restored.get("collections", {}) or {}).items():
            if not isinstance(rows, list):
                continue
            collection = self._client.get_or_create_collection(
                name=_collection_name(library_id, str(segment)),
                metadata={"hnsw:space": "cosine", "library_id": library_id},
            )
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                collection.upsert(
                    ids=[str(row["id"])],
                    embeddings=[row.get("embedding", [])],
                    documents=[row.get("document", "")],
                    metadatas=[row.get("metadata", {})],
                )

    def graph_page_states(
        self,
        library_id: str,
        generation: str,
    ) -> tuple[VisualPageState, ...]:
        files = self._read_state(library_id, generation).get("files", {})
        if not isinstance(files, dict):
            return ()
        states: list[VisualPageState] = []
        for path, record in sorted(files.items()):
            if not isinstance(path, str) or not isinstance(record, dict):
                continue
            pages = tuple(
                sorted(
                    {
                        int(page_id.rsplit("::", 1)[1]) + 1
                        for page_id in record.get("page_ids", [])
                        if isinstance(page_id, str)
                        and page_id.startswith(f"{path}::")
                        and page_id.rsplit("::", 1)[-1].isdigit()
                    }
                )
            )
            reason = record.get("failure_reason")
            page_count = record.get("page_count")
            states.append(
                VisualPageState(
                    library_id=library_id,
                    path=path,
                    provider_id="official-visual-wemm",
                    status=str(record.get("status") or "failed"),
                    failure_reason=str(reason) if reason else None,
                    pages=pages,
                    page_count=int(page_count) if isinstance(page_count, int) and page_count > 0 else None,
                    built_in=str(record["built_in"]) if record.get("built_in") else None,
                )
            )
        return tuple(states)

    def delete_generation(self, library_id: str, generation: str) -> None:
        before = {str(value) for value in self._read_state(library_id, generation).get("segments", [])}
        referenced: set[str] = set()
        if self._generations is not None:
            active = self._generations.active(library_id)
            for state_generation in [active, *self._generations.history(library_id)]:
                if state_generation:
                    referenced.update(str(value) for value in self._read_state(library_id, state_generation).get("segments", []))
        for segment in before - referenced:
            try:
                segment_name = None if segment == "legacy" else segment
                self._client.delete_collection(name=_collection_name(library_id, segment_name))
            except Exception:
                pass
        if generation not in referenced:
            try:
                self._client.delete_collection(name=_collection_name(library_id, generation))
            except Exception:
                pass
        if self._state_root is not None:
            try:
                self._state_path(library_id, generation).unlink(missing_ok=True)
            except OSError:
                pass

    # ---- 只读诊断 ----------------------------------------------------------

    def status(self) -> dict:
        """WEMM 页级视觉导航状态——对齐 obsidian-rag 的 `wemm_status` MCP
        工具："建没建、生效没生效"，用户/AI 一眼能确认，不用靠猜。只读，
        不拉起子进程、不加载模型（同 obsidian-rag 该工具"只读，不启动
        服务、不加载模型"的承诺——用 `self._handle` 的现有快照判断存活，
        不调用 `_ensure_alive()`）。

        当前 generation 的状态文件保留每个 PDF 的成功/部分成功/失败原因；
        本方法汇总有效页数、PDF 数和失败列表，不加载模型。"""
        alive = self._handle is not None and self._handle.is_alive
        libraries: dict[str, dict] = {}
        service = self._health_snapshot() if alive else None
        if self._client is not None:
            active_pairs = self._generations.active_pairs() if self._generations is not None else {}
            for library_id, generation in active_pairs.items():
                state = self._read_state(library_id, generation)
                files = state.get("files", {})
                if not isinstance(files, dict):
                    continue
                page_count = sum(
                    len(record.get("page_ids", []))
                    for record in files.values()
                    if isinstance(record, dict) and record.get("status") in {"indexed", "partial"}
                )
                failures = [
                    {"path": path, "reason": record.get("failure_reason", "未知失败")}
                    for path, record in sorted(files.items())
                    if isinstance(record, dict) and record.get("status") == "failed"
                ]
                libraries[library_id] = {
                    "page_count": page_count,
                    "pdf_count": len(files),
                    "failures": failures,
                }
            for coll in self._client.list_collections():
                if not coll.name.startswith("visual_") or coll.name in {
                    _collection_name(library_id, generation) for library_id, generation in active_pairs.items()
                }:
                    continue
                library_id = coll.name[len("visual_"):]
                metadatas = coll.get(include=["metadatas"])["metadatas"] or []
                pdf_paths = {m["path"] for m in metadatas if m and m.get("path")}
                libraries.setdefault(
                    library_id,
                    {"page_count": len(metadatas), "pdf_count": len(pdf_paths)},
                )
        return {
            "enabled": self._enabled,
            # 后端开关（LEGACY wemm_status 第一行就报 wemm_backend=off/on-local，
            # obsidian-rag/server.py:977-982）——用户/AI 一眼能确认"我到底开没开"
            "backend": self.backend(),
            "subprocess_alive": alive,
            # 子进程日志路径：navigate 拿不到租约、服务拉不起来时让人"去哪儿
            # 看诊断"（LEGACY gpu_arbiter.py:273-283/ obsidian-rag/server.py:965
            # 明确把 data/wemm_server.log 指出来）
            "log_file": self.log_file(),
            # 渲染 DPI 与看图服务模型/设备（旧 wemm_status 的 DPI/model/device
            # 字段；model/device 来自子进程 /health 快照，服务未运行时为 None）
            "dpi": WEMM_RENDER_DPI,
            "service": (
                {"model": service.get("model"), "device": service.get("device"),
                 "dim": service.get("dim"),
                 # 模型此刻是否在显存里（服务存活 ≠ 模型常驻：空闲会自动卸载）——
                 # GUI 全局快照的"看图模型常驻/空闲已卸载"就取这一位
                 "loaded": bool(service.get("loaded"))}
                if isinstance(service, dict) else None
            ),
            "libraries": libraries,
            # 2026-09-29 新增：把"显存被挡下"这件事和两个数字如实报出去。
            # GUI 快照据此在进度条上写清「需要 X GB，当前 Y GB」并提示可去设置里
            # 开强制加载；诊断页也能直接看到，不用去翻 wemm_server.log。
            # `vram_blocked` 为 None = 本轮没被挡（不等于"够用"，够不够服务端才知道）。
            "vram": {
                "required_gb": WEMM_MIN_VRAM_GB,
                "blocked": self._vram_blocked,
                "force_load": self.force_load_enabled(),
            },
        }

    @staticmethod
    def _score_pages_directly(collection, page_ids: list[str], query: list[float], merged: dict) -> None:
        """按 id 取出这些页的向量，用余弦相似度打分（与页库 `hnsw:space=cosine` 的
        `1 - distance` 同一量纲），把更高的分数并进 `merged`。"""
        try:
            got = collection.get(ids=page_ids, include=["embeddings", "metadatas"])
        except Exception:  # noqa: BLE001 - 这个 segment 里没有这些页：跳过即可
            return
        embeddings = got.get("embeddings")
        if embeddings is None:  # 注意：Chroma 返回 numpy 数组，不能写 `or []`（§9）
            embeddings = []
        metadatas = got.get("metadatas")
        if metadatas is None:
            metadatas = []
        query_norm = sum(float(value) * float(value) for value in query) ** 0.5 or 1.0
        for page_id, vector, meta in zip(got.get("ids", []), embeddings, metadatas):
            values = [float(value) for value in vector]
            norm = sum(value * value for value in values) ** 0.5 or 1.0
            score = sum(a * float(b) for a, b in zip(values, query)) / (norm * query_norm)
            previous = merged.get(page_id)
            if previous is None or score > previous[1]:
                merged[page_id] = (meta or {}, score)

    def cache_info(self) -> dict:
        """页库存在哪、每页大约占多少、建页库/试搜要多少显存、闲多久自动卸载——给“转换缓存”
        清单（BC-19）用。只读，不拉起服务、不加载模型。"""
        return {
            "dir": str(self._storage_root) if self._storage_root is not None else None,
            "bytes_per_page": _BYTES_PER_PAGE_ESTIMATE,
            "vram_gb": WEMM_MIN_VRAM_GB,
            "idle_unload_seconds": WEMM_UNLOAD_AFTER_SECONDS,
        }

    def render_page_png(self, pdf_path: Path, page: int, max_side: int = 720) -> bytes:
        """把 PDF 第 `page` 页（从 1 起）画成一张 PNG（最长边不超过 `max_side` 像素）。

        给“看页库这一页长什么样”用（BC-19）：只用 CPU 画这一张，画完关文档，不存盘、不缓存、
        不碰显卡，也不需要看图服务在跑。页码越界、文件打不开都抛 ValueError（原因只含类型）。"""
        import pymupdf

        side = max(64, min(int(max_side), _PREVIEW_MAX_SIDE))
        try:
            document = pymupdf.open(str(pdf_path))
        except Exception as exc:  # noqa: BLE001 - 文件打不开：只报类型，不透传原文
            raise ValueError(f"PDF 打不开（{type(exc).__name__}）") from exc
        try:
            total = int(document.page_count)
            if not 1 <= int(page) <= total:
                raise ValueError(f"没有第 {page} 页（这份 PDF 一共 {total} 页）")
            pdf_page = document.load_page(int(page) - 1)
            longest = max(pdf_page.rect.width, pdf_page.rect.height) or 1.0
            zoom = side / float(longest)
            return pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
        finally:
            try:
                document.close()
            except Exception:  # noqa: BLE001 - 收尾失败不能盖掉已经画好的图（§5）
                pass

    def _health_snapshot(self) -> dict | None:
        """只读 GET 子进程 /health（模型/设备快照）；任何失败返回 None——
        状态诊断绝不因快照失败而失败，同旧 wemm_retriever.health 的容错
        语义（超时 5s）。"""
        if self._handle is None:
            return None
        try:
            import json as _json
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{self._handle.port}/health", timeout=5.0
            ) as response:
                data = _json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            return None

    # ---- 查询态 ----------------------------------------------------------

    def navigate(self, library_id: str, query: str, top_k: int = 5, path: str | None = None) -> list[PageHit]:
        """页级导航。返回**普通空列表**只意味着一件事："查了，但这一页确实
        没有匹配"。任何"这次没能查"的情况（WEMM 后端没开、GPU 租约被别人
        占着且服务没活着、服务拉不起来/请求没打通）都抛
        `VisualVetoError`（缺陷 C）——调用方据此告诉用户"稍后重试"而不是
        谎报"没找到"。"""
        if not self._enabled:
            # 插件本身没启用（用户没装/没启用这个插件）——同 official-mcp-
            # server 里"未装该插件时 navigate_knowledge 返回空结果不是失败"
            # 的承诺，保持原样返回空列表。
            return []
        self._raise_if_backend_off()
        generation = self._generations.active(library_id) if self._generations is not None else None
        state = self._read_state(library_id, generation or "legacy")
        files = state.get("files", {})
        if not isinstance(files, dict):
            return []
        active_ids = {
            page_id
            for file_path, record in files.items()
            if isinstance(record, dict) and record.get("status") in {"indexed", "partial"}
            # `path` 给了就只在这一份 PDF 的页里找（“转换缓存”清单里的试搜，BC-19）
            and (path is None or file_path == path)
            for page_id in record.get("page_ids", [])
        }
        segments = [str(value) for value in state.get("segments", []) if value]
        if not active_ids or not segments:
            # 这个库（这份 PDF）没有页可查：不为它拉起看图服务（BC-11，用到时才开）
            return []
        self._ensure_query_service()
        try:
            result = self._handle.call("embed", {"kind": "text", "content": query, "dim": WEMM_DIM}, timeout=60.0)
        except SubprocessServiceError as exc:
            self._logger.warning("WEMM查询编码失败：%s", exc)
            raise VisualVetoError(
                _VETO_CALL_FAILED, f"（WEMM 看图服务不可用：{type(exc).__name__}，可稍后重试）"
            ) from exc
        if not result.get("ok"):
            self._logger.warning("WEMM查询编码失败：%s", result.get("error"))
            raise VisualVetoError(
                _VETO_CALL_FAILED, f"（WEMM 查询编码失败：{result.get('error')}）"
            )
        merged: dict[str, tuple[dict, float]] = {}
        for segment in segments:
            try:
                segment_name = None if segment == "legacy" else segment
                collection = self._client.get_collection(name=_collection_name(library_id, segment_name))
            except Exception:
                continue
            if path is not None:
                # 只看一份 PDF：它最多几百页，直接按 id 取出这几页的向量现算相似度，不走带过滤
                # 条件的近邻检索（那条路在匹配数很少时容易报错或凑不满结果）。
                self._score_pages_directly(collection, sorted(active_ids), result["embedding"], merged)
                continue
            count = collection.count()
            request = min(count, max(top_k * 4, 32))
            while request > 0:
                hits = collection.query(
                    query_embeddings=[result["embedding"]],
                    n_results=min(request, count),
                    include=["metadatas", "distances"],
                )
                metadatas = hits.get("metadatas") or [[]]
                distances = hits.get("distances") or [[]]
                current_count = 0
                for page_id, meta, distance in zip(hits.get("ids", [[]])[0], metadatas[0], distances[0]):
                    if page_id not in active_ids:
                        continue
                    current_count += 1
                    score = 1.0 - float(distance)
                    previous = merged.get(page_id)
                    if previous is None or score > previous[1]:
                        merged[page_id] = (meta or {}, score)
                if current_count >= top_k or request >= count:
                    break
                request = min(count, max(request * 2, top_k + 1))
        return [
            PageHit(
                library_id=library_id,
                path=str(meta.get("path", "")),
                abs_path=str(meta.get("abs_path", "")),
                page_index=int(meta.get("page", 0)),
                score=round(score, 4),
            )
            for _, (meta, score) in sorted(merged.items(), key=lambda item: item[1][1], reverse=True)[:top_k]
        ]
