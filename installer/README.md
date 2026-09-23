# installer/

Windows 安装包(.exe)与免安装便携版的打包脚本所在地，Phase 4（见 [../docs/ROADMAP.md](../docs/ROADMAP.md)）。

## 现状（2026-09-23，真实 Windows 11 机器上完整验证过：打包→Inno Setup编译→真实安装→真实运行→真实卸载）

两步流程：

```bash
# 第一步：PyInstaller 把 gui_main.py/mcp_stdio.py 各自冻结成 onedir 产物
.venv/Scripts/python.exe installer/build_windows.py
# 第二步：Inno Setup 把两个 onedir 产物打包成一个 setup.exe（需要先装 Inno Setup 6：
# https://jrsoftware.org/isdl.php，或本仓库这次用的写法 —— 下载官方安装器后
# `/SP- /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER` 免UAC提权静默装）
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer/rag-redo.iss
```

产出 `dist/installer/rag-redo-setup-0.1.0.exe`（约600MB——体积偏大的原因见下方"已知限制"）。

**这一轮真实做了什么**（不是假设，每一步都真的跑过）：装了真实 Inno Setup 6 编译器、真实编译出 setup.exe、真实静默安装（`/CURRENTUSER`，不需要管理员权限/UAC）、验证开始菜单快捷方式和卸载入口都真的注册到了 `HKCU`（不碰全局注册表）、真实启动了安装后的 `rag-redo-gui.exe`（真的弹窗、真的有响应）和 `rag-redo-mcp.exe`（真的做了一遍 `initialize`/`tools/list`/`tools/call` 的 JSON-RPC over stdio 往返）、真实运行了卸载程序并确认应用目录和开始菜单快捷方式被删干净、`HKCU` 卸载注册表项被清除、而用户已经建好的索引数据（`%LOCALAPPDATA%\RAG-Redo\data`）完好保留——卸载不等于清空用户数据，这是刻意的设计（见 `installer/rag-redo.iss` 文件头的设计决策说明）。

**这一轮真实验证时抓到并修复的两个真实 bug**（不是纸面设计审查，是真装真跑才暴露的）：

1. **PyInstaller 对 `core` 包自己的子模块也会漏收**——`core/gpu_arbiter.py`/`core/subprocess_service.py` 只被插件代码动态 import（不被 `core/pipeline.py`/`core/runtime.py` 自己的模块间 import 网络覆盖到），PyInstaller 从入口脚本做静态分析时看不到"某个插件运行时会来 import 这两个模块"，冻结产物里这两个文件直接缺失，插件加载时报 `ImportError`/`ModuleNotFoundError`。修法和插件专属第三方依赖同一个套路：`build_windows.py` 加了 `--collect-submodules core`。完整解释见该脚本 docstring。
2. **`sys.executable` 在冻结产物里不是通用解释器——用它当 subprocess_service 插件的兜底解释器会导致应用自我复制成指数级增长的游离进程**（这次真实观测到几分钟内四十多个 `rag-redo-mcp.exe` 进程）。根因：`core/subprocess_service.py::resolve_plugin_python()` 找不到插件专属 venv 时原本会"退化用核心解释器"（`sys.executable`），源码/开发环境下这是安全的（`sys.executable` 是真正的 `python.exe`）；冻结产物里 `sys.executable` 是冻结 exe 自己，把它塞进 `command = ["{python}", "server.py", ...]` 里启动出来的不是子进程该跑的脚本，是把整个应用自己重新拉起一份——而这个新实例的插件启用逻辑又会再触发一次同样的尝试，递归自我复制。修法：冻结环境下这条"退化"路径直接改成抛出清楚的 `SubprocessServiceError`，插件启用失败但折叠进已有的失败处理机制，绝不允许静默产出一个会自我复制的错误命令。完整解释和这次真实抓到的过程见 `core/subprocess_service.py` 里 `resolve_plugin_python`/`_run_env_bootstrap` 两个函数的 docstring；新增回归测试见 `tests/test_subprocess_service.py::TestResolvePluginPythonEnvBootstrap` 里三个 `test_frozen_*` 用例。

**已知限制，还没解决**：
- **体积偏大**（gui/mcp 两个 onedir 各约1GB，安装包约600MB）——根因是 `official-visual-wemm` 真机验证时把 `torch`/`transformers`/`qwen_vl_utils`/`torchvision` 临时装进了核心 `.venv`（见 docs/ROADMAP.md），这些包被 PyInstaller 当成核心依赖一起打包进了两个产物；真正的解决需要先完成 `official-visual-wemm` 的 `env_bootstrap` 真实执行验证（把这些包从核心venv搬到插件自己的独立venv，见下一条），到时候核心冻结产物应该能瘦身到几十到一百多MB量级
- **`official-visual-wemm` 在冻结产物里当前不可用**——上面第2个bug修复之后，这个插件在没有自己独立环境的冻结产物里会清楚地启用失败（不是崩溃/自我复制，是干净的失败状态），因为 env_bootstrap 脚本本身也需要一个能运行任意 Python 脚本的解释器，而冻结产物里同样没有这样的解释器（`_run_env_bootstrap` 现在对冻结环境也直接拒绝执行，理由同上）。**真正的修复需要给冻结产物额外打包一份独立的、可以当通用脚本解释器用的便携 Python**（比如嵌入式 Python 发行版），这样 `env_bootstrap.py` 才有真正的解释器可用来创建 WEMM 自己的 venv——这是一块新的、还没做的打包基础设施，不是这一轮的范围
- 没测过"从这台机器打包出来的 setup.exe 拷贝到一台完全没装过任何开发工具的干净 Windows 机器上能不能装能不能跑"——这是安装包最终要验收的场景，比"在打包机器本机装/卸"更严格，这一轮只做了本机验证
- 中文安装向导语言包（`ChineseSimplified.isl`）没有随 Inno Setup 编译器官方自带，为了不引入一个未经核实来源的第三方翻译文件（供应链风险），这一版安装向导界面是英文，见 `installer/rag-redo.iss` 的 `[Languages]` 段说明
- `official-embedder-bge-m3`/`official-reranker` 懒加载的 `torch`/`sentence-transformers`——这次因为WEMM验证的缘故已经在冻结产物里了，"首次检索自动下载模型权重"这条路径本身还没有在冻结产物里真实跑一次检索验证过

**打包这份插件化架构时踩到的真实坑**（完整解释见 `build_windows.py` 模块 docstring）：PyInstaller 的静态依赖分析看不到插件运行时动态 import 的依赖——无论是第三方库（比如 `official-extractor-docx` 的 `import docx`）还是本项目自己 `core` 包里只被插件引用的子模块，都必须显式用 `--collect-all`/`--collect-data`/`--collect-submodules` 点名，不能指望自动发现——这是插件化架构本身带来的打包复杂度，不是 PyInstaller 用错了。
