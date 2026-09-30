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
import os
import re
import time
import urllib.parse
from pathlib import Path

from .atomic import atomic_write_text

#: “转换暂存”放在每个库缓存目录下的这个子目录里（下划线开头，不会与 uuid 形式的 generation 撞名）。
STASH_DIR = "_stash"
#: 暂存条目超过这么久没被用上就当孤儿清掉（文件后来改了、删了或被排除出索引范围）。
STASH_MAX_AGE_SECONDS = 30 * 24 * 3600
_CONTENT_HASH_RE = re.compile(r"[0-9a-f]{16,128}")


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
        path = self.locate_path(library_id, rel_path, route, generation)
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

    def locate_path(
        self,
        library_id: str,
        rel_path: str,
        route: str | None = None,
        generation: str | None = None,
    ) -> Path:
        """这份正文缓存**应该**在的文件位置（不检查存不存在）。与 `read()` 同一套命名规则——
        “转换缓存”清单（BC-19）靠它告诉用户“存在哪、多大”，不另起一套猜文件名的逻辑。"""
        if route:
            return self._route_text_path(library_id, rel_path, route, generation)
        return self._text_path(library_id, rel_path, generation)

    def library_dir(self, library_id: str) -> Path:
        """这个库的转文字缓存文件夹（各轮 generation 的子文件夹、转换暂存都在它下面）。"""
        return self._dir_for(library_id)

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

    # ---- 转换暂存（2026-09-29 操作者确认）-------------------------------------
    # 上面的缓存按“轮次”（generation）存：一轮索引被停止、出错或进程被杀，这一轮没发布，
    # 它的目录随之丢弃——里面已经转好的 PDF/DOCX 正文也一起没了，下一轮得重新送 MinerU。
    # 2026-09-29 真机：停在 Y2S1 的第 24 个文件时，20 份已解析好的扫描件（约 4 分钟 MinerU
    # 工作）就这样丢了。暂存区按“文件内容指纹 + 产出它的转换器与版本”存，转好一份立刻落盘，
    # 不跟轮次走：内容没变、转换器没升级，下一轮直接拿来用；成功发布后已经用上的条目清掉。

    def _stash_path(self, library_id: str, content_hash: str, route: str) -> Path:
        if not _CONTENT_HASH_RE.fullmatch(content_hash or ""):
            raise ValueError("content_hash 必须是十六进制内容指纹")
        return self._root / library_id / STASH_DIR / f"{content_hash}.{self._route_suffix(route)}.txt"

    def write_stash(self, library_id: str, content_hash: str, route: str, text: str) -> None:
        target = self._stash_path(library_id, content_hash, route)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, text)

    def read_stash(
        self,
        library_id: str,
        content_hash: str,
        routes: tuple[str, ...] | list[str],
    ) -> tuple[str, str] | None:
        """按 `routes` 的先后找这份内容的暂存正文，返回（正文，产出它的路由）。路由里带
        转换器版本，所以转换器升级后旧暂存自然查不到。"""
        if not _CONTENT_HASH_RE.fullmatch(content_hash or ""):
            return None
        for route in routes:
            path = self._stash_path(library_id, content_hash, route)
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                os.utime(path)  # 还在被需要：刷新“最近用到”的时间，别被当孤儿过期清掉
            except OSError:
                pass
            return text, route
        return None

    def prune_stash(
        self,
        library_id: str,
        *,
        settled_hashes: set[str],
        max_age_seconds: float = STASH_MAX_AGE_SECONDS,
        now: float | None = None,
    ) -> int:
        """清掉已经没用的暂存条目：内容已在刚发布的索引里落定（入库或终态）的，以及太久
        没被用上的孤儿。其余（这一轮延后的、被 Agent 授权范围排除没处理到的）留着。
        返回删掉的条目数；单个文件删不掉就跳过。"""
        directory = self._root / library_id / STASH_DIR
        if not directory.is_dir():
            return 0
        cutoff = (time.time() if now is None else now) - max_age_seconds
        removed = 0
        for path in directory.glob("*.txt"):
            content_hash = path.name.split(".", 1)[0]
            try:
                stale = content_hash in settled_hashes or path.stat().st_mtime < cutoff
                if stale:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def list_relative_paths(self, library_id: str, generation: str | None = None) -> list[str]:
        """列出这个库当前缓存里有正文的全部相对路径——`find_duplicates`
        用这个枚举"要比较哪些文件"，不需要重新问 library_manager 一遍
        （那会包含还没成功提取过的文件，这里只关心"确实有正文可比较"
        的子集）。"""
        return sorted(self._load_index(library_id, generation).values())
