"""core/atomic.py — 原子文件写入的唯一权威实现（核心服务，不是插件）。

**为什么收敛到一个模块**：AGENTS.md 数据流铁律要求"同一业务判断只能有一
个权威实现"，而"写持久化文件必须避免半截 JSON/截断文件"这个判断此前在
仓库里被复制了八份以上（resource_arbiter/index_generation/index_failures/
index_progress/summary_store/visual-wemm/ocr-mineru-cloud 各写一遍
tmp+replace），另有 settings/plugins_state/libraries.json/BM25 索引/提取
缓存五处是**裸 write_text 直写目标文件**——进程在写入中途被杀/断电就会
留下截断文件，下次启动读到半截 JSON。参照物：obsidian-rag/library.py::
save_registry（421-427 行）对 libraries.json 的 tmp+replace 原子写——旧
项目自己最关键的注册表就是这么写的。

**Windows 细节**：`os.replace` 在目标文件刚被另一个进程/杀毒软件短暂打开
时会抛 `PermissionError`（句柄延迟释放），这是这台开发机真机验证时实测
踩到过的（见 core/index_progress.py 同款退避重试）；这里把退避重试收敛进
助手，调用方不需要各自复制。

**退避预算为什么是 ~0.8s 而不是最初那 30ms**：并发写同一目标文件时（4 线程
× 30 轮 = 120 次 replace，tests/test_atomic.py 里就是这个场景），Windows 上
持有目标句柄的常常是文件索引器/杀毒软件，句柄释放时间随机器负载浮动。最初
的 `(0.0, 0.01, 0.02)` 三档总预算只有 30ms，机器繁忙时必然有写炸的
（2026-09-27 全量回归实测偶发 `PermissionError(13, 'Access is denied')`），
一个偶发红会让整道回归门禁不可信。首次尝试成功时**不 sleep**，所以不 contended
的常见路径零额外开销。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

#: 递增退避（秒）。第一档 0.0 = 立即重试，命中不了才逐档加长。
_REPLACE_RETRY_DELAYS_S = (0.0, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.4)

#: Windows 上表示"句柄被别人占着"的 winerror：5=ACCESS_DENIED、
#: 32=SHARING_VIOLATION、33=LOCK_VIOLATION。Python 通常把它们映射成
#: `PermissionError`，但不是所有 Python/Windows 组合都映射得那么干净，
#: 所以显式按 winerror 也认一次（`errno` 在 Windows 上是系统通用码，判不准）。
_SHARING_WINERRORS = frozenset({5, 32, 33})


def _is_handle_busy(exc: OSError) -> bool:
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, "winerror", None) in _SHARING_WINERRORS


def _tmp_path_for(target: Path) -> Path:
    # 唯一临时名（pid+线程id）：同一目标文件可能被同进程多线程或宿主/worker
    # 双进程并发写入（index_progress 的心跳/进度就是真实场景）——固定 .tmp 名
    # 会让并发写互相踩踏半截内容。对齐旧 resource_arbiter/index_progress 的
    # 唯一命名先例。
    return target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def _replace_with_retry(tmp_path: Path, target: Path) -> None:
    last_error: OSError | None = None
    for delay in _REPLACE_RETRY_DELAYS_S:
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp_path, target)
            return
        except OSError as exc:
            # 只对"句柄被别人占着"重试。别的 OSError（磁盘满、只读文件系统、
            # 跨卷替换）重试没有意义，直接抛给调用方，别把真实故障拖成 0.8s
            # 的静默等待。
            if not _is_handle_busy(exc):
                raise
            last_error = exc
    assert last_error is not None
    raise last_error


def read_text_retry(path: Path, *, encoding: str = "utf-8") -> str:
    """读文本文件；对"句柄被别人占着"做短退避重试——写侧 `os.replace` 的**读侧对应物**。

    **为什么读侧也要重试**（2026-09-28 审计 M-1）：写侧原子替换靠 `_replace_with_retry`
    扛过 Windows 的瞬时 `PermissionError`，但读者恰好在替换的那一瞬间去打开目标文件，
    同样会撞上瞬时的 `PermissionError`。索引进度文件每秒被 worker 心跳写好几次，读侧
    不重试就把这次瞬时失败当成"没有状态"，`IndexWorkerManager.stop` 因此间歇性回一句
    "没有可停止的索引任务"——停止按钮随机失灵。

    与写侧同一套判定（`_is_handle_busy`）和同一份退避预算：
    - 文件不存在 → `FileNotFoundError` 立即抛，不重试（那是"没有"，不是"暂时读不了"）；
    - 不是句柄占用的其他 `OSError` → 立即抛；
    - 重试耗尽 → 抛最后一次异常（调用方决定"读不出来"怎么说，别把它说成"不存在"）。
    """
    last_error: OSError | None = None
    for delay in _REPLACE_RETRY_DELAYS_S:
        if delay:
            time.sleep(delay)
        try:
            return path.read_text(encoding=encoding)
        except FileNotFoundError:
            raise
        except OSError as exc:
            if not _is_handle_busy(exc):
                raise
            last_error = exc
    assert last_error is not None
    raise last_error


def _atomic_write(target: Path, write) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _tmp_path_for(target)
    try:
        write(tmp_path)
        _replace_with_retry(tmp_path, target)
    finally:
        # 写入或替换失败时不留半截 .tmp 残骸（目标文件本身不受影响）；
        # 只清自己这份唯一命名的临时文件，不碰并发写者的
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def atomic_write_text(target: Path, text: str, *, encoding: str = "utf-8") -> None:
    """把 `text` 原子写入 `target`：先写同目录 .tmp，再 `os.replace` 原子
    改名——读者要么看到完整的旧文件、要么看到完整的新文件，绝看不到半截。"""
    _atomic_write(target, lambda tmp: tmp.write_text(text, encoding=encoding))


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """`atomic_write_text` 的字节版本（归档/图片等二进制负载）。"""
    _atomic_write(target, lambda tmp: tmp.write_bytes(data))
