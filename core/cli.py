"""命令行入口：插件管理器 + 业务命令。

插件管理器部分（scan/status/load/enable/disable/unload）是 Phase 0 的
原始入口；业务命令（index/libraries/export/import/dedup）对齐旧项目的
CLI 能力（index.py:2441 / library.py:700 / export.py:258 / import.py:212 /
dedup.py:235 的命令行入口），全部是对 Pipeline 编排层的薄封装——不自己实现
任何业务顺序（架构红线：GUI/MCP/CLI 必须调用同一业务服务层）。

**三条 2026-09-27 补的纪律**（都来自对旧项目实际代码的逐条比对）：

1. **默认路径与 CWD 无关**：三个默认路径都经 `core/paths.py` 解析（锚定到
   仓库根 / `RAG_REDO_DATA_ROOT` / 打包后的 `%LOCALAPPDATA%`），与
   `gui_main.py` / `mcp_stdio.py` 同源。旧项目 `obsidian-rag/config.py:14`
   `DATA_DIR = Path(__file__).parent / "data"` 就是 CWD 无关的。
2. **索引与 GUI/MCP 的 worker 互斥**：`index` 复用 worker 那把按库文件锁
   （`core/index_progress.py` 的 `FileByteLock`），拿不到就排队等待、超时给
   一行可读错误——对齐 `obsidian-rag/index.py:1702-1730 write_lock()` 与
   `:2480-2481`（`LockBusyError` 直接终止整轮）。
3. **CLI 侧单例守卫**：只给会写共享数据的命令持 `data/cli.pid`（见
   `_needs_cli_guard`），只读诊断命令保持可并发。守卫的两种失败严格分开——
   抢不到锁=**确证**有别的 CLI 在跑（拦，一行中文）；守卫自己坏了
   （`core/singleton.py` 的 fail-open）=放行 + 一行告警，不能让数据目录只读
   这类环境问题把 CLI 变成"永远起不来"。

用法示例：
    python -m core.cli scan
    python -m core.cli libraries add D:\\notes --name 我的笔记
    python -m core.cli libraries list
    python -m core.cli index --library 我的笔记 --full
    python -m core.cli index --library 我的笔记 --fresh-extract
    python -m core.cli export --library 我的笔记 --out backup.zip
    python -m core.cli import backup.zip --root D:\\restored
    python -m core.cli dedup --library 我的笔记
"""
from __future__ import annotations

import argparse
import inspect
import os
import sys
import time
from pathlib import Path

from . import paths
from .runtime import PluginRuntime
from .singleton import FileByteLock, ProcessSingletonGuard

# 业务命令能用到的全部官方插件（同 mcp_stdio.py / gui_main.py 的 REQUIRED_PLUGINS 思路；
# 缺失的插件降级为警告而不是致命错误——CLI 仍可跑纯插件管理命令）。**顺序与 GUI/MCP 的
# 清单去掉各自的门面插件（GUI 壳 / MCP 服务）后完全一致**——三个入口启用的业务插件必须是
# 同一份，`tests/test_cli.py` 钉住。此前 CLI 少了 `official-visual-wemm`：同一个库用 CLI
# 建索引时静默跳过页级视觉阶段，产出的派生数据与 GUI/MCP 触发的索引不一样，且没有提示。
BUSINESS_PLUGINS = [
    "official-extractor-text",
    "official-extractor-pdf-text",
    "official-extractor-docx",
    "official-chunker",
    "official-library-manager",
    "official-lexical-bm25",
    "official-embedder-bge-m3",
    "official-vector-store-chroma",
    "official-fusion-rrf",
    "official-reranker",
    "official-import-export",
    "official-visual-wemm",
    "official-ocr-mineru-cloud",
    "official-ocr-mineru-local",
    "official-dedup",
    "official-library-summary",
    "official-llm-openai-compatible",
    "official-query-enhancer-hyde",
    "official-result-advisor",
]

