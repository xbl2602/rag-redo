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

   **这条对本项目自己的 `core` 包同样成立，不只是第三方库**（2026-09-23
   真机安装+真实MCP协议调用时抓到的真实bug）：`core/gpu_arbiter.py`/
   `core/subprocess_service.py` 只被插件代码动态import（比如
   `official-embedder-bge-m3` 的 `from core import gpu_arbiter`、
   `official-visual-wemm` 的 `from core.subprocess_service import ...`），
   从来不被 `core/pipeline.py`/`core/runtime.py` 自己的模块内部相互
   import——PyInstaller 从 gui_main.py/mcp_stdio.py 的 `from core.pipeline
   import Pipeline` 出发做静态分析，顺着 core 包内部真实的 import 关系
   传递收集，根本不知道"某个插件运行时会来 import 这另外两个模块"，
   实测结果是打出来的冻结产物里 `core/` 包缺了这两个文件，插件在冻结
   环境里加载时报 `ImportError: cannot import name 'gpu_arbiter'`/
   `ModuleNotFoundError: No module named 'core.subprocess_service'`——
   和插件专属第三方依赖是同一类问题，同一个修法：`--collect-submodules
   core` 把整个 core 包的全部子模块无条件收进去，不管 PyInstaller 自己
   的静态分析能不能追踪到谁在用它们。
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
#: core 包自己的子模块也会被插件动态 import（见模块 docstring 第1点的
#: 真实踩坑记录）——不是数据文件，只需要子模块本身都被收进去，用
#: --collect-submodules，不需要 --collect-all 那种连带数据文件的版本。
COLLECT_SUBMODULES = ["core"]

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
    for pkg in COLLECT_SUBMODULES:
        cmd += ["--collect-submodules", pkg]
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
