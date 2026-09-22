# TODO — 多格式文档支持（Word + 文字层 PDF 优先，扫描件 OCR 末轮）

> 状态：方案已定稿（v2，经 council 三委员评审修订），分三轮交付。
> 评审记录：`.council-state/round-plan-1/`（8 个 blocker 已全部吸收进本清单）。
> 进度（2026-08-24）：**R1、R2 已完成并提交**（R1=177ede6，六件套全绿；R2=2ac21b5），
> 明细见 TASK_LOG.md 问题 23/24。当前待办 = **R3：扫描件 OCR** + 用户确认开启真实 vault 的 extensions。
> 历史备注：优先级修订时基线为 92af6e6、META_VERSION 8；现已升到 9 并完成一次真库重建。
>
> ✅ **2026-09-12 问题56（模型冷加载提速）已完成**：离线优先加载（跳 HF 联网核对，
> 冷加载 ~4 倍快）+ 空闲卸载可配（`gpu_idle_unload_seconds`，默认 300s/5min，
> 0=常驻）+ 重排器 fp16（阈值重测无变化）+ 清 bge-m3 孤儿快照 2.17GB。
> 同日问题57：显存管理补强（建索引先放 reranker、空闲卸载加"运行中跳过"护栏）+
> 修复 `config_editor` 行内注释吞逗号导致 config.json 静默失效的 bug。详见 TASK_LOG.md。
> 同日问题58：BGE↔WEMM 交接收紧——建索引开跑前主动 evict WEMM（BGE 阶段不用它）、
> WEMM 阶段改为"真要渲染才放 BGE"（before_serve 回调），纯笔记增量不白放。
> 同日问题59（GOAL：完整修复 9 红灯 + 多 Agent 并发调度与资源管理）：`gpu_arbiter.GPU_LOCK`
> 全局互斥（RLock，驻留变更全持锁、查询不持、navigate 15s 拿不到走降级）；get_model 单飞+
> 释放/切回/降级全进锁；navigate 索引互斥 veto；idle 守护检查-释放原子化；B1 损坏缓存回退+
> 清理指引；B4 探针 fail-open；B5 prune 补 base；B7 mineru 看在途 + evict 锁序；B2 CLI 警示+
> 文档；B8/B9 文案。`tests/test_mcp_scheduling.py` 25/25（已编入 run.py SUITES，全量 15/15）。

## 交付轮次

| 轮次 | 状态 | 范围 | 出口标准 |
|---|---|---|---|
| **R1（最小可交付）** | ✅ 完成 | CLI 全链路 + **DOCX + 文字层 PDF** | `library.py` 开 extensions → `index.py --library` 端到端索引 md/docx/文字层pdf；扫描件 pdf 干净跳过（终态防死循环）；六件套回归全绿 |
| **R2** | ✅ 完成 | **GUI 适配** | 设置页/进度展示对多格式的完整体验 |
| **R3（末轮）** | ⬜ 待启动 | **扫描件 OCR：MinerU 云端 API → 本地部署** | 云端路径可用后接本地 pipeline 联调，xsrc 自愈验证 |

### R3 启动前的前置确认
- [x] ~~用户决定是否现在给真实 vault 开启 extensions~~ ✅ 已按用户决定落地（2026-08-24，问题 25）：
  **默认开启** md/pdf/docx；GUI 库配置改为勾选块；新增 `agent_formats` 人机分权门禁——
  Agent 触发的索引/自动同步仅限文本类+已批准格式，批准一次长期有效、可随时撤销
- [ ] MinerU-Open-CLI 账号/Token 准备（flash-extract 免注册 ≤10MB/20页；extract 注册 Token 200MB/200页）

