"""GUI 契约桥：obsidian-rag `guiweb/contracts.md` v1 冻结的 **37 个方法**。

前端是逐字节复刻的旧前端，它只认这 37 个方法名、参数顺序和返回值的键集合
（`tests/fixtures/legacy_guiweb_contract.json` 冻结，`test_contracts_parity.py` 对
每个方法逐个真实调用并断言键集合）。本文件只做**翻译**：把一次前端调用转成
core / 插件的一次调用，把返回值整形成契约约定的形状。排序、过滤、置信度、勾选裁决
一律来自 core——这里没有任何第二份业务判断（`test_assets.py` 守着前端零业务逻辑）。

**一个方法名只能定义在这里**。`Api`（`api.py`）只放契约之外的"精确 API"（库 id 在前、
供 MCP/CLI/脚本/测试用）。此前 `Api` 里有 6 个与本类同名的方法，子类同名方法遮蔽
mixin，导致契约版整段是死代码、前端拿到缺键的返回值；`test_contracts_parity.py`
现在断言 `Api` 自己不得重定义任何契约方法名。

**库身份**：旧项目里库的"名字"就是库的身份，前端拿到什么名字就用什么名字回传。
rag-redo 把身份拆成了 `library_id` 与显示名 `name`，所以：**对前端输出一律用显示名，
从前端接收一律"先按 library_id、再按唯一的显示名"解析**（`_resolve_library`）。
未知库明确报错，不假装成功。

**参数名与顺序都是契约的一部分**：pywebview 按位置/名把 JS 调用映射到 Python 形参。
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import queue as _queue
import threading
import time
from pathlib import Path
from typing import Any

from core import paths as core_paths
from core.atomic import atomic_write_bytes
from core.gpu_arbiter import probe_card
from core.index_progress import INDEX_LOG_NAME
from official_gui_shell.md_render import md_to_html
from official_gui_shell.preview_job import preview_job
from official_gui_shell.settings_schema import (
    FIELDS,
    GROUPS,
    coerce_setting,
    format_setting_value,
    setting_is_secret,
)

_LOG = logging.getLogger("rag_redo.plugin.official-gui-shell")

#: 导入确认词，逐字比对（guiweb/contracts.md 导入/导出门禁）。
IMPORT_CONFIRM_TEXT = "我确认导入"
#: 提取试验台的硬超时（旧 bridge.py::preview_poll 的 180 秒）。
PREVIEW_TIMEOUT_S = 180.0
#: 正文查看的截断上限（旧 store.DOC_VIEW_MAX_CHARS）。
DOC_VIEW_MAX_CHARS = 200_000

#: 各类缓存的存活时间（秒）。前端每秒推一次快照，freshness 要扫盘、vault 文件数要
#: 遍历全库，不能每秒都跑（旧项目对 vault 文件数同样是 30 秒缓存）。
FRESHNESS_TTL_S = 10.0
VAULT_FILES_TTL_S = 30.0
WEMM_LIVE_TTL_S = 30.0

#: 失败原因的展示顺序与文案（旧 guiweb/bridge.py::ISSUE_ORDER + gui/store.py::ISSUE_TEXT）。
ISSUE_ORDER = ["scanned", "unreadable", "extract-failed", "empty", "tbd", "unknown"]
ISSUE_TEXT = {
    "scanned": (
        "扫描件 PDF",
        "如已在设置中启用 pdf_scan_backend（云端 OCR），下一轮索引会自动重试；"
        "未启用则请到设置中开启，或改用文字层版本",
    ),
    "unreadable": ("不可读", "文件被占用/权限不足，解除后重新索引自动重试"),
    "extract-failed": ("提取失败", "文件可能损坏或加密，修复源文件后重建"),
    "empty": ("空文件", "无正文内容，补全内容后自动入索引"),
    "tbd": ("TBD 占位", "占位符过多暂不索引，补全后自动恢复"),
}

#: 进度阶段：rag-redo 的 core 阶段名 → 旧前端认识的阶段名
#: （前端 stepper：scanning/converting/embedding/writing，wemm 只做阶段名映射）。
_PHASE_MAP = {
    "starting": "scanning",
    "scanning": "scanning",
    "extracting": "converting",
    "file_complete": "converting",
    "embedding": "embedding",
    "writing": "writing",
    "visual": "wemm",
    "finalizing": "writing",
}

#: 预览/正文的"通道"名：rag-redo 记的是插件 id，旧前端的 `ROUTE_NAME` 认旧词汇。
_ROUTE_MAP = {
    "extractor:official-extractor-text": "local",
    "extractor:official-extractor-pdf-text": "local",
    "extractor:official-extractor-docx": "local",
    "extractor:official-ocr-mineru-cloud": "ocr:mineru-cloud",
    "extractor:official-ocr-mineru-local": "ocr:mineru-local",
    "源文件直读": "源文件",
}

_cpu_primed = False


def _exc_text(exc: BaseException) -> str:
    """异常转人话：`KeyError` 的 `str()` 会带一层引号，取它的第一个参数。"""
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def _cpu_percent() -> float | None:
    """本机 CPU 总占用（旧 store.cpu_percent）。第一次调用只是"打底"，返回 None，
    一秒后才有数（契约：`cpu` 首次为 null）。没有 psutil 一律 None（fail-open）。"""
    global _cpu_primed
    try:
        import psutil
    except ImportError:
        return None
    try:
        value = psutil.cpu_percent(interval=None)
    except Exception:  # noqa: BLE001
        return None
    if not _cpu_primed:
        _cpu_primed = True
        return None
    return float(value)


def _quote_path(text: str) -> str:
    """URL 百分号编码（保留 `/`）。不用 `urllib`：插件权限声明了 `network = false`，
    运行时会把 `urllib` 这类名字的导入当成越权能力拒绝加载。"""
    safe = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/")
    return "".join(chr(b) if b in safe else f"%{b:02X}" for b in text.encode("utf-8"))


class _LegacyContractMixin:
    """37 个契约方法的实现。`Api` 继承它，并在 `__init__` 里调用 `_init_bridge_state()`。"""

    # ==================================================================
    # 状态与通用助手
    # ==================================================================

    def _init_bridge_state(self) -> None:
        self._window: Any = None
        self._dead_alerted = False
        self._was_running = False
        self._cache_lock = threading.Lock()
        self._cache: dict[Any, tuple[float, Any]] = {}
        self._wemm_probe_lock = threading.Lock()
        self._wemm_probe_running = False
        self._log_seen_cursor = 0
        # 库简介批量刷新（旧 _sumref）：单库/批量共用同一后台任务，前端轮询
        self._sumref_lock = threading.Lock()
        self._sumref: dict[str, Any] = {
            "running": False, "total": 0, "done": 0, "current": None, "results": {},
        }
        # 提取试验台（旧 _pv_*）：独立子进程，取消 = terminate，180 秒硬超时
        self._pv_lock = threading.Lock()
        self._pv_proc: Any = None
        self._pv_queue: Any = None
        self._pv_started = 0.0
        self._pv_result: dict[str, Any] | None = None
        self._pv_done = False
        #: 子进程入口；测试可换成会睡眠的函数来验证"取消真的能停"。
        self._pv_target = preview_job

    def _cached(self, key: Any, ttl: float, compute: Any) -> Any:
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        value = compute()
        with self._cache_lock:
            self._cache[key] = (now, value)
        return value

    def _invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    # ---- 库解析 ---------------------------------------------------------

    def _libraries(self) -> list[Any]:
        return self._lib_mgr.store.list_libraries()  # type: ignore[attr-defined]

    def _resolve_library(self, name: object) -> str:
        """前端传来的库名 → `library_id`。先精确匹配 id，再匹配**唯一**的显示名；
        重名（多个库同一个显示名）与不存在都明确报错。"""
        text = "" if name is None else str(name)
        libraries = self._libraries()
        for cfg in libraries:
            if cfg.library_id == text:
                return cfg.library_id
        matches = [cfg.library_id for cfg in libraries if cfg.name == text]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"库名「{text}」对应多个库（{'、'.join(matches)}），请改用库 id")
        raise ValueError(f"库不存在：{text}")

    def _resolve_scope(self, libraries: object) -> str:
        """检索/图谱范围：逗号分隔的库名 → 逗号分隔的库 id；空串保持空串
        （交给 core 按 `default_libraries` 收窄，旧 `resolve_entries` 同语义）。"""
        text = str(libraries or "").strip()
        if not text:
            return ""
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if parts and parts[0].lower() == "all":
            return "all"
        return ",".join(self._resolve_library(p) for p in parts)

    def _name_of(self, library_id: str) -> str:
        cfg = self._lib_mgr.store.get(library_id)  # type: ignore[attr-defined]
        return cfg.name if cfg is not None else library_id

    # ---- 日志 / 推送 ------------------------------------------------------

    def _index_log_path(self) -> Path:
        return Path(self._pipeline.runtime.data_dir) / INDEX_LOG_NAME  # type: ignore[attr-defined]

    def _push(self, ev_type: str, payload: dict[str, Any]) -> None:
        """向前端推送一条事件（旧 `Bridge._push`）；窗口没绑定或已关闭 → 静默。"""
        window = self._window
        if window is None:
            return
        try:
            window.evaluate_js(
                "window.__push && window.__push(%s, %s)"
                % (json.dumps(ev_type), json.dumps(payload, ensure_ascii=False))
            )
        except Exception:  # noqa: BLE001 - 窗口已关闭等场景：推送失败静默
            pass

    def _log(self, text: str, is_error: bool = False) -> None:
        """GUI 自身动作写进与索引 worker 同一份日志（前缀 `[GUI]` 区分）。

        同时推一条 `log` 事件让面板立刻显示——但只在**能保证不重复也不丢行**时才推：
        前端的日志游标是文件字节偏移，推送若带错游标，要么和 3 秒轮询重复显示，要么
        跳过 worker 同时写入的行。所以仅当"追加前文件大小恰好等于前端已知游标、追加
        后恰好多出本行"时才推，并把游标同步成新的文件大小；其余情况留给轮询。
        """
        line = "%s [GUI]%s %s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), " ERROR" if is_error else "", text,
        )
        encoded = (line + "\n").encode("utf-8")
        try:
            path = self._index_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            before = path.stat().st_size if path.exists() else 0
            with path.open("ab") as handle:
                handle.write(encoded)
            after = path.stat().st_size
        except OSError:
            return
        if before == self._log_seen_cursor and after == before + len(encoded):
            self._log_seen_cursor = after
            self._push("log", {"lines": [line], "cursor": after})

    # ==================================================================
    # 窗口绑定 / 本地资源
    # ==================================================================

    def bind_window(self, wnd: Any) -> None:
        """`gui_main.py` 在 `create_window` 之后调用：注入窗口句柄，供 `pick_path` 的
        原生对话框和推送（`evaluate_js`）使用。对齐旧 `Bridge.bind_window`。"""
        self._window = wnd

    def get_static_path(self, name: str) -> dict[str, Any]:
        """前端要打开的本地资源绝对路径。`logs`（前端实际传 `log_dir`）→ 日志所在的
        数据目录；`root` → 程序根目录（旧版是两项固定映射）。"""
        data_dir = Path(self._pipeline.runtime.data_dir)  # type: ignore[attr-defined]
        mapping = {
            "logs": str(data_dir),
            "log_dir": str(data_dir),
            "root": str(core_paths.repo_root()),
        }
        return {"path": mapping.get(name, "")}

    def pick_path(self, mode: str = "dir", start: str = "") -> dict[str, Any]:
        """原生文件/文件夹选择弹窗。`mode`: "dir"|"file"；`start` 是输入框现值，用于
        定位起始目录；用户取消 → `{"path": None}`（不是错误）。

        对话框类型常量在 **`webview` 模块**上（`webview.OPEN_DIALOG` /
        `webview.FOLDER_DIALOG`），不在窗口对象上——此前误写成 `window.OPEN_DIALOG`，
        真机上每次点"浏览…"都是 AttributeError。"""
        window = self._window
        if window is None:
            return {"path": None, "error": "窗口未就绪"}
        try:
            import webview

            start_dir = start or ""
            if start_dir and not os.path.isdir(start_dir):
                start_dir = os.path.dirname(start_dir) or ""
            if not os.path.isdir(start_dir):
                start_dir = ""
            if mode == "file":
                result = window.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=False, directory=start_dir,
                    file_types=("文档 (*.pdf;*.docx;*.md;*.txt)", "所有文件 (*.*)"),
                )
            else:
                result = window.create_file_dialog(
                    webview.FOLDER_DIALOG, allow_multiple=False, directory=start_dir,
                )
            path = result[0] if result else None
            if path:
                self._log("选择路径 %s" % path)
            return {"path": str(path) if path else None, "error": None}
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"path": None, "error": str(exc)}

    # ==================================================================
    # 全局快照
    # ==================================================================

    def get_snapshot(self) -> dict[str, Any]:
        """全量状态快照（前端每秒收到同名推送，首次加载时也主动拉一次）。

        数据全部来自 core 的只读面：`index_stats`（文件数/块数，来自 manifest，不加载
        模型）、`index_status`（进度）、`library_freshness`（是否有变更待索引，带缓存）、
        `index_failures`（问题汇总）、`visual_status`（WEMM，带缓存）、`probe_card`
        （整卡只读）。本方法**不自己数块、不自己判断过期、不自己算进度**，只做聚合与
        契约键整形。失败返回 `{"error": ...}`，推送线程据此跳过这一秒。"""
        try:
            libraries = self._libraries()
            libs: list[dict[str, Any]] = []
            states: list[str] = []
            total_files = 0
            total_chunks = 0
            for cfg in libraries:
                stats = self._pipeline.index_stats(cfg.library_id)  # type: ignore[attr-defined]
                state = self._library_state(cfg.library_id, stats)
                states.append(state)
                total_files += stats["files"]
                total_chunks += stats["chunks"]
                libs.append({
                    "name": cfg.name,
                    "state": state,
                    "files": stats["files"],
                    "chunks": stats["chunks"],
                    "path": cfg.root_path,
                })
            if not libs or all(s == "none" for s in states):
                agg = "none"
            elif any(s == "stale" for s in states):
                agg = "stale"
            else:
                agg = "ok"
            progress = self._progress_snapshot()
            if progress["heartbeat"] == "dead" and not self._dead_alerted:
                self._dead_alerted = True
                self._push("alert", {
                    "level": "dead",
                    "text": "索引疑似卡死：心跳已停止，请到索引页查看或停止任务",
                })
            if progress["heartbeat"] != "dead":
                self._dead_alerted = False
            gpu = probe_card()
            return {
                "libs": libs,
                "agg_state": agg,
                "files": total_files,
                "chunks": total_chunks,
                "vault_files": self._cached(("vault_files",), VAULT_FILES_TTL_S, self._vault_file_count),
                "progress": progress,
                "wemm_live": self._wemm_live_cached(),
                "gpu": {
                    "ok": bool(gpu.get("ok")),
                    "mem_used_mb": gpu.get("mem_used_mb"),
                    "mem_total_mb": gpu.get("mem_total_mb"),
                    "util_pct": gpu.get("util_pct"),
                    "power_w": gpu.get("power_w"),
                },
                "cpu": _cpu_percent(),
                "last_elapsed": progress.get("elapsed"),
                "issues": self._issue_snapshot(libraries),
                "wemm": self._wemm_backend_state(),
                "device": self._device_snapshot(bool(gpu.get("ok"))),
            }
        except Exception as exc:  # noqa: BLE001 - 快照不能带崩推送线程
            return {"error": "快照失败：%s" % exc}

    def _library_state(self, library_id: str, stats: dict[str, Any]) -> str:
        """单库三态：`ok` 索引最新 / `stale` 有变更待索引 / `none` 尚未索引（旧
        `library_state`）。判定来自 core 的 `library_freshness`（与搜索前自动同步同一份），
        扫盘结果缓存 10 秒。检测本身失败一律按 stale（旧 `kb_stale` 失败同处理）。"""
        if stats.get("state") != "ok" or not stats.get("files"):
            return "none"

        def compute() -> str:
            try:
                info = self._pipeline.library_freshness(library_id)[library_id]  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                return "stale"
            return "stale" if info.stale else "ok"

        return self._cached(("fresh", library_id), FRESHNESS_TTL_S, compute)

    def _vault_file_count(self) -> int:
        """全部库"按规则应该索引"的文件总数（旧 `vault_file_count`）。"""
        total = 0
        for cfg in self._libraries():
            try:
                decisions = self._lib_mgr.resolve_included_files(cfg.library_id)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            total += sum(1 for _path, included, _reason in decisions if included)
        return total

    def _progress_snapshot(self) -> dict[str, Any]:
        """进度段：全部来自 `pipeline.index_status`（core 的进度读模型）。

        rag-redo 的索引是**按库**各有一份进度文件；旧前端只有一根全局进度条，所以在跑的
        库合并成一份（文件/块数求和、取最长耗时、库名用顿号连接）；没有在跑的，展示最近
        一次结束的那次（`done` 的心跳态叫"完成"，失败/取消回到 idle）。"""
        statuses: list[tuple[Any, dict[str, Any]]] = []
        for cfg in self._libraries():
            try:
                status = self._pipeline.index_status(cfg.library_id)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - 单库查不到不影响其他库
                continue
            if status:
                statuses.append((cfg, status))
        active = [(c, s) for c, s in statuses if s.get("stage") in ("starting", "running")]
        running = bool(active)
        if self._was_running and not running:
            self._invalidate_cache()  # 索引刚结束：库状态/失败汇总要重算
        self._was_running = running
        base: dict[str, Any] = {
            "running": False, "phase": "idle", "files_done": 0, "files_total": 0,
            "chunks_done": 0, "chunks_total": 0, "pct": 0.0, "elapsed": None,
            "library": "", "busy": False, "task": "idle", "heartbeat": "idle",
            "heartbeat_note": None,
        }
        if not active:
            finished = [(c, s) for c, s in statuses if s.get("finished_at") is not None]
            if not finished:
                return base
            cfg, status = max(finished, key=lambda item: float(item[1]["finished_at"]))
            done = status.get("stage") == "done"
            base.update({
                "phase": "done" if done else "idle",
                "files_done": int(status.get("files_done") or 0),
                "files_total": int(status.get("files_total") or 0),
                "chunks_done": int(status.get("chunks_done") or 0),
                "chunks_total": int(status.get("chunks_total") or 0),
                "pct": 100.0 if done else 0.0,
                "elapsed": status.get("elapsed_s"),
                "library": cfg.name,
                "heartbeat": "done" if done else "idle",
            })
            return base

        active.sort(key=lambda item: float(item[1].get("started_at") or 0.0))
        first_status = active[0][1]
        files_done = sum(int(s.get("files_done") or 0) for _c, s in active)
        files_total = sum(int(s.get("files_total") or 0) for _c, s in active)
        phases = [str(s.get("phase") or "") for _c, s in active]
        if files_total > 0:
            pct = min(100.0, files_done / files_total * 100.0)
        else:
            pct = 0.0
        # 合并后的阶段：多个库取最靠前的阶段（还有库在扫描/提取，就不算进入写入）
        mapped = [_PHASE_MAP.get(phase, "scanning") for phase in phases]
        order = ["scanning", "converting", "embedding", "writing", "wemm"]
        phase = min(mapped, key=lambda item: order.index(item) if item in order else 0)
        health = {str(s.get("health") or "healthy") for _c, s in active}
        if health & {"orphaned", "stalled_no_heartbeat"}:
            heartbeat = "dead"
        elif "stalled_no_progress" in health:
            heartbeat = "stalled"
        else:
            heartbeat = "running"
        note = self._heartbeat_note(first_status, phase, heartbeat)
        foreign = any(s.get("owner") == "foreign" for _c, s in active)
        base.update({
            "running": True,
            "phase": phase,
            "files_done": files_done,
            "files_total": files_total,
            "chunks_done": sum(int(s.get("chunks_done") or 0) for _c, s in active),
            "chunks_total": sum(int(s.get("chunks_total") or 0) for _c, s in active),
            "pct": pct,
            "elapsed": max(float(s.get("elapsed_s") or 0.0) for _c, s in active),
            "library": "、".join(c.name for c, _s in active),
            "busy": any(bool(s.get("active")) for _c, s in active),
            "task": "foreign" if foreign else "ours",
            "heartbeat": heartbeat,
            "heartbeat_note": note,
        })
        return base

    @staticmethod
    def _heartbeat_note(status: dict[str, Any], phase: str, heartbeat: str) -> str | None:
        """心跳胶囊文案（旧 `store.heartbeat_note`）：DEAD 优先——红胶囊配"宽限内"
        文案自相矛盾；提取阶段 → 转换提示；停滞宽限内 → 合法长静默提示。"""
        if heartbeat == "dead":
            return None
        if phase == "converting":
            return "文档转换中（大文件耗时属预期）"
        until = status.get("stall_grace_until")
        if isinstance(until, (int, float)) and not isinstance(until, bool) and time.time() < until:
            quiet = max(0, int(time.time() - float(status.get("progress_at") or 0.0)))
            return f"模型加载/写库中（已安静 {quiet}s，宽限内）"
        return None

    def _issue_counts(self, library_id: str) -> dict[str, int]:
        """单库失败汇总 `{reason: 文件数}`（旧 `meta_issues_for`）。"""

        def compute() -> dict[str, int]:
            try:
                payload = self._pipeline.index_failures(library_id) or {}  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                return {}
            counts: dict[str, int] = {}
            for row in payload.get("failures") or []:
                reason = str(row.get("reason") or "unknown")
                counts[reason] = counts.get(reason, 0) + 1
            return counts

        return self._cached(("issues", library_id), 5.0, compute)

    def _issue_snapshot(self, libraries: list[Any]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for cfg in libraries:
            counts = self._issue_counts(cfg.library_id)
            for reason in sorted(
                counts,
                key=lambda r: ISSUE_ORDER.index(r) if r in ISSUE_ORDER else 99,
            ):
                label, advice = ISSUE_TEXT.get(reason, (reason, ""))
                issues.append({
                    "lib": cfg.name, "reason": reason, "count": counts[reason],
                    "label": label, "advice": advice,
                })
        return issues

    def _device_snapshot(self, cuda: bool) -> dict[str, Any]:
        """当前嵌入 / 重排 / CUDA。插件 id 代替模型名（模型由插件决定，不在设置里）；
        `cuda` 用"探测到 NVIDIA 整卡"近似——本插件权限声明 `gpu = false`，不能 import
        torch 去问 `cuda.is_available()`。"""
        try:
            registry = self._pipeline.runtime.registry  # type: ignore[attr-defined]
            embedder = registry.active_of("embedder")
            reranker = registry.active_of("reranker")
        except Exception:  # noqa: BLE001
            embedder = reranker = None
        return {"model": embedder or "", "rerank": reranker or "", "cuda": cuda}

    def _wemm_backend_state(self) -> dict[str, Any]:
        """WEMM 开关（现读设置，不缓存快照）。"""
        default = FIELDS["wemm_backend"].default  # 登记表里对账过的那一份，不在这里再写一遍字面量
        backend = str(self._pipeline.runtime.settings.get("wemm_backend", default) or default)  # type: ignore[attr-defined]
        return {"backend": backend, "url": ""}

    def _wemm_live_cached(self) -> dict[str, Any]:
        """看图服务实况（只读探测）。`visual_status()` 会对服务 `/health` 发一次最长 5 秒
        的请求，不能放在每秒的快照里同步跑——后台线程刷新缓存，快照读缓存。"""
        default = {"alive": False, "loaded": False, "gpu_mem_gb": None}
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(("wemm_live",))
        if hit is not None and now - hit[0] < WEMM_LIVE_TTL_S:
            return hit[1]
        with self._wemm_probe_lock:
            if not self._wemm_probe_running:
                self._wemm_probe_running = True
                threading.Thread(target=self._probe_wemm_live, daemon=True, name="gui-wemm-live").start()
        return hit[1] if hit is not None else default

    def _probe_wemm_live(self) -> None:
        value = {"alive": False, "loaded": False, "gpu_mem_gb": None}
        try:
            for status in self._pipeline.visual_status().values():  # type: ignore[attr-defined]
                if not isinstance(status, dict):
                    continue
                service = status.get("service") if isinstance(status.get("service"), dict) else {}
                value = {
                    "alive": bool(status.get("subprocess_alive")),
                    "loaded": bool(service.get("loaded")),
                    "gpu_mem_gb": service.get("gpu_mem_gb"),
                }
                break
        except Exception:  # noqa: BLE001
            pass
        with self._cache_lock:
            self._cache[("wemm_live",)] = (time.monotonic(), value)
        with self._wemm_probe_lock:
            self._wemm_probe_running = False

    # ==================================================================
    # 库管理
    # ==================================================================

    def list_libraries(self) -> list[dict[str, Any]]:
        """库管理列表（旧 `list_summary` + 状态/问题汇总）。块数=向量库 count、最近索引
        =当前 generation manifest 的 mtime（数值时间戳；前端 `fmtTs` 只认数值/null），
        都不加载模型。"""
        rows: list[dict[str, Any]] = []
        for row in self._pipeline.library_rows():  # type: ignore[attr-defined]
            library_id = row["library_id"]
            try:
                overrides_map = self._lib_mgr.config_view(library_id)["overrides"]  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                overrides_map = {}
            try:
                summary = self._pipeline.get_library_summary(library_id)  # type: ignore[attr-defined]
                summary_row = {
                    "text": summary.text, "source": summary.source,
                    "updated_at": summary.updated_at, "fingerprint": summary.fingerprint,
                    "model": summary.model,
                }
            except Exception:  # noqa: BLE001
                summary_row = {"text": "", "source": "none", "updated_at": None,
                               "fingerprint": None, "model": None}
            stats = self._pipeline.index_stats(library_id)  # type: ignore[attr-defined]
            rows.append({
                "name": row["name"],
                "path": row["root_path"],
                "collection": library_id,
                "blocks": row["blocks"],
                "last_indexed": row["last_indexed"],
                "overrides": ",".join(f"{k}={v!r}" for k, v in overrides_map.items()),
                "state": self._library_state(library_id, stats),
                "issues": self._issue_counts(library_id),
                "summary": summary_row,
                "summary_stale": bool(row.get("summary_stale")),
            })
        return rows

    def get_library_config(self, name: str) -> dict[str, Any]:
        """库配置弹层数据：`{effective, overrides, all_keys}`（旧契约）。`overrides` 里有
        的键 = 该库偏离出厂默认；没有 = 沿用默认。"""
        try:
            return self._lib_mgr.config_view(self._resolve_library(name))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {"effective": {}, "overrides": {}, "all_keys": [], "error": _exc_text(exc)}

    def add_library(self, path: str, name: str | None = None) -> dict[str, Any]:
        """按路径注册一个库；`name` 留空 = 目录名。**名字就是库的身份**（旧项目语义）：
        库 id 取名字本身，名字与已有库的 id/显示名都不能重（旧"库名已存在"）。"""
        try:
            root = Path(str(path)).expanduser().resolve()
            display = (name or "").strip() or root.name or "default"
            for cfg in self._libraries():
                if display in (cfg.library_id, cfg.name):
                    raise ValueError(f"库名已存在：{display}")
            cfg = self._lib_mgr.store.add_library(None, display, str(root))  # type: ignore[attr-defined]
            self._invalidate_cache()
            self._log("添加库：%s（%s）" % (display, path))
            return {"ok": True, "library_id": cfg.library_id}
        except Exception as exc:  # noqa: BLE001 - 见模块 docstring
            return {"ok": False, "error": _exc_text(exc)}

    def remove_library(self, name: str, drop: bool = False) -> dict[str, Any]:
        """注销库；`drop=True` 连索引数据一起清（旧 `remove_library(drop)`）。笔记文件
        永远保留。数据清理走 core 的全局回收（`prune_unreferenced_data`，问题49：没用到
        就删）——有索引任务在跑时不动手，留给下一轮索引完成后的回收，如实告知。"""
        try:
            library_id = self._resolve_library(name)
            display = self._name_of(library_id)
            self._lib_mgr.store.remove_library(library_id)  # type: ignore[attr-defined]
            self._invalidate_cache()
            note = "已从注册表移除；笔记文件保留"
            if drop:
                if self._any_index_active():
                    note += "；有索引任务在运行，索引数据将在其完成后的全局回收里清理"
                else:
                    self._pipeline.prune_unreferenced_data()  # type: ignore[attr-defined]
                    note += "；索引数据已清理"
            self._log("移除库：%s（%s）" % (display, "连数据删除" if drop else "仅注销"))
            return {"ok": True, "note": note}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _exc_text(exc)}

    def _any_index_active(self) -> bool:
        for cfg in self._libraries():
            try:
                status = self._pipeline.index_status(cfg.library_id)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            if status and status.get("stage") in ("starting", "running"):
                return True
        return False

    def set_library_config(self, name: str, updates: dict[str, Any]) -> dict[str, Any]:
        """批量写库配置 → `{ok, errors:{键:文案}}`。空字符串 = 恢复默认；逐键各自生效。"""
        try:
            library_id = self._resolve_library(name)
            errors = self._lib_mgr.set_config(library_id, updates or {})  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "errors": {"": _exc_text(exc)}}
        if not errors:
            self._log("更新库配置：%s（%s）" % (name, ", ".join(updates or {})))
        return {"ok": not errors, "errors": errors}

    def unset_library_config(self, name: str, keys: list[str]) -> dict[str, Any]:
        """把指定配置键恢复为出厂默认。契约形状 `{ok}`；单个键失败不整体失败（旧实现逐键
        try），但**库本身不存在必须报错**——不能对着一个不存在的库假报成功。"""
        try:
            library_id = self._resolve_library(name)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _exc_text(exc)}
        for key in keys or []:
            try:
                self._lib_mgr.unset_config(library_id, [key])  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - 逐键尽力，契约不报错
                continue
        return {"ok": True}

    # ---- 库简介（问题60）---------------------------------------------------

    def set_library_summary(self, name: str, text: str) -> dict[str, Any]:
        """用户在 GUI 直接手写/保存：无条件生效（source=user），不经过任何确认门禁——
        门禁只保护"AI 想覆盖用户已写内容"，用户改自己的东西不需要向自己确认。"""
        try:
            library_id = self._resolve_library(name)
            result = self._pipeline.set_library_summary_direct(  # type: ignore[attr-defined]
                library_id, text, source="user"
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _exc_text(exc)}
        self._invalidate_cache()
        self._log("手动编辑库简介：%s" % name)
        return result if isinstance(result, dict) and "ok" in result else {"ok": True}

    def refresh_library_summaries_batch(self, names: list[str] | None, force: bool = False) -> dict[str, Any]:
        """后台生成/刷新一个或多个库的简介，**立即返回**，前端用
        `refresh_library_summaries_poll` 轮询（单库刷新也走这条路径，names 传一个元素）。
        `names` 为空 = 全部库；已有任务在跑时返回 `{ok: False, error}`。`force=False` 时
        遇到 source=user（用户手写过）的库不覆盖，在结果里标 `needs_confirm`。"""
        with self._sumref_lock:
            if self._sumref["running"]:
                return {"ok": False, "error": "已有简介刷新任务在运行，请等它跑完或稍后再试"}
            libraries = self._libraries()
            if names:
                targets = []
                for requested in names:
                    try:
                        targets.append(self._resolve_library(requested))
                    except ValueError:
                        continue  # 旧行为：不在注册表里的名字直接忽略
            else:
                targets = [cfg.library_id for cfg in libraries]
            targets = list(dict.fromkeys(targets))
            if not targets:
                return {"ok": False, "error": "没有可刷新的库（库名不在注册表里）"}
            self._sumref = {
                "running": True, "total": len(targets), "done": 0,
                "current": self._name_of(targets[0]), "results": {},
            }
        threading.Thread(
            target=self._run_summary_refresh_batch, args=(targets, bool(force)),
            daemon=True, name="sum-refresh-batch",
        ).start()
        self._log("库简介刷新已启动：%s%s" % (
            "、".join(self._name_of(t) for t in targets), "（强制覆盖手写）" if force else "",
        ))
        return {"ok": True, "total": len(targets)}

    def _run_summary_refresh_batch(self, library_ids: list[str], force: bool) -> None:
        for library_id in library_ids:
            display = self._name_of(library_id)
            with self._sumref_lock:
                self._sumref["current"] = display
            try:
                current = self._pipeline.get_library_summary(library_id)  # type: ignore[attr-defined]
                if current.source == "user" and not force:
                    result: dict[str, Any] = {"ok": False, "needs_confirm": True}
                else:
                    text, fingerprint, model = self._pipeline.generate_library_summary(  # type: ignore[attr-defined]
                        library_id
                    )
                    self._pipeline.set_library_summary_direct(  # type: ignore[attr-defined]
                        library_id, text, source="ai", fingerprint=fingerprint, model=model
                    )
                    result = {"ok": True, "text": text}
            except Exception as exc:  # noqa: BLE001 - 单库失败不拖垮批次
                result = {"ok": False, "error": _exc_text(exc)}
            with self._sumref_lock:
                self._sumref["results"][display] = result
                self._sumref["done"] += 1
        with self._sumref_lock:
            n_ok = sum(1 for r in self._sumref["results"].values() if r.get("ok"))
            self._sumref["current"] = None
            self._sumref["running"] = False
        self._invalidate_cache()
        self._log("库简介刷新完成：%d/%d 成功" % (n_ok, len(library_ids)))

    def refresh_library_summaries_poll(self) -> dict[str, Any]:
        with self._sumref_lock:
            state = self._sumref
            return {
                "running": state["running"], "total": state["total"], "done": state["done"],
                "current": state["current"],
                "results": {k: dict(v) for k, v in state["results"].items()},
            }

    # ==================================================================
    # 勾选树（裁决全部来自 library-manager）
    # ==================================================================

    def selection_tree(self, name: str, sub: str = "") -> dict[str, Any]:
        """目录勾选树。**本方法不做任何裁决**：逐节点的 `state`/`explicit`/`state_text`
        全部由 `library-manager` 用 `selection.explicit_verdict`/`decide_included`（问题47
        的唯一权威实现）算好后返回，这里只透传。"""
        try:
            return self._lib_mgr.selection_tree(self._resolve_library(name), sub)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {
                "lib": name, "sub": sub or "", "root": "", "folders": [], "dirs": [],
                "files": [], "selection_in": [], "selection_out": [], "extensions": [],
                "default": "follow", "error": _exc_text(exc),
            }

    def selection_update(self, name: str, changes: list[dict]) -> dict[str, Any]:
        """勾选变更。GUI 就是用户本人，**直接生效，不需要 MCP 那套提案+确认码**。校验与
        合并仍走 library-manager 的 `normalize_selection_changes`/`apply_selection_changes`，
        同位置矛盾在写入前就被拒绝。"""
        try:
            library_id = self._resolve_library(name)
            result = self._lib_mgr.apply_selection_direct(library_id, changes or [])  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self._log("勾选范围更新失败（%s）：%s" % (name, exc), is_error=True)
            return {"ok": False, "error": _exc_text(exc), "selection_in": [], "selection_out": []}
        if result.get("ok"):
            self._invalidate_cache()
            self._log("勾选范围更新（%s）：%s" % (
                name, "；".join("%s→%s" % (c.get("path"), c.get("action")) for c in (changes or [])),
            ))
        else:
            self._log("勾选范围更新失败（%s）：%s" % (name, result.get("error")), is_error=True)
        return result

    def selection_resolve_conflict(self, name: str, path: str) -> dict[str, Any]:
        """同位置矛盾一键解决：只在本库 exclude_dirs 移除该项并纳入勾选，其他库不动。"""
        try:
            library_id = self._resolve_library(name)
            result = self._lib_mgr.resolve_selection_conflict(library_id, path)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _exc_text(exc), "selection_in": [], "selection_out": []}
        if result.get("ok"):
            self._invalidate_cache()
            self._log("勾选矛盾已解决（%s）：仅本库排除名单移除 %s 并纳入" % (name, path))
        return result

    def selection_format_bulk(self, name: str, ext: str, include: bool) -> dict[str, Any]:
        """格式快捷批量勾选。语义与收口都在 library-manager（`format_selection_bulk`）。"""
        try:
            library_id = self._resolve_library(name)
            result = self._lib_mgr.format_bulk(library_id, ext, bool(include))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "changed": 0, "error": _exc_text(exc)}
        if result.get("ok"):
            self._invalidate_cache()
            self._log("勾选格式批量（%s）：%s %s，影响 %d 项" % (
                name, ext, "纳入" if include else "排除", int(result.get("changed") or 0),
            ))
        return result

    # ==================================================================
    # 索引
    # ==================================================================

    def start_index(self, full: bool = False, libraries: str = "") -> dict[str, Any]:
        """启动索引。`libraries` 逗号分隔的库名，空 = 全部已注册库。逐库调 core 的
        `start_index_library`，本方法不重试、不排队、不判断该不该重跑。"""
        try:
            scope = self._resolve_scope(libraries)
            if scope in ("", "all"):
                targets = [cfg.library_id for cfg in self._libraries()]
            else:
                targets = scope.split(",")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _exc_text(exc)}
        if not targets:
            return {"ok": False, "error": "没有可索引的库"}
        if self._any_index_active_in(targets):
            return {"ok": False, "already_running": True, "error": "已有索引任务在运行"}
        started: list[str] = []
        for library_id in targets:
            try:
                result = self._pipeline.start_index_library(  # type: ignore[attr-defined]
                    library_id, source="gui", full=bool(full)
                )
            except Exception as exc:  # noqa: BLE001 - 单库失败不拖垮批次
                return {"ok": False, "started": started, "error": _exc_text(exc)}
            if not result.started:
                return {"ok": False, "started": started, "error": result.message}
            started.append(library_id)
        self._invalidate_cache()
        names = "、".join(self._name_of(t) for t in started)
        self._log("触发%s重建（库=%s）" % ("全量" if full else "增量", names))
        return {
            "ok": True,
            "started": started,
            "full": bool(full),
            "message": "已触发%s：%s" % ("全量重建" if full else "增量更新", names),
        }

    def _any_index_active_in(self, library_ids: list[str]) -> bool:
        for library_id in library_ids:
            try:
                status = self._pipeline.index_status(library_id)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            if status and status.get("stage") in ("starting", "running") and status.get("active"):
                return True
        return False

    def stop_index(self) -> dict[str, Any]:
        """停止索引。**只能停本 GUI 进程拉起的任务**——别的进程（如 MCP）触发的索引返回
        明确拒绝并指引等它跑完（旧 bridge.py 同一语义：跨进程不许抢）。归属来自 core
        进度读模型的 `owner`/`can_stop`，不再另存一份"我拉起过哪些 run"。"""
        stopped_any = False
        foreign = False
        failure = ""
        for cfg in self._libraries():
            try:
                status = self._pipeline.index_status(cfg.library_id)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            if not status or not status.get("active"):
                continue
            if status.get("owner") != "self" or not status.get("can_stop"):
                foreign = True
                continue
            try:
                stopped, message = self._pipeline.stop_index_library(  # type: ignore[attr-defined]
                    cfg.library_id, str(status.get("run_id") or "")
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "stopped": False, "error": _exc_text(exc)}
            if stopped:
                stopped_any = True
            else:
                failure = failure or message
        if stopped_any:
            self._invalidate_cache()
            self._log("已请求停止索引子进程", is_error=True)
            return {"ok": True, "stopped": True}
        if failure:
            return {"ok": False, "stopped": False, "reason": failure}
        if foreign:
            return {
                "ok": False, "stopped": False,
                "reason": "该任务不是本应用启动的（可能来自 MCP 或旧进程），请等待完成或用命令行工具处理",
            }
        return {"ok": True, "stopped": False, "reason": "当前没有运行中的索引任务"}

    # ==================================================================
    # 检索
    # ==================================================================

    def search(
        self,
        query: str,
        top_k: int = 5,
        libraries: str = "",
        include_body: bool = True,
    ) -> dict[str, Any]:
        """混合检索。委托 core `search_with_advice`，本方法不排序、不算置信度、不封顶——
        `confidence` 原样来自 core，`notice` 条目来自 core 的 advice 通道（结果本体与建议
        走不同字段，前端分开渲染）。`libraries` 为空时交给 core 按 `default_libraries` 收窄。"""
        if not (query or "").strip():
            return {"results": [], "error": "请输入问题"}
        started = time.monotonic()
        try:
            response = self._pipeline.search_with_advice(  # type: ignore[attr-defined]
                self._resolve_scope(libraries),
                str(query).strip(),
                top_k=int(top_k or 5),
                include_body=bool(include_body),
            )
        except Exception as exc:  # noqa: BLE001
            self._log("搜索失败：%s" % exc, is_error=True)
            return {"results": [], "error": _exc_text(exc)}
        elapsed = round(time.monotonic() - started, 1)
        self._log("检索「%s」%s 耗时 %.1fs" % (
            str(query).strip(), ("（%s）" % libraries) if libraries else "（全部库）", elapsed,
        ))
        from core.pipeline import DEFAULT_CONFIDENCE_WARN_THRESHOLD, confidence_tier

        warn = self._pipeline.runtime.settings.get(  # type: ignore[attr-defined]
            "confidence_warn_threshold", DEFAULT_CONFIDENCE_WARN_THRESHOLD
        )
        results: list[dict[str, Any]] = []
        for item in response.results:
            body = item.text or ""
            known = item.chunk_index >= 0 and item.total_chunks > 0
            results.append({
                "lib": self._name_of(item.library_id),
                "rel": item.path,
                "heading": item.heading_breadcrumb or None,
                # 0 起的块序号 / 该文件总块数；未知 → null，前端显示"整段"
                "chunk_idx": item.chunk_index if known else None,
                "chunk_total": item.total_chunks if known else None,
                "confidence": round(item.confidence, 3),
                "confidence_tier": confidence_tier(item.confidence, warn),
                "backfilled": bool(item.backfilled),
                "body": body,
                # 命中正文默认看渲染：后端直接带 rendered_html，前端不裸显 MD 源码
                "rendered_html": md_to_html(body),
            })
        for advice in response.advice:
            text = advice if isinstance(advice, str) else str(getattr(advice, "text", advice))
            if text:
                results.append({
                    "lib": "", "rel": "", "heading": None, "chunk_idx": None,
                    "chunk_total": None, "confidence": None, "body": text, "notice": True,
                })
        return {"results": results, "elapsed": elapsed, "error": None}

    # ==================================================================
    # 正文 / 关系 / 打开
    # ==================================================================

    def read_document(self, lib: str, rel: str) -> dict[str, Any]:
        """GUI 内正文查看。**零触发只读**：绝不后台触发 OCR 或云端调用——`.md/.txt` 现读
        源文件，pdf/docx 只读既有提取缓存，没提取过就如实说。超长截断 20 万字。"""
        empty = {"markdown": "", "rendered_html": "", "chars": 0, "truncated": False, "route": "-"}
        try:
            library_id = self._resolve_library(lib)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, **empty, "error": _exc_text(exc)}
        try:
            document = self._pipeline.read_document(library_id, rel or "")  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            message = _exc_text(exc)
            if "还没有被成功索引过" in message:
                message = "该文件尚未被索引/提取，请先增量重建后再查看"
            self._log("正文查看失败 %s/%s：%s" % (lib, rel, message), is_error=True)
            return {"ok": False, **empty, "error": message}
        text = document.text or ""
        truncated = len(text) > DOC_VIEW_MAX_CHARS
        if truncated:
            text = text[:DOC_VIEW_MAX_CHARS]
        self._log("正文查看 %s/%s（%d 字%s）" % (lib, rel, len(text), "·已截断" if truncated else ""))
        return {
            "ok": True,
            "markdown": text,
            "rendered_html": md_to_html(text),
            "chars": len(text),
            "truncated": truncated,
            "route": _ROUTE_MAP.get(document.source, document.source or "-"),
            "error": None,
        }

    def note_relations(self, lib: str, rel: str) -> dict[str, Any]:
        """双链关系：`{resolved, file, outlinks, inlinks}`。库不存在 / 没索引过 → 未命中。"""
        miss = {"resolved": False, "file": None, "outlinks": [], "inlinks": []}
        try:
            library_id = self._resolve_library(lib)
            relations = self._pipeline.note_relations(library_id, rel)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return miss
        return {**miss, **relations}

    def open_source(self, lib: str, rel: str, heading: str = "") -> dict[str, Any]:
        """打开源文件：Obsidian vault（含 `.obsidian`）走 `obsidian://` URI，普通目录直接
        打开。`heading` 参数保留（前端契约不变）但**不拼进 URI**——旧项目 2026-09-11 的
        结论：字面 `#` 追加会让 Obsidian 报"文件不存在"，跳标题要在真机上验证 `%23`
        写法后再回填。路径必须落在库目录内。"""
        try:
            library_id = self._resolve_library(lib)
            cfg = self._lib_mgr.store.get(library_id)  # type: ignore[attr-defined]
            root = Path(cfg.root_path).resolve()
            target = (root / str(rel)).resolve()
            if root != target and root not in target.parents:
                raise ValueError("源文件路径越出库目录")
            opener = getattr(os, "startfile", None)
            if not callable(opener):
                return {"ok": True, "opened": False, "path": str(target)}
            if (root / ".obsidian").is_dir():
                url = "obsidian://open?vault=%s&file=%s" % (_quote_path(root.name), _quote_path(str(rel)))
                opener(url)
            else:
                if not target.is_file():
                    raise FileNotFoundError(str(target))
                opener(str(target))
            self._log("打开 %s" % rel)
            return {"ok": True, "opened": True, "path": str(target)}
        except Exception as exc:  # noqa: BLE001
            self._log("打开失败 %s：%s" % (rel, exc), is_error=True)
            return {"ok": False, "error": _exc_text(exc)}

    def open_path(self, path: str) -> dict[str, Any]:
        """用系统默认程序打开本地路径（"打开文件夹/日志目录"）。"""
        try:
            target = Path(path)
            if not str(path) or not target.exists():
                return {"ok": False, "error": f"路径不存在: {path}"}
            opener = getattr(os, "startfile", None)
            if not callable(opener):
                return {"ok": False, "error": "当前系统不支持打开本地路径"}
            opener(str(target))
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ==================================================================
    # 设置
    # ==================================================================

    def get_settings(self) -> dict[str, Any]:
        """设置页全量：`{groups, missing_keys}`。`value` 一律字符串（bool→"true"/"false"，
        list→"a,b"）；展示的是**当前生效值**（没设过 = 默认值），secret 键由前端渲染成
        密码框。分组与文案见 `settings_schema.py`。"""
        settings = self._pipeline.runtime.settings  # type: ignore[attr-defined]
        groups: list[dict[str, Any]] = []
        for group in GROUPS:
            fields = []
            for field in group.fields:
                value = settings.get(field.key, field.default)
                fields.append({
                    "key": field.key,
                    "label": field.label,
                    "kind": field.kind,
                    "hint": field.hint,
                    "rebuild": field.rebuild,
                    "secret": setting_is_secret(field.key),
                    "choices": [list(choice) for choice in field.choices],
                    "suggest": [list(choice) for choice in field.suggest],
                    "value": format_setting_value(field.kind, value),
                })
            groups.append({
                "title": group.title, "level": group.level, "desc": group.desc, "fields": fields,
            })
        return {"groups": groups, "missing_keys": []}

    def save_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        """批量保存设置 → `{errors:{键:文案}}`（空 errors = 成功，已热读生效）。

        按键**声明的类型**转换；任何一个键转换失败 → 整批不落盘（旧
        `config_editor.apply_updates`："任一失败即整体中止"）。值等于默认值 → 清除该键
        而不是写死（前端保存时会把页面上所有字段一起发回，不这样做会把每个默认值都钉死）。
        未登记的键跳过（旧 `kind_of(key) is None`）。"""
        parsed: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for key, raw in (updates or {}).items():
            if key not in FIELDS:
                continue
            try:
                parsed[key] = coerce_setting(key, raw)
            except ValueError as exc:
                errors[str(key)] = "格式错误：%s" % exc
        if errors:
            self._log("设置保存失败：%s" % errors, is_error=True)
            return {"errors": errors}
        settings = self._pipeline.runtime.settings  # type: ignore[attr-defined]
        try:
            for key, value in parsed.items():
                if value == FIELDS[key].default:
                    settings.unset(key)
                else:
                    settings.set(key, value)
        except Exception as exc:  # noqa: BLE001 - 写盘失败要让前端看见
            return {"errors": {"__file__": "写入失败：%s" % exc}}
        self._log("设置已保存并热读生效（%s）" % ", ".join(parsed))
        return {"errors": {}}

    # ==================================================================
    # 图谱 / 语义边
    # ==================================================================

    def graph(self, libraries: str = "") -> dict[str, Any]:
        """图谱数据（读模型来自 core `graph`）。节点 `lib` 一律是显示名（前端按它着色/过滤）。"""
        empty = {"nodes": [], "edges": [], "libs": [], "stats": {"nodes": 0, "edges": 0}}
        try:
            response = self._pipeline.graph(self._resolve_scope(libraries) or "all")  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {**empty, "error": _exc_text(exc)}
        nodes: list[dict[str, Any]] = []
        for node in response.nodes:
            item: dict[str, Any] = {
                "id": node.node_id,
                "lib": self._name_of(node.library_id),
                "rel": node.path,
                "type": node.node_type,
                "chunks": node.chunks,
                "updated": node.updated_ns / 1_000_000_000 if node.updated_ns is not None else None,
                "fail_reason": node.failure_reason,
                "theme": node.theme,
                "pipeline": {"mineru": node.extraction_state, "wemm": node.visual_state},
                "big": node.is_hub,
            }
            if node.page_number is not None:
                item["page"] = node.page_number
            if node.page_count is not None:
                item["pages"] = node.page_count
            nodes.append(item)
        return {
            "nodes": nodes,
            "edges": [{"a": e.source, "b": e.target, "kind": e.kind} for e in response.edges],
            "libs": [self._name_of(library_id) for library_id in response.library_ids],
            "stats": {"nodes": response.stats.nodes, "edges": response.stats.edges},
        }

    def semantic_edges(self, libraries: str = "", threshold: float = 0.62) -> dict[str, Any]:
        """按需语义边（默认不加载嵌入模型，所以和 `graph()` 分开）。纯读路径，失败返回
        `{"edges": [], "error": ...}` 而不是抛异常。"""
        try:
            scope = self._resolve_scope(libraries) or "all"
            self._push("notice", {"text": "正在加载嵌入模型并计算语义边…"})
            response = self._pipeline.graph_semantic_edges(  # type: ignore[attr-defined]
                scope, threshold=float(threshold)
            )
        except Exception as exc:  # noqa: BLE001
            return {"edges": [], "error": _exc_text(exc)}
        if response.error:
            self._log("语义边失败：%s" % response.error, is_error=True)
        return {
            "edges": [{"a": e.source, "b": e.target, "sim": e.similarity} for e in response.edges],
            "error": response.error,
        }

    # ==================================================================
    # 诊断
    # ==================================================================

    def dedup_run(self, threshold: float | None = None) -> dict[str, Any]:
        """近似去重，跨全部已注册库（只读，阻塞在调用线程）。`clusters` 打平成相似对
        `{lib, a, b, sim}`（旧契约），`stats={files, clusters, seconds}`。"""
        started = time.monotonic()
        pairs: list[dict[str, Any]] = []
        files = 0
        try:
            for cfg in self._libraries():
                report = self._pipeline.find_duplicate_links(  # type: ignore[attr-defined]
                    cfg.library_id, threshold=float(threshold) if threshold is not None else 0.8
                )
                files += int(report["scanned"])
                for a, b, sim in report["links"]:
                    pairs.append({"lib": cfg.name, "a": a, "b": b, "sim": sim})
        except Exception as exc:  # noqa: BLE001
            return {"clusters": [], "stats": None, "error": _exc_text(exc)}
        self._log("近似去重：%d 组近似对" % len(pairs))
        return {
            "clusters": pairs,
            "stats": {"files": files, "clusters": len(pairs), "seconds": round(time.monotonic() - started, 1)},
            "error": None,
        }

    def _log_tail_lines(self, n: int = 4000) -> list[str]:
        """索引日志尾 n 行（供失败明细拼"更具体报错"）；读不到 → 空列表。"""
        try:
            lines = self._index_log_path().read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-n:] if len(lines) > n else lines

    @staticmethod
    def _log_snippet_for(rel: str, tail: list[str]) -> list[str]:
        """在日志尾里找最近提到该文件的 3 行，作失败明细的可展开详情。"""
        if not tail:
            return []
        base = rel.replace("\\", "/").rsplit("/", 1)[-1]
        if not base:
            return []
        return [line for line in tail if base in line][-3:]

    def failures(self, lib: str = "") -> dict[str, Any]:
        """失败明细。`lib` 空/"all" = 聚合全部库（`multi=True`，前端在文件名前显示库 chip）。
        `total`=失败条数，`healthy`=正常索引文件数（core 记的 `succeeded`）。"""
        try:
            if lib in ("", "all", None):
                targets = [cfg.library_id for cfg in self._libraries()]
            else:
                targets = [self._resolve_library(lib)]
        except Exception as exc:  # noqa: BLE001
            return {"total": 0, "healthy": 0, "multi": False, "rows": [], "error": _exc_text(exc)}
        tail = self._log_tail_lines()
        rows: list[dict[str, Any]] = []
        healthy = 0
        for library_id in targets:
            try:
                payload = self._pipeline.index_failures(library_id) or {}  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            healthy += int(payload.get("succeeded") or 0)
            display = self._name_of(library_id)
            for item in payload.get("failures") or []:
                path = str(item.get("path", ""))
                detail = item.get("detail")
                lines = self._log_snippet_for(path, tail)
                if detail:
                    lines = [str(detail), *lines]
                rows.append({
                    "lib": display, "rel": path,
                    "reason": str(item.get("reason") or "unknown"),
                    "will_retry": bool(item.get("will_retry")),
                    "detail": lines,
                })
        rows.sort(key=lambda r: (r["lib"], r["rel"]))
        return {"total": len(rows), "healthy": healthy, "multi": len(targets) > 1,
                "rows": rows, "error": None}

    def wemm_status(self, lib: str) -> dict[str, Any]:
        """WEMM 页库逐 PDF 状态：`{exists, total_pages, rows:[{lib, rel, pages, failed, reason}]}`。
        行只列 PDF（WEMM 页级导航仅对 PDF 有意义）。"""
        try:
            library_id = self._resolve_library(lib)
        except Exception as exc:  # noqa: BLE001
            return {"exists": False, "total_pages": 0, "rows": [], "error": _exc_text(exc)}
        try:
            states = self._pipeline.visual_page_states(library_id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {"exists": False, "total_pages": 0, "rows": [], "error": _exc_text(exc)}
        display = self._name_of(library_id)
        rows = []
        total = 0
        for state in sorted(states, key=lambda s: s.path):
            if not state.path.lower().endswith(".pdf"):
                continue
            failed = state.status == "failed" or bool(state.failure_reason)
            pages = None if failed and not state.pages else len(state.pages)
            if pages:
                total += pages
            rows.append({
                "lib": display, "rel": state.path, "pages": pages, "failed": bool(failed),
                "reason": state.failure_reason or "",
            })
        return {"exists": bool(rows), "total_pages": total, "rows": rows, "error": None}

    def wemm_probe(self) -> dict[str, Any]:
        """WEMM 看图服务只读探测：数据来自 core `visual_status`（插件自己的健康快照），
        本方法不拉起服务、不加载模型。"""
        try:
            providers = self._pipeline.visual_status()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {"alive": False, "detail": "未启动或不可达（%s）" % type(exc).__name__}
        if not providers:
            return {"alive": False, "detail": "未启用页级视觉检索插件"}
        status = next(iter(providers.values()))
        service = status.get("service") if isinstance(status, dict) else None
        alive = bool(isinstance(status, dict) and status.get("subprocess_alive"))
        if alive and isinstance(service, dict) and service.get("loaded"):
            detail = "模型已进显存（%s · %s）" % (service.get("model", "?"), service.get("device", "?"))
        elif alive:
            detail = "服务存活，待首次请求时自动加载模型"
        else:
            detail = "未启动或不可达——下次页级导航会按需自动拉起"
        self._log("WEMM 探测：%s" % detail)
        return {"alive": alive, "detail": detail}

    # ==================================================================
    # 提取试验台（独立子进程：取消 = terminate，180 秒硬超时）
    # ==================================================================

    def preview_start(self, path: str, backend: str | None = None) -> dict[str, Any]:
        """对单个本地文件跑一次提取（独立子进程），立即返回；结果用 `preview_poll` 取。
        `backend` 覆盖后端（空/None = 跟随全局设置）。"""
        if not path or not Path(str(path)).is_file():
            return {"ok": False, "error": "文件不存在：%s" % path}
        with self._pv_lock:
            if self._pv_proc is not None and self._pv_proc.is_alive():
                return {"ok": False, "error": "已有预览在运行，请先取消"}
            runtime = self._pipeline.runtime  # type: ignore[attr-defined]
            registry = runtime.registry
            plugin_ids = sorted({
                plugin_id
                for point in registry.provider_points() if point.startswith("extractor:")
                for plugin_id in registry.providers_of(point)
            })
            context = multiprocessing.get_context("spawn")
            self._pv_queue = context.Queue()
            self._pv_proc = context.Process(
                target=self._pv_target,
                args=(
                    self._pv_queue, str(runtime.plugins_dir), str(runtime.data_dir),
                    tuple(plugin_ids), dict(registry.active_choices()), str(path), backend or "",
                ),
                daemon=True,
            )
            self._pv_started = time.time()
            self._pv_result = None
            self._pv_done = False
            self._pv_proc.start()
        self._log("提取试验台启动：%s（后端=%s）" % (Path(str(path)).name, backend or "跟随全局"))
        return {"ok": True}

    @staticmethod
    def _preview_result_of(payload: dict[str, Any] | None) -> dict[str, Any]:
        """子进程 payload → 前端 result（纯函数，不碰进程/队列，可单测；旧
        `_preview_result_of`）。`ok` = 子进程正常交付；是否产出内容看 `reason`
        （空 = 有产出，非空 = 管线未产出及原因）。"""
        payload = payload or {}
        info = payload.get("info") or {}
        markdown = info.get("md") or ""
        route = info.get("route") or "-"
        return {
            "ok": bool(payload.get("ok")),
            "error": payload.get("error"),
            "markdown": markdown,
            "rendered_html": md_to_html(markdown),
            "reason": info.get("reason") or "",
            "route": _ROUTE_MAP.get(route, route),
            "cached": bool(info.get("cached")),
            "elapsed": info.get("elapsed") or 0,
            "chars": info.get("chars") or len(markdown),
        }

    @staticmethod
    def _preview_failure(error: str) -> dict[str, Any]:
        return {
            "ok": False, "error": error, "markdown": "", "rendered_html": "",
            "reason": "extract-failed", "route": "-", "cached": False, "elapsed": 0, "chars": 0,
        }

    def preview_poll(self) -> dict[str, Any]:
        """取试验台结果。`done=True` 后 `result` 不再变化（前端据此收起轮询）。进程异常退出、
        超过 180 秒都在这里收口：超时会**真的终止子进程**。"""
        with self._pv_lock:
            if self._pv_proc is None:
                return {"running": False, "done": True, "result": None}
            if self._pv_done:
                return {"running": False, "done": True, "result": self._pv_result}
            try:
                payload = self._pv_queue.get_nowait()
            except _queue.Empty:
                payload = None
            if payload is not None:
                self._pv_result = self._preview_result_of(payload)
                return self._finish_preview_locked()
            if not self._pv_proc.is_alive():
                # 进程已退出但队列里还可能有最后一条：再取一次，取不到才算异常退出
                try:
                    payload = self._pv_queue.get(timeout=0.2)
                except _queue.Empty:
                    payload = None
                self._pv_result = (
                    self._preview_result_of(payload) if payload is not None
                    else self._preview_failure("预览进程异常退出")
                )
                return self._finish_preview_locked()
            if time.time() - self._pv_started > PREVIEW_TIMEOUT_S:
                self._terminate_preview_locked()
                self._pv_result = self._preview_failure("预览超时（180s），已强制终止")
                return self._finish_preview_locked()
            return {"running": True, "done": False, "result": None}

    def _finish_preview_locked(self) -> dict[str, Any]:
        self._pv_done = True
        self._push("preview", {"running": False, "done": True})
        return {"running": False, "done": True, "result": self._pv_result}

    def _terminate_preview_locked(self) -> None:
        proc = self._pv_proc
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)

    def preview_cancel(self) -> dict[str, Any]:
        """取消试验台：**真的终止子进程**（云端 OCR/GPU 任务随之停下），不是只清状态。"""
        with self._pv_lock:
            if self._pv_proc is not None and self._pv_proc.is_alive():
                self._terminate_preview_locked()
                self._log("提取试验台已取消", is_error=True)
            self._pv_done = True
        return {"ok": True}

    # ==================================================================
    # 日志 / 导出 / 导入
    # ==================================================================

    def log_tail(self, cursor: int | None = None) -> dict[str, Any]:
        """索引日志增量读取。`cursor` 是上一次返回的**文件字节偏移**；None = 从头读最近 300
        行。数据源是 core 把 worker 输出重定向到的 `<data_dir>/index_worker.log`（GUI 自身
        动作也写进同一份，不另开日志）。"""
        try:
            data = self._index_log_path().read_bytes()
        except OSError:
            return {"lines": [], "cursor": 0}
        self._log_seen_cursor = len(data)
        if cursor is None:
            tail = data.decode("utf-8", errors="replace").splitlines()[-300:]
            return {"lines": tail, "cursor": len(data)}
        start = min(max(int(cursor or 0), 0), len(data))
        return {
            "lines": data[start:].decode("utf-8", errors="replace").splitlines(),
            "cursor": len(data),
        }

    def export_run(self) -> dict[str, Any]:
        """导出全部库到 `<data_dir>/exports/`（写归档的动作在 core：`export_library` 产出
        bytes，落盘路径由本层决定）。"""
        dest_dir = Path(self._pipeline.runtime.data_dir) / "exports"  # type: ignore[attr-defined]
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            written: list[str] = []
            for cfg in self._libraries():
                archive = self._pipeline.export_library(cfg.library_id)  # type: ignore[attr-defined]
                target = dest_dir / f"{cfg.library_id}.zip"
                atomic_write_bytes(target, archive)
                written.append(str(target))
        except Exception as exc:  # noqa: BLE001
            self._log("导出失败：%s" % exc, is_error=True)
            return {"ok": False, "error": _exc_text(exc)}
        self._log("导出完成：%d 个库 → %s" % (len(written), dest_dir))
        return {"ok": True, "message": f"已导出 {len(written)} 个库到 {dest_dir}", "files": written}

    def import_run(self, confirm_text: str) -> dict[str, Any]:
        """导入归档。**必须逐字输入确认词**才执行（防误触，对齐旧 contracts.md 门禁）。
        目标根目录取当前第一个库的父目录，导入后的库 id 沿用归档内记录。"""
        if confirm_text != IMPORT_CONFIRM_TEXT:
            return {"ok": False, "error": "确认文本不匹配，未执行导入"}
        exports = Path(self._pipeline.runtime.data_dir) / "exports"  # type: ignore[attr-defined]
        if not exports.is_dir():
            return {"ok": False, "error": "没有可导入的归档目录"}
        libraries = self._libraries()
        if not libraries:
            return {"ok": False, "error": "请先注册至少一个库（导入需要目标根目录）"}
        root_parent = Path(libraries[0].root_path).resolve().parent
        imported: list[str] = []
        errors: list[str] = []
        for archive in sorted(exports.glob("*.zip")):
            try:
                new_id = self._pipeline.import_library(  # type: ignore[attr-defined]
                    archive.read_bytes(), root_path=str(root_parent / archive.stem)
                )
                imported.append(new_id)
            except Exception as exc:  # noqa: BLE001 - 逐个归档尽力
                errors.append(f"{archive.name}: {_exc_text(exc)}")
        self._invalidate_cache()
        self._log("导入完成：成功 %d，失败 %d" % (len(imported), len(errors)), is_error=bool(errors))
        return {
            "ok": not errors,
            "imported": imported,
            "error": "; ".join(errors) if errors else None,
        }
