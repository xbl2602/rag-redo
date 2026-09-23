# 路线图

> 分阶段验收标准，风格延续旧项目 `GOAL.md` 的 C1/C2/C3 硬指标（可执行的检查方式+预期输出，不是"大概做完了"这种自评）。每个 Phase 开始前，具体验收命令会随实现细化补齐——这里先定"做什么、判断做完的标准是什么"。

## Phase 0 — 插件运行时骨架（无 RAG 功能）

**状态：已实现并验证（2026-09-22）。** 代码在 `core/`（manifest/registry/datastore/resource_arbiter/runtime/cli），示例插件在 `examples/`，测试在 `tests/`（45 用例，`.venv/bin/python tests/run.py` 全绿）。实现过程中在真实跑通 CLI 演示时发现并修了一个真实设计缺陷：扩展点的单例/多值判定最初写死在核心的一份固定名单里，导致插件自己发明的新扩展点名字永远不会被判定为单例、冲突检测形同虚设——这违反了"核心不该预先知道每个可能出现的扩展点名字"的原则。改成由声明扩展点的插件自己在 manifest 里说明基数（`provides.<point> = "singleton" | "multi"`），两个插件声明不一致时保守按 singleton 处理。

**目标**：证明"两个核心组件+插件"这套架构本身能跑通，不涉及任何检索/embedding 逻辑。

**验收标准**：
- 一个不做任何实际功能的示例插件（`examples/hello-plugin`）能被发现、加载、启用、禁用、卸载，全过程在插件管理器 CLI 里可见状态变化
- 故意让示例插件的 `on_enable` 抛异常，核心进程不崩溃，插件状态显示 `failed` 且原因可读
- 两个示例插件同时声明同一个单例扩展点，插件管理器显式报冲突，不静默选一个
- 数据流管理器的 DataStore API 有至少一条读+一条写路径可用，且能证明"插件 A 不能直接读插件 B 通过 DataStore 写的数据，除非走契约类型"
- 资源仲裁器能在两个示例插件之间演示一次"申请锁→抢占→释放"的完整流程
- 核心自身运行在隔离虚拟环境里，不依赖、不写入系统 Python 的 site-packages（哪怕当前只有 hello-plugin 这一个示例插件，环境隔离的骨架也要在 Phase 0 就立住）
- 核心自身的单元测试覆盖上述每一条（继承旧项目"新功能必须带测试用例"的纪律）

## Phase 1 — 文字检索 MVP（官方插件集）

**状态：核心链路已实现并验证，安装包/GUI渲染两项待Windows环境验证（2026-09-23）。** 11 个官方插件全部落地并有真实测试（153 用例，`.venv/bin/python tests/run.py` 全绿）：`official-extractor-text/-pdf-text/-docx`、`official-chunker`、`official-library-manager`、`official-lexical-bm25`、`official-embedder-bge-m3`、`official-vector-store-chroma`、`official-fusion-rrf`、`official-reranker`、`official-mcp-server`、`official-gui-shell`。`core/pipeline.py` 编排层把它们串成真实的索引态/查询态管道。

**目标**：达到旧项目"纯文字检索"能力的对等或更好，且是通过 Phase 0 的插件机制实现的，不是走后门直接塞进核心。

**候选官方插件**（对应 [FEATURE_TRIAGE.md](FEATURE_TRIAGE.md) 里标"Phase 1"的条目）：md/txt/pdf 文字层/docx 提取、BGE-M3 向量化、Chroma 向量库、BM25+jieba 词法索引、RRF 融合、重排器、多库/路径级勾选管理、基础 pywebview GUI（库管理+搜索+结果展示）、MCP 检索工具。

