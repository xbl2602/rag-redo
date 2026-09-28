"""归档格式：把"一个库的可移植数据"打包成单个 zip 文件，反过来解包。

打包多份 JSON，不是拍脑袋——manifest/vectors/bm25 三者生命周期和来源完全
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
再写）。2.x 往后的新增字段一律是**可选**的（源文件清单、逐条目 sha256），
所以版本号没有跟着动：新软件读旧包不报错，旧软件读新包也只是看不到新
字段、照旧能用——这正是"未来能诚实拒绝"这条路的反面（向后兼容）也必须
一起成立。

**三道闸门（缺陷 1/2 的修复落点）**：

1. `pack()` 拒绝为 0 块的库打包（对齐 obsidian-rag/export.py:199-201
   "索引为空（0 块），跳过导出"并计入失败数、退出码 1）。理由：用户会把
   一个空包当备份存下来，还以为备份成功了。
2. `pack()` 写 `entries_sha256`（除 manifest.json 自身外每个条目的
   sha256），`unpack()` 逐条目比对。这是旧项目 obsidian-rag/export.py:
   142-175 verify_package 与 obsidian-rag/import.py:86-108
   verify_manifest 的同一道闸；manifest.json 自身不参与校验和，因为它就是
   信任根（校验和记在里面）。
3. `unpack()` 与 `verify()` 逐条目报**可定位**的错误（哪个条目、期望什么、
   实际什么），而不是笼统的"JSON 解析失败"；`unpack()` 在 pipeline 的第一
   次落盘之前就完成全部校验并抛异常，因此天然满足旧项目
   import.py:86-108 "任一失败即中止，目标数据未被改动"的语义。

**源文件（缺陷 1）**：旧项目真的把 vault 源文件打进包里
（obsidian-rag/export.py:207-218 逐文件 sha256 + zf.write(fpath,
arcname=f"vault/{rel}")），接收端 import.py:199-208 place_vault 落回
vault_export/<库名>/，并在 import.py:288-292 明确提示"若源文件不在库路径
下，下次检索前自动同步会判定'文件全删'并清空索引"。REDO 侧把这件事补齐：

- `manifest["source_files"]`：**永远**存在的完整源文件清单
  `{rel_path: {"sha256":..., "size":..., "status":..., "in_archive": bool}}`，
  由 index.json 的 files 记录（里面本来就有 content_hash/size/status）
  推导，不需要读取任何源文件；
- `manifest["source_files_included"]`：正文**是否真的在包里**。与上一条
  分开是关键：清单永远有，正文默认没有，把两者混成一个字段就是缺陷 1
  最初的样子（谁都读不出到底缺了什么）；
- `manifest["source_files_notice"]`：一句可以直接展示给用户的中文提示，
  `unpack()` 会按自己读到的清单**重算**它（不信 manifest 里存的那句，
  否则用户手改 manifest 就能把提示抹掉）。

正文本身要不要进包由 `pack(..., sources=..., include_source_files=True)`
控制：调用方（core/pipeline.py）读 Vault 文件、把 `{rel: bytes}` 交给本插件，
本插件不碰用户的文件系统（plugin.toml 里也就没有 `filesystem` 权限）。默认
开关为 True = 旧项目行为；将来想要"只备份索引不备份笔记正文"是显式关掉，
不是默认就悄悄没有。**无论开关如何，清单与校验和都写全**——关掉正文时至少
还知道"缺了哪些文件、各自该是什么校验和"。

**导入计划（缺陷 3）**：`import_plan()` 是一个**纯函数**，产出导入将要做的
全部动作与回滚所需的全部信息，自己一个字节都不写。插件侧改不了
core/pipeline.py（文件所有权边界），但"导入是一个可回滚事务"这件事所需的
素材必须先备齐——pipeline 侧照 `ImportPlan`/`RollbackPlan` 接即可。
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field, replace
from io import BytesIO
from pathlib import Path, PurePosixPath

ARCHIVE_FORMAT_VERSION = 2

_MANIFEST_ENTRY = "manifest.json"
_VECTORS_ENTRY = "vectors.json"
_BM25_ENTRY = "bm25.json"
_INDEX_ENTRY = "index.json"
_EXTRACTED_ENTRY = "extracted.json"
_RELATIONS_ENTRY = "relations.json"
_FAILURES_ENTRY = "failures.json"
_VISUAL_ENTRY = "visual.json"
#: 源笔记正文在包内的路径前缀（对齐旧项目 export.py:136 的
#: `arcname=f"vault/{rel}"`，只是这里不叫 vault 免得和"用户的库目录"混淆）。
_SOURCE_PREFIX = "sources/"

#: 解包时允许的"zip 头里声明的解压后总体积"上限（字节）。`unpack` 在内存里把每个条目整块读出，
#: 几十 KB 的压缩炸弹（deflate 理论压缩比可达 ~1000:1）能声明出几十 GB，直接把进程内存耗尽。
#: 旧项目 import.py 用 `extractall` 落盘，没有这道闸；这里是内存内解包，必须有。阈值远高于
#: 任何真实归档（向量 JSON + 源笔记正文，个人库通常几十到几百 MB），它是防炸弹的护栏、
#: 不是配额：超限时在**读取任何条目之前**就拒绝，此时调用方还没写过任何东西。
MAX_UNPACKED_BYTES = 8 * 1024**3

_REQUIRED_ENTRIES = (_MANIFEST_ENTRY, _VECTORS_ENTRY, _BM25_ENTRY)
_OPTIONAL_ENTRIES = (
    _INDEX_ENTRY,
    _EXTRACTED_ENTRY,
    _RELATIONS_ENTRY,
    _FAILURES_ENTRY,
    _VISUAL_ENTRY,
)

#: manifest / index_manifest 这两层**顶层**键名里出现这些片段就拒绝打包。
#: 只查这两层的顶层键：它们是受控词表（见 core/pipeline.py:2405-2420 与
#: index.py 的 index_manifest 结构），而 files/relations 这类"以用户相对
#: 路径为键"的字典里完全可能有叫 api_key.md 的笔记——拿数据当威胁去扫，
#: 误伤用户文件名是自己制造 bug（tests 里有钉死的用例）。
_CREDENTIAL_HINTS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "bearer",
    "authorization",
)


class ArchiveFormatError(Exception):
    """归档缺文件、条目损坏、或格式版本太新读不了时抛出——这是"打不开这个
    包"的诚实报错，不是插件失败折叠的范畴（那是"某个文件索引失败"仍可继续
    跑其他文件的场景），调用方应该把这个错误原因原样展示给用户，不要吞掉。

    带上 entry/field/expected/actual 四个结构化字段，是为了让调用方能
    程序化地判断"是哪个条目坏了"（写进 GUI 的错误面板、MCP 的错误 JSON），
    而不是只能给人看一句话。str(exc) 仍然是完整可读的中文说明。
    """

    def __init__(
        self,
        message: str,
        *,
        entry: str | None = None,
        field: str | None = None,
        expected: object = None,
        actual: object = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.entry = entry
        self.field = field
        self.expected = expected
        self.actual = actual
        self.hint = hint


class ArchiveEmptyLibraryError(ArchiveFormatError):
    """要打包的库一个块都没有。

    对齐旧项目 obsidian-rag/export.py:199-201：`if not ids: log("索引为空
    （0 块），跳过导出"); return None`，调用方（export.py:301-316）把它计入
    失败数并 `sys.exit(1)`。REDO 侧不能改 pipeline 的返回码，但归档插件
    明确拒绝打包，pipeline/MCP/CLI 就必然报错——比返回一个"看起来成功的
    空包"诚实。做成子类是为了向下兼容：现有 `except ArchiveFormatError`
    的调用方不用改就能继续工作。
    """


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _assert_no_credentials(payload: dict, where: str) -> None:
    for key in payload:
        lowered = str(key).lower()
        if any(hint in lowered for hint in _CREDENTIAL_HINTS):
            # 报错里只出现**键名**，绝不出现值——异常文本会进日志（MCP 错误
            # JSON、GUI 错误面板、测试输出），AGENTS.md §7 禁止敏感值进日志。
            raise ArchiveFormatError(
                f"{where} 里出现疑似凭据字段 {key!r}，归档元数据不允许包含凭据（AGENTS.md §7）"
                f"；请把它留在用户配置里，不要随库一起导出",
                entry=_MANIFEST_ENTRY if where == "manifest" else _INDEX_ENTRY,
                field=str(key),
            )


def _normalize_rel(rel: str) -> str:
    """库内相对路径 → 包内条目名后缀。拒绝任何能写出目标目录的路径
    （zip slip，对齐 obsidian-rag/import.py:59-66 _validate_zip_members：
    包内含绝对路径或 `..` 的条目一律拒绝）。"""
    text = str(rel).replace("\\", "/").strip()
    if not text:
        raise ArchiveFormatError(f"源文件相对路径为空，无法放进归档：{rel!r}")
    pure = PurePosixPath(text)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise ArchiveFormatError(
            f"源文件相对路径含非法路径条目：{rel!r}（不允许绝对路径或 ..）",
            hint="归档里的路径必须是库目录下的相对路径",
        )
    return str(pure)


def _source_inventory(index_manifest: dict | None, sources: dict[str, bytes] | None) -> dict[str, dict]:
    """从 index.json 的 files 记录推出"这个库原本有哪些源文件、各自多大、
    内容校验和是什么"。files 记录里本来就有 size/content_hash/status
    （core/pipeline.py 的 _file_fingerprint 落的），所以这一步**不需要读取
    任何源文件**——不碰用户的文件系统是本插件的权限边界。
    """
    inventory: dict[str, dict] = {}
    files = index_manifest.get("files") if isinstance(index_manifest, dict) else None
    if isinstance(files, dict):
        for path, record in files.items():
            if not isinstance(path, str) or not isinstance(record, dict):
                continue
            inventory[_normalize_rel(path)] = {
                "sha256": str(record.get("content_hash") or ""),
                "size": int(record.get("size", -1)) if isinstance(record.get("size", -1), int) else -1,
                "status": str(record.get("status") or ""),
                "in_archive": False,
            }
    for path, blob in (sources or {}).items():
        rel = _normalize_rel(path)
        payload = bytes(blob)
        # 包内带了正文时，清单里的 sha256/size 必须是**包内字节**的值，不能
        # 留源文件路径上的旧值——否则接收端比对的是另一个东西。
        inventory[rel] = {
            "sha256": _sha256(payload),
            "size": len(payload),
            "status": inventory.get(rel, {}).get("status", ""),
            "in_archive": True,
        }
    return inventory


def _source_notice(included: bool, inventory: dict[str, dict]) -> str:
    count = len(inventory)
    if included:
        return (
            f"本归档已携带 {count} 个源笔记正文（{_SOURCE_PREFIX} 前缀），"
            f"导入时应落位到库目录下对应的相对路径。"
        )
    return (
        f"本归档未携带笔记正文：{count} 个源文件只有清单（相对路径 + 大小 + sha256），"
        f"导入后需要把源笔记放回库目录——否则检索前自动同步会判定'文件全删'"
        f"并清空索引（对齐 obsidian-rag/import.py:288-292 的提示）。"
    )


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
    sources: dict[str, bytes] | None = None,
    include_source_files: bool = True,
) -> bytes:
    """manifest/vectors/bm25 以及可选的完整索引状态都是纯 JSON 兼容字典。

    `sources` 是 `{库内相对路径: 文件字节}`，由调用方读取后交进来——本插件
    不持有 vault 读写权限（plugin.toml 的 permissions 里没有 filesystem）。
    `include_source_files=False` 时不写正文，但 `manifest["source_files"]`
    里的完整清单与校验和照样写全。
    """
    payload_manifest = dict(manifest or {})
    payload_vectors = dict(vectors or {})
    payload_bm25 = dict(bm25 or {})
    _assert_no_credentials(payload_manifest, "manifest")
    if isinstance(index_manifest, dict):
        _assert_no_credentials(index_manifest, "index_manifest")

    if not payload_vectors:
        library_name = str(payload_manifest.get("name") or payload_manifest.get("library_id") or "(未命名)")
        raise ArchiveEmptyLibraryError(
            f"库 {library_name!r} 的索引为空（0 块），拒绝导出——"
            f"用户会把空包当备份存下来，却以为备份成功了。"
            f"请先对这个库跑一次 index_library（CLI: reindex / MCP: reindex_knowledge）"
            f"让块数大于 0，再导出。",
            entry=_VECTORS_ENTRY,
            expected="至少 1 个向量块",
            actual=0,
        )

    inventory = _source_inventory(index_manifest, sources)
    embedded: dict[str, bytes] = {}
    if include_source_files and sources:
        for path, blob in sources.items():
            embedded[_normalize_rel(path)] = bytes(blob)

    payload_manifest["archive_format_version"] = ARCHIVE_FORMAT_VERSION
    payload_manifest["source_files"] = inventory
    payload_manifest["source_files_included"] = bool(embedded)
    payload_manifest["source_file_count"] = len(inventory)
    payload_manifest["chunk_count"] = len(payload_vectors)
    payload_manifest["source_files_notice"] = _source_notice(bool(embedded), inventory)

    # 除 manifest.json 自身外的每个条目都记 sha256（对齐旧项目
    # export.py:104-118 build_manifest 的 sha256 段：payload 与 index_meta
    # 各记一个，manifest 自身不记——它就是信任根）。
    blobs: list[tuple[str, bytes]] = [
        (_VECTORS_ENTRY, _encode(payload_vectors)),
        (_BM25_ENTRY, _encode(payload_bm25)),
    ]
    for name, value in (
        (_INDEX_ENTRY, index_manifest),
        (_EXTRACTED_ENTRY, extracted),
        (_RELATIONS_ENTRY, relations),
        (_FAILURES_ENTRY, failures),
        (_VISUAL_ENTRY, visual),
    ):
        if value is not None:
            blobs.append((name, _encode(value)))
    for rel, payload in sorted(embedded.items()):
        blobs.append((f"{_SOURCE_PREFIX}{rel}", payload))
    payload_manifest["entries_sha256"] = {
        name: _sha256(blob) for name, blob in blobs if name != _MANIFEST_ENTRY
    }

    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_MANIFEST_ENTRY, _encode(payload_manifest))
        for name, blob in blobs:
            zf.writestr(name, blob)
    return buf.getvalue()


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _decode(name: str, blob: bytes) -> object:
    try:
        return json.loads(blob)
    except UnicodeDecodeError as exc:
        raise ArchiveFormatError(
            f"条目 {name} 不是合法的 UTF-8 文本（第 {exc.start} 字节处解码失败）——"
            f"包可能已损坏或被改写，请重新导出/传输",
            entry=name,
        ) from exc
    except json.JSONDecodeError as exc:
        raise ArchiveFormatError(
            f"条目 {name} 不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）"
            f"——请重新导出/传输这个包",
            entry=name,
            field=f"line {exc.lineno} column {exc.colno}",
        ) from exc


def _reject_unsafe_names(names: list[str]) -> None:
    """包内条目名含绝对路径或 `..` 一律拒绝（对齐 obsidian-rag/import.py:
    59-66）。以前包内只有几个固定 JSON 名，这条形同虚设；一旦包内带源笔记
    正文、接收端要按相对路径往用户库目录里写，它就成了真实的写入逃逸面。"""
    for name in names:
        if name.startswith("/") or ":" in name.split("/")[0]:
            raise ArchiveFormatError(
                f"包内含非法路径条目：{name}（不允许绝对路径或盘符）",
                entry=name,
            )
        if any(part == ".." for part in name.replace("\\", "/").split("/")):
            raise ArchiveFormatError(
                f"包内含非法路径条目：{name}（不允许 .. 逃出解压目录）",
                entry=name,
            )


def unpack(data: bytes) -> dict:
    """解包 + **全量校验**。任何一步失败都抛 ArchiveFormatError，且抛的时
    候调用方还没写过任何东西——这正是旧项目 import.py:86-108 "任一失败即
    中止，目标数据未被改动"要的那道闸。"""
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveFormatError(
            f"归档不是有效的 zip 文件（文件被截断或压根不是归档）：{exc}。"
            f"请重新导出/传输这个包",
            hint="跨网络/网盘传输的 zip 最常见的损坏形态就是尾部被截断",
        ) from exc

    try:
        names = [info.filename for info in zf.infolist() if not info.is_dir()]
        _reject_unsafe_names(names)
        declared = sum(info.file_size for info in zf.infolist() if not info.is_dir())
        if declared > MAX_UNPACKED_BYTES:
            raise ArchiveFormatError(
                f"归档解压后声明的总体积 {declared / 1024**3:.1f} GiB 超过上限 "
                f"{MAX_UNPACKED_BYTES / 1024**3:.0f} GiB（压缩包本身只有 {len(data) / 1024**2:.1f} MiB）"
                f"——这不是一个正常的导出包，已在读取任何内容前拒绝",
                field="uncompressed_size",
                expected=f"<= {MAX_UNPACKED_BYTES}",
                actual=declared,
                hint="真实归档的解压体积不会比压缩包大出几个数量级；疑似压缩炸弹或被篡改的文件头",
            )
        missing = [name for name in _REQUIRED_ENTRIES if name not in names]
        if missing:
            raise ArchiveFormatError(
                f"归档缺少必要条目：{'、'.join(missing)}"
                f"（一个可导入的归档至少要有 {'、'.join(_REQUIRED_ENTRIES)}）",
                entry=missing[0],
                expected=list(_REQUIRED_ENTRIES),
                actual=names,
            )
        raw: dict[str, bytes] = {}
        for name in names:
            try:
                raw[name] = zf.read(name)
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                # zipfile 读条目时会做 CRC 校验，损坏在这里暴露
                # （旧项目 verify_package 的"解压触发 CRC + 逐文件 sha256"）。
                raise ArchiveFormatError(
                    f"条目 {name} 解压失败（CRC 校验未通过）：{exc}——包已损坏，请重新传输",
                    entry=name,
                ) from exc
    finally:
        zf.close()

    manifest = _decode(_MANIFEST_ENTRY, raw[_MANIFEST_ENTRY])
    if not isinstance(manifest, dict):
        raise ArchiveFormatError(
            f"条目 {_MANIFEST_ENTRY} 必须是一个 JSON 对象，实际是 {type(manifest).__name__}",
            entry=_MANIFEST_ENTRY,
        )
    version = manifest.get("archive_format_version")
    if version is None or not isinstance(version, int) or version > ARCHIVE_FORMAT_VERSION:
        raise ArchiveFormatError(
            f"归档格式版本 {version!r} 比本软件支持的版本({ARCHIVE_FORMAT_VERSION})更新，"
            f"请先升级软件再导入",
            entry=_MANIFEST_ENTRY,
            field="archive_format_version",
            expected=f"<= {ARCHIVE_FORMAT_VERSION}",
            actual=version,
        )

    decoded: dict[str, object] = {
        "manifest": manifest,
        "vectors": _decode(_VECTORS_ENTRY, raw[_VECTORS_ENTRY]),
        "bm25": _decode(_BM25_ENTRY, raw[_BM25_ENTRY]),
    }
    for name in _OPTIONAL_ENTRIES:
        decoded[{
            _INDEX_ENTRY: "index_manifest",
            _EXTRACTED_ENTRY: "extracted",
            _RELATIONS_ENTRY: "relations",
            _FAILURES_ENTRY: "failures",
            _VISUAL_ENTRY: "visual",
        }[name]] = _decode(name, raw[name]) if name in raw else None

    sources: dict[str, bytes] = {}
    for name, blob in raw.items():
        if name.startswith(_SOURCE_PREFIX):
            sources[name[len(_SOURCE_PREFIX):]] = blob

    payload = {
        "manifest": decoded["manifest"],
        "vectors": decoded["vectors"],
        "bm25": decoded["bm25"],
        "index_manifest": decoded["index_manifest"],
        "extracted": decoded["extracted"],
        "relations": decoded["relations"],
        "failures": decoded["failures"],
        "visual": decoded["visual"],
        "sources": sources,
    }
    _check_entry_checksums(manifest, raw)
    verify(payload)
    payload["notices"] = notices(payload)
    return payload


