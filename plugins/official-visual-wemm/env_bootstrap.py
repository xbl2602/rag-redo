#!/usr/bin/env python3
"""official-visual-wemm 的 env_bootstrap 脚本——首次启用这个插件时，由
core/subprocess_service.py::resolve_plugin_python() 用核心自己的解释器
跑一次（见该函数 docstring）。职责单一：在这个脚本所在目录（也就是插件
目录本身）下建一个独立 venv，把 requirements.txt 列的依赖装进去——不做
其他事，不碰核心的 `.venv`，不碰系统 Python（架构红线7）。

跑完之后 core/subprocess_service.py 会按约定路径
（`<plugin_dir>/.venv/Scripts/python.exe` 或 `.../bin/python`）重新探测，
探测到了就用这个新装好的解释器启动子进程——这个脚本本身不负责"启动
server.py"，那是子进程正常的启动流程该做的事，职责边界干净分离。

纯标准库实现（venv/subprocess），不引入任何额外依赖——这个脚本运行的
时候，"目标环境还没装好"正是它存在的理由，不能反过来要求它自己需要
提前装点什么才能跑。
"""
from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
VENV_DIR = PLUGIN_DIR / ".venv"
REQUIREMENTS = PLUGIN_DIR / "requirements.txt"


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def main() -> int:
    if not REQUIREMENTS.is_file():
        print(f"缺少 {REQUIREMENTS}，无法确定要装什么依赖", file=sys.stderr)
        return 1

    print(f"[env_bootstrap] 在 {VENV_DIR} 建独立虚拟环境...", file=sys.stderr)
    venv.create(VENV_DIR, with_pip=True)

    venv_python = _venv_python(VENV_DIR)
    if not venv_python.exists():
        print(f"[env_bootstrap] venv 创建后没有找到预期的解释器: {venv_python}", file=sys.stderr)
        return 1

    print(f"[env_bootstrap] 装依赖（{REQUIREMENTS}）...此步骤体积较大，可能要几分钟", file=sys.stderr)
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
        cwd=str(PLUGIN_DIR),
    )
    if result.returncode != 0:
        print(f"[env_bootstrap] pip install 失败，退出码 {result.returncode}", file=sys.stderr)
        return result.returncode

    print("[env_bootstrap] 完成", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