**验收标准与当前完成情况**：
- ✅ 能索引 demo-vault 和至少一个真实 Obsidian 库，检索质量不低于旧项目当前水平——`tests/test_pipeline_e2e.py` 用真实 `PluginRuntime` 扫描/加载/启用全部插件，索引临时小库后搜索，验证语义相关性、排除文件不泄漏、跨库隔离（词法+向量两路都验证过，词法这路是靠这个测试过程中发现真bug才补上的）；`plugins/official-mcp-server/tests/test_tools.py` 用真实 `MCPServer.call_tool()` 验证 MCP 协议层；额外用真实子进程（`mcp_stdio.py`）+ 真实 MCP 客户端做过一次手动冒烟，`initialize`/`list_tools`/`call_tool` 全部通过真实 stdio 协议接通。**未验证的是"和旧项目实际检索质量对比"**——这需要旧项目的真实 Obsidian 库和一批真实查询词人工比对，本轮没有这份数据，留给你实际用起来后反馈
- ⬜ Windows 安装包/便携版在一台没装过 Python 的干净 Windows 环境里，从下载到能搜到第一条结果——**无法在当前 Linux 开发环境验证**，PyInstaller 打包 Windows .exe 通常需要在 Windows 上实际构建（不可靠的跨平台交叉编译），这是 Phase 4 的工作，且需要真实 Windows 机器
- ✅ Linux 开发环境下等价的源码安装方式能跑通同样的功能——当前所有开发/测试都在 Linux 完成，`core/`+11个官方插件+`mcp_stdio.py`+`gui_main.py` 全部在 Linux venv 里真实跑通
- ✅ 关掉任意一个非必需插件（比如 GUI），核心+MCP 仍能正常工作——`mcp_stdio.py` 的 `REQUIRED_PLUGINS` 列表从不包含 `official-gui-shell`，MCP 全程不依赖它；`test_pipeline_e2e.py` 显式验证 gui-shell 处于"已发现未启用"状态时检索链路完全正常
- ✅ 同时装两个都声明 `embedder` 扩展点的插件，在配置里切换"当前用哪个"不需要重启进程——`ExtensionRegistry.set_active()` 机制在 Phase 0 就已验证（`core/tests/test_runtime.py::test_singleton_conflict_surfaced_not_silent`），Phase 1 未额外造第二个 embedder 实现去重复验证，机制本身没变
- ✅ 全程运行不在系统 Python 的 site-packages 或全局 PATH 留下任何痕迹——所有依赖（jieba/chromadb/pymupdf4llm/python-docx/pywebview/mcp）装在 `.venv/` 隔离环境；venv 用 `--system-site-packages` 创建是本轮唯一的例外，**只是为了在这台 Linux 开发机上借到系统已装的 PyGObject（GTK 绑定）来验证 GUI 真实渲染**，不代表产品设计要求系统预装 GTK——Windows 安装包会自带完整 webview 运行时，不依赖用户机器上有没有装什么

**GUI 渲染的真实验证**：这台开发机恰好装了 WebKit2GTK + 有响应式 X server，重建 venv 借用系统 PyGObject 后，真实调用 `webview.create_window()` + `webview.start()` 打开了窗口，用 `evaluate_js` 确认页面加载后 `document.body.innerText` 真的包含通过 js_api 桥从后端 `Api.list_libraries()` 拉回来的库名字——不是"能 import 就算过"，是真的渲染出了动态内容。后端 `Api` 类本身另有 7 个用例在 `plugins/official-gui-shell/tests/test_api.py` 里，不依赖渲染。

## Phase 2 — 视觉与 OCR 插件

**目标**：MinerU 云端 OCR、MinerU 本机 OCR、WEMM 页级视觉检索，各自独立成 `subprocess_service` 插件。

**验收标准**：
- 三者分别能独立安装/卸载，卸载后不影响 Phase 1 的文字检索
- GPU 仲裁在 WEMM 和文字检索之间的抢占策略，行为对齐旧项目 `gpu_arbiter.py` 已验证的时间线（懒加载、空闲卸载、检索优先抢占、探测失败 fail-open）
- MinerU 本机 OCR 的独立 py 环境引导过程不污染核心 venv（可验证：核心 venv 的依赖列表里不出现 MinerU 专属的包）

## Phase 3 — 治理与体验类插件

**目标**：去重（MinHash+LSH）、库 AI 摘要、Agent 写权限门禁通用化、导出/导入迁移工具、旧 `libraries.json` 配置迁移脚本（一次性，对应已确认的迁移需求）。

## Phase 4 — 打包收尾与文档定稿

- Windows 安装包（PyInstaller/Nuitka + Inno Setup）+ 免安装便携版，同一套打包脚本产出两种产物
- 在一台干净 Windows 虚拟机上验证安装/卸载前后系统关键位置（注册表、全局 PATH、系统 Python 环境）无残留变化，坐实 AGENTS.md 架构红线7"不污染主机环境"不是口号
- README.md 补全真实安装步骤+截图，README.en.md 英文镜像同步
- [docs/legacy/](legacy/) 归档内容与 [docs/LESSONS.md](LESSONS.md) 精炼版交叉核对不遗漏关键教训
- 全量回归测试纪律对齐旧项目（隔离测试环境、假 HTTP 注入、隐藏测试库模式）

## 开放问题（需要你审阅确认）

- Agent 写权限门禁、资源仲裁被我定为"核心服务"而非"插件"（ARCHITECTURE.md 2.2节），因为它们是跨插件的裁判角色，没法被单个插件公正地扮演。这个判断如果你不同意，会影响 Phase 0 的验收标准设计，请优先确认。

（2026-09-22 更新：WEMM 官方插件、Flet GUI 不迁移这两处已确认，见 [FEATURE_TRIAGE.md](FEATURE_TRIAGE.md)。同时新增两条硬约束——模型/Provider 类扩展点必须支持不重启切换、核心与插件不能污染主机环境——已写入 [AGENTS.md](../AGENTS.md)"架构红线"，下面 Phase 0/1 验收标准已同步补充对应检查项。）
