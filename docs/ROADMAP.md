# 路线图

> 分阶段验收标准，风格延续旧项目 `GOAL.md` 的 C1/C2/C3 硬指标（可执行的检查方式+预期输出，不是"大概做完了"这种自评）。每个 Phase 开始前，具体验收命令会随实现细化补齐——这里先定"做什么、判断做完的标准是什么"。

## TODO（下一步，按优先级）

> 换机器/换环境接着做时先看这一节——每条都能在下面对应 Phase 的"状态"段落里找到更详细的背景，这里只列"要做什么、卡点是什么"。

1. ~~`official-visual-wemm`（Phase 2）~~——**已完成（2026-09-23）**，见下方 Phase 2 状态段落。
2. ~~GPU/显存精细生命周期管理~~——**已完成（2026-09-23）**，见下方 Phase 2 状态段落。
3. ~~`env_bootstrap` 的真正执行（Phase 2 遗留）~~——**已完成（2026-09-24）**：`core/subprocess_service.py::resolve_plugin_python()` 会在冻结或便携运行环境中寻找 `runtime/python/python.exe`，用它执行插件声明的 `env_bootstrap.py`；`official-visual-wemm` 保留真实独立 venv 引导，测试仍可用 `RAG_REDO_SKIP_ENV_BOOTSTRAP` 跳过重依赖安装。便携 Python 已纳入 `installer/build_windows.py`，干净机验收仍未完成。
4. ~~给 `official-ocr-mineru-local` 接真实模型（Phase 2）~~——**已完成（2026-09-23），真机真模型验证过，过程中发现并修复一个真实的子进程死锁bug**：这台机器上用户此前已经用 `uv tool install --python 3.12 -U "mineru[all]"` 装过一份完整的 MinerU 工具环境（含 CUDA torch，模型权重已在 `mineru.json` 声明的 `models-dir` 里），架构决策是"探测复用外部工具环境"而不是像 `official-visual-wemm` 那样用 `env_bootstrap` 建独立venv重装一遍——原因是 MinerU 官方就是发行成独立命令行工具的，重装一遍纯粹浪费磁盘且会触发不必要的重新下载（用户明确反馈"已经有本地MinerU了，不要重复下载"），照抄 `obsidian-rag/gpu_arbiter.py::_resolve_mineru_python` 的探测路径（`%APPDATA%/uv/tools/mineru/Scripts/python.exe` 等标准落点），`RAG_REDO_MINERU_PYTHON` 环境变量可显式覆盖。`server.py` 真正调用 MinerU 官方 `mineru.cli.api_client.ReusableLocalAPIServer`（懒加载内服务+两级空闲释放+`/evict`软驱逐+单文件超时随页数缩放，结构对齐 obsidian-rag/mineru_server.py）。**真机验证时抓到一个新的、之前完全没预料到的严重bug**：内服务（真正跑模型的 uvicorn 子进程）会卡死在"进程活着但 `/health` 永远连不上"——根因是 MinerU 官方代码启动这个内服务子进程时没有显式指定 `stdout`/`stderr`（继承调用方fd），而调用方正是被 `core/subprocess_service.py::SubprocessServiceHandle` 用 `stdout=PIPE, stderr=PIPE` 启动、平时没人持续排空的这个OCR壳进程——内服务自己的启动日志量一旦写满 Windows 管道默认的64KB缓冲区，`write()` 系统调用直接阻塞，内服务从此再也起不来（实测卡满 300s 就绪超时；脱离这层管道单独跑反而几秒钟就绪，两相对比才定位到）。修法：壳进程一启动就把自己的 fd 1/2（不是 Python 层面的 `sys.stdout` 对象，必须 `os.dup2` 才对子进程继承生效）重定向到一个真实日志文件，对齐 obsidian-rag 自己把 `mineru_server.py` 启动时指向真实文件而非调用方管道的既有做法，完整解释见 `server.py::_redirect_stdio_to_logfile` 的 docstring。修复后真实验证：真实渲染一份无文字层的PDF（中文+英文混排，用系统 `msyh`/pymupdf内置CJK字体渲染，确认过没有文字层）交给本机 `pipeline` 后端识别，端到端约20~40秒（含内服务冷启动+真实 Layout/OCR-det/OCR-rec 模型推理），中英文文字都被正确识别、GPU资源租约/抢占协议复用 `official-visual-wemm` 同一套、禁用后确认无游离进程残留（`taskkill /F /T` 连内服务子进程一起清干净）。11个新增/更新用例全部走真实 `PluginRuntime` 生命周期（含新增的解释器探测测试——真实按本机既有 uv tool 安装路径探测到位，不是mock）。
5. ~~Phase 3 剩余两项（库 AI 摘要 + Agent 写权限门禁通用化）~~——**已完成（2026-09-23）**，见下方 Phase 3 状态段落。新发现一项未跟踪的功能缺口：调查过程中确认旧项目 `retriever.py::hyde_generate`（查询侧 HyDE 增强，让 LLM 为查询生成一段假设答案文档再去检索）是和库摘要**平行、独立**的功能，用同一协议但不同的配置端点（`hyde_llm_url`/`hyde_llm_model` vs `library_summary_llm_url`/`library_summary_llm_model`）——HyDE 已于 2026-09-24 作为独立 `official-query-enhancer-hyde` 插件完成（`query_enhancer` 多值扩展点）：默认关闭，首轮 top1 低于 `hyde_min_confidence=0.5` 才调用独立 OpenAI 兼容端点生成 60~150 字假设文档，重查后仅在第二轮置信度严格更高时替换，失败静默保留首轮；端点/模型/API Key/超时/token 均保留独立设置与环境变量覆盖。
6. ~~Phase 4：Windows 分发~~——**已切换为便携 ZIP（2026-09-24）**：`build_windows.py` 不再运行 PyInstaller，也不再依赖 Inno Setup；它生成自带独立 Python、核心代码、插件和启动脚本的 `dist/rag-redo-portable.zip`。仍需在一台没装过任何开发工具的干净 Windows 机器上验证，并继续做体积优化。
7. ~~多库并查检索选择+folder过滤+置信度真分尺度~~——**已完成（2026-09-23）**：对照 obsidian-rag/retriever.py::hybrid_search 逐项核对 `search_knowledge` 时发现的真实缺口（此前 ROADMAP 从未把这单独列为追踪项，不是遗漏了已知计划，是这轮对照审计才发现），见下方 Phase 1 状态段落。
8. ~~全面功能审计（2026-09-23）发现的新缺口~~——**核心行为契约已全部完成（2026-09-24）**，见下方独立小节及其后续段落"操作者 `/goal` 锁定"完成所有！"后续完成情况"。仍未完成的是本地 Windows 干净机安装验收、GUI 的操作者人工验收（2026-09-28 已补自动化的真实进程冒烟与浏览器联调，见下方"2026-09-26～28"一节；BC-15 仍为 `partial`）和打包体积优化，不是行为契约缺口。
9. ~~完整文件级增量索引~~——**已完成（2026-09-24）**：per-file manifest + 分段 generation + 精确阶段失效 + 原子发布 + 无重算 compaction，文字/BM25/提取缓存/WEMM 页库全部接入；MCP/GUI 同时支持增量和显式完整重建。
10. **业务 CLI 移植（顺延）**——旧项目有完整业务 CLI（`index.py:2441` 命令行索引、`library.py:700` 库注册管理、`import.py`/`export.py`、`dedup.py`），rag-redo 的 `core/cli.py` 只有插件管理命令（scan/status/load/enable/disable/unload），不是同一业务入口。操作者 2026-09-25 拍板低优先顺延：命令应为 Pipeline 编排层的薄封装（同 MCP/GUI 调同一服务层的纪律），不含新业务逻辑。
11. **每插件导入隔离（登记）**——`core/runtime.py` 把插件目录加进 sys.path 才能加载入口模块，两个插件若有同名顶层模块会冲突；真实的每插件导入隔离（子进程内加载或独立命名空间）待有真实冲突案例再设计。
12. **DataStore 深度封装（登记）**——StorageHandle 仍返回裸 Path（in_process 受信任插件定位下够用，见 core/datastore.py docstring 的边界说明）；`DataStore.write/read` 契约值机制已实现但生产数据流暂无调用方（插件通信走 Pipeline 编排）；`gui_main.py` 直传 lib_mgr 插件实例给 GUI Api 属有记录的薄封装层偏离。三者都是架构改进项，不是行为缺口。

## 2026-09-26～28 GUI 启动修复与完整审计闭环

> **起因与纠正**：2026-09-28 的完整审计（对照 `PROBLEMS_2026-09-28.md` 的 A1–A22）发现，此前本文档与行为契约里"GUI 已完成"的表述**不实**——`official-gui-shell` 被运行时判为 `invalid`，GUI 实际起不来；而 60+ 条 GUI 测试全绿，因为它们直接实例化 `Api`、对源码文本做 grep，没有任何一条走过真实的插件加载路径。回归基线也不是文档里记的全绿，而是 **48/51**（`test_api` 22 报错、MCP `find_duplicates` 4 失败、`test_index_progress` 间歇失败）。操作者用 `/goal` 锁定："先把 GUI 搞定（不然没法上手测试），然后其余可以解决的一次性解决"（`GOAL.md` C6–C12）。
>
> **结果**：全量回归 **53/53**（`tests/run.py` 退出码 0；日志 0 条 Traceback、0 条 SyntaxWarning；回归结束后无遗留 WEMM/MinerU 服务进程）。约 80 个文件、累积 3 天的未提交改动已按主题拆成多个提交（`git log e141ce4..HEAD`；按文件粒度拆分，单个提交不保证独立全绿），**未 push**。

**GUI（审计 A1 / A3–A8 / A10 / A12 / A16）**
- **真实启动路径门禁**（`plugins/official-gui-shell/tests/test_boot.py`）：全部 20 个必需插件经真实 `PluginRuntime` 加载并启用后无一 `invalid`/`failed`；用假 `webview` 跑**真实的** `gui_main.main()`（临时数据目录）：窗口规格与冻结夹具逐项一致、`bind_window` 被调用、推送线程真的向前端推了真实快照并在窗口关闭后停止；必需插件起不来时 `main()` 打印真实原因并非零退出（此前静默放过）；`paths.plugins_state_file()` 无参可用，GUI/MCP/CLI 三入口共用 `core/paths.py`（此前 CLI 抄了一份相对工作目录的错版本）。
- **桥接层重写**（`contract_bridge.py`）：旧 `guiweb/contracts.md` 的 37 个方法逐个真实调用，返回值键集对冻结夹具 `legacy_guiweb_contract.json` 逐个对账（不只比签名）；库"显示名"与 `library_id` 分离（输出显示名，输入按 id、再按唯一显示名解析，重名/未知名明确报错而不是假成功）；进度按 `index_status` 映射成旧形状；提取试验台在子进程里跑、可取消、180 秒硬超时；`index_stats.files` 口径同旧 `meta_stats_for`（含落终态/失败的文件），有测试钉住（1 个 empty + 2 个成功 → `files == 3`，成功数另看 `index_failures()["succeeded"]`）。
- **设置页**（`settings_schema.py`）：只登记 rag-redo 真的有人读的键（9 个分组，不做假开关），整批保存全有或全无；`test_settings_schema.py` 用 AST 把每个键对账到 core/插件里真实的 `settings.get(...)` 调用点与默认值，漂移即红。
- **`test_api.py` 迁移**到新 API（原 22 个报错清零），前端资产仍是逐字节复刻并由 sha256 固定。
- **真实进程冒烟**（本机、临时数据目录）：`gui_main.py` 启动后 6~8 秒内出现标题 **"Obsidian RAG"** 的窗口，标准错误为空；正常关窗后整棵进程树无残留。**标题后续（2026-09-29）**：操作者确认窗口标题定为 "Obsidian RAG 2.0"，`gui_main.WINDOW_SPEC`、夹具 `redo_title` 与 `GOAL.md` C6 冒烟命令已同步。
- **前端对真实后端的浏览器联调**（harness 只放在临时目录，不入库；真实 `Api` + 真实推送循环，仅嵌入/重排/LLM 为假）：图谱、库、勾选范围（点击关闭 `empty.md` → 保存 → 回读进入排除名单）、检索（含结果建议、双链出入链）、设置（保存后回读）、索引、试验台、诊断（含去重）全部渲染并可操作；0 个 JS 报错、0 个失败请求。**未覆盖**：点击"开始索引"会拉起真实索引 worker 并加载真实 BGE-M3，联调未走这条；图谱在只有 8 个节点的小库上初始取景偏离、节点落在可视区之外（点"适应视图"后正常；`graph()` 载荷的键集与前端自带 `mock.js` 一致，判断是冻结前端自身的初始布局行为，原因未深究，也没有拿旧后端同数据对照）；自动化窗格里 `requestAnimationFrame` 被冻结，联调垫片用定时器模拟。
- **联调中发现并修复的真实缺陷**：`gui_main.main()` / `mcp_stdio.main()` 退出时不收口运行时，插件拉起的 WEMM 看图服务（及其子进程）在 Windows 上不会跟着父进程走——每开关一次窗口留下一对孤儿进程（真实进程冒烟复现并验证已消除）。修法：两个入口的任何退出路径都 `runtime.close()`；同时让 `PluginRuntime.close()` **不再改写**磁盘上的启用记录（此前每次退出都把 `plugins_state.json` 擦成 `{"enabled": []}`，`scan()` 的自动恢复形同虚设）。A19"全量回归遗留游离 WEMM server"此前没有单独定位泄漏源；入口与 CLI 都收口后，全量回归结束时已无任何遗留服务进程（验证标准：回归前后 `python.exe` 进程数不变、无 `server.py` 服务进程）。

