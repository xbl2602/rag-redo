r"""core/library_key.py — 库 id 落盘目录名的唯一归一实现。

**补的是哪个坑**：`library_id` 是用户可见的自由文本（可以带空格、中文，
`add_library` 只挡 `/\:*?"<>|` 与控制字符），而索引态的派生数据
（失败诊断 `index_failures/generations/<key>/`、双链
`note_relations/generations/<key>/`、manifest、generation 指针）都要按库
分目录落盘。归一方式此前有三份**互相独立**的 `re.sub(r"[^\w.-]", "_", id)`：
`core/index_failures.py:45`、`core/note_relations.py:62`、
`core/pipeline.py:1280`（全局回收的孤儿判定）。无哈希后缀的字符替换不是
单射——`"a b"` 与 `"a_b"` 归一后是同一个目录名，两个不同的库共用一份数据；
更糟的是全局回收会把其中一份当孤儿 `rmtree` 掉（用户可观察的数据丢失）。
`core/index_generation.py` 的两个 `_key` 早就是"安全名 + 短哈希"（单射），
本模块把那个做法提成唯一实现供全项目复用。

**为什么不做成 DataStore 命名空间**：`core/contracts.py`/DataStore 那一层
目前是 Phase 0 占位（见 `core/settings.py` 模块 docstring 同一条理由），
这些派生状态由核心自己的模块自己管，插件不经手。

**与 `core/index_generation.py` 的关系**：那里的
`IndexGenerationStore._key` / `IndexManifestStore._key` 是本函数逻辑的
逐字拷贝（文件所有权限制下未做改动，见本次修复报告）。两份实现必须
永远给出同一个字符串，`tests/test_pipeline_data_safety.py` 里有钉住这一点的
用例，任何一边改动而另一边没跟上会立刻红。
"""
from __future__ import annotations

import hashlib
import re

# 与 core/index_generation.py::_key 逐字一致的安全字符集：保留 Unicode 单词
# 字符（`\w` 在 Python 3 默认 UNICODE 语义下含中文），其余一律换下划线。
_UNSAFE = re.compile(r"[^\w.-]")

#: 安全名截断长度。截断后再拼哈希，保证 key 长度有界（Windows 单个路径分量
#: 上限 255），同时不依赖"截断后仍唯一"——唯一性由哈希后缀保证。
_SAFE_LIMIT = 48


def library_storage_key(library_id: str) -> str:
    """`library_id` → 落盘目录名（安全名 + 短哈希，单射）。

    带空格/中文的真实库 id 照常可用（照常注册、照常索引），只是**目录名**
    不再和别的库撞车：`"a b"` → `a_b-<16位sha>`，`"a_b"` → `a_b-<另一个sha>`。
    """
    safe = _UNSAFE.sub("_", library_id)[:_SAFE_LIMIT] or "library"
    digest = hashlib.sha256(library_id.encode("utf-8")).hexdigest()[:16]
    return f"{safe}-{digest}"
