# 当前目标

## 目标

**（当前，2026-09-29 晚锁定）** 全部执行和修复——把操作者真机增量重建 Y2S1 库时反馈的 7 个问题（显卡吃不满、CPU 吃满/两个模型同时占显存/转一个索引一个、总览页 WEMM/缓存/PDF 开关、WEMM 等中间环节是否真被调用、CamScanner 扫描件被误判为空、设置页缺 MinerU API Key 等、关 GUI 后显存与进程残留）对应的方案 A~E 及配套项一次性全部落地并修好：A 索引顺序改回“先全部转换切块→连续做向量→一次写入”，B 轮到 WEMM 前先让文字模型让出显卡，C 本机 MinerU 服务改为可并发应答且健康检查不再被拖慢，D 设置页补 MinerU API Key 等真实在用的键并让云端识别读设置，E 水印页不再冒充文字层，另加关窗回收日志与 MinerU 单文件 CPU/显卡实测。

**（前序，2026-09-28 锁定，原样保留）** 先把 GUI 相关的问题全部搞定——GUI 必须能真正启动、能让操作者上手测试（最高优先级：GUI 起不来，操作者就没法做任何实际测试）；GUI 之后，把 2026-09-28 完整审计（`PROBLEMS_2026-09-28.md` 的 A1–A22）里其余可以解决的问题一次性全部解决，全程不回归、按主题提交、不 push。

**（前序，原样保留）** 把 2026-09-23 全面功能审计（对照 obsidian-rag 16个MCP工具 + 约60个CFG配置项）里列出的全部A/B/C类缺口实现完，让 rag-redo 达到与 obsidian-rag 的功能对等，同时全程保持 33+ 个既有测试套件不回归。

## 验收标准

> 命令均为 Windows PowerShell 写法，在仓库根目录执行。公共前置：`$py = ".venv\Scripts\python.exe"; $env:PYTHONIOENCODING = "utf-8"`。
> 单个测试文件用 `& $py tests\run.py --suite "$PWD\<相对路径>"` 运行（`--suite` 必须给绝对路径）。

### 前序目标的验收标准（C1–C5，原样保留）

- **C1（回归门槛）**：现有全部测试套件保持全绿，任何新功能开发都不能破坏已有能力。
  验证：`& ".venv\Scripts\python.exe" tests\run.py`
  预期：`$LASTEXITCODE -eq 0`，输出末尾出现"XX/XX 套通过"且套数不低于当前的34。

- **C2（MCP工具集对齐）**：`official-mcp-server` 注册的工具名集合完整覆盖 obsidian-rag `server.py` 的全部16个工具（`list_libraries`/`get_selection`/`propose_selection_changes`/`apply_selection_changes`/`get_library_sample`/`propose_library_summary`/`apply_library_summary`/`note_relations`/`search_knowledge`/`reindex_knowledge`/`index_status`/`navigate_knowledge`/`read_document`/`find_duplicates`/`index_failures`/`wemm_status`）——`export_library`/`import_library` 等 rag-redo 独有的额外工具不受影响，只看"是否覆盖"不看"是否相等"。
  验证：`& ".venv\Scripts\python.exe" -m unittest plugins.official-mcp-server.tests.test_tools -v 2>&1 | Select-String "test_tools_are_discoverable_with_schema"`
  预期：该测试用例存在且断言这16个工具名全部在 `list_tools()` 返回集合内，`$LASTEXITCODE -eq 0`。

- **C3（后台索引+进度查询）**：`reindex_knowledge` 改为后台执行、立即返回；`index_status` 能查到真实的处理中/完成/心跳状态，不是占位返回值。
  验证：`& ".venv\Scripts\python.exe" -m unittest plugins.official-mcp-server.tests.test_tools -k index_status`（或等价的新增测试文件）
  预期：真实测试断言"调用后立即返回"+"轮询能看到进度变化直至完成"，`$LASTEXITCODE -eq 0`。

- **C4（双链解析可用）**：`[[wikilink]]` 语法真实被解析（提取阶段落地"出链/入链"记录），`note_relations` 返回真实的出链/入链而不是空壳。
  验证：`& ".venv\Scripts\python.exe" -m unittest plugins.official-mcp-server.tests.test_tools -k note_relations`
  预期：真实构造两篇互相 `[[链接]]` 的笔记，索引后查询能拿到正确的出链/入链列表，`$LASTEXITCODE -eq 0`。

