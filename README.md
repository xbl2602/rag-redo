# RAG REDO

> Obsidian 笔记本地语义检索系统的完全重构版本。**当前是规划阶段，还没有可运行的软件**——如果你是来找安装教程的，还早，见下面"现状"。

## 这是什么

在自己电脑上，按意思搜自己的 Obsidian 笔记，笔记不出本机。这是 `obsidian-rag` 项目的完全重构：核心从几个几万行的大文件，改成"插件运行时+数据流管理器"这两个轻量核心组件，其余一切（选哪个 embedding 模型、支不支持 OCR、要不要页面级视觉检索、用哪个 GUI）都是可插拔的插件——像给 Minecraft 装模组一样自由组合，而不是绑死一整套功能。

## 现状

规划阶段，正在按 [docs/ROADMAP.md](docs/ROADMAP.md) 的 Phase 0 推进（插件运行时骨架）。架构设计文档已完成，欢迎阅读：

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) —— 架构总览
- [docs/DATA_FLOW.md](docs/DATA_FLOW.md) —— 数据流规则
- [docs/PLUGIN_SPEC.md](docs/PLUGIN_SPEC.md) —— 插件规范
- [docs/ROADMAP.md](docs/ROADMAP.md) —— 分阶段路线图
- [docs/FEATURE_TRIAGE.md](docs/FEATURE_TRIAGE.md) —— 旧项目能力去留提案
- [docs/LESSONS.md](docs/LESSONS.md) —— 旧项目架构教训精炼版

## 目标（做完之后应该是什么样）

- **安装**：Windows 下载一个安装包或免安装便携版，双击就能用，不需要手动装 Python/CUDA 这些环境
- **插件化**：核心极小，检索算法、embedding 模型、OCR、GUI 都是插件，可以自由启用/禁用/替换/自制
- **数据流**：所有数据怎么流、格式是什么都有统一权威定义，不是"哪里方便就从哪接一根线"
- **依赖独立**：不依赖用户本机预装的运行环境，重型可选能力按需下载，不逼着所有人一次性装一大堆用不上的东西
- **Windows 优先，Linux 可开发**

*英文版随 Phase 4 产品成型后补充（README.en.md）。*