**其余审计项**
- **A2** `find_duplicates`：单库不再崩溃；多库（含默认 `libraries=""`）按每个库各自的 Agent 授权格式过滤，未授权格式的文件名与重复关系不出现在返回里（BC-02 保持 `pass` 的前提）。
- **A9** `IndexWorkerManager.stop`：读侧瞬时失败（Windows 句柄被占）不再被误报成"拒绝停止"，读取带同一套退避重试；用注入 `PermissionError` 的确定性用例 + 20 次真实停止循环 0 失败。
- **A11** CLI 补齐 `official-visual-wemm`：三入口业务插件清单同源（差异只允许各自的门面插件，`test_cli` 钉住），视觉插件只在 `index/export/import` 命令里启用，命令结束收口。
- **A14** 契约门禁校验 `test_refs` 真实存在（文件、类、方法按 AST 查，不靠子串），并带自测——构造一条失效引用门禁必须转红；顺手修掉 BC-08/BC-09 里指向不存在测试的悬空引用。
- **A17** 两处 `SyntaxWarning` 清零（全库 `-W error` 编译通过）；**A19** 见上（回归后 0 遗留进程）；**A20** 测试替身补 `release_gpu_slot`/`idle_check`，日志无回溯；**A21** 失效注释/docstring 已更正（12 分组、MRO、`bind_window`、三入口共用）。
- **A22** `unpack` 体积上限：内存内解包遇到压缩炸弹必须在读取任何条目之前拒绝，按 zip 头声明的总解压体积判断，`MAX_UNPACKED_BYTES = 8 GiB`。**旧项目没有这道闸**（`import.py` 用 `extractall` 落盘），属新增防护，阈值 8 GiB 已于 2026-09-29 经操作者确认，登记在 BC-10。
- **升级路径说明（A22 后半）**：库 id → 落盘目录名统一为"安全名 + 短哈希"（`core/library_key.py`）后，此前版本写在**无哈希后缀旧目录名**下的双链关系与失败诊断不会再被读到（`note_relations` 回 `resolved=False`、`index_failures` 报无记录），**没有做自动迁移**；该库下一次索引会按新目录名重新生成，无需手工处理。带空格/中文的库 id 本身不受影响。

**待操作者决定的事项——2026-09-29 已逐条拍板并登记**
- **A13 两处对旧行为的偏离**——操作者批准登记：文本提取器剥 UTF-8 BOM + 换行归一为 LF（登记在 BC-04）；单例守卫不做 PID 预检（登记在 BC-06）。
- **BC-15 的 4 处差异**——操作者批准登记（BC-15 acceptance）：窗口标题定为 "Obsidian RAG 2.0"、设置页只列真实读取的键（9 组 vs 旧 13 组）、切块粒度/collection 不能按库覆盖（**已知能力缺口，接受现状**；日后若要补齐需单独立项）、进度条 pct 取 0~100（旧项目自身 bug）。
- **`unpack` 体积上限 8 GiB**——操作者确认，登记在 BC-10。
- **`%TEMP%` 里约 2615 个历史测试残留目录**（Chroma 句柄未释放的存量）：操作者决定暂不处理；GUI 测试环境已改成"先关运行时再清理"，不再新增。
- **BC-15 / BC-16 仍为 `partial`**：只剩真实窗口内的人工验收，只有操作者能做，不代勾。

## 2026-09-29 模型存放路径可配置（BC-17，新能力）

操作者需求：模型默认放在项目内文件夹，用户可自定义路径、也能把路径指到已有模型所在位置（不重复下载）。
- **之前的现状**：嵌入/重排交给 HuggingFace 默认缓存（`C:\Users\<用户>\.cache\huggingface\hub`），看图 WEMM 把这个位置写死，MinerU 由它自己的环境管，全都不在项目里、也没有设置项可改。旧项目同样（只有 MinerU 子进程设过 `HF_HOME`）。
- **现在**：设置项 `models_dir`（设置页「模型」组的「模型存放路径」），留空 = 项目内 `models/`（已加 `.gitignore`）。嵌入/重排每次加载模型时现读；看图/本机 OCR 是子进程，靠环境变量 `HF_HUB_CACHE` 传入，下次（重新）启动生效。规则实现在 `core/paths.py::models_dir/models_env`，三个入口共用。
- **本机配置**：操作者指示把现有模型位置作为自定义路径——已写入真实数据目录的设置（`data-real/settings.json`，不入库）：`C:\Users\xbl26\.cache\huggingface\hub`（里面已有 bge-m3、bge-reranker-v2-m3、WeMM-Embedding-2B、MinerU2.5 等）。
- **已知局限**：MinerU 只测了环境变量传递，没用真实 MinerU 模型验证它遵循该变量；BC-17 状态 `partial`，待操作者真实使用确认。

## 2026-09-29 夜 真机复测：增量重建不跳过文件、转换阶段显卡几乎不干活（登记在 BC-12/BC-03）

操作者按上一节修复后重开 GUI 点增量重建（19:42），反馈两点：①增量不跳过文件，每个库都重新加载模型、逐个文件处理；②转换阶段显卡功耗几乎为零、CPU 尖峰，整体很慢。

- **① 的根因（已修，BC-12）**：清单里“提取器签名”记的是“这一轮能用的提取器 + 它们的设置”，并且**任何一种格式**的提取器签名一变就整库作废重提。会让它变的不只是升级：上一节为修水印扫描件把 PDF 提取器升到 0.3.0；本机 MinerU 某一轮没在 10 秒内起来会被索引子进程卸载、下一轮又好了；设置里换扫描件后端或补 Key。19:42 那一轮的结果就是 Obsidian Vault（225 篇 md + 2 个 docx，**一个 PDF 都没有**）、agents（20）、skills（16）全部 `changed`、0 个 `unchanged`，重新切块重算向量，合计约 2 分钟。**改为**：清单记插件目录里全部提取器的代码版本（`plugin.toml` version，与提取缓存的路由键同源），只有代码升级过的那种格式重新提取（该格式此前的终态也一起重试）；可用性和设置的变化只通过逐文件能力签名让 scanned/extract-failed 终态重试。旧清单里 OCR 插件记的是设置（`selected:后端:状态`），按“版本未知”处理，升级后不因记法不同重做。用真实清单只读核对：下次增量 Obsidian Vault/agents/skills 需重做 0 个文件；Y2S1 的 23 个 PDF 会按 0.3.0 重提一次（修水印必需），docx 不动。
- **① 顺带发现的第二个原因（已修，BC-03）**：`failure_will_retry` 没有排除已成功入库的文件——成功入库的 PDF 没有失败原因，被归成 extract-failed 去比能力签名；于是本机 MinerU 某轮没起来或换了后端后，含 PDF 的库在“搜索前自动同步”里一直被判过期，每次搜索都白跑一轮同步。
- **“每个库都重新加载模型”**：每个库在各自的索引子进程里跑（崩溃隔离），有块要算向量时每个库各加载一次嵌入模型（本机从磁盘缓存加载，日志里读权重不到 1 秒，连同初始化是几秒量级）。修掉上面两条后，没有变化的库根本不会加载模型。本次不动。
- **② 转换阶段慢、显卡闲的实情**：Y2S1 那一段确实是本机 MinerU 在干活——服务日志显示 19:44:37 起 20 个扫描件一个接一个解析、中间没有空档（主程序一直在等 MinerU），每份 4.8～28 秒（2～57 页）；每份里真正用显卡的是版面/公式/表格/文字识别几段短推理（各零点几到三秒），其余是 PDF 渲染成图、前后处理等 CPU 活，所以显卡功耗一阵一阵、CPU 尖峰。旧项目同样是一次送一份、服务端串行（`_API_LOCK`）。请求参数已经很精简（不导出图片、不要中间文件）。
- **② 里的真浪费（未改，待操作者决定）**：19:48 停止这一轮时，这 20 份已经解析好的结果（约 4 分钟 MinerU 工作）随未发布的这一轮一起被删了（提取缓存按“轮次”存，停止即丢弃），下次要重新解析；旧项目的提取缓存按文件内容存、转完一个就落盘，停下不丢。另一个可选提速是一次送多份小扫描件给 MinerU（它自己支持多文件合批，能把显卡批次做大），效果未实测。两项都是新设计，待操作者决定。
- **测试**：新增 `tests/test_pipeline_e2e.py::TestIncrementalOnlyRedoesWhatAnExtractorChangeTouches`（5 条：升级 PDF 提取器只重做 PDF、升级后该格式终态仍重试、MinerU 一轮缺席/恢复都不重做也不判过期、换后端补 Key 不重做、旧记法清单不重做；改前 3 条失败）；`test_existing_ocr_cache_is_reused_after_backend_changes_to_none` 的期望从“重建 1 个”改为“不变 1 个”（有意的行为变化，保护的“不重跑 OCR、文件仍可检索”不变）。全部只在假模型/单元测试里验证，待真机复测：再点一次增量重建，前三个库应在几秒内结束且不加载模型。
- **一处边界变化（已登记 BC-12）**：停用某个提取器插件不再让它提取过的文件在下一轮被重提、落成失败；这些文件保持原样可检索，文件本身改动后才会重新处理（与“撤销 Agent 授权 → 冻结旧索引”同一取向）。

## 2026-09-29 晚 真机增量重建 Y2S1 库的七个反馈：显卡吃不满、转一个索引一个、WEMM 没被调用、扫描件误判为空、设置页缺 Key（登记在 BC-01/04/11/15）

操作者在真实库（Vault/agents/skills/Y2S1，Y2S1 是 74 个课程 PDF/docx）上做增量重建，报告七个问题。逐条追查（部分用真实数据的只读副本 + 内置浏览器里的真实前端页面验证），结论与处理如下。

