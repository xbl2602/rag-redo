"""归档格式：把"一个库的可移植数据"打包成单个 zip 文件，反过来解包。

打包三份 JSON，不是拍脑袋——manifest/vectors/bm25 三者生命周期和来源完全
不同（库配置来自 library-manager，向量来自 vector_store，词法索引来自
lexical_index，见 core/pipeline.py 的 export_library/import_library），
分文件存放让人手动打开归档检查内容时不用先解析一个几十MB的单体JSON就能
看懂"这个库叫什么名字"。用 zip 不用 tar——Windows 用户双击就能免装任何
额外工具直接看到里面的文件，符合"目标用户不懂命令行"的安装哲学（同一条
理由延伸到导出格式本身）。

**格式版本号**：manifest.json 里带 archive_format_version，一旦将来这份
格式需要不兼容变更（比如拆分 vectors.json 为分片），旧软件读到更新的格式
要能清楚报错"这份归档比我支持的新，请升级本软件"，而不是读出一份错位的
半成品数据——这不是当前就要用到的能力，只是先把"未来能诚实拒绝"这条路
留出来，不是过度设计（真正的多版本兼容迁移逻辑要等真的出现第二个版本时
再写）。
"""
from __future__ import annotations

import json
import zipfile
from io import BytesIO

ARCHIVE_FORMAT_VERSION = 2

_MANIFEST_ENTRY = "manifest.json"
_VECTORS_ENTRY = "vectors.json"
_BM25_ENTRY = "bm25.json"
_INDEX_ENTRY = "index.json"
_EXTRACTED_ENTRY = "extracted.json"
_RELATIONS_ENTRY = "relations.json"
_FAILURES_ENTRY = "failures.json"
_VISUAL_ENTRY = "visual.json"


class ArchiveFormatError(Exception):
    """归档缺文件、JSON损坏、或格式版本太新读不了时抛出——这是"打不开这个
    包"的诚实报错，不是插件失败折叠的范畴（那是"某个文件索引失败"仍可继续
    跑其他文件的场景），调用方应该把这个错误原因原样展示给用户，不要吞掉。
    """


def pack(
    manifest: dict,
    vectors: dict,
    bm25: dict,
    *,
    index_manifest: dict | None = None,
    extracted: dict | None = None,
    relations: dict | None = None,
    failures: dict | None = None,
    visual: dict | None = None,
) -> bytes:
    """manifest/vectors/bm25 以及可选的完整索引状态都是纯 JSON 兼容字典。"""
    payload_manifest = dict(manifest)
    payload_manifest["archive_format_version"] = ARCHIVE_FORMAT_VERSION
    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_MANIFEST_ENTRY, json.dumps(payload_manifest, ensure_ascii=False))
        zf.writestr(_VECTORS_ENTRY, json.dumps(vectors, ensure_ascii=False))
        zf.writestr(_BM25_ENTRY, json.dumps(bm25, ensure_ascii=False))
        optional = {
            _INDEX_ENTRY: index_manifest,
            _EXTRACTED_ENTRY: extracted,
            _RELATIONS_ENTRY: relations,
            _FAILURES_ENTRY: failures,
            _VISUAL_ENTRY: visual,
        }
        for name, value in optional.items():
            if value is not None:
                zf.writestr(name, json.dumps(value, ensure_ascii=False))
    return buf.getvalue()


def unpack(data: bytes) -> dict:
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveFormatError(f"不是有效的归档文件: {exc}") from exc

    missing = [name for name in (_MANIFEST_ENTRY, _VECTORS_ENTRY, _BM25_ENTRY) if name not in zf.namelist()]
    if missing:
        raise ArchiveFormatError(f"归档缺少必要文件: {missing}")

    try:
        manifest = json.loads(zf.read(_MANIFEST_ENTRY))
        vectors = json.loads(zf.read(_VECTORS_ENTRY))
        bm25 = json.loads(zf.read(_BM25_ENTRY))
        optional = {
            name: json.loads(zf.read(name))
            for name in (
                _INDEX_ENTRY,
                _EXTRACTED_ENTRY,
                _RELATIONS_ENTRY,
                _FAILURES_ENTRY,
                _VISUAL_ENTRY,
            )
            if name in zf.namelist()
        }
    except json.JSONDecodeError as exc:
        raise ArchiveFormatError(f"归档内容损坏，不是合法JSON: {exc}") from exc

    version = manifest.get("archive_format_version")
    if version is None or version > ARCHIVE_FORMAT_VERSION:
        raise ArchiveFormatError(
            f"归档格式版本 {version!r} 比本软件支持的版本({ARCHIVE_FORMAT_VERSION})更新，请先升级软件再导入"
        )

    return {
        "manifest": manifest,
        "vectors": vectors,
        "bm25": bm25,
        "index_manifest": optional.get(_INDEX_ENTRY),
        "extracted": optional.get(_EXTRACTED_ENTRY),
        "relations": optional.get(_RELATIONS_ENTRY),
        "failures": optional.get(_FAILURES_ENTRY),
        "visual": optional.get(_VISUAL_ENTRY),
    }
