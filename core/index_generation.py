from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


INDEX_MANIFEST_VERSION = 1


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
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
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
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def list_generations(self, library_id: str) -> list[str]:
        directory = self._root / self._key(library_id)
        if not directory.is_dir():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))

    def clear(self, library_id: str, generation: str) -> None:
        try:
            self._path_for(library_id, generation).unlink(missing_ok=True)
        except OSError:
            pass

    def referenced_generations(self, library_id: str, generations: list[str] | tuple[str, ...]) -> set[str]:
        referenced: set[str] = set()
        for generation in generations:
            manifest = self.read(library_id, generation)
            if manifest is None:
                continue
            for field in ("vector_segments", "extract_segments", "lexical_segments"):
                values = manifest.get(field, [])
                if isinstance(values, list):
                    referenced.update(str(value) for value in values if value)
        return referenced