- **① 显卡吃不满、功耗在 0 和高值之间来回跳 / ② 转一个索引一个、两个模型同时占显存**：根因是 `Pipeline.index_library` 是“一个文件：提取→切块→向量→写库”再下一个；旧项目 `index.py:2056-2300` 是先逐个转换切块并攒块，全部转完后连续向量化，最后统一写入。**已改回旧顺序**（BC-04）：第一段逐文件提取/清洗/切块（各种终态就地落定），第二段对全部待嵌块连续向量化（64 块一片给进度心跳，片内按文本长度从长到短排以减少填充；嵌入器内部批大小 8、按显存自动收紧的旧安全上限**不动**），第三段统一写向量库（1000 块一批）与词法库并生成清单，最后原子发布。任何一段异常整轮不发布。进度口径同步对齐旧 `gui/store.py::progress_ratio`：向量化阶段按块数、其余阶段按文件数（GUI 进度条与 core 的 `percent` 一致），向量化/写入阶段不再给按文件外推的剩余时间。
- **② 里“CPU 吃满”是谁**：**不是 MinerU**。Y2S1 那两次重建里 MinerU 一次都没解析成功（本机 OCR 插件在索引进程里“启用失败：子进程 10.0s 内没有通过 health_check”，47 个扫描件被整轮延后）。CPU 满来自另一个转换器——有文字层 PDF 用的 `pymupdf4llm`：对 5 页的 CamScanner 文件实测，0.9 秒墙钟吃掉 15 秒 CPU（约 17/20 个核）。这个转换器的线程数**没有限制**（本次不动）。
- **健康检查超时的根因（BC-11）**：本机 MinerU 服务是单线程 `HTTPServer` 且 `/health` 里现场 `import torch`；旧项目 `mineru_server.py` 是 `ThreadingHTTPServer`，`/health` 不做重活。**已改**为多线程服务 + 后台刷新显存快照，解析进行中 `/health` 也秒回（有测试；改回旧实现三条转红）。
- **③ 总览页三个开关没反应**：用真实数据在真实前端页面里实测——“PDF 原件”有反应（70 个节点能隐藏/恢复）；“PDF→MD 缓存”旧项目后端本来就不产生这类节点（`guiweb/graph_data.py` 只有 md/pdf/page/pagegroup），属旧项目遗留的空开关，不改；“WEMM 页节点”当时是 0 个，原因是下一条。
- **④ WEMM 有没有真被调用**：**没有**。4 个库日志全是“WEMM子进程未运行，页级索引本轮跳过”，70 个 PDF 的页库全被记成失败。两处与旧项目不一致，**已修**（BC-11）：一是文字向量模型做完后还带着 GPU 名额、WEMM 抢不到——旧项目 `_release_for_wemm` 会在 WEMM 真要占显卡前让 bge/reranker 下车，现在 `visual_index.index_library` 新增可选 `before_serve` 回调，视觉插件仅在“确有页要渲染”时调用，核心传入 `_release_text_models_for_visual`；二是页级索引的失败/部分成功记录只要 PDF 没变就永远不重试——旧项目终态条目不走快速路径、每轮重试，现对齐。无页可渲染时既不让路也不拉起看图服务（零拉起零开销）。**副作用（需知）**：这 70 个 PDF 的页库下一次增量重建会被真正建起来（渲染每一页并编码，PDF 页数多时耗时较长）。
- **⑤ CamScanner 扫描件被判“空文件”**：不是 OCR 没开。这类扫描件每页盖着水印 “CamScanner”（恰好 10 个字符），达到“每页 ≥ 10 字符即有文字层”的门槛，被当成文字 PDF，转出来只有 5 行水印，切块清洗后为空，记成终态 `empty`，OCR 从未被调用。**旧项目规则一模一样，同样会踩**。**新规则**（旧项目没有，操作者 2026-09-29 确认，登记 BC-01）：至少两页、且每页折叠空白后文字完全相同、不超过 40 字符 → 视为水印、无文字层，整本按扫描件路由；提取器版本升到 0.3.0，旧数据下一轮重试。用真实的 7 个 `empty` 文件复核，全部转为 `scanned`（开了 OCR 就会走 OCR）。**已知局限**：只有一页的水印扫描件无法据此识别。
- **⑥ 设置页缺 MinerU API Key**：属实。旧项目设置页有 `mineru_api_key`（保密）与 `mineru_model_version`，云端提取读它们；rag-redo 只认环境变量，只用界面的人没有地方填，此前“设置页只列真实读取的键”的判断漏了这两项。**已补**（BC-15）：设置页“PDF 与云端 OCR”组新增两项，云端插件与能力签名改为“设置页优先、环境变量兜底”（补上 Key 会改变能力签名，存量 scanned 终态下一轮自动重试）。旧项目其余 MinerU 键（并发/每分钟提交上限/超时/本地服务地址/页数上限/`pdf_text_backend`）rag-redo 没有对应实现，仍不列。
- **⑦ 关 GUI 后显存没释放、进程没关**：**你那一次的具体原因仍未复现**，但追查中抓到了一条真实机制并已加固（BC-11）。实测两条正常路径都干净：正常关窗（WEMM/MinerU 两个子进程全部回收、无残留线程）；索引 worker 被强杀（Windows Job Object 让它带起的子进程一并死）。**抓到的机制**：宿主进程（GUI/MCP/索引 worker）**异常没了**（崩溃、被“结束任务”、被强杀）来不及走 `stop()` 时，Windows 上子进程不会跟着走——2026-09-29 我自己的一次测试进程被终止后，一个 WEMM 服务孤儿活了 29 分钟以上，直到我手动清掉（旧项目服务也只有“空闲 30 分钟自退出”，没有这条兜底）。**加固**：宿主拉起子进程时把自己的 pid 写进环境变量 `RAG_REDO_PARENT_PID`，WEMM 与本机 MinerU 服务每 2 秒确认宿主还在，宿主没了就自退出（MinerU 服务连同它的内服务 `mineru-api` 按进程树一起结束，最多等 10 秒让内服务优雅停）。**实测（真实 MinerU，模型已装进显存）**：宿主被硬杀（TerminateProcess，无任何收口机会）后，约 8 个进程的整棵树 **12.4 秒内全部消失**，显存 3693MiB 回到基线 2562MiB，无残留（此前的行为是它们会一直活着占显存）。有 4 条单元/端到端测试，关掉监视后端到端用例转红。**仍不能声称已解决你那一次**：不知道你当时是怎么关的 GUI；另外补了关窗日志——关窗时往 GUI 日志追加“窗口已关闭：回收了 N 个子进程，运行时收口用时 X 秒”，退出时若仍有子进程再追加 ERROR 级“仍有 N 个子进程未回收：进程名(pid)…”（BC-15）。下次遇到请把这几行日志和任务管理器里残留的进程名给我。
- **MinerU 单文件实测（本机，2026-09-29；5 页扫描件 `CamScanner 15-01-2026 17.03.pdf`，pipeline 后端）**：服务拉起 1.3 秒；**冷态**首个请求 57.9 秒（大头是内服务与模型加载；显存 2.3GB→峰值 4.9GB，整机 CPU 峰值 90%、MinerU 进程树峰值约 16.5 个核，此阶段显卡利用率均值仅 2.6%、峰值 32%）；**热态**同一文件 3.3 秒，MinerU 进程树平均约 6.3 个核（峰值 9.7/20），显卡利用率均值 14%、峰值 48%，功耗 27~46W；识别出 5205 个字符（这个文件此前被判“空”）。空闲后模型仍驻留约 1.3GB 显存（≈3.6GB），停服务后显存回到基线（2.5GB，其中 ≈2.3GB 是桌面软件占的），无 MinerU 进程残留。**结论**：MinerU 的 pipeline 后端在这类扫描件上是**CPU 重、显卡利用率不高**（页面渲染/预处理/后处理在 CPU，神经网络在显卡）；“MinerU 调用显卡所以不吃 CPU”不成立。样本只有 1 个 5 页文件，数字只代表这台机器这一个样本。
- **没改的**：显卡单批 8 段的旧安全上限；WEMM 服务仍是单线程 `HTTPServer`（旧项目是多线程，暂无真机症状，另记）；`pymupdf4llm` 的线程数；旧项目 13 组设置里无对应实现的键；MCP 单库 `reindex_knowledge` 与命令行 `index` 仍不排队。
- **待真机确认**：①重新点增量重建，看四个库是否依次跑、日志里是否还有“WEMM子进程未运行”、Y2S1 的 7 个“空文件”是否变成被 OCR 识别；②总览页 WEMM 页节点是否出现；③设置页填 MinerU Key 后云端识别是否生效；④GPU 功耗曲线是否比之前平滑。以上都还只在假模型/单元测试里验证过，BC-15 保持 `partial`。

## 2026-09-29 增量重建：多个库同时开跑、中途崩溃且无处可查（登记在 BC-15）

操作者真机增量重建时发现：显存反复冒尖后掉回底部、库里的文档没变成向量、诊断页也没有任何失败提示。追查结果：
- **现象根因一（与旧项目行为不一致）**：GUI 点一次重建，对每个库各起一个后台索引进程，**4 个库同时开跑**（进度文件里 4 个进程开始时间只差 1.2 秒），4 份 BGE-M3 同时占显卡/CPU。旧项目一次点击只起一个进程，在 `index.py` 的 `__main__` 里按库逐个循环（某库失败记日志继续下一库，停止 = 整轮取消）。这是此前没人批准过的偏离，按项目规则算缺陷。
- **现象根因二**：4 个索引进程全部死于 `UnicodeEncodeError: 'charmap' codec can't encode characters`（进度文件里 `stage=failed`，Vault 库已经算完全部 2184 个块、崩在最后的视觉阶段），进程只留了这一句摘要，没有堆栈，日志里也没有任何一行。**具体是哪一行输出触发的没有定位到**：单个进程在 cp1252 下跑同一批文件是正常的；已确认 stdout 在 cp1252 下写中文会抛、stderr 不会。
- **诊断页为什么空**：旧项目诊断页的失败明细表只列**文件级**失败（没转成/没索引上的文件）；整轮索引崩溃的原因在日志面板里。新版此前崩溃后日志里什么都没有。
- **本次修复**：①`Pipeline.start_index_libraries` → `IndexWorkerManager.start_batch`：多库**依次串行**，第一个立刻起，其余显示“排队中”，前一个退出才起下一个；失败继续下一库；停止在跑的 = 整批取消，停止排队中的 = 只摘掉它；GUI 只把整批交给核心；进度条只显示当前在跑的库，交接空档不闪成“完成”。②worker 以 UTF-8 模式启动（`PYTHONUTF8=1`，只在起进程那一刻设置并还原）+ 标准流统一 UTF-8，从根上消除这一类编码崩溃。③worker 崩溃时日志里留下 `[库名] 索引失败：原因` 一行和完整堆栈（GUI 日志面板可见）。
- **没改的**：MCP 的单库 `reindex_knowledge` 与命令行 `index --library` 各自只处理一个库、不排队，同库互斥仍靠每库文件锁；MCP 两次调用不同库仍可并行（旧项目那里有全局“已有任务在运行”判断，是否也要对齐待操作者定）；命令行在输出被重定向时也会因 cp1252 写中文而崩，同类问题这次没动。
- **待真机确认**：重新点一次增量重建，看四个库是否依次跑完、日志面板是否有内容；如果仍有库失败，现在日志里会有完整堆栈可查。

## 2026-09-29 桌面真机使用反馈：黑屏闪窗、启动顺序

> 操作者把新做的桌面快捷方式指到 `data-real`（真实 4 个库）后真机反馈三个问题：①双击后先出一个黑色弹窗、等好几秒才出界面；②出界面后每隔几秒还会再弹出关闭一次、界面跟着刷新，一直重复；③担心多次点击会开出多个 GUI。逐项根因追查+修复：

- **反复弹窗（根因，已修）**：`core/gpu_arbiter.py::probe_card()` 是 GUI 全局快照每秒调用一次（内部 5s 缓存）的整卡探测，和 `vram_free_gb()` 的 nvidia-smi 兜底一样，`subprocess.run(["nvidia-smi", ...])` 没带 Windows 专属的 `creationflags=CREATE_NO_WINDOW`——宿主是 `pythonw.exe`，没有控制台，Windows 会给子进程现开一个、用完即关，这就是"每隔几秒黑屏一闪"。`core/subprocess_service.py`（WEMM/MinerU-local 子进程的 `Popen`、`taskkill`、`env_bootstrap` 的 `pip install`）和两个插件各自子进程里的同款 nvidia-smi 调用同理修复，共 7 处调用点补齐（`core/index_progress.py` 里已有的一处一直是对的，这次统一按它的写法补）。新增 `tests/test_gpu_arbiter.py::TestProbeCard`（此前完全没有测试覆盖）和 `tests/test_subprocess_service.py::TestNoConsoleWindowOnWindows` 钉住不回归。
- **启动慢+首次黑屏（根因，已修两层）**：①桌面快捷方式原本走 `cmd.exe /c set ... && start ...` 来设置 `RAG_REDO_DATA_ROOT`，cmd.exe 本身就是那个"先出的黑色弹窗"——改成 `wscript.exe` 跑一个纯本机的 `.vbs`（`WshShell.Environment` 设环境变量、`WshShell.Run` 隐藏方式拉起 `pythonw.exe`），不进版本库，是操作者桌面上的个人文件。②`gui_main.py::build_runtime()` 此前按声明顺序同步 `load()`+`enable()` 全部 20 个插件，其中 `official-visual-wemm`（默认开）和 `official-ocr-mineru-local`（`data-real` 配了 `mineru-local`）是 `subprocess_service` 插件，`on_enable` 会真的 `Popen` 子进程并阻塞轮询 `health_check`（最多 10s/个）——窗口在 `_serve()` 建出来之前必须先扛完这最多约 20s。旧项目 `guiweb/app.py::main` 本来就是"窗口先建、`Bridge()` 不碰任何子进程"，这次把 rag-redo 对齐回这个已验证过的旧行为：新增 `DEFERRED_PLUGIN_IDS`，这两个插件只在 `build_runtime()` 里 `load()`、真正 `enable()` 挪到窗口打开、推送线程起来之后的一个后台线程（`_enable_deferred_plugins`），并且 `_serve()` 的 `finally` 里必须先 `join()` 这个线程才能让 `main()` 去调 `runtime.close()`——不然 `close()` 会跳过还没到 enabled/disabled 状态的插件，子进程就成了没人收的游离进程。`plugins/official-gui-shell/tests/test_boot.py` 新增两条测试钉住"窗口不被这两个插件的 enable 阻塞"和"close() 一定等后台线程跑完"。**真机效果**：走真实桌面 shortcut 对 `data-real` 实测，双击到出窗口约 6 秒、单例守卫挡住了几乎同时的第二次启动、关闭后 0 进程残留；这台机器上 WEMM/MinerU-local 的 health_check 本来就不慢（读独立 venv 已建好），所以这次没有测出总时长的大幅下降，改动的价值是消除了"健康检查一旦变慢（最坏 ~20s）就会连累窗口打不开"这条尾部风险，并让 rag-redo 的启动顺序重新对齐旧项目。
- **多次点击开多个 GUI（确认无需修——已有防护）**：`core/singleton.py::ProcessSingletonGuard` 已经是文件字节锁、在 `main()` 最开头获取，与 `build_runtime()` 的速度无关；真机测试里故意用两次几乎同时的双击验证过，第二次会在 `build_runtime()` 之前就被拒绝退出，全程只出现一个窗口（`tests/test_singleton.py` 的 `test_real_second_process_cannot_acquire_while_first_holds` 等用例早已覆盖这条）。

以上三处均为修复代码缺陷/对齐旧项目已验证过的启动顺序，不是新的产品行为设计，未新增行为契约条目。

## 2026-09-25 通宵行为对齐审计——报告核实与九项修复

> 操作者提供了一份过时的调研报告并要求先核实时效性，再"继续未完成工作 + 检查已实现代码是否有简化"。方法：六个只读调研 agent 把报告的每条声明对照当前代码与旧项目生产代码逐条验证，然后按 AGENTS.md §8（先复现测试后实现、逐项全量回归、分项提交）执行。全程 45/45 套测试全绿（新增约 30 条用例）。工作区在报告写作后已被推送（报告里"79 个文件未提交/领先 24 提交"已过时），14 条 BC 契约当时已全部标 pass——但核实证明其中三条是虚标（见下）。

