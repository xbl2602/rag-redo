"""MinerU 云端 OCR HTTP 客户端。"""
from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque


class MineruCloudError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        token_invalid: bool = False,
        gone: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.token_invalid = token_invalid
        # gone=True：远端任务确定不存在/响应不可解析（HTTP 404、非 JSON）——
        # 对齐 obsidian-rag 问题36 的决策：轮询遇 404/非 JSON 判 gone 并清除
        # 断点簿记，堵"永久续接一个不存在的任务"的死循环。
        self.gone = gone


class _RealHttpClient:
    def __init__(
        self,
        endpoint: str = "https://mineru.net/api/v4",
        *,
        rate_per_minute: int = 45,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._rate_per_minute = max(0, int(rate_per_minute))
        self._submit_times: deque[float] = deque()
        self._submit_lock = threading.Lock()

    def _gate(self) -> None:
        if not self._rate_per_minute:
            return
        while True:
            with self._submit_lock:
                now = time.monotonic()
                while self._submit_times and self._submit_times[0] <= now - 60:
                    self._submit_times.popleft()
                if len(self._submit_times) < self._rate_per_minute:
                    self._submit_times.append(now)
                    return
                delay = 60 - (now - self._submit_times[0])
            time.sleep(max(0.01, min(delay, 1.0)))

    def _json_request(self, request: urllib.request.Request, timeout: float) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            token_invalid = exc.code in {401, 403}
            raise MineruCloudError(
                f"HTTP {exc.code}",
                retryable=retryable,
                token_invalid=token_invalid,
                gone=exc.code == 404,
            ) from exc
        except urllib.error.URLError as exc:
            raise MineruCloudError(f"网络错误: {type(exc).__name__}", retryable=True) from exc
        except (TimeoutError, OSError) as exc:
            raise MineruCloudError(f"网络错误: {type(exc).__name__}", retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise MineruCloudError("响应不是合法JSON", gone=True) from exc
        if not isinstance(payload, dict):
            raise MineruCloudError("响应不是JSON对象")
        return payload

    def submit(self, file_bytes: bytes, filename: str, *, is_ocr: bool) -> dict:
        api_key = os.environ.get("MINERU_API_KEY")
        if not api_key:
            raise MineruCloudError("缺少 MINERU_API_KEY 环境变量")
        self._gate()
        body = json.dumps(
            {
                "enable_formula": True,
                "enable_table": True,
                "config": {"mineru_model_version": os.environ.get("MINERU_MODEL_VERSION", "vlm")},
                "files": [{"name": filename, "is_ocr": bool(is_ocr), "data_id": "doc"}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/file-urls/batch",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        payload = self._json_request(request, timeout=30.0)
        data = payload.get("data") or {}
        batch_id = data.get("batch_id")
        urls = data.get("file_urls") or []
        if not batch_id or not urls or not urls[0]:
            raise MineruCloudError("提交响应缺少batch_id或上传地址")
        return {"batch_id": str(batch_id), "upload_url": str(urls[0])}

    def upload(self, upload_url: str, file_bytes: bytes) -> None:
        request = urllib.request.Request(upload_url, data=file_bytes, method="PUT")
        try:
            with urllib.request.urlopen(request, timeout=120.0):
                return
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            raise MineruCloudError(f"上传HTTP {exc.code}", retryable=retryable) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MineruCloudError(f"上传网络错误: {type(exc).__name__}", retryable=True) from exc

    def poll(self, batch_id: str, *, timeout: float = 600.0) -> tuple[str | None, str, bytes | None]:
        api_key = os.environ.get("MINERU_API_KEY")
        if not api_key:
            raise MineruCloudError("缺少 MINERU_API_KEY 环境变量")
        deadline = time.monotonic() + max(30.0, timeout)
        while time.monotonic() < deadline:
            request = urllib.request.Request(
                f"{self.endpoint}/extract-results/batch/{batch_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            try:
                payload = self._json_request(request, timeout=30.0)
            except MineruCloudError as exc:
                if exc.gone:
                    return None, "gone", None
                if exc.retryable:
                    time.sleep(1.0)
                    continue
                raise
            code = payload.get("code")
            if code not in (0, 200):
                return None, "gone", None
            results = (payload.get("data") or {}).get("extract_result") or []
            state = results[0].get("state") if results else "pending"
            if state in {"failed", "error"}:
                return None, "failed", None
            if state == "done":
                zip_url = results[0].get("full_zip_url")
                if not zip_url:
                    return None, "download", None
                return self._download(str(zip_url))
            time.sleep(2.0)
        return None, "timeout", None

    def _download(self, url: str) -> tuple[str | None, str, bytes | None]:
        try:
            with urllib.request.urlopen(url, timeout=120.0) as response:
                archive = zipfile.ZipFile(io.BytesIO(response.read()))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, zipfile.BadZipFile) as exc:
            return None, "download", None
        markdown = [info for info in archive.infolist() if info.filename.lower().endswith(".md")]
        if not markdown:
            return None, "no-md", None
        selected = max(markdown, key=lambda info: info.file_size)
        text = archive.read(selected).decode("utf-8", "replace")
        if not text.strip():
            return None, "empty", None
        sidecar = next(
            (archive.read(info) for info in archive.infolist() if info.filename.lower().endswith("_content_list.json")),
            None,
        )
        return text, "done", sidecar

    def ocr(self, file_bytes: bytes, filename: str) -> str:
        result = self.submit(file_bytes, filename, is_ocr=True)
        self.upload(result["upload_url"], file_bytes)
        text, status, _sidecar = self.poll(result["batch_id"])
        if text is not None:
            return text
        raise MineruCloudError(f"云端任务未完成: {status}", retryable=status in {"timeout", "download"})
