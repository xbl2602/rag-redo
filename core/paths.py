r"""core/paths.py — 三个入口（GUI / MCP / CLI）共用的路径锚定规则。

**为什么要有这一层**：2026-09-27 CLI 缺陷修复发现的坑——`gui_main.py` 和
`mcp_stdio.py` 各自把"数据目录在哪"这段逻辑抄了一份（两处逐字相同，所以行为
一致），而 `core/cli.py` 抄了第三份**错的**那版：三个默认路径全相对**当前工作
目录**（`--data-root` 默认 `data`、`--plugins-dir` 默认 `plugins`）。后果是
`cd C:\ && python -m core.cli index --library 我的笔记` 会在 `C:\data` 建一份
空注册表、报"未知库"，用户完全看不出这是路径问题——旧项目 `obsidian-rag/
config.py:14` 的 `DATA_DIR = Path(__file__).parent / "data"` 明确是 **CWD
无关**的，CLI 跑在任何目录都必须指向同一份数据。

所以这里把规则收成**唯一一份**（`docs/DATA_FLOW.md` 规则4"同一件事只能在一处
定义"），三个入口都调它。规则逐字保持与改造前的 `gui_main.py:22-44` /
`mcp_stdio.py:30-45` 一致，优先级不变：

1. `RAG_REDO_DATA_ROOT` 环境变量（显式指定，最高优先级，两个冻结产物都用它
   指向同一份数据）；
2. 打包安装态（`sys.frozen`）→ `%LOCALAPPDATA%/RAG-Redo/data`——Inno Setup
   卸载只删安装目录，不该连带删掉用户建好的索引库；
3. 源码运行态 → `<仓库根>/data`（与 `plugins/` 同级）。

**为什么默认锚定、显式传参仍按 CWD 解析**：用户手敲 `--data-root ./x` 时按
CWD 解析才是直觉（和所有 CLI 一样），这里不做特殊处理；被修的是**默认值**——
默认值必须与 CWD 无关，否则"在仓库根跑"和"在别处跑"会指向不同的数据。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: 数据目录环境变量名（GUI / MCP / CLI 三处共用的唯一名字）
DATA_ROOT_ENV = "RAG_REDO_DATA_ROOT"
#: 打包安装态下的应用数据子目录（%LOCALAPPDATA%\<APP_DIR>\<DATA_DIR_NAME>）
APP_DIR_NAME = "RAG-Redo"
DATA_DIR_NAME = "data"
PLUGINS_DIR_NAME = "plugins"
PLUGINS_STATE_FILE = "plugins_state.json"
#: 模型默认存放在发行目录下的这个文件夹（项目内），用户可用设置项改到别处（BC-17）
MODELS_DIR_NAME = "models"
#: 设置项名：用户自定义的模型存放路径，留空 = 默认的项目内 `models/`
MODELS_DIR_SETTING = "models_dir"
#: HuggingFace 的"模型缓存目录"环境变量——子进程（看图/本机 OCR）靠它找模型
HF_CACHE_ENV = "HF_HUB_CACHE"


def repo_root() -> Path:
    """发行目录根——`plugins/` 必须在这里，且必须是用户能自己增删的真实文件夹
    （不做插件市场）。

    打包后（PyInstaller）跑的是冻结 exe，`__file__` 指向打包器内部临时/内嵌
    路径而不是发行目录，这时必须以 exe 自己的位置为准（`plugins/` 不能被打包
    进冻结产物内部）——与改造前 `gui_main.py:22-25` / `mcp_stdio.py:30-33` 同。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # core/paths.py → 上一级就是仓库根。不写 resolve()：与 gui_main/mcp_stdio
    # 的 `Path(__file__).parent` 逐字一致，避免符号链接下三个入口算出不同的根。
    return Path(__file__).parent.parent


def data_root() -> Path:
    """应用数据目录——GUI、MCP、CLI 必须是同一个目录，否则三边看到的是三批
    不同的库（这是本次修复的核心用户可见行为）。"""
    configured = os.environ.get(DATA_ROOT_ENV)
    if configured:
        return Path(configured)
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / APP_DIR_NAME / DATA_DIR_NAME
    return repo_root() / DATA_DIR_NAME


def plugins_dir() -> Path:
    return repo_root() / PLUGINS_DIR_NAME


def models_dir(configured: object = "") -> Path:
    """模型存放目录（里面放 HuggingFace 缓存布局的 `models--<组织>--<名字>` 文件夹）。

    **规则（BC-17，旧项目没有这个能力，2026-09-29 操作者提出并确认）**：
    1. `configured`（设置项 `models_dir`）非空 → 用它。支持 `~` 与 `%VAR%`；
       相对路径按**发行目录**解析，不按当前工作目录（与 `data_root()` 同一条
       "CWD 无关"纪律，否则在别处跑一次 CLI 就会指到另一个空文件夹）。
    2. 留空 → `<发行目录>/models`（项目内，默认值）。
    3. 宽容一步：用户把路径指到 HuggingFace 的 `HF_HOME`（模型其实在它的
       `hub` 子文件夹里）时，自动落到 `hub`——只在"这一层没有任何
       `models--*`、而 `hub` 里有"时才这么做，不会误伤正常目录。

    只算路径，不创建目录、不检查里面有没有模型：文件夹不存在不是错误
    （首次下载时由 HuggingFace 自己建）。
    """
    text = str(configured or "").strip()
    if text:
        path = Path(os.path.expandvars(text)).expanduser()
        if not path.is_absolute():
            path = repo_root() / path
    else:
        path = repo_root() / MODELS_DIR_NAME
    hub = path / "hub"
    try:
        if hub.is_dir() and not any(child.name.startswith("models--") for child in path.iterdir()):
            return hub
    except OSError:
        pass
    return path


def models_env(configured: object = "", base: dict[str, str] | None = None) -> dict[str, str]:
    """给子进程（看图 WEMM / 本机 OCR MinerU）用的环境变量：继承当前环境（丢
    PATH 子进程必死，同旧项目 `gpu_arbiter._mineru_env` 的 E1 教训），只增量
    覆盖 `HF_HUB_CACHE` 让它们去同一个模型目录找/下模型。旧项目对 MinerU 子进程
    就是这么设 `HF_HOME` 的（`gpu_arbiter.py:341-354`），这里推广到所有模型
    子进程，并统一到用户可配的 `models_dir`。"""
    env = dict(os.environ if base is None else base)
    env[HF_CACHE_ENV] = str(models_dir(configured))
    return env


def plugins_state_file(root: Path | None = None) -> Path:
    """插件启用状态文件（`data/plugins_state.json`）——与数据目录同目录，
    GUI / MCP / CLI 共用同一份。`root` 给调用方一个可覆盖的数据目录；不传就用
    `data_root()`。

    形参**不能**叫 `data_root`：那会遮蔽同名的模块级函数，无参调用时
    `data_root()` 变成 `None()` 直接 TypeError（此前 CLI 恰好总是传参，所以
    一直没暴露）。"""
    base = Path(root) if root is not None else data_root()
    return base / PLUGINS_STATE_FILE