#: 页级视觉插件的 `on_enable` 会**立刻**抢 GPU 租约并拉起看图子进程（`wemm_backend`
#: 默认 on）。只有真会读写页级视觉数据的命令才该为它付这个代价：`index` 要建页库，
#: `export`/`import` 要带上/恢复页级视觉状态；`libraries` 增删列表、`dedup` 纯文本比对
#: 与它无关，启用了只是白白拉起一个 GPU 服务。
_VISUAL_PLUGIN_IDS = frozenset({"official-visual-wemm"})
_VISUAL_COMMANDS = frozenset({"index", "export", "import"})

#: 不需要页级视觉的命令（`libraries` / `dedup`）启用的插件集。
REQUIRED_PLUGINS = [p for p in BUSINESS_PLUGINS if p not in _VISUAL_PLUGIN_IDS]

_BUSINESS_COMMANDS = {"index", "libraries", "export", "import", "dedup"}


def _plugins_for(command: str) -> list[str]:
    """某个业务命令需要启用的插件集（保持 `BUSINESS_PLUGINS` 的顺序）。"""
    if command in _VISUAL_COMMANDS:
        return list(BUSINESS_PLUGINS)
    return list(REQUIRED_PLUGINS)

#: CLI 单例守卫的 PID 文件名——与 `gui.pid`（GUI）/ `server.pid`（MCP）分开，
#: 三者各管各的实例类型，但都锚在同一个 DATA_ROOT 上。
CLI_PID_FILE = "cli.pid"

#: 按库索引锁的等待参数，逐字对齐旧项目 `obsidian-rag/config.py:42-43`
#: （lock_timeout_seconds=60 / lock_poll_seconds=0.5，
#: `obsidian-rag/index.py:43-44` 读进 LOCK_TIMEOUT_SECONDS/LOCK_POLL_SECONDS）。
LIBRARY_LOCK_TIMEOUT_S = 60.0
LIBRARY_LOCK_POLL_S = 0.5


class LibraryLockBusy(Exception):
    """拿不到某个库的索引锁——对应旧项目 `obsidian-rag/index.py:1695`
    `LockBusyError`，由调用方转成一行可读错误（不是 Python traceback）。"""


class _CliGuardBusy(Exception):
    """另一个 CLI 实例正持着单例锁（消息已打印）。"""


def _print_status(runtime: PluginRuntime) -> None:
    if not runtime.plugins:
        print("(没有发现任何插件)")
        return
    for plugin_id, plugin in sorted(runtime.plugins.items()):
        line = f"{plugin_id:30s} {plugin.state.value}"
        if plugin.error:
            line += f"  原因: {plugin.error}"
        print(line)
    conflicts = runtime.registry.conflicts()
    if conflicts:
        print("\n冲突的单例扩展点（需要显式指定当前用哪个，不会自动选）：")
        for point, providers in conflicts.items():
            print(f"  {point}: {providers}")


def _boot_pipeline(args: argparse.Namespace):
    """业务命令的启动器：扫描并启用官方插件集，构造 Pipeline。"""
    runtime = PluginRuntime(args.plugins_dir, state_file=args.state_file, data_dir=args.data_root)
    runtime.scan()
    for plugin_id in _plugins_for(args.command):
        if plugin_id not in runtime.plugins:
            print(f"警告：插件 {plugin_id} 未发现，相关能力会缺失", file=sys.stderr)
            continue
        runtime.load(plugin_id)
        runtime.enable(plugin_id)
        state = runtime.plugins[plugin_id]
        if state.state.value == "failed":
            print(f"警告：插件 {plugin_id} 启用失败: {state.error}", file=sys.stderr)
    from .pipeline import Pipeline

    return runtime, Pipeline(runtime)


def _singleton(pipeline, point: str):
    plugin_id = pipeline.runtime.registry.active_of(point)
    if plugin_id is None:
        raise SystemExit(f"错误：没有已启用的 {point} 插件，无法执行该命令")
    return pipeline._plugin(plugin_id)


# ---- CLI 单例守卫（只给会写共享数据的命令）--------------------------------------