def _check_entry_checksums(manifest: dict, raw: dict[str, bytes]) -> None:
    declared = manifest.get("entries_sha256")
    if not isinstance(declared, dict):
        # 2.0 时代的老包没有这个字段：不能因此判它损坏（向后兼容），但也
        # 就没有逐条目 sha256 这层保护——如实放过，交给结构校验。
        return
    for name, expect in declared.items():
        if name not in raw:
            raise ArchiveFormatError(
                f"条目 {name} 出现在 manifest 的校验和清单里，但包内没有这个条目",
                entry=name,
                expected=expect,
            )
        actual = _sha256(raw[name])
        if actual != expect:
            raise ArchiveFormatError(
                f"条目 {name} 的 sha256 与 manifest 记录不一致"
                f"（期望 {expect}，实得 {actual}）——包已损坏或被改写，请重新导出/传输",
                entry=name,
                expected=expect,
                actual=actual,
            )


def notices(payload: dict) -> tuple[str, ...]:
    """按**读到的**清单重算给用户看的问题清单，绝不信 manifest 里存的那句
    提示（否则用户手改 manifest 就能把"包里没有正文"这条提示抹掉）。"""
    manifest = payload.get("manifest") if isinstance(payload, dict) else None
    if not isinstance(manifest, dict):
        return ()
    inventory = manifest.get("source_files")
    included = bool(manifest.get("source_files_included"))
    if isinstance(inventory, dict) and not inventory:
        return (
            "归档没有记录源文件清单，无法核对目标目录缺了哪些文件"
            "（这份包可能是很早的版本导出的）",
        )
    return (_source_notice(included, inventory or {}),) if isinstance(inventory, dict) else ()