- **C5（文档闭环，人工确认）**：`docs/ROADMAP.md`"2026-09-23 全面功能审计"小节里 A/B/C 类的每一条都从"未实现"更新为带真实验证细节的"✅ 已完成"记录（同已完成条目一贯的写法：做了什么、怎么验证的、有什么已知简化）。
  验证：人工通读该小节，逐条核对是否名实相符——机器很难判定"文档描述是否诚实"，这条不做自动化 grep。

### 当前目标的验收标准（C6–C12，新增；按优先级排序，先 GUI 后其余）

- **C6（GUI 真的能启动——最高优先级；对应 A1 / A7 / A16）**：GUI 全部 20 个插件能经真实 `PluginRuntime` 加载并启用（无 `invalid`/`failed`），`gui_main.main()` 的启动路径经得起"走真实入口"的门禁——此前 60+ 条 GUI 测试全绿却起不来，正是因为没有任何测试走过真实加载路径。
  验证（自动）：新增 `plugins\official-gui-shell\tests\test_boot.py`，`& $py tests\run.py --suite "$PWD\plugins\official-gui-shell\tests\test_boot.py"`
  预期：`$LASTEXITCODE -eq 0`；该文件至少断言：①`gui_main.REQUIRED_PLUGINS` 全部经真实 `PluginRuntime` 加载并启用后无一为 `invalid`/`failed`；②用假 `webview` 跑 `gui_main.main()`（临时数据目录）：`create_window` 收到与 `tests/fixtures/legacy_guiweb_contract.json` 一致的窗口规格、`bind_window` 被调用、推送线程真实向前端推送并在窗口关闭后停止不残留；③任一必需插件 `invalid`/`failed` 时 `main()` 打印原因并以非零码退出（不再静默放过）；④`paths.plugins_state_file()` 无参调用不抛错，GUI/MCP/CLI 三入口共用同一份 `core.paths`。
  验证（本机真实进程冒烟，需图形会话 + WebView2）：`$env:RAG_REDO_DATA_ROOT = Join-Path $env:TEMP "rag-redo-smoke"; $p = Start-Process -PassThru -FilePath $py -ArgumentList "gui_main.py"; Start-Sleep 25; Get-Process | Where-Object { $_.MainWindowTitle -eq "Obsidian RAG 2.0" }; Stop-Process -Id $p.Id`
  预期：能查到标题为 `Obsidian RAG 2.0` 的窗口进程（2026-09-29 操作者确认窗口标题定为该名字，原写的 `RAG REDO` 已按此改）（venv 启动器会派生子进程，窗口在子进程上，需按标题查、按精确 PID 清理）。
  **人工确认（操作者）**：亲手运行 `.venv\Scripts\python.exe gui_main.py`，窗口弹出且各页面可进入。窗口内观感只有操作者的眼睛能最终判定，这一步我不代勾。

- **C7（GUI 桥接契约真实对齐；对应 A5 / A6）**：旧项目 `guiweb/contracts.md` 的 37 个桥接方法，返回值的键集真实满足固定夹具，而不是只比签名；库"显示名"与 `library_id` 不再混用。
  验证：`& $py tests\run.py --suite "$PWD\plugins\official-gui-shell\tests\test_contracts_parity.py"`
  预期：`$LASTEXITCODE -eq 0`；测试对夹具 `legacy_guiweb_contract.json` 的全部 37 个方法逐个真实调用（临时数据目录 + 假嵌入/假 HTTP），断言返回值含 `required_keys`（列表项含 `item_required_keys`）；含"显示名 ≠ id 的库调用全部按名参数方法，行为与用 id 调用一致，未知名报错而不是假成功"的用例。

