"""插件管理器命令行入口。

用法：
    python -m core.cli [--plugins-dir DIR] [--state-file FILE] scan
    python -m core.cli [--plugins-dir DIR] [--state-file FILE] status
    python -m core.cli [--plugins-dir DIR] [--state-file FILE] enable <plugin_id>
    python -m core.cli [--plugins-dir DIR] [--state-file FILE] disable <plugin_id>
    python -m core.cli [--plugins-dir DIR] [--state-file FILE] unload <plugin_id>

Phase 0 验收标准要求的"全过程在插件管理器 CLI 里可见状态变化"就是这个模块；
见 ../docs/ROADMAP.md。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runtime import PluginRuntime


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag-redo-plugin-manager")
    parser.add_argument("--plugins-dir", type=Path, default=Path("plugins"))
    parser.add_argument("--state-file", type=Path, default=Path("data/plugins_state.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan")
    sub.add_parser("status")
    for name in ("load", "enable", "disable", "unload"):
        p = sub.add_parser(name)
        p.add_argument("plugin_id")

    args = parser.parse_args(argv)
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
