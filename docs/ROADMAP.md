# 路线图

> 分阶段验收标准，风格延续旧项目 `GOAL.md` 的 C1/C2/C3 硬指标（可执行的检查方式+预期输出，不是"大概做完了"这种自评）。每个 Phase 开始前，具体验收命令会随实现细化补齐——这里先定"做什么、判断做完的标准是什么"。

## TODO（下一步，按优先级）

> 换机器/换环境接着做时先看这一节——每条都能在下面对应 Phase 的"状态"段落里找到更详细的背景，这里只列"要做什么、卡点是什么"。

1. **`official-visual-wemm`（Phase 2）**——未开始。这是一个会影响共享契约的设计决策，不是照抄OCR插件模式的体力活：需要新的 `visual_index` 扩展点、页面渲染（`pymupdf` 已有，不需要新依赖）、以及 `core/pipeline.py` 的 `search()` 融合逻辑接入第三路排名。核心问题——`core/contracts.py` 的 `SearchResult` 要不要为"页面级命中"单独建一个类型（当前是chunk级）？这会影响 GUI/MCP 两个消费方，动手前建议先过一遍再定。
2. **给 `official-ocr-mineru-local` 接真实模型（Phase 2）**——当前子进程里 `_real_ocr()` 是懒导入+清楚报错的占位，`RAG_REDO_FAKE_OCR=1` 才是测试走的路径，没有装、也没有下载任何真实OCR依赖/权重（这是本轮明确的约束：沙盒虚拟机空间有限）。真机器/有空间的环境上可以：①选定具体依赖（README 里占位写的是 `mineru`，需要确认实际包名/安装方式）；②在 `_real_ocr()` 里接真实调用；③同时把 `env_bootstrap` 的真正执行逻辑补上（见第3条），不要让重依赖悄悄装进核心 `.venv`。
3. **实现 `env_bootstrap` 的真正执行（Phase 2 遗留）**——`plugin.toml` 的 `env_bootstrap` 字段核心从来没有真正跑过；`core/subprocess_service.py` 的 `resolve_plugin_python()` 目前永远会退化成用核心自己的解释器。只要还没有插件需要真实重依赖，这个简化不算错，但接第2条之前必须先把这个补上。
4. **Phase 3 剩余两项**——库 AI 摘要（`official-library-summary`，需要先设计 `llm_provider` 扩展点+有序回退链，AGENTS.md"插件规则"一节已经写了设计意图）、Agent 写权限门禁通用化（`core/write_gate.py` 机制已实现，但还没有任何插件真的调用它触发）。
5. **Phase 4：Windows 安装包**——PyInstaller 打包这一半已经在真实 Windows 11 机器上做完并验证过（见下方 Phase 4 状态段落），免安装 onedir 产物能跑；**还没做的是 Inno Setup 安装包包装本身**（开始菜单快捷方式/卸载入口）+ 在一台"没装过任何开发工具"的干净 Windows 机器上验证打包产物能跑（目前只在打包机器本机验证过）+ 安装/卸载前后系统关键位置无残留的实测。

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

**状态：核心链路已实现并验证，安装包/GUI渲染两项待Windows环境验证（2026-09-23）。** Phase 1 目标的 10 个官方插件全部落地：`official-extractor-text/-pdf-text/-docx`、`official-chunker`、`official-library-manager`、`official-lexical-bm25`、`official-embedder-bge-m3`、`official-vector-store-chroma`、`official-fusion-rrf`、`official-reranker`、`official-mcp-server`、`official-gui-shell`。`core/pipeline.py` 编排层把它们串成真实的索引态/查询态管道。另有 2 个 Phase 3 治理类插件（`official-dedup` 近似去重、`official-import-export` 导出/导入迁移，见下方 Phase 3）已提前落地——共 14 个官方插件、227 用例、24 个测试套件，`.venv/bin/python tests/run.py` 全绿。

**目标**：达到旧项目"纯文字检索"能力的对等或更好，且是通过 Phase 0 的插件机制实现的，不是走后门直接塞进核心。

**候选官方插件**（对应 [FEATURE_TRIAGE.md](FEATURE_TRIAGE.md) 里标"Phase 1"的条目）：md/txt/pdf 文字层/docx 提取、BGE-M3 向量化、Chroma 向量库、BM25+jieba 词法索引、RRF 融合、重排器、多库/路径级勾选管理、基础 pywebview GUI（库管理+搜索+结果展示）、MCP 检索工具。