> ✅ **R3a 已完成（2026-08-24，问题 26）**：扫描件 OCR 接入 MinerU 云端 HTTP API
> （直连 mineru.net，无需安装 CLI）。`pdf_scan_backend`（none/mineru-cloud）+
> `mineru_api_key` + `mineru_timeout_seconds` 三键进 config 与 GUI 设置页，
> 双端同文件后写覆盖。终态条目带 xsrc 能力签名：启用后端/补 Key 后存量 scanned
> 自动重试转正，无需 --full。缓存键升级 `<md5>.<route>.v2`。R3b 本地部署搁置。
>
> ✅ **2026-08-26 已修复（问题30）：冒烟验证发现真实 bug，此前从未真正跑通过云端 API**。
> 用户配好 `mineru_api_key` 后首次真实冒烟，发现 `_mineru_cloud_extract` 里硬编码的两个
> 接口路径是错的：提交用的 `{_MINERU_BASE}/file-protocol/batch`、轮询用的
> `{_MINERU_BASE}/file-protocol/batch/{batch_id}`，实测均返回 HTTP 404（服务器层面路由不存在，
> 不是鉴权/参数错误——响应体是纯文本 `404 page not found`，不是 JSON）。查官方文档
> （https://mineru.net/apiManage/docs）确认正确路径应为：提交 `POST {_MINERU_BASE}/file-urls/batch`、
> 轮询 `GET {_MINERU_BASE}/extract-results/batch/{batch_id}`（请求/响应体字段名本身没错，
> 只是 URL 路径错）。因为现有单测（test_extractors.py）全部用 mock 的 `requests` 模块，
> mock 只会验证代码怎么调用、不会验证真实 URL 是否存在，所以这个 bug 完全没被六件套挡住。
> 后果：`pdf_scan_backend=mineru-cloud` 这个功能自 R3a 上线以来，任何真实调用都会 404，
> 被 `_mineru_cloud_extract` 的异常折叠机制吞掉、折成 `extract-failed`/`scanned` 终态，
> 表现为"静默跳过"而不是报错——不会崩溃，但从未真正 OCR 成功过一次。
> **已修复：两处 URL 字符串改正，`_mineru_cloud_extract` 新增必填 `is_ocr` 参数，并新增
> 显式断言实际 URL 字符串的回归测试（防止 mock 再次掩盖同类问题）**，与线 A（`pdf_text_backend`
> 新开关）合并一轮做完，详见 TASK_LOG.md 问题30。诊断脚本与实测产物见
> `scratchpad/mineru_probe.py` / `scratchpad/mineru_probe_out/`（未提交，仅诊断用）。
>
> ✅ 顺带确认了本来要等这次冒烟才能验证的问题：**`is_ocr=False` 时图片确实还会被提取**。
> 拿一份自制的「真文字层 + 1张内嵌图」PDF 用修正后的路径实测，返回的 zip 里：
> `full.md`（正文，且已经自动带 `![](images/xxx.jpg)` 图片引用，位置就在原文中图片所在处）、
> `images/xxx.jpg`（抠出来的图，验证内容一致）、`*_content_list_v2.json`（分块结构化数据，
> 见下方线B条目）、`layout.json`、`*_model.json`、`*_origin.pdf`。证实版面检测/图片裁切
> 确实不依赖 OCR 开关，之前的社区信源交叉印证是对的。
>
> ✅ **2026-09-02 已修复（问题33）：请求体从未传 `model_version`，一直在用服务端未指定
> 时的默认版本**。通读官方 API 文档发现的第二个隐蔽遗漏（性质与问题30不同——问题30是
> 404 会整体失效且容易被察觉，这次是"能用但一直没显式要求用更准的 vlm 模式"，请求正常
> 返回 200，不会以任何错误形式暴露）。**已修复：新增 `config.mineru_model_version`
> （默认 vlm，可切 pipeline），请求体新增该字段，`EXTRACT_VERSION` 2→3 使旧缓存整体失效
> 重提**。顺带把 `pdf_text_backend` 补齐了 `mineru-local` 占位选项（此前只有扫描件分支
> 有这个占位值，文字层分支没有——而课件大多是有文字层的，这条路径更常用）；选中后安全
> 退化为本地直提，不装任何模型、不改变现有产出，只是把入口先占好，详见 TASK_LOG.md 问题33。
>
> ✅ **2026-09-03 已完成（问题34）：混合型 PDF 整本按扫描件路由**。此前 `_extract_pdf`
> 用「文字层页占比 < 0.5 → 整本扫描件」的整本二分，对「PPT 原生文字 + 教材扫描图」混装
> 课件两个方向翻车：占比低的整本零内容（ManometerEquation 型）；占比高的走文字层直提、
> 图片页内容静默丢失且提取"成功"无异常信号（Note9 型，全部例题页丢失）。**新规则：
> 逐页检测（单页 ≥10 字符算文字页），只要存在任何图片页，整本按 pdf_scan_backend 路由**
> ——mineru-cloud 时整本 is_ocr=True 送云端 vlm 认字（一份连贯完整 MD，不做逐页拆分拼接）；
> 其余取值整本落 scanned 终态待 xsrc 自愈（宁可诚实空缺，不留半份）。顺带把扫描分支收拢
> 为「只有 mineru-cloud 送云端」，封死未知取值 fall-through 进云端分支的口子。
> `EXTRACT_VERSION` 3→4。六件套全绿（extractors 56/56，含补跑问题33 未在本机验证的 7 例）。
> 同轮决策：**read_document MCP 工具暂缓不做**（见 Backlog 暂缓条目），详见 TASK_LOG.md 问题34。
>
> ✅ **2026-09-03 已完成（问题37）：WEMM 页级视觉导航 + read_document + 近似文档去重**。
> 新增 `wemm_server.py`（全局 Python 本地看图服务，端口 9101，WeMM-Embedding-2B 512 维）、
> `wemm_indexer.py`（逐页渲染→页向量，独立 `wemm_<collection>` 页库 + 独立 meta/版本号，带
> 门禁/终态/自愈/精确清理）、`wemm_retriever.py`（页级导航检索)、`dedup.py`（文本级
> MinHash+LSH 近似去重，只读建议）。MCP 新增三工具：`navigate_knowledge`（页级视觉导航）、
> `read_document`（取完整正文+绝对路径，零触发只读缓存）、`find_duplicates`（近似去重）。
> 默认关闭（`wemm_backend=off`，隐私/显存优先）。三处 config 同步；新增 58 用例
> （wemm_indexer 26、wemm_retriever 13、dedup 19）全绿，六件套全绿。详见 TASK_LOG.md 问题37。
>
> ✅ **2026-09-04 已完成（问题38）：失败溯源 + WEMM 可确认手段 + 渲染 DPI 档位 + 真实全量建库验证**。
> 新增 `index_failures(library)`（把提取静默失败变成可溯源清单，按终态原因分组、标注 `〆 下轮将
> 自动重试`）与 `wemm_status()`（每库 PDF 数/页向量/后端开关/服务存活/渲染失败，一眼确认 WEMM
> 是否生效）两个 MCP 工具；`navigate_knowledge`/`wemm_status` 改调用时现读 config
> （长驻 MCP server 的 CFG 是 import 快照，中途开关/改 DPI 旧进程读不到）。新增 `wemm_render_dpi`
> 设置（40/60/90/120，默认 60，改后需 `--full` 重建；单页嵌入耗时随 DPI 强相关）。真实全量建库
> 实证：LECTURE NOTE 382 页向量 / 18 PDF，`navigate_knowledge` 返回真实命中页。另修复 test_extractors
> 配置隔离（真实 config.json 开了 mineru-cloud → 一次挂 33 例，改成跑测前把 OCR 键重置为
> DEFAULTS、测完还原，71/71 稳定）。详见 TASK_LOG.md 问题38。
>
> ✅ **2026-09-04 已完成（问题39）：全面质量审查修复轮**（用户委托另一 agent 完成问题37/38
> 后，由审查 agent 复查 + 本轮修复）。修复清单：①**WEMM 失败终态死寂（P0）**——此前"记入
> 终态待重试"是假承诺，size+mtime 快速路径对终态条目照跳，服务抖动一次该 PDF 永久退出页级
> 导航；现在终态带 `xsrc=wemm:<模型>:<维度>:<DPI>` 签名、快速路径跳过终态条目，失败每轮真重试，
> 且改 DPI/换模型自动重渲染（不再依赖人工 --full）；②**写库假账防护**——upsert 分批（1000/批）
> 且全部批次成功才把成功条目并入 meta，杜绝"meta 记了页数、库没写上"触发整库重编码死循环；
> ③**显存管理**——wemm_server 改懒加载（启动不进显存，首个请求才加载）+ `--unload-after`
> 空闲卸载改后台守护线程驱动（原实现只在有新请求时检查，而空闲恰无请求，等于永不卸载）+
> health 不再持模型锁（索引进行中不再误报"服务不可用"）+ dtype 兼容新旧 transformers；④
> **配置热读补全**——新增 `config.reload_config()`，ensure_fresh/reindex_knowledge/导航入口
> 统一现读，修掉"外层现读放行、内层旧快照拒绝"的自相矛盾，reindex 也能看到中途补的 OCR Key；
> ⑤**navigate_knowledge 库范围语义对齐 search_knowledge**（空=默认库、all−exclude，此前静默
> 搜全部库）+ 修提示死循环（此前指路 reindex_knowledge 建 WEMM 页库，而它根本不建）；⑥
> **read_document 补齐存档设计**——抬头加字数/产出方式（缓存命中现返回产出路由），正文不再
> 截断；⑦ index_failures 重试判定与 index._backend_changed 对齐、空串原因不再从报告消失；
> ⑧ dedup bottom-k 估计量按并集第 k 小值截断分子（修边界假阳性）；⑨ 页级检索零写副作用
> （get_collection）+ 逐库错误汇总不再静默；⑩ wemm_server HTTP keep-alive 请求体消费修复。
> 新增 5 用例（wemm 36、dedup 23、extractors 72），九件套全绿。详见 TASK_LOG.md 问题39。
>
> ✅ **2026-09-04 已完成（问题41）：GPU 显存仲裁**（用户拍板「WEMM 默认开、按需拉起、
> 用完自动关、与其他模型互斥在线」）。新增 `gpu_arbiter.py`：WEMM 服务按需自动拉起
> （幂等、PID/日志落盘）+ 空闲 5 分钟卸显存、30 分钟自退出 + 显存互斥（WEMM 加载前等
> ≥5.5GB；bge-m3 加载前不足则 evict WEMM；server 空闲 10 分钟自动卸模型）；检索优先、
> fail-open（探测失败绝不阻塞）。`wemm_backend` 默认 on，新增 `wemm_python` 配置。
> 冒烟遗留的旧 wemm_server（占 5.09GB 显存两天）已清理。test_gpu_arbiter 28 例，
> 十件套全绿。详见 TASK_LOG.md 问题41。>
> ✅ **同轮追加：建页库接入索引管线**——增量/全量重建完成后自动同步 WEMM 页库
> （`index_library` → `_wemm_auto_phase`：off 零开销跳过、先释放本进程 bge-m3 让路、
> 服务懒拉起——无变更轮次零拉起；异常只记日志绝不波及文字索引）。命令行
> `wemm_indexer.py` 保留为手动立即建库入口。wemm_indexer 47 例。
>
> ✅ **2026-09-04 已完成（问题40）：GUI「文件生效明细」面板**（用户要求：确认 WEMM/MinerU
> 生效要能逐文件看，不能只给数字）。主界面工具栏新增入口，对话框按库展示两区：文字索引区
> 逐文件列失败/跳过原因与下轮自动重试标注（判定与 index._backend_changed 同谓词）；
> WEMM 区逐 PDF 列页向量数/渲染失败原因 + 服务存活探测（回环只读），**点行直接打开那份
> PDF** 配合导航返回的页码人工核对内容。store 层新增 4 个零侵入只读函数；test_gui_store
> +5 用例（55）。详见 TASK_LOG.md 问题40。

## 核心思想

索引时把非 MD 文件转成 Markdown，复用既有整条切块管线；源文件零写入；
一切「不产块的文件」必须落持久化终态防重建死循环。

## 架构 v2 关键机制（实施时不得偏离）

### A. 统一终态机制（封堵五个死循环入口）
- `_load_text(fpath)` → `(text | None, 字节hash | "unreadable")`：
  - 空串/纯空白归一为 None（堵空产出入口）
  - OSError 不上抛，hash 用哨兵值 `"unreadable"`（锁定/OneDrive/AV 场景两轮哨兵等值 → 自动判稳；解锁后真实 hash ≠ 哨兵 → 自动重试）
  - 后缀路由一律 `suffix.lower()`（堵 `.PDF`/`.MD` 静默 xfail）
