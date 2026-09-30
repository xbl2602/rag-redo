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

**torch 必须单独从 CUDA 源装（2026-09-29 修复的真机严重缺陷）**：
`requirements.txt` 里原本只写了一个裸 `torch`，pip 从 PyPI 解析出来的是
**CPU-only 轮子**（本机实测装成了 `torch 2.14.0+cpu`，
`torch.version.cuda = None`、`torch.cuda.is_available() = False`）。于是：

1. WEMM 服务**物理上不可能把模型装进显卡**——页级编码只能跑 CPU，慢到不可用；
2. `server.py::_vram_free_gb` 的 torch 分支在这台机器上不可用，回退到
   nvidia-smi，而 WDDM 笔记本上 nvidia-smi 的 `memory.free` 比 torch 的
   `mem_get_info` 低报约 5.2 GiB（本机实测 torch 6.878 GiB vs nvidia-smi
   1.681 GiB），于是页级索引一直卡在「空闲显存 5.4GB < 需求 5.5GB」的
   假门槛上，日志里的那个数字从一开始就是错的。

旧项目能用是因为它不隔离环境，直接调全局 Python（带 CUDA torch）；rag-redo
按架构红线7 把 WEMM 隔离进自己的 venv，隔离出来的是个没有显卡的 venv。
所以这里必须显式指定 CUDA 轮子源，并且**装完当场验证**，装成 CPU 版要当场
失败并说清楚，而不是等到索引时以"显存不够"的面貌出现。
"""
from __future__ import annotations

import json
import subprocess
import sys
import venv
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
VENV_DIR = PLUGIN_DIR / ".venv"
REQUIREMENTS = PLUGIN_DIR / "requirements.txt"

#: torch 的 CUDA 轮子源。裸 `pip install torch` 在 Windows 上装出来的是 CPU-only
#: 轮子（见模块 docstring），必须显式指到这个源才会拿到带 CUDA 的版本。
TORCH_INDEX_URL = "https://download.pytorch.org/whl/cu128"
#: 只钉到"大版本"，不钉到小版本——本机 CUDA 驱动支持到 12.8（cu128），而 CUDA
#: 轮子的可用小版本随上游发布节奏变；钉死小版本反而容易装出一个与本机驱动不
#: 匹配、且从没在这台机器上验过的组合。核心 .venv 装的是 2.11.0+cu128。
TORCH_SPEC = "torch==2.11.0"


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _pip(venv_python: Path, args: list[str]) -> int:
    return subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", *args],
        cwd=str(PLUGIN_DIR),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).returncode


def _install_rest(venv_python: Path) -> int:
    """装 requirements.txt 里的其余依赖，**并且必须让 pip 换不掉 torch**。

    这一步不是多余的（2026-09-29 实测连踩两次）：

    第一次踩：requirements.txt 里列了 `torchvision`，它硬依赖一个精确的
    `torch==<版本>`，pip 从默认 PyPI 解析出 CPU-only 轮子，把第一步装好的
    CUDA 轮子整体换掉（venv 体积 5GB → 0.9GB，装完又变回 `2.14.0+cpu`）。

    第二次踩：只把 `torch` 从 requirements.txt 拿掉、给 pip 加
    `--extra-index-url` 指向 CUDA 源，仍然失败——因为 pip 要的 `2.14.0` 在
    cu128 源上根本不存在（那上面只有 2.9.0/2.9.1/2.10.0/2.11.0），只能回 PyPI。
    可见光"让 pip 看得见 CUDA 源"不够，必须**同时禁止它换版本**。

    所以这里用约束文件（`-c`）把 torch 钉死在 `2.11.0+cu128`：pip 解析依赖时
    一旦有包要求别的 torch 版本，会当场报冲突失败，而不是悄悄装出一个 CPU 版
    ——"装不上"是好的失败方式，"装上了但用不了显卡"要难查得多。
    """
    constraint = PLUGIN_DIR / "constraints-cuda.txt"
    try:
        constraint.write_text(f"{TORCH_SPEC}+cu128\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"[env_bootstrap] 写约束文件失败: {exc}", file=sys.stderr)
        return 1
    return _pip(
        venv_python,
        [
            "--extra-index-url", TORCH_INDEX_URL,
            "-c", str(constraint),
            "-r", str(REQUIREMENTS),
        ],
    )


def _verify_cuda(venv_python: Path) -> int:
    """装完当场确认这个解释器的 torch 真的能用 CUDA。

    不验证的后果已经踩过：装成 CPU 版时一切"正常"，直到页级索引卡在假显存
    门槛上，日志指向显存不够，真因是环境里没有 CUDA——排查方向完全被带偏
    （2026-09-29 真实排查过程）。所以这里失败要当场失败、说清楚。
    """
    probe = (
        "import json, torch;"
        "print(json.dumps({"
        "'version': torch.__version__,"
        "'cuda_build': torch.version.cuda,"
        "'available': bool(torch.cuda.is_available()),"
        "'device': torch.cuda.get_device_name(0) if torch.cuda.is_available() else None"
        "}))"
    )
    result = subprocess.run(
        [str(venv_python), "-c", probe],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        print(f"[env_bootstrap] 无法导入 torch: {result.stderr.strip()}", file=sys.stderr)
        return 1
    try:
        info = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"[env_bootstrap] torch 探测输出无法解析: {result.stdout!r}", file=sys.stderr)
        return 1
    if not info.get("available"):
        print(
            "[env_bootstrap] 装到的 torch 没有 CUDA 支持"
            f"（{info.get('version')}，cuda={info.get('cuda_build')}）——"
            f"页级视觉索引只能跑 CPU，实际不可用。\n"
            f"           请确认 {TORCH_INDEX_URL} 上有与本机驱动匹配的轮子。",
            file=sys.stderr,
        )
        return 1
    print(
        f"[env_bootstrap] torch {info['version']} CUDA 就绪，设备：{info.get('device')}",
        file=sys.stderr,
    )
    return 0


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

    # ① torch 单独从 CUDA 源装。放第一位：它是最大也最容易失败的依赖，先装它
    #    才能在几分钟内就暴露"这个源上没有可用轮子"，而不是等装完 4 个包才发现。
    print(
        f"[env_bootstrap] 装 torch（{TORCH_SPEC}，源 {TORCH_INDEX_URL}）..."
        "体积较大，可能要几分钟",
        file=sys.stderr,
    )
    if _pip(venv_python, ["--index-url", TORCH_INDEX_URL, TORCH_SPEC]) != 0:
        print("[env_bootstrap] 从 CUDA 源装 torch 失败", file=sys.stderr)
        return 1

    # ② 其余依赖。requirements.txt 里不再列 torch——`torchvision` 会顺带把它从
    #    PyPI 拉下来替换掉 CUDA 轮子（见 `_install_rest` 的 docstring），所以这里
    #    必须带着同一个 CUDA 源。
    print(f"[env_bootstrap] 装其余依赖（{REQUIREMENTS}）...", file=sys.stderr)
    if _install_rest(venv_python) != 0:
        print("[env_bootstrap] 装其余依赖失败", file=sys.stderr)
        return 1

    # ③ 当场验证，别把"没有 CUDA"留到索引时以"显存不够"的面貌暴露。
    if _verify_cuda(venv_python) != 0:
        return 1

    print("[env_bootstrap] 完成", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