- **C8（GUI 行为回归；对应 A3 / A4 / A7 / A8 / A10 / A12）**：`test_api.py` 迁移到新 API 后全绿；设置保存不再静默丢值；选择树与旧项目 `guiweb/bridge.py:434-557` 语义一致；进度/KPI 是真实值；提取试验台可取消可超时；`index_stats.files` 口径与 docstring 一致。
  验证：`foreach ($s in "plugins\official-gui-shell\tests\test_api.py","plugins\official-gui-shell\tests\test_assets.py","plugins\official-library-manager\tests\test_selection.py","plugins\official-library-manager\tests\test_config.py") { & $py tests\run.py --suite "$PWD\$s"; if ($LASTEXITCODE -ne 0) { break } }`
  预期：`$LASTEXITCODE -eq 0`；且新增用例覆盖：①对全部 `SETTING_FIELD_META` 键"保存→读回相等"往返，转换失败进 `errors` 且不落盘；②5 文件夹选择树夹具（`docs` 中性为 `auto_in`、`docs/private` 可见且 `self_blocked=True`、`report.docx` 可见为 `auto_out`、非法 `sub` 返回 error）；③`_progress_snapshot` 随 `index_status` 变化；④`preview_cancel` 后任务确实停止 + 超时用例；⑤1 个 `empty` + 2 个成功 → `index_stats.files` 与文档口径一致。

- **C9（`find_duplicates` 修复且不越权；对应 A2）**：单库不再崩溃；多库（含默认 `libraries=""`）也按每个库各自的 Agent 授权格式过滤，未授权格式的文件名与重复关系不出现在返回里（BC-02 保持 `pass` 的前提）。
  验证：`& $py tests\run.py --suite "$PWD\plugins\official-mcp-server\tests\test_tools.py"`
  预期：`$LASTEXITCODE -eq 0`；含此前失败的 4 个 `find_duplicates` 用例转绿，以及新增"两库 + 未授权 docx → docx 不出现"用例。

- **C10（其余审计项；对应 A9 / A11 / A14 / A17 / A19 / A20 / A21 / A22）**：`IndexWorkerManager.stop` 不再因读侧瞬时失败误拒；CLI 补齐 `official-visual-wemm`（三入口清单一致，差异走显式白名单）；契约门禁校验 `test_refs` 真实存在；无 `SyntaxWarning`；测试替身/回溯噪声、失效注释与 docstring、`unpack` 体积上限按清单处理。
  验证：`& $py tests\test_agents_contract.py`；`& $py -W error -m py_compile plugins\official-extractor-docx\official_extractor_docx\extract.py plugins\official-vector-store-chroma\tests\test_store.py`；以及各项对应的回归测试（随 C1 全量回归执行）。
  预期：全部 `$LASTEXITCODE -eq 0`；契约门禁内含自测——构造一条失效引用时门禁必须转红；`stop` 用注入 `PermissionError` 的确定性用例通过。

- **C11（全量回归 + 提交卫生；对应 A15）**：C1 全量回归 `$LASTEXITCODE -eq 0`（基线 48/51，目标 ≥51/51，含新增门禁）；此前累积的约 80 处未提交改动按主题拆分提交（中文 + `feat:/fix:/docs:/test:` 前缀，带 `Co-Authored-By` 行），只用显式路径 `git add`，不含凭据/真实 Vault/模型缓存/索引库/临时数据，不 push。
  验证：`& $py tests\run.py`；`git status --porcelain`；`git log --oneline e141ce4..HEAD`；`git ls-files | Select-String -Pattern "data-real|\.sqlite3?$|\.db$|\.env$|\.gguf$|\.safetensors$"`；`& $py tests\test_agents_contract.py`（含 AGENTS.md/CLAUDE.md 同步）
  预期：全量回归 0；`git status --porcelain` 无输出；`git log` 显示多个按主题的提交；敏感文件模式无匹配；未执行 push。

- **C12（文档闭环，人工确认）**：`docs/ROADMAP.md` 补记 2026-09-26～28 的实际工作与 BC-15 的真实状态；`docs/behavior_contract.json` 里 BC-15 的"当前实现/测试引用"与实况一致，状态不虚标。
  验证：人工通读——机器无法判定文档是否诚实，不做自动化 grep。**C12 与 C5 一样由操作者确认，我不自行勾选。**

### 当前目标的验收标准（C13–C19，2026-09-29 晚新增）