- `_index_core` 分支顺序：**None 判定严格先于 is_tbd_heavy**（否则 content=None 炸 AttributeError 废整轮）
- 非正常路径统一出口：
  ```python
  meta[rel] = {"hash", "chunks":0, "size", "mtime",
               "xfail":True,
               "reason":"unreadable|extract-failed|empty|tbd|scanned"}
               # scanned = 扫描件暂不支持（R1），R3 接入后端后自动重试转正
               # 且必须 current_rels.add(rel) 使条目持久化
  ```
- kb_stale 对二进制源：`extract=False` 拿字节哈希比对即可判「真没变」，绝不跑转换（GUI 每秒轮询的零成本边界）；entry 为 None 的失败新文件照常计 added 一次 → 触发一次重建落终态 → 此后稳定收敛

### B. 单一事实来源与谓词收拢
- extractors.py 导出 `TEXT_EXTS={md,txt}`、`BINARY_EXTS={pdf,docx}`、`SUPPORTED_EXTS`
- index 的 TEXT_SOURCE_EXTS 与 library 的白名单校验都引用它，禁止三处硬编码
- 单点谓词 `_skipped(info): bool(info.get("tbd") or info.get("xfail"))` 收拢四处判断

### C. 预留给 R3 的机制（R1 不实现，仅留口）
- 终态条目 `xsrc` 字段 + kb_stale 失配自动重试（换后端无需 --full）
- 缓存键 backend 维度：R1 用 `<md5>.v<EXTRACT_VERSION>`，R3 升级为 `<md5>.<backend>.v<N>`
- 子进程卫生全套（树杀/临时目录/UTF-8 解码/超时配置）

---

## R1 — 当前焦点：CLI 全链路 + DOCX + 文字层 PDF（无 OCR、零新增配置键）

> ✅ **已完成（2026-08-24）**：全部条目落地并通过六件套回归（test_extractors 16/16 新增；
> 其余五件全绿）。明细与实测数字见 TASK_LOG.md 问题 23。扫描件处理方式有微调：
> 提取入口返回 `(md, reason)` 二元组以支撑终态分类；空正文 md 一并落 empty 终态
> （顺带收敛了「明确不做」里的备案隐患）。

- [x] **1. requirements.txt + 安装验证**：pymupdf / pymupdf4llm / python-docx（Py3.14 轮子验证，lxml 是唯一硬风险；失败即停回报，退路=docx 优雅降级）。版本号安装成功后回填锁定
- [x] **2. extractors.py**（新文件，只依赖 config.CFG，无子进程、无后端分发）：
  - [x] `extract_to_markdown(path) -> str|None`：唯一入口，绝不抛异常、绝不写源目录
  - [x] DOCX 路：python-docx 按 body 子元素保序遍历；Heading N/标题 N 样式→#×N（>3 钳 ###，兜底 base_style）；表格→管道表（\| 转义、单元格换行→空格）
  - [x] PDF 路：fitz 探测文字层覆盖率（≥0.5 页占比）→ pymupdf4llm.to_markdown()（list 返回值 join 归一）；**扫描件为主 → 返回 None + warn_once(说明 R3 前暂不支持)，由 index 记 reason="scanned" 终态**
  - [x] 缓存层：键=`<md5>.v<EXTRACT_VERSION>`；原子写 tmp 带 pid + os.replace；**写失败仅跳过缓存照常返回结果**；None 不写缓存；启动清扫 >24h 孤儿 *.tmp；目录可注入参数覆盖（测试隔离）
  - [x] 懒加载 import（pymupdf/python-docx 未装时对应格式优雅降级 None + 安装提示）
- [x] **3. index.py**：
  - [x] META_VERSION 8→9（v9 注释：多格式提取+原始字节指纹）
  - [x] `_load_text()` 统一入口（终态机制 A 全部语义在此）替换 kb_stale/_index_core 两处裸 read_text
  - [x] kb_stale：二进制源走字节哈希快速比对；removed/expected 排除口径改用 `_skipped()`
  - [x] _index_core 主循环重排（None→tbd→哈希短路→终态落盘→正常切块）；frontmatter 仅对 TEXT_EXTS 生效
  - [x] 提取前 update_progress(phase="converting")；progress_text 加 converting 豁免分支
  - [x] `__main__` 多库循环逐库 try/except（一库失败不连坐）
  - [x] 单点谓词 `_skipped(info)`
- [x] **4. library.py**：set_config extensions 白名单校验引用 SUPPORTED_EXTS；小写归一去重保序
- [x] **5. retriever.py**：零改动（已核实块元数据链路天然兼容）
- [x] **6. tools/check_notes.py**：scan_library 对非 TEXT_EXTS 跳过正文分析（修二进制 UnicodeDecodeError 崩溃）
- [x] **7. tests/test_extractors.py**（新文件，风格对齐 audit_regression_test.py 非 pytest）：
  - [x] docx 回环（标题层级/管道表/正文）；pdf 文字层生成提取
  - [x] 大写扩展名 `.PDF`/`.Docx` 路由正确（红队 B3 回归）
  - [x] 伪造二进制→None 无堆栈；空 docx→None 走 xfail
  - [x] 缓存命中计数不增；None 不写缓存；缓存写失败不影响返回值
  - [x] xfail 防死循环全序列：坏 pdf→meta 有 chunks:0/xfail→第二遍零变更→修复+mtime 变化→第三遍出块；哨兵 unreadable 两轮判稳
  - [x] TBD 重 pdf → 终态条目 reason=tbd → 不再触发重建（红队 B1 回归）
  - [x] 扫描件 pdf（无文字层）→ 终态 reason=scanned → 不触发重复重建
  - [x] 源目录零写入快照断言
  - [x] 静态断言：kb_stale/_index_core 含 _load_text 不含裸 fpath.read_text
- [x] **8. tests/verify_export_import.py L149-151**：rglob("*.md") 计数 → manifest vault_files[].rel 权威清单集合比对
- [x] **9. R1 回归出口**：六件套全绿（audit 19/19 / library_registry 14/14 / server_singleton 5/5 / test_config_editor / test_gui_store / verify_export_import）
- [x] **10. R1 文档**：TASK_LOG.md 追加条目；AI_GUIDE.md 注明「扫描件 pdf 暂不支持，计划末轮接入 MinerU」

### R1 用户侧启用（零额外安装）

```powershell
python library.py config "Obsidian Vault" --set extensions=md,pdf,docx
python index.py --library "Obsidian Vault"
```

---

## R2 — GUI 适配

> ✅ **已完成（2026-08-24）**：converting 相位展示+双看门狗口径对齐、xfail 终态可见性（状态卡汇总+ISSUE_TEXT 指引）、extensions 字段格式提示；test_gui_store 增至 30 用例全过，六件套全绿。明细见 TASK_LOG.md 问题 24。

- [x] gui/widgets.py 库设置页：extensions 字段旁标注支持的格式（md/txt/pdf/docx）与扫描件暂不支持的提示
- [x] 索引进度：converting 相位在 GUI 进度区的展示；heartbeat_state 豁免与 server 侧 progress_text 对齐（双看门狗一致，防一边正常一边弹卡死）
- [x] 提取失败文件的 GUI 可见性：列表/统计中区分 xfail 条目（含 reason），给出处置指引
- [x] TASK_LOG 追加 R2 记录

---

## R3（末轮）— 扫描件 OCR：MinerU 云端 API → 本地部署