def verify(payload: dict) -> None:
    """写库之前的最后一道闸：结构 + 计数 + vectors ↔ index 一致性。

    旧项目对位：obsidian-rag/import.py:179-188（重建后 count 校验 + 抽 3
    块 embedding 余弦校验）与 :86-108（sha256 全量校验）。REDO 侧能在归档
    层做的那部分在这里做完（count、id 互指、逐行向量形状），真正写库后的
    count/余弦复核仍然属于 pipeline 的职责。

    正常返回 None；不通过抛 ArchiveFormatError（带 entry/field）。
    """
    if not isinstance(payload, dict):
        raise ArchiveFormatError(f"归档 payload 必须是一个字典，实际是 {type(payload).__name__}")
    for key in ("manifest", "vectors"):
        if key not in payload:
            raise ArchiveFormatError(f"归档 payload 缺少必需键 {key!r}")
    manifest = payload["manifest"]
    if not isinstance(manifest, dict):
        raise ArchiveFormatError(
            f"条目 {_MANIFEST_ENTRY} 必须是一个 JSON 对象，实际是 {type(manifest).__name__}",
            entry=_MANIFEST_ENTRY,
        )
    library_id = manifest.get("library_id")
    if not isinstance(library_id, str) or not library_id:
        raise ArchiveFormatError(
            f"manifest 缺少 library_id（导入需要一个目标库标识）",
            entry=_MANIFEST_ENTRY,
            field="library_id",
        )
    if not isinstance(manifest.get("name"), str) or not manifest.get("name"):
        raise ArchiveFormatError(
            f"manifest 缺少库名 name",
            entry=_MANIFEST_ENTRY,
            field="name",
        )

    vectors = payload["vectors"]
    if not isinstance(vectors, dict):
        raise ArchiveFormatError(
            f"条目 {_VECTORS_ENTRY} 必须是 {{chunk_id: 行}} 的 JSON 对象",
            entry=_VECTORS_ENTRY,
        )
    _verify_vector_rows(vectors)

    bm25 = payload.get("bm25")
    if bm25 is not None and not isinstance(bm25, dict):
        raise ArchiveFormatError(f"条目 {_BM25_ENTRY} 必须是一个 JSON 对象", entry=_BM25_ENTRY)
    for key in ("doc_lengths", "doc_tokens_cache"):
        value = (bm25 or {}).get(key)
        if value is not None and not isinstance(value, dict):
            raise ArchiveFormatError(
                f"条目 {_BM25_ENTRY} 的 {key} 必须是一个 JSON 对象",
                entry=_BM25_ENTRY,
                field=key,
            )

    for name, key in (
        (_INDEX_ENTRY, "index_manifest"),
        (_EXTRACTED_ENTRY, "extracted"),
        (_RELATIONS_ENTRY, "relations"),
        (_FAILURES_ENTRY, "failures"),
        (_VISUAL_ENTRY, "visual"),
    ):
        value = payload.get(key)
        if value is not None and not isinstance(value, dict):
            raise ArchiveFormatError(f"条目 {name} 必须是一个 JSON 对象", entry=name)

    declared_count = manifest.get("chunk_count")
    if isinstance(declared_count, int) and declared_count != len(vectors):
        raise ArchiveFormatError(
            f"manifest 声明的 chunk_count={declared_count}，但条目 {_VECTORS_ENTRY} "
            f"里只有 {len(vectors)} 个块——两者不一致，包可能被截断或改写过",
            entry=_VECTORS_ENTRY,
            field="chunk_count",
            expected=declared_count,
            actual=len(vectors),
        )

    inventory = manifest.get("source_files")
    if inventory is not None:
        if not isinstance(inventory, dict):
            raise ArchiveFormatError(
                "manifest 的 source_files 必须是 {相对路径: {...}} 的 JSON 对象",
                entry=_MANIFEST_ENTRY,
                field="source_files",
            )
        for rel, record in inventory.items():
            if not isinstance(rel, str) or not isinstance(record, dict):
                raise ArchiveFormatError(
                    f"manifest 的 source_files 条目 {rel!r} 必须是一个对象",
                    entry=_MANIFEST_ENTRY,
                    field="source_files",
                )
            if not isinstance(record.get("sha256", ""), str):
                raise ArchiveFormatError(
                    f"manifest 的 source_files[{rel!r}].sha256 必须是字符串",
                    entry=_MANIFEST_ENTRY,
                    field="source_files",
                )
        declared_sources = manifest.get("source_file_count")
        if isinstance(declared_sources, int) and declared_sources != len(inventory):
            raise ArchiveFormatError(
                f"manifest 声明的 source_file_count={declared_sources}，"
                f"但 source_files 里只有 {len(inventory)} 项",
                entry=_MANIFEST_ENTRY,
                field="source_file_count",
                expected=declared_sources,
                actual=len(inventory),
            )

    sources = payload.get("sources") or {}
    if not isinstance(sources, dict):
        raise ArchiveFormatError(
            f"包内源文件条目必须是 {{相对路径: 字节}} 的映射",
            entry=_SOURCE_PREFIX,
        )
    for rel, blob in sources.items():
        if isinstance(inventory, dict) and rel not in inventory:
            raise ArchiveFormatError(
                f"包内带了源文件 {rel!r}，但 manifest 的 source_files 清单里没有它"
                f"——清单与正文对不上，包不可信",
                entry=f"{_SOURCE_PREFIX}{rel}",
                field="source_files",
            )
        if isinstance(inventory, dict):
            expect = str(inventory[rel].get("sha256") or "")
            actual = _sha256(bytes(blob))
            if expect and actual != expect:
                raise ArchiveFormatError(
                    f"包内源文件 {rel} 的 sha256 与 manifest 记录不一致"
                    f"（期望 {expect}，实得 {actual}）",
                    entry=f"{_SOURCE_PREFIX}{rel}",
                    expected=expect,
                    actual=actual,
                )

    _verify_index_consistency(payload.get("index_manifest"), vectors)