def _lock_holder_pid(path: Path) -> int | None:
    """锁文件里记录的持有者 PID（纯诊断用，读不到就当没有——诊断信息缺失不该
    变成拒绝服务的理由）。"""
    try:
        raw = path.read_bytes()[:64].strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _guard_holds_lock(guard: ProcessSingletonGuard) -> bool:
    """这个 guard 现在**真的**持着字节锁吗？

    `ProcessSingletonGuard.acquire()` 的 True 有两种含义（见 core/singleton.py
    的 fail-open 契约）："成功抢到锁"和"守卫自己坏了、放行"。所以判断
    "我是否被保护"**不能**看返回值，只能看底层锁句柄在不在
    （`guard._lock._f`）——AGENTS.md §7"所有权未知不能宣称资源空闲"。
    """
    return getattr(getattr(guard, "_lock", None), "_f", None) is not None


def _needs_cli_guard(args: argparse.Namespace) -> bool:
    """哪些子命令需要 CLI 单例守卫——判据是"会不会写共享数据"：

    - `index`/`export`/`import`：写库的 Chroma 段/索引清单/注册表，或至少与
      GUI/MCP 争同一批文件，必须互斥；
    - `libraries add/remove`：改的是 GUI 也在改的同一个注册表；
    - `dedup`、`libraries list`：纯只读；
    - `scan/status/load/enable/disable/unload`：只碰 `plugins_state.json`
      这一份小状态（原子写），旧项目的 CLI 同样没有守卫（`library.py:699`
      起、`index.py:2440` 起都只有 write_lock），保持可并发——诊断命令在
      一次长时间索引期间必须还能跑。
    """
    if args.command in ("index", "export", "import"):
        return True
    if args.command == "libraries":
        return getattr(args, "op", "") in ("add", "remove")
    return False


def _acquire_cli_guard(args: argparse.Namespace) -> ProcessSingletonGuard | None:
    """需要守卫的命令在这里拿 `data/cli.pid`；不需要返回 None。"""
    if not _needs_cli_guard(args):
        return None
    pid_file = Path(args.data_root) / CLI_PID_FILE
    guard = ProcessSingletonGuard(pid_file)
    if not guard.acquire():
        # 走到这里只有一种可能：守卫**确证**抢不到锁，也就是确实有另一个 CLI
        # 实例正持着它（`core/singleton.py` 里"守卫自己坏了"是 fail-open 放行、
        # 根本走不到这个分支）。所以这里必须拦，且必须让用户一眼看出拦他的
        # 是单例守卫而不是"库坏了"——这是他唯一能看到的诊断。
        raise _CliGuardBusy(
            f"错误：CLI 单例守卫拒绝本次命令——已有另一个 rag-redo 命令在运行"
            f"（{pid_file}），本次命令已中止：并发写同一份数据会损坏索引。"
            "等前一个命令结束（或确认它已死）后重试。"
        )
    if not _guard_holds_lock(guard):
        # 守卫 fail-open：命令照跑（对齐 obsidian-rag/singleton.py:96-98），
        # 但必须说清"没有互斥保护"，不能让用户以为有。
        print(
            f"注意：CLI 单例守卫没有拿到锁（{pid_file} 不可用，见上一行告警），"
            "本次运行没有 CLI 之间的互斥保护。",
            file=sys.stderr,
        )
    return guard


# ---- 按库索引锁（与 GUI/MCP 的 worker 同一把）---------------------------------


def _library_lock_path(data_root: Path, library_id: str) -> Path:
    """worker 侧用的库锁文件路径（`core/index_progress.py:268`）——**直接复用
    它的命名规则**（`_status_key`），不在 CLI 里抄第三份文件名算法，否则两边
    一旦漂移就等于没有互斥。"""
    from .index_progress import _status_key

    return Path(data_root) / "index_progress" / "locks" / f"{_status_key(library_id)}.lock"


