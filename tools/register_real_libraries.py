# -*- coding: utf-8 -*-
"""实测准备：把旧 obsidian-rag 的 4 个库按旧生效配置注册进 rag-redo。

生效配置来源（2026-09-25 实读）：
- data/libraries.json 各条目的显式覆盖
- 条目为 null 的字段 → 旧全局 config.json 的对应值（agents/skills 两库）
- selection_default = 旧全局 selection_new_files = "follow"
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "official-library-manager"))

from official_library_manager.config import LibraryConfigStore

# 旧全局 config.json 的排除名单（agents/skills 条目为 null 时的生效值）
GLOBAL_EXCLUDE_DIRS = [
    ".obsidian", ".smart-env", ".trash", ".git", "TEMP", "templates",
    ".opencode", ".council-state", "clippings-source", "90-Archive",
]
GLOBAL_EXCLUDE_FILES = ["目录.md", "AGENTS.md", "LOG.md", "README.md", "Home.md"]
GLOBAL_EXCLUDE_PATTERNS = ["session-", "会话", ".tmp", "MOC-"]

LIBRARIES = [
    {
        "library_id": "Obsidian Vault",
        "name": "Obsidian Vault",
        "root_path": r"D:\_STOREROOM\lol\Obsidian Vault",
        "selection_in": ["00-Inbox", "90-Archive", "AGENTS.md"],
        "selection_out": [
            "00-Inbox/1Clippings/任务清单.md",
            "00-Inbox/1Clippings/视频清单.md",
            "Clippings",
        ],
        "enabled_extensions": [".md", ".txt", ".pdf", ".docx"],
        "agent_formats": [],
        "exclude_dirs": [
            ".obsidian", ".smart-env", ".trash", ".git", "TEMP", "templates",
            ".opencode", ".council-state", "clippings-source",
        ],
        "exclude_files": GLOBAL_EXCLUDE_FILES,
        "exclude_patterns": GLOBAL_EXCLUDE_PATTERNS,
    },
    {
        "library_id": "agents",
        "name": "agents",
        "root_path": r"C:\Users\xbl26\.config\opencode\agents",
        "selection_in": [],
        "selection_out": [],
        "enabled_extensions": [".md", ".pdf", ".docx"],
        "agent_formats": [],
        "exclude_dirs": GLOBAL_EXCLUDE_DIRS,
        "exclude_files": GLOBAL_EXCLUDE_FILES,
        "exclude_patterns": GLOBAL_EXCLUDE_PATTERNS,
    },
    {
        "library_id": "skills",
        "name": "skills",
        "root_path": r"C:\Users\xbl26\.config\opencode\skills",
        "selection_in": [],
        "selection_out": [],
        "enabled_extensions": [".md", ".pdf", ".docx"],
        "agent_formats": [],
        "exclude_dirs": GLOBAL_EXCLUDE_DIRS,
        "exclude_files": GLOBAL_EXCLUDE_FILES,
        "exclude_patterns": GLOBAL_EXCLUDE_PATTERNS,
    },
    {
        "library_id": "LECTURE NOTE",
        "name": "LECTURE NOTE",
        "root_path": r"D:\.material\.WORKSTATION\USM COURSE RELATED\Y1S2\ESA122 fluid\LECTURE NOTE",
        "selection_in": ["CHAPTER 7 Dimensional Analysis and Similarity (Part 4).pdf"],
        "selection_out": [],
        "enabled_extensions": [".md", ".pdf", ".docx"],
        "agent_formats": [".pdf", ".docx"],
        "exclude_dirs": GLOBAL_EXCLUDE_DIRS,
        "exclude_files": GLOBAL_EXCLUDE_FILES,
        "exclude_patterns": GLOBAL_EXCLUDE_PATTERNS,
    },
]


def main() -> int:
    store = LibraryConfigStore(REPO_ROOT / "data" / "libraries.json")
    for spec in LIBRARIES:
        if store.get(spec["library_id"]) is None:
            store.add_library(spec["library_id"], spec["name"], spec["root_path"])
        store.set_policy(
            spec["library_id"],
            new_file_default="follow",
            enabled_extensions=spec["enabled_extensions"],
            exclude_dirs=spec["exclude_dirs"],
            exclude_files=spec["exclude_files"],
            exclude_patterns=spec["exclude_patterns"],
        )
        store.set_agent_formats(spec["library_id"], spec["agent_formats"])
        store.set_selection(
            spec["library_id"],
            selection_in=spec["selection_in"],
            selection_out=spec["selection_out"],
        )
        print(f"已注册: {spec['library_id']} -> {spec['root_path']}")
    print(f"共 {len(store.list_libraries())} 个库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