**报告判断有误、实为与旧项目一致（不动）**：
- **Graph 四项全部与旧项目同构**：未索引普通文件不显示（graph_data.py:89-105 同）、语义边基于文件名+路径而非正文（semantic.py:46 逐字相同）、O(N²)（graph_data.py:220-233 同阶）、MCP 无 Graph 工具（旧 server.py grep "graph" 零命中）。报告把"继承设计"当成了缺口。
- **HyDE 自带 HTTP 客户端不是简化而是对齐**：旧项目没有 provider 抽象，`retriever.py:503-541` 的 hyde_generate 就是自带 urllib 客户端 + 独立 `hyde_llm_url/model/api_key` 配置端点；rag-redo 的 official-llm-openai-compatible 接口（`complete(system, user)`）也装不下 per-调用方端点。不做"统一到 Provider 层"。
- **GUI 搜索无 freshness 与旧项目一致**：旧 GUI bridge.py:672 直调 hybrid_search 无 ensure_fresh，GUI/MCP 不对称是两代共有的行为。
- **advisor exclude 建议错位**：事实成立但旧项目 advice.py:148-150 同错——经操作者拍板**修复而非复刻**（见下），已作为经确认的偏离登记 BC-08。

**核实为真实偏离、已修复（各一条提交）**：
1. **BC-02/03/04 虚标 pass 的三条数据安全缺口**（增量索引把"本轮看不到"一律当"确认删除"）：①库路径消失/目录扫空 → 新增 `library_freshness()` 报 missing/emptied，MCP 自动同步跳过并写入响应 notes、同步失败降级旧索引检索（对齐 index.py:1506-1529 + server.py:264-273/314-317；从未索引+空目录=收敛态不重建）；②deferred 不再落 `chunk_ids:[]` 记录——旧条目与旧块原样保留继续服务，词法旧块删除迁移到各确定丢弃点（对齐 index.py:2142-2148"不落终态、不动 meta"）；③撤销 Agent 授权改为冻结语义——保留条目与块、视觉 PDF 保留在有效页集合不重渲染（对齐 index.py:2058-2066 + wemm_indexer.py:190"其余冻结"）。
2. **GUI 进程单例守卫**：gui_main.py 接 ProcessSingletonGuard（旧项目 gui/app.py:46-69 与 guiweb/app.py:46-87 两个 GUI 入口都有锁，此前只给 MCP 接了）；真实子进程接线测试 2 条。
3. **原子写统一**：新增 core/atomic.py 权威实现（tmp+os.replace+Windows PermissionError 退避重试，临时名含 pid+线程id 防同目标并发互踩）——修 5 处裸 write 直写（settings.json/plugins_state.json/提取缓存/BM25索引/libraries.json——后者对齐旧 library.py:421-427 原子写；另 GUI 导出归档），并把 8 处各自复制的 tmp+replace 收敛到同一助手。
4. **GUI 设置 secret 打码 + 键元信息**：get_settings 改 `{values, meta}` 打包返回（对齐 bridge.py:801-820），`*_api_key/*_token` 规则兜底 + SETTING_FIELD_META 中文说明（消除 api.py"设置键无说明"已知简化），前端 secret 键打码显示/编辑框切密码态（对齐 app.js:1640-1641）。
5. **检索侧 GPU 收口**：移除 embed/rerank 加载路径的 wait_for_vram(900s) 盲等（返回值此前未检查；旧项目检索侧从不阻塞等待——`_vram_maybe_evict_wemm` 是主动驱逐检查，wait 语义只属于 WEMM/MinerU 服务端）；补齐 on_disable 归还 gpu:0 名额（rerank.py docstring 一直声称但从未实现）。
6. **CUDA 冷却期状态机按旧项目原行为补齐**（消除 embed.py"单次降级永不切回"的已知简化）：新增 core/gpu_arbiter.CudaCooldownGate——冷却窗口（设置项 `cuda_cooldown_seconds` 默认300，对齐旧 config.py）内直接 CPU、到期毫秒级探测（64MB 分配）通过才切回、失败原因/device 健康落盘 device_state.json；embed/rerank 接入加载失败冷却+CPU重试一次、慢批检测（>30s 连续两批降级，对齐 encode_safe 的 WDDM 共享显存溢出检测）。
7. **library-summary 指纹精确算法**：新增 `library_content_fingerprint()`——排序聚合全部已索引文件 path:content_hash 后 sha256 截16位（逐字对齐 library_summary.py:40-50；旧数据源 meta.hash 的等价物是 manifest 的 content_hash），消除"采样片段哈希代理"的已知简化；并补齐指纹传递链（MCP propose 对齐 server.py:508、GUI AI 刷新对齐 bridge.py:414 三元组、用户手写不带指纹对齐 bridge.py:356-365）——此前 is_stale 过时提示因无指纹而完全休眠。
8. **DataStore issue_path 权限收口**：storage_handle 授权登记为唯一依据，未声明 data_write 的插件不再有申领持久化路径的通道（此前 handle 门禁可被直接绕过）；多插件共享 index_generations legacy 目录的刻意设计保持允许并写入文档。
9. **文档/打包**：README.en.md 过时的 Inno Setup/打包不可用表述改为便携 ZIP 现状；core/subprocess_service.py docstring"便携 Python 还没做"滞后于代码的段落修正；便携包 README 补 MinerU 外部 uv tool 环境说明；build_windows.py 构建时生成 requirements-lock.txt（133 项锁定版本，"最小依赖清单"第一步）。

**核实后决定顺延/登记的事项**：
- **业务 CLI**（旧项目 index.py:2441 索引/library.py:700 库管理/import/export/dedup；rag-redo 只有插件管理 CLI）——操作者拍板低优先顺延，未动。
- **runtime.py:281-283 的 sys.path 简化**（每插件导入隔离，防同名顶层模块冲突）——真实边界收紧涉及导入机制重构，登记待办未动。
- **DataStore 深度封装**（handle 返回裸 Path、契约值 write/read 生产零调用、gui_main 直传 lib_mgr 实例给 GUI Api）——in_process 受信任插件定位下门禁已到合理边界；深度封装与 GUI 走契约通道是架构改进项，登记待办。
- **干净机验收与便携包体积优化**——需要无开发工具的真实机器，无法无人值守执行，仍待操作者。
- 全量高频连跑下 core/test_index_progress 的两条子进程计时用例偶发抖动（单跑稳定、昨夜起点提交同样偶发，属项目已知的高频连跑子进程残留问题家族，见 Phase 4 记录），已通过并发唯一临时名修复消除一个潜在干扰源。

## 2026-09-25 终审：四份穷举对照 + 决策记录挖掘——十七项修复

> 操作者指示"全部做，看旧项目决策记录，确保真的对齐"。方法：四份穷举审计（旧 config.py 全部 63 键逐键对照、旧 server.py 全部 16 工具签名 diff、旧决策记录 TASK_LOG/TODO/docs 全量挖掘、旧 GUI bridge.py 39 方法+前端交互逐项对照），按影响排序修复，45/45 套测试全程全绿。

**修复清单（按提交顺序）**：
1. **检索核心四项静默偏离**：RRF k=60→2（旧刻意选择 TASK_LOG:920）；候选池 top_k×3→max(top_k×8, 200)（retriever.py:640，top_k=5 时 15→200）；重排池 rerank_candidates=50+池外余量接续（retriever.py:626）；新增 rerank_enabled 开关+各库归一化降级合并+RRF 一致度置信度（retriever.py:627/789）；交付窗口 top_k×4（FOLD_WINDOW_FACTOR）。
2. **wikilink 清洗进索引文本**（问题15/审计F9）：core/text_cleaning.py 逐字移植——别名/目标词保留、路径锚点剥离、![[嵌入]]删除；链接先从原文抽取（关系语义不变）；text_pipeline 签名机制对齐旧 META_VERSION。
3. **frontmatter 锚点进嵌入/BM25**（问题18/审计F20）：title/tags/文件名+标题链逐段去重拼进块文本，ctx 存 metadata 供交付剥离；重排器输入=存储文本。
4. **return_chunk_limit 2000+行边界截断+[块 k/N]**（问题10）：_truncate_at_line 逐字移植（±300 行边界收边），SearchResult 新增 chunk_index/total_chunks/truncated。
5. **出厂排除默认集**（问题1）：.obsidian/.smart-env/.trash/.git/TEMP/templates + 目录.md/AGENTS.md/LOG.md/README.md + session-/会话/.tmp（新建库默认，已持久化配置不受影响）。
6. **切块器按旧语义重写**（问题8/9/18、审计F8）：H1-H3、围栏内#不算标题、表格宁大勿断+上下文绑定、列表项边界切、缩写保护、无 overlap、600 限制；CHUNKER_VERSION 0.3.0。
7. **BM25 双通道**（问题21/审计F1）：jieba 滤停用词+中文 2-gram 兜底+英文 token（retriever.py::tokenize 逐字对齐）；INDEXER_VERSION 0.2.0。
8. **MinerU 云端 404/非JSON→gone 清簿记**（问题36）：堵"永久续接不存在的任务"。
9. **杂项阈值**：find_duplicates 0.8、stall_timeout 25、GUI top_k=5。
10. **reindex_knowledge 补 allow_new_formats 授权流**（server.py:620-656）：pending_agent_formats 报告+确认后持久化 agent_formats——MCP 层授权流此前断裂。
11. **read_document 补 abs_path+标题回退匹配**（旧抬头含绝对路径+标题匹配约定）。
12. **业务 CLI 移植**（index/library/export/import/dedup 五个命令行入口，对齐 index.py:2441/library.py:700/export.py:258/import.py:212/dedup.py:235）——全部是 Pipeline 薄封装。
13. **GUI 批量后台刷新简介+轮询**（bridge.py:367-420 协议），移除"同步阻塞"已知简化。
14. **check_notes.py 移植**（D类，操作者确认）：规则/豁免集逐条一致，三个集成点适配。
15. **GUI 后端补齐**：remove_library/get_library_config/set_library_config/dedup_run/wemm_status/open_path（bridge.py 可行子集）。
16. **B3 核实为已对齐**：思考型模型 180s/2000 预算与 reasoning_content 处理本就对齐；旧 call_llm 本无独立冷却计时器（ROADMAP 旧备注不准确，CUDA 冷却已按旧状态机移植）。
17. **B5 登记为有意设计**：GPU_HOLDER_ID 共享 holder 支持检索侧两模型共存+幂等续期（旧项目无插件禁用概念，引用计数属核心增强项）。

**核实为与旧项目一致、不动的**：GUI 搜索无 freshness（旧 GUI 同）；Advisor 建议文案偏离（操作者已批准，见 BC-08）；selection_new_files 三态 follow 未移植（rag-redo 无旧式全局格式开关，格式门禁由 extractors/agent_formats 承担——登记为架构差异）。

**仍登记的余项（按审计清单精确到条目）**：
- sidecar 噪声清洗双轨（问题48 v10/v11：死图链/页码/样板行剥离）——扫描件语料质量，未移植。
- MinerU 云端并行（问题35 cloud_jobs 线程池 + mineru_concurrency 三档）、批量提交（50文件/批）、token 失效响应体错误码（A0202/A0211）——吞吐行为未对齐。
- 模型加载离线优先（问题56 local_files_only）/fp16——**已完成（2026-09-25 真库实测驱动）**：core/model_loading.py 承载旧 index.py::_load_pretrained 语义（local_files_only 优先、缺/坏回退联网、双失败抛清理指引）；embedder 按旧 _load_model fp16 优先+混拔回退 fp32；reranker 按旧 _get_reranker max_length=512+fp16+batch_size=16。**按显存自动批次（问题59-B4 的 encode_safe 收紧段）仍未移植**——fp16 后实测不再触发共享显存溢出，优先级降低但语义缺口仍在。
- 勾选"按深度裁决"（问题47附记2）+ 同位置冲突拒绝——**已完成（2026-09-25 真库实测驱动，commit 5c844e9）**：selection.py 逐字移植旧 selection_hit/excluded_dir_depth/decide_included（最近显式赢、更深目录排除压较浅纳入、同位置打架排除站住、目录条目单部件子串语义）+ 旧 collect_md_files 中性分支（follow/include/exclude 三态、exclude_patterns 文件名前缀 startswith、exclude_files 精确名）；写路径提案侧事前拦截+set_selection 兜底拒绝；migrate 工具三态直传；测试镜像旧 test_selection.py 拍板用例。
- 无法无人值守执行、需要操作者：干净机验收、便携包体积优化、WEMM 独立环境从零引导（需联网）、GUI 真人交互冒烟、push。~~检索质量与旧项目真实对比~~——**已完成（2026-09-25）**，见下节。

## 2026-09-25 真库实测——与旧项目同库全链路验证

