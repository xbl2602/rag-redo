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
踩到过的（见 core/index_progress.py 同款退避重试）；这里把"3 次短退避
重试"收敛进助手，调用方不需要各自复制。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

_REPLACE_RETRY_DELAYS_S = (0.0, 0.01, 0.02)


def _replace_with_retry(tmp_path: Path, target: Path) -> None:
    last_error: OSError | None = None
    for delay in _REPLACE_RETRY_DELAYS_S:
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp_path, target)
            return
        except PermissionError as exc:  # Windows 句柄延迟释放，见模块 docstring
            last_error = exc
    assert last_error is not None
    raise last_error


def _atomic_write(target: Path, write) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(target.name + ".tmp")
    try:
        write(tmp_path)
        _replace_with_retry(tmp_path, target)
    finally:
        # 写入或替换失败时不留半截 .tmp 残骸（目标文件本身不受影响）
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
