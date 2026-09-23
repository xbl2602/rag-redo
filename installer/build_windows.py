"""Windows 打包脚本：用 PyInstaller 把 gui_main.py / mcp_stdio.py 各自冻结
成 onedir 产物，并把 plugins/ 原样拷贝成产物旁边一个真实的、可编辑的
文件夹——不能让 PyInstaller 把插件代码打包进冻结产物内部，这违反
AGENTS.md"这个项目不是什么"一节的约束（v1 插件发现只扫描本地 plugins/
目录，用户手动获取插件文件夹，不是内嵌死的）。

真实在 Windows 11 上跑通过一次，踩到并解决的关键点，供以后改这份脚本
时参考：

1. **PyInstaller 的静态依赖分析看不到插件的依赖**。gui_main.py /
   mcp_stdio.py 自己只 `import core.xxx`，插件模块（比如
   official-extractor-docx 的 `import docx`）是运行时才通过
   `plugins/*/` 被塞进 sys.path 后动态 import 的，PyInstaller 分析
   gui_main.py 的调用图时根本看不到这条路径，所以 `docx`、
   `pymupdf.layout` 的 onnx 资源文件这些插件专属依赖必须显式用
   `--collect-all`/`--collect-data` 点名，不能指望自动发现。这份脚本
   点名的这几个包对应的是当前 Phase 1 官方插件集实际用到的第三方库
   （chromadb/mcp/pymupdf4llm/pymupdf/docx/jieba）——以后官方插件集
   加新依赖，这里要跟着补一项，不会自动生效。
2. **`__file__` 在冻结后不指向发行目录**，gui_main.py/mcp_stdio.py 已经
   各自加了 `sys.frozen` 判断，改用 `Path(sys.executable).parent`；这份
   脚本只管打包，不需要再处理这件事。
3. onedir（不是 onefile）——onefile 每次启动要解压到临时目录，那样
   "plugins/ 是产物旁边一个稳定路径"这个前提就不成立了，必须用 onedir。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

#: 插件动态加载导致 PyInstaller 看不到的第三方依赖，见模块 docstring
#: 第1点。改官方插件集时同步维护这份清单。
COLLECT_ALL = ["chromadb", "mcp", "pymupdf4llm", "docx"]
COLLECT_DATA = ["jieba", "pymupdf"]

TARGETS = [
    ("rag-redo-gui", "gui_main.py", "--windowed"),
    ("rag-redo-mcp", "mcp_stdio.py", "--console"),
]


def _build_one(name: str, entry: str, windowed_flag: str) -> Path:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", name,
        "--onedir", windowed_flag,
        "--specpath", str(REPO_ROOT / "build" / "specs"),
        "--distpath", str(REPO_ROOT / "dist"),
        "--workpath", str(REPO_ROOT / "build" / name),
    ]
    for pkg in COLLECT_DATA:
        cmd += ["--collect-data", pkg]
    for pkg in COLLECT_ALL:
        cmd += ["--collect-all", pkg]
    cmd.append(str(REPO_ROOT / entry))

    subprocess.run(cmd, cwd=REPO_ROOT, check=True)

    dist_dir = REPO_ROOT / "dist" / name
    plugins_dst = dist_dir / "plugins"
    if plugins_dst.exists():
        shutil.rmtree(plugins_dst)
    shutil.copytree(REPO_ROOT / "plugins", plugins_dst)
    return dist_dir


def main() -> None:
    for name, entry, windowed_flag in TARGETS:
        dist_dir = _build_one(name, entry, windowed_flag)
        print(f"built: {dist_dir}")


if __name__ == "__main__":
    main()