> 操作者指示"用跟旧项目一样的库实测全套系统：全量/增量/部分选库索引、库配置、路径管理、文档类型管理、全检索，检查语义与执行问题和同步效果"。方法：注册旧项目 data/libraries.json 的全部 4 个库（Obsidian Vault 227 文件/agents 20/skills 16/LECTURE NOTE 18，逐库同步旧生效配置含选区与 agent_formats），每个环节与旧项目对照。全程断网（复用本地 HF 缓存模型），实测驱动修复 3 个真问题，47/47 套测试全绿。实测脚本沉淀在 tools/（register_real_libraries/compare_enumeration/search_probe_new/search_probe_old/compare_search/mutation_test/config_test），实测数据根 `data-real/`（gitignored，保持仓库 `data/` 不存在以满足测试隔离断言）。

**环境问题（非代码缺陷）**：`.venv` 里装的是 CPU 版 torch（2.14.0+cpu）——CUDA 冷却门行为完全正确（探测失败→如实降级 CPU+device_state.json 诊断），但嵌入全程在 CPU 上跑（约 130 块/分钟，GPU 正常时应为数十倍）。不联网修复：两个项目同为 Python 3.14.6，从旧项目 venv 复制自包含的 torch 2.11.0+cu128（4.2GB）+torchgen/functorch/dist-info，并卸载误装的 torchvision 0.29.0+cpu（对着 torch 2.14 编译，transformers 一 import 就炸"Could not import module 'PreTrainedModel'"）。**部署纪律：rag-redo 的 Windows venv 必须装 cu128 版 torch，pip 默认源装的是 CPU 版。**

**实测驱动修复的 3 个真问题**：
1. **fp16/离线优先缺失 → 显存溢出 + 逐查询重载**：fp32 双模型常驻把 8GB 卡顶进 WDDM 共享显存溢出，单批重排分钟级→慢批检测降级 CPU，8 个查询 8~152 秒且重排器反复重载。按旧项目 2026-09-12 决策修复后（fp16+max_length=512+batch_size=16+离线优先），fp16 双模型共 2.6GB 显存、200 对重排 2.7 秒、单查询 1.1~1.4 秒（与旧项目同速）。
2. **勾选裁决语义偏离**（问题47 完整落地，commit 5c844e9）：真实库里有 `session-`/`会话`/`MOC-` 前缀文件，旧语义是文件名前缀 startswith，此前的 fnmatch 实现会错误收录；更深的目录排除应压过较浅的显式纳入，此前只处理了同位置打架。
3. **tbd 占位检查越界到二进制格式**：旧 index.py:1571 只对 md/txt 做 is_tbd_heavy；rag-redo 把含大量 [TBD] 的课程报告 docx（旧项目建了 182 块索引）误判 tbd 跳过。修复后 Vault 逐文件状态与旧项目完全同态（.md 判 tbd、~$ Word 锁文件 extract-failed、BACKUP.docx 正常入库）。

**对齐验证结果**：
- **文件枚举**：4 库 281 文件收录判定与旧项目 collect_md_files **零差异**（含选区/排除/前缀/子串/深度裁决全部语义）。
- **全量索引**：agents 20/20、skills 16/16 成功；Vault 225 成功/2 失败与旧项目逐文件同态；LECTURE NOTE 4 md 成功 + 14 扫描版 PDF 折叠成结构化 `scanned` 终态（断网无 OCR 可用，无半份结果、无宿主异常——BC-01/04 语义实测通过）。
- **检索对比**：8 个真实主题查询（火箭/SAF/奖学金/静力学/量纲/并行调度/GraphRAG/本地大模型），正文模式 top1 文件 **7/8 与旧项目一致**，md 语料（切块边界相同）多处逐位一致；唯一分歧的"静力学"题旧项目命中云端 OCR 过的扫描版 PDF（MinerU 云缓存），断网环境无法复现——属已登记环境缺口非代码缺陷。置信度真分尺度档位吻合（确定命中 0.84~0.98、模糊查询 0.03~0.3、噪音 <0.05）。
- **增量索引**（skills 副本库实测）：no-op 复跑 0 重算；新增/修改/删除各精确命中 1 文件（BC-03/增量语义）。
- **配置面**（副本库实测）：Agent 文档授权流（pdf 授权前 agent 视角拒绝+pending_agent_formats 报告、授权后可见，BC-02）；勾选提案→写门禁确认→生效→索引尊重排除（删除对应 4 文件的块）→恢复回补；同位置打架提案事前拒绝；export→import→dedup CLI 全链路通。
- **速度对齐**：GPU 修复后 4 库全量索引约 3.5 分钟（CPU 时单 Vault 需 30+ 分钟），与旧项目同量级。

**实测后仍未对齐/需操作者的**：扫描件内容质量依赖 MinerU 云端（断网不可复现，`pdf_text_backend` 文字层送云端与问题35并行度仍在余项清单）；encode_safe 按显存自动收紧批次（问题59-B4 剩余段）未移植；GUI 前端交互全面对齐仍是最大余项；便携包/干净机验收需操作者。

## 2026-09-23 全面功能审计——已发现、尚未实现的缺口

> 操作者要求"检查decision log/相关文档核对功能是否完整复刻"后的系统性核对结果。方法：`grep "@server.tool()"` 拿到 obsidian-rag 全部16个MCP工具的权威清单，逐个核对 rag-redo 当前实现；同时通读 `config.py` 全部 CFG 默认值、`singleton.py`、`guiweb/graph_data.py`+`semantic.py`。**这些都是这次才发现的真实缺口，不是已知计划里遗漏的执行细节**——此前的 FEATURE_TRIAGE.md 表格粒度停在"插件级"，从没有逐个核对过 MCP 工具级别和 CFG 配置项级别，这次审计把粒度下钻到了那两层。

**A类——底层能力已存在，只差 MCP 封装（低工作量）**：
- ✅已完成 `find_duplicates`：`official-dedup` 插件的 `DedupPlugin.find_duplicate_groups(library_id)` 已实现且有测试，但从未注册成 MCP 工具——AI 现在完全无法触发近似去重诊断。
- ✅已完成 `wemm_status`：`official-visual-wemm` 子进程的 `/health` 探测已存在（`_handle.is_alive`/health_check），但没有对外的 MCP 只读诊断工具，用户/AI 没法直接问"WEMM 到底能不能用"。

**B类——需要新写逻辑，但能直接复用已有基础设施（中等工作量）**：
- ✅已完成 `get_selection`/`propose_selection_changes`/`apply_selection_changes`：obsidian-rag 里 AI 可以经 MCP **提议**库内文件级勾选变更（纳入/排除某文件/目录），用户确认后生效——rag-redo 的 `official-library-manager` 已经有底层写方法 `LibraryConfigStore.set_selection()`，也已经有通用的 `core/write_gate.py` 两段式确认服务（`official-library-summary` 已经证明了这个模式能跑），但从没有人把两者接起来给 AI 用。AI 现在只能"读"库的勾选状态（`resolve_included_files`），不能"提议改"。
- ✅已完成 置信度分档标注（"低置信度，仅供参考"）：`search_knowledge` 的工具描述里写了 <0.30/0.30~0.75/≥0.75 三档的**说明文字**，但没有像 obsidian-rag 那样真的在低置信度结果上**附加标注**——AI 拿到的是裸数字，得自己套文档里的阈值判断，obsidian-rag 是直接标好的。
- ✅已完成 同篇结果封顶/截断（`max_chunks_per_file`/`truncate_mark`/`return_chunk_limit`）：obsidian-rag 的正文模式会给"同一篇笔记最多出现几块"设上限、超长块会截断并标注"完整内容见源文件"——rag-redo 现在对同一篇文章命中再多块也照单全收，也没有单块长度上限。
- ✅ `small_to_big` 父节回填（2026-09-24）：`official-chunker` 为同标题正文块输出稳定 `section_id` 与完整父节文本，Pipeline manifest 每节只保存一份；检索仍用小块 BM25/向量/重排打分，正文模式仅对多块且超过 300 字的父节整体回填，同节只交付最高分块，同文件封顶或折叠后继续用后续候选补足 `top_k`。`include_body=False` 的 list 模式不展开、不折叠、不按文件封顶；MCP 返回 `backfilled`，GUI 显示“已回填父节全文”。
- ✅已完成 进程单例守卫（`singleton.py`）：obsidian-rag 用非阻塞文件锁保证同一时刻只有一个 MCP server 实例在跑——原因是真实观测到过 opencode 等 MCP 客户端在启动时误拉起两个实例，导致模型/索引写锁双份常驻。rag-redo 的 `mcp_stdio.py` 完全没有这层防护，理论上同样会中招（架构红线6"不产生游离进程"这条本来管的是"自己启动的子进程"，这个是"自己这个主进程被重复拉起"，是同一个问题家族的另一种表现形式，此前没人往这个角度想过）。

**C类——全新功能，需要真正的新设计（工作量最大，多数会碰 `core/`，按红线4需要先确认再动）**：
- ✅已完成 `note_relations`（双链关系查询）+ **通用配置存储的缺失是很多小缺口的共同根因**：rag-redo 目前**完全没有** Obsidian `[[wikilink]]` 解析能力——不只是 MCP 工具缺，是从提取阶段开始就没有"这篇笔记链接到谁"这件事的任何记录。这是一块新的、独立的能力（解析+存储+查询三层都要新建），graph 视图（下一条）也依赖它。
- ✅已完成 `index_status`（索引进度查询）+ **后台索引**：obsidian-rag 的重建索引是后台线程执行、带心跳+卡死检测，MCP 立即返回、AI 用 `index_status` 轮询进度。rag-redo 的 `index_library()` 是**同步阻塞调用**——大库重建索引时 MCP 这次调用会一直卡着直到全部做完，没有进度可看，AI 客户端也可能等超时。这个不是"锦上添花"的功能，是真实的可用性风险（`docs/LESSONS.md` 这次审计前刚补的"长时间任务需要心跳/进度通道"那条教训，一直没有真正被应用到索引这个最需要它的地方）。
- ✅已完成 `read_document`（读某文档的完整提取正文）：rag-redo 的 `ExtractedDocument.text` 只在 `index_library()` 内部一闪而过（提取完立刻切块，切块完就丢），从不落盘/缓存，所以现在**没有任何办法**在索引之后再问"这篇文章完整提取出来是什么样"——.md/.txt 还能退化成直接重读源文件，但 pdf/docx 需要重新跑一遍提取（本机OCR的话代价很高）才能回答，需要先决定"要不要做一层提取结果缓存"这个设计问题。
- ✅ 自适应建议系统（2026-09-24）：新增 `official-result-advisor` 插件，完整迁移旧 `advice.py` 的确定性规则（低置信、单条强命中、同名不同路径、非笔记库、多条高置信、单文件集中、PDF/Word、父节回填、list 模式、命中少、关键词式查询），最多返回 2 条、零 I/O/模型；新增 `SearchAdviceInput`/`SearchResponse`，MCP/GUI 通过顶层 `advice` 通道消费，和 `results` 分离。
- ✅ Graph 读模型与 GUI 已完成（2026-09-24，`BC-09=pass`）：类型化 `GraphNode`/`GraphEdge`/`GraphResponse`，Pipeline 从同一 active generation 组合 manifest、双链、WEMM 页状态和权威文件枚举；PDF 直接连接 `page/pagegroup`，24 页折叠阈值、hub、路径主题、终态、可选语义边和 generation/模型签名缓存均有测试；GUI 已接入加载/空/错状态、图层、Inspector、缩放平移和语义错误提示；导入归档同步恢复关系、失败终态和 WEMM 状态。
- ✅已完成 **通用插件配置存储**：这是本轮审计里最值得单独点名的一条——`core/context.py::PluginContext` 目前只有 `data_dir`，没有任何"用户可调、可持久化的设置"入口。obsidian-rag 的 `config.py` 有约60个 CFG 项（RRF双路权重、`rerank_candidates`池大小、`default_libraries`、`cuda_cooldown_seconds`、`confidence_warn_threshold`、`mineru_python` 覆盖路径等），这次审计里至少6处独立缺口（RRF权重写死、`default_libraries`没法配、`mineru_python`只能靠环境变量硬覆盖没有GUI入口……）根子都在"没有这层"。值得作为一个独立的核心服务先设计清楚（大概率是新的核心组件，不是插件——两个已有核心服务`resource_arbiter`/`write_gate`都不懂具体配置项的含义，这个也该一样是"通用键值存储+插件各自声明自己关心哪些键"），再回头把上面几条一次性接进去，而不是每个缺口各自发明一套"环境变量兜底"。

**D类——大概率不在"核心RAG能力"范围内，如实列出但不建议优先**：
- `tools/check_notes.py`（笔记命名规范扫描）：这是原作者个人笔记组织习惯（MOC-前缀/frontmatter title等硬规则）绑定的一次性 CLI 工具，不是通用 RAG 能力，是否移植取决于操作者自己的笔记习惯是否也遵循同一套规范，不是"功能对齐"意义上的缺口。

以上均为如实记录、未开始实现——本轮只做了审计和记录，没有擅自开始动工（C类多数会改 `core/`，按架构红线4先汇报等操作者定夺优先级）。