### 3a. 云端 API（先做）
- [ ] MinerU-Open-CLI 接入：flash-extract 免注册（≤10MB/20页）；extract 免费注册 Token（200MB/200页）
- [ ] config.py 四键（DEFAULTS+CONFIG_TEMPLATE 同步）：`pdf_scan_backend`（默认 mineru-cloud）/ `mineru_cloud_cmd` / `mineru_local_cmd` / `mineru_timeout_seconds`（进 _POSITIVE_KEYS）
- [x] gui/config_editor.py GROUPS 加「PDF/OCR 提取」组收编四键（缺这步 test_config_editor 必红）✅ 已随问题30落地（组名定为「PDF 提取后端」，含 `pdf_text_backend`）
- [x] 设置页可用性重构（2026-08-26，TASK_LOG 问题 31）：左导航分「常用/开发者」两小节 + 单组详情面板；枚举字段改下拉（pdf_scan_backend/pdf_text_backend）、布尔改开关、模型字段加推荐候选芯片；每字段带一句话说明与 ⟳ 需重建标记；API Key 密码框遮显；打开时从 config.json 刷新回显
- [ ] 后端分发框架 + 子进程卫生全套：列表参数禁 shell=True；UTF-8 显式解码；超时可配；超时 taskkill /T /F 树杀+proc.wait() 收尸；CREATE_NO_WINDOW；输出目录=data/ocr_cache/tmp-<pid>-<ts>/，rglob("*.md") 取最大者，try/finally 清理
- [ ] 终态条目引入 xsrc 字段 + kb_stale 失配自动重试；缓存键升级 `<md5>.<backend>.v<N>`
- [ ] AI_GUIDE 补「MinerU-Open-CLI 安装」段
- [ ] 测试：reason=scanned 的存量终态条目在接入后端后自动重试转正的端到端用例

### 3b. 本地部署联调

> 2026-09-02（问题33）：`pdf_text_backend` 新增了 `mineru-local` 占位值（与
> `pdf_scan_backend` 的既有占位值对齐），选中后安全退化为本地直提、不装任何模型——
> 这只是把配置入口和路由分支先占好，下面几条"真正跑通本地部署"的待办一条都没有
> 因此被完成，不要误读为已实测。

- [ ] 本机安装：Python 3.12 独立 venv + `uv pip install "mineru[pipeline]"`（~2-3GB，纯 CPU 不占显存；模型首跑下载 ~870MB）
- [ ] mineru-local 实测：真实扫描件跑通；核对输出目录旗标、退出码语义、树杀有效性、UTF-8 输出
- [ ] timeout 实测校准；性能记录（页级耗时、内存峰值、与 bge-m3 编码并发的资源互扰观察）
- [ ] xsrc 自愈端到端验证：cloud 索引 → 切 local → 自动重试且缓存键正确失效
- [ ] TASK_LOG 追加 R3 记录；评估是否翻转默认 backend 为 mineru-local（隐私优先）

---

## 索引进度看板：停滞宽限机制（2026-08-26，问题32）

> ✅ 已完成。模型加载/等写锁/写库等合法长静默不再被「进度停滞」看门狗误报；
> 心跳停止（真死）判定永远优先，宽限绝不掩盖。明细见 TASK_LOG.md 问题 32。

- [x] index.py：`stall_grace_until` 自过期宽限字段（绝对时间戳）+ `stall_grace_s` 写入 kwarg（永不落盘）；默认清除 + max 合并 + clamp≤600 + fail-closed；`_stall_grace` 两段式守卫助手（running+pid 双检，防死锁/防复活残留文件）
- [x] 四埋点：get_model cuda/cpu 冷加载、_try_switch_back_cuda 入口、fallback_to_cpu 收尾、waiting-lock→write_lock→writing；G7 单点还原 converting→scanning（三出口全覆盖）
- [x] 判定侧双实现镜像：index.progress_text 三态分支重排 + gui/store.heartbeat_state 宽限改判 running；新增 `heartbeat_note` 纯函数（DEAD-first 短路）；GUI KPI/waiting-lock 中文映射；stall_timeout 设置页 hint 补宽限说明
- [x] 测试：audit +16（C2 复现用例 + C3 六条 CUDA 用例）、test_extractors +1（G7）、test_gui_store +4（含双看门狗一致性）；六件套全绿，verify_export_import 39/39

---

## 明确不做（备案）

- ~~既有 md 空正文守卫不落 meta 的隐患~~ ✅ 已随 R1 终态机制收敛（空文件落 `empty` 终态，TASK_LOG 问题 23）
- GUI/server tbd_ratio 口径不一致（gui/store.py:69 不传 ratio，既有偏差）
- OCR 结果页级进度 tick（个人库规模下 converting 相位豁免双看门狗已够）
- pymupdf4llm 对恶意 pdf 的解析隔离（单用户本地威胁模型，风险可接受）

## Backlog（按需触发，当前不排期）

- ~~**read_document MCP 工具（按文件读取完整正文）**~~ ✅ **已完成（2026-09-03，问题37 随 WEMM
  落地，实现方式与原设计一致）**：背景是纯语言模型消费路径（检索导航 → 整篇读取转写稿）缺
  一个"按文件取完整 MD"的 MCP 工具（调研纪要 14.6 节缺口）。当时因用户主力模型已具视觉、
  纯语言路径暂无消费者而暂缓；本次随视觉导航一并做了。**实现即按原设计**：入参对齐
  note_relations（库内相对路径或不含扩展名标题 + 单库）；门禁 = 只能读 meta 中已存在的
  （用户已授权索引的）文件，AI 无法借它绕过授权触发云端提取；md/txt 直读源、pdf/docx 走
  **提取缓存（零触发）**；拒绝跳出库目录的路径写法；返回带文件名/字数/产出方式抬头、
  不截断全文。详见 TASK_LOG.md 问题37。
  > ✅ **2026-09-04 补记（问题39）**：问题37 实现时抬头缺字数/产出方式且正文超 2 万字符
  > 截断，与存档设计不符，问题39 已补齐。另：原"暂缓"决定由用户在 2026-09-04 质量检查轮
  > 委托审查方自行处置待办（"针对代办方案，自行决定如何操作"），本工具予以保留；若要
  > 下线，删 server.py 的 `read_document`/`_read_source_text`/`_ROUTE_LABELS` 即可，
  > `extractors.read_cached_markdown` 仍被 dedup 复用需保留。

- ~~**PDF 本地/云端后端开关挂错分支**~~（2026-08-26 讨论发现）✅ **已完成（2026-08-25/26
  讨论，问题30 已实现）**：现在 `pdf_scan_backend`
  （config.py DEFAULTS）只在 `extractors._extract_pdf` 判定"扫描件"（文字层页占比<0.5）
  的分支上生效；有文字层的正常 PDF 分支完全写死走本地 `pymupdf4llm`，没有任何开关。但
  扫描件分支本地是**零处理能力**（读不出字），本地/云端并非两个可比选项，`pdf_scan_backend`
  实质是"要不要为唯一能用的路径（MinerU）付费"的成本开关；真正存在"本地能用、云端更准"这种
  质量/成本权衡的，反而是现在没有开关的文字层分支——MinerU 结构识别（标题/表格/版面）更准，
  用户可能想为质量付费，现在没有这个选项。触发条件：与下一条「图片/图表语义内容缺失」共同
  推进时一并解决（该功能要覆盖数字件 PDF 就必须先有这个开关）；也可独立先做，价值不依赖图片
  功能。方案方向：文字层分支新增独立开关（暂拟名 `pdf_text_backend`，local/mineru-cloud），
  不复用 `pdf_scan_backend`（语义不同，别混一个键里）。

  **落地摘要（2026-08-26，问题30）**：按上面的方案方向原样实现——新增 `pdf_text_backend`
  （local/mineru-cloud，默认 local）独立开关，`_extract_pdf` 两个分支各自独立 resolve
  自己的配置键（不再共用一个提前 resolve 的 `backend` 变量）；送云端时 `is_ocr=False`，
  缓存路由新增独立标签 `mineru-text`。实现中额外发现并修正了缓存路由候选列表的一个隐藏
  耦合（`local`/`mineru-text` 不再满足"同文件只有一条路由能成功产出"的旧假设，需要按当前
  配置只查其一，否则切换后端会被另一路由的历史缓存假命中）；详见 TASK_LOG.md 问题30。

  **2026-08-26 用户决定：本轮（今天）只落地这一条 + 上面「R3a」条目下记录的 URL bug 修复，
  两个一起改（反正都碰 `_mineru_cloud_extract`）。线B/线C 今天不动代码，只把资讯/决策/讨论
  记录完整存在这份文档里，留到以后单独开轮再看。**

  **为线B/线C 预留的架构留白（不提前实现，只是今天写代码时别把路堵死）**：
  - `_mineru_cloud_extract` 现在的写法是拿到 zip 后只挑最大的 `.md` 读出来，其余成员在函数
    内就地丢弃，只返回 `(md, reason)` 两元组。线A 不需要改这个取舍（线A 也只要 md）,但实现时
    不要把"提前关闭/丢弃 zip 对象"这类写法做得难以扩展——线B 以后要读 `images/*.jpg`、线C
    以后要读 `*_content_list_v2.json`，这两样今天已经实测确认会出现在同一个 zip 里（见上面
    R3a 条目的实测记录），以后加一步"顺便多留一点 zip 里的东西"应该是纯加法，不该逼着重写
    这个函数。
  - 新开关 `pdf_text_backend` 的取值只表达"用谁来提取文字"，**不要**以后偷懒把"要不要顺便
    生成图片描述"（线B）或"要不要顺便去噪"（线C）也塞进这同一个键的取值空间（比如搞出
    `mineru-cloud-with-caption` 这种复合值）——这三件事本质独立（提取引擎 / 要不要看图说话 /
    要不要 LLM 洗稿），以后各开各的键，避免以后配置值组合爆炸。
  - 线A 新路由如果起独立缓存路由标签（而不是复用 `ocr:mineru-cloud`，见下面"具体技术风险"），
    顺带也是在给线B/线C 以后叠加"这份缓存要不要包含图片描述"这类新维度留出干净的位置——
    路由标签的含义越单一清晰，以后往上叠加维度越不容易互相打架。
  - 这几条都是"未来别返工"级别的提醒，不是要求线A 现在就多写代码实现线B/线C 的能力。

