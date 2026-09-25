"""命令行入口：插件管理器 + 业务命令。

插件管理器部分（scan/status/load/enable/disable/unload）是 Phase 0 的
原始入口；业务命令（index/libraries/export/import/dedup）对齐旧项目的
CLI 能力（index.py:2441 / library.py:700 / export.py:258 / import.py:212 /
dedup.py:235 的命令行入口），全部是对 Pipeline 编排层的薄封装——不自己实现
任何业务顺序（架构红线：GUI/MCP/CLI 必须调用同一业务服务层）。

用法示例：
    python -m core.cli scan
    python -m core.cli libraries add D:\\notes --name 我的笔记
    python -m core.cli libraries list
    python -m core.cli index --library 我的笔记 --full
    python -m core.cli export --library 我的笔记 --out backup.zip
    python -m core.cli import backup.zip --root D:\\restored
    python -m core.cli dedup --library 我的笔记
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .runtime import PluginRuntime

# 业务命令需要启用的官方插件集（同 mcp_stdio.py 的 REQUIRED_PLUGINS 思路；
# 缺失的插件降级为警告而不是致命错误——CLI 仍可跑纯插件管理命令）。
REQUIRED_PLUGINS = [
    "official-extractor-text",
    "official-extractor-pdf-text",
    "official-extractor-docx",
    "official-chunker",
    "official-library-manager",
    "official-lexical-bm25",
    "official-embedder-bge-m3",
    "official-vector-store-chroma",
    "official-fusion-rrf",
    "official-reranker",
    "official-import-export",
    "official-ocr-mineru-cloud",
    "official-ocr-mineru-local",
    "official-dedup",
    "official-library-summary",
    "official-llm-openai-compatible",
    "official-query-enhancer-hyde",
    "official-result-advisor",
]

_BUSINESS_COMMANDS = {"index", "libraries", "export", "import", "dedup"}


def _print_status(runtime: PluginRuntime) -> None:
    if not runtime.plugins:
        print("(没有发现任何插件)")
        return
    for plugin_id, plugin in sorted(runtime.plugins.items()):
        line = f"{plugin_id:30s} {plugin.state.value}"
        if plugin.error:
            line += f"  原因: {plugin.error}"
        print(line)
    conflicts = runtime.registry.conflicts()
    if conflicts:
        print("\n冲突的单例扩展点（需要显式指定当前用哪个，不会自动选）：")
        for point, providers in conflicts.items():
            print(f"  {point}: {providers}")


def _boot_pipeline(args: argparse.Namespace):
    """业务命令的启动器：扫描并启用官方插件集，构造 Pipeline。"""
    runtime = PluginRuntime(args.plugins_dir, state_file=args.state_file, data_dir=args.data_root)
    runtime.scan()
    for plugin_id in REQUIRED_PLUGINS:
        if plugin_id not in runtime.plugins:
            print(f"警告：插件 {plugin_id} 未发现，相关能力会缺失", file=sys.stderr)
            continue
        runtime.load(plugin_id)
        runtime.enable(plugin_id)
        state = runtime.plugins[plugin_id]
        if state.state.value == "failed":
            print(f"警告：插件 {plugin_id} 启用失败: {state.error}", file=sys.stderr)
    from .pipeline import Pipeline

    return runtime, Pipeline(runtime)


def _singleton(pipeline, point: str):
    plugin_id = pipeline.runtime.registry.active_of(point)
    if plugin_id is None:
        raise SystemExit(f"错误：没有已启用的 {point} 插件，无法执行该命令")
    return pipeline._plugin(plugin_id)


def _run_business(args: argparse.Namespace) -> int:
    runtime, pipeline = _boot_pipeline(args)
    lib_mgr = _singleton(pipeline, "library_manager")

    if args.command == "libraries":
        if args.op == "add":
            root = str(Path(args.path).resolve())
            name = args.name or Path(root).name
            library_id = args.id or name
            lib_mgr.store.add_library(library_id, name, root)
            print(f"已注册库：{library_id}（{name}）→ {root}")
            return 0
        if args.op == "remove":
            cfg = lib_mgr.store.get(args.library_id)
            if cfg is None:
                print(f"错误：未知库 {args.library_id}", file=sys.stderr)
                return 1
            lib_mgr.store.remove_library(args.library_id)
            print(f"已注销库：{args.library_id}（注册表移除；索引数据保留，重新注册即可恢复）")
            return 0
        for cfg in lib_mgr.store.list_libraries():
            print(f"{cfg.library_id}\t{cfg.name}\t{cfg.root_path}")
        return 0

    if args.command == "index":
        report = pipeline.index_library(args.library, full=args.full)
        print(f"索引完成：{args.library}")
        print(
            f"  新增 {report.added} / 变更 {report.changed} / 未变 {report.unchanged}"
            f" / 删除 {report.removed} / 重试 {report.retried}"
        )
        print(f"  成功 {report.succeeded} / 失败 {report.failed} / 延后 {report.deferred}")
        for file_report in report.files:
            if file_report.extract_failure:
                print(f"  ✗ {file_report.path}: {file_report.extract_failure}")
        return 0 if report.failed == 0 else 1

    if args.command == "export":
        archive = pipeline.export_library(args.library)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        from .atomic import atomic_write_bytes

        atomic_write_bytes(out, archive)
        print(f"已导出库 {args.library} → {out}（{len(archive) / 1024 / 1024:.1f} MB）")
        return 0

    if args.command == "import":
        archive_path = Path(args.archive)
        if not archive_path.is_file():
            print(f"错误：归档不存在 {archive_path}", file=sys.stderr)
            return 1
        new_id = pipeline.import_library(
            archive_path.read_bytes(), root_path=args.root, library_id=args.library_id or None
        )
        print(f"已导入为新库：{new_id}（root={args.root}）")
        return 0

    if args.command == "dedup":
        groups = pipeline.find_duplicates(args.library, threshold=args.threshold)
        clusters = [g for groups in groups.values() for g in groups]
        if not clusters:
            print("未发现近似重复。")
            return 0
        print(f"发现 {len(clusters)} 组近似重复：")
        for group in clusters:
            print(f"  · {'  ≈  '.join(group)}")
        return 0

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag-redo")
    parser.add_argument("--plugins-dir", type=Path, default=Path("plugins"))
    parser.add_argument("--state-file", type=Path, default=Path("data/plugins_state.json"))
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("RAG_REDO_DATA_ROOT", "data")),
        help="应用数据目录（默认 ./data 或 RAG_REDO_DATA_ROOT）",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan")
    sub.add_parser("status")
    for name in ("load", "enable", "disable", "unload"):
        p = sub.add_parser(name)
        p.add_argument("plugin_id")

    # ---- 业务命令（对齐旧项目 CLI）----
    p_index = sub.add_parser("index", help="对指定库执行一次同步索引")
    p_index.add_argument("--library", required=True, help="库 id（库列表见 libraries list）")
    p_index.add_argument("--full", action="store_true", help="完整重建（忽略增量清单）")

    p_lib = sub.add_parser("libraries", help="库注册表管理（对齐旧 library.py CLI）")
    lib_sub = p_lib.add_subparsers(dest="op", required=True)
    lib_sub.add_parser("list", help="列出全部库")
    p_add = lib_sub.add_parser("add", help="注册新库")
    p_add.add_argument("path", help="笔记文件夹路径")
    p_add.add_argument("--name", default=None, help="库名（默认取文件夹名）")
    p_add.add_argument("--id", default=None, help="库 id（默认取库名）")
    p_rm = lib_sub.add_parser("remove", help="注销库（仅移出注册表，数据保留）")
    p_rm.add_argument("library_id")

    p_export = sub.add_parser("export", help="导出库为可移植归档（对齐旧 export.py CLI）")
    p_export.add_argument("--library", required=True)
    p_export.add_argument("--out", required=True, help="输出 zip 路径")

    p_import = sub.add_parser("import", help="从归档导入为新库（对齐旧 import.py CLI）")
    p_import.add_argument("archive", help="导出归档路径")
    p_import.add_argument("--root", required=True, help="新库的笔记目录")
    p_import.add_argument("--library-id", default="", help="新库 id（缺省取归档内记录）")

    p_dedup = sub.add_parser("dedup", help="近似重复检测（只读建议，对齐旧 dedup.py CLI）")
    p_dedup.add_argument("--library", required=True)
    p_dedup.add_argument("--threshold", type=float, default=0.8, help="相似度阈值（默认 0.8）")

    args = parser.parse_args(argv)

    if args.command in _BUSINESS_COMMANDS:
        try:
            return _run_business(args)
        except SystemExit as exc:
            return int(exc.code or 1) if exc.code else 1
        except KeyError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 - CLI 顶层收口，打印而非堆栈崩溃
            print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    runtime = PluginRuntime(args.plugins_dir, state_file=args.state_file)
    runtime.scan()

    if args.command in ("scan", "status"):
        _print_status(runtime)
        return 0

    if args.command == "load":
        runtime.load(args.plugin_id)
    elif args.command == "enable":
        runtime.load(args.plugin_id)
        runtime.enable(args.plugin_id)
    elif args.command == "disable":
        runtime.disable(args.plugin_id)
    elif args.command == "unload":
        runtime.unload(args.plugin_id)

    _print_status(runtime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
