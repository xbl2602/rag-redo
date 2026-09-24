"""云端 OCR 提取与断点状态。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from core.atomic import atomic_write_bytes, atomic_write_text
from core.contracts import ExtractedDocument

from .ocr import MineruCloudError, _RealHttpClient

EXTRACTOR_VERSION = "0.3.0"
PLUGIN_ID = "official-ocr-mineru-cloud"


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
    ) -> None:
        self._client = http_client if http_client is not None else _RealHttpClient()
        self._pending_path = pending_path
        self._sidecar_dir = sidecar_dir
        self._quota_path = quota_path
        self._pending_lock = threading.Lock()

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

    def _write_sidecar(self, content_hash: str, sidecar: bytes | None) -> None:
        if self._sidecar_dir is None or sidecar is None:
            return
        try:
            self._sidecar_dir.mkdir(parents=True, exist_ok=True)
            path = self._sidecar_dir / f"{content_hash}.json"
            atomic_write_bytes(path, sidecar)
        except OSError:
            pass

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
                pending = self._pending_match(str(full_path), content_hash)
                if pending is None:
                    submitted = self._retry_call(
                        lambda: self._client.submit(data, full_path.name, is_ocr=True)
                    )
                    self._retry_call(lambda: self._client.upload(submitted["upload_url"], data))
                    self._quota_add(self._page_count(full_path))
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
            if exc.retryable:
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
