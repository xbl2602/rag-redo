"""MinerU 云端 OCR HTTP 客户端。"""
from __future__ import annotations

import io
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque

# 提交错误类别（问题35，按 mineru.net 官方错误码表分类）——逐字对齐旧
# extractors.py:732-763。不能一套重试逻辑应付所有情况：
#   token    → Token 错误/过期（A0202/A0211）→ 置全局失效标志停整批；
#   fatal    → 格式/空文件/超限 → 重试无意义，立即失败；
#   transient→ 服务异常/队列满/429 → 值得按退避重试。
_TOKEN_CODES = frozenset({"A0202", "A0211"})
_FATAL_CODES = frozenset({"-60002", "-60004", "-60005", "-60006"})
_TRANSIENT_CODES = frozenset({"-10001", "-60007", "-60009"})


def _classify_mineru_code(code: object) -> str:
    """官方错误码 → 提交错误类别。未知码按可重试处理（宁可多试一次，不误杀）。"""
    c = str(code).upper()
    if c in _TOKEN_CODES:
        return "token"
    if c in _FATAL_CODES:
        return "fatal"
    if c in _TRANSIENT_CODES or c == "429":
        return "transient"
    return "transient"


_NO_KEY_MESSAGE = "缺少 MinerU API Key（请在设置页填写，或设置 MINERU_API_KEY 环境变量）"


def resolve_api_key(settings=None) -> str:
    """MinerU 云端 API Key：**设置页的 `mineru_api_key` 优先，环境变量 `MINERU_API_KEY` 兜底**。

    旧项目 obsidian-rag 的 Key 存在配置里、设置页可填（gui/config_editor.py 的保密字段；
    extractors.py:1239 读它）；rag-redo 此前只认环境变量，只用界面的人没有地方填。现在
    两处都认，此前靠环境变量配置的用法不受影响。每次调用现读，不缓存：设置页改了立刻生效。
    返回去掉首尾空白后的值，都没配返回空串。本函数的返回值绝不写进日志/异常。"""
    value = ""
    if settings is not None:
        value = str(settings.get("mineru_api_key", "") or "").strip()
    return value or str(os.environ.get("MINERU_API_KEY", "") or "").strip()


def resolve_model_version(settings=None) -> str:
    """云端解析模型版本（vlm | pipeline）：设置页 `mineru_model_version` 优先，环境变量
    `MINERU_MODEL_VERSION` 兜底，默认 vlm（旧项目 config.py 的默认值）。"""
    value = ""
    if settings is not None:
        value = str(settings.get("mineru_model_version", "vlm") or "").strip()
    if value and value != "vlm":
        return value
    return str(os.environ.get("MINERU_MODEL_VERSION", "") or "").strip() or value or "vlm"


class MineruCloudError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        token_invalid: bool = False,
        gone: bool = False,
        kind: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.token_invalid = token_invalid
        # gone=True：远端任务确定不存在/响应不可解析（HTTP 404、非 JSON）——
        # 对齐 obsidian-rag 问题36 的决策：轮询遇 404/非 JSON 判 gone 并清除
        # 断点簿记，堵"永久续接一个不存在的任务"的死循环。
        self.gone = gone
        # kind：官方错误码类别（token/fatal/transient，旧 _MineruSubmitError
        # 的同类语义）；retry_after：429 响应头要求等待的秒数
        self.kind = kind
        self.retry_after = retry_after


class _RealHttpClient:
    def __init__(
        self,
        endpoint: str = "https://mineru.net/api/v4",
        *,
        rate_per_minute: int = 45,
        settings=None,
    ) -> None:
        self._settings = settings
        self.endpoint = endpoint.rstrip("/")
        self._rate_per_minute = max(0, int(rate_per_minute))
        self._submit_times: deque[float] = deque()
        self._submit_lock = threading.Lock()
        # Token 失效全局标志（问题35）：A0202/A0211 一旦出现，同一批后续
        # 请求大概率全部失败，置位后提交快速失败。每轮索引云端段开始时
        # 由调用方 reset_token_flag() 归零（长驻进程跨轮次复用）。
        self._token_invalid = threading.Event()

    def _api_key(self) -> str:
        return resolve_api_key(self._settings)

    def has_key(self) -> bool:
        """是否配了 Key（设置页或环境变量）；每次现读。"""
        return bool(self._api_key())

    def token_invalid(self) -> bool:
        return self._token_invalid.is_set()

    def reset_token_flag(self) -> None:
        self._token_invalid.clear()

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
        api_key = self._api_key()
        if not api_key:
            raise MineruCloudError(_NO_KEY_MESSAGE)
        if self._token_invalid.is_set():
            # 同批已有任务发现 Token 失效：本文件不发请求直接失败（问题35
            # ——重试只会烧频控配额；调度方/后续任务据此快速失败）
            raise MineruCloudError("Token 已失效，跳过提交", retryable=False)
        body = json.dumps(
            {
                "enable_formula": True,
                "enable_table": True,
                "config": {"mineru_model_version": resolve_model_version(self._settings)},
                "files": [{"name": filename, "is_ocr": bool(is_ocr), "data_id": "doc"}],
            }
        ).encode("utf-8")
        # 提交重试（问题35）：1 次首发 + 3 次重试（退避 1/2/4s + 抖动；
        # 429 尊重 Retry-After）。transient（网络异常/429/服务异常类错误码）
        # 才重试；fatal（超限/格式/空文件）与 token 立即上抛。
        last_error: MineruCloudError | None = None
        for attempt in range(1, 5):
            try:
                return self._submit_once(body, api_key)
            except MineruCloudError as exc:
                last_error = exc
                if exc.kind == "token":
                    # A0202/A0211：置全局失效标志——同批后续请求大概率全部失败，
                    # 后续提交据此快速失败，不再为注定失败的请求烧频控配额
                    # （旧 _mineru_submit 在 except 分支置位，1190）
                    self._token_invalid.set()
                if exc.kind != "transient" or attempt == 4:
                    raise
                time.sleep(exc.retry_after if exc.retry_after else min(2 ** (attempt - 1), 4) + random.random() * 0.5)
        raise last_error if last_error else MineruCloudError("任务提交失败")

    def _submit_once(self, body: bytes, api_key: str) -> dict:
        self._gate()
        request = urllib.request.Request(
            f"{self.endpoint}/file-urls/batch",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        payload = self._json_request(request, timeout=30.0)
        code = payload.get("code", 200)
        if code not in (0, 200):
            kind = _classify_mineru_code(code)
            raise MineruCloudError(
                f"任务提交异常 code={code}", retryable=(kind == "transient"), kind=kind,
            )
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
        api_key = self._api_key()
        if not api_key:
            raise MineruCloudError(_NO_KEY_MESSAGE)
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