def _acquire_library_lock(
    data_root: Path, library_id: str, *, timeout: float | None = None, poll: float | None = None
) -> FileByteLock:
    """拿按库索引锁；拿不到就轮询等待，超时抛 `LibraryLockBusy`。

    **为什么 CLI 必须自己拿这把锁**：`core/pipeline.py::index_library` 是同步
    路径，本身不加锁——只有 `start_index_library`（后台 worker）才落
    `core/index_progress.py:268` 那把锁。不加这一层，`cli.py index` 与 GUI/MCP
    启动的 worker 会同时写同一个库的 Chroma 段/索引清单，全程无互斥。

    **为什么同步跑而不改走 worker**：旧项目的 CLI 索引是**前台同步**的
    （`obsidian-rag/index.py:2473-2484` 逐库跑完并打印结果、失败计数决定退出
    码），换成"立即返回 + 轮询进度"会改掉用户可见语义（输出形态、退出码
    含义、"命令跑完才结束"这个预期）。所以保留同步执行，只补上互斥。

    **不会自己锁自己**（上一版注释写错了，这里更正）：那把锁**只有 worker 子
    进程会取**（`core/index_progress.py:268`，全仓库唯一取它的写点），而
    `index_library` 是纯同步调用、内部既不起 worker 也不碰
    `index_progress/locks/`（`core/resource_arbiter.py` 走的是
    `data/resource_locks/`，两者互不相干）。也就是说 CLI 拿锁与 worker 拿锁
    永远发生在**不同进程**，正是互斥要的效果。真正会自己锁自己的只有
    "同一进程再 new 一个 FileByteLock 取同一把锁"，而那只会让 `acquire()`
    立刻返回 False（不会阻塞），最坏情况是干净失败。

    **死锁自愈为什么不需要抄**：旧项目 `index.py:1681-1693` 超时后会检查持有者
    PID 是否已死并清空锁文件重试；这里超时即意味着持有者**活着**——字节锁随
    进程死亡由操作系统释放，我们抢不到就说明确实有人在跑。PID 只用于把诊断
    信息写进错误消息（"疑似持有者"，措辞与旧项目一致）。
    """
    # timeout/poll 在**调用时**读模块常量，而不是写成默认参数值：默认参数在
    # 函数定义时就绑定了，打在模块常量上的覆盖（测试、未来的配置项）会静默
    # 失效——那正是改造前的 bug：测试把 LIBRARY_LOCK_TIMEOUT_S 打成 0.05s，
    # 实际仍然死等 60s，只表现为"这个用例慢得离谱"，没有任何报错。
    if timeout is None:
        timeout = LIBRARY_LOCK_TIMEOUT_S
    if poll is None:
        poll = LIBRARY_LOCK_POLL_S
    path = _library_lock_path(data_root, library_id)
    lock = FileByteLock(path)
    deadline = time.monotonic() + timeout
    announced = False
    while True:
        try:
            acquired = lock.acquire()
        except OSError as exc:
            raise LibraryLockBusy(f"无法获取库锁 {path}：{type(exc).__name__}: {exc}") from None
        if acquired:
            try:
                # 写自己的 PID，让并发方的错误消息能指出是谁在跑
                lock.write(str(os.getpid()).encode("ascii"))
            except OSError:
                pass
            return lock
        if not announced:
            announced = True
            print(
                f"库「{library_id}」正被另一个索引进程占用，等待索引锁（最多 {timeout:g} 秒）…",
                file=sys.stderr,
            )
        if time.monotonic() >= deadline:
            holder = _lock_holder_pid(path)
            who = f"（疑似持有者 PID {holder}）" if holder else ""
            raise LibraryLockBusy(
                f"库「{library_id}」的索引锁等待超时（>{timeout:g} 秒）{who}："
                "可能仍有其他索引进程/残留实例在运行。请用 index_status 查看进度"
                "（MCP 工具或 GUI 索引面板），或结束残留进程后重试。"
            )
        time.sleep(poll)