- **图片/图表语义内容缺失**（原「图片内容打标」旧条目，2026-08-26 深入讨论后重写）：现管线
  只索引文字，图片/图表本身的语义内容（电路图/曲线图/机械图等）完全不入索引——本地直提和
  MinerU 云端 OCR 都只认字不认图；且当前两条路连图片文件本身都没保留（MinerU 结果 zip 其实
  带 `images/` 文件夹，现在只取最大的 `.md`、图片被整体丢弃；本地 `pymupdf4llm.to_markdown()`
  调用也没开 `write_images`）。用户实际场景已触发（工科课件多为扫描件 PDF，图表信息不可忽略），
  从"等触发"转为"设计中，待定几个开放问题"，不再是纯 backlog 空转项。

  已定设计方向：
  - 图片描述必须**独立成块**，不能拼进原文段落一起切（会重演问题15"关系信息污染检索排序"的
    翻版，这次污染的是"正文块被图片说明稀释/掺混"）。做法类比双链关系图但不同：双链纯 metadata
    不进检索，图片描述**需要能被搜到**（用户原话"图片本身的内容也有价值"）——所以是"检索库里
    独立一条记录，插在原图位置、不与相邻正文块合并"，与表格"故意粘连上下文"相反（表格离开
    上下文读不懂，图片描述要求模型自己写成一段自足的话，不需要依附旁边正文）。
  - ~~依据社区交叉信源（非官方逐字确认）：`is_ocr=false` 大概率不影响图片被抠出来~~ ✅
    2026-08-26 已用真实 API 实测坐实（见上方「R3a」条目的冒烟记录）：自制一份「真文字层+1张
    内嵌图」PDF，`is_ocr=false` 提交后返回的 zip 里图片被完整保留、markdown 里也自动带了
    `![](images/xxx.jpg)` 引用，位置就在原文对应处。不再是推断，是实测结论。这意味着文字层
    PDF 送 MinerU 时可以不为已有文字重复花 OCR 的钱，只买版面/图版识别（依赖上一条开关先做）。
  - 实测顺带拿到了结果 JSON 的真实结构（`*_content_list_v2.json`），对线B/线C 都有用：
    数组按阅读顺序排列每一块，每块有 `type`（如 `paragraph`/`image`）+ `bbox`（页面坐标）；
    图片块的 `content` 里有 `image_source.path`（对应 `images/` 里的文件）、`image_caption`
    /`image_footnote`（原图注/脚注文字，本次测试图没有配图注所以是空数组，但字段确认存在）。
    对线B 的意义：不需要额外定位逻辑，markdown 里的 `![](images/...)` 引用本身已经在原文
    正确位置，切块时顺着现有文本扫描到这个引用就是插入独立块的位置，不必依赖这份 JSON 的
    bbox 做定位；JSON 主要用于以后想拿"原图注文字"这个补充信号时用。对线C 的意义：`type`
    字段证实了"按块类型只清洗低风险类型"这个设计前提是成立的，不是猜的。

  开放问题（阻塞实现）：
  - ~~MinerU 的"图片描述"功能到底是什么~~ ✅ 已查清（MinerU 论文 arXiv:2409.18839 原文）：
    是原图注文字（"figure caption"作为版面元素被检测+提取，跟正文/表格/标题同一类处理），
    **不是模型生成的语义描述**——论文明确写架构里没有"看图理解内容"这一环，只做版面检测
    （定位图在哪）+ 裁切（把图抠出来），不涉及图像内容理解。即用即弃的原图小标题（如
    "图3.2 xxx"）本身仍有用（可以跟 VLM 生成的长描述拼一起，短标题利于关键词命中、长描述
    利于语义命中），但不能替代看图说话这一步，独立 VLM 这一环省不掉。
  - 描述来源二选一（上一条排除了"MinerU 自带"这个选项）：本地 VLM（复用 `hyde_llm_url` 那套
    LM Studio 本地 OpenAI 兼容服务模式，零云端花费但吃本地显存、需换一个真正支持视觉的模型）
    ／独立云端 VLM（每张图都要花钱调用）。本地这条要打个问号：本地显卡 8GB 已经和 bge-m3
    抢显存（MinerU VLM/hybrid 后端因此被搁置，见下方 Backlog 条目）——不只是"装不装得下"的
    问题，工科图表（电路图/曲线图这种）对视觉理解精度要求本来就高，小尺寸本地模型在这类任务
    上的理解力通常明显弱于大参数云端模型，本地这条路很可能是"装得下但看不准"，质量本身也是
    赌注，不只是显存够不够的问题。2026-08-26 补充：这个弱项不止"看不懂图在讲什么"这一层，
    连"图上贴的字读不读得准"（元件编号/坐标轴刻度这类嵌在图里的细小文字）这层更基础的活，
    本地通用视觉模型大概率也不如 MinerU 扫描件路径用的专精 OCR 引擎（PP-OCRv6）——专精
    识字模型和通用视觉模型是两种不同优化方向的工具，不是同一件事的强弱版本。两层都偏弱，
    进一步指向云端更可靠，但也进一步坐实"MinerU 抠字/抠图 ≠ 解释图片含义"，独立看图说话
    这一步无论走本地还是云端都省不掉。
  - 描述质量是赌注：空泛描述（"一张包含方框和箭头的图"）比不做还差——检索命中但零信息量，
    伤用户对系统的信任，需要专门设计给模型的指令。
  - 成本量级与双链关系图完全不同（那个是纯本地正则+CPU，几乎免费）：这条路径真花钱/真吃
    资源，覆盖面从"仅扫描件"扩大到"所有 PDF"后开销进一步放大，需要用户明确接受量级再动手。

  **2026-08-26 用户决定：线B 今天不实现，所有资讯/决策/开放问题保留在这里，以后单独开轮。**

- **MinerU 结果 LLM 后处理去噪**（2026-08-26 新增讨论，未定论）：MinerU 云端结果 zip 里
  markdown 之外还有结构化 json，现在整个被丢弃只取 md（`extractors._mineru_cloud_extract`）。
  2026-08-26 实测确认了真实文件名与结构：`*_content_list_v2.json`，数组每项含 `type`
  （`paragraph`/`image` 等）+ `bbox` + 对应内容，块类型确实是结构化字段（不是要另外推断）。
  工科扫描件 OCR 噪音率不低（用户实测反馈"准确率不太高"），设想让 LLM 读这份 json 定点
  清洗——利用 `type` 字段只清"正文"类噪音（错别字/断行/格式），公式/表格/数值密集块直接
  跳过不碰。**核心风险**：LLM"修正 OCR 噪音"与"自信编造看似合理实则错误的内容"边界模糊，
  两者呈现效果一模一样、事后无法分辨——对工科内容（公式/单位/数值/型号）而言，被自信改错
  比留着乱码更危险（乱码至少让人起疑，改错的不会）。若做，范围必须严格限定在低风险块类型，
  且指令明确"拿不准就保留原样，绝不内容补全"；这一步会再叠加一层 LLM 调用开销，和图片描述
  那条一样不是免费的，账要一起算。**2026-08-26 用户决定：今天不实现，留到以后单独开轮。**