def _verify_vector_rows(vectors: dict) -> None:
    for chunk_id, row in vectors.items():
        if not isinstance(row, dict):
            raise ArchiveFormatError(
                f"条目 {_VECTORS_ENTRY} 里的块 {chunk_id!r} 必须是一个对象"
                f"（document/metadata/embedding），实际是 {type(row).__name__}"
                f"——这类坏行以前在 core/pipeline.py:2524-2525 被静默跳过，"
                f"结果是索引里少一块但导入照样成功",
                entry=_VECTORS_ENTRY,
                field=str(chunk_id),
            )
        embedding = row.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise ArchiveFormatError(
                f"条目 {_VECTORS_ENTRY} 里的块 {chunk_id!r} 缺 embedding 或不是非空数组"
                f"（实际是 {type(embedding).__name__}）",
                entry=_VECTORS_ENTRY,
                field=f"{chunk_id}.embedding",
            )
        for position, value in enumerate(embedding):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ArchiveFormatError(
                    f"条目 {_VECTORS_ENTRY} 里的块 {chunk_id!r} 的 embedding[{position}] "
                    f"不是数字（{type(value).__name__}）",
                    entry=_VECTORS_ENTRY,
                    field=f"{chunk_id}.embedding[{position}]",
                )
        document = row.get("document")
        if document is not None and not isinstance(document, str):
            raise ArchiveFormatError(
                f"条目 {_VECTORS_ENTRY} 里的块 {chunk_id!r} 的 document 不是字符串",
                entry=_VECTORS_ENTRY,
                field=f"{chunk_id}.document",
            )
        metadata = row.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ArchiveFormatError(
                f"条目 {_VECTORS_ENTRY} 里的块 {chunk_id!r} 的 metadata 不是对象",
                entry=_VECTORS_ENTRY,
                field=f"{chunk_id}.metadata",
            )