def _fresh_extract_supported(pipeline) -> bool:
    """`pipeline.index_library` 现在收不收 `fresh_extract` 关键字？

    用 `inspect.signature` 判定而不是 `try/except TypeError`：TypeError 既可能
    是"参数不认"，也可能是索引跑起来之后内部某个真实 bug 抛的，后者会被误
    判成"不支持"然后把一个真错误报成 CLI 的旗标问题（`obsidian-rag/index.py:
    2450-2455` 的 `--fresh-extract` 是先清缓存再索引，REDO 侧契约是
    `index_library(..., fresh_extract=...)` 参数，两者由 pipeline 那个代理落地）。
    """
    try:
        parameters = inspect.signature(pipeline.index_library).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return "fresh_extract" in parameters


def _exc_message(exc: BaseException) -> str:
    """`str(KeyError("未知库: x"))` 会带上一层引号（`'未知库: x'`），终端上像
    乱码；取 `args[0]` 才是抛出方真正写的消息。"""
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def _close_runtime(runtime) -> None:
    """命令结束时收口整个运行时：卸载插件、终止它们拉起的子进程、归还 GPU 租约。

    CLI 是"跑一条命令就退出"的短进程。插件拉起的子进程（页级视觉的看图服务等）不会
    自己跟着父进程走——Windows 上父进程退出不会带走子进程——所以不在这里收口，就是每条
    命令泄漏一对孤儿进程（CLAUDE.md §4.3：子进程必须能被停止、等待、回收，不能留下
    端口、进程树或锁残留）。收口失败绝不能改写命令本身的退出码。"""
    close = getattr(runtime, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _run_business(args: argparse.Namespace) -> int:
    runtime, pipeline = _boot_pipeline(args)
    try:
        return _dispatch_business(args, pipeline)
    finally:
        _close_runtime(runtime)


def _dispatch_business(args: argparse.Namespace, pipeline) -> int:
    lib_mgr = _singleton(pipeline, "library_manager")

    if args.command == "libraries":
        if args.op == "add":
            root = str(Path(args.path).resolve())
            name = args.name or Path(root).name
            library_id = args.id or name
            lib_mgr.store.add_library(library_id, name, root)
            print(f"已注册库：{library_id}（{name}）→ {root}")
            return 0
        if args.op == "remove":
            cfg = lib_mgr.store.get(args.library_id)
            if cfg is None:
                print(f"错误：未知库 {args.library_id}", file=sys.stderr)
                return 1
            lib_mgr.store.remove_library(args.library_id)
            print(f"已注销库：{args.library_id}（注册表移除；索引数据保留，重新注册即可恢复）")
            return 0
        for cfg in lib_mgr.store.list_libraries():
            print(f"{cfg.library_id}\t{cfg.name}\t{cfg.root_path}")
        return 0

    if args.command == "index":
        if lib_mgr.store.get(args.library) is None:
            # 库不存在时先干净报错，别为一个不存在的库去建锁文件
            print(f"错误：未知库 {args.library}（库列表见 libraries list）", file=sys.stderr)
            return 1
        supported = _fresh_extract_supported(pipeline)
        if args.fresh_extract and not supported:
            # 顺序纪律：旗标校验在**取库锁之前**——用户要的命令我们做不了时，
            # 绝不能先占住库锁（那会把并发方的 worker/另一个 CLI 挡在门外）
            # 再去告诉他"这个旗标不支持"。静默忽略旗标同样不行：用户会以为
            # 提取缓存被清了，实际没有。
            print(
                "错误：当前 core/pipeline.py 的 index_library 还不支持 fresh_extract"
                "（--fresh-extract 需要索引层先接上这个参数），本次未执行索引。",
                file=sys.stderr,
            )
            return 1
        try:
            lock = _acquire_library_lock(args.data_root, args.library)
        except LibraryLockBusy as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        try:
            # 接线完成就把旗标如实透传下去（**含 False**）。改造前这里写的是
            # `{"fresh_extract": True} if (args.fresh_extract or supported) else {}`
            # ——只要 pipeline 支持这个参数就硬塞 True，于是"用户没传
            # --fresh-extract"也会被索引层当成强制重解析：`core/pipeline.py`
            # 里 `force_extract = fresh_extract` 会连带清掉提取缓存正文，
            # 白烧 MinerU/OCR 配额且结果与用户意图相反（--fresh-extract 的
            # 默认必须是关的，见 obsidian-rag/index.py:2445-2447）。
            extra = {"fresh_extract": args.fresh_extract} if supported else {}
            report = pipeline.index_library(args.library, full=args.full, **extra)
        finally:
            # 无论成功失败都释放（AGENTS.md §7：文件锁必须有异常收口路径）
            lock.release()
        print(f"索引完成：{args.library}")
        print(
            f"  新增 {report.added} / 变更 {report.changed} / 未变 {report.unchanged}"
            f" / 删除 {report.removed} / 重试 {report.retried}"
        )
        print(f"  成功 {report.succeeded} / 失败 {report.failed} / 延后 {report.deferred}")
        for file_report in report.files:
            if file_report.extract_failure:
                print(f"  ✗ {file_report.path}: {file_report.extract_failure}")
        return 0 if report.failed == 0 else 1

    if args.command == "export":
        archive = pipeline.export_library(args.library)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        from .atomic import atomic_write_bytes

        atomic_write_bytes(out, archive)
        print(f"已导出库 {args.library} → {out}（{len(archive) / 1024 / 1024:.1f} MB）")
        return 0

    if args.command == "import":
        archive_path = Path(args.archive)
        if not archive_path.is_file():
            print(f"错误：归档不存在 {archive_path}", file=sys.stderr)
            return 1
        new_id = pipeline.import_library(
            archive_path.read_bytes(), root_path=args.root, library_id=args.library_id or None
        )
        print(f"已导入为新库：{new_id}（root={args.root}）")
        return 0

    if args.command == "dedup":
        groups = pipeline.find_duplicates(args.library, threshold=args.threshold)
        clusters = [g for groups in groups.values() for g in groups]
        if not clusters:
            print("未发现近似重复。")
            return 0
        print(f"发现 {len(clusters)} 组近似重复：")
        for group in clusters:
            print(f"  · {'  ≈  '.join(group)}")
        return 0

    return 0


def _build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。三个路径参数的默认值一律走 `core/paths.py`——与
    `gui_main.py` / `mcp_stdio.py` 同源、且**与当前工作目录无关**（旧项目
    `obsidian-rag/config.py:14` 同样是 CWD 无关；改造前这里写的是相对路径
    `plugins` / `data/plugins_state.json` / `data`，于是换个目录跑 CLI 就会
    静默用上另一份甚至空的数据）。显式传参时仍按 CWD 解析——用户手敲
    `--data-root ./x` 的直觉就该是相对当前目录。"""
    data_root = paths.data_root()
    parser = argparse.ArgumentParser(prog="rag-redo")
    parser.add_argument(
        "--plugins-dir",
        type=Path,
        default=paths.plugins_dir(),
        help=f"插件目录（默认 {paths.plugins_dir()}）",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=paths.plugins_state_file(data_root),
        help=f"插件启用状态文件（默认 {data_root / paths.PLUGINS_STATE_FILE}）",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=data_root,
        help=(
            f"应用数据目录（默认 {data_root}；环境变量 {paths.DATA_ROOT_ENV} 优先，"
            "打包安装态为 %%LOCALAPPDATA%%\\RAG-Redo\\data）"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan")
    sub.add_parser("status")
    for name in ("load", "enable", "disable", "unload"):
        p = sub.add_parser(name)
        p.add_argument("plugin_id")

    # ---- 业务命令（对齐旧项目 CLI）----
    p_index = sub.add_parser("index", help="对指定库执行一次同步索引")
    p_index.add_argument("--library", required=True, help="库 id（库列表见 libraries list）")
    p_index.add_argument("--full", action="store_true", help="完整重建（忽略增量清单）")
    p_index.add_argument(
        "--fresh-extract",
        action="store_true",
        help="忽略提取缓存强制重新提取（与 --full 独立：--full 仍复用提取缓存，"
        "对齐 obsidian-rag/index.py:2445-2447）",
    )

    p_lib = sub.add_parser("libraries", help="库注册表管理（对齐旧 library.py CLI）")
    lib_sub = p_lib.add_subparsers(dest="op", required=True)
    lib_sub.add_parser("list", help="列出全部库")
    p_add = lib_sub.add_parser("add", help="注册新库")
    p_add.add_argument("path", help="笔记文件夹路径")
    p_add.add_argument("--name", default=None, help="库名（默认取文件夹名）")
    p_add.add_argument("--id", default=None, help="库 id（默认取库名）")
    p_rm = lib_sub.add_parser("remove", help="注销库（仅移出注册表，数据保留）")
    p_rm.add_argument("library_id")

    p_export = sub.add_parser("export", help="导出库为可移植归档（对齐旧 export.py CLI）")
    p_export.add_argument("--library", required=True)
    p_export.add_argument("--out", required=True, help="输出 zip 路径")

    p_import = sub.add_parser("import", help="从归档导入为新库（对齐旧 import.py CLI）")
    p_import.add_argument("archive", help="导出归档路径")
    p_import.add_argument("--root", required=True, help="新库的笔记目录")
    p_import.add_argument("--library-id", default="", help="新库 id（缺省取归档内记录）")

    p_dedup = sub.add_parser("dedup", help="近似重复检测（只读建议，对齐旧 dedup.py CLI）")
    p_dedup.add_argument("--library", required=True)
    p_dedup.add_argument("--threshold", type=float, default=0.8, help="相似度阈值（默认 0.8）")

    return parser


def _exit_code(exc: SystemExit) -> int:
    """`SystemExit` 的收口——`raise SystemExit("错误：…")` 是本文件内部的错误
    约定（见 `_singleton`），旧项目 CLI 同样是一行中文 + `sys.exit(1)`
    （`obsidian-rag/library.py:770-772`）。改造前这里写的是
    `int(exc.code or 1)`，遇到字符串 code 会再抛一次 ValueError，用户看到
    的是两层 traceback。"""
    if exc.code is None:
        return 1
    if isinstance(exc.code, int):
        return exc.code
    print(str(exc.code), file=sys.stderr)
    return 1


def _run_plugin_commands(args: argparse.Namespace) -> int:
    runtime = PluginRuntime(args.plugins_dir, state_file=args.state_file, data_dir=args.data_root)
    runtime.scan()

    if args.command in ("scan", "status"):
        _print_status(runtime)
        return 0

    if args.command == "load":
        runtime.load(args.plugin_id)
    elif args.command == "enable":
        runtime.load(args.plugin_id)
        runtime.enable(args.plugin_id)
    elif args.command == "disable":
        runtime.disable(args.plugin_id)
    elif args.command == "unload":
        runtime.unload(args.plugin_id)

    _print_status(runtime)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        guard = _acquire_cli_guard(args)
    except _CliGuardBusy as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        if _needs_cli_guard(args):
            # 路径问题永远是 CLI 最容易被误判成"库坏了"的原因，直接打一行
            print(f"数据目录：{args.data_root}", file=sys.stderr)
        if args.command in _BUSINESS_COMMANDS:
            try:
                return _run_business(args)
            except SystemExit as exc:
                return _exit_code(exc)
            except (ValueError, RuntimeError) as exc:
                # 旧项目 CLI 就是这两种错误走一行中文（library.py:770-772）
                print(f"错误：{_exc_message(exc)}", file=sys.stderr)
                return 1
            except KeyError as exc:
                print(f"错误：{_exc_message(exc)}", file=sys.stderr)
                return 1
            except Exception as exc:  # noqa: BLE001 - CLI 顶层收口，打印而非堆栈崩溃
                print(f"错误：{type(exc).__name__}: {_exc_message(exc)}", file=sys.stderr)
                return 1
        return _run_plugin_commands(args)
    finally:
        # 必须释放：异常/早退路径上把锁攥到进程结束，会让同一进程里后续的
        # CLI 调用永远撞上"另一个实例在跑"。
        if guard is not None:
            guard.release()


if __name__ == "__main__":
    sys.exit(main())