- MinerU VLM/hybrid 后端（需 ~7.7GB 显存，8GB 卡与 bge-m3 冲突，除非换卡否则不可行）
- **多维过滤检索（tags/frontmatter 属性/时间范围）**：现 `search_knowledge`/`hybrid_search`
  只支持 folder（目录前缀）+ libraries/exclude（库范围），tags 虽已提取（`extract_frontmatter`）
  但只揉进了嵌入锚点文本帮语义匹配，不是可精确过滤的结构化字段——vault 话题域重叠时
  （如"配置"一词横跨 CFD 笔记与 AI 笔记）语义相似度这一维不够用，需要标签这种结构性约束
  兜底（同问题20"置信度虚高"根因的延伸：分数是"像不像"不是"对不对"）。
  触发条件：先确认 vault 里 frontmatter tags 覆盖率与体系一致性是否足够支撑这个过滤器
  （若标签打得随意，做出来收益有限）；确认后再排期（2026-08-25 讨论记录）。
  **多库前提**：本系统是多库架构，非 Obsidian 类库（课业资料/代码注释/日常笔记等）天然
  没有 frontmatter 标签体系——过滤器须做成「有标签的库能用、没有的库自然空转不受影响」，
  不能假设所有库都具备这套元数据，不是全局强制开关。
- ~~双链关系图（出链/入链，独立于语义检索排序）~~ ✅ **后端 + MCP 已完成（2026-08-25，
  问题28）**：新增 `index.py` `extract_wikilink_targets`/`resolve_note_relations` +
  meta `links` 字段（旁路于 `clean_wikilinks` 之外，不影响问题15修复的排序）、`_links_missing`
  惰性回填机制（kb_stale/_index_core 同步）、MCP 工具 `note_relations(path, library="")`；
  入链不持久化、按需现算，规避"两份数据对不上"风险。七件套全绿（含新增7例）。
  **GUI 展示已完成（2026-08-25，问题29）**：语义检索卡每条结果新增"关联笔记"内联展开
  入口（出链/入链列表），与正文展开互相独立的开关，纯展示层接线，复用本条已有的
  `resolve_note_relations`，不改后端。详见 TASK_LOG.md 问题29。
- **MinerU 云端批量并行加速（"batch功能"）**：✅ **已完成（2026-09-03，问题35，与问题34
  同日各占一提交）**。按下方既定四步方案落地：①主循环扫描段用 `classify_extraction`
  预判分流，cloud 文件攒进 `cloud_jobs` 与"何时发请求"解耦；②`ThreadPoolExecutor`（配置
  `mineru_concurrency` 默认 3，1=串行回退）只并行 `_mineru_cloud_extract` 网络段
  （PyMuPDF 不保证多线程安全，worker 不触碰 pymupdf）；③结果经 `as_completed` 回主
  线程走统一的 `_store_chunks` 收口（meta/进度/切块全部单线程，无竞态）；④进度改批量
  语义（"云端处理中：已完成 K/N"）。健壮性配套同步落地：滑动窗口限速
  （`mineru_rate_per_minute` 默认 45 对官方 50/分钟留余量）、错误三分类（transient 指数
  退避+抖动重试尊重 Retry-After / fatal 立即失败 / token 置全局标志停整批）、断点簿记
  `data/extract_cache/mineru_pending.json`（中断任务下轮自动续接，不重复提交）、孤儿
  清理、每日页数配额 80% 日志提醒。测试 +11 例（67/67），六件套全绿。详见 TASK_LOG.md
  问题35。

  > ✅ **2026-09-03 追加（问题36）**：`mineru_concurrency` 新增 **0=最大吞吐模式**——
  > 不设固定并发，提交节奏完全交给滑动窗口限速闸门：窗口没满立刻送（最大限度）、
  > 接近每分钟频控自动停下、窗口滑动续送，任务完成腾出的线程让排队文件立刻补位，
  > 直到全部完工；内部线程池上限 128 防异常规模。配套修正：轮询遇 429/5xx 改为
  > deadline 内退避续询（绝不误判任务失败），404/非 JSON 才判 gone 且清簿记条目
  > （堵住"永久续接一个不存在的任务"）。默认值仍为 3，max 是可选项。测试 +4
  > （71/71），六件套全绿。详见 TASK_LOG.md 问题36。

  以下为方案设计阶段的原始记录（保留备查）：

  **认知纠正（方案设计阶段已排除的错误方向）**：GitHub Discussions 里能搜到的
  `MINERU_API_MAX_CONCURRENT_REQUESTS` 环境变量、`--workers` 启动参数、Docker/K8s 水平
  扩容等方案，全部是讲自建本地 MinerU 服务的服务器端配置——本项目走的是 `mineru.net`
  云端托管 API，普通调用方身份，这些完全不适用。

  **官方限制**（已交叉验证）：单次批量提交（`file-urls/batch`）最多 50 个文件 URL；
  单文件 ≤200 页/≤200MB；每账号每日 1000 页最高优先级解析额度（超出降级不拒绝）；
  IP 限频超出返回 HTTP 429（具体 QPS 未公开）。**同时处理中的任务并发上限官方文档未给
  出数字，需要实测摸底，不能拍脑袋设置**。

  **方案方向（已确认，未写代码）**：不能简单给现有 `for` 循环套线程池——该循环把"指纹
  比对""提取转换""终态写 meta""进度计数""切块入库"全部揉在一起顺序执行，大量代码隐含
  "我是唯一线程"假设（`meta[rel]=...`、`changed+=1` 均无锁），直接并发化会产生竞态。
  拟分四步：①扫描阶段先跑完指纹比对，把需要送云端的文件攒成列表，与"何时真正发请求"
  解耦；②只用 `ThreadPoolExecutor` 并行 `_mineru_cloud_extract` 这一段网络 I/O（提交/
  上传/轮询/下载，非 CPU 密集，无需多进程），并发数从保守的 2-3 起步，实测摸高；
  ③`as_completed` 拿到结果后回主线程，`meta[rel]=...`/`changed+=1`/`update_progress`/
  切块入库等状态更新继续单线程串行执行，天然避免竞态；④`update_progress` 语义要从
  "当前处理哪一个文件"改成"N 个文件云端处理中，已完成 M 个"的批量语义。配额保护必须
  随方案一起落地：分批提交（单批≤50）、接近每日 1000 页配额时记日志提醒会被降优先级。

  ~~**触发条件**：架构级改动……需要用户确认具体方案后再单独开一轮实施~~（✅ 2026-09-03
  用户批准后单独一轮实施完成，并发场景测试用例已纳入六件套，AGENTS.md 架构红线
  全部保持未破坏；历史记录见 TASK_LOG.md 问题33"遗留"与问题35）。

---

## GUI 第二实现（问题 42，2026-09-06）：guiweb = pywebview 壳 + HTML 前端 + 全库图谱

> 状态：✅ 已完成并与 Flet GUI 并存（gui/ 保留不删）。动机与架构见 TASK_LOG.md 问题 42。

- [x] pywebview 选型验证（Python 3.14 + WebView2 双向桥冒烟）
- [x] 契约 28 方法 + 推送事件；bridge.py 复用 store/worker/config_editor/library 零逻辑重写
- [x] 全库图谱数据层（双链/WEMM 归属/PDF 管线四态可验证/主题聚落/可选语义边）
- [x] 前端六视图（图谱/检索/库/索引/试验台/诊断/设置）+ 动态岛 + 全部确认门禁，离线零 CDN
- [x] 接线静态检查（wiring_check）纳入回归；tests/test_guiweb.py 45 用例
- [x] 真实数据实跑验证（5 库快照 / 397 节点图谱 / 失败明细 / WEMM 探活）
- [x] 真机首轮反馈修复（问题 43）：设置页空下拉根因（JS 空数组 truthy）、
      图谱检索 busy 反馈 + 角标残留清理、原生路径选择弹窗（pick_path 三处接线）