def _verify_index_consistency(index_manifest: object, vectors: dict) -> None:
    """index.json 说某个已索引文件有 N 个块，向量里就必须真有这 N 个——两边
    对不上意味着"索引清单说有、向量里没有"或"向量里有、清单说没有"，两种
    都会让恢复出来的库行为诡异（要么检索缺块，要么留下查不到出处的幽灵块）。
    """
    if not isinstance(index_manifest, dict):
        return
    files = index_manifest.get("files")
    if not isinstance(files, dict):
        return
    referenced: set[str] = set()
    for path, record in files.items():
        if not isinstance(record, dict) or record.get("status") != "indexed":
            continue
        chunk_ids = record.get("chunk_ids")
        if isinstance(chunk_ids, list):
            referenced.update(str(chunk_id) for chunk_id in chunk_ids if chunk_id)
    missing = sorted(referenced - set(vectors))
    if missing:
        raise ArchiveFormatError(
            f"条目 {_INDEX_ENTRY} 声明了 {len(missing)} 个已索引块，但条目 "
            f"{_VECTORS_ENTRY} 里没有（例：{'、'.join(missing[:3])}）——"
            f"包不完整或被截断，导入后会得到一个缺块的库",
            entry=_VECTORS_ENTRY,
            field="active_chunk_ids",
            expected=sorted(referenced),
            actual=sorted(vectors),
        )
    orphan = sorted(set(vectors) - referenced)
    if orphan:
        raise ArchiveFormatError(
            f"条目 {_VECTORS_ENTRY} 里有 {len(orphan)} 个块没被 {_INDEX_ENTRY} 的任何"
            f"已索引文件引用（例：{'、'.join(orphan[:3])}）——导入后它们会成为查不到"
            f"出处的幽灵块",
            entry=_VECTORS_ENTRY,
            field="active_chunk_ids",
            expected=sorted(referenced),
            actual=sorted(vectors),
        )


