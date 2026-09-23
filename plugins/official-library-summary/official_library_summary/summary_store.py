"""库简介的持久化存储——official-library-summary 插件自己的
`ctx.data_dir/library_summary/` 子目录，不碰 official-library-manager 的
存储（架构红线3：插件不直接读写持久化存储，一律通过自己的 data_dir 子
目录；数据流铁律4：库范围/库元数据的唯一权威判定在 library_manager，
这个插件只把 library_id 当一个不透明的 key 用，不重新判断"这个库存不
存在"——那个判断留给 core/pipeline.py 调用 library_manager 做，同
official-visual-wemm 用 library_id 做自己独立 collection 名字的先例）。

字段对齐旧项目 library.py 的 `_BLANK_SUMMARY`/`SUMMARY_SOURCES`/
`SUMMARY_MAX_CHARS`：source 三态（none=从未生成/ai=AI生成可被覆盖/
user=用户手写覆盖需门禁确认），逐字迁移这几个产品判断过的常量，不重新
发明。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

SUMMARY_MAX_CHARS = 300
SUMMARY_SOURCES = ("none", "ai", "user")
_BLANK = {"text": "", "source": "none", "updated_at": None, "fingerprint": None, "model": None}


class SummaryStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            # 文件不存在/损坏时安全降级为"没有任何库有简介"，不崩溃——
            # 同 core/runtime.py::_load_state 的"失败折叠成诚实终态"纪律。
            return {}

    def _save(self, data: dict) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def get(self, library_id: str) -> dict:
        """读侧防御：从没写过/字段缺失/source 非法一律回退成空白态，绝不
        让脏数据往上层传播成未处理异常。"""
        data = self._load()
        entry = data.get(library_id)
        if not isinstance(entry, dict):
            return dict(_BLANK)
        out = dict(_BLANK)
        out.update({k: entry.get(k) for k in _BLANK if k in entry})
        if out["source"] not in SUMMARY_SOURCES:
            out["source"] = "ai" if out.get("text") else "none"
        if not isinstance(out.get("text"), str):
            out["text"] = ""
        return out

    def set(
        self,
        library_id: str,
        text: str,
        source: str,
        *,
        fingerprint: str | None = None,
        model: str | None = None,
    ) -> dict:
        """唯一写入口——source 必须是 ai/user，none 只表示"未生成"，不
        通过这个方法写入（清空简介用这个方法写 text=""，source 仍需
        显式给 ai 或 user，不允许悄悄写成 none 状态）。"""
        if source not in ("ai", "user"):
            raise ValueError(f"非法 source：{source!r}（合法：ai/user）")
        text = text.strip()
        if len(text) > SUMMARY_MAX_CHARS:
            raise ValueError(f"简介超长（{len(text)} 字，上限 {SUMMARY_MAX_CHARS} 字）：请精简后再写入")
        data = self._load()
        entry = {"text": text, "source": source, "updated_at": time.time(), "fingerprint": fingerprint, "model": model}
        data[library_id] = entry
        self._save(data)
        return entry