**操作者定夺：先做通用插件配置存储（2026-09-23 已完成）**——C类里点名的"最高杠杆"一项。新增 `core/settings.py::SettingsStore`（核心服务，不是插件）：具名键值对持久化到 `data_dir/settings.json`，`get(key, default)` 按调用方传入的 `default` 类型校验磁盘值（类型不符回退默认值+警告，不静默接受错误类型，对齐 obsidian-rag/config.py::`_coerce` 的教训）；不预先声明全局 DEFAULTS——每个调用方在自己的 `get()` 调用点传自己的默认值，这本身就是"这个设置项属于谁"的权威声明，符合插件互相独立的架构原则，不是照抄 obsidian-rag 单一 `CFG` 字典的做法。`core/context.py::PluginContext` 新增 `settings` 字段，`core/runtime.py` 构造并注入。真实接入三处此前审计发现的独立缺口证明不是只搭了空壳基建：①`official-library-manager::resolve_libraries` 的 `default_libraries`（对齐 obsidian-rag 同名配置项，`libraries` 参数留空时先查这个、配置全部失效才回退全部库）；②`core/pipeline.py::search` 的 RRF 两路权重 `fusion_dense_weight`/`fusion_bm25_weight`（此前写死1.0/1.0，用记录调用参数的假 `fuse()` 替身验证真的传下去了，不是纸面改了签名）；③`official-ocr-mineru-local` 的 `mineru_python` 覆盖路径（新增设置项优先级层，在已有的 `RAG_REDO_MINERU_PYTHON` 环境变量和自动探测之间）。`official-gui-shell::Api` 新增 `get_settings()`/`set_setting()`/`unset_setting()` 三个通用方法，GUI 设置面板现可直接编辑、删除并查看显式设置；多库勾选、摘要、失败明细、正文/双链和 Graph 打开源文件交互也已接入同一 API。真机用真实BGE-M3+reranker验证过：`default_libraries` 真的把空 `libraries` 参数收窄到指定库，权重设置真的改变了融合调用参数且不影响重排器的最终精排质量。新增46个用例（`core/test_settings.py` 12个 + 各接入点的直测+e2e验证），全量回归已扩展到44/44套、670用例。

**操作者 `/goal` 锁定"完成所有！"后续完成情况（2026-09-24）**——按上面 A/B/C 类清单逐项推进，全程保持 `tests/run.py` 全绿（从34/34一路推进到44/44套、670用例）：
- **A类两项全部完成**：`find_duplicates`（现算临时 `DedupIndex`，不碰 `add_document`/`find_duplicate_groups` 那条常驻索引路径）、`wemm_status`（遍历 `visual_index` 提供者的 `status()`）都已注册成 MCP 工具，`read_document` 顺带一起做了（见下）。
- **B类五项全部完成**：
  - `get_selection`/`propose_selection_changes`/`apply_selection_changes`——新增 `official-library-manager::norm_selection_path`（拒绝绝对路径/盘符/UNC/`~`/`..`逃逸，逐字对齐 obsidian-rag `library.py::norm_sel_path`）+ 写权限门禁三件套，复用 `core/write_gate.py`。**与库摘要 `propose()` 不同——这里没有"此前非AI手写就直接生效"的快捷分支，永远走门禁**：被排除的文件会从整个检索流程消失，风险等级更高，逐字对齐 obsidian-rag 原文档"硬性确认门禁：本工具绝不直接生效"的措辞。
  - 置信度分档标注——`core/pipeline.py::confidence_tier()`（高相关≥0.75/中相关≥`confidence_warn_threshold`默认0.30/其余弱相关，逐字对齐 `retriever.py::_conf_tier` 分档线），`search_knowledge`/GUI `Api.search` 都补上标注；`confidence_drop_threshold`（默认0.0=关闭）新增但沿用 obsidian-rag 当前口径不默认收紧。
  - 同篇结果封顶——`max_chunks_per_file`（默认3，对齐 obsidian-rag 默认值）在 `search()` 内联生效，GUI/MCP 两个消费方自动获得一致行为，不需要各自实现。
  - 进程单例守卫——新增 `core/singleton.py::ProcessSingletonGuard`（对 PID 文件本身加非阻塞字节锁做原子判定，逐字对齐 obsidian-rag `singleton.py` 的锁原语，同样踩过"Windows 上 `os.kill(pid,0)` 会真的杀掉目标进程"这条坑，改用 `OpenProcess`+`WaitForSingleObject`），接入 `mcp_stdio.py::main()`，真实起两个进程验证过第二个会被正确拒绝。
  - `small_to_big` 父节回填——`official-chunker` 输出父节 id/全文，manifest 每节存一份；正文模式回填、同节折叠、同文件封顶后补位，list 模式保持小块，MCP/GUI 显式返回/显示回填标记。
- **C类四项完成，Graph 基础与终态/导入等价性完成**：
  - `note_relations`+wikilink 解析——新增 `core/note_relations.py`（`extract_wikilink_targets()` 逐字对齐 obsidian-rag `index.py` 同名函数的 `[[目标]]`/`[[目标|别名]]`/`[[目标#标题]]`/`![[嵌入]]` 语法规则；`NoteRelationsStore` 只持久化每个文件的出链列表，入链永远现算，不维护反向索引），`index_library()` 提取阶段顺手记录，新增 `note_relations` MCP 工具，真实构造互链的两篇笔记验证过出链/入链解析正确。
  - `index_status`+后台索引——此前 2026-09-24 记录为线程后台/简化心跳；现已升级为完整状态：`reindex_knowledge` 通过独立 spawn worker 启动，每个库使用跨进程文件锁；worker 独立每 5 秒写心跳并更新 `progress_at`，合法静默宽限内不误报卡死。`index_status` 返回 `healthy`/`stalled_no_heartbeat`/`stalled_no_progress`/`orphaned` 健康状态，MCP `reindex_knowledge` 返回 `run_id`/PID；GUI 展示阶段、进度、健康并可停止本 GUI 自己启动的 worker，foreign 任务拒绝停止。每轮向量、BM25、提取缓存、双链/失败记录和 WEMM 页库先写独立 generation，全部完成后才原子发布；停止、异常或原生崩溃不会切换半成品，旧 generation 会延迟一代清理。GPU 资源锁也改为跨进程文件锁 + 原子 holder 元数据 + 抢占请求：宿主与多个 worker 不会再各自误以为 `gpu:0` 空闲；高优先级检索侧、同级 WEMM/MinerU、以及同 holder 的跨进程模型刷新都会先让旧进程执行 `/evict`/模型卸载再移交锁，15 秒拿不到则按旧项目 `GPU_LOCK` 语义降级，不无限等待。正常结束、异常退出和宿主退出都会清理 worker，Windows 使用 kill-on-close Job Object + `taskkill /T`，POSIX 使用 `PR_SET_PDEATHSIG` + 独立 process group。底层由 `Pipeline.start_index_library()` 和 `core/index_progress.py::IndexWorkerManager` 编排，并保留此前已修复的并发文件写入问题：临时文件+`os.replace` 原子改名，以及 Windows 句柄释放延迟导致的 `PermissionError` 退避重试。
  - **完整文件级增量索引（2026-09-24）**——`core/index_generation.py::IndexManifestStore` 为每个库保存 per-file `size/mtime_ns/content_hash/status`、当前 chunk id、extractor/chunker/embedder/vector/lexical 版本签名和 segment 链。每轮先扫描并规划 `added/changed/removed/unchanged/retried`：内容与 `mtime+size` 都未变直接复用；仅时间戳变但内容哈希相同仍复用；删除/排除从有效清单和 BM25 中清除；失败文件下一轮自动重试。extractor 链变化只重抽取，chunker 变化复用提取正文重切块，embedder/vector store 变化清空不兼容向量段并重嵌入，lexical 版本变化从有效向量记录重建 BM25。向量/提取缓存采用旧 segment + 本轮 delta，manifest 原子切换后读者永远看不到半成品；段积累到阈值时复制现有向量、提取正文和 BM25 状态到 compact segment，不重新调用模型，随后清理不再被当前 generation 引用的旧段。`official-visual-wemm` 同样保存 PDF 指纹/渲染签名/有效页 id，只重渲染新增或修改 PDF，查询跨页段合并并屏蔽删除/失败页，段数阈值无重算压缩。MCP `reindex_knowledge(full=false)` 和 GUI“增量更新”是默认路径，`full=true`/GUI“完整重建”可显式绕过清单。
  - `read_document`——新增 `core/extract_cache.py`（索引时按 generation segment 缓存每个文件的提取正文，哈希文件名规避长中文嵌套路径的 Windows 路径长度问题），`.md`/`.txt` 现读源文件，其余格式沿有效 segment 链读取，未索引/已排除/当前失败文件明确报错。
  - **额外顺手做了审计清单里没点名、但同属"索引失败溯源"这类诊断能力的 `index_failures`**（对齐 obsidian-rag 同名工具；失败文件按当前输入与阶段签名自动重试，成功后从当前 generation 清除）。
  - Graph 基础读模型、PDF→page/pagegroup、双链、语义边、queued/终态和 GUI 面板已完成；导入归档同步恢复关系、失败状态和 WEMM 页状态，`BC-09=pass`。
- 新增/改动测试：`core/test_note_relations.py`（19）、`core/test_index_progress.py`（13）、`core/test_index_failures.py`（6）、`core/test_singleton.py`（17，含真实子进程验证）、`official-library-manager/tests/test_selection.py`+`test_config.py` 新增选择写权限门禁与路径校验用例、`official-mcp-server/tests/test_tools.py`（45，覆盖自动同步、Agent 门禁和导航）、`core/test_pipeline_e2e.py`（118，覆盖终态、freshness、Graph 和完整导入导出）。

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
- ✅ 能索引 demo-vault 和至少一个真实 Obsidian 库，检索质量不低于旧项目当前水平——`tests/test_pipeline_e2e.py` 用真实 `PluginRuntime` 扫描/加载/启用全部插件，索引临时小库后搜索，验证语义相关性、排除文件不泄漏、跨库隔离（词法+向量两路都验证过，词法这路是靠这个测试过程中发现真bug才补上的）；`plugins/official-mcp-server/tests/test_tools.py` 用真实 `MCPServer.call_tool()` 验证 MCP 协议层；额外用真实子进程（`mcp_stdio.py`）+ 真实 MCP 客户端做过一次手动冒烟，`initialize`/`list_tools`/`call_tool` 全部通过真实 stdio 协议接通。以上这些用的都是测试纪律要求的注入假模型（不碰真实 BGE-M3/reranker）。**2026-09-23 补了一次真模型验证**：这台 Windows 机器上 `BAAI/bge-m3`/`BAAI/bge-reranker-v2-m3` 已经在标准 HuggingFace 缓存里（旧项目之前下载过），装上 `torch`/`sentence-transformers` 后强制 `HF_HUB_OFFLINE=1`（联网不通直接报错，排除"偷偷下载"的可能）跑了一次真实索引+搜索，模型从本地缓存直接加载成功，搜"插件架构"确实语义相关地命中了`插件架构入门.md`里讲插件运行时/数据流管理器的段落（confidence 1.0/0.83），不是随便返回点什么。**仍未验证的是"和旧项目实际检索质量对比"**——这需要旧项目的真实 Obsidian 库和一批真实查询词人工比对，本轮没有这份数据，留给你实际用起来后反馈
- ✅ **多库并查检索选择+folder过滤+置信度真分尺度（2026-09-23 补，2026-09-24 GUI 接通）**：`search()` 支持逗号多库并查、all、exclude 反选、folder 过滤；GUI 库列表现在提供全选/清空和复选框，搜索与 Graph 共用同一选择范围，同时保留 active library 作为索引控制和摘要/正文操作对象。新增/改动测试：`core/test_pipeline_e2e.py` 6个真实跨库检索用例（真的建两个库、真的搜到两库结果、真的验证exclude/folder/未知库名报错）、`official-library-manager/tests/test_config.py` 7个 `resolve_libraries` 直测、`official-mcp-server/tests/test_tools.py` 3个新用例，全部真实跑通不是mock
- 🟡 Windows 便携版在一台没装过 Python 的干净 Windows 环境里，从解压到能搜到第一条结果——便携 ZIP 构建路径已完成，仍需在干净机上真实启动 GUI/MCP 并完成 WEMM 首次环境引导
- ✅ Linux 开发环境下等价的源码安装方式能跑通同样的功能——当前所有开发/测试都在 Linux 完成，`core/`+14个官方插件+`mcp_stdio.py`+`gui_main.py` 全部在 Linux venv 里真实跑通
- ✅ 关掉任意一个非必需插件（比如 GUI），核心+MCP 仍能正常工作——`mcp_stdio.py` 的 `REQUIRED_PLUGINS` 列表从不包含 `official-gui-shell`，MCP 全程不依赖它；`test_pipeline_e2e.py` 显式验证 gui-shell 处于"已发现未启用"状态时检索链路完全正常
- ✅ 同时装两个都声明 `embedder` 扩展点的插件，在配置里切换"当前用哪个"不需要重启进程——`ExtensionRegistry.set_active()` 机制在 Phase 0 就已验证（`core/tests/test_runtime.py::test_singleton_conflict_surfaced_not_silent`），Phase 1 未额外造第二个 embedder 实现去重复验证，机制本身没变
- ✅ 全程运行不在系统 Python 的 site-packages 或全局 PATH 留下任何痕迹——所有依赖（jieba/chromadb/pymupdf4llm/python-docx/pywebview/mcp）装在 `.venv/` 隔离环境；venv 用 `--system-site-packages` 创建是本轮唯一的例外，**只是为了在这台 Linux 开发机上借到系统已装的 PyGObject（GTK 绑定）来验证 GUI 真实渲染**，不代表产品设计要求系统预装 GTK——Windows 安装包会自带完整 webview 运行时，不依赖用户机器上有没有装什么

