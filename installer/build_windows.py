"""Build a self-contained Windows portable bundle.

The bundle contains a standalone Python runtime, the application source,
plugins, and two small launchers. It does not use PyInstaller or Inno Setup.
"""
from __future__ import annotations

import shutil
import sys
import sysconfig
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "dist" / "rag-redo-portable"
OUTPUT_ZIP = REPO_ROOT / "dist" / "rag-redo-portable.zip"
SOURCE_SITE_PACKAGES = Path(sysconfig.get_path("purelib"))


def _copy_tree(source: Path, target: Path, *, ignore=None) -> None:
    shutil.copytree(source, target, ignore=ignore, dirs_exist_ok=True)


def _runtime_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in {"__pycache__", "site-packages", "test", "idlelib", "tkinter", "tcl", "tkdemos"}}
    ignored.update(name for name in names if name.endswith(".pyc"))
    return ignored


def _site_packages_ignore(directory: str, names: list[str]) -> set[str]:
    """site-packages 专用的忽略规则。

    这里**不能**排除名为 "data" 的目录——不少包把资源/权重/模板放在
    `pkg/data/` 下（连 torch 生态都有），砍掉会得到一个能启动但一 import
    就炸的包。运行期产物（__pycache__/*.pyc/.venv）可以安全排除。
    """
    ignored = {name for name in names if name in {"__pycache__", ".pytest_cache", ".mypy_cache"}}
    ignored.update(name for name in names if name.endswith(".pyc"))
    return ignored


def _app_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", "tests"}}
    ignored.update(name for name in names if name.endswith(".pyc"))
    # 运行期产物一律不进包："data" 目录（插件自己的落盘目录曾误建在这里，
    # 见 plugins/official-ocr-mineru-local/official_ocr_mineru_local/data/）
    # 和 "*.log" 都是构建机上的残留，正式数据由启动器设的
    # RAG_REDO_DATA_ROOT=%LOCALAPPDATA%\RAG-Redo\data 承载，不该跟着包走。
    # 只用于 app 树（core/ 与 plugins/），site-packages 走上面那条规则。
    ignored.update(name for name in names if name == "data")
    ignored.update(name for name in names if name.endswith(".log"))
    return ignored


def _copy_runtime(target: Path) -> None:
    source = Path(sys.base_prefix)
    target.mkdir(parents=True, exist_ok=True)
    for pattern in ("python*.dll", "python*.exe", "vcruntime*.dll"):
        for path in source.glob(pattern):
            shutil.copy2(path, target / path.name)
    for directory in ("DLLs", "Lib", "Scripts"):
        source_dir = source / directory
        if source_dir.is_dir():
            _copy_tree(source_dir, target / directory, ignore=_runtime_ignore)
    if not (target / "python.exe").is_file():
        raise RuntimeError(f"便携 Python 缺少解释器: {target / 'python.exe'}")
    _copy_tree(SOURCE_SITE_PACKAGES, target / "Lib" / "site-packages", ignore=_site_packages_ignore)


def _copy_app(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for directory in ("core", "plugins"):
        _copy_tree(REPO_ROOT / directory, target / directory, ignore=_app_ignore)
    for filename in ("gui_main.py", "mcp_stdio.py", "README.md"):
        shutil.copy2(REPO_ROOT / filename, target / filename)


def _write_launchers(target: Path) -> None:
    gui = """@echo off
setlocal
set \"ROOT=%~dp0\"
set \"RAG_REDO_DATA_ROOT=%LOCALAPPDATA%\\RAG-Redo\\data\"
\"%ROOT%runtime\\python\\pythonw.exe\" \"%ROOT%app\\gui_main.py\" %*
"""
    gui_debug = """@echo off
setlocal
set \"ROOT=%~dp0\"
set \"RAG_REDO_DATA_ROOT=%LOCALAPPDATA%\\RAG-Redo\\data\"
\"%ROOT%runtime\\python\\python.exe\" \"%ROOT%app\\gui_main.py\" %*
pause
"""
    mcp = """@echo off
setlocal
set \"ROOT=%~dp0\"
set \"RAG_REDO_DATA_ROOT=%LOCALAPPDATA%\\RAG-Redo\\data\"
\"%ROOT%runtime\\python\\python.exe\" \"%ROOT%app\\mcp_stdio.py\" %*
"""
    readme = """RAG Redo 便携版

1. 双击 start-gui.cmd 启动图形界面。
2. 需要排错时双击 start-gui-debug.cmd。
3. MCP 客户端请使用 start-mcp.cmd 作为 command，并传入其完整路径。
4. 用户数据保存在 %LOCALAPPDATA%\\RAG-Redo\\data，删除本文件夹不会删除索引数据。
5. 首次使用模型功能可能需要下载模型；WEMM 首次启用会在插件目录建立独立环境。
6. 本机 PDF OCR（MinerU）按设计复用机器上已有的 uv tool 环境：
   先手动执行 uv tool install --python 3.12 -U "mineru[all]"，程序会自动探测；
   不装也能用，混合 PDF 会保持 scanned 终态，或改用 MinerU 云端。
"""
    for filename, content in {
        "start-gui.cmd": gui,
        "start-gui-debug.cmd": gui_debug,
        "start-mcp.cmd": mcp,
        "README.txt": readme,
    }.items():
        (target / filename).write_text(content, encoding="utf-8", newline="")


def _write_dependency_lock(target: Path) -> None:
    """记录构建环境的依赖版本清单（ROADMAP Phase 4"最小运行时依赖清单"的
    第一步）：便携包携带的是构建机当前 venv 的 site-packages，版本随构建机
    浮动——没有这份清单，干净机出问题时无法还原"当时到底装了什么"。用
    importlib.metadata 枚举（离线，不依赖子进程 pip）。体积优化（把清单
    收敛到真实最小集）仍待干净机验收后进行。"""
    from importlib import metadata

    lines = sorted(
        f"{dist.metadata['Name']}=={dist.version}"
        for dist in metadata.distributions()
        if dist.metadata["Name"]
    )
    (target / "requirements-lock.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline=""
    )


def build() -> Path:
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)
    _copy_runtime(OUTPUT_DIR / "runtime" / "python")
    _copy_app(OUTPUT_DIR / "app")
    _write_launchers(OUTPUT_DIR)
    _write_dependency_lock(OUTPUT_DIR)
    if OUTPUT_ZIP.exists():
        OUTPUT_ZIP.unlink()
    shutil.make_archive(str(OUTPUT_DIR), "zip", root_dir=OUTPUT_DIR.parent, base_dir=OUTPUT_DIR.name)
    return OUTPUT_ZIP


if __name__ == "__main__":
    print(f"portable bundle: {build()}")
