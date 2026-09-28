"""云端 OCR 提取与断点状态。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from core.atomic import atomic_write_bytes, atomic_write_text
from core.contracts import ExtractedDocument

from .ocr import MineruCloudError, _RealHttpClient

EXTRACTOR_VERSION = "0.3.0"
PLUGIN_ID = "official-ocr-mineru-cloud"
# 与 core/runtime.py:301 给插件的 ctx.logger 同名（rag_redo.plugin.<id>），
# 插件内直接取同名 logger，测试与宿主日志看到的是同一条通道。
_LOGGER = logging.getLogger(f"rag_redo.plugin.{PLUGIN_ID}")
# 每日额度预警线：官方每账号每日 1000 页最高优先级额度，超出降优先级但仍
# 处理（非拒绝）。阈值与文案对齐 obsidian-rag/index.py:2184-2187——计数只
# 用于接近额度时提醒"接下来会变慢"，不做成硬门禁（降级≠失败，阻断反制造问题）。
QUOTA_WARN_PAGES = 800
QUOTA_DAILY_PAGES = 1000


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fail(
    library_id: str,
    path: str,
    reason: str,
    content_hash: str = "",
    *,
    state: str | None = None,
) -> ExtractedDocument:
    return ExtractedDocument(
        library_id=library_id,
        path=path,
        text=None,
        failure_reason=reason,
        extracted_by=PLUGIN_ID,
        extractor_version=EXTRACTOR_VERSION,
        content_hash=content_hash,
        failure_state=state,
    )


class MineruCloudExtractor:
    def __init__(
        self,
        http_client=None,
        *,
        pending_path: Path | None = None,
        sidecar_dir: Path | None = None,
        quota_path: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = http_client if http_client is not None else _RealHttpClient()
        self._pending_path = pending_path
        self._sidecar_dir = sidecar_dir
        self._quota_path = quota_path
        self._logger = logger if logger is not None else _LOGGER
        self._pending_lock = threading.Lock()
        # 配额预警每轮只打一次（旧项目在并行云端段预检一次；REDO 串行逐文件
        # 提交，等价口径是每轮一次），由 reset_token_flag（轮开始）清零。
        self._quota_warned = False

    def _load_pending(self) -> dict:
        if self._pending_path is None or not self._pending_path.is_file():
            return {}
        try:
            data = json.loads(self._pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_pending(self, data: dict) -> None:
        if self._pending_path is None:
            return
        try:
            atomic_write_text(
                self._pending_path, json.dumps(data, ensure_ascii=False, indent=2)
            )
        except OSError:
            pass

    def _pending_match(self, path: str, content_hash: str) -> dict | None:
        with self._pending_lock:
            for batch_id, entry in self._load_pending().items():
                if isinstance(entry, dict) and entry.get("path") == path and entry.get("md5") == content_hash:
                    return {"batch_id": str(batch_id), **entry}
        return None

    def _pending_add(self, batch_id: str, path: str, content_hash: str) -> None:
        if self._pending_path is None:
            return
        with self._pending_lock:
            data = self._load_pending()
            data[str(batch_id)] = {"path": path, "md5": content_hash, "route": "ocr:mineru-cloud"}
            self._save_pending(data)

    def _pending_remove(self, batch_id: str) -> None:
        if self._pending_path is None:
            return
        with self._pending_lock:
            data = self._load_pending()
            if data.pop(str(batch_id), None) is not None:
                self._save_pending(data)

    def reset_token_flag(self) -> None:
        """轮开始钩子（core/pipeline.py:541-547 每轮索引对每个
        extractor:pdf 插件调用）。对齐 obsidian-rag/index.py:1874-1878：
        长驻进程跨轮次复用同一客户端，不复位的话 Token 失效标志一旦置位
        就锁死到进程重启，用户补好 Key 后重跑索引毫无反应。"""
        self._quota_warned = False
        reset = getattr(self._client, "reset_token_flag", None)
        if callable(reset):
            reset()

    @staticmethod
    def _file_digest(path: str) -> str:
        """磁盘上文件的当前字节指纹；读不到（已删/无权限/是目录）返回空串。"""
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            return ""
        return digest.hexdigest()

    def prune_pending(self) -> int:
        """清理在途簿记孤儿，返回**发现的**孤儿条数（对齐 obsidian-rag/
        index.py:2176-2177 每轮云端段开头调 mineru_pending_prune →
        extractors.py:923-934）。写盘失败时簿记保持原样，条数照样返回、
        另有 warning 说明——返回值是"发现了几条孤儿"，不是"删掉了几条"。

        孤儿 = 簿记条目指向的文件已不在磁盘上，或字节已变（服务器端结果
        即使完成也没有归宿，md5 对不上任何当前文件）。刻意不删的：文件
        仍在且字节没变的在途条目——服务器端任务独立于本进程存活，删了等于
        把已经付过的提交配额扔掉，下轮重新提交纯烧额度（这正是 LEGACY 把
        pending md5 并入存活集的原因）。

        与旧实现的可见差异（有意）：LEGACY 的存活集是"本轮待处理集合"
        （调度方手里的批量视图），REDO 串行逐文件提取拿不到本轮全貌，
        因此退化为"文件在且字节没变即视为可能仍在途"。保守方向相同——只
        会多留一条待下轮确认的条目，不会误删可续接的在途任务。"""
        if self._pending_path is None:
            return 0
        with self._pending_lock:
            data = self._load_pending()
            if not data:
                return 0
            digests: dict[str, str] = {}
            kept: dict = {}
            for batch_id, entry in data.items():
                path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
                recorded = str(entry.get("md5") or "") if isinstance(entry, dict) else ""
                if not path or not recorded:
                    # 结构损坏的条目同样没有归宿（续接时匹配不上任何东西）
                    continue
                if path not in digests:
                    digests[path] = self._file_digest(path)
                if digests[path] and digests[path] == recorded:
                    kept[batch_id] = entry
            removed = len(data) - len(kept)
            if not removed:
                return 0
            try:
                self._save_pending(kept)
            except OSError as exc:
                # 清理失败只记日志不抛：一次簿记清理失败不该让整轮索引崩掉
                # （对齐 extractors.py:885-886 的降级口径）
                self._logger.warning(
                    "mineru_pending 孤儿清理写失败（忽略，仅影响断点恢复）：%s", exc
                )
                return removed
            self._logger.info("清理 MinerU 在途簿记孤儿 %d 条", removed)
            return removed

    def _write_sidecar(self, content_hash: str, sidecar: bytes | None) -> None:
        if self._sidecar_dir is None or sidecar is None:
            return
        try:
            self._sidecar_dir.mkdir(parents=True, exist_ok=True)
            path = self._sidecar_dir / f"{content_hash}.json"
            atomic_write_bytes(path, sidecar)
        except OSError:
            pass

    def read_sidecar(self, content_hash: str) -> list | None:
        """读 MinerU 官方块标注 sidecar（问题48附记 v11 双轨，索引侧清洗用）。

        返回 content_list 的 list，或 None（无 sidecar / 损坏 / 非 list——
        老文件未重提故无 sidecar，索引侧走启发式回退）。绝不抛异常，
        对齐旧 extractors.read_cache_sidecar 契约。"""
        if self._sidecar_dir is None or not content_hash:
            return None
        try:
            path = self._sidecar_dir / f"{content_hash}.json"
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else None
        except (OSError, ValueError):
            return None

    def _quota_read(self) -> dict:
        """读今日累计 {date, files, pages}；无记录/跨天/损坏都视作零值
        （对齐 extractors.py:955-961 mineru_quota_today，只读不写）。"""
        today = time.strftime("%Y-%m-%d")
        data: object = {}
        if self._quota_path is not None and self._quota_path.is_file():
            try:
                data = json.loads(self._quota_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        if not isinstance(data, dict) or data.get("date") != today:
            return {"date": today, "files": 0, "pages": 0}
        return data

    def _quota_warn(self, pages: int) -> None:
        """提交前把"今日累计 + 本文件页数"投影一下，超过 800 页就提醒
        （对齐 obsidian-rag/index.py:2181-2187 的本轮投影预警）。

        为什么要有：官方每日 1000 页最高优先级额度用满后任务只是被降优先级、
        处理变慢，**不会失败**——用户只会感到"莫名变慢"，没有任何报错可循。
        日志里只出现页数，绝不出现 API Key（AGENTS.md §7）。"""
        if self._quota_path is None or self._quota_warned:
            return
        projected = int(self._quota_read().get("pages", 0)) + int(pages)
        if projected <= QUOTA_WARN_PAGES:
            return
        self._quota_warned = True
        self._logger.warning(
            "⚠ 本次约 %d 页送云端，今日累计将达 %d 页（>%d）：接近每日 %d 页"
            "最高优先级额度，后续任务可能被降优先级、处理变慢（仍会被处理，"
            "不会失败）。",
            int(pages), projected, QUOTA_WARN_PAGES, QUOTA_DAILY_PAGES,
        )

    def _quota_add(self, pages: int) -> None:
        if self._quota_path is None:
            return
        today = time.strftime("%Y-%m-%d")
        try:
            data = json.loads(self._quota_path.read_text(encoding="utf-8")) if self._quota_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict) or data.get("date") != today:
            data = {"date": today, "files": 0, "pages": 0}
        data["files"] = int(data.get("files", 0)) + 1
        data["pages"] = int(data.get("pages", 0)) + pages
        try:
            atomic_write_text(self._quota_path, json.dumps(data, ensure_ascii=False))
        except OSError:
            pass

    @staticmethod
    def _retry_call(call, attempts: int = 3):
        for attempt in range(attempts):
            try:
                return call()
            except MineruCloudError as exc:
                if not exc.retryable or attempt == attempts - 1:
                    raise
                time.sleep(0.05 * (2**attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def _page_count(path: Path) -> int:
        try:
            import pymupdf

            document = pymupdf.open(str(path))
            try:
                return int(document.page_count)
            finally:
                document.close()
        except Exception:
            return 0

    def extract(self, library_id: str, path: str, root: Path) -> ExtractedDocument:
        full_path = root / path
        if full_path.suffix.lower() != ".pdf":
            return _fail(library_id, path, "不是PDF，云端OCR跳过")
        try:
            data = full_path.read_bytes()
        except OSError as exc:
            return _fail(library_id, path, f"读取失败: {type(exc).__name__}: {exc}", state="unreadable")
        content_hash = _content_hash(data)
        if isinstance(self._client, _RealHttpClient) and not os.environ.get("MINERU_API_KEY"):
            return _fail(library_id, path, "scanned", content_hash, state="scanned")

        try:
            if not all(hasattr(self._client, name) for name in ("submit", "upload", "poll")):
                text = self._retry_call(lambda: self._client.ocr(data, full_path.name))
            else:
                token_check = getattr(self._client, "token_invalid", None)
                if token_check is not None and token_check():
                    # 同批前面的文件已发现 Token 失效（问题35）：本文件**本轮
                    # 跳过**，不能记终态。
                    # 终态范围对齐 obsidian-rag/index.py:2203-2207——被取消
                    # （尚未启动）的任务 `continue`，不落终态、不动 meta，
                    # 下一轮自然重试；只有真正触发失效的那一个文件经
                    # index.py:2218-2225 落 _terminal_entry(extract-failed)。
                    # 这里若回 extract-failed 就是把"本轮没轮上"伪装成稳定
                    # 终态：core/pipeline.py:682-701 的 stable_terminal 只看
                    # 能力签名（backend + Key 是否存在，都没变）→ 判
                    # action="unchanged"，用户补好额度后重跑索引毫无反应。
                    # AGENTS.md §5「瞬态服务不可用必须与永久失败区分」：
                    # 归 deferred（core/pipeline.py:871-890 消费：不落终态、
                    # 不动 manifest、保留旧条目与旧块，下轮重试）。
                    return _fail(library_id, path, "deferred", content_hash, state="deferred")
                pending = self._pending_match(str(full_path), content_hash)
                if pending is None:
                    submitted = self._retry_call(
                        lambda: self._client.submit(data, full_path.name, is_ocr=True)
                    )
                    self._retry_call(lambda: self._client.upload(submitted["upload_url"], data))
                    pages = self._page_count(full_path)
                    self._quota_warn(pages)
                    self._quota_add(pages)
                    self._pending_add(str(submitted["batch_id"]), str(full_path), content_hash)
                    batch_id = str(submitted["batch_id"])
                else:
                    batch_id = str(pending["batch_id"])
                text, status, sidecar = self._retry_call(
                    lambda: self._client.poll(batch_id, timeout=600.0)
                )
                if text is None:
                    if status in {"timeout", "download"}:
                        return _fail(library_id, path, "deferred", content_hash, state="deferred")
                    self._pending_remove(batch_id)
                    return _fail(
                        library_id,
                        path,
                        "extract-failed" if status != "empty" else "empty",
                        content_hash,
                        state="extract-failed" if status != "empty" else "empty",
                    )
                self._pending_remove(batch_id)
                self._write_sidecar(content_hash, sidecar)
        except MineruCloudError as exc:
            if exc.retryable or exc.token_invalid:
                # retryable：瞬态（429/5xx/网络）→ 本轮跳过，下轮重试；
                # token_invalid：本文件续接在途任务时 Key 恰好失效/被吊销
                # （A0202/A0211 或 401/403），同样不是"这份 PDF 提不出来"，
                # 不能记终态。断点簿记条目**保留**：服务器端任务还在，
                # Key 修好后按 batch_id 续接拿结果，不重复提交烧配额。
                return _fail(library_id, path, "deferred", content_hash, state="deferred")
            return _fail(library_id, path, f"extract-failed: {exc}", content_hash, state="extract-failed")
        except Exception as exc:
            return _fail(library_id, path, f"extract-failed: {type(exc).__name__}", content_hash, state="extract-failed")

        if not text.strip():
            return _fail(library_id, path, "empty", content_hash, state="empty")
        return ExtractedDocument(
            library_id=library_id,
            path=path,
            text=text,
            failure_reason=None,
            extracted_by=PLUGIN_ID,
            extractor_version=EXTRACTOR_VERSION,
            content_hash=content_hash,
        )
