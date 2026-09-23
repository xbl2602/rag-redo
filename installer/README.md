# installer/

Windows 安装包(.exe)与免安装便携版的打包脚本所在地，Phase 4（见 [../docs/ROADMAP.md](../docs/ROADMAP.md)）。

## 现状（2026-09-23，真实 Windows 11 机器上验证过）

`build_windows.py` 用 PyInstaller 把 `gui_main.py`/`mcp_stdio.py` 各自冻结成 onedir 产物（`dist/rag-redo-gui/`、`dist/rag-redo-mcp/`），并把 `plugins/` 原样拷贝成产物旁边一个真实文件夹——不打包进冻结产物内部，理由见脚本 docstring。跑法：

```bash
.venv/Scripts/python.exe installer/build_windows.py
```

真实验证过（不是假设）：两个产物都在没装 Python 的意义上是自包含的（冻结了 core/ + 官方插件集用到的全部第三方依赖），真的 Popen 启动过——GUI 那个真的弹出窗口、真的渲染出库管理/搜索界面（截图比对过和源码直接跑 `gui_main.py` 一模一样）；MCP 那个真的用子进程 + JSON-RPC over stdio 做过 `initialize`/`tools/call`，工具全部正常返回。

**已知限制，还没做**：
- 还没接 Inno Setup——现在只有免安装的 onedir 文件夹，双击 `rag-redo-gui.exe` 能跑，但没有开始菜单快捷方式/卸载入口这套包装
- 没验证过安装/卸载前后系统关键位置（注册表、全局 PATH）无残留变化——onedir 产物本身不写这些东西，但真正的"安装包"形态（Inno Setup 生成的 setup.exe）没做过，这条验收标准要等它做了才能勾
- `official-embedder-bge-m3`/`official-reranker` 懒加载的 `torch`/`sentence-transformers` 没装在打包用的 venv 里（README.md 说的"首次检索自动下载几个GB模型权重"这条路径冻结后有没有问题，没有实测过，需要真机上跑一次真实检索验证）
- 只在一台机器上验证过一次，没有测过"从这台机器打包出来的 dist/ 文件夹拷贝到一台完全没装过任何开发工具的干净 Windows 机器上能不能跑"——这是安装包最终要验收的场景，比"在打包机器本机跑冻结产物"更严格

**打包这份插件化架构时踩到的真实坑**（完整解释见 `build_windows.py` 模块 docstring）：PyInstaller 的静态依赖分析看不到插件运行时动态 import 的第三方库（比如 `official-extractor-docx` 的 `import docx`），必须显式 `--collect-all`/`--collect-data` 点名，不能指望自动发现——这是插件化架构本身带来的打包复杂度，不是 PyInstaller 用错了。