**GUI 渲染的真实验证**：这台开发机恰好装了 WebKit2GTK + 有响应式 X server，重建 venv 借用系统 PyGObject 后，真实调用 `webview.create_window()` + `webview.start()` 打开了窗口，用 `evaluate_js` 确认页面加载后 `document.body.innerText` 真的包含通过 js_api 桥从后端 `Api.list_libraries()` 拉回来的库名字——不是"能 import 就算过"，是真的渲染出了动态内容。后端 `Api` 类本身另有 7 个用例在 `plugins/official-gui-shell/tests/test_api.py` 里，不依赖渲染。

## Phase 2 — 视觉与 OCR 插件

**目标**：MinerU 云端 OCR、MinerU 本机 OCR、WEMM 页级视觉检索，各自独立成插件。

**状态（2026-09-23）**：
- ✅ **`core/subprocess_service.py` 落地**——之前 `core/runtime.py` 对 `subprocess_service` 插件直接 `raise NotImplementedError`（占位），现在真的实现了：`SubprocessServiceHandle` 负责启动声明的 `command`、轮询 `health_check`、本机HTTP调用方法、干净终止子进程（真实验证：`os.kill(pid, 0)` 确认进程真的没了）。自动分配空闲端口，避免多个 `subprocess_service` 插件抢端口。`in_process`/`subprocess_service` 现在走同一条 `_instantiate()` 路径，区别只在于 `entry` 指向的本地类是否用这个工具拉起子进程。8 用例，另有 `tests/test_runtime.py` 3 个真实走完整 `PluginRuntime` 生命周期的用例
- ✅ **`official-ocr-mineru-cloud`**（**改成 `in_process`，不是最初设想的 `subprocess_service`**——只是一次HTTP调用，没有需要独立环境隔离的重依赖，`subprocess_service` 反而是不必要的复杂度；`懒加载/可注入HTTP客户端`模式同 `official-embedder-bge-m3`，单测全程注入假客户端，不碰真实网络/API Key；已恢复旧设计的批量提交/上传/轮询、45 次/分钟闸门、瞬态重试、pending 断点续接、sidecar 和配额记账。11 用例
- ✅ **`official-ocr-mineru-local`**（真实 `subprocess_service`：GPU资源仲裁租约协商 + 真实子进程 + 本机HTTP，`extractor:pdf` 多值扩展点，和 `official-extractor-pdf-text`/云端/本机 OCR 保持整本路由：`pdf_scan_backend` 默认 `none`，任一页文字层 strip 后少于 10 字符就返回精确 `scanned`，只有用户选中的 OCR provider 被调用；已有 OCR 缓存按云端→本机→文字层优先，切换到 `none` 不丢弃可用 OCR 正文）。**真实模型接入+真机验证（2026-09-23）**：探测复用本机已装好的 `uv tool install mineru[all]` 工具环境（不重新下载，`RAG_REDO_MINERU_PYTHON` 可覆盖），子进程真实调用 MinerU 官方 `ReusableLocalAPIServer`（懒加载+两级空闲释放+`/evict`软驱逐+超时随页数缩放），真实识别过中英文混排扫描件，端到端约20~40s；过程中抓到并修复一个真实的子进程输出管道写满导致内服务死锁的bug，完整记录见 TODO 第4条与 `server.py` 模块docstring。`RAG_REDO_FAKE_OCR` 环境变量继续保留给测试/无GPU机器用，注入确定性假结果验证"子进程+HTTP+chain-try链路通不通"。15 个插件生命周期用例，另有 `tests/test_pipeline_e2e.py::TestOcrChainTryFallback` 6 个用例覆盖默认 none、无 Key、混合 PDF 整本路由、缓存优先、索引与搜索
- ✅ **`official-visual-wemm`（2026-09-23，真实 Windows 11 机器上完整实现+真机验证）**——按 AGENTS.md"功能/行为/UI 层面拿不准怎么设计先去 obsidian-rag 找答案"的规则，动手前先完整读了旧项目 `wemm_indexer.py`/`wemm_server.py`/`wemm_retriever.py` 三个文件的真实实现，关键调查结论：**WEMM 从来不参与文字检索的 RRF 融合排序**——旧项目里它是完全独立、单独调用的"第二检索系统"（`navigate_knowledge` 工具，和 `search_knowledge` 彻底分离，绝不混向量空间/绝不混分数），不是要往 `search()` 里加第三路排名。这个发现直接简化了最初以为"需要动 `SearchResult` 融合契约"的设计难题：新增 `core/contracts.py::PageHit`（和 `SearchResult` 互不相通的独立类型）+ `visual_index` 多值扩展点，`core/pipeline.py` 只新增两个方法——`index_library()` 内部一个独立后置阶段（镜像旧项目 `_wemm_auto_phase`，绝不影响文字索引的 report/成败判定）、以及全新的 `navigate()`（不是 `search()` 的变体）。插件本身是真实 `subprocess_service`（GPU资源租约协商 + 真实子进程 + 本机HTTP，同 `official-ocr-mineru-local` 的模式），子进程内部真实用 `transformers.AutoModel`+`qwen_vl_utils` 调用 `tencent/WeMM-Embedding-2B`（`RAG_REDO_FAKE_WEMM=1` 时用确定性假向量，测试用，同 OCR 插件纪律）。存储上**故意不复用** `official-vector-store-chroma` 的 Chroma 目录（那属于伸手进另一个插件的持久化存储，违反数据流铁律5）——用自己的 `ctx.data_dir/visual_wemm/chroma` 独立数据库+独立 collection（`visual_<library_id>`），物理隔离比旧项目"同一个Chroma文件不同collection"的隔离粒度更彻底，效果一致（绝不混向量空间）。新增 MCP 工具 `navigate_knowledge`、GUI Api `navigate()` 方法（GUI 目前不渲染页面缩略图/结果专属界面——调查到旧项目本身也是这样，只有状态/诊断面板，不是这里偷懒）。17个新增/更新用例（`official-visual-wemm` 插件自身7个 + `official-mcp-server` 补的2个），全部真实跑通（真实 Popen 子进程、真实 pymupdf 多页PDF渲染、真实 Chroma 读写、真实清理陈旧页向量、真实 GPU 租约获取/释放/杀子进程验证无游离进程）。**真模型验证**（不是只测假向量）：这台机器上 `tencent/WeMM-Embedding-2B` 已经在标准 HuggingFace 缓存里（用户此前用旧项目下载过），装了 `torch`+`transformers`+`qwen_vl_utils`+`torchvision`（CPU，全部走 PyPI 官方源，没有下载模型权重本身——权重复用现成缓存）后跑了一次真实推理：渲染一页含"插件架构"/"混合检索"文字的PDF，编码成图像向量，和两句文字查询（一句相关一句无关）分别算余弦相似度，相关查询 0.4675 vs 无关查询 0.2098——真实的跨模态语义判别力，不是随机数。CPU单张图编码约54s（含首次模型加载），这台机器没有CUDA GPU。**增量状态（2026-09-24 补齐）**：WEMM 按 PDF 内容指纹、渲染 DPI/维度和插件签名只重渲染新增/修改文件，删除/失败页从有效集合屏蔽；查询跨 segment 合并，段数达到阈值时复制已有页向量压缩，不重新跑模型。**仍存在的限制**：这次真模型验证时是把 torch/transformers/qwen_vl_utils/torchvision 临时装进核心 `.venv` 跑通的——`env_bootstrap` 机制本身之后已经补齐（见下方"GPU/显存精细生命周期管理"段落末尾），`official-visual-wemm/requirements.txt`+`env_bootstrap.py` 也已经写好并声明进了 `plugin.toml`，但这台机器上还没有实际跑过一次"删掉临时装进核心venv的这些包、让插件自己的 `.venv` 从零 env_bootstrap 装一遍"的干净验证，如实记录这个残留的验证缺口，不是机制没做。
- ✅ **GPU 精细生命周期管理（2026-09-23 补齐，按 obsidian-rag/gpu_arbiter.py+wemm_server.py+index.py 真实行为完整移植，不再是"粗粒度占位"）**：新增 `core/gpu_arbiter.py`（vram_free_gb 显存探测/wait_for_vram 阻塞等待/request_evict 主动驱逐，全部 fail-open，供 in_process 插件直接 import）；`core/resource_arbiter.py::acquire` 新增 `preempt_equal` 参数（同一优先级层级"谁刚需要谁能挤开对方"，不需要靠数值分高低，对应旧项目 WEMM/MinerU 互相抢占显存的真实行为）。`official-visual-wemm`/`official-ocr-mineru-local` 的 server.py 现在有真实的两级空闲释放（默认300s空闲卸载模型释放显存但保留子进程、再空闲1800s子进程整体自退出）+ `/evict` 软驱逐端点（收到请求立即卸载，正在编码的一条会等做完再卸）+ 真实 VRAM 门槛等待（加载前等其他模型让路）；plugin.py 一侧新增 `_ensure_alive()`（子进程可能因空闲自退出而不在了，真正调用前按需透明重新拉起，同旧项目 `gpu_arbiter.ensure_server` 的幂等语义，避免"省资源"的优化变成"用久了突然不工作"的真实回归）、`_soft_evict()`（资源仲裁器的抢占回调只请求卸载模型，不整个杀掉子进程）。`official-embedder-bge-m3`/`official-reranker`（in_process，跑在核心进程里）现在也真的参与 GPU 仲裁：有 CUDA 优先用 CUDA、加载前用高优先级（100，压过 WEMM/OCR-local 的10）抢占式申请"gpu:0"名额（对齐旧项目"检索优先抢占"）、空闲300s后台线程自动卸载模型（两个插件各自的空闲计时器，不是共享一个，但效果上等价于旧项目"检索侧整体空闲卸载"）。这台开发机没有真实 CUDA GPU，抢占/驱逐的行为用真实 `ResourceArbiter`+mock CUDA 探测验证过（见各插件 tests/test_*.py 新增的 GPU 仲裁用例），不是纸面设计。**跨进程补齐（2026-09-24）**：GUI/MCP 宿主和 spawn worker 不再各自只看进程内 holder；`ResourceArbiter` 用文件锁作为唯一互斥依据，原子 holder 元数据让另一进程知道当前 holder/优先级，抢占请求触发持锁进程执行既有 `on_preempt` 后真正让锁。已用真实父进程 WEMM/MinerU → 子进程完整 PluginRuntime 启用 → 父进程再次使用的回归验证，不只是 mock。**仍然如实记录的简化**：①CUDA 失败没有旧项目那套"冷却期+定时自动探测切回"状态机，只做单次降级到CPU；②embedder/reranker 共用同一个 GPU 名额持有者身份（`GPU_HOLDER_ID`），只禁用其中一个而不禁用另一个这种罕见配置下，名额可能被过早释放（详见 rerank.py 模块 docstring）；③子进程侧的空闲卸载/自退出常数是硬编码模块常量（项目现有约定，无配置系统），不像旧项目那样能在设置页调
- ✅ **MinerU 本机 OCR 的独立 py 环境引导（`env_bootstrap`）**——`plugin.toml` 声明的 `env_bootstrap` 已由 `core/subprocess_service.py` 实际执行；源码环境使用当前解释器，冻结环境优先使用安装目录内的便携 Python，缺失时明确拒绝，不会把冻结 exe 自我复制。测试通过 `RAG_REDO_SKIP_ENV_BOOTSTRAP` 跳过重依赖安装。

## Phase 3 — 治理与体验类插件

**目标**：去重（MinHash+LSH）、库 AI 摘要、Agent 写权限门禁通用化、导出/导入迁移工具、旧 `libraries.json` 配置迁移脚本（一次性，对应已确认的迁移需求）。