@dataclass(frozen=True)
class RollbackPlan:
    """导入失败时要撤销的全部东西 + 建议顺序。

    缺陷 3 的现场：core/pipeline.py:2506/2509/2514/2522 连续四次落盘
    libraries.json，**之后**才 upsert 向量（2580-2593）、写词法（2595-2607）、
    写提取缓存/关系/视觉/失败诊断（2609-2656）。中途任一异常 → 库留在注册
    表、活跃 generation 为空、`libg_<hash>` 半截集合留在 Chroma；而 prune
    明确声明不按名清扫集合，等于永久泄漏。更硬的后果：库已注册，重跑同一
    份归档会撞上 core/pipeline.py:2496-2497 的"库已存在，拒绝覆盖"，
    旧项目 obsidian-rag/import.py:13 承诺的"重跑幂等：中途失败可原样重跑"
    就此断裂。
    """

    target_id: str
    #: 已落盘注册表动作的**逆序**（撤销顺序 = 这个顺序）。
    registry_writes: tuple[str, ...]
    #: 导入若落盘了源笔记正文，这些路径是本次新建的文件，回滚要删掉。
    written_source_files: tuple[str, ...]
    #: 建议的回滚执行顺序（人可读 + 可测）。discard_index_generation 放在
    #: 最前：它在 generation 尚未发布时不会早退（core/pipeline.py:1338-1339
    #: 只在"已发布"时 return），一次就把 Chroma 半截集合、词法索引、提取
    #: 缓存、关系、失败诊断、视觉库全清了。
    order: tuple[str, ...] = (
        "discard_index_generation",
        "store.remove_library",
    )