**验收标准与当前完成情况**：
- ✅ 能索引 demo-vault 和至少一个真实 Obsidian 库，检索质量不低于旧项目当前水平——`tests/test_pipeline_e2e.py` 用真实 `PluginRuntime` 扫描/加载/启用全部插件，索引临时小库后搜索，验证语义相关性、排除文件不泄漏、跨库隔离（词法+向量两路都验证过，词法这路是靠这个测试过程中发现真bug才补上的）；`plugins/official-mcp-server/tests/test_tools.py` 用真实 `MCPServer.call_tool()` 验证 MCP 协议层；额外用真实子进程（`mcp_stdio.py`）+ 真实 MCP 客户端做过一次手动冒烟，`initialize`/`list_tools`/`call_tool` 全部通过真实 stdio 协议接通。**未验证的是"和旧项目实际检索质量对比"**——这需要旧项目的真实 Obsidian 库和一批真实查询词人工比对，本轮没有这份数据，留给你实际用起来后反馈
- 🟡 Windows 安装包/便携版在一台没装过 Python 的干净 Windows 环境里，从下载到能搜到第一条结果——**PyInstaller 打包本身已经在真实 Windows 11 机器上做完并验证过**（onedir 产物真实弹窗渲染、真实 MCP 子进程通信，见 Phase 4 状态段落），但还没有"Inno Setup 安装包包装"+"在一台没装过任何开发工具的干净机器上验证打包产物"这两步，所以还不能勾满
- ✅ Linux 开发环境下等价的源码安装方式能跑通同样的功能——当前所有开发/测试都在 Linux 完成，`core/`+14个官方插件+`mcp_stdio.py`+`gui_main.py` 全部在 Linux venv 里真实跑通
- ✅ 关掉任意一个非必需插件（比如 GUI），核心+MCP 仍能正常工作——`mcp_stdio.py` 的 `REQUIRED_PLUGINS` 列表从不包含 `official-gui-shell`，MCP 全程不依赖它；`test_pipeline_e2e.py` 显式验证 gui-shell 处于"已发现未启用"状态时检索链路完全正常
- ✅ 同时装两个都声明 `embedder` 扩展点的插件，在配置里切换"当前用哪个"不需要重启进程——`ExtensionRegistry.set_active()` 机制在 Phase 0 就已验证（`core/tests/test_runtime.py::test_singleton_conflict_surfaced_not_silent`），Phase 1 未额外造第二个 embedder 实现去重复验证，机制本身没变
- ✅ 全程运行不在系统 Python 的 site-packages 或全局 PATH 留下任何痕迹——所有依赖（jieba/chromadb/pymupdf4llm/python-docx/pywebview/mcp）装在 `.venv/` 隔离环境；venv 用 `--system-site-packages` 创建是本轮唯一的例外，**只是为了在这台 Linux 开发机上借到系统已装的 PyGObject（GTK 绑定）来验证 GUI 真实渲染**，不代表产品设计要求系统预装 GTK——Windows 安装包会自带完整 webview 运行时，不依赖用户机器上有没有装什么

**GUI 渲染的真实验证**：这台开发机恰好装了 WebKit2GTK + 有响应式 X server，重建 venv 借用系统 PyGObject 后，真实调用 `webview.create_window()` + `webview.start()` 打开了窗口，用 `evaluate_js` 确认页面加载后 `document.body.innerText` 真的包含通过 js_api 桥从后端 `Api.list_libraries()` 拉回来的库名字——不是"能 import 就算过"，是真的渲染出了动态内容。后端 `Api` 类本身另有 7 个用例在 `plugins/official-gui-shell/tests/test_api.py` 里，不依赖渲染。

## Phase 2 — 视觉与 OCR 插件

**目标**：MinerU 云端 OCR、MinerU 本机 OCR、WEMM 页级视觉检索，各自独立成插件。

