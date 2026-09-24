"""core/extract_cache.py — 提取结果缓存（核心服务，不是插件）。

**补的是哪个坑**：2026-09-23 全面功能审计发现，`index_library()` 里
`ExtractedDocument.text` 只在切块前一闪而过——切完块立刻丢弃，从不落盘。
这导致索引完成之后**没有任何办法**再问"这篇文档完整提取出来是什么样"，
obsidian-rag 的 `read_document`（读某文档的完整提取正文）、`find_duplicates`
（近似重复检测——按整篇提取文本的 MinHash 比较，不是按 chunk 片段）两个
MCP 工具都依赖这个能力，rag-redo 此前完全没有。

**为什么是核心服务**：这份缓存由编排层（`core/pipeline.py::index_library()`）
在索引过程中产出、又由编排层（`read_document`/`find_duplicates` 走的
`core/pipeline.py` 方法）消费——两头都是核心自己，不是"两个互不知情的
插件需要中立裁判"的场景，但因为 `core/pipeline.py` 本身不是插件、没有
`ctx` 可用、也不该直接管理原始文件 I/O 细节，所以拆成一个独立的小核心
模块（同 `core/settings.py`/`core/write_gate.py` 的既有先例：核心自己的
状态由核心自己的模块管理，不通过 `DataStore`——那个类的 docstring 自己
写明是 Phase 0 占位，见 `core/settings.py` 模块 docstring 同一条理由）。

**增量缓存语义**：`core/index_generation.py::IndexManifestStore` 保存每个文件当前有效的提取阶段签名和缓存 segment 链。源文件、extractor 链或模型阶段变化时，Pipeline 只重写变化文件；读取时按清单状态从最新 segment 查旧提取正文，失败文件不会回退到已失效的旧正文。每轮 compaction 会把仍有效正文复制到单一 segment，并清理不再被当前 generation 引用的旧缓存。

**文件名用路径哈希而不是转义后的原始路径**：库内相对路径可能又长又含
中文/特殊字符（比如"20-Projects/机器学习论文合集/2024年最新进展详细
笔记.md"），转义成安全文件名后长度不可控，真实可能撞上 Windows 单个
路径分量/MAX_PATH 的限制。改用固定长度的路径哈希做文件名，另外维护
一份 `_index.json`（hash→原始相对路径）供反查，不依赖"从文件名猜回
原路径"这种脆弱的方式。
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse
from pathlib import Path

from .atomic import atomic_write_text


def _hash_for(rel_path: str) -> str:
    return hashlib.sha256(rel_path.encode("utf-8")).hexdigest()


class ExtractCache:
    """一个库一个缓存目录：`root/<library_id>/<路径哈希>.txt` + 同目录下
    `_index.json`（hash→原始相对路径，供 `list_relative_paths` 反查）。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _dir_for(self, library_id: str, generation: str | None = None) -> Path:
        if generation:
            return self._root / library_id / generation
        return self._root / library_id

    def _text_path(self, library_id: str, rel_path: str, generation: str | None = None) -> Path:
        return self._dir_for(library_id, generation) / f"{_hash_for(rel_path)}.txt"

    def _route_suffix(self, route: str) -> str:
        return urllib.parse.quote(route, safe="-_.")

    def _route_text_path(
        self,
        library_id: str,
        rel_path: str,
        route: str,
        generation: str | None = None,
    ) -> Path:
        return self._dir_for(library_id, generation) / (
            f"{_hash_for(rel_path)}.{self._route_suffix(route)}.txt"
        )

    def _index_path(self, library_id: str, generation: str | None = None) -> Path:
        return self._dir_for(library_id, generation) / "_index.json"

    def _load_index(self, library_id: str, generation: str | None = None) -> dict[str, str]:
        path = self._index_path(library_id, generation)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_index(self, library_id: str, index: dict[str, str], generation: str | None = None) -> None:
        atomic_write_text(
            self._index_path(library_id, generation),
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True),
        )

    def write(
        self,
        library_id: str,
        rel_path: str,
        text: str,
        generation: str | None = None,
        route: str | None = None,
    ) -> None:
        self._dir_for(library_id, generation).mkdir(parents=True, exist_ok=True)
        target = (
            self._route_text_path(library_id, rel_path, route, generation)
            if route
            else self._text_path(library_id, rel_path, generation)
        )
        # 原子写：read_document 读到的正文绝不能是断电留下的半截文本
        atomic_write_text(target, text)
        index = self._load_index(library_id, generation)
        index[_hash_for(rel_path)] = rel_path
        self._save_index(library_id, index, generation)

    def read(
        self,
        library_id: str,
        rel_path: str,
        generation: str | None = None,
        route: str | None = None,
    ) -> str | None:
        path = (
            self._route_text_path(library_id, rel_path, route, generation)
            if route
            else self._text_path(library_id, rel_path, generation)
        )
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def read_preferred(
        self,
        library_id: str,
        rel_path: str,
        routes: tuple[str, ...] | list[str],
        generation: str | None = None,
    ) -> str | None:
        for route in routes:
            text = self.read(library_id, rel_path, generation, route)
            if text is not None:
                return text
        return None

    def iter_entries(
        self,
        library_id: str,
        rel_path: str,
        generation: str | None,
    ):
        directory = self._dir_for(library_id, generation)
        prefix = f"{_hash_for(rel_path)}."
        for path in sorted(directory.glob(f"{prefix}*.txt")) if directory.is_dir() else ():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            route: str | None = None
            if path.name != f"{_hash_for(rel_path)}.txt":
                route = urllib.parse.unquote(path.stem.split(".", 1)[1])
            yield text, route

    def read_any(
        self,
        library_id: str,
        rel_path: str,
        generation: str | None = None,
    ) -> str | None:
        for text, _route in self.iter_entries(library_id, rel_path, generation):
            return text
        return None

    def export_state(self, library_id: str, generation: str) -> dict:
        state: dict[str, list[dict[str, str | None]]] = {}
        for path in self.list_relative_paths(library_id, generation):
            entries: list[dict[str, str | None]] = []
            for text, route in self.iter_entries(library_id, path, generation):
                entries.append({"text": text, "route": route})
            if entries:
                state[path] = entries
        return state

    def import_state(
        self,
        library_id: str,
        state: dict,
        generation: str,
    ) -> None:
        for path, entries in state.items():
            if not isinstance(path, str) or not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                    continue
                route = entry.get("route")
                self.write(
                    library_id,
                    path,
                    entry["text"],
                    generation=generation,
                    route=str(route) if route else None,
                )

    def clear_library(self, library_id: str, generation: str | None = None) -> None:
        """整库重建索引前先清空——避免被删除/排除出检索范围的文件在缓存
        里留下陈旧正文（`read_document`/`find_duplicates` 会一直"看得到"
        一份用户以为已经移除的文件，属于真实的用户可感知不一致，不是
        无关紧要的内部状态）。幂等：目录本来就不存在也不报错。"""
        import shutil

        library_dir = self._dir_for(library_id, generation)
        if library_dir.is_dir():
            shutil.rmtree(library_dir)

    def list_relative_paths(self, library_id: str, generation: str | None = None) -> list[str]:
        """列出这个库当前缓存里有正文的全部相对路径——`find_duplicates`
        用这个枚举"要比较哪些文件"，不需要重新问 library_manager 一遍
        （那会包含还没成功提取过的文件，这里只关心"确实有正文可比较"
        的子集）。"""
        return sorted(self._load_index(library_id, generation).values())