**状态（2026-09-23）**：
- ✅ 去重——`official-dedup`，MinHash+LSH（`datasketch`），10 用例
- ✅ 旧 `libraries.json` 配置迁移——`tools/migrate_libraries_json.py`，12 用例，字段映射来自实际读旧项目 `library.py`/`index.py` 源码
- ✅ 导出/导入迁移工具——`official-import-export`（`archive_codec` 单例扩展点，zip 归档格式，manifest/vectors/bm25 三份 JSON）+ `core/pipeline.py` 的 `export_library`/`import_library`（和 `index_library`/`search` 同一种"只有编排层知道跨插件顺序"的编排方法：`archive_codec` 插件本身不知道 library_manager/lexical_index/vector_store 的存在，只负责归档的打包/解包，见 `official-import-export` 插件模块 docstring）。真实往返测试覆盖：`tests/test_pipeline_e2e.py::TestExportImportLibrary`（编排层）、`plugins/official-mcp-server/tests/test_tools.py`（MCP 工具 `export_library`/`import_library`）、`plugins/official-gui-shell/tests/test_api.py`（GUI Api，真实写/读归档文件）。已知的刻意简化：导入目标 `library_id` 如果已存在会直接拒绝，不支持"覆盖已有库"（部分覆盖导致新旧数据混杂的正确性风险大于"必须先手动删除旧库"这点不便，见 `core/pipeline.py` 的 `import_library` 注释）
- ✅ **库 AI 摘要 + Agent 写权限门禁通用化（2026-09-23，两项一起完成——库摘要是写权限门禁的第一个真实调用方）**：调查了旧项目 `library_summary.py`/`summary_gate.py`/`server.py` 的真实实现后确认关键设计——①采样用最远点采样直接复用索引时已经算好的向量（"免费的副产品"，不为了写简介重新读全文）；②对话中的 AI agent 自己用采样片段写简介、调 `propose_library_summary` 提交（省一次LLM调用），GUI"刷新简介"按钮走另一条路径真的调一次配置好的 LLM；③AI生成的简介随便覆盖，用户手写的简介覆盖前必须走写权限门禁两段式确认。落地成三个新增/改动点：a) `official-vector-store-chroma` 新增 `sample()`（纯Python最远点采样，不新增numpy依赖）；b) 新插件 `official-llm-openai-compatible`（`llm_provider="multi"` 扩展点，OpenAI兼容chat completions协议，环境变量配置对齐旧项目LM Studio默认值，`core/pipeline.py` 按插件id字母序链式尝试，和 `extractor:pdf` chain-try同一个模式，为未来第二个provider留好扩展位）；c) 新插件 `official-library-summary`（`library_summary="singleton"` 扩展点，自己的 `ctx.data_dir` 存储，`propose()`/`apply()` 直接调用 `ctx.write_gate`——这是 `core/write_gate.py` 落地以来第一个真实调用方，真实端到端验证过"用户手写简介→AI提案→用户确认码校验→生效"完整链路，见 `tests/test_pipeline_e2e.py::TestLibrarySummaryPipeline::test_propose_over_user_summary_requires_gate_confirmation`）。新增 MCP 工具 `get_library_sample`/`propose_library_summary`/`apply_library_summary`，`list_libraries` 补充 `summary` 字段（library_summary插件未启用时优雅降级为None，不影响核心检索功能，保住"关掉任意非必需插件核心仍正常工作"这条验收标准）；GUI Api 新增 `get_library_summary`/`set_library_summary`（用户手写，无条件生效不走门禁）/`refresh_library_summary`（force参数控制是否覆盖用户手写）。**已知的刻意简化**：①内容指纹用采样片段内容算，不是旧项目"聚合全部文件路径:md5"的精确算法（rag-redo暂无中心化的"库全量文件哈希"查询接口，为这一处单独设计跨插件契约不成比例，效果上仍能达到"提示可能已过时"的目的）；②CUDA/思考型模型冷却重试这类旧项目`call_llm`早就处理过的细节沿用了同一套fail-open空串返回，但没有独立实现冷却计时器（同GPU那次改动"已知简化"的同一条理由）；③GUI"刷新简介"旧项目走后台线程+轮询，这里简化成同步调用（rag-redo的GUI Api层此前完全没有这类异步基础设施，如实记录不是遗漏）；④embedder/reranker那次GPU改动里提到的"共享GPU_HOLDER_ID"简化在这里不涉及，llm_provider是纯网络调用不占用GPU资源仲裁。

## Phase 4 — 打包收尾与文档定稿

**状态（2026-09-23，真实 Windows 11 机器上做的，不是 Linux 沙盒里假设的）**：

- ✅ **换机器验证**：项目此前只在 Linux 沙盒里开发过，第一次在真实 Windows 11 机器上重建 `.venv`、装轻量依赖、跑 `tests/run.py`——过程中真实发现并修了4个此前从没在真机上暴露过的 Windows 兼容性 bug：①测试构造合成插件时把 `sys.executable`（Windows 路径带反斜杠）直接拼进 TOML 字符串，触发 TOML 转义解析错误；② `os.kill(pid, 0)` 探测进程存活的 POSIX 惯用法在 Windows 上不成立（抛 `OSError` 不是 `ProcessLookupError`），改用 Win32 `OpenProcess` 判断；③ `official-extractor-text` 读文件不做换行符归一化，Windows 上 `\r\n` 原样带进检索文本；④ `tests/run.py` 打印中文用例名时，非真实控制台（管道/重定向）下 stdout 编码退化成系统码页，`UnicodeEncodeError` 崩溃，改成显式 `reconfigure(encoding="utf-8")`。同时发现一个更严重的问题：`tests/test_runtime.py` 里一个测试真的启动子进程做真实验证，却没有对应的 `tearDown` 兜底清理——在一次性沙盒环境里这个坑完全不可见（进程随容器一起没了），只有在持久化的真实机器上跑几次测试后才会看到系统里堆积出真的游离 `server.py` 进程，已修（架构红线6"不产生游离进程"对测试代码自己同样适用）。全部 27 个测试套件（255+ 用例）修完后在真实 Windows 上稳定全绿。
- ✅ **GUI/MCP 两个入口在真实 Windows 上跑通**：`gui_main.py` 真的用 pywebview 弹出窗口、加载 Edge WebView2 后端、渲染出库管理/搜索界面（截图验证过内容不是空白/报错页）；`mcp_stdio.py` 真的被当子进程 Popen 起来，用真实 JSON-RPC over stdio 做过 `initialize`/`tools/list`/`tools/call`，5个工具（search_knowledge/list_libraries/reindex_knowledge/export_library/import_library）全部正常响应——这两项此前在 Linux 沙盒里从未被真实验证过（GUI 只在 WebKit2GTK 上测过，MCP 只测过协议层不测过真实 Windows 进程拉起）。
- ✅ **Windows 便携版构建（替代 PyInstaller）**：`installer/build_windows.py` 复制独立 Python 运行时、当前 venv 的运行时依赖、核心代码和插件，生成 `dist/rag-redo-portable.zip`；用户解压后双击 `start-gui.cmd` 或使用 `start-mcp.cmd`，不需要安装 Python、CUDA、Node 或 Inno Setup。真实构建和干净机启动仍待本轮验证。
- ✅ **`core/subprocess_service.py` 的进程树清理修复（2026-09-23，做 `official-visual-wemm` 真机验证时发现）**：在这台机器上真实跑了几十轮全量测试后，用 `Get-CimInstance Win32_Process` 抓到过上百个真游离的 `server.py` 子进程，一路查下去确认根因——这台机器的 Python 安装里，`.venv\Scripts\python.exe` 自己会再派生一个真正执行代码的子进程，`Popen.terminate()`/`.kill()` 只杀得掉外层那个，真正绑着端口的子进程完全不受影响，变成架构红线6明令禁止的"游离进程"。改成 Windows 上用系统自带的 `taskkill /F /T` 连整棵进程树一起杀（POSIX 上给子进程开独立进程组、对整个组发信号），绝大多数场景下验证过干净（单独跑任意一个测试文件都不再留痕）。**如实记录一个没能在这轮彻底根除的残留**：连续快速跑完全部27+个测试套件这种高频场景下，仍然偶发看到数量不固定（0~10对）的残留，`taskkill` 自己都报告成功，行为更像是这台机器的 Python 发行版本身在某个时间窗口又派生了一次子进程、taskkill 扫描时还没抓到——没能在这轮定位到那一层的根因，如实记录不假装完全修好；真实影响面有限（只在测试高频连续启停子进程时可能触发，打包成 PyInstaller 冻结产物后不经过这层"venv转发"，不会有这个问题）。顺带修了一个同类的真实 bug：`official-mcp-server/tests/test_tools.py` 的测试基类此前完全没有 `tearDown`，因为它原本的 `REQUIRED_PLUGINS` 全是 `in_process` 插件，没人管禁用与否都不会露出破绽——这次给它加了 `official-visual-wemm`（真实 `subprocess_service`）之后这个此前隐形的坑就现出了原形，已经补上 `asyncTearDown`。
- 🕘 **Inno Setup 安装包包装（历史方案，2026-09-23）**：曾新增 `installer/rag-redo.iss`。设计决策（旧项目从来没做过安装包，这是真正的新设计决策）：`/CURRENTUSER` 免管理员权限/UAC（目标用户不懂电脑，弹UAC是不必要的门槛）；GUI 和 MCP 两个冻结产物分装进 `{app}\gui\`/`{app}\mcp\` 两个子文件夹（避免两份 PyInstaller onedir 各自的 `_internal` 运行时目录互相覆盖）；只注册开始菜单快捷方式+卸载入口，不碰其他注册表/全局PATH（架构红线7字面落实）。**为了让 GUI 和 MCP 两个分装的产物共享同一批用户数据**，改了 `gui_main.py`/`mcp_stdio.py` 的数据目录解析：冻结时不再用"exe自己所在目录/data"（那样两个分装产物会各有一份互不相通的数据），改用 `%LOCALAPPDATA%\RAG-Redo\data`（Windows标准的"这个应用自己的用户数据"位置，也是卸载程序体不会碰到的位置）；源码/开发环境下行为不变（仍是 `REPO_ROOT/data`），不影响现有测试。真实验证过完整循环：装 Inno Setup 6 编译器→编译出 `rag-redo-setup-0.1.0.exe`（约600MB，体积原因见下；脚本现已升级为 `0.2.0`，并额外安装 `{app}\runtime\python` 便携解释器）→真实静默安装（免UAC）→开始菜单快捷方式和 `HKCU` 卸载注册表项确认真的注册了→真实启动装好的 `rag-redo-gui.exe`（真弹窗、有响应）和 `rag-redo-mcp.exe`（真实 JSON-RPC over stdio 完整握手+调用）→真实运行卸载程序，确认应用目录/开始菜单/注册表项都清干净了、但用户数据目录完好保留（卸载≠清空用户数据，刻意设计）。
**这一轮真实验证时抓到并修复了两个此前完全没暴露过的真实bug**（都不是纸面设计审查能发现的，必须真装真跑）：①PyInstaller 静态分析漏收 `core` 包里只被插件动态import的子模块（`core/gpu_arbiter.py`/`core/subprocess_service.py`），插件加载报 ImportError——修法是 `build_windows.py` 加 `--collect-submodules core`；②**更严重的一个**：`core/subprocess_service.py::resolve_plugin_python()` 原本"找不到插件专属venv就退化用核心解释器"这条 fail-open 路径，在冻结产物里会把 `sys.executable`（冻结exe自己）当成通用python塞进子进程启动命令，导致应用把自己重新拉起、递归自我复制——真实观测到几分钟内四十多个游离 `rag-redo-mcp.exe` 进程，是架构红线6明令禁止的场景以一种全新方式复现；修法是冻结环境下这条路径直接改成抛出清楚的 `SubprocessServiceError`，绝不允许静默退化成自我复制的错误命令，新增3个回归测试（`tests/test_subprocess_service.py::TestResolvePluginPythonEnvBootstrap` 里的 `test_frozen_*`）。完整过程见 [installer/README.md](../installer/README.md)。
- ⬜ **在一台没装过任何开发工具的干净 Windows 机器上验证打包产物**——目前只在打包机器本机验证过完整安装/卸载循环，这是比"本机能装能卸"更严格的最终验收场景
- ✅ 安装/卸载前后系统关键位置（`HKCU`注册表、开始菜单）无残留变化——见上条"Inno Setup安装包包装"的真实验证记录；全局注册表/全局PATH本来就没有触碰过（`/CURRENTUSER`模式决定的）
- ✅ README.md 补全真实安装步骤，README.en.md 英文镜像已创建（2026-09-23）——**还没补的是截图**，这台机器上没有截图工具接入这次的自动化验证流程
- ✅ [docs/legacy/](legacy/) 归档内容与 [docs/LESSONS.md](LESSONS.md) 精炼版交叉核对（2026-09-23，见下方新增的8条教训）
- ⬜ 全量回归测试纪律对齐旧项目（隔离测试环境、假 HTTP 注入、隐藏测试库模式）——这条本身已经在做（见上面"换机器验证"），但还没有系统性地对照旧项目纪律逐条过一遍
- ⬜ **便携包体积优化与干净机验收**：当前便携包会携带当前 venv 的运行时依赖，体积仍可能偏大；下一步是建立最小运行时依赖清单并在无开发工具的 Windows 机器上验证 GUI/MCP/WEMM。
- ⬜ 全量回归测试纪律对齐旧项目（隔离测试环境、假 HTTP 注入、隐藏测试库模式）——这条本身已经在做（见上面"换机器验证"），但还没有系统性地对照旧项目纪律逐条过一遍

## 开放问题（需要你审阅确认）

- Agent 写权限门禁、资源仲裁被我定为"核心服务"而非"插件"（ARCHITECTURE.md 2.2节），因为它们是跨插件的裁判角色，没法被单个插件公正地扮演。这个判断如果你不同意，会影响 Phase 0 的验收标准设计，请优先确认。

（2026-09-22 更新：WEMM 官方插件、Flet GUI 不迁移这两处已确认，见 [FEATURE_TRIAGE.md](FEATURE_TRIAGE.md)。同时新增两条硬约束——模型/Provider 类扩展点必须支持不重启切换、核心与插件不能污染主机环境——已写入 [AGENTS.md](../AGENTS.md)"架构红线"，下面 Phase 0/1 验收标准已同步补充对应检查项。）