- **C13（A 索引顺序，BC-04/BC-05/BC-15）**：`index_library` 改为三段——①逐文件“提取→清洗→切块”并收集；②对全部待嵌块连续做向量（不再夹着提取/写入）；③统一写入向量库/词法库/清单。结果（块、清单、失败终态、返回报告）与改前逐项等价，停止/异常/崩溃仍不发布半成品 generation。
  验证：`& $py tests\run.py --suite "$PWD\tests\test_pipeline_e2e.py"`；`& $py tests\run.py --suite "$PWD\tests\test_index_progress.py"`
  预期：`$LASTEXITCODE -eq 0`；新增用例断言“嵌入器第一次被调用之前，所有待处理文件都已提取完”“deferred/失败/空文件的终态与旧路径一致”“中途异常不发布 generation”。

- **C14（B WEMM 交接，BC-11/BC-15）**：有 PDF 需要建页库时，先让文字向量模型与重排模型释放显卡名额再交给 WEMM；没有 PDF 需要建页库时不白白释放。
  验证：`& $py tests\run.py --suite "$PWD\tests\test_pipeline_e2e.py"`
  预期：`$LASTEXITCODE -eq 0`；新增用例：有变更 PDF → 视觉索引开跑前 embedder/reranker 的 `release_gpu` 已被调用；纯 md 增量 → 不调用。

- **C15（C 本机 MinerU 健康检查，BC-11）**：`official-ocr-mineru-local` 的服务改用 `ThreadingHTTPServer`；`/health` 不再在请求线程里导入 torch，长时间解析进行中 `/health` 仍秒回。
  验证：`& $py tests\run.py --suite "$PWD\plugins\official-ocr-mineru-local\tests\test_plugin.py"`
  预期：`$LASTEXITCODE -eq 0`；新增用例：`/extract` 卡住时并发请求 `/health` 在 1 秒内返回 200；源码不再使用单线程的 `http.server.HTTPServer(`。

- **C16（D 设置页补键，BC-15/BC-01）**：设置页“PDF 与云端 OCR”组补上 `mineru_api_key`（保密字段，界面不回显明文，不进日志）与 `mineru_model_version`；MinerU 云端插件优先读设置、环境变量兜底。
  验证：`& $py tests\run.py --suite "$PWD\plugins\official-gui-shell\tests\test_api.py"`；`& $py tests\run.py --suite "$PWD\plugins\official-gui-shell\tests\test_contracts_parity.py"`；`& $py tests\run.py --suite "$PWD\plugins\official-ocr-mineru-cloud\tests\test_plugin.py"`
  预期：`$LASTEXITCODE -eq 0`；新增用例：保存→读回往返、保密字段不回显、设置里有 key 时无需环境变量即视为“有 key”、能力签名随 key 有无变化。

- **C17（E 水印页，BC-01，操作者 2026-09-29 确认）**：PDF 每页文字都是同一句短话（如 “CamScanner”）时，视为无文字层，整本走 OCR 路由，不再被水印骗成“有文字→切块后为空”。
  验证：`& $py tests\run.py --suite "$PWD\plugins\official-extractor-pdf-text\tests\test_extract.py"`
  预期：`$LASTEXITCODE -eq 0`；新增用例：5 页全是 “CamScanner” → `failure_reason == "scanned"`；正常文字 PDF、带页码/页眉但内容各不相同的 PDF 不受影响；`docs/behavior_contract.json` 的 BC-01 登记该偏离并引用该测试。

- **C18（关窗回收日志 + MinerU 实测）**：关窗时把“回收了几个子进程、用时多久”写进 GUI 日志；对一个小 PDF 单独实测 MinerU 的 CPU/显卡占用并记入 ROADMAP。
  验证：`& $py tests\run.py --suite "$PWD\plugins\official-gui-shell\tests\test_boot.py"`；人工阅读 `docs/ROADMAP.md` 对应小节。
  预期：`$LASTEXITCODE -eq 0`；新增用例断言关窗后日志含回收摘要；实测数字由本机真实跑出，未跑成则如实写“未测”。**实测数字的真伪只有操作者机器能复核，我不代勾。**

- **C19（回归 + 契约 + 提交卫生）**：`& $py tests\run.py` 全绿（允许已知的“杀掉子进程后立刻查是否消失”类间歇用例单独重跑通过并如实说明）；`& $py tests\test_agents_contract.py` 通过；按主题分次提交，未 push，未提交 `data-real/`。
  验证：`& $py tests\run.py`；`& $py tests\test_agents_contract.py`；`git status --porcelain`；`git log --oneline -12`
  预期：全量回归 0；门禁 0；`git status --porcelain` 无源码改动（`docs/media/` 非我创建，不动不提交）。