- [ ] 后续可选：pywebview 打包单 exe（PyInstaller）；语义边缓存落盘；WEMM 页节点缩略图预览

---

## 路径级勾选建模（问题 44，2026-09-06）：库内文件/文件夹级决定建不建库

> 状态：✅ 已完成。设计经用户逐项拍板（定稿并入 TASK_LOG.md 问题44）。

- [x] 数据面：selection_in/out 存注册表 + resolve_selection 最近显式赢 + 格式批量语义
- [x] 扫描漏斗：collect_md_files 勾选过滤，11 处调用点穿线（排除=对管线不存在，
      条目裁剪/块清理/WEMM 页裁剪全复用现有机制）
- [x] MCP 硬门禁：selection_gate 两段式（提案号+6位确认码+10min TTL+一次性+审计），
      propose/apply/get_selection 三工具；read_document 拒读显式排除文件
- [x] GUI：库管理「勾选范围」右侧抽屉（下钻/徽章/跟随恢复/格式快捷批量/攒批保存）
- [x] config 新键 selection_new_files（follow/include/exclude，默认 follow）
- [x] 测试：test_selection.py 63 用例 + 十件套全回归绿


---

## 检索置信度语义锚 + 展示分零点重标定（问题 45，2026-09-06）

> 状态：✅ 已完成。动机：用户质疑 0.50~0.70 的窄分数区间会误导 AI agent——
> 实测证实有效动态范围只有 0.50~0.73（噪音地板 0.50~0.52，强命中上限 ~0.73）。
> 用户进一步追问"未命中怎么还有 50%"后追加零点重标定。
> 详见 TASK_LOG.md 问题 45。（编号备注：本条最初挂问题44,因同日另一会话将
> "库内路径级勾选"注册为问题44,整体改挂问题45。）

- [x] 九组真库查询实测分布取证（确定命中/模糊口语/库中不存在三组对照）
- [x] 来源行置信度附分档词：`[置信度 x.xx·高相关/中相关/弱相关]`（边界来自实测）
- [x] 展示分零点重标定：`retriever._conf_display` 锚点映射 0.50→0.00、0.73→1.00,
      未命中归零;排序/阈值过滤仍用原始分,换打分模型须重测锚点
- [x] 两套 GUI 解析正则兼容新旧格式 + 配色档位换展示分尺度（绿 ≥0.85 / 青 ≥0.20）
- [x] `search_knowledge` 工具说明写明分数语义（LLM 读法指南）；config 注释双尺度说明
- [x] 评估并否决 sigmoid 温度拉伸（单调变换不改排序，对 agent 无实质帮助）
- [x] 发现并备案：drop 阈值护栏在 sigmoid 地板下永不触发，warn 才是日常主护栏

## 问题48：提取质量第一档（2026-09-08）

- [x] 索引层噪声清洗：页码行/逐字重复样板行/死图链剥离（_finalize 漏斗，META_VERSION→10，下轮索引全量重建一次）
- [x] 回归：audit 43/43（新增4项）、extractors 73/73（含版本钉子同步）、guiweb 89/89、其余套件全绿
- [x] 真库验证：1446份缓存md，样板307份命中删130万字符、页码147份删5.3万字符、死图链23份删5.6万字符
- [x] 第二档（sidecar 双轨）：云端解包落官方块标注 sidecar；索引有 sidecar 精确删页眉/页脚/页码、无则启发式回退；META→11（一次全量重嵌，零配额零重提）；extractors 74/74、audit 44/44、其余套件全绿；71 个 v1 孤儿缓存移入 _orphan_v1_bak_2026-09-08

## 问题49：索引完成后全局回收（2026-09-08）

- [x] 盘点：Chroma/WEMM 的已删文件块每轮索引本就在精确清理；缺口=提取缓存孤儿 + 已删库残留
- [x] index.prune_unreferenced_data：删孤儿 md/sidecar/tmp + 已删库指纹 + 残留 collection（多库 meta 并集 + 在途 MinerU 断点簿记受保护；幂等、失败降级）
- [x] 触发=整轮索引全部成功后（CLI + server._run_index，GUI/MCP 共用）；had_error 跳过
- [x] tests/test_prune.py 5 用例；全套件回归绿

## 问题50：guiweb 诊断页失败明细/WEMM 明细修复（2026-09-08）

- [x] failures("全部库")=0：空串当库名找库 -> 改聚合全部库
- [x] 标题计数错位：total 原是正常索引数(176) 非失败数 -> total=失败条数 + healthy 正常数
- [x] 错误行可展开：detail 取该文件最近索引日志摘录 + 打开源文件按钮
- [x] WEMM 明细空列：store 元组行未在 guiweb 桥转对象 -> 转 {lib,rel,pages,failed,reason}
- [x] WEMM 行点击展开/打开源文件；mock/contracts/app.css 同步
- [x] test_guiweb 96/96（新增 failures 聚合与 wemm 转换回归）

## 问题51：MinerU 本地部署 R3b（2026-09-09）

- [x] 环境：uv tool install mineru[all]（py3.12 隔离）+ cu128 torch（官方 CPU 轮子换 CUDA）
- [x] mineru_server.py：壳（串行锁/懒加载/空闲卸载/页上限）+ 复用 ReusableLocalAPIServer
- [x] arbiter：ensure_mineru（端口顺延）+ evict_mineru（双向抢占）+ MINERU_MIN_VRAM_GB=4.5
- [x] extractors：扫描分支真调用 + kind=local 串行 + deferred + sig 就绪位 + VER 4→5
- [x] index：deferred 跳过不落终态；bge 加载前反向 evict_mineru
- [x] 配置/GUI：3 新键 + scan 下拉/试验台/budget/文案去占位 + contracts
- [x] 回归：extractors 78/78、arbiter 52、其余十件套全绿
- [x] E:\models\hf 建 pipeline 模型 + 真 VRAM 峰 1827MB(含 1 页)
- [x] 真gp- torchvision 0.23->0.26+cu128(nms ABI 断 方) + pymupdf 衡 tool 环境(页数计)
- [x] 框 chain 通 mineru_server /parse -> md 102 字 OK
- [x] MINERU_MIN_VRAM_GB 保 4.5(官 4GB + 余量)

## 检索命中渲染 + GUI 内正文查看（问题 52，2026-09-10）

> 状态：✅ 已完成。用户需求：输出 md 默认看渲染好的而非纯字；点开不离开 GUI 看正文。
> 详见 TASK_LOG.md 问题 52。

- [x] 共享层 `store.read_document_text`（零触发只读 + 穿越拒绝 + 20 万字截断）
- [x] guiweb：`_md_to_html` 升级 + search 带 rendered_html + read_document + mDoc 弹层
- [x] Flet：展开态 `ft.Markdown` + 收起去标记预览 + 正文弹层
- [x] 契约/mock/ parity 同步 + 两处测试文件新增 12 项用例

## 图谱第一屏返工（问题 53，2026-09-10）

> 状态：✅ 已完成。用户反馈字堆字/显存高/放大模糊/检索只出两条/像杂草。
> 详见 TASK_LOG.md 问题 53。

- [x] 标签密度策略（缩小或节点多时只留 hub/命中/选中/悬停）
- [x] 去 will-change 常驻合成层 + 阴影瘦身 + 超量 perf 模式（显存/模糊同根因）
- [x] 图谱检索：多取候选按文件分组（上限 12）+ 三环封顶轨道 + 非命中径向排斥
- [x] 页图层默认关 + 锚点环形铺开 + 初始散布拉大；`G.lastSnips` 落地激活 Inspector 命中片段

## guiweb 命中路径解析修复 + 置信度跳变定位（问题 54，2026-09-11）

> 状态：🟡 解析侧与置信度尺度已修并全绿；**同节折叠/弹性配额待用户选**。详见 TASK_LOG.md 问题 54。

