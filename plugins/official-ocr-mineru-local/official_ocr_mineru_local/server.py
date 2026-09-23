#!/usr/bin/env python3
"""official-ocr-mineru-local 的子进程服务端——单独进程运行。

故意只用标准库（不装 FastAPI/uvicorn 等第三方HTTP框架）：这样"子进程
生命周期+本机HTTP协议这一层能不能真的跑通"完全不受"有没有装什么框架"
影响，设计考虑同 ../../../core/subprocess_service.py 模块 docstring。

真正的本机OCR模型调用在 `_real_ocr()`，懒导入（这个沙盒环境刻意不装、
不下载真实模型——用户明确要求：VM磁盘空间有限，且插件架构本身应该是
"模型无关"的，不该为了验证插件能不能跑起来就强绑一个具体模型下载，见
plugin.py 模块 docstring）。`RAG_REDO_FAKE_OCR` 环境变量存在时用确定性
假实现，只给测试/架构验证用——生产环境绝不该设这个变量，设了也没意义
（真实用户会希望真的做OCR，不是拿到"[fake-ocr]"这种占位文本）。

**`/evict` 端点（2026-09-23 补：GPU 资源仲裁的软驱逐协议，和
official-visual-wemm/server.py 是同一套协议，供 plugin.py 的
resource_arbiter 抢占回调调用）目前是安全的空操作**——这个沙盒环境还
没有真实OCR引擎可卸载（`_real_ocr` 还没接入真实模型，见 TODO 第2条），
先把协议端点占住，接入真实模型时在这里补真正的"卸载引擎/释放显存"逻辑
（结构对齐 official-visual-wemm/server.py 的 `_unload_engine_locked`/
`_check_idle_unload`/两级空闲释放），不是遗漏，是"先把协议打通、模型
到位后再补真正的生命周期管理"这个已知顺序的一部分。
"""
from __future__ import annotations

import http.server
import json
import os
import sys
from pathlib import Path


def _real_ocr(full_path: Path) -> str:
    try:
        import mineru  # noqa: F401  # 真正的本机OCR依赖，占位——具体选型见README，这个沙盒环境未安装
    except ImportError as exc:
        raise RuntimeError("本机OCR依赖(mineru)未安装——见 README 的安装说明") from exc
    raise RuntimeError("真实本机OCR调用尚未接入（这一步要等依赖真的装好、且有真实模型权重后再实现）")


def _fake_ocr(full_path: Path) -> str:
    return f"[fake-ocr] {full_path.name} 的识别结果（仅测试/架构验证用，不是真实OCR文字）"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/evict":
            # 见模块 docstring："安全的空操作"——没有真实引擎可卸载，回 ok
            # 让抢占方的 fail-open 语义正常工作，不是假装做了什么。
            self._json(200, {"ok": True})
            return
        if self.path != "/extract":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        full_path = Path(payload.get("root", "")) / payload.get("path", "")
        try:
            if not full_path.exists():
                raise FileNotFoundError(str(full_path))
            if os.environ.get("RAG_REDO_FAKE_OCR"):
                text = _fake_ocr(full_path)
            else:
                text = _real_ocr(full_path)
            self._json(200, {"text": text, "failure_reason": None})
        except Exception as exc:  # noqa: BLE001 - 子进程这一侧也不能让异常直接炸掉HTTP响应
            self._json(200, {"text": None, "failure_reason": f"{type(exc).__name__}: {exc}"})

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass  # 静默，避免污染核心进程的 stdout/stderr


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1])
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