## 范围

**做（2026-09-29 晚新增，先于下面各项执行）**：方案 A~E（见 C13–C17）、关窗回收日志与 MinerU 单文件实测（C18）、随之而来的契约/ROADMAP/测试更新（C19）。

**做**（前序，按顺序：先 GUI，再其余）：
- GUI：A1（插件 invalid）、A7（进度/推送/`bind_window`/窗口规格/设置分组）、A5（契约键集）、A6（显示名 vs id）、A4（设置保存）、A8（选择树）、A10（提取试验台）、A12（KPI 口径）、A3（`test_api.py` 迁移）、A16（`core.paths` 遮蔽）
- 其余：A2（`find_duplicates` 崩溃 + 越权）、A9（停止按钮间歇拒绝）、A11（CLI 缺 visual-wemm）、A14（门禁校验引用存在）、A17（SyntaxWarning）、A19/A20（测试进程与日志噪声）、A21（错误注释/docstring）、A22（`unpack` 体积上限 + 升级路径说明）
- 补"走真实入口"的门禁，杜绝"直接实例化 + 只 grep 文本"的假绿
- 按主题拆分提交现有未提交改动（不 push）；更新 ROADMAP 与 GOAL 进度

**不做（2026-09-29 晚新增）**：不把显卡单批 8 段的旧项目安全上限调大；不限制 PDF 转文字器的 CPU 线程数；不恢复旧项目 13 组设置里“新版本本来没有对应功能”的键（`mineru_concurrency`/`mineru_rate_per_minute`/`pdf_text_backend` 等只在有真实实现时才补）；不 push。

**不做（前序，明确排除，需要时再单独提出）**：
- 不 push；不代勾 C5 / C12；不改动 C1–C5 已锁定的验收标准
- A13（文本提取器 BOM 剥离 + CRLF→LF、单例守卫不做 PID 预检）：这是对旧行为的偏离，按 CLAUDE.md §3.3 必须先由操作者确认再写入行为契约——我只准备条目草案并请示，不自行宣称"已批准"
- A18 中 `%TEMP%` 里 2615 个历史测试残留目录的存量清理（由操作者决定）；BC-15 是否翻为 `pass` 由操作者确认
- 新增产品功能、GUI 视觉重设计（GUI 资产按 BC-15 逐字节复刻旧项目，不改）、打包/安装器干净机验证
- 上一轮审计标注"未深审"的区域（`resource_arbiter`、`subprocess_service`、rerank、Chroma store、OCR/WEMM 插件差异、result-advisor、installer、archive 内部）——除非修复过程中被直接牵连
- 前序目标里已明确排除的项（D 类 `check_notes.py`、HyDE 本轮范围、安装包验证、完整图形化设置面板/Graph 完整渲染面板）

## 注意事项（无法机器校验的部分）

- GUI 窗口内的真实观感与手感，只有操作者亲手运行才能最终确认（C6 人工确认）。我会用"真实进程冒烟 + 对真实后端的前端联调"提供证据，但不把它说成"你已验收"。
- 每处偏离旧行为的决定都要有 `BC-*` 与旧项目证据；旧项目找不到对应行为时先停下向操作者确认，不自行猜测。
- 回归基线（2026-09-28 实测）：`tests/run.py` 48/51 红——`core/test_index_progress`（间歇）、`official-gui-shell/tests/test_api.py`（22 报错）、`official-mcp-server/tests/test_tools.py`（4 失败）。

## 当前进度

