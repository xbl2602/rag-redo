# RAG REDO

> Obsidian 笔记本地语义检索系统的完全重构版本。**核心检索链路已经能跑通，Windows 安装包也真实装卸验证过了**（见下面"现状"）。[English README](README.en.md)

## 这是什么

在自己电脑上，按意思搜自己的 Obsidian 笔记，笔记不出本机。这是 `obsidian-rag` 项目的完全重构：核心从几个几万行的大文件，改成"插件运行时+数据流管理器"这两个轻量核心组件，其余一切（选哪个 embedding 模型、支不支持 OCR、要不要页面级视觉检索、用哪个 GUI）都是可插拔的插件——像给 Minecraft 装模组一样自由组合，而不是绑死一整套功能。

## 现状

Phase 1（文字检索 MVP）核心链路已实现并有真实测试覆盖（350+ 用例）：19 个官方插件（多格式提取、切块、库管理、BM25 词法检索、BGE-M3 向量化、Chroma 向量库、RRF 融合、重排、MCP 工具、GUI 壳、近似去重、导出/导入、MinerU云端/本机OCR、WEMM页级视觉导航、库摘要、OpenAI兼容LLM Provider）+ 编排层 + Agent 写权限门禁。`official-visual-wemm`（页级视觉导航，独立于文字检索的"第二检索系统"，MCP 工具 `navigate_knowledge`）已经在真实 Windows 机器上完整实现并用真实 `tencent/WeMM-Embedding-2B` 模型验证过语义效果；GPU/显存精细生命周期管理（空闲卸载/自退出/主动驱逐/检索侧优先抢占）已按旧项目真实行为补齐。库摘要功能（帮 AI 在检索前先判断"这个库值不值得查"，用户手写的简介受 Agent 写权限门禁保护）已完整实现。详见 [docs/ROADMAP.md](docs/ROADMAP.md) 的逐项完成情况。

**还没做的**：Windows 安装包已经能真实装/卸（Inno Setup 打包，`/CURRENTUSER` 免管理员权限，装/卸真实验证过开始菜单快捷方式+注册表项干净、用户数据卸载后保留，见 [installer/README.md](installer/README.md)），但还没在一台"没装过开发工具"的干净机器上验证过（只在打包机器本机验证过），且当前打包产物里 `official-visual-wemm` 暂时不可用（冻结产物缺一个能跑 env_bootstrap 的独立解释器，已知限制）；`official-ocr-mineru-local` 还没接真实 OCR 模型（当前是可运行但用假结果的骨架）；查询侧 HyDE 增强（旧项目里和库摘要平行独立的功能，这次调查库摘要时确认还没做）。

架构设计文档：

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) —— 架构总览
- [docs/DATA_FLOW.md](docs/DATA_FLOW.md) —— 数据流规则
- [docs/PLUGIN_SPEC.md](docs/PLUGIN_SPEC.md) —— 插件规范
- [docs/ROADMAP.md](docs/ROADMAP.md) —— 分阶段路线图与完成情况
- [docs/FEATURE_TRIAGE.md](docs/FEATURE_TRIAGE.md) —— 旧项目能力去留与迁移状态
- [docs/LESSONS.md](docs/LESSONS.md) —— 旧项目架构教训精炼版

## 现在就能试（源码方式，Linux/Windows 都验证过）

Windows 安装包脚本已经装卸验证过（见 [installer/README.md](installer/README.md)），但还没有发布出去给人直接下载——想要安装包的话，`.venv/Scripts/python.exe installer/build_windows.py` 打包，再用 Inno Setup 编译 `installer/rag-redo.iss` 得到 `setup.exe`。下面是更常用的源码+虚拟环境这条路径，Linux/Windows 下命令等价，把 `.venv/bin/` 换成 `.venv\Scripts\`：

```bash
git clone <this-repo> rag-redo
cd rag-redo
python3 -m venv .venv
.venv/bin/pip install jieba chromadb pymupdf4llm python-docx pywebview mcp datasketch
# 下面这行是文字语义检索(BGE-M3)+重排的模型依赖，体积较大（CPU版本约几百MB到1GB），
# 首次用某个库检索时还会自动下载 BGE-M3 + 重排模型权重（几个GB，只需一次）：
.venv/bin/pip install torch sentence-transformers --index-url https://download.pytorch.org/whl/cpu
# 下面这行是可选的：只有要用 official-visual-wemm（PDF页级视觉导航，
# navigate_knowledge 工具）才需要装，体积更大（torch若已装上面那行会复用）：
.venv/bin/pip install transformers qwen-vl-utils torchvision --index-url https://download.pytorch.org/whl/cpu
```

打开图形界面（自带 [demo-vault/](demo-vault/) 可以直接拿来试，点"新建库"填个名字和 `demo-vault` 的绝对路径，再点"重建当前库索引"，然后搜"插件 架构"之类的词）：

```bash
.venv/bin/python gui_main.py
```

或者接入支持 MCP 的 AI 工具（Claude Code / opencode 等），把下面这段配置指向 `mcp_stdio.py`：

```json
{
  "type": "stdio",
  "command": "/path/to/rag-redo/.venv/bin/python",
  "args": ["mcp_stdio.py"],
  "cwd": "/path/to/rag-redo"
}
```

接好后 AI 就能用这些工具：`search_knowledge`（文字混合检索）/ `navigate_knowledge`（PDF页级视觉导航，独立的"第二检索系统"，不参与前者的融合排序）/ `list_libraries` / `reindex_knowledge` / `export_library` / `import_library` / `get_library_sample`+`propose_library_summary`+`apply_library_summary`（库摘要：帮 AI 在检索前先判断"这个库值不值得查"，用户手写的简介受写权限门禁保护，AI 不能未经确认就覆盖）。

跑测试（不需要装 torch/sentence-transformers/qwen-vl-utils——测试全程注入假模型，见 [docs/LESSONS.md](docs/LESSONS.md)）：

```bash
.venv/bin/python tests/run.py
```

## 目标（做完之后应该是什么样）

- **安装**：Windows 下载一个安装包或免安装便携版，双击就能用，不需要手动装 Python/CUDA 这些环境
- **插件化**：核心极小，检索算法、embedding 模型、OCR、GUI 都是插件，可以自由启用/禁用/替换/自制
- **数据流**：所有数据怎么流、格式是什么都有统一权威定义，不是"哪里方便就从哪接一根线"
- **依赖独立**：不依赖用户本机预装的运行环境，重型可选能力按需下载，不逼着所有人一次性装一大堆用不上的东西
- **Windows 优先，Linux 可开发**