**状态（2026-09-23）**：
- ✅ **`core/subprocess_service.py` 落地**——之前 `core/runtime.py` 对 `subprocess_service` 插件直接 `raise NotImplementedError`（占位），现在真的实现了：`SubprocessServiceHandle` 负责启动声明的 `command`、轮询 `health_check`、本机HTTP调用方法、干净终止子进程（真实验证：`os.kill(pid, 0)` 确认进程真的没了）。自动分配空闲端口，避免多个 `subprocess_service` 插件抢端口。`in_process`/`subprocess_service` 现在走同一条 `_instantiate()` 路径，区别只在于 `entry` 指向的本地类是否用这个工具拉起子进程。8 用例，另有 `tests/test_runtime.py` 3 个真实走完整 `PluginRuntime` 生命周期的用例
- ✅ **`official-ocr-mineru-cloud`**（**改成 `in_process`，不是最初设想的 `subprocess_service`**——只是一次HTTP调用，没有需要独立环境隔离的重依赖，`subprocess_service` 反而是不必要的复杂度；`懒加载/可注入HTTP客户端`模式同 `official-embedder-bge-m3`，单测全程注入假客户端，不碰真实网络/API Key）。9 用例
- ✅ **`official-ocr-mineru-local`**（真实 `subprocess_service`：GPU资源仲裁租约协商 + 真实子进程 + 本机HTTP，`extractor:pdf` 多值扩展点，和 `official-extractor-pdf-text`/`official-ocr-mineru-cloud` 三层链式尝试——文字层→云端OCR→本机OCR，插件id字母序决定尝试顺序，不需要额外编排逻辑）。**刻意不下载真实OCR模型**（虚拟机磁盘空间有限，且插件架构本身就该是"模型无关"的——具体模型选型/权重下载应该在用户真正启用这个插件时才发生，不该为了验证插件架构本身而强绑真实下载，同 `official-embedder-bge-m3`/`official-reranker` 的懒加载纪律）；子进程内部用 `RAG_REDO_FAKE_OCR` 环境变量注入确定性假结果，测试验证的是"子进程+HTTP+chain-try链路通不通"，不是"识别准不准"。6 用例，另有 `tests/test_pipeline_e2e.py::TestOcrChainTryFallback` 2 个用例真实走完整三层链式尝试+索引+搜索
- ⬜ **`official-visual-wemm`**——未开始。和 OCR 不同，WEMM 是页级*视觉*检索，需要新的 `visual_index` 扩展点、新的页面渲染步骤（`pymupdf` 已有，不需要新依赖）、以及 `core/pipeline.py` 的 `search()` 融合逻辑接入第三路排名（当前只融合 lexical+vector 两路）——这涉及 `core/contracts.py` 的 `SearchResult` 契约要不要为"页面级命中"（而不是当前的chunk级命中）单独建一个类型，是个会影响所有 `SearchResult` 消费方（GUI/MCP）的真实设计决策，值得先和操作者过一遍再动手，不是照抄 OCR 的模式就能顺手做完的小事
- ⬜ **GPU 仲裁在 WEMM 和文字检索之间的抢占策略**——`official-ocr-mineru-local` 已经演示了"申请/释放一个具名资源租约"的最小可用集成，完整的"检索优先抢占 WEMM"时序验证要等 WEMM 落地后一起做
- ⬜ **MinerU 本机 OCR 的独立 py 环境引导（`env_bootstrap`）**——`plugin.toml` 的 `env_bootstrap` 字段核心还没有真正执行过；`core/subprocess_service.py` 的 `resolve_plugin_python()` 会在找不到插件专属 venv 时退化用核心自己的解释器，对当前"只用标准库、没有真实重依赖"的参考实现没有问题，但真的要接入需要重依赖的真实模型之前，这一步必须先落地——不能让插件在没有真正独立环境的情况下把重依赖悄悄装进核心 venv

## Phase 3 — 治理与体验类插件

**目标**：去重（MinHash+LSH）、库 AI 摘要、Agent 写权限门禁通用化、导出/导入迁移工具、旧 `libraries.json` 配置迁移脚本（一次性，对应已确认的迁移需求）。

**状态（2026-09-23）**：
- ✅ 去重——`official-dedup`，MinHash+LSH（`datasketch`），10 用例
- ✅ 旧 `libraries.json` 配置迁移——`tools/migrate_libraries_json.py`，12 用例，字段映射来自实际读旧项目 `library.py`/`index.py` 源码
- ✅ 导出/导入迁移工具——`official-import-export`（`archive_codec` 单例扩展点，zip 归档格式，manifest/vectors/bm25 三份 JSON）+ `core/pipeline.py` 的 `export_library`/`import_library`（和 `index_library`/`search` 同一种"只有编排层知道跨插件顺序"的编排方法：`archive_codec` 插件本身不知道 library_manager/lexical_index/vector_store 的存在，只负责归档的打包/解包，见 `official-import-export` 插件模块 docstring）。真实往返测试覆盖：`tests/test_pipeline_e2e.py::TestExportImportLibrary`（编排层）、`plugins/official-mcp-server/tests/test_tools.py`（MCP 工具 `export_library`/`import_library`）、`plugins/official-gui-shell/tests/test_api.py`（GUI Api，真实写/读归档文件）。已知的刻意简化：导入目标 `library_id` 如果已存在会直接拒绝，不支持"覆盖已有库"（部分覆盖导致新旧数据混杂的正确性风险大于"必须先手动删除旧库"这点不便，见 `core/pipeline.py` 的 `import_library` 注释）
- ⬜ 库 AI 摘要——未开始，需要 `llm_provider` 扩展点的懒加载/可注入 HTTP 客户端模式，留给下一轮
- ⬜ Agent 写权限门禁通用化——`core/write_gate.py` 已实现（Phase 0），但尚未被任何插件实际调用触发，见 [FEATURE_TRIAGE.md](FEATURE_TRIAGE.md)

## Phase 4 — 打包收尾与文档定稿

**状态（2026-09-23，真实 Windows 11 机器上做的，不是 Linux 沙盒里假设的）**：

