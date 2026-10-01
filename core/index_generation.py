from __future__ import annotations

import hashlib
import json
import os
import re
import threading

from .atomic import atomic_write_text
from pathlib import Path


INDEX_MANIFEST_VERSION = 1

#: 清单里记着“还在用哪几段数据”的三个字段（回收旧数据时只看这三个）。
SEGMENT_FIELDS = ("vector_segments", "extract_segments", "lexical_segments")

#: 每份清单的分段引用，按“文件路径 → (文件号, 修改时间, 大小, 分段集合)”记在进程里。清单
#: 一份 generation 一个文件、写好就不再改（写是“先写临时文件再换上”，换上就是另一个文件号）；
#: 三样里有一样不同就重读，读不了的不记。
#: 2026-10-01 真机：每个库每轮收尾都要把所有库的全部清单（Y2S1 一份 15MB）各读一遍、
#: 只为看这三个字段，四个库一轮光这个就 2.5 秒；同一个 worker 接着做下一个库时它们都没变。
_SEGMENT_CACHE: dict[str, tuple[tuple[int, int, int], frozenset[str]]] = {}
_SEGMENT_CACHE_LOCK = threading.Lock()


class IndexGenerationStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _key(self, library_id: str) -> str:
        safe = re.sub(r"[^\w.-]", "_", library_id)[:48] or "library"
        digest = hashlib.sha256(library_id.encode("utf-8")).hexdigest()[:16]
        return f"{safe}-{digest}"

    def _path_for(self, library_id: str) -> Path:
        return self._root / f"{self._key(library_id)}.json"

    def _read(self, library_id: str) -> dict:
        try:
            data = json.loads(self._path_for(library_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def active(self, library_id: str) -> str | None:
        generation = self._read(library_id).get("active")
        return str(generation) if isinstance(generation, str) and generation else None

    def history(self, library_id: str) -> list[str]:
        history = self._read(library_id).get("history", [])
        if not isinstance(history, list):
            return []
        return [str(item) for item in history if isinstance(item, str) and item]

    def active_pairs(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if not self._root.is_dir():
            return result
        for path in self._root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            library_id = data.get("library_id")
            generation = data.get("active")
            if isinstance(library_id, str) and library_id and isinstance(generation, str) and generation:
                result[library_id] = generation
        return result

    def commit(self, library_id: str, generation: str) -> bool:
        if not generation:
            return False
        path = self._path_for(library_id)
        previous = self.active(library_id)
        payload = {
            "library_id": library_id,
            "active": generation,
            "previous": previous,
            "history": [previous] if previous else [],
            "committed_pid": os.getpid(),
        }
        try:
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
            return True
        except OSError:
            return False


class IndexManifestStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _key(self, library_id: str) -> str:
        safe = re.sub(r"[^\w.-]", "_", library_id)[:48] or "library"
        digest = hashlib.sha256(library_id.encode("utf-8")).hexdigest()[:16]
        return f"{safe}-{digest}"

    def _path_for(self, library_id: str, generation: str) -> Path:
        return self._root / self._key(library_id) / f"{generation}.json"

    def read(self, library_id: str, generation: str | None) -> dict | None:
        if not generation:
            return None
        path = self._path_for(library_id, generation)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("format_version") != INDEX_MANIFEST_VERSION:
            return None
        return data

    def write(self, manifest: dict) -> bool:
        library_id = str(manifest.get("library_id") or "")
        generation = str(manifest.get("generation") or "")
        if not library_id or not generation or manifest.get("format_version") != INDEX_MANIFEST_VERSION:
            return False
        path = self._path_for(library_id, generation)
        try:
            atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2))
            return True
        except OSError:
            return False

    def list_generations(self, library_id: str) -> list[str]:
        directory = self._root / self._key(library_id)
        if not directory.is_dir():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))

    def clear(self, library_id: str, generation: str) -> None:
        path = self._path_for(library_id, generation)
        with _SEGMENT_CACHE_LOCK:
            _SEGMENT_CACHE.pop(str(path), None)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def segments(self, library_id: str, generation: str | None) -> frozenset[str] | None:
        """这份清单引用的全部分段（`SEGMENT_FIELDS` 三个字段的并集）；没有这份清单、读不了
        或版本不对时是 None（与 `read` 同口径）。按文件号、修改时间和大小缓存，见 `_SEGMENT_CACHE`。"""
        if not generation:
            return None
        path = self._path_for(library_id, generation)
        try:
            stat = path.stat()
        except OSError:
            return None
        key = str(path)
        stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        with _SEGMENT_CACHE_LOCK:
            cached = _SEGMENT_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        manifest = self.read(library_id, generation)
        if manifest is None:
            return None
        values: set[str] = set()
        for field in SEGMENT_FIELDS:
            listed = manifest.get(field, [])
            if isinstance(listed, list):
                values.update(str(value) for value in listed if value)
        result = frozenset(values)
        with _SEGMENT_CACHE_LOCK:
            _SEGMENT_CACHE[key] = (stamp, result)
        return result

    def referenced_generations(self, library_id: str, generations: list[str] | tuple[str, ...]) -> set[str]:
        referenced: set[str] = set()
        for generation in generations:
            segments = self.segments(library_id, generation)
            if segments is not None:
                referenced.update(segments)
        return referenced