@dataclass(frozen=True)
class ImportPlan:
    """导入将要做的**全部**动作 + 回滚所需信息。纯计算，不写任何东西。

    调用顺序（core/pipeline.py::import_library 侧照此改）：unpack →
    verify → import_plan → 按 plan 执行 → 失败按 plan.rollback 撤销。
    """

    target_id: str
    library_name: str
    library_config: dict
    id_map: dict[str, str]
    vector_rows: list[dict] = field(default_factory=list)
    vector_batches: list[list[str]] = field(default_factory=list)
    active_chunk_ids: list[str] = field(default_factory=list)
    lexical_state: dict = field(default_factory=dict)
    dropped_lexical_ids: tuple[str, ...] = ()
    extracted: dict = field(default_factory=dict)
    relations: dict = field(default_factory=dict)
    visual: dict = field(default_factory=dict)
    failures: dict | None = None
    files: dict[str, dict] = field(default_factory=dict)
    source_files: dict[str, dict] = field(default_factory=dict)
    source_blobs: dict[str, bytes] = field(default_factory=dict)
    missing_source_files: tuple[str, ...] = ()
    source_files_included: bool = False
    notices: tuple[str, ...] = ()
    rollback: RollbackPlan | None = None


#: 归档内部字段，不是"库配置"——import_plan 不把它们原样交给 library_manager。
_ARCHIVE_ONLY_MANIFEST_KEYS = frozenset(
    {
        "archive_format_version",
        "entries_sha256",
        "source_files",
        "source_files_included",
        "source_file_count",
        "source_files_notice",
        "chunk_count",
    }
)