- ✅ **换机器验证**：项目此前只在 Linux 沙盒里开发过，第一次在真实 Windows 11 机器上重建 `.venv`、装轻量依赖、跑 `tests/run.py`——过程中真实发现并修了4个此前从没在真机上暴露过的 Windows 兼容性 bug：①测试构造合成插件时把 `sys.executable`（Windows 路径带反斜杠）直接拼进 TOML 字符串，触发 TOML 转义解析错误；② `os.kill(pid, 0)` 探测进程存活的 POSIX 惯用法在 Windows 上不成立（抛 `OSError` 不是 `ProcessLookupError`），改用 Win32 `OpenProcess` 判断；③ `official-extractor-text` 读文件不做换行符归一化，Windows 上 `\r\n` 原样带进检索文本；④ `tests/run.py` 打印中文用例名时，非真实控制台（管道/重定向）下 stdout 编码退化成系统码页，`UnicodeEncodeError` 崩溃，改成显式 `reconfigure(encoding="utf-8")`。同时发现一个更严重的问题：`tests/test_runtime.py` 里一个测试真的启动子进程做真实验证，却没有对应的 `tearDown` 兜底清理——在一次性沙盒环境里这个坑完全不可见（进程随容器一起没了），只有在持久化的真实机器上跑几次测试后才会看到系统里堆积出真的游离 `server.py` 进程，已修（架构红线6"不产生游离进程"对测试代码自己同样适用）。全部 27 个测试套件（255+ 用例）修完后在真实 Windows 上稳定全绿。
- ✅ **GUI/MCP 两个入口在真实 Windows 上跑通**：`gui_main.py` 真的用 pywebview 弹出窗口、加载 Edge WebView2 后端、渲染出库管理/搜索界面（截图验证过内容不是空白/报错页）；`mcp_stdio.py` 真的被当子进程 Popen 起来，用真实 JSON-RPC over stdio 做过 `initialize`/`tools/list`/`tools/call`，5个工具（search_knowledge/list_libraries/reindex_knowledge/export_library/import_library）全部正常响应——这两项此前在 Linux 沙盒里从未被真实验证过（GUI 只在 WebKit2GTK 上测过，MCP 只测过协议层不测过真实 Windows 进程拉起）。
- ✅ **PyInstaller 打包**：`installer/build_windows.py` 把两个入口各自冻结成 onedir 产物，`plugins/` 保持成产物旁边一个真实可编辑文件夹（不打包进冻结产物内部，理由见脚本 docstring）。真实验证过两个冻结产物都能独立跑（不需要系统装 Python）：GUI 弹窗渲染和直接跑源码时截图比对一致，MCP 走真实子进程+JSON-RPC 全部正常。过程中发现插件化架构给打包带来的真实复杂度：PyInstaller 的静态依赖分析看不到插件运行时才动态 import 的第三方库（比如 `official-extractor-docx` 的 `docx`），必须显式点名收集，不能指望自动发现——细节见脚本 docstring 和 [installer/README.md](../installer/README.md)。
- ⬜ **Inno Setup 安装包包装**——还没做，目前只有免安装 onedir 产物，没有开始菜单快捷方式/卸载入口这套包装
- ⬜ **在一台没装过任何开发工具的干净 Windows 机器上验证打包产物**——目前只在打包机器本机验证过，这是比"本机能跑冻结产物"更严格的最终验收场景
- ⬜ 安装/卸载前后系统关键位置（注册表、全局 PATH、系统 Python 环境）无残留变化的实测——onedir 产物本身不写这些东西，但要等 Inno Setup 那层做完才能真正验收
- ⬜ README.md 补全真实安装步骤+截图，README.en.md 英文镜像同步
- ⬜ [docs/legacy/](legacy/) 归档内容与 [docs/LESSONS.md](LESSONS.md) 精炼版交叉核对不遗漏关键教训
- ⬜ 全量回归测试纪律对齐旧项目（隔离测试环境、假 HTTP 注入、隐藏测试库模式）——这条本身已经在做（见上面"换机器验证"），但还没有系统性地对照旧项目纪律逐条过一遍

## 开放问题（需要你审阅确认）

- Agent 写权限门禁、资源仲裁被我定为"核心服务"而非"插件"（ARCHITECTURE.md 2.2节），因为它们是跨插件的裁判角色，没法被单个插件公正地扮演。这个判断如果你不同意，会影响 Phase 0 的验收标准设计，请优先确认。

（2026-09-22 更新：WEMM 官方插件、Flet GUI 不迁移这两处已确认，见 [FEATURE_TRIAGE.md](FEATURE_TRIAGE.md)。同时新增两条硬约束——模型/Provider 类扩展点必须支持不重启切换、核心与插件不能污染主机环境——已写入 [AGENTS.md](../AGENTS.md)"架构红线"，下面 Phase 0/1 验收标准已同步补充对应检查项。）
