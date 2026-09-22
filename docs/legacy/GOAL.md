# 当前目标

## 目标
完整执行"库简介"功能：为 `list_libraries` 增加每库的导航性内容简介（不是逐字摘抄原文），支持用户手动编辑与 AI（GUI 按钮/对话中的 agent）生成两种写入路径，用户写入的内容受门禁保护不被静默覆盖，且生成动作只能被显式触发，绝不在索引流程中自动跑。

## 验收标准

- **C1 全量回归绿（含新测试套件，防修坏）**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; $out = .venv\Scripts\python tests\run.py 2>&1 | Out-String; if ($out -match '结果：(\d+)/(\d+) 套通过' -and $matches[1] -eq $matches[2] -and [int]$matches[2] -ge 16) { 'C1 PASS' } else { 'C1 FAIL'; exit 1 }
  ```
  预期：C1 PASS（全部套件通过，且套件总数比现状 15 至少多 1——新增的 `test_library_summary.py` 已编入 `tests/run.py` SUITES）。

- **C2 新测试套件独立跑通（风格对齐 audit_regression_test：标准库、逐用例 PASS/FAIL、`_run_all()`）**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python tests\test_library_summary.py; if ($?) { 'C2 PASS' } else { 'C2 FAIL'; exit 1 }
  ```
  预期：退出码 0。必须覆盖：①采样函数在假向量下选出正确数量的代表块；②内容指纹随文件变化正确翻转、不变则不变；③门禁——`source=none/ai` 直接写成功、`source=user` 无码拒绝/错码拒绝/过期拒绝/对码通过（结构照抄 `test_selection.py` 对 `selection_gate` 的测试）；④LLM 调用点用注入 fake `requests`（同 MinerU 云端测试手法），不打真实网络；⑤全程走 `_IsoEnv`，不碰真实 Chroma/真库。

- **C3 MCP 工具契约到位（agent 运行时只读 docstring，规范必须落在这里，不能只写在 AGENTS.md）**：
  ```powershell
  $pats = 'def get_library_sample','def propose_library_summary','def apply_library_summary','仅在用户明确要求'; $miss = $pats | Where-Object { -not (Select-String -Quiet -Pattern $_ server.py) }; if ($miss.Count -eq 0) { 'C3 PASS' } else { "C3 FAIL: missing $($miss -join ', ')"; exit 1 }
  ```
  预期：C3 PASS（三个新工具存在，且至少一处 docstring 含"仅在用户明确要求"这类触发纪律用语；`list_libraries` 需能展示简介/过期提示，人工过一遍输出确认）。

