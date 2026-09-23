#!/usr/bin/env python3
"""official-visual-wemm 的 env_bootstrap 脚本——首次启用这个插件时，由
core/subprocess_service.py::resolve_plugin_python() 用核心自己的解释器
跑一次（见该函数 docstring）。职责单一：在这个脚本所在目录下建一个独立
venv，把 requirements.txt 列的依赖装进去——不做其他事，不碰核心的
`.venv`，不碰系统 Python（架构红线7）。

**这个脚本和 requirements.txt 必须和 server.py 放在同一个目录**（也就是
`official_visual_wemm/` 这个 Python 包目录本身，不是 `plugin.toml` 所在
的插件根目录）——`plugin.py::_start_handle()` 传给 `resolve_plugin_python`
的 `self._plugin_dir` 就是 `Path(__file__).parent`（`__file__` 是
plugin.py 自己的路径，和 server.py 同一个目录），`.venv` 也建在这个目录
下。2026-09-23 真机打包+真实安装验证时踩过一次这个坑：最初把这两个文件
放在插件根目录，打包后真实调用时报"env_bootstrap 脚本缺失"，才发现两处
目录概念不一致——移到这里之后就是这份文件现在实际所在的位置。

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