def import_plan(
    payload: dict,
    target_id: str,
    *,
    root_path: str | Path | None = None,
    upsert_batch: int = 500,
) -> ImportPlan:
    """算出导入将做的每一件事，本身一个字节都不写（对齐缺陷 3 的要求：
    先有完整素材，再谈事务化执行）。

    与 core/pipeline.py:2530-2578 的现行算法逐条对齐（同样的
    `f"{target_id}:{path}:{chunk_index}"` 目标 id、同样的 500 条一批），
    **只有一处故意不同**：未映射的词法 id 走"丢弃"而不是
    `f"{target_id}:{key}"` 兜底——那个兜底会把源 id 拼成 `新库:旧库:path:0`
    这种错号，而向量侧同场景是丢弃，两侧策略不一致。旧项目
    obsidian-rag/import.py:164-177 根本没有重映射，两侧天然一致，所以
    "丢弃"才是对齐旧行为的那一侧；丢掉的 id 记在 `dropped_lexical_ids`
    里并在 notices 里说明，不静默。

    不在这里调 `verify()`：校验是调用方在 unpack 之后的独立一道闸
    （LEGACY import.py:86-108 的位置），混进 plan 会让"校验"变成
    "规划的一部分"，两次调用容易只做一次。
    """
    manifest = payload.get("manifest") if isinstance(payload, dict) else None
    if not isinstance(manifest, dict):
        raise ArchiveFormatError(
            f"归档 payload 缺少 manifest，无法规划导入",
            entry=_MANIFEST_ENTRY,
        )
    vectors = payload.get("vectors") if isinstance(payload.get("vectors"), dict) else {}
    source_files = manifest.get("source_files")
    inventory: dict[str, dict] = dict(source_files) if isinstance(source_files, dict) else {}
    included = bool(manifest.get("source_files_included"))
    blobs = {rel: bytes(data) for rel, data in (payload.get("sources") or {}).items()}

    id_map: dict[str, str] = {}
    vector_rows: list[dict] = []
    collisions: dict[str, str] = {}
    for source_chunk_id, row in vectors.items():
        if not isinstance(row, dict):
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        path = str(metadata.get("path", ""))
        try:
            chunk_index = int(metadata.get("chunk_index", 0))
        except (TypeError, ValueError):
            chunk_index = 0
        target_chunk_id = f"{target_id}:{path}:{chunk_index}"
        id_map[str(source_chunk_id)] = target_chunk_id
        if target_chunk_id in collisions:
            collisions[target_chunk_id] = str(source_chunk_id)
            continue
        vector_rows.append({
            "chunk_id": target_chunk_id,
            "document": row.get("document", ""),
            "metadata": metadata,
            "embedding": row.get("embedding"),
        })

    dropped: list[str] = []
    lexical_state = dict(payload.get("bm25") or {})
    if isinstance(lexical_state, dict):
        for key in ("doc_lengths", "doc_tokens_cache"):
            values = lexical_state.get(key)
            if not isinstance(values, dict):
                continue
            remapped: dict[str, object] = {}
            for source_key, value in values.items():
                mapped = id_map.get(str(source_key))
                if mapped is None:
                    dropped.append(str(source_key))
                    continue
                remapped[mapped] = value
            lexical_state[key] = remapped

    files: dict[str, dict] = {}
    source_manifest = payload.get("index_manifest")
    source_files = source_manifest.get("files") if isinstance(source_manifest, dict) else None
    if isinstance(source_files, dict):
        for path, source_record in source_files.items():
            if not isinstance(path, str) or not isinstance(source_record, dict):
                continue
            record = dict(source_record)
            record["chunk_ids"] = [
                id_map.get(str(chunk_id), f"{target_id}:{path}:{index}")
                for index, chunk_id in enumerate(record.get("chunk_ids", []))
            ]
            # size/mtime_ns/content_hash 留 None：指纹必须由 pipeline 在目标
            # 目录上实测（core/pipeline.py::_file_fingerprint），插件既没有
            # 目标目录的读权限，也不该替别人猜一个指纹。
            record["size"] = None
            record["mtime_ns"] = None
            record["content_hash"] = None
            files[path] = record
    for row in vector_rows:
        path = str(row["metadata"].get("path", ""))
        record = files.setdefault(path, {
            "size": None,
            "mtime_ns": None,
            "content_hash": None,
            "status": "indexed",
            "failure_state": None,
            "failure_reason": None,
            "failure_detail": None,
            "links": [],
            "chunk_ids": [],
        })
        record["chunk_ids"].append(row["chunk_id"])
    for record in files.values():
        record["chunk_ids"] = list(dict.fromkeys(record.get("chunk_ids", [])))

    missing = _missing_source_files(inventory, root_path)
    plan = ImportPlan(        target_id=target_id,
        library_name=str(manifest.get("name") or target_id),
        library_config={
            key: value
            for key, value in manifest.items()
            if key not in _ARCHIVE_ONLY_MANIFEST_KEYS
        },
        id_map=id_map,
        vector_rows=vector_rows,
        vector_batches=[
            [row["chunk_id"] for row in vector_rows[start : start + upsert_batch]]
            for start in range(0, len(vector_rows), max(1, upsert_batch))
        ],
        active_chunk_ids=[row["chunk_id"] for row in vector_rows],
        lexical_state=lexical_state,
        dropped_lexical_ids=tuple(dict.fromkeys(dropped)),
        extracted=payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {},
        relations=payload.get("relations") if isinstance(payload.get("relations"), dict) else {},
        visual=payload.get("visual") if isinstance(payload.get("visual"), dict) else {},
        failures=payload.get("failures") if isinstance(payload.get("failures"), dict) else None,
        files=files,
        source_files=inventory,
        source_blobs=blobs,
        missing_source_files=missing,
        source_files_included=included,
        rollback=RollbackPlan(
            target_id=target_id,
            registry_writes=(
                "store.set_agent_formats",
                "store.set_policy",
                "store.set_selection",
                "store.add_library",
            ),
            written_source_files=tuple(sorted(blobs)),
        ),
    )
    return replace(plan, notices=_plan_notices(plan, collisions))


def _missing_source_files(inventory: dict[str, dict], root_path: str | Path | None) -> tuple[str, ...]:
    if root_path is None or not inventory:
        return ()
    root = Path(root_path)
    return tuple(sorted(rel for rel in inventory if not (root / rel).is_file()))


def _plan_notices(plan: ImportPlan, collisions: dict[str, str]) -> tuple[str, ...]:
    """只报**真问题**。一切正常时不制造噪音——调用方要能直接把 notices 原样
    展示给用户，多余的"一切正常"只会让人学会忽略这一栏。"""
    messages: list[str] = []
    if plan.missing_source_files:
        sample = "、".join(plan.missing_source_files[:3])
        messages.append(
            f"归档未携带笔记正文，目标目录下缺少 {len(plan.missing_source_files)} 个源文件"
            f"（例：{sample}）。请把源笔记放回 {plan.target_id} 的库目录——"
            f"否则检索前自动同步会判定'文件全删'并清空索引"
            f"（对齐 obsidian-rag/import.py:288-292 的第 2 条提示）"
        )
    elif not plan.source_files_included and not plan.source_files:
        messages.append(
            "归档没有记录源文件清单，无法核对目标目录是否缺文件（这份包可能是旧版本导出的）"
        )
    if plan.dropped_lexical_ids:
        messages.append(
            f"有 {len(plan.dropped_lexical_ids)} 个词法索引条目在向量里找不到对应块，已丢弃"
            f"（例：{'、'.join(plan.dropped_lexical_ids[:3])}）——"
            f"保留它们只会让 BM25 单路命中一个检索不到的块"
        )
    if not plan.vector_rows:
        messages.append("归档内没有任何向量块，导入后这个库将检索不到任何内容")
    if collisions:
        messages.append(
            f"有 {len(collisions)} 个向量块在目标库里会落到同一个 id"
            f"（{plan.target_id!r} 下的 path+chunk_index 重复），后写的会覆盖先写的："
            f"{'、'.join(sorted(collisions)[:3])}"
        )
    return tuple(messages)