- [x] `bridge.parse_search_text` 改「定位锚点」解析纪律：置信度标记为截断点 / 标题
      `find(" (## ")` + 剥末尾 `)`（不再用 `[^)]+` 正则）/ 无标题按 `" ["` 兜底
- [x] 回归 8 项：编号含 `)` 标题、`[已回填父节全文]` 尾巴、行中低置信注记、空标题、
      无标题、**生产端 round-trip 契约哨兵**（`retriever._format_results` → 解析）
- [x] 徽章空分数守卫：显示 `-- 无分数`（不再 `Math.round(null*100)=0` 冒充 0 分）
- [x] `open_source` 去掉字面 `#` 标题锚点（Obsidian 要求 `%23` 编码进 `file` 值；
      跳标题待本机实测通过后再按正确形式回填）
- [x] **根因修复**：重排分只 sigmoid 一次（`CrossEncoder` 自带 Sigmoid，此前套了两遍，
      全体置信度被压进 (0.5,0.731)）；`_CONF_ANCHORS`/`_conf_display` 随压缩一起删除
- [x] 旧补丁逐个处置：问题43 分档线 0.65→**0.75**、问题45 零点重标定**删除**、
      两 GUI 配色/徽章 0.85/0.20→**0.75/0.30**（用例断言与后端常量同值）、
      问题43/45 的锚点用例改写为「禁止二次激活」+ 数值透传行为锚
- [x] 阈值按真分重标：`confidence_warn_threshold` 0.55→**0.30**；
      `confidence_drop_threshold` 0.40→**0.0 显式关闭**（修好尺度后旧值会突然开始
      丢结果 = 把用户"先放着"的及格线偷偷打开）；`data/config.json` 同步
- [x] **同节折叠**：名额改按「实际交付」计——正文模式交候选窗口（top_k×4），
      `_format_results` 边折叠边计数：同小节只交付一次全文、同篇封顶数交付条数、
      top_k 数交付条数（折叠腾出的名额由窗口补位，条数不减）；`_expand_parent` 加缓存
- [ ] **待选**：同篇封顶弹性配额（按「有内容的文档数」定每篇配额；需先定"哪些文档算有内容"）
- [ ] **待选**：及格线（`confidence_drop_threshold` > 0，实测建议 0.05~0.10）——
      用户明确先放着，本次保持关闭
- [ ] 附记：`tests/run.py` 会写真实 `data/`（进度/锁/16 个提取缓存文件），与
      AGENTS.md「真 data/ 零触碰」不符，待单独处理

## 检索结果自适应建议（问题 55，2026-09-11）

> 状态：✅ 已完成（advice.py 12 条规则 + MCP 说明书 + 两套 GUI 提示横幅统一）。
> 详见 TASK_LOG.md 问题 55。

- [x] `advice.py`：12 种结果形态 → 可执行建议（≥3 条强命中提醒调大 top_k、同名不同目录、
      命中落非笔记库、整批偏低、只有一条相关、含 PDF、已折叠回填、清单模式、命中太少…），
      纯规则零 I/O，每次≤2 条（建议不能变成噪音）
- [x] 接线 `_format_results`（正文/清单两种模式都把建议插在结果前）；原"整体置信度偏低"
      单独提示并入规则①（同条件，不重复说两遍）
- [x] `server.search_knowledge` 说明书：修正过期尺度（真分）、新增"（…）行是建议不是结果"
      与"用法剧本"（read_document / navigate_knowledge / include_body=false / 调 top_k）
- [x] Flet `_render_results` 提示行改渲染成横幅（与 guiweb 同规则，修掉幽灵卡老 bug）
- [x] 回归：audit 46/46（12 场景逐条断言）、test_guiweb 137/137、test_gui_store 0 failures
- [ ] 待选：建议是否加配置开关（当前固定最多 2 条、恒开）
- [ ] 待选：去重的"同名不同标题/同标题不同目录"识别要不要做成独立的库健康检查项
      （当前只在检索结果里提醒；`find_duplicates` 判的是内容相似度，实测 FLUENT 两篇仅 10% 重叠）

## 库简介（问题60，2026-09-16）：list_libraries 补上"这库到底讲什么"

> 状态：✅ 已完成（`library_summary.py` 采样生成 + `summary_gate.py` 覆盖保护 +
> 3 个 MCP 工具 + guiweb 编辑/刷新入口）。详见 TASK_LOG.md 问题60。

- [x] `library.py`：registry 新增 `summary` 字段 + 唯一写口 `set_library_summary`
      （source=ai/user 校验 + 300 字上限）+ 读侧防御 `get_library_summary`
- [x] `library_summary.py`：复用索引时已算好的 Chroma 块向量做最远点采样
      （15~25 个代表块，输入规模与库大小无关）+ 内容指纹/过期判定 +
      复用 `hyde_generate` 式 OpenAI 兼容调用（本地/云端只是 url/api_key 取值差异）
- [x] `summary_gate.py`：仿 `selection_gate.py` 的两段式确认（source=user 时
      AI 覆盖须提案号+确认码+TTL+一次性；用户自己改不经过此门禁）
- [x] MCP：`get_library_sample`/`propose_library_summary`/`apply_library_summary`，
      docstring 硬编码触发纪律（仅显式请求）与内容规范（导航性质/禁止摘抄/≤300字）；
      `list_libraries` 展示简介与过期提示
- [x] guiweb：库卡片简介展示+编辑入口、库页顶部"刷新全部简介"（目标=当前库范围
      多选，空=全部库）+ 常驻进度条（转圈+X/Y+当前库名，可隐藏不影响后台继续跑）、
      设置页「库简介生成」分组（`config_editor.py` 共享，Flet `gui/` 设置页同步可见）
- [x] 生成改后台任务+轮询（`refresh_library_summaries_batch`/`_poll`，同提取
      试验台 start/poll 模式）：单库/批量共用一条路径，关弹层/切标签页不影响
      任务继续跑，完成后 toast；用户手写锁定的库批量时统一汇总问一次是否覆盖
- [x] 真机实测修复（2026-09-16）：Chroma embeddings 是 numpy 数组，`or []` 类
      真值判断直接抛异常，改 `is None`/`len()` 判空；本地思考型模型（Qwen3）
      超时/token 预算太紧（30s/400 token 抄的是 HyDE 查询期配置），改 180s/2000
      且做成可调配置 `library_summary_llm_timeout_seconds`/`_max_tokens`
- [x] 回归：`tests/test_library_summary.py` 50/50，编入 `tests/run.py`
      （16/16 全绿）；`guiweb/wiring_check.py` 全绿
- [x] 真机连续批量刷新暴露的 3 个问题（2026-09-16 第二轮）：① 本地 LLM 服务端
      "提示词前缀缓存"跨库串味（不是代码传上下文，是 llama.cpp/LM Studio 类
      服务端复用了上一次请求的 KV 缓存）——`build_prompt` 把库名挪到全文第一行
      + 显式声明"独立请求勿沿用"，`call_llm` 无 `api_key`（判定本地）时带
      `cache_prompt: false`；② 批量场景里同一个手写库被反复追问覆盖——`app.js`
      新增会话级免打扰记忆 `SUMBATCH.skip`，被拒绝过的库批量刷新时静默跳过，
      仅在用户专门打开该库自己的弹窗点"刷新简介"时才清掉记忆重新问；③ 简介
      弹窗太小看不清内容——改用 `modal-doc`（720px）+ `.sum-textarea-lg`（最小
      高度 260px/14px 字号）
- [ ] 待选：Flet `gui/app.py` 的库管理页补上同款编辑/刷新入口（本轮先 guiweb）
- [ ] 待用户：真实本地（LM Studio）或云端 LLM 端到端冒烟（已用真实 LM Studio +
      qwen3.6-35b-a3b 验证过修复有效，但尚未确认修复后完整跑出一份简介）
- [x] 简介内容规范 3 选 1 已拍板：用户选"范围地图型 + 检索决策卡型"融合——
      先总体定位+主要主题板块，再明确"适合查什么/大概率查不到什么"（正反
      两面）；已改写进 `library_summary._PROMPT_INSTRUCTIONS`