- [x] C1 全部测试套件保持全绿——**2026-09-28 实测：修复前 48/51（3 套红，此前记录的 39/39 已失实）；修复后 `tests/run.py` 53/53 套通过、退出码 0**（日志 0 条 Traceback / 0 条 SyntaxWarning，回归后无遗留 WEMM/MinerU 服务进程）
- [x] C2 MCP 工具集对齐16个——全部16个 obsidian-rag 工具均已覆盖（list_libraries/get_selection/propose_selection_changes/apply_selection_changes/get_library_sample/propose_library_summary/apply_library_summary/note_relations/search_knowledge/reindex_knowledge/index_status/navigate_knowledge/read_document/find_duplicates/index_failures/wemm_status），另有 `export_library`/`import_library` 两个 rag-redo 独有额外工具。验证：`.venv/Scripts/python.exe -m unittest plugins.official-mcp-server.tests.test_tools -k test_tools_are_discoverable_with_schema` 通过。（注：同一文件里 4 个 `find_duplicates` 用例当前为红，见 C9）
- [x] C3 后台索引 + index_status——`reindex_knowledge` 已改为调用 `Pipeline.start_index_library()` 后台执行+立即返回，新增 `index_status(library_id)` 工具查询真实进度/心跳健康；底层 `core/index_progress.py` 真实抓到并修复两个并发bug（`Path.write_text`非原子导致读到截断JSON、Windows下`os.replace`撞上并发读句柄的瞬时`PermissionError`）。验证：`.venv/Scripts/python.exe -m unittest plugins.official-mcp-server.tests.test_tools -k index_status` 2 用例通过。
- [x] C4 wikilink 解析 + note_relations——新增 `core/note_relations.py`（逐字对齐 obsidian-rag `index.py::extract_wikilink_targets`/`resolve_note_relations` 的解析规则+"只存出链、入链现算"设计），`index_library()` 提取阶段顺手记录、新增 `note_relations` MCP 工具。验证：`.venv/Scripts/python.exe -m unittest plugins.official-mcp-server.tests.test_tools -k note_relations` 3 用例通过（含真实构造互链两篇笔记验证出链/入链）。
- [ ] C5 ROADMAP.md 文档闭环——`docs/ROADMAP.md` 已更新（A/B/C类清单逐条标注✅已完成/⬜仍未做+新增汇总段落），**等待操作者人工通读确认名实相符**（C5 本身要求人工确认，不由我自行勾选）
- [ ] C6 GUI 真的能启动（A1/A7/A16）——**自动化部分已通过**：`test_boot.py` 19 条全绿（真实运行时加载 20 个插件全 enabled、真实 `gui_main.main()` + 假 webview、致命退出非零码、三入口共用 `core.paths`）；本机真实进程冒烟通过（正常关窗后进程树 0 残留）。**2026-09-29 操作者真机反馈修复**：桌面双击后黑屏闪窗+反复弹窗（`nvidia-smi`/子进程 `Popen` 缺 Windows `CREATE_NO_WINDOW`）与窗口被 WEMM/MinerU-local 两个 subprocess_service 插件的 `enable()` 阻塞（现改为窗口先出、这两个插件后台追）均已修复并有真机 smoke + 新增回归测试，详见 `docs/ROADMAP.md` 对应日期小节。**窗口标题已由操作者定为 "Obsidian RAG 2.0"**（2026-09-29，冒烟命令同步改）。**未勾的原因**："人工确认（操作者）"一步只有操作者能做，我不代勾
- [x] C7 GUI 桥接契约真实对齐（A5/A6）——`test_contracts_parity.py` 全绿：37 个方法逐个真实调用对冻结夹具键集，含显示名 ≠ id 的库按名调用与未知名报错用例
- [x] C8 GUI 行为回归（A3/A4/A7/A8/A10/A12）——四个套件（`test_api`/`test_assets`/`test_selection`/`test_config`）全绿；①设置往返 ②5 文件夹选择树 ③进度随 `index_status` ④预览取消与超时 ⑤`index_stats.files` 口径（1 empty + 2 成功 → 3，`tests/test_pipeline_e2e.py`）均有用例
- [x] C9 `find_duplicates` 修复且不越权（A2）——`official-mcp-server/tests/test_tools.py` 全绿，含此前失败的 4 个用例与"两库 + 未授权 docx → docx 不出现"
- [x] C10 其余审计项（A9/A11/A14/A17/A19/A20/A21/A22）——契约门禁 11 项全绿（含"失效引用必须转红"自测）；`-W error` 编译全库 176 个 `.py` 0 失败；`stop` 注入 `PermissionError` 用例通过；A22 `unpack` 体积上限已加（阈值 8 GiB，待操作者确认后登记契约）；A13 不在本项范围，草案待操作者决定
- [x] C11 全量回归 + 按主题提交（A15）——`tests/run.py` 53/53、退出码 0；累积改动已按主题拆成多个提交（`git log e141ce4..HEAD`），敏感文件模式与凭据扫描无命中，`test_agents_contract` 通过（含 AGENTS.md/CLAUDE.md 同步），未 push
- [ ] C12 文档闭环（人工确认，我不代勾）
- [x] C13 A 索引顺序改回“先全部转换切块→连续做向量→一次写入”——`test_pipeline_e2e.py` 243 条 + `test_pipeline_data_safety.py` 94 条全绿；新增 4 条（全部提取完才有第一次向量化 / 跨文件连续调用 / 进度按块计 / 向量化中途失败不发布），改回旧实现时前三条转红；旧的“嵌入器调用 N 次”断言改为数被向量化的总块数（调用粒度是被有意改掉的结构）；GUI 进度条与 core percent 同步按阶段计（提交 b854016）
- [x] C14 B WEMM 交接前让文字模型让出显卡——`visual_index.index_library` 新增可选 `before_serve`，视觉插件仅在有页要渲染时调用；同时补上“页级索引失败记录每轮重试”（旧项目行为）；插件 4 条 + 主管道 2 条测试（提交 6abfce8）
- [x] C15 C 本机 MinerU 服务并发应答 + /health 秒回——进程内起服务端验证：解析卡住时 /health 1 秒内 200、探测不在请求线程、服务是 ThreadingHTTPServer；改回旧实现三条转红（提交 6475ea9）
- [x] C16 D 设置页补 MinerU API Key 等真实在用的键，云端读设置——新增 `official-ocr-mineru-cloud/tests/test_plugin.py`（14 条）+ 设置页往返/保密/枚举 + 主管道能力签名；验证命令里 C16 写的 test 文件路径 `test_plugin.py` 即该新建文件（提交 08d70de）
- [x] C17 E 水印页不再冒充文字层（BC-01 登记）——提取器 6 条 + 主管道 2 条；真实的 7 个 `empty` 文件复核全部转为 `scanned`；已知局限：单页水印扫描件无法识别（提交 d1cc38f）
- [ ] C18 关窗回收日志 + MinerU 单文件实测——关窗回收日志已做并有 4 条测试（提交 a101bfa）；MinerU 单文件实测已在本机跑出并写入 ROADMAP（5 页扫描件：冷态 57.9 秒、热态 3.3 秒，热态 MinerU 进程树平均约 6.3 个 CPU 核、显卡利用率均值 14%/峰值 48%）。**追加（第 7 条）**：追查中抓到真实机制——宿主异常没了时 WEMM/MinerU 服务孤儿会一直活着占显存（实测一个活了 29 分钟以上）；已加“宿主没了就自退出”兜底（BC-11，`RAG_REDO_PARENT_PID` + 每 2 秒检查；有 4 条测试，关掉监视后端到端用例转红；真实 MinerU 模型已装进显存时宿主被硬杀，进程树 12.4 秒内全部消失、显存回基线）。**未勾的原因**：数字只是这台机器一个样本，真伪由操作者复核，我不代勾；操作者当时那一次“关 GUI 后残留”的具体原因仍未复现
- [x] C19 全量回归 + 契约门禁 + 分次提交（不 push）——第一次全量 `tests/run.py` **55/55 套通过、退出码 0**（0 条 Traceback、0 条 SyntaxWarning）；加入“宿主没了子进程自退出”兜底后再跑一次 54/55：唯一红的是已知的间歇用例 `tests/test_subprocess_service.py::test_stop_actually_terminates_the_process_not_just_marks_it_gone`（杀掉子进程后立刻用 OpenProcess 查它是否消失，Windows 上进程已终止但只要还有句柄就仍能被打开；那次机器明显更慢，耗时约为第一次的两倍），单独重跑该用例 5/5 通过、整套重跑 3/3 通过，未改动它；契约门禁 11/11；分次提交见 `git log`；未 push；未提交 `data-real/`。工作区里另有 `README.md` 改动、`LICENSE`、`docs/DIAGRAMS.md`、`docs/media/` 未跟踪——都不是我改的/建的，未动未提交
