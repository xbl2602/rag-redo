# 当前目标

## 目标

**（当前，2026-09-28 锁定）** 先把 GUI 相关的问题全部搞定——GUI 必须能真正启动、能让操作者上手测试（最高优先级：GUI 起不来，操作者就没法做任何实际测试）；GUI 之后，把 2026-09-28 完整审计（`PROBLEMS_2026-09-28.md` 的 A1–A22）里其余可以解决的问题一次性全部解决，全程不回归、按主题提交、不 push。

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
  验证（本机真实进程冒烟，需图形会话 + WebView2）：`$env:RAG_REDO_DATA_ROOT = Join-Path $env:TEMP "rag-redo-smoke"; $p = Start-Process -PassThru -FilePath $py -ArgumentList "gui_main.py"; Start-Sleep 25; Get-Process | Where-Object { $_.MainWindowTitle -eq "RAG REDO" }; Stop-Process -Id $p.Id`
  预期：能查到标题为 `RAG REDO` 的窗口进程（venv 启动器会派生子进程，窗口在子进程上，需按标题查、按精确 PID 清理）。
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

## 范围

**做**（按顺序：先 GUI，再其余）：
- GUI：A1（插件 invalid）、A7（进度/推送/`bind_window`/窗口规格/设置分组）、A5（契约键集）、A6（显示名 vs id）、A4（设置保存）、A8（选择树）、A10（提取试验台）、A12（KPI 口径）、A3（`test_api.py` 迁移）、A16（`core.paths` 遮蔽）
- 其余：A2（`find_duplicates` 崩溃 + 越权）、A9（停止按钮间歇拒绝）、A11（CLI 缺 visual-wemm）、A14（门禁校验引用存在）、A17（SyntaxWarning）、A19/A20（测试进程与日志噪声）、A21（错误注释/docstring）、A22（`unpack` 体积上限 + 升级路径说明）
- 补"走真实入口"的门禁，杜绝"直接实例化 + 只 grep 文本"的假绿
- 按主题拆分提交现有未提交改动（不 push）；更新 ROADMAP 与 GOAL 进度

**不做（明确排除，需要时再单独提出）**：
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
- [ ] C6 GUI 真的能启动（A1/A7/A16）——**自动化部分已通过**：`test_boot.py` 17 条全绿（真实运行时加载 20 个插件全 enabled、真实 `gui_main.main()` + 假 webview、致命退出非零码、三入口共用 `core.paths`）；本机真实进程冒烟通过（6~8 秒出现窗口，标准错误为空，正常关窗后进程树 0 残留）。**未勾的原因**：①冒烟命令按窗口标题 "RAG REDO" 查找，而窗口规格沿用冻结夹具里旧项目的 "Obsidian RAG"，实测窗口标题是后者——二者不一致，待操作者决定（我不改验收标准）；②"人工确认（操作者）"一步只有操作者能做，我不代勾
- [x] C7 GUI 桥接契约真实对齐（A5/A6）——`test_contracts_parity.py` 全绿：37 个方法逐个真实调用对冻结夹具键集，含显示名 ≠ id 的库按名调用与未知名报错用例
- [x] C8 GUI 行为回归（A3/A4/A7/A8/A10/A12）——四个套件（`test_api`/`test_assets`/`test_selection`/`test_config`）全绿；①设置往返 ②5 文件夹选择树 ③进度随 `index_status` ④预览取消与超时 ⑤`index_stats.files` 口径（1 empty + 2 成功 → 3，`tests/test_pipeline_e2e.py`）均有用例
- [x] C9 `find_duplicates` 修复且不越权（A2）——`official-mcp-server/tests/test_tools.py` 全绿，含此前失败的 4 个用例与"两库 + 未授权 docx → docx 不出现"
- [x] C10 其余审计项（A9/A11/A14/A17/A19/A20/A21/A22）——契约门禁 11 项全绿（含"失效引用必须转红"自测）；`-W error` 编译全库 176 个 `.py` 0 失败；`stop` 注入 `PermissionError` 用例通过；A22 `unpack` 体积上限已加（阈值 8 GiB，待操作者确认后登记契约）；A13 不在本项范围，草案待操作者决定
- [x] C11 全量回归 + 按主题提交（A15）——`tests/run.py` 53/53、退出码 0；累积改动已按主题拆成多个提交（`git log e141ce4..HEAD`），敏感文件模式与凭据扫描无命中，`test_agents_contract` 通过（含 AGENTS.md/CLAUDE.md 同步），未 push
- [ ] C12 文档闭环（人工确认，我不代勾）