- **C4 配置与 GUI（guiweb）落地**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python -c "import sys; sys.path.insert(0,'.'); import config; errs=config.template_consistency_errors(); assert not errs, errs; assert 'library_summary_llm_url' in config.DEFAULTS and 'library_summary_llm_model' in config.DEFAULTS and 'library_summary_llm_api_key' in config.DEFAULTS; print('cfg-ok')"
  if ($?) { $hit = Get-ChildItem guiweb -Recurse -Include *.js,*.py,*.md | Select-String -Quiet -Pattern '刷新简介|library_summary'; if ($hit) { 'C4 PASS' } else { 'C4 FAIL: guiweb 无落地痕迹'; exit 1 } } else { 'C4 FAIL: config 未同步'; exit 1 }
  ```
  预期：C4 PASS（config.json 模板与 DEFAULTS 一致，新增本地/云端 LLM 三键仿 `hyde_*` 模式；guiweb 前端有"刷新简介"入口，`contracts.md`/`FEATURE_PARITY.md` 同步更新）。Flet `gui/` 本轮不做，仅确认其现状不受影响（`tests\test_config_editor.py` 仍需在 C1 里保持绿）。

- **C5 文档三处同步**：
  ```powershell
  $ok = (Select-String -Quiet -Pattern '库简介' AGENTS.md) -and (Select-String -Quiet -Pattern '库简介' TASK_LOG.md) -and (Select-String -Quiet -Pattern '库简介' TODO.md); if ($ok) { 'C5 PASS' } else { 'C5 FAIL'; exit 1 }
  ```
  预期：C5 PASS。AGENTS.md 新增一节须包含：导航/澄清性质定义、禁止逐字摘抄原文、长度上限（建议 100~300 字一段话不用 markdown）、不点名具体笔记细节、触发规则仅显式请求、门禁复用说明。

## 范围
- 做：`library.py` 新增 `summary` 字段（不进 `OVERRIDE_KEYS`）；`config.py` 新增 `library_summary_llm_url`/`_model`/`_api_key`（仿 `hyde_*`，本地免 key、云端带 key）；新文件 `library_summary.py`（Chroma 向量采样 + prompt 拼装 + 复用 `hyde_generate` 式 HTTP 调用）；新文件 `summary_gate.py`（仿 `selection_gate.py` 的提案号+确认码+TTL+一次性）；`server.py` 三个新 MCP 工具 + `list_libraries` 展示适配；guiweb 库卡片"编辑"/"刷新简介"入口 + 设置页新字段；AGENTS.md/TASK_LOG.md/TODO.md 三处文档；`tests/test_library_summary.py`
- 不做：Flet 旧版 `gui/` 的编辑入口（先跳过，之后再补，但不能被这次改动搞坏）；索引完成后自动触发生成（用户已明确要求禁止）；本地生成式 LLM 的模型下载/常驻管理（沿用 HyDE 现有的"用户自己在 LM Studio 之类跑好，程序只管调 HTTP"边界）；真实云端 API 冒烟（无 Key，需要用户提供后单独人工验证）；push（未经要求不 push）

## 注意事项
- 触发纪律是本次核心红线：生成/刷新只能由用户在 GUI 点按钮，或在对话里向 agent 明确提出请求触发；不得挂在 `index_library` 完成回调之后自动跑（用户已明确否决自动触发）
- 覆盖保护的边界：门禁只保护"AI 想覆盖用户已写内容"这一种场景；用户自己在 GUI 编辑框手动改并保存，不需要经过门禁，无条件生效
- 内容规范必须双落地：既在 AGENTS.md 写清楚，也要在 MCP 工具 docstring 里写（agent 运行时不读 AGENTS.md，只读 docstring）
- LLM 调用复用 `retriever.hyde_generate`（[retriever.py:503](retriever.py:503)）的 OpenAI 兼容 chat completions 调用方式，只是加一个可选 `api_key`；不要重新发明一套 HTTP 客户端
- API Key 属敏感值：错误消息只含类型与摘要，不得进日志（架构红线 8）
- 改完建议人工核对真实库 collection 数未增未减（当前基线 9 个），确认新测试全程走 `_IsoEnv` 没有误连真库

## 当前进度
- [x] C1 全量回归绿（16/16，含新测试套件）
- [x] C2 新测试套件独立跑通（test_library_summary.py 49/49）
- [x] C3 MCP 工具契约到位（get_library_sample/propose/apply_library_summary + 触发纪律用语）
- [x] C4 配置与 GUI（guiweb）落地（config 三键 + template 一致 + guiweb 编辑/刷新入口）
- [x] C5 文档三处同步（AGENTS.md/TASK_LOG.md/TODO.md 均含"库简介"）

## 上一目标存档（council 9 红灯 + MCP 调度，已完成）

> 以下为上一轮 GOAL.md 原文归档，验收标准保留备查。

### 目标
完整修复 council 审计确认的 9 个红灯问题，顺便解决多个 Agent 同时调用 MCP 时的调度问题和各类资源管理（模型单飞加载、GPU 占用型操作互斥、空闲卸载/驱逐让行在途任务、回收存活集完备）。

### 验收标准
- **C1 全量回归绿（防修坏）**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; $out = .venv\Scripts\python tests\run.py 2>&1 | Out-String; if ($out -match '结果：15/15 套通过') { 'C1 PASS' } else { 'C1 FAIL'; exit 1 }
  ```
- **C2 并发调度与资源管理回归套件绿**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python tests\test_mcp_scheduling.py; if ($?) { 'C2 PASS' } else { 'C2 FAIL'; exit 1 }
  ```
- **C3 真库零污染（9 个 collection 基线，无 `hidden-test` 残留）**：
  ```powershell
  $env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python -c "import sys; sys.path.insert(0,'.'); import chromadb; from index import CHROMA_DIR; names=sorted([c.name for c in chromadb.PersistentClient(path=str(CHROMA_DIR)).list_collections()]); print(names); assert len(names)==9 and not any('hidden-test' in n for n in names), names; print('C3 PASS')"
  ```
- **C4 文档与文案修复（B8/B9/过期注释）**。

### 当前进度（全部完成）
- [x] C1 全量回归绿（15/15）
- [x] C2 并发调度与资源管理回归套件绿（tests/test_mcp_scheduling.py 25/25，已编入 run.py SUITES）
- [x] C3 真库零污染（9 个 collection，无 hidden-test 残留）
- [x] C4 文档与文案修复（AI_GUIDE 补 guiweb；清掉过期文案与 1800 注释）

## 上一目标存档（R3a，已完成）

> 以下为更早一轮 GOAL.md 原文归档，验收标准保留备查。

### 目标
完成 R3a：扫描件 PDF 经 MinerU 云端 API 自动 OCR 入索引（R3b 本地部署搁置）；OCR 相关配置（含 API Key）可在 config.json 与 GUI 双端设置，天然后写覆盖。

### 当前进度（全部完成）
- [x] C1 配置层（三键入 DEFAULTS/模板/_POSITIVE_KEYS；默认 none 安全值）
- [x] C2 后端框架+客户端（mock HTTP 全覆盖：happy/no-key/fail-fold/缓存路由维度）
- [x] C3 xsrc 重试语义（签名失配穿透快速路径，转正后幂等；门禁冻结优先级不变）
- [x] C4 GUI 设置组（config_editor GROUPS「扫描件 OCR」三键；test_config_editor 守护通过）
- [x] C5 回归+文档（六件套全绿；TASK_LOG 问题 26、AI_GUIDE MinerU 段、TODO R3a 标记）

### 上一目标完成记录（2026-08-24）
人机分权门禁已交付（d02bda0）：默认多格式开启、agent_formats 门禁+GUI 开关（7b9ecd0 合并单开关）、格式撤销/删除语义回归（eca3918）。六件套全绿。
