# 当前目标

## 目标

把 2026-09-23 全面功能审计（对照 obsidian-rag 16个MCP工具 + 约60个CFG配置项）里列出的全部A/B/C类缺口实现完，让 rag-redo 达到与 obsidian-rag 的功能对等，同时全程保持 33+ 个既有测试套件不回归。

## 验收标准

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

## 范围

**做**：
- A类：`find_duplicates`、`wemm_status` 两个 MCP 工具封装
- B类：库内文件选择的 `get_selection`/`propose_selection_changes`/`apply_selection_changes`（复用 `core/write_gate.py`）、置信度低分标注、同篇结果封顶（`max_chunks_per_file`）、`small_to_big` 父块回填、MCP 进程单例守卫
- C类：`note_relations` + wikilink 解析、后台索引 + `index_status`、`read_document`、`advice.py` 思路的自适应建议系统、`index_failures`（简化版——rag-redo 没有 obsidian-rag 那套持久化 meta，会基于最近一次索引报告实现，不强求逐字节对齐）
- Graph 视图的**后端数据能力**（链接图+可选语义边，对齐 `guiweb/graph_data.py`/`semantic.py` 的纯函数部分）

**不做（明确排除，需要时再单独提出）**：
- D类 `tools/check_notes.py`（笔记命名规范检查，个人工作流工具，非通用RAG能力）
- HyDE 查询增强（已有独立追踪项，非本轮审计范围）
- Windows 安装包干净机器验证、打包体积优化、给冻结产物打包便携Python（打包相关，非"功能"缺口）
- 完整的图形化设置面板 UI、Graph 视图的完整 GUI 渲染面板（后端能力做完即算数，视觉呈现留到明确要求时再做，延续本项目"底层能力先完整、UI 刻意分阶段"的一贯做法——如果这条判断有误，请指出，我会调整范围重新锁定）

## 当前进度

- [ ] C1 全部测试套件保持全绿（持续验证项，每完成一个子任务后重跑；最近一次 `tests/run.py` 结果 36/36）
- [ ] C2 MCP 工具集对齐16个（当前覆盖11/16：list_libraries/get_library_sample/propose_library_summary/apply_library_summary/search_knowledge/reindex_knowledge/index_status/navigate_knowledge/wemm_status/read_document/find_duplicates；`export_library`/`import_library` 是 rag-redo 独有额外工具，不计入16个对齐范围。仍缺5个：`get_selection`/`propose_selection_changes`/`apply_selection_changes`（B类选择写权限门禁）、`note_relations`（依赖C4 wikilink解析）、`index_failures`（简化版）——以 `test_tools_are_discoverable_with_schema` 的实际断言集合为准）
- [x] C3 后台索引 + index_status——`reindex_knowledge` 已改为调用 `Pipeline.start_index_library()` 后台执行+立即返回，新增 `index_status(library_id)` 工具查询真实进度/心跳健康；底层 `core/index_progress.py` 真实抓到并修复两个并发bug（`Path.write_text`非原子导致读到截断JSON、Windows下`os.replace`撞上并发读句柄的瞬时`PermissionError`）。验证：`.venv/Scripts/python.exe -m unittest plugins.official-mcp-server.tests.test_tools -k index_status` 2 用例通过。
- [ ] C4 wikilink 解析 + note_relations
- [ ] C5 ROADMAP.md 文档闭环
