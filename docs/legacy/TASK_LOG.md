# 个人 AI 工程助手 — 任务记录

> 开发记录：Obsidian Vault + RAG + MCP + AI Agent 全链路搭建与调优
> 更新：2026-08-11

---

## 1. 任务背景

用户（USM 航空航天工程学生）用 Obsidian 维护个人知识库，目标是搭建"个人 AI 工程助手"：

```
工程师
  + AI Agent（opencode + DeepSeek v4 flash API）
  + 知识系统（Obsidian Vault 语义检索）
  + 自动化（MCP 工具）
```

**核心问题**：Vault 文件量小（117 个知识文件）但内容零散，文件粒度的检索失效 ——
AI 若想回答"免费证书有哪些"，必须通读全文才能定位到分散的笔记。

**关键决策**：
- 检索层全本地（数据不出本机），仅问答内容发给 DeepSeek API
- Embedding 用 BGE-M3（多语言、0.6B、8192 上下文），向量库用 Chroma
- 需要 RAG 的原因不是文件多，而是"内容分散 + 抽象查询无法定位"
- 不自建 MCP 服务器（用 opencode 原生工具），本项目只提供检索能力

**交付物**：
- 架构文档：`个人AI工程助手-系统架构方案.md`（桌面）
- 检索系统：本项目 `obsidian-rag/`（索引 + 混合检索 + MCP 服务器）

---

## 2. 解决的问题（按时间线）

### 问题 1：精确名词能命中，泛化查询失败

- **现象**：搜 "IBM SkillsBuild" 命中；搜 "免费证书" 返回 `目录.md` 等无关文件，
  完全错过 `OTHER CERT/论点/免费入门证书清单.md`。
- **根因**：
  1. 纯 dense 检索对抽象查询语义泛化不足
  2. 结构类文件（`目录.md`、`AGENTS.md`、`LOG.md`）污染结果
  3. opencode 会话记录（`session-ses_*.md`）被当知识索引
- **解决**：见下文 §4。

### 问题 2：全量重建会把索引清空（数据丢失）

- **现象**：`--full` 先 `delete_collection` 清库，随后进程被中断 → Chroma 变成 0 块。
- **根因**：清库是立即的，重建是异步慢速的，中断窗口导致数据真空。
- **解决**：这是操作层面问题，不是代码 bug；恢复方法 = 重跑 `--full`。
  已在本文件 §5 记录"不要在重建中途强杀进程"。

### 问题 3：CUDA 版 PyTorch 装不上（Smart App Control 拦截）

- **现象**：`torch 2.13.0+cu130` 加载报 `OSError: [WinError 4551]`
  "An Application Control policy has blocked this file"。
- **根因**：Windows 11 的 Smart App Control（SAC，enforce 状态）基于**云端信誉**
  拦截低信誉 DLL。cu130 是最新构建，`torch_global_deps.dll` 信誉不足被拦；
  `torch_cpu.dll` 同样无签名却能加载，证明拦截是信誉驱动而非签名驱动。
- **解决**：降级到 `torch 2.11.0+cu128`（已发布约半年，信誉充分，
  且 RTX 5060 Blackwell sm_120 要求 CUDA ≥12.8，cu128 恰好满足）→ 一次通过。

### 问题 4：Python 3.14 兼容性顾虑（未发生，属预判）

- 起初怀疑 torch CUDA 版不支持 Python 3.14 需降级。
- 实测官方支持 Python 3.10-3.14，`cp314-win_amd64` wheel 存在，无需降级。

### 问题 5：库悄悄过期（指纹检查缺失）

- **现象**：每次搜索前 AI 必须**记得**调 `reindex_knowledge`，否则库陈旧。
- **方案 B（已实现）**：新增 `kb_stale()`（`index.py`）指纹检查——
  只扫 Vault 文件 MD5 对比 `index_meta.json`，不加载模型、毫秒级。
  `search_knowledge` 调用前 `ensure_fresh()`（`server.py`）自动增量同步，
  AI 无感知、库永远新鲜。变更时返回结果开头提示。
- **附带**：文件发现逻辑抽为 `collect_md_files()`，索引与指纹检查共用同一套
  过滤规则，避免误报。

### 问题 6：双语检索弱（中文查不到英文笔记）

- **现象**：中文查询"仿真配置/设计参数"搜不到 `HEBAT3_ORK_Design_Parameters.md`
  （全英文内容）。dense 相似度低（0.555），BM25 中文 2-gram 与英文 token 零重叠（score=0）。
- **方案（已实现）**：**索引期中文锚点增强**——切块时若文件 title/summary 含中文
  且正文以英文为主，把中文元数据拼到块头（`index.py` `make_anchor`）。
  - dense 相似度 0.555 → 0.641（提升 15.5%）
  - BM25 中文 2-gram 能通过锚点命中英文块
  - 锚点**只加首块**（chunk=0），避免逐块冗余；存 metadata `anchor` 字段
  - 检索显示时按 metadata 精确前缀剥除（`retriever.py` 用 `meta.get("anchor")`，
    不用正则），LLM 只看到原文
- **验证**：中文查询"HEBAT3 仿真配置 设计参数 飞行数据"直接命中
  "3. Simulation Configuration" 块。

### 问题 7：代码审查发现的三个真实 Bug（2026-08-02 修复）

用户对 `index.py` / `retriever.py` 做了逐行 code review，发现 3 个真实 Bug
（另附若干性能问题与死代码，见下）：

- **Bug 1（删除文件不清理 → 永久 stale）**：
  索引只"写新"不"删旧"，已删文件块永远留在 Chroma 污染结果；
  且指纹检查把已删文件计入 `removed` → 每次搜索都 stale → 反复全量重建。
  - 修复：清理逻辑改为按 `meta` 记录的块数生成精确 `valid` id 集合，
    **`save_meta` 前先修剪 meta 中磁盘上已不存在的文件键**（关键一步），
    无效 id 一次性 `collection.delete`。
  - 验证：建 `TMPBUGTEST.md` → 索引 → 删除 → 再索引 → "清理 1 个失效块"，
    Chroma 与 meta 均无残留，`kb_stale()` 不再误报。
- **Bug 2（块数变少留幽灵块）**：文件从 3 块缩到 1 块后，旧 id
  （`::1`/`::2`）残留。同 Bug 1 修复逻辑覆盖（valid 集按新块数生成，
  超出的旧 id 自动判失效）。
  - 验证：`GHOSTTEST.md` 3 块 → 缩为 1 块 → "清理 2 个失效块"。
- **Bug 3（--full 真空窗口）**：旧逻辑先 `delete_collection` 清库再慢速重建，
  中断即 0 块（即问题 2）。修复：`--full` 先嵌入全部新向量，**进入写锁后**
  才 `delete_collection` + upsert，真空窗口缩到毫秒级；`msvcrt` 文件锁
  `data/index.lock` 防并发写。
- **性能修复**：`kb_stale()` 快速路径（mtime+size 命中则免 MD5）；
  检索结果格式化合并 N+1 次 `get` 为单次批量 `get`；BM25 索引构建
  合并两次 `get` 为一次。
- **死代码清理**：删 `rrf_fuse()`（权重融合已够用）、`build_bm25_index()`
  （建索引是 `get_bm25` 内部步骤）、`alpha` 参数（未使用）。
- **锚点设计修正**：锚点只加首块（原方案加到每块，浪费 + 冗余）；
  剥除从 `ANCHOR_RE` 正则改为 metadata `anchor` 字段精确前缀匹配（无假阳性）。
- 验证通过后全量重建 787 块 / 117 文件（28.7s），双语 + 泛化查询回归全绿。

### 问题 8：切块切在句子中间，语义被截断（2026-08-02 修复）

- **现象**：暴力切块（1500 字符硬截断）可能在句子中间剪断，
  "在阅读本文件之前，不可以执行这个程序"被切成"执行这个程序"，
  否定/条件语义被曲解。
- **方案**：先扫描 Vault 结构（117 文件：429 块 ≤200、36 块 >1500、
  14 个文件有超长段落）——结论是不需要 embedding 语义切块，
  标题切块已覆盖 80% 文件，只对超长块做**两级边界降级**（见 §4.3）。
- **验证**：787 → 1058 块；>1500 从 36 → 5（全为纯表格，无句号可切）；
  双语/泛化/网格无关性查询回归全绿；锚点仍只在首块。

### 问题 9：第二轮 code review 的三个真 Bug + 边界项（2026-08-02 修复）

- **Bug A（folder 过滤只挡 dense）**：`where` 只作用于 dense 查询，
  BM25 全库打分 → folder 外的高分命中照样进 top_k（如 folder="ROCKETRY"
  仍能返回 OTHER CERT 内容）。**且实测发现当前 Chroma 版本已不支持
  `$startswith`**（`ValueError: Expected where operator...got $startswith`），
  即 folder 过滤的 dense 路此前一直是坏的。
  修复：弃用 where，dense 查全库候选后在内存按块 id 前缀过滤，
  BM25 侧同理按 `file` 元数据过滤——两路对称、零版本依赖。
  验证：folder="ROCKETRY" 查询 5 条结果零越界。
- **Bug B（--full 真空窗口实际 ~28s 非毫秒）**：`model.encode` 实际在
  `write_lock()` 内（注释写"锁外"是错的），全量重建时锁被持有整个编码
  时长，并发进程 LK_LOCK 重试 10s 后抛 OSError。
  修复：编码移出锁外（先编码后持锁），锁内只剩 delete/upsert/清理/
  save_meta（毫秒级）；注释同步修正。
- **Bug C（ensure_fresh 无异常保护）**：index_vault 抛错 → search 整个
  失败，LLM 连陈旧结果都拿不到。修复：try/except 降级为
  "用旧索引 + 明确提示"，检索永不整体失败。
- **边界 4（段落级块不封顶）**：多段落中单段超长不切（只对唯一段落降级）。
  修复：每个段落独立判断 >1500 再降级句子切。
- **边界 5（缩写误切）**：`Mr.`/`e.g.`/`3.14` 被当句子边界。修复：
  常见缩写保护表（大小写变体）+ 英文边界要求"点后空格+大写/数字"+
  拼接补空格防粘连。
- **潜伏项顺手处理**：index_vault 增量复用 mtime+size 快速路径
  （免读全文+MD5，与 kb_stale 同策略）；collect_md_files 只跑一遍
  （current_rels 复用）；index_meta.json 原子写（tmp + os.replace）。
- **保持现状（单用户本地，风险极低）**：`--full` 期间并发读、
  `$startswith` 版本依赖（已弃用 where，不再依赖）。

---

## 3. 困难点

| # | 困难 | 说明 | 应对 |
|---|------|------|------|
| 1 | MCP 2.0 API 变化 | 2.0.0 移除 FastMCP 高层 API，文档滞后 | 改用 `MCPServer` + `@server.tool()` + `run("stdio")`（实测可用） |
| 2 | stdio 协议污染 | `print` 到 stdout 会破坏 MCP 帧 | 所有日志走 `log()` → stderr（`index.py:26`） |
| 3 | Windows 执行策略 | `opencode.ps1` 被拦截 | 用 `opencode.cmd` 调用 |
| 4 | 中文编码 | 默认 cp1252 下中文输出崩溃 | 设 `PYTHONIOENCODING=utf-8` |
| 5 | LSP 误报 | IDE 报 `chromadb`/`sentence_transformers` 无法解析 | LSP 用系统 Python 而非 venv，属误报，忽略 |
| 6 | 陌生高 CPU 进程 | 疑为本项目残留 | 排查后确认是 ANSYS Fluent 的 python 进程，非本项目 |
| 7 | SAC 拦截 CUDA DLL | 见问题 3 | 降级 cu130 → cu128 |
| 8 | 残留进程 | 多次测试遗留 python 进程占内存 | `Get-CimInstance` 定位并 `Stop-Process` |
| 9 | stdin 中文乱码 | PowerShell 管道传中文给 python 变成 `?`，污染测试查询词 | 测试脚本写 `.py` 文件（UTF-8）执行，不用 heredoc |
| 10 | 英文笔记双语检索弱 | 中文查询对全英文内容 dense/BM25 双路失效 | 索引期中文锚点增强（见问题 6） |

---

## 4. 如何解决（设计思路）

### 4.1 检索架构：Hybrid = Dense + BM25，加权融合

纯 dense 的语义泛化不够（抽象查询失败），纯 BM25 的词匹配又不够语义。
二者互补 → 混合检索。

- **Dense**：BGE-M3 嵌入查询，与全部块做余弦相似度
  （Chroma HNSW 空间为 cosine，`index.py:98`）
- **BM25**：关键词召回（`retriever.py`）
  - 分词：英文按词 + 中文连续片段按 **2-gram** 切（兼顾中英混合）
  - 参数 k1=1.5, b=0.75
- **融合**：加权得分
  ```
  score = dense_weight * sim + bm25_weight * (bscore / (1 + bscore))
  ```
  默认 dense 0.6 / bm25 0.4（`retriever.py:121`）

### 4.2 索引清洗（消除污染）

- **结构文件排除**：`STRUCTURE_FILES = {目录.md, AGENTS.md, LOG.md, README.md}`
  这些是链接清单/指令文件，非知识本体。
- **目录排除**：`.obsidian`、`.smart-env`、`.trash`、`TEMP`、`templates`
- **会话文件排除**：`EXCLUDE_PATTERNS = ("session-", "会话", ".tmp")`
  拦截 opencode 会话日志（如 `session-ses_0439.md`）
- **效果**：全量重建后 994 → 776 块，污染项 0。

### 4.3 切块策略

- 按 Markdown H1/H2/H3 标题切块（`split_by_headings`），
  每块带 `heading` 元数据，块 id 为 `{相对路径}::{块序号}`。
- 短文件（正文 ≤200 字符）整文件作为单块，用 frontmatter `title` 做标题。
- **两级降级切块（2026-08-02，问题 8）**：标题块 >1500 字符时降级为
  段落切（`split_paragraphs`，按空行）；段落本身 >1500 再降级为
  句子切（`split_sentences`，按 `[。！？!?]` 边界）。**永不从句子中间剪断**
  （单句超长宁长勿断），彻底消除"否定/条件语义被截断"类问题。
  纯表格（无句号/空行）无法再切，属正确边界。
- frontmatter 元数据（title/tags）提取后随块存储。
- **效果**：787 → 1058 块（+271）；>1500 块从 36 → 5（全部为表格）；
  平均块长 301 字符；`原始数据.md`（22392 单块）拆为 107 个主题块。

### 4.4 增量索引

- 文件指纹存 `data/index_meta.json`（hash + size + mtime_ns）；
  未变的文件跳过，变了的重新切块嵌入（`index.py`）。
- `upsert` 按 id 幂等写入，永不重复。
- 失效块清理（Bug 1/2）：以 `meta` 中每个文件记录的块数生成精确 `valid` id 集合，
  先修剪已删除文件的 meta 条目，再 `collection.delete` 所有不在 valid 的 id
  （`index.py` 写锁内）。删除文件与块数变少两类失效一次覆盖。

### 4.5 CUDA 迁移

- `get_model()` 自动探测：`cuda` 优先，回退 `cpu`（`index.py:31-38`）。
- `SentenceTransformer(MODEL_NAME, device=device)` 一次指定设备。
- 结果：全量索引 4分16秒 → **32 秒**（约 8 倍），查询嵌入毫秒级。
- 约束：RTX 5060 是 Blackwell（sm_120），要求 **CUDA ≥12.8**；
  torch cu128 / cu130 均可，cu128 更稳（SAC 信誉通过）。

### 4.6 系统调用链

```
opencode（LLM 判断何时检索）
   → MCP stdio 调用 search_knowledge / reindex_knowledge（server.py）
      → retriever.hybrid_search()
         → get_model() [BGE-M3 on CUDA] + Chroma query + BM25
   → LLM 结合检索结果作答，附 [来源] 链接
```

注册于 `~/.config/opencode/opencode.json`（MCP `obsidian-rag`）。

---

## 5. 操作备忘（踩坑记录）

```powershell
# 运行脚本前设编码（否则中文崩溃）
$env:PYTHONIOENCODING = "utf-8"

# 全量重建（清库重嵌；中途勿强杀进程，否则索引变空）
$env:HF_HUB_OFFLINE = "1"; .venv\Scripts\python.exe index.py --full

# 增量重建
.venv\Scripts\python.exe index.py

# 调用 opencode（ps1 被执行策略拦截）
& "$env:APPDATA\npm\opencode.cmd" run "..."
```

- torch CUDA 版来源：`--index-url https://download.pytorch.org/whl/cu128`
- 若遇到 SAC 拦截 DLL（WinError 4551）：降级到一个发布更久的 cu 版本，
  或从 Windows 安全中心关闭 Smart App Control（enforce 状态不可逆，慎用）。
- LSP 对 venv 依赖的解析报错是误报，忽略。

---

### 问题 10：检索结果"截断当完整" + 六项检索体验缺口（2026-08-02 修复）

- **现象**：用户实测检索"免费 CERT"时，19 块的清单文件只返回 5 块截断切片，
  AI 却把它当完整答案汇报——skill 缺完整性守卫。审查（general 子代理逐行
  review）后确认方案并修复 6 项 + 2 个顺手项。
- **方案**：
  1. **完整性标记（retriever.py `_format_result`）**：来源行带 `[块 k/N]`
     （k 取 meta.chunk，N 从 BM25 缓存 `_bm25_files` Counter 派生——与检索
     内容同源同生命周期，reindex 后同步重建，无独立缓存不撒谎）；超
     CHUNK_LIMIT 的块尾部附 `… [本块已截断，完整内容见源文件]`。
  2. **同文件封顶**：正文模式每文件最多 3 块（`MAX_CHUNKS_PER_FILE`），
     按分数保留最高、边迭代边计数填满 top_k；list 模式不封顶（标记即
     完整性提示）。命中封顶输出汇总行"同一文件最多展示 3 块…"。
  3. **folder 边界**：`_in_folder()` 前缀+边界校验（`rel == folder` 或
     `startswith(folder + "/")`），杜绝 folder="AI" 误匹配 "AIML/"、
     "AI.md"、"AI Dev/"；`_norm_folder()` 统一 `\\`→`/`、去首尾空白斜杠；
     folder 支持父目录与单文件。
  4. **dense 候选池**：`dense_k = max(top_k*8, 200)` 无条件放大（原 50），
     修 folder 检索后过滤的小目录召回天花板（BM25 侧本就全库打分）。
  5. **性能**：融合阶段 `in`/`.index()` 的 O(M·N) 列表扫描 → dict 查找
     （`dense_map`/`bm25_map`），二次搜索实测 0.02s。
  6. **文案对齐（server.py）**：reindex_knowledge docstring 改为"一般无需
     手动调用，仅在自动同步失败或用户明确要求时"；search_knowledge
     docstring 注明首次调用/变更后首次调用耗时数十秒属正常 + folder 精确
     语义 + `[块 k/N]` 标记说明。
  7. **skill（obsidian-knowledge-search）**：Step 6 重复 → 7（修编号）；
     新增 **Completeness guard 硬规则**（k<N 且涉清单/数量 → 必读源文件；
     截断标记 → 必读源文件；同文件 ≥2 块 → 整读优先；快问快答可只引块但
     不得声称完整）；folder 语义修正；首次延迟提示；list 模式口径
     （不封顶 + 标记说明）；frontmatter 描述与正文一致化。
- **验证**：验收脚本 27/27 全绿（见 §7 验收标准）。附带清理：C 盘 0 字节
  告急（pip cache 4.8GB + 旧 bge-m3 快照 + Chroma ONNX 垃圾 79MB），清出
  7.1GB——ONNX MiniLM 是 Chroma 默认嵌入函数触发的下载，本项目用 BGE-M3
  根本不需要，已删。

---

## 7. 验收标准（问题 10 修复）

| # | 验收项 | 结果 |
|---|--------|------|
| 1 | 来源行带 `[块 k/N]`，N 与 index_meta.json 块数一致（19 块文件回 19） | ✔ |
| 2 | 正文模式同文件 ≤3 块 + 封顶汇总行；list 模式不封顶、无汇总行 | ✔ |
| 3 | 截断标记（临时 collection 构造 7000 字符块单测） | ✔ |
| 4 | folder 边界 9 组单测 + `_norm_folder` 2 组全过；"OTHER CERT/论点"/"ROCKETRY" 零越界 | ✔ |
| 5 | 泛化查询（GitHub Foundations 多少钱）、双语查询（HEBAT3 仿真配置）回归命中 | ✔ |
| 6 | 二次搜索 0.02s（<5s 门槛） | ✔ |
| 7 | skill 编号无重复 Step、含 Completeness guard 节、与代码语义一致 | ✔ |

---

## 6. 现状与下一步

**已验证**：
- MCP 工具被 opencode 实际调用并正确作答（带出处）
- 泛化查询（"免费入门证书有哪些" / "GitHub Foundations 认证多少钱"）命中目标笔记
- CUDA 加速生效，索引 1058 块 / 117 文件，无污染
- 指纹自动同步：改文件后首次搜索自动增量更新并提示；二次搜索幂等无提示
- 双语检索：中文查询命中全英文笔记（中文锚点增强，dense 0.555→0.641）
- 三个 Bug 修复 + 性能/死代码清理后回归：删文件清理、幽灵块清理、
  锚点只在首块、双语/泛化查询全绿（28.7s 全量重建 787 块）
- 两级边界切块（问题 8）：永不剪断句子，超长块降级段落/句子切，
  787 → 1058 块，>1500 块 36 → 5（纯表格）

**待办**：
- [x] 端到端再验一轮（opencode 实际问答）→ 已由问题 10 验收脚本覆盖
- [ ] 评估防重复 server 进程方案（避免多次测试叠加内存占用）
- [ ] （可选）若泛化查询仍偶发不中，调 BM25 加权 / RRF 融合 / folder 限定
- [ ] 给 `HEBAT3_ORK_Design_Parameters.md` 等其他全英文笔记补中文 `summary`，
      让中文锚点更贴合内容（增强效果已验证，但锚点来自元数据，元数据越准命中越准）

---

## 问题 11：现存不足崩溃 + 无法自动修复 + 无法自动切 CPU（2026-08-05 修复）

- **现象**：显存/内存不足时：`torch.cuda.OutOfMemoryError` 直接崩、MCP server 启动即死
  且无限重启循环、检索永久失败；`import torch` 偶发 `WinError 1455`（页面文件不足）。
- **根因（7 个）**：
  1. **P5（致命）** `index.py` 顶层 `from sentence_transformers import ...` →
     **任何 import index 的进程（含 MCP server 启动）都会加载 CUDA DLL** →
     内存不足时启动即死，进程层"无法自动修复"。
  2. **P1** `get_model()` 设备一次性探测（`cuda if available else cpu`），
     `_model` 缓存后永不更换 → 无任何切 CPU 机制。
  3. **P2** 索引/检索编码裸奔（`model.encode` 无 try/except、无 batch 控制），
     一次编码全部新块；且 CUDA OOM 后驱动上下文损坏，同模型对象继续每次崩溃。
  4. **P3** `kb_stale` 只校验 Vault 指纹，不校验 Chroma↔meta 一致性 →
     `--full` 中途被杀（清库窗口）后 0 块 + meta 完好 + 文件未变 → **永不自动重建**。
  5. **P7** 即使发现 0 块，增量路径指纹全命中 `new_ids` 为空 → 0 块保持 0 块。
  6. **P4** server 工具层无兜底，异常直接上抛。
  7. **P6** CPU 降级内存也不足：bge-m3 fp32 ≈2.3GB，需 fp16 减半。
- **方案（已实现）**：
  1. **延迟导入**：`sentence_transformers` 移入 `_build_model()` 内，import index/server 不碰 torch。
  2. **设备状态机**（`index.py`）：CUDA 初始化/编码失败 → 进入 **5 分钟冷却**（`_cuda_cooldown_until`，
     进程内，非硬性窗口）→ 冷却到期后每次调用先做**毫秒级显存探测**（`_cuda_probe`，分配 64MB
     tensor），通过即用 CUDA、失败再冷却；`fallback_to_cpu()` 同进程内卸载 CUDA 模型 + `empty_cache()`；
     `device_state.json` 仅作诊断落盘（失败原因/时间），不再做门禁。
     **显存恢复后自动切回**：CPU 模型缓存期间，每次请求探测通过即 `_try_switch_back_cuda()`
     （先释放 CPU 模型避免双模型内存峰值，失败回滚 CPU 并冷却）——无 24h 硬等待。
  3. **`encode_safe()` 统一入口**：捕获 OOM/页面文件/内存类错误（`_is_memory_error` 匹配
     "out of memory"/"paging file"/1455 等）→ 自动降级 CPU 重试一次；非内存错误不降级；
     CPU 也失败才抛（不可恢复）。索引（`index_vault`）与检索（`hybrid_search`）共用；
     encode 显式 `batch_size=32`（检索 1）。
  4. **CPU fp16**：`_build_model("cpu", fp16=True)` 减半内存（失败回退 fp32）。
  5. **一致性自愈**：`kb_stale` 增加 `_chroma_count()` 校验（meta 块数 vs Chroma 实际 count，
     不加载模型、毫秒级），不符 → stale → 自动重建；`index_vault` 检测
     "meta 有数据但 Chroma 空" → 清空 meta 强制全量重嵌（修 P7 的增量空转）。
  6. **server 兜底**：两个工具 try/except 返回明确提示文本（旧索引仍可用），不崩。
- **验证**（全部通过）：
  - 导入链：`import server` 后 `sys.modules` 无 torch/sentence_transformers。
  - 状态机 9 组单测：冷却期不探测/探测失败重冷却/到期探测通过即就绪/切回成功/切回失败
    回滚+冷却/缓存路径冷却期零探测/自动切回/encode OOM 降级。
  - **真实场景**：CUDA 真 OOM（978MiB 分配失败）→ 日志降级 → CPU fp16 加载 1s →
    中文查询命中正确（21.6s 首查，`[块 k/N]` 标记正确）→ 增量 reindex 成功（973 块，
    kb_stale 归零）；显存恢复后清冷却 → 探测通过 → **直接加载 CUDA 成功**（无需等窗口）。
  - 崩溃恢复：系统级崩溃后 MCP server 自动重启加载新代码（112MB 未预载模型 vs 旧版 782MB）。
- **环境提醒**：本机内存长期紧张（空闲常 <2GB）+ C 盘告急会诱发 WinError 1455；
  模型缓存需保持完整（HF 磁盘不足警告时勿删 `~/.cache/huggingface` 下 bge-m3）。

---

## 问题 12：RAG 数据跨机器分发（导出/导入工具 + Linux 支持）（2026-08-06 完成）

- **背景**：`data/` 被 .gitignore，GitHub 同步不携带索引；虚拟机（Ubuntu）上跑
  RAG 需要重新全量嵌入（28s+）且依赖网络与 GPU。需求：导出一个文件，接收端
  一条命令秒级恢复索引（不重算嵌入），兼作测试靶子数据（含完整性自检）。
- **交付**：
  - `export.py`：自动刷新（stale 才增量重建）→ chromadb API 读全库（不加载模型，
    持 index.lock）→ 打包 zip：`payload.jsonl.gz`（id/text/embedding/meta，JSON+gzip）
    + `vault/` 源文件 + `index_meta.json` + `manifest.json`（逐文件 sha256）
    + `AI_GUIDE.md`（注入本次导出信息）→ **导出自校验**（把自己当接收方解包比对）→
    `data/export/` 只留最近 3 个。输出固定 `data/export/obsidian-rag-export-*.zip`（9.4MB/973块）。
  - `import.py`：两阶段防半成品——先 CRC+sha256 全量校验（任一失败中止、目标零改动），
    通过后才：交互确认覆盖（`--yes` 跳过，AI 非交互必加）→ 备份 index_meta →
    持锁 delete+重建+分批 upsert（500/批，纯向量重插）→ count 校验 + 随机 3 块
    embedding 余弦一致性 → 原子写回 index_meta → `vault_export/` 落位 →
    包移入 `data/archive/` → 清临时区。重跑幂等。
  - `AI_GUIDE.md`：AI 自动执行引导（仓库模板 + 包内注入版），含 Ubuntu/Windows 双平台
    命令、验证点、失败分支决策表、只读靶子数据用法。
  - `requirements.txt`：chromadb 1.5.9 / mcp 2.0.0 / sentence-transformers 5.6.1 锁版本，
    torch 平台命令注释（Windows cu128 源 / Linux 默认源）。
  - `tests/verify_export_import.py`：端到端验证套件（39 项断言，标准库无 pytest）。
- **代码改动（最小面）**：
  1. `index.py` 跨平台文件锁：`import msvcrt`（Windows 专属，Linux 上 `import index` 直接崩）
     → `_IS_WINDOWS = os.name == "nt"` + `_lock_acquire/_lock_release` 函数内按平台局部导入
     （msvcrt 字节锁 / fcntl flock），`write_lock()` 接口不变。**Windows 分支逐字保留**。
  2. `index.py` VAULT 环境变量覆盖：`VAULT = os.environ.get("OBSIDIAN_VAULT", 原路径)`，
     本机行为不变；接收端指向 `vault_export/` 防"文件全删"误判清库。
- **过程中发现并修复的 bug**：
  1. numpy 数组 `or []` 触发 truth-value 歧义（export 读库、import 自检两处）。
  2. import 抽查取样逻辑写岔（会积累数百条）→ 引用池 + `random.Random(7).sample(3)`。
  3. 测试脚本对 gzip 二进制 count 行数 → 先 `gzip.decompress`。
  4. **精度陷阱（关键）**：旧 chroma 结构库 embedding 存 float64 原值；新库对 cosine
     空间做**归一化 + float32 存储**（cos=1 但值差 ≤1 ULP，2.98e-8）→ "逐位比对"必然
     失败。自检判据改为**余弦相似度 ≥ 1-1e-6**（检索排名的本质等价判据）。
  5. import 把传入包移入 archive（接收方语义正确）→ 测试脚本改用归档后的包继续后续组。
- **验证**：39/39 全过——回归（import 链/锁/指纹/幂等/检索）、导出 12 项、
  副本导入 9 项（count/meta/vault/归档/清理/中文文件名往返）、覆盖与交互取消、
  **损坏注入**（payload 篡改与包截断 → 中止且目标索引零改动）、留 3 个、**端到端
  检索对比：真库与副本 top-3 结果逐字符一致**。
- **遗留/风险**：
  - fcntl 锁分支无法本机（Windows）实测，交付 AI_GUIDE §5 Linux 验收清单
    （`import index` 无 msvcrt 报错 → 全流程导入 → 首查触发 bge-m3 下载属预期）。
  - 本机 CUDA 仍被 SAC 拦截 `_argkmin` DLL（sklearn 扩展）→ 实测走 CPU fp16 兜底，功能不受影响。
  - 导入库 embedding 存储为 float32+归一化，与原库 float64 检索结果等价（余弦判据保障）。

## 问题 13：残留实例持锁死等 + 无进度可见性（2026-08-07 修复）

- **背景**：某次会话结束后遗留两个 `server.py` 孤儿进程（17:16 启动），持续持有
  `index.lock` 的 msvcrt 字节锁；新会话实例（20:07）在 `write_lock()` 上无限阻塞，
  所有 MCP 调用 30s 超时、进程 CPU 归零看似死机 15+ 分钟。同时索引期间毫无
  进度可见性——MCP 调用超时后既不知道任务在跑、也不知道还要多久、无法判断卡死。
- **交付**：
  1. **写锁超时 + 持有者定位（index.py）**：`_lock_acquire` 改非阻塞轮询
     （`LK_NBLCK` / `LOCK_NB`，0.5s 间隔），`LOCK_TIMEOUT_SECONDS=60` 超时抛
     `LockBusyError`（含持有者 PID 与处置建议）。锁文件记录持有者 PID；
     超时后若持有者已死则清锁重试一次兜底。**不再无限死等**。
  2. **进度文件（index.py）**：`data/index_progress.json`（原子写）——阶段
     （scanning/embedding/writing/done/error）、文件 done/total、块 done/total、
     耗时、ETA（按块进度线性推算）、心跳 `updated_at`。约定：running 时心跳
     超 30s 视为疑似卡死。嵌入分批（`EMBED_BATCH_SIZE=64`）逐批更新心跳。
  3. **后台索引（server.py）**：`reindex_knowledge` 改后台线程立即返回；
     新增 `index_status` 工具输出进度文本（含 ETA 与卡死告警）；`ensure_fresh`
     检测到变更时后台更新、用旧索引先出结果（索引为空的首跑场景例外，同步等），
     杜绝"索引几分钟 + MCP 30s 超时"假死。
  4. **并发防护（index.py）**：`encode_safe` 加线程锁（模型不可重入：后台索引
     线程与检索并发编码会崩）；`server.py` 对残留 running 标志按心跳时效判定
     （超 2×30s 视为死进程残留，不挡新任务）。
- **过程中发现并修复的 bug**：
  1. `_lock_record_holder` 写入 PID 后文件指针未归位 → `msvcrt.locking` 按当前
     指针解锁 ≠ 锁定位置 → PermissionError 覆盖正常流程（解锁前强制 `f.seek(0)`）。
  2. `write_lock` 的 finally 无条件释放锁 → 超时未获锁时也调 UNLCK 抛
     PermissionError 掩盖 `LockBusyError`（改为仅 `acquired=True` 时释放）。
  3. 测试污染：临时 vault 测试写入了真实 Chroma/meta（40 条目）→ 用真实 vault
     增量重建自动清理（"清理 40 个失效块"，块数精确回到 1185）。
- **验证**：
  - 锁竞争：A 持锁 8s，B 超时 3s → 抛 `LockBusyError`（含持有者 PID 与建议），
    无 PermissionError 掩盖、无死等。
  - 进度：真实全量重建中实时轮询 `index_progress.json` 见 "嵌入 768/1185 块"，
    完成 phase=done（1185 块，149s）。
  - 端到端 MCP：stdio 会话 `tools/list` 三个工具齐；`index_status` 显示 done 详情；
    `reindex_knowledge` 秒回（后台启动 PID）；搜索在索引期间不阻塞、结果正确
    （新路径 10-Areas/... 命中）；最终 index_status 干净收束。
- **使用方式（AI/人）**：`reindex_knowledge` 或自动同步启动后，轮询 `index_status`
  看阶段/进度/ETA；心跳 >30s 且 running → 告警并建议查 PID 处置；遇到
  LockBusyError → 按提示结束残留进程重试。
- **补充（自适应卡死基线，2026-08-07）**：初版 `PROGRESS_STALE_SECONDS=30` 是固定
  阈值，慢机器（CPU 嵌入单批可达 1-2 分钟）会正常干活被误报卡死。改为自适应：
  每次 `update_progress` 观测"距上次心跳间隔"，取本次任务最大值 `max_gap_s`；
  卡死阈值 = `max(30s, max_gap_s × 2)`——快机器（CUDA 基线 ~8s）16s 无心跳即警，
  慢机器（基线 ~90s）放宽到 180s 不误报；首次模型加载期（无基线）附带"任务早期
  可能属正常"提示。`server.py` 的残留 running 判定同用自适应阈值。
  验证：6 场景矩阵（快/慢 × 正常/卡死 + 加载期）输出全部正确。
- **补充 2（定时心跳 + 双重判定，2026-08-07 v3）**：自适应基线本质是"事件驱动心跳
  （间隔=硬件性能）"的补偿，规则复杂。用户建议改为固定频率心跳后重构：
  - **独立心跳线程**每 `HEARTBEAT_INTERVAL=5s` 从内存状态强制写盘（刷新 updated_at，
    不动 last_advance_at）；事件更新（批次完成/阶段切换）仍立即写盘推进进度。
    心跳间隔与硬件无关——模型加载期、单批嵌入期间心跳照常走，心跳停止即真异常。
  - **双重判定**（固定阈值）：心跳停 >15s（3×5s）→ 疑似卡死；心跳正常但进度
    （块/文件数）>25s（5×5s）未推进 → 疑似批次内卡死（假活）。
  - `EMBED_BATCH_SIZE` 64→16（进度数字跳动密度 = 停滞检测粒度，吞吐代价可忽略）。
  - 移除 max_gap_s 自适应基线（语义被定时心跳取代）；`server.py` 残留判定回归
    固定 `HEARTBEAT_TIMEOUT`。
  - 验证：三场景全过——事件推进（心跳/ETA/推进时间显示正常）；26s 无推进精确
    触发停滞告警、恢复推进即消除；伪造 60s 前心跳触发卡死告警（含加载期提示）；
    真实运行（50 块/16.6s）模型加载期心跳每 5s 刷新可见。

---

## 问题 14：8GB 卡共享显存溢出 → 400 倍减速假死（2026-08-10 修复）

- **背景/现象**：全量索引后期，嵌入批次从秒级退化到约 15 分钟/40 块；GPU 100%
  占用但功耗仅 38W（正常编码 70-113W）；进程不报错、心跳正常推进，MCP 状态
  显示"疑似卡死"却永不结束。显存监控：nvidia-smi 7742/8151 MiB（94.9%），
  Task Manager 显示独占 6.8GB + **共享显存 5.4GB**（RTX 5060 Laptop，8GB）。
- **诊断过程**：
  1. 索引结束后 GPU 空闲仍占 7742 MiB（fp32 模型仅 2.3GB）→ 缓存分配器池
     只进不出（代码无 `empty_cache`），高水位残留常驻。
  2. 峰值分配 ~12.2GB > 物理 8.1GB → **Windows WDDM 显存溢出不报错，静默
     排入共享显存（系统内存）**：CUDA 层分配永远"成功"，`_is_memory_error`
     兜底永不触发 → 原有 CPU 降级机制对这类故障完全失效。
  3. 带宽崩塌是减速机理：GDDR7 ~375GB/s vs DDR5 系统内存 ~60GB/s 差约 6 倍，
     每批张量横跨两块内存 + 页迁移抖动 → 400 倍减速、功耗爬行（38W）。
  4. 心跳监测盲区："假活"检测只认"进度完全不推进"（>25s），识别不了
     "推得极慢但没停"的病态模式。
- **根因（3 个代码缺陷）**：
  1. CUDA 路径未开 fp16（CPU 路径反而开了）→ 权重 2.3GB + 激活值双份精度。
  2. 批间从不释放显存池 → 池子只涨不缩（空闲也占 7.7GB + 5.4GB 共享）。
  3. `embed_batch_size=16` 过大 → 单批激活峰值把总需求顶破 8.1GB 物理显存。
- **修复（index.py + config，全部本次落地）**：
  1. **CUDA 启用 fp16**（`_load_model`，与 CPU 统一：fp16 优先、失败回退 fp32）：
     权重 2.3→1.1GB、激活值减半。质量损失可忽略（检索指标差 <0.3%，
     CPU 路径此前已在用 fp16）。
  2. **`_release_cuda_cache()`**（新增 helper）：`encode_safe` 每次编码后
     `torch.cuda.empty_cache()`（finally 保证执行），嵌入循环结束再收一次——
     分配器高水位不再残留（空闲 7.7GB→~1.9GB）。
  3. **`embed_batch_size` 16→8**（`data/config.json` 运行时配置 +
     `config.py` DEFAULTS/模板 三处同步）。
  4. **慢批看门狗**（`encode_safe` CUDA 路径）：单批耗时 >30s → 告警计数；
     连续两批仍慢 → 主动 `fallback_to_cpu()`（复用既有降级机制）。WDDM 永不
     抛错，耗时是唯一信号——这是系统识别病态模式的唯一机制。
  5. **自动批次 `_auto_batch_size()`**（新增）：每次编码前按
     `torch.cuda.mem_get_info()` 实时可用显存收紧批次：
     `cap = (free − 3.0GB 固定开销) ÷ 0.4GB/块`，钳制 [2, 32]，配置值仅作
     上限；收紧时打日志。校准参数来自全量实测（fp16 模型+上下文+工作区
     ≈3GB；1500 字长块 ≈0.45GB/块取 0.4 留余量）。
- **决策记录**：
  - 自动批次上限**维持 32**：曾提议收紧到 12，评估后判定 8GB 卡由公式天然
    压在 7-8（free≈5.8GB → (5.8−3)/0.4≈7），32 上限仅在大显存卡上生效，
    无实际风险，维持 32 不改。
  - 修复顺序决策：先落地 3 项显存修复（fp16 / empty_cache / batch 8），
    看门狗与自动批次为增强层；三者可独立回滚。
- **验证**（真实 vault 全量重建，155 文件 / 1544 块，监控 1s 采样）：
  - 总耗时 37.9s（扫描 ~2s + 嵌入 ~32s + 写库 ~3s）；对比修复前"40 块
    ~15 分钟"（约 350 倍提速）。
  - 嵌入期 GPU 80-100% util、功耗 70-113W（满血运行）；峰值显存 5.5GB
    （纯物理，结构上不可能再溢出共享）；空闲回落 ~1.9GB。
  - 看门狗零告警、无 OOM、无降级；数据一致性 meta 155 文件/1544 块
    == Chroma count 1544。
  - 合成压测：1600 块 7.6s（209 块/s）；批间 empty_cache 开销可忽略。
  - 自动批次三场景：正常 free 5.8GB→批 7；模拟占用 4GB（free 3.8GB）→
    批 2 且编码正常；释放后自动回升到批 6-7。
- **操作记录**：
  - 旧 MCP server 进程（旧代码 + 7.7GB 常驻）被终止，opencode 需重启重连
    MCP 工具；新 server 加载 fp16 模型，空闲占用直接降至 ~1.7GB。
  - 期间 vault 大规模重组（152 文件迁移至 PARA 结构），增量索引实际重嵌
    1544 块（近似全量）；Home.md 原地修改。
  - 全量重建后 Chroma 块数 1352→1544（含用户新增 6 个文件，其中 3 个
    24-50KB 转录长文，贡献 ~173 块）。
- **已知遗留（观察项，暂不处理）**：`update_progress` 原子写（tmp +
  os.replace）与并发读进度文件的进程存在 Windows 写竞态，碰撞时打
  `WinError 5` 告警但被吞掉、不影响运行；频率低且无害，留观。

---

## 问题 15：wiki 引用标签污染检索 + 切块逻辑版本化（2026-08-10 修复）

- **现象/需求**：vault 63% 文件含 wiki 链接（150/239），纯引用 `[[机器]]` 的
  标签文本原样进入嵌入与 BM25 → 搜"机器"会命中仅引用它的文件（如"如何制作
  蛋糕.md"引用"机器.md"），而非内容文件；路径型链接
  `[[../../20-Projects/.../AGENTS|别名]]` 的相对路径垃圾（英文目录名）也进向量。
- **决策记录（用户拍板）**：
  1. 无别名纯引用 `[[目标]]` / `[[目标#标题]]` / `[[目标#^块]]` / `![[嵌入]]`
     → **彻底去除**（消除引用噪声）。
  2. 带别名 `[[目标|别名]]`（含表格转义 `\|`）→ **保留别名**（Obsidian 中
     别名 = 读者实际看到的文字，有信息量）。
  3. 统一规则，MOC 等链接密集文件不加特例（简单可预期）。
- **修复（index.py）**：
  1. **`clean_wikilinks()`**：正则 `!?\[\[([^\]]*)\]\]`；`\|` 先还原为分隔符
     再按 `|` 切分（处理表格转义）；别名 strip 后返回，否则返回空串。
     在 `extract_frontmatter` 之后、切块之前统一清洗——索引与 BM25 共用
     同一份清洗后文本。
  2. **`META_VERSION = 2` 版本化**：`save_meta` 写入 `_version` 字段；
     `index_vault` 加载时版本不匹配 → 强制全量重嵌。**动机**：文件指纹
     （mtime+size+md5）感知不到代码升级，此前若改切块逻辑，增量索引会
     静默沿用旧文本；版本化让未来切块迭代自动触发重建。
- **验证**：
  - `clean_wikilinks` 14 个模式单测全过（别名 / 表格转义管道 / #标题 /
    #^块 / !嵌入 / 相邻链接 / 空链接 / 无链接文本）。
  - 全量重建 39s（监控 1s 采样），**1536 块**（原 1544，清洗后 8 块跌破
    切块阈值，符合预期）；一致性 meta 155 文件 / 1536 块 == Chroma 1536；
    `_version: 2` 落盘。
  - 库内 **0/1536 块残留 `[[` 语法**；**0 个文件被清洗成空块**（链接密集
    的 MOC 文件仍有标题/描述内容）。
  - 别名保留抽查全 OK：Home.md "TODO 待办总台"/"MOC-Aerospace"、
    MOC-AI.md "暑期项目 · AI Knowledge System"。
  - **引用对行为**：MOC-AI.md 原含无别名 `[[MOC-Engineering]]` → 重建后
    该文本 0 命中，搜 "MOC-Engineering" top5 无 MOC-AI.md（痛点场景解决）；
    带别名引用（MOC-Ideas.md）按决策 2 保留显示文本，仍可命中（符合设计）。
  - 附带收益：检索结果展示文本不再含 `[[...]]` 语法垃圾。
- **监控发现（全程 1s 采样，均非 bug，记录在案）**：
  - 空闲显存 1.9→4.0GB：两个常驻 MCP server 均已懒加载 fp16 模型
    （各 ~2GB：权重 1.1 + 上下文）——第二次实例是中途某次搜索触发的，
    正常；自动批次自适应 cap 7→6，峰值 7076 MiB < 8151，无共享显存溢出。
  - `WinError 5` 竞态复现 3 次（问题 14 已知遗留；`_write_progress_file`
    已捕获并按设计记录，无影响，仍留观）。
  - **噪声修复**：自动批次原每批都刷"收紧为 N"日志（本次 ~100 行）→
    改为仅 cap 值变化时记录一次；`--full` 时版本升级日志不再误报
    （空 meta 无 `_version`，之前每次全量都打）。

---

## 问题 16：meta 顶层 `_version` 破坏遍历 → 自动同步失效（2026-08-10 修复）

- **现象**：搜索时每次返回"检测到 Vault 变化，但自动更新索引失败：
  'int' object has no attribute 'get'；以下为旧索引结果"。
- **根因**：问题 15 把 `_version: 2`（int）作为顶层字段写入 meta——
  原本 meta 的字典语义是"键=文件路径，值=条目 dict"，`_version` 破坏该
  语义。三处遍历全中雷（`meta.items()` / `meta.values()` 遇到 int 调 `.get` 崩溃）：
  1. `kb_stale` 一致性校验（`index.py:691`）：`sum(info.get("chunks") for info in meta.values())`
  2. `kb_stale` 已删文件统计（`set(meta) - seen`）：即使不崩，`_version` 键
     也会被算作"已删文件"→ `removed=1` → **永远 stale、每次搜索都触发重建**
  3. `index_vault` 空库自愈日志（832）与失效块清理 valid 生成（939）
- **为什么问题 15 验证时没爆**：验证走 `--full`（meta 先清空再重写）+ CLI
  脚本直连检索，不经过含 `_version` 的 meta 遍历路径；MCP server 当时仍是
  旧代码。本次 opencode 重启后 server 加载新代码，`ensure_fresh → kb_stale`
  首次触发即崩。
- **修复（index.py，最小面）**：三处遍历加 `isinstance(x, dict)` 守卫；
  removed 统计改为先过滤出 dict 条目（`meta_files`）再减 `seen`。
- **验证**：`kb_stale` 返回 `stale=False, stats 全 0`（不再误报）；
  832/939 已由同一守卫覆盖，`py_compile` 通过。
- **遗留（长期可选）**：守卫只治标。根治是把 meta 文件改为嵌套结构
  （`{"meta": {...}, "_version": 2}`），需迁移兼容旧 meta——当前守卫 + 版本化
  已足够，暂不做。

---

## 问题 17：桌面可视化控制台（GUI）（2026-08-11 完成）

- **目标**：独立桌面窗口（非浏览器）展示索引状态并支持手动触发，新手友好。
- **技术选型**（与用户三轮讨论敲定）：Flet 0.86（Python + Material 3，
  纯 Python 免 Node）；方案比对了 Qt（颜值上限低）/Electron（重且需 Node）
  /Flet（现代 + 全 Python）。
- **设计**：`docs/specs/2026-08-11-gui-design.md`（经 UI 设计子代理产稿 +
  用户确认，深色监控台 + Teal 主色）。
- **架构（零侵入）**：GUI 进程只读现有 progress/meta 文件 + spawn 索引
  子进程（`python index.py [--full]`），常驻不占显存；仅"搜索测试"临时
  加载模型（走 encode_safe 降级）。与 MCP server 并存靠现有 index.lock。
- **功能**：KPI 卡（文件/块/耗时/三态状态）、进度条+心跳灯（复用现有
  卡死/假活判定）、实时日志（ERROR/WARNING 着色）、增量/全量按钮
  （全量有确认框、默认焦点取消）、搜索测试（复用 hybrid_search）、
  设备条、深浅主题切换、打开文件夹。
- **交付**：`gui/`（app/theme/store/worker/widgets 5 模块）+ 测试
  （`tests/test_gui_store.py` 7 例 + `tests/smoke_gui.py` 无窗口冒烟）。
- **踩坑记录**（flet 0.86 API 变化大，均已在 smoke 测试覆盖防回归）：
  1. `ft.padding.symmetric` → `ft.Padding.symmetric`；
  2. `CrossAxisAlignment.BOTTOM` → `END`；
  3. `ft.alignment.center_left` → `ft.Alignment.CENTER_LEFT`；
  4. `ft.app()` 废弃 → `ft.run(main)`；
  5. `Page.close()` 不存在 → 对话框用 `open=False + update()`；
  6. 按钮 `style=None` 时不能赋子属性 → 构造时传 `ButtonStyle(text_style=...)`；
  7. **教训**：勿用 PowerShell `Get-Content/Set-Content` 批量改含中文的
     UTF-8 文件（ANSI 解码→乱码→写回=损坏且不可逆，三个文件重建）；
     中文文件修改一律用 UTF-8 感知的编辑工具。
- **手动待验证**（自动化难以覆盖）：搜索测试的真实点击流、确认框按钮、
  全量重建全流程、主题切换观感——由用户跑 `python gui/app.py` 体验。
- **v2 迭代（2026-08-11）**：用户反馈 v1 中排"高度塌陷"（进度卡+搜索卡
  无高度锚点被压成 0，心跳灯/搜索框/进度不可见）+ 审美不足 → 经 UI 子代理
  重新设计并实施：
  - **布局纪律**：全窗口仅日志区弹性，其余区块固定高度锚点——header 48 /
    KPI 92 / **中排 280**（进度卡+搜索卡 expand 横向均分）/ 设备条 40；
    冒烟测试新增 `mid_row.height == SIZE["mid_h"]` 断言防回归。
  - **视觉**：深空灰三层底（#0E1117/#161B23/#11151C）+ 薄荷青
    `#4BCEB8`（浅色主题同构换色）；三字体体系（YaHei UI/Bahnschrift 大
    数字/Consolas 等宽）。
  - **组件重写**（theme.py token 化 + widgets.py 全量重写）：KPI 卡带图标
    + accent 短横线；心跳胶囊移至 header 右上（运行呼吸/红卡死/橙假活/
    绿完成/弱空闲）；进度卡 4 阶段 stepper + 36px 百分比 + 双行
    计时/ETA + 块计数；搜索卡 3 层结构（输入/状态条/结果列表）。
  - **耗时卡语义修正**（用户确认）：运行中=实时「mm:ss」+ 阶段名；未索引
    =「—」；其余=上次完成耗时（≥60s「x分y秒」/<60s「x.xs」）+「上次完成
    HH:MM」。
  - **踩坑新增**：`ft.Colors.with_opacity(opacity, color)` 参数顺序是
    **透明度在前**（写反会 `'<=' not supported`，widgets 曾 21 处反序）；
    Windows 控制台 cp1252 打中文会崩 → smoke 测试入口强制
    `sys.stdout.reconfigure(encoding="utf-8")`。
  - **v2 验证**：9 单测（+format_elapsed/format_mmss）+ 无窗口冒烟（含锚点
    断言）+ 真窗 20s 零 traceback（测试后已关闭）。
- **遗留（Roadmap 候选）**：exe 打包（`flet pack`）、主题持久化
  （当前会话内切换，未写入 config.json）、显存占用实时展示（progress
  无该字段，需加后端字段）。

---

## 问题 18：检索质量大修——标题链入文本 + 表格/列表绑定 + 重排器 + 顺序 bug（2026-08-11 修复）

- **现象**：用户搜"个人能力""fluent配置"能定位到正确文件（如
  `FLUENT配置与求解设置.md`），但返回段落质量差、正文块错位；并质疑
  "湍流模型/网格策略与 fluent 配置语义相关，为何排不上去"。
- **诊断（数据实测）**：
  1. **标题不参与检索**（根因）：`split_by_headings` 把标题单独存 metadata，
     嵌入文本与 BM25 只吃正文。Obsidian 笔记"标题=主题浓缩"的强判别信号
     被系统性丢弃 → 词面盲区（"个人能力"21 块 / "fluent"8 块标题命中但
     正文不命中）；dense 侧 FLUENT 干货块（湍流模型/流体域，多为表格）
     相似度仅 0.39-0.56，**低于无关叙述块**（MOC-AI 0.60）。
  2. **表格转述实验无效**：把 `| a | b |` 表格转成 "a: x; b: y" 再嵌入，
     sim 几乎不变（0.41→0.40）——表格体不是病因，标题缺失才是。
  3. **加标题链立竿见影**：`FLUENT配置与求解设置 / 1. 湍流模型...` 拼入后
     三个干货块 sim 0.39-0.56 → **0.59-0.62**，反超所有无关块。
  4. **顺带挖出系统性展示 bug**：Chroma `get(ids=...)` 返回顺序**不保证**
     与传入 ids 一致，而 `_format_result` 用 `zip` 按返回顺序输出 →
     **算法排序正确但展示顺序被静默打乱**，此前所有验收的 pos 数据全被
     污染。重排器评估因此一度失真（分数错位）。
- **修复（index.py / retriever.py / config.py）**：
  1. **标题链拼入块文本**：`split_by_headings` 维护 H1>H2>H3 嵌套路径
     （如 `FLUENT 配置与求解设置 / 2.1 流体域`），每块文本 =
     标题链 + 正文（嵌入与 BM25 同受益）；metadata 存 `hp` 供输出剥离。
  2. **停用 `make_anchor` 中文锚点**（ADR-3）：使命被标题链完全覆盖
     （标题本就含中文词）；anchor 字段退役。
  3. **表格绑定上下文**：表格段落并入直接上文（引导句）+ 直接下文
     （解释/结论），含表格的块整体保留（宁大勿断），消除表格裁断与
     孤立 `---` 残块。
  4. **列表保护**：连续列表项跨空行合并（`任务清单.md` 从 9 个单行块 →
     2 块）；超长列表按**列表项边界**切，永不从项中间剪断
     （`is_list_block` / `split_list_block`）。
  5. **两阶段精排**：dense+BM25 融合 top `rerank_candidates`（默认 10）
     → bge-reranker-v2-m3 cross-encoder 对 (query, 块) 逐对精排 → top_k。
     懒加载、失败自动降级纯融合；`rerank_enabled` 可关。
  6. **`_format_result` 顺序修复**：按 cid 映射后**严格按 ranked 顺序**
     输出（修复 Chroma 乱序假象）。
  7. `META_VERSION` 2 → 3（标题链）→ 4（表格/列表），自动全量重嵌。
- **评估**（5 组查询 × 黄金文件：fluent配置/个人能力/y+控制/网格无关性/OfficeCLI）：
  - 顺序 bug 修复前：top1=2/5、top3=3/5（乱序假象）
  - 修复后纯融合：**top1=3/5、top3=5/5、top5=5/5**（y+控制 4→1、网格 4→2）
  - +重排器：与纯融合持平（融合已够好，重排器兜底模糊语义查询）
  - 表格裁断 0 对；列表 9 对拆散 → 0 真裁断；块数 1559 → 1462
- **验证**：smoke_gui / test_gui_store / test_config_editor（新增
  rerank 配置项 + bool 类型支持）/ verify_export_import 全过；真窗 12s 零
  traceback；GUI 设置页新增 rerank_model / rerank_candidates / rerank_enabled。
- **配置**：`data/config.json` 检索段新增三键（重排模型名、候选数、总开关）；
  重排器首次加载需下载模型 ~1.1GB（一次性）。



## 问题 18：多库支持（一个注册表管理多个 RAG 库，跨库检索）

- **需求**：原系统单 vault → 单索引；用户希望任意 md 文件夹可单独注册为 RAG 库，
  检索时可选单库 / 多库并查 / 全部（默认）/ 反选（全选排除），且每库独立配置。
  核心约束：AI agent 可无歧义调用（先枚举再选库，未知库名报错）；暂不做 GUI；
  PDF/docx 等格式只留扩展位（`extensions` 字段），嵌入模型保持全局（union 检索
  要求同一向量空间）。
- **方案**：单 Chroma 实例多 collection（每库一个 collection + 按库 BM25 缓存），
  否决"每库独立数据目录"（N 个 PersistentClient 开销翻倍、物理隔离本机无用）。
- **实现**：
  1. `library.py`（新）：`data/libraries.json` 注册表增删改查（`list/add/remove/config`）
     + 生效配置合并（null=继承 config.json 全局）+ `resolve_entries` 白名单减法选库
     （未知库名/空集报错并列出可用库）。
  2. `index.py`：`_index_core` 参数化（collection/指纹文件/排除/切块/扩展名按库），
     `index_vault` 保留为 legacy 入口；`index.py --library <名|all>`；进度含 `library` 字段。
  3. `retriever.py`：BM25 全局单例 → 按 collection 缓存字典；跨库重排池（各库融合
     top rerank_candidates 进全局池，cross-encoder 纯文本打分统一跨库分数）；
     重排不可用降级按库归一化合并；结果 `[来源] <库名>/<相对路径>`（同名文件不歧义）；
     同文件封顶键改 (库, rel)。
  4. `server.py`：新增 `list_libraries` 工具；`search_knowledge` 加 `libraries`/`exclude`
     参数（空=全部、"all"=全部、反选=exclude）；`reindex_knowledge(library="")`。
  5. `export.py`/`import.py`：`--library`（默认 all 逐库独立打包）；manifest 记录库名；
     导入目标库未注册可 `--create --path` 自动注册。
  6. 迁移：首次运行自动把旧 vault 合成首个库（collection 沿用 obsidian_kb，
     `index_meta.json` 改名随行，指纹保留**零重建**）。
  7. GUI 最小兼容：`_open_result` 剥库名前缀（多库 GUI 属后续迭代）。
- **踩坑**：① Chroma Collection 对象不可哈希，跨库分组取文档须用库名作键；
  ② `startswith` 不能收 list（effective_config 输出 list，须 tuple）；
  ③ collection 派生名原 strip 尾部 `_`，中英文差异的库名会撞 collection，改保留。
- **测试**：`tests/library_registry_test.py` 8 项（迁移/合并/CRUD/选库语义/空注册表）；
  双库端到端冒烟 11 项（全库/单库/多库/反选/未知名/空集/folder/置信度）全过；
  eval 回归 **top1=4/5、top3=5/5、top5=5/5**（基线 3/5·5/5·5/5，无回退）。
- **文档**：vault 根 AGENTS.md 检索章节 + Obsidian RAG 使用指南加多库选范围；
  AI_GUIDE.md 导入加 `--library`；config 模板注释改指 libraries.json。
- **审计修复**（general 子代理审查后）：`--create` 默认路径先 mkdir；损坏注册表备份
  `.bak` 防覆盖丢失；库路径消失跳过同步保留旧索引（不清库）；BM25 缓存带 count
  快照自愈（外部索引后自动重建）；zip slip 防护（拒绝绝对路径/`..` 条目）；
  选库去重；非法库名条目加载时过滤；collection 覆盖值校验；包名加 hash 防撞名；
  export 单库刷新失败不中断 all 模式；list_summary/_chroma_is_empty 改只读
  get_collection（不产生创建副作用）。单测 8 → 14 项。
- **重排器解包 bug（2026-08-12 定位）**：`rr_scores = reranker.predict([(query, doc_got[(n, c)]) for n, c, _ in pool])`
  中 `pool` 元素是 (name, collection, cid)，解包写成 `n, c, _` 导致 c=Collection 对象
  （不可哈希）→ 每次重排必抛"cannot use 'tuple' as a dict key"，except 静默降级归一化
  合并——**自多库重构起重排路径从未真正运行过**。修复：`for n, _, c in pool`。
  修复后 eval 回到 ADR-7 文档基线 top1=3/5、top3=5/5、top5=5/5（此前 4/5 为降级
  模式的偶然偏优）。防线：`retriever.rerank_failures` 计数，eval 回归检测到即警告。
- **返回截断行边界（表格不拦腰切）**：索引侧"宁大勿断"保留的超长表格块（实测
  OfficeCLI-SKILL.md 单块 4779 字符）返回时被 2000 字符硬切在表格行中间；新增
  `_truncate_at_line`：截断点附近 ±300 字符内找完整行边界收边，行/表格行永不被拦腰切。
- **单例守卫（2026-08-12）**：实测发现 opencode 启动 MCP 时可能连续拉起多个
  server 实例（启动后 1s 内双实例，双份模型常驻 ~4GB + 写锁竞争）。新增
  `singleton.py`：PID 文件 + 存活探测 + atexit 清理，后启动者立即退出。
  实证：opencode 在场实例存在时，新实例被正确拒绝退出；实例退出自动清理 PID
  文件（无残留）。单测 5 项；另发现 opencode 环境会向子进程注入 Ctrl+C
  （KeyboardInterrupt 出现在随机位置，Python 3.14），测试进程 SIG_IGN 免疫。

---

## 问题 19：GUI 多库化（v3）——库下拉 / KPI 双行 / 库管理对话框（2026-08-12 完成）

- **背景**：多库后端（问题 18）落地后 GUI 仍是单库 v2——`store.py` 用单库
  `load_meta()`/`kb_stale(VAULT)`（旧 `index_meta.json` 已改名，实际已失效），
  搜索不带 `libraries`、索引不带 `--library`、打开源文件只剥前缀
  （不同路径的库无法正确定位）。设计文档标注"多库 GUI 属后续迭代"，本次兑现。
- **设计决策（用户确认）**：库选择 = header 下拉（全部库 + 单库）；KPI =
  **双行**（大数字=全库汇总，副行=选中库明细）；库管理 = 独立对话框
  （列库/添加/移除/改配置/打开文件夹），注册/注销走 `library.py` 既有函数，
  与 CLI 行为完全一致。
- **实现**：
  1. `gui/store.py` 重写：按库统计 `meta_stats_for(cfg)`（读 `meta_path(name)`）、
     按库三态 `library_state(cfg)`（`kb_stale` 传库的 meta/collection/排除/扩展名）、
     聚合 `library_snapshot()`（任一 stale→stale；全 none→none；否则 ok）；
     旧 `meta_stats()/index_state()` 改为全部库汇总口径（兼容调用方）。
  2. `gui/worker.py`：`start(full, library="")` → 子进程 `index.py [--library 名]`，
     启动日志带库名。
  3. `gui/widgets.py` 新增 `LibraryPicker`（下拉，全部库+各库名，空注册表禁用）
     与 `LibraryManagerDialog`（库列表含块数/最近索引/覆盖项，添加库=路径+可选名、
     配置=7 个覆盖键留空恢复继承、移除=仅注销/注销并删数据双按钮、打开文件夹）；
     `ProgressCard` 计数行附当前索引库名；`StatusCard` 支持自定义副行；
     `DeviceBar` 附选中库。
  4. `gui/app.py`：header 放下拉 + 库管理按钮；KPI 双行（文件/块=选中库明细副行，
     状态卡=聚合大状态+选中库明细副行，耗时卡附库名）；搜索传 `libraries`；
     增量/全量按钮作用于选中库（全部库=后端默认全库）；全量确认框按目标库报块数；
     `_open_result` 多库感知：来源行拆 `<库名>/<rel>` → 查注册表 → 库路径是
     Obsidian vault（含 `.obsidian`）走 `obsidian://open?vault=<库文件夹名>`，
     任意 md 文件夹库走系统默认打开，旧格式回退主 vault；`_open_vault` 打开
     选中库文件夹。
- **踩坑**：
  1. flet 0.86 `Dropdown` 无 `on_change`（改用 `on_select`）、`TextButton` 无
     `icon_size`/`height`（删参数）。
  2. **双模块陷阱（关键）**：smoke 里 `from app import App` 与 `import gui.app`
     是两个模块对象（sys.path 同时含根目录和 gui/），`patch("gui.app.xxx")`
     对 `App` 方法内的名字不生效（方法引用的是顶层 `app` 模块的绑定）。
     修复：smoke 统一 `import gui.app as appmod; App = appmod.App`。
  3. 测试默认参数引用函数参数（`collection="kb_%s" % name`）→ NameError，改 None。
- **测试**：`test_gui_store.py` 27 项（新增 10 项多库：meta_stats_for 含非 dict
  键、library_state 三态、snapshot 聚合/单 stale/全 none/空注册表、is_library_dir、
  _split_lib_rel）；`smoke_gui.py` 新增 7/8、8/8 步（下拉默认值/库管理对话框构建/
  多库打开系统与 URI 两路/选库参数）；真实窗口 15s 零 traceback。
- **遗留（Roadmap 候选）**：库管理对话框的"添加库"目前为路径文本输入
  （无系统文件夹选择器）；exe 打包；主题持久化（沿用 v2 遗留）。

---

## 问题 19b：GUI 进程残留 + 单例失效（AI 关闭后窗口/CMD 不消失）（2026-08-12 修复）

- **现象**：用户手动点 X 关闭 GUI 正常；但 AI agent（opencode）启动 GUI 后
  "关闭"时，GUI 窗口和 CMD 控制台窗口仍残留，需手动清理。
- **根因**：
  1. **flet 0.86 桌面模式是双进程**：`python gui/app.py` 实际拉起父子两个
     python 进程（实测 32200 父 + 9212 子），`__main__` 在子进程执行。
     AI 只杀父进程 → 渲染子进程 + CMD 控制台残留。
  2. `os.kill(pid,0)` 对 pythonw 子进程探测**误判"已死"**（实测：进程活着
     但探测返回 False）→ 旧版单例守卫失效，重复启动出双实例（4 进程并存）。
  3. 用 `python.exe` 启动必带 CMD 黑窗（控制台程序）；`pythonw.exe` 无窗口。
- **修复**：
  1. **启动用 pythonw**：`pythonw gui/app.py`（无 CMD 黑窗，flet 桌面可跑，
     实测父子双进程同存）。
  2. **`gui/stop.py`（新）**：两层终止策略——①读 `data/gui.pid` 后
     `taskkill /T /F` 整树；②**兜底 WMI 扫描命令行含 `gui/app.py` 的
     全部 python* 进程**（覆盖 PID 文件缺失/误判场景），终止后复查无残留
     并清理 PID 文件。实测：双进程全部清空，PID 文件删除。
  3. **单例守卫重写（app.py）**：弃用 PID 探测，改**文件字节锁**
     （Windows msvcrt / Linux fcntl flock，与 index.py 的 write_lock 同款，
     锁 fd 全局持有防 GC，进程退出自动释放）→ 新实例拿不到锁立即退出。
     PID 文件降级为诊断/兜底用途。
  4. `gui/stop.py` 用法：`python gui/stop.py`（AI 可直接调）。
- **验证**（真实窗口）：
  - pythonw 启动 → 双进程 + PID 文件 → `stop.py` 终止 → 进程树全空、PID 文件
    已清理（实测 6264+23832、32200+9212、34904+36124 三组均全清）。
  - 单例：第一实例在跑时启动第二实例 → 第二实例 2 秒内自行退出，
    进程树保持单实例（2 个 pythonw）；stop.py 后干净。
  - 单测 27 项 + 冒烟 8 步全过。
- **AI 使用约定**：启动 `pythonw gui/app.py`（或 Start-Process 指 pythonw）；
  关闭 `python gui/stop.py`。不要 Stop-Process 单杀 PID。
- **19c 补充（2026-08-12，用户反馈"依旧有进程"）**：残留的其实不是 python，
  而是 **flet.exe**（Flutter 渲染窗口进程）——flet 桌面实际是**三层进程**
  （pythonw 主 → pythonw 子 → flet.exe 窗口）。之前 stop.py 只匹配
  python*/gui/app.py，flet.exe 命令行是 `flet.exe tcp://... <assets>`，父进程
  死亡后成孤儿残留，且 AI 测试期间用 Stop-Process 单杀 python 会制造它。
  **修复**：stop.py 的 WMI 匹配加入 `Name='flet.exe' 且命令行含项目根目录`
  （assets 参数带完整路径）；实测三层进程 22316→24300→20564 一次全清。
  AI_GUIDE.md 新增 §8 GUI 启动/关闭约定（pythonw 启、stop.py 关、禁单杀）。

---

## 问题 20：置信度虚高——第一名恒为 100%，无关内容也显示高置信度（2026-08-13 修复）

- **现象**：排除 Obsidian Vault 后用 agents/skills/test 库搜"FLUENT 配置"，全文毫不相关的块
  也显示 `[置信度 1.00]`（实测连 USER_GUIDE 的"配置与数据位置"都 0.72）；用户对检索可信度产生怀疑。
- **根因**：`retriever.py` 置信度是**相对归一化**——每库融合分除以库内最高分，跨库再除以全局
  最高分（`scores = {k: v / gmax}`）。第一名恒为 1.00，库内没有真相关内容时"矮子里拔将军"，
  弱匹配也被包装成确定命中。排序正确（相对最优），但标签误导。
- **修复**：改为**绝对融合分**：`(dense_weight·1/(1+d) + bm25_weight·b/(1+b)) / (dense_weight + bm25_weight)`。
  dense 距离与 BM25 原始分本就映射到 (0,1)，加权上限 = 权重和，除以权重和即得绝对 0-1 相似度，
  不再除以本轮最高分。检索排序仍按融合分取 top_k（排序保持相对最优，标签变诚实）。
- **验证**：正例（Vault 内搜 FLUENT 配置）召回真内容 `FLUENT配置与求解设置.md` 0.83~0.86、
  `任务流程.md` 0.81、`B3_CFD能力评估.md` 0.80；反例（无 FLUENT 库）最高 0.72 且内容低相关。
  正反例分得开，不再虚高。
- **遗留（认知约束）**：绝对分是"相似度"而非"语义正确性"。0.4~0.7 的块可能只是命中"配置"等
  高频道用词——需结合文件名/标题判断，不能只看数字。已写入 Vault 决策记录 ADR-10。

---

## 问题 21：检索质量全面升级——RRF 融合 / jieba 分词 / 小块索引 / HyDE / 批次机制（2026-08-13）

- **背景**：用户质疑检索质量不佳（相关度低但置信度高、泛化查询命中差），要求研究主流做法并全面改进。
  研究结论（Qdrant/Anthropic/Pinecone/LlamaIndex/arXiv 2024-2026）：加权原始分融合不可靠（量纲不同）、
  候选池应 50-100、块应 ~600 字符+small-to-big、中文 BM25 需 jieba、泛化查询需 HyDE、bge-m3 已落后。
- **改动（5 个提交）**：
  1. **RRF 融合**：dense+BM25 加权(0.6/0.4) → RRF(k=2) 排名融合。旧方案 BM25 无界、b/(1+b)≈1 恒主导，
     dense 语义被废（Qdrant 明确结论）。置信度改为双路排名一致度归一化。
  2. **候选池 10→50**：融合排序有误差，池太小好块进不了重排决赛。
  3. **jieba 分词**：中文 BM25 从纯 2-gram → jieba 词级+2-gram 双通道+停用词。实测"个人能力"→[个人,能力]。
  4. **块 1500→600 + small-to-big**：小块语义纯净；命中碎片块时回填父节全文（[父节全文] 标记）。
     META_VERSION 4→5 全量重建（1759 块）。索引时间 40s（bge-m3）。
  5. **HyDE**（默认关）：本地 LLM（LM Studio qwen2.5-3b）为低置信度查询生成假设文档再检索；LLM 不在线静默降级。
  6. **批次机制修复**（重点）：_auto_batch_size 原 (free-3)/0.4 保守压批次到 6；我初改 (free-1)/0.05 过松放批次到 32，
     导致 Qwen3-Embedding（decoder 架构，attention 显存随 batch×seq² 涨）长块 bs=32 达 7.7GB/8GB，
     WDDM 溢出排入系统 RAM → 页面文件被吃满 + "100% GPU 但 30W" 病态。最终上限固定 8、free<4.5GB 再降 4。
     另修复 index_library 未转发 incremental/full 的 bug（--full 此前从未真正全量重建过）。
- **验证**：12 组评估集（5 原有 + 7 泛化）。bge-m3 与 Qwen3 对比：两者 top1=6/12、top3=9/12 完全持平。
  重建耗时 bge-m3 38s/@1GB 显存 vs Qwen3 ~320s/@4.5-7.7GB → **保留 bge-m3**（重建快、显存安全，评估无差异）。
  失败查询 个人能力（抽象词面零重叠 + B3 笔记缺概括关键词）属笔记质量问题，HyDE 3b 模型生成不稳未能救回。
- **遗留**：bge-m3 → Qwen3 可切换（改 model_name + --full 重建），留了评估对比数据；HyDE 默认关（需 LM Studio）。

---

## 问题 22：深度审计 + 20 项修复——配置回退 / os.kill 杀进程 / 向量污染 / 死键死代码（2026-08-14）

- **背景**：用户要求（1）检查检索、切块、向量化，在约束内提高索引质量；（2）深入 audit 找潜在致命问题，
  先验证真实存在再给清单，不急于修。当前在 Linux 虚拟机（无 GPU、无依赖、9.5GB 磁盘），
  目标工况是 Windows+GPU，需评估验证方式以免搞坏虚拟机。
- **验证手段**：零依赖 stub 沙箱——仓库副本（去 .git/data）+ 注入 chromadb/mcp 最小 stub，
  真实执行仓库代码。切块/分词/BM25/融合/配置/锁/meta 全是纯逻辑，0MB 成本即可验证。
  不装 torch（527MB）、不下模型（bge-m3 2.3GB + reranker 1.1GB），装齐峰值逼近磁盘上限且本机无 GPU。
  Windows/CUDA/GUI 相关结论一律标注"无法在本机验证"。审计报告见 `docs/2026-08-14-index-quality-audit.md`。
- **致命问题（已全部修复，F 编号对应审计报告）**：
  1. **F2 v5 大改被静默回退（最严重）**：`DEFAULTS` 与 `CONFIG_TEMPLATE` 是两份互相矛盾的默认值
     （chunk 600 vs 1500、候选池 50 vs 10）。`load_config` 缺文件时写模板但**返回 DEFAULTS**，
     于是首跑 600/50、第二跑起 1500/10——问题 21 的收益从第二次启动就没了。
     且 `small_to_big` 不在模板里反而一直取 DEFAULTS 的 True，得到最坏组合：1500 大块 + 小块专用的父节回填。
     导出包不含 config.json，**每一份分发副本必然踩中**。
     修：模板值/注释对齐 DEFAULTS（保留全部手写注释）、`template_consistency_errors()` 断言出厂种子、
     `load_config` 对既有 config.json 幂等补写缺键（老机器永远拿不到新键的问题一并解决）。
  2. **F5 `os.kill(pid,0)` 在 Windows 上是终止进程，不是探测存活**：CPython 对非 CTRL_* 信号一律
     `OpenProcess`+`TerminateProcess`。三个调用点：单例守卫会杀掉正在服务的 server 然后自己也退出（一个不剩）；
     锁超时会在 Chroma 写一半时杀掉持锁进程；**GUI 主循环每 1 秒调 `index_busy()`**，会杀掉自己拉起的索引子进程。
     `gui/app.py:47` 早有注释"实测 os.kill 对 pythonw 误判已死导致重复实例"——双实例正是这个 bug 造成的，
     不是它在解决的问题（另一分支 OpenProcess 失败 → 误判已死 → 两个都跑）。
     修：Windows 改 `OpenProcess(SYNCHRONIZE)`+`WaitForSingleObject(0)` 只读探测，singleton 复用同一实现。
  3. **F1 jieba 不在 requirements.txt**：`tokenize` 内裸 import，新机器按 AI_GUIDE 部署后一检索就
     ImportError，被 server 宽 except 吞成"（检索失败：No module named 'jieba'）"，**检索 100% 不可用**。
     修：加 `jieba==0.42.1` + 缺失时降级纯 2-gram。
  4. **F8 代码围栏内的 # 污染向量**：不只是多切一节——伪标题会成为其后所有真实小节的父标题，
     而标题路径要拼进嵌入文本，等于把 Python 注释混进正文向量。修：跟踪 ```/~~~ 围栏状态。
  5. **F9 裸 `[[wikilink]]` 被整个删除**：`[[火箭发动机]]` → `见  一节`。Obsidian 里裸链接是主流写法，
     链接目标恰是最高信号的概念词，被同时从嵌入文本和 BM25 词表抹掉。修：保留目标词，去路径与 #锚点，仅 `![[]]` 删除。
  6. **F20 文件名/title/tags 从不进嵌入**：只写 metadata。与 F9 叠加后概念层信息基本没进索引。
     修：文件级锚点拼进待嵌入文本（逐段去重——短文档 heading 取 title、文件名常与 title 同名，
     不去重会产生重复串、扭曲 BM25），完整前缀存 metadata `ctx` 供检索侧剥离。
  7. **F6 置信度与排序不同源**：排序用重排分、置信度用 RRF 分。实测输出 0.21/0.25/0.30/0.38/1.00——
     声称降序却单调递增，默认配置下的常态，直接误导消费输出的 LLM。修：重排生效时用 sigmoid(重排 logit)。
  8. **F7 BM25 缓存永不失效**：指纹只比 `count()`，而"改一段文字"通常不改块数。GUI 把 index.py 当
     独立子进程拉起，server 里的 `reset_bm25_index()` 根本不会被调用 → 关键词侧一直用旧文本。
     修：指纹改 `(count, index_meta 的 mtime_ns)`。
  9. **F16 `--create` 导入后下一次检索清空索引**：import.py 主动建空目录，而 `kb_stale` 对"空目录"
     不带 missing 标志 → 自动同步判"文件全删" → 清空刚导入的数据。import.py:291 的警告正是这个场景，
     而 `--create` 结构性保证了它成立。修：返回 `emptied` 标志，优先于 version_upgrade（宁可不重建也不清空）。
  10. **F10/F11 死键与死代码**：`fusion_dense_weight`/`fusion_bm25_weight` 赋值后从不读取（AST 确认），
      却挂在 GUI"实时生效"分组下；`hybrid_search_hyde` 零调用方，问题 21 的 HyDE 是未接线的死代码。
      修：权重接进 RRF（DEFAULTS 改 1.0/1.0 保持等权，现有排序不变）；HyDE 由 server 接入，
      新增 `return_top_confidence` 让首轮直接带回置信度，去掉原来"开 HyDE = 2 倍检索开销"。
  11. **其余**：F3 转义引号打断注释剥离致整份配置静默回退（GUI 里 truncate_mark 打个双引号即触发）；
      F4 配置零类型校验（`rerank_enabled:"false"` 是真值，重排照开）；F12 模板与 GUI 各缺同样 5 个键；
      F13 `index_vault` 不转发 incremental/full（问题 21 修了孪生的 `index_library`，漏了这个）；
      F14 `kb_stale` 从不看 `_version`，版本号提升不主动触发重建；F15 等长中文库名派生同一 collection
      且以下划线结尾不合 Chroma 命名规则；F17 `split_sentences` 收不到每库 chunk_max；
      F18 "父节全文"缺命中块、顺序打乱（命中第3块输出 3,1,2）、每段带重复前缀；F19 多行 YAML tags 解析成空串。
- **验证**：新增 `tests/audit_regression_test.py` 19/19 通过；既有套件 `library_registry_test` 13/14
  （唯一失败 numpy 缺失，HEAD 上同样）、`server_singleton_test` 5/5、`test_config_editor` 全绿。
  另跑了完整 E2E（真实 _index_core + 假编码器 + stub Chroma）：切块、ctx 组装、small-to-big 回填全部正确。
- **过程中的额外发现**：
  1. **仓库自带的 `test_config_editor.test_groups_cover_all_defaults` 在 HEAD 上就是红的**，
     报的正是 F12 那 5 个键——这条回归测试早就存在、早就失败，只是没人跑（用 git archive HEAD 复现确认）。
  2. **F17 性质更正**：不是"长段落突破块上限"（单句超限"宁大勿断"是既定设计），
     而是每库 chunk_char_limit 覆盖传不到句子层，会回落到全局配置。
  3. E2E 抓到我自己在 F20 引入的 ctx 重复串 bug，已修并补测试。
- **遗留 / 上线前须知**：
  - **META_VERSION 5→6，必须一次全量重建**（切块规则与嵌入文本都变了）。修好的 F14 会让 ensure_fresh
    自动发现版本变化并触发；也可 `python index.py --library <名> --full`。
  - **重建前先确认 `data/config.json` 的 `chunk_char_limit` 实际值**。补写逻辑不改已存在的键值，
    若它现在是 1500 则重建出来仍是 v4 大块，600+small-to-big 的配套设计拿不到收益。
  - 若 config.json 里已有 `fusion_*_weight: 0.6/0.4`，它们现在是**真生效**的了（等价于给 dense 加权）。
    要保持与此前完全一致的排序需手动改成 1.0/1.0。
  - 中文库名的 collection 会改名（当前唯一注册库 `Obsidian Vault` → `kb_obsidian_vault` 不受影响）。
  - **未审计面**：`gui/widgets.py`(1562 行)、`gui/app.py` 其余部分、CUDA 降级状态机、Windows msvcrt 锁、
    心跳/进度写入原子性、export.py 完整数据完整性路径。
  - **无法在本机验证**：F5 三个 Windows 调用点（Linux 上 os.kill(pid,0) 是良性探测，本机测试全绿）、
    Chroma 1.5.9 Rust 后端是否拒绝退化 collection 名（校验规则在 segment.py，而 PersistentClient 已走 Rust 后端）。

### Windows 实机验证结果（2026-08-14，问题 22 补）

在 Windows 目标机（8GB RTX 5060、32GB RAM、Python 3.14）上按 AGENTS.md 完成全部验证，结论：**全部通过，已 fast-forward 合并到 main（bc34202）**。

- **阶段 1 纯逻辑测试**：audit_regression 19/19、library_registry 14/14、server_singleton 5/5、test_config_editor 0 failures、test_gui_store 0 failures、verify_export_import 39/39。
- **阶段 2 F5**：新写法 _pid_alive 实测 自身=True / 999999999=False / -1=False / 'x'=False；未做双 server 实例实测（GUI/服务当时未在跑）。
- **阶段 3 F2**：真实 config.json 为 chunk_char_limit=600、rerank_candidates=50、small_to_big=true、fusion_*_weight=0.6/0.4（手动改过，未踩首跑陷阱）；补写行为正常——只新增 hyde_enabled/hyde_llm_url/hyde_llm_model/hyde_min_confidence 四个键，既有值与注释未被改动。fusion 0.6/0.4 现已真生效（dense 偏重），已记录待用户决定是否改 1.0/1.0。
- **阶段 4 真实索引**：主库在验证时被 ensure_fresh 自动重建为 v6（1763 块）；test/agents/skills 三个库手动 --full 补建（163/42/137 块），四库全部 _version=6。重建耗时 22-24s/库。
- **评估**：v6 重建后 eval_retrieval 12 组 **top1=5/12、top3=10/12、top5=10/12**（基线 6/9/9）。top3/top5 各 +1；CFD 查询 top1 从 y+ 变为同相关的 ansys 引用集（重排器判断）。
- **可见效果逐条确认**：置信度严格降序（0.73/0.72/0.72，F6 生效）；来源行出现 [已回填父节全文]（F18 生效）；v6 文档带文件级锚点 ctx（F20 生效）；缺失 jieba 时降级日志路径已在代码确认。
- **遗留**：三小库重建完成；Vault 文档（主页/使用指南/架构/决策记录/Roadmap/操作手册）已同步置信度与 small-to-big 语义；retriever.py _format_results docstring 已修正。

## 问题 23：多格式文档支持 R1——DOCX + 文字层 PDF（2026-08-24）

**范围修订**：原方案 R1 含 MinerU 云端 OCR；用户决定扫描件 OCR 整体后移到末轮（TODO.md 已重排：R1=Word+文字层PDF，R2=GUI，R3(末)=OCR）。R1 零新增配置键、零子进程、零 GUI 改动。

### 实现
- **extractors.py（新）**：唯一入口 `extract_to_markdown(path) -> (md|None, reason)`（与 TODO 原拟的 `-> str|None` 不同：index 落终态需要 reason，故改返回二元组）。DOCX 按 body 子元素保序遍历（标题钳 ###、管道表转义、单元格换行压空格）；PDF 以「文字页占比 ≥0.5」判层，扫描件返回 `(None,"scanned")`。缓存键 `<字节md5>.v1`，原子写，写失败仅跳过缓存；None 不写缓存；启动清扫 >24h 孤儿 tmp。懒加载 import，绝不抛异常、绝不写源目录。TEXT/BINARY/SUPPORTED_EXTS 单一事实来源。
- **index.py**：META_VERSION 8→9；新增 `_load_text()`（原始字节 MD5 指纹——对合法 UTF-8 与旧内容指纹等值，既有条目免迁移；OSError→哨兵 `"unreadable"` 两轮判稳；后缀一律 lower()）、`_skipped()` 单点谓词、`_terminal_entry()` 统一终态（reason ∈ unreadable|extract-failed|empty|tbd|scanned）；主循环 None/unreadable 判定严格先于 TBD；kb_stale 二进制源只比字节哈希绝不提取；converting 进度相位 + progress_text 停滞豁免；`__main__` 逐库 try/except（LockBusy 除外）。
- **library.py**：set_config extensions 白名单校验引用 SUPPORTED_EXTS（小写归一+去重+保序）。**tools/check_notes.py**：跳过二进制源（修 UnicodeDecodeError 崩溃）。**verify_export_import.py**：REPO_FILES 补 extractors.py；vault_export 计数改 manifest 权威清单集合比对。

### 过程中抓到并修掉的三个真 bug（新测试逮住）
1. `_index_core` 正常成功路径漏 `current_rels.add(rel)` → 切块成功的文件被裁剪出 meta、块随即被当幽灵清掉（单点 add 移到 stat 之后统一覆盖所有存活路径）。
2. P7 自愈分支误伤全终态库（每轮把合法终态 meta 清空重落，永不收敛）。
3. **一致性死循环（P7 推广）**：Chroma 部分丢块时（实测：验证中途进程被杀 → WAL 段未持久化，HEBAT3_Technical_Report 的 104 块丢失），meta 期望≠实际每轮报 stale 但增量无块可补、永远修不回。现推广为通用校验：期望≠实际即自动转全量重建（全终态库 0==0 不误伤）。真库实测自愈：1594←1490，38.9s。
- 另收敛一项备案隐患：「既有 md 空正文守卫不落 meta → 每轮误计 added 每轮 stale」随 empty 终态机制一并解决（原列于「明确不做」，因与同一代码路径重合顺带完成）。

### 回归结果（Windows 实机，Py3.14）
test_extractors **16/16**（新）· audit_regression **19/19** · library_registry **14/14** · server_singleton **5/5** · test_config_editor 0 failures · test_gui_store 0 failures · verify_export_import **39/39**。
注：管道环境下跑测试需 `$env:PYTHONIOENCODING='utf-8'` 前缀（交互控制台不受影响）。

### 真实索引影响
v9 升级触发一次全量重建（legacy 入口实测 1594 块 / 80.5s GPU，批次自动收紧 32→8）。**运行中的 GUI/server 若加载的是旧代码需重启**，否则新旧逻辑会交替操作同一 Chroma/meta。

### 遗留
- 扫描件 pdf 在 vault 中存在：当前记 scanned 终态跳过，R3 接 MinerU 后凭 mtime 变化或手动 --full 转正（xsrc 自动重试机制属 R3）。
- 提取器依赖缺失时的优雅降级路径未做单测（ImportError 模拟成本高），靠懒加载+warn_once 兜底。
- 真实 vault 尚未开启 extensions（行为变更，待用户确认后执行 `library.py config "Obsidian Vault" --set extensions=md,pdf,docx`）。

## 问题 24：多格式文档支持 R2——GUI 适配（2026-08-24）

R1 提交（177ede6）后的 GUI 层配套，全部为展示/判定口径对齐，检索链路零改动。

### 改动
- **gui/store.py**：
  - `heartbeat_state` 对 converting 相位豁免停滞告警（与 index.progress_text 双看门狗口径一致：心跳停止仍判 dead，豁免不掩盖真死）；
  - 新增 `meta_issues_for(cfg)`（按 reason 统计 xfail 终态文件数，只读指纹文件）与 `ISSUE_TEXT`（五种 reason 的中文标签+处置指引）。
- **gui/widgets.py**：
  - ProgressCard 阶段条插入「转换」chip（scanning→**converting**→embedding），converting 计数行显示「文档转换 x/y · PDF/DOCX→Markdown」；
  - HeartbeatPill.set_state 增可选 note 参数（运行中文案覆盖，不改状态色/呼吸）；
  - 库配置对话框 extensions 字段 helper 补「支持 md/txt/pdf/docx；扫描件 PDF 暂不支持 OCR（索引时自动跳过）」。
- **gui/app.py**：
  - 刷新循环在 converting 相位给心跳胶囊传 note=「文档转换中（大文件耗时属预期）」，用户不再误读为卡死；
  - 状态卡副行追加提取跳过汇总（形如「⚠ 提取跳过 4 个文件：扫描件×3、不可读×1」），按当前选中库范围聚合。
- **tests/test_gui_store.py**：+3 用例（converting 停滞豁免且 dead 不被豁免掩盖 / meta_issues_for 按 reason 计数与缺文件容错 / ISSUE_TEXT 覆盖全部终态 reason），27→**30 用例全过**。

### 回归
六件套全绿：audit 19/19、library_registry 14/14、server_singleton 5/5、test_config_editor 0 failures、test_gui_store 0 failures（含新增 3 例）、verify_export_import 39/39。

### 备注
- GUI 视觉观感（chip 配色、文案长度）待用户下次开 GUI 人工确认；逻辑层已由单测锁定。
- 真实 vault extensions 启用仍待用户确认（同问题 23 遗留）。

## 问题 25：多格式「人机分权」——默认开启 + Agent 门禁（2026-08-24）

用户需求：多格式默认开启；GUI 可自选格式并持久化；Agent 可继续做文本类索引，但**未经用户批准的格式不得被 Agent 重建/增量纳入**（含检索触发的自动同步）。批准粒度经确认：一次批准长期有效，可随时撤销。

### 实现
- **library.py**：`DEFAULT_EXTENSIONS=["md","pdf","docx"]`（entry.extensions 为 null 时继承 → 现有库与新建库自动默认开启）；OVERRIDE_KEYS/LIST_KEYS 增加 **`agent_formats`**；set_config 校验（仅二进制格式、须已在当前 extensions 启用、允许空=全部收回）；effective_config 输出 `agent_formats = extensions ∩ 已批准`（extensions 收窄时授权自动失效）。
- **index.py**：kb_stale/_index_core 新增 `agent_allowed` 参数——未授权后缀的文件在循环最早期（stat 之前）**冻结**：零 I/O、不转换、不计变更、条目与块原样保留（绝不裁剪清理）；无条目则视同不存在。一致性自愈的期望块数天然包含冻结条目，无误伤。
- **server.py**：Agent 可达的三个入口全部走门禁——
  - `ensure_fresh()`（search 自动同步）：kb_stale/index_library 携带受限集合 `TEXT_EXTS ∪ agent_formats`；存在待批准文件时返回明确提示；
  - `reindex_knowledge(library, allow_new_formats=false)`：新参数。未授权格式列出数量并提示"先向用户确认"；`allow_new_formats=true` = 用户已同意，将新格式写入注册表 `agent_formats` **持久化**并纳入本次任务；
  - `_start_background_index/_run_index` 经 lib dict 的 `_agent_allowed` 键透传。
  - GUI/CLI（人类路径）不带门禁参数，行为不变。
- **gui/widgets.py**：库配置对话框 extensions 文本框升级为 **md/txt/pdf/docx 勾选块**；新增"AI Agent 权限"勾选行（pdf/docx，取消某格式时联动收回其授权）；保存写入两类设置（= 用户设置持久化）。

### 测试与回归
- test_extractors **17/17**（新增 test_agent_gate_freezes_unapproved_binaries：冻结不嵌入/条目保留/受限视角判稳/批准后补齐且不重嵌/新文件两视角行为）
- library_registry **15/15**（新增 agent_formats 校验与交集语义用例；旧断言 ["md"] 按新默认更新）
- 其余四件全绿：audit 19/19、server_singleton 5/5、config_editor 0 failures、gui_store 30 PASS、verify_export_import 39/39。

### 备注
- 默认开启对真实 vault 的实际生效点 = 下一次任何索引运行（md 部分指纹全命中，只新增 pdf/docx 的转换与嵌入）。
- Agent 门禁是"提示+冻结"而非硬拒绝：Agent 始终可以维护文本层；越权风险由冻结语义消除。
- MCP 工具签名向后兼容：allow_new_formats 不传 = false = 严格模式。

### 补充验证（同日，问题 26 前置）：格式撤销与文件增删改语义
新增两个集成用例锁死行为：
1. **取消勾选格式** → 下轮索引自动清除该格式的条目与全部块（回到无该类型版本）；重新勾选后凭提取缓存快速恢复、无需重新解析。
2. **物理删除二进制文件** → 连 Agent 受限视角也会正常裁剪清理（冻结只作用于仍在磁盘上的未授权文件，不给已删文件续命）。
顺手修掉一个被新用例逮住的既有死角：kb_stale 的判空基准含 `_version` 哨兵与非 dict 脏数据——「条目清空后的收敛态」会被 emptied 分支永远误报 stale。现以真实条目数为准；「meta 与文件双空」判稳。
回归：test_extractors 19/19，六件套全绿。期间真库再现 Chroma 分叉（2370 vs 1594，疑似并发写入者），一致性自愈自动全量重建修复并幂等收敛——自愈机制实战有效。

## 问题 27：提取试验台——GUI 单文件转译效果预览（2026-08-24）

用户需求：点按钮选文件上传，旁边返回提取结果，像 Google Translate 左右对照那样预览转译质量；但左右分栏空间利用率低。

### 设计取舍
输入是二进制文件，"左侧原文"没有可展示物——因此不做分栏，**整幅留给产出**：
- 顶部控制行：选择文件 + 开始提取 + 当前后端徽章（本地直提 / MinerU 云端·已配Key）
- 信息徽章行：路由（local / ocr:mineru-cloud）、耗时、字符数、缓存命中、失败原因+处置指引（复用 ISSUE_TEXT）
- 主体两个自绘页签：「渲染预览」（flet Markdown，GitHub 扩展集，表格/标题可读）与「Markdown 源码」（等宽只读框便于复制）
入口：库管理对话框工具栏「提取试验台」按钮。与索引用同一条管线（extract_preview → _extract_full），所见即所得；预览不落索引终态。

### 实现
- **extractors.py**：抽取 `_extract_full()` 返回 (md, reason, route, cached)；公开 API `extract_to_markdown` 保持二元组契约不变；新增 `extract_preview(path)` 输出过程信息 dict。
- **gui/widgets.py**：新增 `ExtractLabDialog`。flet 0.86 控件模型适配：FilePicker 为服务型控件且 `pick_files` 是 async 方法（async 事件处理器直接 await 结果，不再走 on_result 回调）；弃用签名大改的 ft.Tabs，改自绘页签按钮 + visible 切换（版本免疫）。提取在后台线程执行，UI 不冻结。
- 测试：test_extractors 增 `test_extract_preview_contract`（字段形态/pair 契约不回归/缓存命中可见），**23 用例全过**；gui_store 导入级验证组件可构建。六件套全绿。

### 体验修订（同日，用户实测反馈六项）
1. 防重入 + 明确动画：进行中按钮禁用并改文案「提取中…」，新增**不确定进度条**；
2. 动态提示：底部说明按当前生效后端实时生成（本地直提→「扫描件将被跳过」；云端→「可能数十秒」），不再静态误导；
3. 活动秒表（心跳）：进度条旁每 0.7s 刷新「⏱ Xs 运行中 · 超时预算 ~Ys」，死机与否一目了然；中断收尾语义成文——daemon 线程随 GUI 进程消亡、缓存原子写至多留孤儿 tmp（启动清扫回收）、预览不碰 meta/Chroma 无需回滚；
4. 后端可选：新增「跟随全局 / 本地直提 / MinerU 云端」下拉，**单次覆盖**仅影响本次预览（extract_preview(backend=…) 参数穿透），不污染全局配置；
5. 渲染净化：新增 sanitize_render_md——<b>/<i> 转 **/*，<u>/<span> 等裸 HTML 剥除（flet Markdown 不渲染裸 HTML 会原样显示）；源码页保持原样以源码为准；
6. 复用实例打开时重置为干净待命态（防上次中途关闭遗留禁用按钮）。
测试：test_extractors **25 用例全过**（+sanitize 净化、+backend 单次覆盖不污染全局）；gui_store 0 failures；audit 19/19。

## 问题 28：双链关系图——出链/入链查询（不影响检索排序）（2026-08-25）

用户想要类似 Obsidian 反向链接面板的能力：给定一篇笔记，查它链接到谁（出链）、谁链接到它（入链）。问题15（2026-08-10）已经把 `clean_wikilinks()` 定成"清洗 `[[wiki链接]]` 为纯阅读文字后再切块/嵌入"——链接目标词绝不能重新混进嵌入文本，那正是问题15要修的污染（例如"蛋糕的制作方法.md"提了一句 `[[如何制作奶油]]`，链接目标词留在嵌入文本里会导致搜"制作奶油"命中错的那篇）。这条清洗行为本轮完全不动。设计上把关系数据做成与检索完全旁路的第二条管道：两条管道共享同一段原始正文，一条不变（清洗→切块→嵌入排序），另一条纯粹旁路（抽取链接目标→存进 meta→按需反查），后者不进嵌入、不进 BM25、不影响任何排序。本轮只做后端 + MCP 工具，GUI 展示留待下一轮。

### 实现
- **index.py**：新增 `extract_wikilink_targets(text)`，与 `clean_wikilinks` 共用同一条 `[[...]]` 正则与解析规则，但取"目标"而非"显示文字"（`[[目标|别名]]` 取目标、`[[目标#标题]]` 去锚点取目标、`![[嵌入]]` 与 `[[#本文锚点]]` 不计入）。`_index_core` 的文本与二进制两条正文分支里，都在 `clean_wikilinks(body)` 清洗**之前**先算出 links，清洗动作本身一字未改；meta 成功条目新增 `links` 字段（去重排序后的目标名列表）。
- **回填机制**：新增 `_links_missing(entry)`——非终态条目缺 `links` 键即判定需要重跑，与既有的 `_backend_changed` 同属"惰性触发重试"：不强制 `--full`，下一轮增量索引里该文件自然穿透快速路径重新处理一次（因为快速路径不区分"只是缺个字段"与"内容变了"，穿透后走的是完整的重新分块+重新嵌入），之后即收敛。终态（xfail/tbd）条目天然没有 `links`、也不该有——`_skipped` 已排除它们，不会被这个机制误拉回正常处理分支。`_index_core` 两处快速路径（size+mtime 分支、hash 分支）与 `kb_stale` 对应两处同步加了 `not _links_missing(...)` 判断（AGENTS.md 架构红线 6 的教训：Agent 门禁那次两侧必须同步改，否则一侧收敛一侧不收敛，永远误报/漏报 stale）。
- **`resolve_note_relations(meta_file, target)`**：出链/入链查询，完全基于当前 meta 现算、不持久化 inlinks（入链是全局反向索引，维护缓存比现查更容易过期；库是个人笔记量级，现算成本可忽略）。target 支持库内相对路径或不含扩展名的标题（按文件 stem 匹配，同 Obsidian wikilink 引用写法）；标题重名时任取其一，不追求消歧（与 Obsidian 本身行为一致）；断链（目标文件不存在）静默不出现在出链里；自链不计入自己的出链/入链。
- **server.py**：新增 MCP 工具 `note_relations(path, library="")`，库选择语义对齐 `search_knowledge`（空 = 默认库，经 `resolve_entries` 解析；只能定位单库，不支持 "all"，因为一篇笔记只属于一个库；默认库解析出多个时报错提示显式指定 library）。
- 零新增配置键（无需开关，没有链接的库自然空转）；`META_VERSION`（仍 9）与 `extractors.EXTRACT_VERSION` 均未动——这次改动不影响切块/嵌入的文本内容，不在这两个版本号的语义范围内。

### 测试
- **audit_regression_test.py** 新增 `test_extract_wikilink_targets`（裸链接取目标、带别名取目标而非别名——与 `clean_wikilinks` 方向相反、路径+锚点剥离、嵌入不计入、纯锚点不计入、去重、表格转义管道），**21/21 通过**（+1）。
- **test_extractors.py** 新增 6 例（`_IsoEnv` 隔离 + 假编码器，不加载真模型/真 Chroma）：链接抽取基本用例；回填机制端到端（手工删 meta 条目的 `links` 键模拟"功能上线前的旧索引"→下轮自动补齐且其余字段不变、编码调用次数证明确实被重新处理而非跳过）；`kb_stale` 同步生效（缺 `links` 判 stale/changed，验证两侧机制真的同步而非只改了一边）；终态条目缺 `links` 不被强制重跑；`resolve_note_relations` 端到端（含自链排除、断链静默丢弃、查询不存在标题返回 `resolved=False`）；同名标题歧义不崩溃。**38/38 通过**（+6）。
- 七件套回归全绿：audit_regression **21/21**、test_extractors **38/38**、library_registry **15/15**、server_singleton **5/5**、test_config_editor 0 failures、test_gui_store 0 failures、verify_export_import **39/39**（真库导出/导入/检索演练，含一次真实 hybrid_search）。

### 遗留
- ~~GUI 展示（关联笔记入口，"这篇笔记的出链/入链"面板）留待下一轮~~ 已在问题29完成。
- 真实 vault 的现有 meta 条目普遍缺 `links` 字段：下一次任何增量索引运行（含 `search_knowledge` 触发的自动同步）会对当前已索引的每个非终态文件穿透一次快速路径、重新分块+重新嵌入以补齐该字段，效果上类似一次全库重跑，但只发生一次，之后恢复正常增量跳过。这是 `_links_missing` 机制的预期行为（用于在不动 `META_VERSION` 的前提下补齐存量数据），非 bug，但用户下次触发索引时应预期到这次性能开销。本轮跑七件套回归时（2026-08-25）该次性重建已实际触发（Chroma 2674 块 vs meta 1898 块 → 自动全量重建），验证了这条预期成立，且与本轮 GUI 改动无关。

---

## 问题 29：双链关系图——GUI 展示（关联笔记内联展开）（2026-08-25）

问题28完成了双链关系查询的后端与 MCP 工具，GUI 展示留到了这一轮。本轮把这条能力接进语义检索卡：检索到一条结果后，除了展开正文，还能再点一下同一行新增的"关联笔记"按钮，内联看到这篇笔记的出链（它链接到谁）与入链（谁链接到它），不用切到 Obsidian 里翻反向链接面板。纯展示层接线，`resolve_note_relations` 原样复用、一字未改。

### 实现
- **gui/store.py**：新增 `note_relations_for(cfg, target)`，模式照抄既有的 `meta_issues_for`——只读该库 meta 指纹文件，不加载模型、不碰 Chroma；任何异常（含 meta 缺失/损坏）一律折叠为安全默认值 `{"resolved": False, "file": None, "outlinks": [], "inlinks": []}`，不外泄异常。内部调用 `index.resolve_note_relations(meta_path(cfg["name"]), target)`。
- **gui/widgets.py**（`SearchCard`）：`__init__` 新增可选回调 `on_relations=None`。`show_results()` 的 `_render_state` 新增 `relations_shown`（当前展开"关联笔记"区的结果下标集合）与 `relations_cache`（下标→查询结果，避免同一条结果反复展开时重复调用回调）。每条结果标题行在"在 Obsidian 中打开"按钮旁新增一个图标按钮（`ft.Icons.HUB`，实测在项目当前 flet 0.86.5 环境下存在，无需换用候补图标），tooltip"查看关联笔记（双链）"，点击走独立的 `_toggle_relations(i, rel)`——与控制正文展开的 `_toggle`/`expanded` 完全独立的另一个开关，互不干扰：只看正文、只看关系、两者都看、两者都不看，四种组合都成立。展开态下追加渲染"出链（本文链接到）：…\n入链（谁链接到本文）：…"；`resolved=False`（笔记已改名/移动，meta 里查无）时给出"未找到该笔记的索引记录，可能已重命名或移动"的兜底提示，不留空白也不报错。未接 `on_relations`（默认 None）时按钮不挂点击事件（与 `open_btn` 无 `on_open` 时的处理方式一致）。
- **gui/app.py**：`SearchCard` 构造新增 `on_relations=self._note_relations`；新增 `App._note_relations(rel)`，复用 `_open_result` 已在用的 `_split_lib_rel`/`_lib_by_name` 把结果行的 `<库名>/<相对路径>` 前缀解析回库配置，再调 `note_relations_for`；库名未知（旧格式结果行、或库已被移除）时直接返回 `resolved=False`，不抛异常。
- 零新增配置键；未改动 `index.py`/`server.py`/`extractors.py`——后端与 MCP 工具在问题28已验证正确，本轮纯粹是给已有能力接一个 GUI 入口。

### 测试
- **test_gui_store.py** 新增 `test_note_relations_for`：照抄 `test_meta_issues_for_counts_xfail_by_reason` 的打桩模式（`patch.object(gstore, "meta_path", ...)` 指向临时 meta.json），验证出链/入链解析正确（含按文件 stem 匹配不含扩展名的标题查询）；meta 指纹文件不存在时返回 `resolved=False` 而不抛异常。
- 新增 3 例：本项目第一次直接单测 `SearchCard`，不搭真实 flet Page/窗口——`_render_results()` 本身不碰 page，测试绕开真实点击事件派发，直接调用 `_toggle_relations(i, rel)`：①首次展开触发一次查询、收起不重查、再展开命中缓存不重复查询，且验证关系展开不影响正文展开的 `expanded` 集合（两个开关互相独立）；②`resolved=False` 时结果卡片渲染兜底文案；③未接 `on_relations` 时直接调用 `_toggle_relations` 也不抛异常、不产生缓存条目。
- 七件套回归全绿：audit_regression **21/21**、library_registry **15/15**、server_singleton **5/5**、test_config_editor 0 failures、**test_gui_store 0 failures（43 例，+4）**、test_extractors **38/38**、verify_export_import **39/39**（真库导出/导入/检索演练）。

至此双链关系图功能全部完成（后端 + MCP 问题28、GUI 问题29）。

## 问题 30：MinerU 云端 API 路径 bug 修复 + 文字层 PDF 可选送 MinerU（`pdf_text_backend`）（2026-08-26）

用户今天配好真实 `mineru_api_key` 后做的首次真实冒烟测试意外发现一个既有 bug：`_mineru_cloud_extract` 里硬编码的两处接口路径是错的——提交用的 `{_MINERU_BASE}/file-protocol/batch`、轮询用的 `{_MINERU_BASE}/file-protocol/batch/{batch_id}`，实测均返回 HTTP 404（纯文本 `404 page not found`，路由层面不存在，不是鉴权/参数错误）。查官方文档（https://mineru.net/apiManage/docs）并实测校正，正确路径是提交 `POST {_MINERU_BASE}/file-urls/batch`、轮询 `GET {_MINERU_BASE}/extract-results/batch/{batch_id}`（请求/响应体字段名本身没错，只是 URL 路径错）。**后果：`pdf_scan_backend=mineru-cloud` 这个功能自问题26（R3a）上线以来，任何真实调用都会 404**，被异常折叠机制悄悄吞成 `extract-failed`/`scanned` 终态，表现为"静默跳过"而非崩溃或报错——不会引发用户警觉，但从未真正 OCR 成功过一次。既有 `test_extractors.py` 的 mock HTTP 用例全部显示通过，是因为 mock 只验证"代码怎么调用 requests"，从不检查 URL 字符串是否是服务器上真实存在的路径，这类 bug 结构性地不在其覆盖范围内。

顺带落地了 2026-08-25/26 讨论、记录在 TODO.md Backlog 里的一个架构问题：`pdf_scan_backend` 此前只在"扫描件"分支生效（本地对扫描件零处理能力，该开关实质是"要不要为唯一能用的路径 MinerU 付费"）；有文字层的正常 PDF 分支完全写死走本地 `pymupdf4llm`，没有任何开关——而这条分支恰恰存在真实的质量/成本权衡（MinerU 结构识别更准，用户可能想为质量付费）。本次新增独立开关 `pdf_text_backend`（local/mineru-cloud），语义与 `pdf_scan_backend` 不同、不复用同一个键；送云端时 `is_ocr=False`，不为已有文字重复付 OCR 的钱。

### 实现
- **extractors.py**：
  - `_mineru_cloud_extract(path, is_ocr)` 签名新增必填参数 `is_ocr`（不设默认值，两个调用点都必须显式传），修正两处 URL，docstring 同步更正契约描述并记录本次修复。
  - 新增 `get_text_backend()` / `TEXT_BACKENDS = ("local", "mineru-cloud")`，写法照抄 `get_scan_backend()` 的防御风格（非法值回退 local）。
  - `_extract_pdf` 重构：删掉函数顶部"提前 resolve backend"那行（旧代码在还不知道文件是否扫描件之前就把 `backend` 无条件解释成扫描件语义，会污染文字层分支）；改为两个分支各自独立 resolve 自己的配置键——扫描件分支 `scan_backend = backend or get_scan_backend()`（语义不变），文字层分支新增 `text_backend = backend or get_text_backend()`，`== "mineru-cloud"` 才送云端（`is_ocr=False`，路由标"mineru-text"），其余任何值（含扫描件分支专用的 "none"/"mineru-local"）一律安全落到本地直提，不报错不崩溃。
  - `_extract_full` 的缓存路由候选列表新增独立标签 `"mineru-text"`（不复用 `"ocr:mineru-cloud"`），换后端旧缓存天然失效。**在此基础上发现并修正一个必要的额外问题**：字面按方案给的 `routes` 4 元素恒定列表（`["ocr:mineru-cloud","ocr:mineru-local","mineru-text","local"]` 无条件全查）会让 `_cache_get` 的"任一路由命中即真"逻辑失效——该逻辑的正确性建立在"同一文件字节只会由一条路由成功产出"这个假设上，扫描件相关的两个 `ocr:*` 路由仍满足这个假设（文件是否扫描件由内容确定性判定，与配置无关），但 `local`/`mineru-text` 不再满足：同一份文字层 PDF 在不同 `pdf_text_backend` 下会产生两个都合法但内容不同的成功结果。若不修，切换 `pdf_text_backend` 后如果另一路由恰好已有历史缓存，会被假命中，切换永远不生效——这正好是任务给出的测试要求 4 明确要锁死的行为，字面实现和这条测试要求相互矛盾。修法：`_extract_full` 现在按当前 `backend` 覆盖/全局 `pdf_text_backend` 只计算并加入**唯一**一个文字层路由标签（`mineru-text` 或 `local`，二选一），扫描件的两个 `ocr:*` 路由不受影响、逻辑不变。
  - `_mineru_cloud_extract` 的"未配 Key"分支按 `is_ocr` 拆分处理：`is_ocr=True`（扫描件）保持原样返回 `(None, "scanned")`；`is_ocr=False`（文字层）改返回 `(None, "extract-failed")`——沿用 "scanned" 会让 GUI 提示"发现扫描件 PDF...或改用文字层版本"，而触发这个分支的文件本来就是文字层，这条建议对用户是自相矛盾的误导。
  - 模块顶部 docstring 补文字层 PDF 路由说明段。
- **config.py**：DEFAULTS 新增 `"pdf_text_backend": "local"`；CONFIG_TEMPLATE 对应位置加注释块（紧跟 `pdf_scan_backend` 之后、`mineru_api_key` 之前，共用同一账号/Key/超时预算），第八节标题从"扫描件 OCR"改为"PDF 提取后端"（现在管两类场景）；`template_consistency_errors()` 校验通过。
- **gui/config_editor.py**：GROUPS 里"扫描件 OCR"分组改名"PDF 提取后端"并加入 `pdf_text_backend` 字段。
- **gui/widgets.py**（`ExtractLabDialog` 试验台 + `SettingsDialog` 设置页）：
  - 试验台"跟随全局 / 本地直提 / MinerU 云端"下拉的值是 `"auto"/"none"/"mineru-cloud"`（穿透为 `backend=None/"none"/"mineru-cloud"`）。`_extract_pdf` 重构前，这个下拉对文字层文件是**完全的死选项**——`backend` 只在扫描件分支被读取，选"MinerU 云端 OCR"对着一份正常 PDF 点提取，界面不报错但也绝不会真的调云端，静默照常走本地直提。重构后自动生效，新增端到端测试 `test_extract_preview_backend_override_reaches_text_layer_branch` 验证。
  - **顺带发现并修复一个上传前置确认的漏问缺口**：试验台的"云端上传需先弹确认框"逻辑（`_run` 里 `if self._effective_backend() == "mineru-cloud"`）原本只读 `get_scan_backend()`；dropdown=auto 时，若用户只把 `pdf_text_backend`（而非 `pdf_scan_backend`）设为 mineru-cloud，这个判断会漏判——文字层文件会在用户没看到任何确认框的情况下被真实上传到第三方。修：新增 `_effective_text_backend()` + `_will_call_cloud()`（两个后端任一为 mineru-cloud 即需确认，宁可多问不能漏问），`_run()` 改用后者。确认框文案同步改为不预设"一定是 OCR"（文字层送云端时实际是 `is_ocr=False` 的结构识别，不是 OCR）。
  - 设置页 `SettingsDialog._save()` 存在同构的既有确认机制（`pdf_scan_backend` 切到 mineru-cloud 需先确认，因为库里存量扫描件会被批量外传），但只检查这一个键——新增的 `pdf_text_backend` 若不纳入同一机制，用户在设置页把它切到 mineru-cloud（今后每份文字层 PDF 都会被上传）会完全没有任何确认提示，与既有设计原则不一致。已将检查泛化为对两个键都生效（`_switches_to_cloud` 提取为独立方法，`_confirm_cloud_backend` 接收本次触发的键集合、按实际涉及的键列出对应文件类别）。
  - 试验台底部动态提示（`_hint_text`，即 TASK_LOG 问题26 记录的"本地直提→扫描件将被跳过；云端→可能数十秒"那段）原文只讲扫描件场景，对文字层文件场景完全沉默（不是说错，是没提，但现在这个下拉对文字层文件也真正生效了，沉默会让用户读不到任何与自己文件相关的信息）。最小化调整：在原有文案后追加一句读 `get_text_backend()` 的独立分句，说明文字层 PDF 在当前后端下的实际处理方式，不改动原有那两句的措辞。
- **is_ocr 参数改动波及的既有测试**：4 处 `ex._mineru_cloud_extract = lambda p: (...)` monkeypatch 补上 `**_kw` 容错（否则新的关键字参数 `is_ocr=` 会让这些桩函数抛 TypeError）；`_FakeRequests` 的 `get()` 轮询 URL 匹配、`post()` 请求体记录同步改为新路径/可断言的 json 载荷。

### 测试
- **URL 回归**：`test_mineru_cloud_extract_uses_correct_api_urls_both_is_ocr_values`（直接断言 mock 记录到的 POST/GET URL 字符串本身，覆盖 is_ocr=True/False 两个调用点，而非只看"提取成功"这种弱结论）；`test_mineru_cloud_happy_path_and_cache_route` 同步补强 URL 断言。
- **pdf_text_backend 默认值零行为回归**：`test_pdf_text_backend_default_local_unchanged`（不设置/显式设为 local 时路由仍是 "local"，且用调用计数断言绝不触达 `_mineru_cloud_extract`）。
- **pdf_text_backend=mineru-cloud 开启行为**：`test_pdf_text_backend_mineru_cloud_routes_with_is_ocr_false`（断言 is_ocr=False 参数值、路由 "mineru-text"、缓存命中/未命中）。
- **缓存路由隔离**：`test_pdf_text_backend_cache_route_isolation`（同文件先 local 后切 mineru-cloud 不假命中旧缓存，两条缓存独立共存，切回 local 仍命中原缓存）——这条测试就是抓住上面那个"字面 routes 列表与测试要求矛盾"问题的用例。
- **is_ocr 两个调用点**：直接调用层面 `test_mineru_cloud_extract_uses_correct_api_urls_both_is_ocr_values` 覆盖两者；路由层面扫描件走 `test_mineru_cloud_happy_path_and_cache_route`（True）、文字层走 `test_pdf_text_backend_mineru_cloud_routes_with_is_ocr_false`（False）分别覆盖。
- **未配 Key 时文字层分支的 reason 值**：`test_mineru_no_key_text_branch_folds_to_extract_failed_not_scanned`。
- **试验台端到端**：`test_extract_preview_backend_override_reaches_text_layer_branch`（文字层文件 + backend="mineru-cloud" 真调云端且 is_ocr=False，backend=None 时真走本地，两条路径内容互斥验证未被串用）；`test_extract_lab_auto_backend_confirms_when_only_text_backend_is_cloud` + 对照组 `test_extract_lab_auto_backend_no_confirm_when_both_local`（消费上面发现的漏问缺口修复）；`test_settings_text_backend_switch_requires_confirm`（设置页仅切 `pdf_text_backend` 同样需确认）。
- 六件套 + 本轮涉及套件全绿：`test_extractors` **44/44**、`test_gui_store` **0 failures**（含全部新增用例）、`test_config_editor` **0 failures**、`audit_regression` **21/21**、`library_registry` **15/15**、`server_singleton` **5/5**、`config.template_consistency_errors()` 通过、`verify_export_import` **39/39**（真库导出/导入/检索演练）。

### 遗留
- 本项目实际生产库目前**没有**真正开着 `pdf_scan_backend=mineru-cloud` 跑过（已向用户确认），因此这次不存在需要手动挽救的存量数据。但如果以后出现类似情况——曾经开着某个 MinerU 相关后端真实跑过、产生了失败终态——这些条目会卡在旧 `xsrc` 签名下不会被 `current_backend_sig()` 的自动重试机制捡回来（签名字符串本身没变，变的只是代码内部行为/URL），需要用户手动 `--full` 才会重新受益于本次修复。
- 已用本地直提成功索引过的文字层 PDF，用户开启 `pdf_text_backend=mineru-cloud` 后**不会自动重新处理**（这类文件不是终态失败条目，不在 `current_backend_sig`/自动重试机制的适用范围内），需要用户 `--full` 重建才会用新后端重新提取——与 `chunk_char_limit` 等配置类改动的一贯做法一致，本次未新增"检测到配置变化就自动重建"的机制。
- 线B（图片语义描述）、线C（MinerU 结果 LLM 后处理去噪）本轮未动代码，讨论/决策/开放问题原样保留在 TODO.md，留待以后单独开轮。

## 问题 31：GUI 设置页重构——分类导航 + 常用/开发者分层 + 枚举可视化选择（2026-08-26）

用户反馈设置页三个可用性问题：①44 个字段平铺在一个长列表里滚动，"文本混在一起难以定位"；②常用设置和开发者参数没有区分，锁轮询间隔这类几乎永不动的东西和知识库路径并列；③封闭枚举/布尔/模型名全靠手输文本——用户面对 `pdf_scan_backend` 不知道合法值是 `mineru-cloud` 这种魔法字符串，面对 `model_name` 不知道有什么模型可选、`true/false` 也要手打。纯 GUI 层重构，不触碰索引/检索管线，META_VERSION / EXTRACT_VERSION 均不变。

### 实现
- **gui/config_editor.py**：GROUPS 从"组名→字段列表"的二元组列表升级为带元数据的 dict 列表——每组含 `title`/`level`（basic=常用 / advanced=开发者，basic 组整体排在前面）/`icon`/`desc` 一句话说明；分组从 10 个按使用频率重组为 11 个（知识库、模型、PDF 与云端 OCR、检索输出为常用组；融合与排序调优、切块粒度、排除规则、HyDE、性能与硬件、锁与心跳、导出导入为开发者组）。新增 FIELD_META 每键元数据：中文 `label`（吸收原 widgets._cli_name 的映射并补齐此前裸奔的 hyde_*/pdf_*/rerank_* 等 16 键）、`hint` 一句话说明（CONFIG_TEMPLATE 注释的浓缩版）、`rebuild` 标记（结构类配置，GUI 据此打 ⟳ 提醒）、`choices` 封闭枚举（pdf_scan_backend: none/mineru-cloud；pdf_text_backend: local/mineru-cloud）、`suggest` 推荐候选芯片（model_name 四个嵌入模型、rerank_model 三个重排模型，开放值仍可手输不设限）、`secret`（mineru_api_key 渲染成密码框可反显）。写回层（load_raw/save_value/_replace_value/apply_updates/_value_to_json/kind_of/missing_keys/ALL_KEYS）全部原样保留，云端上传确认的数据契约不受影响。
- **gui/widgets.py** `SettingsDialog` 重构为主从布局：左侧 196px 导航列分「常用」「开发者」两小节（含 rebuild 组的 ⟳ 角标），右侧单组详情面板（组图标+说明+该组字段），一次只看一组，彻底消灭长滚动。控件按元数据分流渲染：bool→Switch（不再手打 true/false）；有 choices→Dropdown（选项即合法值，不再猜字符串；若 config.json 被手改成枚举外的值会保真显示为"（当前配置值）"，保存不会静默改写）；有 suggest→输入框+推荐模型芯片（点击回填，仍可自由输入任意 HF 模型标识）；secret→密码框。每个字段的 hint 直接展示在控件下方（⟳ 开头的说明 = 改后需全量重建），标题栏常驻图例。对话框尺寸 680×540→800×560。删除已无引用的 `_cli_name`/`_kind_hint`。
- **打开即刷新**：新增 `_refresh_values()`，每次 open() 从最新 CFG 回填全部控件值——修复既有缺陷（对话框对象随 App 常驻，外部手改 config.json 后再开设置页看到的还是构造时的旧值）。
- 对外契约保持：`_fields[key]=(输入控件, kind)` 结构、`_save`→`_switches_to_cloud`→`_confirm_cloud_backend` 云端二次确认链路原样，tests/test_gui_store.py 三个设置页用例不改一字通过；smoke_gui 的 `_fields`/`_dlg` 断言同样兼容。

### 后记（同日）：「知识库」组语义澄清
用户指出多库架构下设置页却只见"一个库"，误导源头是重构时沿袭的组描述"数据源根目录"。事实：真正的库列表在 `data/libraries.json` 注册表（GUI 工具栏「📚 库管理」管理，每库可覆盖 extensions/exclude/collection），config.json 的 `vault`/`collection_name` 是单库时代遗留的全局默认——现仅剩三个作用：`_migrate_legacy()` 首库自动迁移源（迁移完成后改它对已注册库零影响）、GUI 打开非注册库结果时的兜底路径（app.py `VAULT_DIR`）、接收端 `OBSIDIAN_VAULT` 场景。已把组名改为「知识库（全局默认）」、desc 明确指向库管理、两键 label/hint 同步改写；不隐藏这两键是因为测试契约要求设置页覆盖全部 DEFAULTS 键（test_groups_cover_all_defaults），且旧单库路径（`index_vault(VAULT)`）仍是受支持用法。

追加（用户追问模型与排除语义后）：①澄清推荐芯片≠本机已装清单——本机 RAG 管线实际只有 bge-m3 + bge-reranker-v2-m3 两个模型，芯片是 HuggingFace 推荐候选、点选后首次使用才下载，现芯片行上方有说明文案、当前在用的候选标「✓ 使用中」并高亮描边；②重排/HyDE 组 desc 与 hint 改为白话两步走解释（融合粗筛→cross-encoder 精排；HyDE=先让本地 LLM 写假设答案再检索）；③排除规则与切块粒度组 desc 及字段 hint 明确"全局默认值、可在库管理→库配置按库覆盖"，其中 tbd_exclude_ratio 标注仅全局生效（不在 library.OVERRIDE_KEYS 内）。

### 测试
- test_config_editor.py 新增 4 条静态契约（10 用例全绿）：`test_groups_structure_valid`（dict 结构必备键、level 只取两值、常用组整体在前、键不跨组重复）、`test_field_meta_complete`（每个暴露键必须有非空中文 label 与 hint，FIELD_META 无幽灵键——新键漏写 meta 在测试期就红）、`test_choice_fields_match_defaults`（枚举 choices 必须包含 DEFAULTS 默认值与 mineru-cloud 选项，防止下拉默认值错位导致保存静默改写）、`test_structural_keys_marked_rebuild`（9 个结构类配置必须标 rebuild=True）。
- 六件套全绿：config_editor 0 failures、smoke_gui 全过（44 字段构建）、audit_regression **21/21**、test_gui_store **0 failures**（46 用例，含三个设置页云端确认用例零改动通过）、library_registry **15/15**、server_singleton **5/5**、test_extractors **44/44**、verify_export_import **39/39**。

### 遗留
- SettingsDialog.apply(colors) 仍只存色不重绘（主题切换后已打开的设置对话框沿用旧配色）——重构前即如此，本轮未扩大范围。
- default_libraries 等列表类字段仍是逗号分隔文本输入（写回层 `_value_to_json` 已能拆分），后续可考虑做成勾选块，但库集合是动态注册的，需要先解决"对话框打开时拉取注册表"的依赖方向，本轮不做。

## 问题 32：索引进度看板误报修复——停滞宽限机制 `stall_grace_until`（2026-08-26）

council 两轮评审（`.council-state/round-plan-3/4/`）确认的四个「正常运行被误报心跳停滞」根因：**R1** update_progress 合并语义导致宽限/状态字段跨事件、跨任务残留；**R2** 宽限若用"后写者胜"合并会被更短的值反向缩短；**R3** converting 相位置位后有三个未还原出口（提取失败/空 body/切块），豁免窗口泄漏；**R4** write_lock 排队上限 60s 远超 STALL_TIMEOUT 25s，等锁必误报，且无变更路径直达时 phase 还停在 scanning。机制经方案门两轮评审定稿（maker-v2 + 复审六条强制/建议项）：给进度报告引入**自过期、默认自清**的宽限字段——写入方传相对秒数 `stall_grace_s`（永不落盘），落盘键为绝对截止时间戳 `stall_grace_until`；停滞看门狗在宽限内不判 stalled，心跳停止（DEAD）判定永远优先于一切豁免。

### 实现
- **index.py**：
  - 新增硬编码常量（不进 config，防"永久静默开关"；注释含升级矩阵与阈值漂移声明：lock_timeout>180 或冷加载>300 时对应窗口回退现状误报，非恶化）：`STALL_GRACE_MAX_S=600 / STALL_GRACE_MODEL_LOAD=300 / STALL_GRACE_WRITE=180`。
  - update_progress 改造：pop 掉 kwarg `stall_grace_s` → 默认移除既有 `stall_grace_until`（一次性豁免语义：任何普通进度事件终止宽限）→ 正数值才按 `max(旧值, now+min(grace_s, MAX))` 重写（防回退攻击）→ 非数值/≤0 不写（fail-closed）；docstring 注明语义。心跳 `_heartbeat_tick` 整表拷贝原样保留该字段（宽限在静默窗口内存活靠它）。progress_start 锁下独立 pop 显式清残留（与默认 pop 双保险；不持锁调 update_progress 防不可重入死锁）。
  - 新增 `_stall_grace(seconds, **fields)` 两段式守卫助手（复刻 _report_device 先例）：锁内取内存快照判 running+pid==本进程，锁外才调更新——守卫挡住"无任务写噪音"与"接手强杀残留文件复活死任务"（后者会拖垮 server._index_running 放行逻辑）。判定侧新增 `_stall_grace_left(p, now)` 唯一入口（running + isinstance 数值 + now<截止，非法值视为无宽限）。
  - 四个埋点：①get_model 缓存未命中实际加载分支内 cuda/cpu 两路各一处（显式 MODEL_LOAD 常量；禁止放函数入口——否则每批刷新等于永久静音看门狗）；②_try_switch_back_cuda 入口（盖住 fp16→fp32 双次串行加载与失败回滚恢复全程）；③fallback_to_cpu 收尾标记（其后的静默重载发生在调用方 _encode 内，由埋点①cpu max 合并续写）；④write_lock 前 `phase="waiting-lock"` + 宽限 → 拿锁后 `phase="writing"` 续宽限（waiting-lock 为新增 phase 值，全部消费方核对安全降级：widgets stepper idx=-1 兜底、PHASE_COLOR.get 默认值、progress_ratio 走 else、server 不消费 phase）。
  - G7 单点还原：extract_to_markdown 返回处立即 `update_progress(phase="scanning")`，一处覆盖三个出口——converting 豁免窗口严格闭合于转换真实耗时（600s 级 MinerU 云端 OCR 也完全在窗内，MinerU 不加埋点）。
  - progress_text 分支重排（优先级即判定顺序，与 GUI 镜像勿重排）：DEAD 原文不动 → converting 白名单原文不动（无条件生效不依赖字段，旧读新兼容）→ 宽限内信息行（含 PID[缺失容错为 ?]+已安静秒数+剩余秒数，无告警字样）→ stalled 告警原文 → 正常行。
- **gui/store.py**：heartbeat_state 插宽限分支（DEAD 先于一切 → converting 白名单保留 → in_grace 改判 RUNNING 色/呼吸不变 → stalled），判定表达式与 index._stall_grace_left 逐字镜像、互指注释；新增纯函数 `heartbeat_note(progress)`：DEAD-first 短路（红 DEAD 胶囊绝不配"宽限内"文案）、converting 文案保留、宽限内输出"模型加载/写库中（已安静 Ns，宽限内）"、否则 None。
- **gui/app.py**：内联三元换 heartbeat_note 调用；KPI 行阶段名经 PHASE_TEXT 中文映射（waiting-lock 不裸显英文内部值）。
- **gui/widgets.py**：仅 PHASE_TEXT/PHASE_COLOR 各加一行 `"waiting-lock": "等锁"/"accent"` 映射（不进 stepper）。
- **gui/config_editor.py**：stall_timeout hint 追加"；特定阶段（转换/模型加载/写库）有内置宽限"。
- server.py / extractors.py / config.py 零改动；META_VERSION=9 / EXTRACT_VERSION=2 未动（不影响切块与提取内容）。

### 测试
- **audit_regression_test.py** +16 例（21→37，风格对齐既有 PASS/FAIL + `_ProgressIso` 隔离：清内存表 + PROGRESS_FILE/DATA_DIR/DEVICE_STATE_FILE 重定向临时目录，save/restore 全局）：写侧 G1①普通更新清除、G1②跨任务残留（progress_start 双保险+结构断言 pop 在锁外）、G2 max 合并+clamp、G3 守卫四分支（空内存不写/异 pid 字节不变/同 pid≈now+s/残留文件原样）、助手不可重入死锁（行为探针 acquire(False)+AST 结构断言 With 块内无调用）、类型防御+clamp+判侧 fail-closed、kwarg 永不落盘；判定侧 C2 核心 test_progress_text_grace_states 三断言（宽限信息行含 PID+安静秒数无告警字样/过期恢复告警/心跳冻结仍 DEAD）+ PID None 容错 + G8 converting 无字段白名单边界 + 心跳 tick 保留宽限字段；C3 六条 CUDA 用例（fake torch 注入 sys.modules，index 全懒加载已验证）：②切回入口结构（先于 old=_model 与 _load_model("cuda")，注释含回滚覆盖声明）、③降级收尾标记存在性与尾部顺序+_encode 调用方重载、①双分支结构（缓存命中分支之后、各自先于加载、恰两处）、冷却期内重复降级/切回序列 clamp≤600 且 max 不回退（慢批降级链：③→get_model 重写→②回滚全序列）、无运行任务全部埋点零写入、④waiting-lock→writing spy 序列（真 Chroma 仅落临时目录+假编码器，断言紧邻顺序+两相位带宽限+done 终态无宽限）。
- **test_extractors.py** +1（44→45）：test_converting_phase_restored_after_extract——spy 快照序列断言 converting 后紧跟 scanning、后续无 converting 残留 + 结构断言还原语句位于提取调用与第一个出口分支之间（单点物理覆盖三出口）。
- **test_gui_store.py** +4（46→50 用例 0 failures）：C2 的 test_heartbeat_grace_running_then_expired_then_dead、非法类型 fail-closed、heartbeat_note 六态（含 DEAD-first 短路与已安静秒数）、双看门狗一致性（六个样本两侧结论逐一对照，压住镜像表达式漂移）。
- 六件套全绿：audit_regression **37/37**、library_registry **15/15**、server_singleton **5/5**、test_config_editor **0 failures**、test_gui_store **0 failures（50 例）**、test_extractors **45/45**、verify_export_import **39/39**（真库导出/导入/检索演练）。

### 备注
- 升级过渡期矩阵（方案 §7.1）：旧代码读新文件 = 现状行为（缺字段走原逻辑，converting 白名单无条件兜底）；新代码读旧文件 = 宽限缺失照旧告警；任意方向混跑不劣于引入前。阈值漂移同理：用户调大 lock_timeout 超 180 或冷加载实际超 300 时对应窗口回退现状误报，非恶化。
- 备案（reliability N2）：同进程并发污染（server 后台索引中检索线程触发设备切换写宽限进索引记录）——有界、fail-open、下次进度事件即清除，不改。
- 已知残余：embedding 单批 >25s 仍会暴露（现状如此，批次间有进度事件，属真实病态应暴露）；MinerU 600s 级安静期靠 converting 白名单而非宽限覆盖（§7.2 覆盖链，锁定用例成对）。

### diff 门后记（同日）
council diff 门 6 委员评审：security/product/redteam/performance 四席 PASS；architect 与 reliability 独立报告同一 blocker——_try_switch_back_cuda 的 del old 位于 try 块内且先于 log()/_report_device()，这两句抛异常（stderr 管道断裂等）时 except 回滚分支引用已删除的名字 → UnboundLocalError 掩盖原始异常、回滚未完成，与红线 1"收尾代码自己抛异常击穿容错承诺"同构。修复采用强化变体：引用释放改 old = None 并移至 try 块末尾全部可抛调用之后（若仅原位替换 del→None，_report_device 抛异常时回滚会把 _model 恢复成 None 丢掉 CPU 模型）。按纪律先写复现用例验证 RED 再修绿：test_switchback_rollback_survives_report_device_crash（monkeypatch _report_device 抛哨兵，断言不外泄 + index._model is cpu_model 身份比对恢复 + 冷却重武装 + 原始异常折叠进诊断）。checker 复核 9/9 PASS（audit 38/38、registry 15/15、singleton 5/5、config_editor/gui_store 0 failures、extractors 45/45、verify_export_import 39/39），LSP possibly-unbound 报警消除。

## 问题 33：MinerU 云端请求补 `model_version` 参数 + `pdf_text_backend` 新增 `mineru-local` 占位入口（2026-09-02）

用户与 Claude 在另一条调研会话里通读 MinerU 官方 API 文档后发现：`_mineru_cloud_extract` 的提交请求体从未包含 `model_version` 字段——不是"选择了较弱的 pipeline 模式"，是压根没做选择，服务端按未声明时的默认版本处理（官方文档建议显式传 `vlm` 以获得更高精度，尤其是密集公式、复杂版面场景）。这个遗漏不会以任何错误形式暴露：请求正常返回 200，产出正常写入缓存，只是解析精度低于本可获得的水平——与问题30那次的 404 路径错误不同，问题30会让功能整体失效且容易被察觉，这次是"能用但一直没用最好的模式"，更隐蔽。

顺带处理了另一件事：`pdf_text_backend` 目前只有 `local`/`mineru-cloud` 两个值，本地部署模型（无论是 MinerU 本地 vlm/pipeline 模式、还是未来可能接入的其他本地工具）在"文字层 PDF"这条路径上完全没有入口占位——`pdf_scan_backend`（扫描件分支）已经有 `mineru-local` 这个占位值（问题26起，TODO.md backlog 记录尚未实测联调），但文字层分支没有对应物。而用户的核心场景（工程课件）大多数是有文字层的 PDF，不是扫描件，这条路径反而更常用。

### 实现
- **config.py**：DEFAULTS 新增 `"mineru_model_version": "vlm"`（pipeline | vlm，非法值回退 vlm）；CONFIG_TEMPLATE 对应位置加注释块（紧跟 `pdf_text_backend` 之后、`mineru_api_key` 之前）；`pdf_text_backend` 的注释追加 `mineru-local` 说明；`template_consistency_errors()` 校验通过。
- **extractors.py**：
  - `EXTRACT_VERSION` 2→3：请求体新增字段属于"产出内容会变化"的改动，必须让旧缓存（用未指定版本时的服务端默认产出）整体失效重提，不能只改代码不动版本号——否则用户切换后感知不到任何变化（问题9节前调研反复强调的这一点，这次真正落地）。
  - 新增 `get_model_version()`（照抄 `get_scan_backend`/`get_text_backend` 的防御风格：懒加载 config、非法值回退）。
  - `_mineru_cloud_extract` 提交请求体加 `"model_version": get_model_version()`，两个调用点（扫描件 `is_ocr=True`、文字层 `is_ocr=False`）都自动生效，不需要分别处理。
  - `TEXT_BACKENDS` 新增 `"mineru-local"`；`_extract_pdf` 文字层分支新增该值的处理：**安全退化为本地直提**（走既有 `pymupdf4llm.to_markdown` 路径），只打印一次警告，不返回 `None`。这里的语义特意与扫描件分支的 `mineru-local` 处理（直接跳过不产出）区分开写进了注释——扫描件分支本地零处理能力，跳过是唯一选项；文字层 PDF 本地 pymupdf4llm 本来就能产出内容，"选了本地模型入口但没实现"退化成"不产出"是倒退，所以退化目标是本地直提。缓存路由标签仍记 `"local"`（`_extract_full` 的路由计算逻辑天然如此，因为产出内容确实等价，未改动那段代码）。
  - `get_text_backend()` docstring 补充 `mineru-local` 占位说明。
- **gui/config_editor.py**：`pdf_text_backend` 的 `choices` 加入 `("mineru-local", "本地部署模型（占位，尚未实现，自动退化为本地直提）")`；新增 `mineru_model_version` 的 FIELD_META（label/choices/hint）并加入"PDF 与云端 OCR"组的 fields 列表（否则 `test_groups_cover_all_defaults`/`test_field_meta_complete` 必红——新键漏写元数据在测试期就会暴露，这是问题31留下的静态契约机制生效的一个例子）。
- **gui/widgets.py**：`ExtractLabDialog._hint_text()` 的 `text_tail` 字典补 `"mineru-local"` 分支说明文案（此前若 `_effective_text_backend()` 返回这个值，`.get(..., "")` 会静默落空字符串，用户选中这个选项预览文字层文件时看不到任何相关说明——这正是该方法自己在注释里警告过的那类问题，之前只是没预料到会新增这个枚举值）。试验台下拉本身（`_dd_backend` 三档：跟随全局/本地直提/MinerU云端）不新增选项——它是"单次体验效果差异"用的简化下拉，不是 `SCAN_BACKENDS ∪ TEXT_BACKENDS` 的穷举展示，一个已知会退化的占位选项放进去意义不大，与既有设计定位一致，不改。

### 测试（tests/test_extractors.py，新增 7 例）
- `test_get_model_version_default_and_invalid_fallback`：默认 vlm；显式设 pipeline 生效；大小写不敏感；非法值回退 vlm。
- `test_mineru_cloud_extract_sends_model_version_both_is_ocr_values`：请求体必须携带 `model_version` 字段且跟随配置变化——`is_ocr` 两个取值 × `model_version` 两个取值共 4 种组合都断言请求体实际字段值（不是只断言"提取成功"这种弱结论，问题30已经用同样的教训写过一次）。
- `test_mineru_cloud_extract_default_model_version_is_vlm_when_unset`：config 里完全不设这个键时（例如旧 config.json 未升级），请求体仍必须落到 `vlm`，不能悄悄退回"没有这个字段"的旧行为——这正是本次要修的缺陷本身，必须专门锁死。
- `test_pdf_text_backend_mineru_local_degrades_to_local_no_network`：`mineru-local` 退化为本地直提、产出内容与 `local` 路径逐字节一致、route 落在 `local`、缓存正确写入命中、且用调用计数断言零网络调用（用 monkeypatch 计数替代 `_mineru_cloud_extract`，不依赖真实网络/Key）。
- `test_get_text_backend_accepts_mineru_local`：`get_text_backend()` 认可这个新值为合法（不被非法值防御误伤回退成 `local`——那样配置页选中它、读回时会"看起来什么都没选"，与 GUI 层 `test_choice_fields_match_defaults` 的假设脱节）。
- 六件套完整回归本次未能在开发环境跑通（本机 `.venv` 依赖 chromadb/torch，本次改动过程中用的是另一个受限沙箱，只装了 pymupdf/pymupdf4llm 单独验证了 extractors.py 层面的 5 个新用例，全部真实通过，非仅语法检查）；`config.py`/`gui/config_editor.py` 相关的静态一致性校验（`template_consistency_errors()`、`test_groups_cover_all_defaults`/`test_field_meta_complete`/`test_choice_fields_match_defaults` 的逻辑本体）已在沙箱里手工复现验证通过。**用户本机跑一次 `.venv\Scripts\python tests\test_extractors.py` 和 `.venv\Scripts\python tests\test_config_editor.py` 走完整六件套仍是必要的收尾动作**，本次改动未做过。

### 遗留
- 与问题30相同的性质：本项目实际生产库目前没有真正开着 `pdf_text_backend=mineru-cloud` 或已生效的旧 `model_version` 缺省调用长期跑过（云端调用此前完全没做过验证性实测，属于第一次真正配置齐全后使用），因此不存在需要手动挽救的存量数据；但已用本地直提成功索引过的文字层 PDF，本次 `EXTRACT_VERSION` 递增会让它们的本地直提缓存同样失效重提——这是预期行为（版本号是全局的，不区分"这次改动其实只影响云端分支"），下一轮索引会重新提取但产出内容不变（本地直提逻辑本身未改），只是多一次无意义的重复计算，暂不优化。
- `mineru-local` 目前仍是纯占位——扫描件分支自问题26起就有这个值但从未实测联调（TODO.md backlog 未完成），本次只是把同样的占位机制补齐到文字层分支，让两个分支的配置结构一致、为将来真正接入本地模型（MinerU 本地部署或其他工具）铺好统一入口，没有新增任何本地推理能力，也没有安装任何模型。
- 并行/批量加速改造（滑动窗口限流、有界并发提取、错误分类重试等）本次未动——`index.py` 主循环是高度状态化的单线程扫描/切块/落盘流程，共享大量可变状态（`meta`/`current_rels`/`new_ids` 等），贸然并发化风险远高于本次两处改动，且是架构级决策，按 AGENTS.md"拿不准的设计决策：停下问用户，不要自行扩大范围"，留给用户确认具体方案后再单独开一轮实施，不在本次一并做。

## 问题 34：混合型 PDF 整本按扫描件路由（2026-09-03）

### 背景
用户在另一条调研会话（留档：桌面《obsidian-rag-pdf-研究纪要.md》）里用两份真实工程
课件实测出 `_extract_pdf` 的架构性缺陷：**整份一刀切判定**——`文字层页占比 < 0.5 →
整本按扫描件`，对「PPT 原生文字页 + 教材扫描图」混装的课件两个方向同时翻车：

- **ManometerEquation.pdf**（文字页 3/9 = 0.33）：整本判扫描件，未开云端时零内容入库，
  连 3 页真文字也被丢弃；
- **Note9.pdf**（文字页 8/13 = 0.615）：整本判文字层 PDF 走 pymupdf4llm 直提，第 9-13 页
  **全部例题**（纯图片页）被**静默丢弃**——提取"成功"、无任何异常信号，错误内容直接
  入库。这比提取失败更危险：agent 按导航打开的是一本缺了全部例题的残本。

更糟的是判定极不稳定：某页文字层恰好 9 个字符（卡在 `_TEXT_PAGE_MIN_CHARS=10` 阈值下
方一个字符）就能让占比跨过 0.5 线，整本走向随机翻转。

### 决策
用户拍板：**混合型整本按扫描件处理，不做逐页拆分拼接**——两个不同引擎的产出缝在一起
会有格式/顺序接缝，检索时容易出怪结果；整本交给同一个引擎（MinerU 云端 vlm）从头读到
尾，产出一份连贯完整的 Markdown。配额代价（纯文字页也被视觉认字一遍）在个人库量级下
可忽略（每日 1000 页最高优先级额度余量大）。同轮决策：**read_document MCP 工具暂缓**
（用户主力模型已具备视觉能力，"导航确认 → 直接整份阅读原 PDF"路径成立，md/txt 笔记
agent 平台本就能按路径读；纯语言模型路径当前无真实使用者）——门禁设计存档进 TODO
暂缓条目，将来启用不必重新论证。

### 实现
- **extractors.py**：
  - `_extract_pdf` 重写分拣：逐页检测文字层不变（`_TEXT_PAGE_MIN_CHARS=10`），判定从
    「占比 < 0.5」改为「**存在任何图片页**（`text_pages < page_count`）→ 整本走扫描件
    分支」。阈值只决定"这一页算什么"，整本走向只看"有没有图片页"——边缘字符抖动
    不再能让整本判定翻转。
  - 扫描分支收拢：**只有 `mineru-cloud` 送云端**（is_ocr=True，整本一次提交不逐页拆分），
    其余任何取值（none 默认 / mineru-local 占位 / 文字层语义的 local / 未知值）一律
    跳过落 scanned 终态。收拢顺带封死旧代码的一个口子：旧分支对未匹配取值会
    fall-through 到云端调用（如试验台把 `local` 覆盖值传进扫描分支时）。
  - 未启用云端时的警告文案更新为「PDF 含图片页…已整本跳过」（覆盖混合型与纯扫描件）。
  - 删除 `_TEXT_PAGE_RATIO` 常量（占比阈值层废除，避免留下"看起来还在用"的死规则）。
  - `EXTRACT_VERSION` 3→4：v3 及更早版本对混合型文件会产出半份 local 结果，v4 语义下
    这类文件要么整本云端要么 scanned，产出不同；版本号递增使旧缓存（含 local 路由的
    半份结果）整体失效，防止 v4 的 text_route 候选误命中 v3 半份缓存。
- **index.py / server.py / GUI 零改动**：scanned 终态的 xsrc 自愈机制现成覆盖"开启云端
  后下一轮自动整本重试转正"；试验台的联网确认弹窗（`_will_call_cloud`）按扫描后端
  判定，与新路由天然一致。

### 测试（tests/test_extractors.py，+6 例，56/56）
- `test_mixed_pdf_whole_file_scanned_without_backend`：混合型未开云端 → 整本 scanned；
- `test_mixed_pdf_text_majority_also_routes_to_scan_branch`：Note9 形态（文字页占多数，
  旧规则 ratio=0.8 会判文字层直提）→ 现在同样整本扫描分支；
- `test_mixed_pdf_whole_file_cloud_ocr_when_enabled`：开云端 → is_ocr=True + route=
  ocr:mineru-cloud + 整本一次提交（files 数组单元素，不逐页拆分）；
- `test_all_text_pages_still_local_route`：纯文字层 PDF 回归不变（route=local）；
- `test_scan_branch_only_mineru_cloud_sends_to_cloud`：none/mineru-local/local/未知值
  四种取值全部跳过且零网络调用（注入会炸的网络桩，fall-through 即红）；
- `test_page_text_threshold_9_vs_10_chars`：9 字符页=图片页、10 字符页=文字页的阈值边缘。
- 六件套全绿：extractors **56/56**、audit **38/38**、registry **15/15**、singleton **5/5**、
  config_editor/gui_store 0 failures、verify_export_import **39/39**（同时补上了问题33
  当时未在本机 .venv 跑过的 7 例——本轮全量通过）。

### 遗留
- 存量影响：本机真实库当前没有任何 PDF，升级零迁移负担。将来若发现"老 PDF 还是旧
  结果"，做一次全量重建即可（EXTRACT_VERSION 已保证缓存不会假命中，全量只是省心）。
- 开启云端后的第一轮若恰逢云端故障，混合型文件会从"半份"变成"提取失败终态"（旧块
  清理），恢复后自动重试转正——"宁可诚实空缺、不留半份"原则的代价，接受。
- MinerU 云端批量并行加速（8.2.4 四步方案 + 9.6 健壮性结论）按既定纪律单独开轮，
  紧随本轮实施（见问题 35）。

## 问题 35：MinerU 云端批量并行加速（2026-09-03）

### 背景
TODO backlog 既定项：`_mineru_cloud_extract` 是"提交→上传→轮询[`time.sleep` 原地阻塞]
→下载"的单文件阻塞流程，`_index_core` 主循环逐文件顺序处理、零并发原语——库里有几十
上百份课件要送云端时逐份串行等待，是纯网络 I/O 浪费。方案经调研会话定稿（四步法 +
九个工程健壮性问题的结论），用户批准本轮与问题34 连续实施、各占一个提交。

### 认知基线（方案设计阶段确认，实施时不再重新论证）
- 自建 MinerU 服务的并发配置（环境变量/启动参数/扩容）与云端托管 API 完全无关，不可照搬；
- 官方频控：三个提交接口共用 50 个文件/分钟（滚动）、5000 文件/天、1000 页/天最高优先级
  （超出降级不拒绝）、单批 ≤50 文件——个人库量级距离触顶有量级余量，限流是"体面退让"
  的边界情况，不是要突破的瓶颈；
- "同时处理中任务数"上限官方未公布，并发数必须保守起步（默认 3，做成配置可实测摸高）；
- 只并行网络 I/O 段：`ThreadPoolExecutor` 足够（无需多进程）；所有共享状态变更保持在
  主线程单线程执行，天然无竞态。

### 实现
- **config.py / gui/config_editor.py**：新增 `mineru_concurrency`（int，默认 3，进
  `_POSITIVE_KEYS`；1=串行=并行化前的旧行为，回退用）与 `mineru_rate_per_minute`
  （int，默认 45，0=不限速；对应官方 50/分钟频控留安全余量）。两键进「PDF 与云端
  OCR」组 FIELD_META/fields（test_config_editor 静态契约强制，问题31 机制生效）。
- **extractors.py**（并行基础设施）：
  - `_classify_mineru_code` + `_MineruSubmitError(kind, code)`：提交错误三分类——
    transient（网络异常/429/-10001/-60007/-60009）指数退避+抖动重试（`_SUBMIT_MAX_
    ATTEMPTS=4`，429 尊重 `Retry-After` 头封顶 60s）；fatal（-60002/-60004/-60005/
    -60006）立即失败不重试；token（A0202/A0211）置全局失效标志 `_token_invalid`，
    同批后续请求快速失败、调度方取消未启动任务（停止为注定失败的请求烧频控配额）。
    未知错误码按 transient 处理（宁可多试一次，不误杀）。
  - `_window_delay`/`_submit_gate`：滑动窗口限速（发送时间戳队列 + 最近 60s 计数），
    每次提交尝试（含重试）各占一槽；持锁等待=提交节奏串行化，正是限速目的。
  - 断点簿记 `data/extract_cache/mineru_pending.json`（`_pending_add/_remove/_match/
    mineru_pending_prune`）：上传成功即落 `{batch_id: {path, route, md5}}`，进程被杀
    后条目留存；下一轮同一文件提取时按 path+md5 匹配 → `_mineru_resume` 续接服务器
    端结果（轮询/下载/写缓存），绝不重复提交。终态语义：成功/failed/gone/no-md/empty
    → 移除条目；timeout/download/网络异常 → 保留条目（服务器端任务可能仍在跑或结果
    仍可取）。簿记文件放提取缓存目录下——试验台的隔离缓存目录天然隔离其预览任务
    的中断条目，不会污染生产簿记（红线 7 同一教训）。孤儿清理（文件不在本轮集合/
    字节已变化）在云端段开始时执行。
  - `_mineru_poll_result`：轮询+下载从 `_mineru_cloud_extract` 抽出（新鲜提交与续接
    共用同一条路径）；返回结构化 `(md, why)`，调用方按 why 决定簿记去留。
  - `classify_extraction(path, backend, md5)`：分流预判，与 `_extract_pdf` 路由条件
    严格同源（漂移只影响并行收益、绝不影响产出正确性——真正执行仍走 extract_to_
    markdown 完整路径）；返回 `(kind, pages, is_ocr, route)`，kind=cloud 才攒批。
  - `mineru_cloud_extract_for_parallel(path, is_ocr, key)`：worker 显式入口——PyMuPDF
    不保证多线程安全，worker 只做纯网络 I/O 绝不触碰 pymupdf（路由判定全部在主线程
    classify 阶段完成）；md5 透传免重复读盘。
  - `mineru_quota_add/today`：每日文件数/页数计数（`mineru_quota.json`，翻篇自动清零）。
    仅提醒非硬门禁——降级≠失败，阻断反而制造问题。
- **index.py**（主循环改造，四步法落地）：
  - ①分流：二进制分支先 `classify_extraction` 预判，cloud 且并发>1 → 攒进 `cloud_jobs`
    （rel/fpath/st/bhash/is_ocr/route/pages）继续扫描；inline（缓存秒回/本地直提/
    快速失败）按原路径当场提取。扫描段绝不原地阻塞等云端。
  - 成功路径抽取 `_store_chunks(rel, st, bhash, front, body)` 闭包：链接抽取→清洗→
    空内容防御→两级切块→meta 写入→进度，文本类/本地二进制/云端结果三条路径共用
    （与抽取前内联版本逐行等价）；G7 单点还原语义不变（converting→scanning 紧跟
    extract_to_markdown 返回处）。
  - ②③并行执行+主线程收口：云端段 `ThreadPoolExecutor(max_workers=min(并发, 任务数))`
    包 `mineru_cloud_extract_for_parallel`，`as_completed` 逐个回主线程——失败落
    `_terminal_entry`（带 xsrc）、成功走 `_store_chunks`；Token 失效时 `fut.cancel()`
    取消未启动任务（被取消文件本轮不动 meta，下轮自然重试）。
  - ④进度批量语义："待送云端（已收集 N 个）"→"MinerU 云端并行处理 N 个文件（并发
    M，中断的任务自动续接）"→"云端处理中：已完成 K/N（文件）"；converting 相位豁免
    停滞告警天然覆盖云端长等待。
  - 配额提醒：分发前按 classify 页数预估，今日累计将超 800 页（1000 页额度的 80%）
    打日志提醒"会被降优先级、变慢"。
- server.py / retriever.py / GUI 运行时零改动（config_editor 元数据除外）。

### 测试（tests/test_extractors.py，+11 例，67/67）
- extractors 层：`test_rate_limiter_window_math`（满额等待/窗口滑动/0=不限）、
  `test_mineru_submit_retry_transient_then_success`（两次网络失败后退避重试成功，
  断言 POST 计数与退避记录）、`test_mineru_submit_respects_retry_after`（429+头 →
  恰按 7s 等待）、`test_mineru_submit_fatal_code_no_retry`（-60005 仅 1 次 POST 零退避）、
  `test_mineru_token_error_sets_flag_and_folds`（置全局标志+后续调用零请求）、
  `test_mineru_pending_record_resume_after_interrupt`（轮询途中"被杀"→簿记留存→
  下轮续接：POST 计数不增、簿记清空、缓存写入）、`test_mineru_pending_prune_orphans`
  （孤儿/变更文件条目清理）、`test_classify_extraction_matrix`（8 分支矩阵）、
  `test_mineru_quota_counter`。
- index 端到端（_IsoEnv 隔离）：`test_index_cloud_parallel_dispatch_and_chunking`
  （3 份含图 PDF，Barrier 断言真并发 ≥2，meta 出块、簿记清空）、
  `test_index_token_abort_skips_remaining`（4 任务并发 2：仅 1 个真正上云，其余快速
  失败/取消，索引正常完成）。
- 开发中修了一处自测暴露的 bug：`_mineru_submit` 对 token 类错误 raise 前漏调
  `_token_invalid.set()`（测试先红后绿，正是"Token 失效不置标志则调度方无从取消"）。
- 六件套全绿：extractors **67/67**、audit **38/38**、registry **15/15**、singleton **5/5**、
  config_editor/gui_store 0 failures、verify_export_import **39/39**。

### 备注
- 模拟并发下的真实网络（429/降级）仍属人工冒烟范畴；官方未公布并发上限，`mineru_
  concurrency` 保持保守值 3，用户实测无 429/无大量降级后可逐步调高。
- 断点续接只能挽回"已上传成功"的任务（提交失败的任务服务器端不存在，重提交即是
  正常路径）；簿记文件随缓存目录走，试验台预览中断的条目随临时目录销毁。
- 并行只覆盖"网络 I/O 等待"的重叠；嵌入/写库仍与之前完全相同（锁外编码+锁内写入），
  不在本轮范围。

## 问题 36：`mineru_concurrency=0` 最大吞吐模式 + 轮询瞬时异常退让（2026-09-03）

### 背景
问题35 交付后用户提出：多文件同时送改成可选的"最大限度送"——接近 Limit 就停下，
等结果返回继续送，直到完工。关键澄清：官方的"Limit"是**每分钟提交数**（三接口共用
50/分钟滚动窗口），不是"同时在跑任务数"（该数字官方未公布）；因此 max 模式的正确
形态不是"猜一个更大的并发数"，而是**把固定并发上限拿掉、让既有的滑动窗口限速闸门
成为唯一节流阀**——窗口没满立刻送（最大限度），接近频控原地等（停下），窗口滑动
自动续送；任务完成腾出的线程让排队文件立刻补位（等结果返回继续送），直到全部完工。

### 实现
- **index.py**：`_mineru_concurrency()` 语义扩展——`0`（或负数）= 最大吞吐模式；
  `1` = 串行（旧行为）；`≥2` = 固定并发（默认 3 不变）。max 模式下线程池开到
  内部上限 `_MINERU_MAX_POOL=128`（防异常规模任务撑爆本机线程；个人库规模到不了，
  到达即意味着先撞上每日页数配额，多排队无害）；分流判定条件由 `>1` 修正为 `!=1`
  （max 模式返回 0，旧条件会把它误判成串行走内联路径——自测前人工审查发现）。
- **extractors.py**：`_mineru_poll_result` 响应分流修正——429/5xx 属**瞬时异常**，
  deadline 内按 Retry-After（封顶 60s）或 2×轮询间隔退避后继续轮询，绝不误判任务
  失败（max 模式在途任务多、轮询请求密，撞限流必须体面退让）；404/非 JSON 响应
  （服务器不认识该 batch）才判 `gone`，且 json 解析失败不再外泄（旧代码遇到非 JSON
  响应体会异常外泄 → 外层折叠但簿记条目永久滞留，卡死在"续接一个不存在的任务"上）。
- **config.py / gui/config_editor.py**：`mineru_concurrency` 移出 `_POSITIVE_KEYS`
  （0 是合法取值），DEFAULTS/模板/GUI hint 同步三档语义。

### 测试（+4 例，71/71）
- `test_mineru_concurrency_max_mode_parsing`：0/负数→max、1→串行、5→5、垃圾值→3；
- `test_index_max_mode_all_jobs_in_flight`：5 任务 barrier(5) 全员同时在飞（无固定
  并发上限的端到端证明），meta 正常出块；
- `test_mineru_poll_429_transient_retries_within_deadline`：429 按 Retry-After 等 2s、
  500 按 2×轮询间隔等 6s，最终成功且簿记清空；
- `test_mineru_poll_gone_nonjson_removes_pending_entry`：404 非 JSON → gone + 簿记
  条目移除（堵永久滞留）。
- 六件套全绿：extractors **71/71**、audit **38/38**、registry **15/15**、singleton **5/5**、
  config_editor/gui_store 0 failures、verify_export_import **39/39**。

### 备注
- max 模式的实际节流 = `mineru_rate_per_minute`（默认 45/分钟）；任务耗时分钟级时
  稳态在途数 ≈ 提交速率 × 任务时长，个人库规模下先撞到的通常是每日 1000 页优先级
  配额（超出降优先级、任务变慢但仍完成——超时走既有"簿记保留、下轮续接"路径）。
- 默认值仍为 3：max 模式是**可选项**，用户按需把 `mineru_concurrency` 设为 0。


## 问题 37：WEMM 页级视觉导航 + read_document + 近似文档去重（2026-09-03）

### 背景
纯 bge-m3 文字索引只能定位"哪份文档命中"，检索结果也没有页码概念；扫描件 PDF 更是
整本无字（问题34 的遗憾缩影）。用户提出：加一个**页级视觉导航**——把每份 PDF 的每一页
渲染成图、交给多模态嵌入模型编码成"每页一个向量"，检索时告诉 AI"内容在哪个 PDF 的哪一页"，
从而让有视觉能力的模型直读原 PDF 对应页。配套两条支撑功能：`read_document`（按文件取完整
MD 正文 + 绝对路径）与**近似文档去重**（找出库内内容几乎相同的重复文档）。

### 决策
- **模型 WeMM-Embedding-2B**（腾讯微信视觉团队，多模态 2B，512 维 matryoshka），
  **本地 transformers 服务** `wemm_server.py` 跑在**全局 Python**（torch 2.11+cu128、
  transformers 5.14.1，RTX 5060 Laptop 8GB VRAM 实装 5.08GB 可容纳）；**项目 .venv
  零依赖**——`.venv` 只通过 HTTP 调本地服务，加载模型/编码全部发生在服务进程。
  弃用 Ollama（`/api/embed` 不接受图片、`/api/chat` 不输出 embedding，issue #7677 未解）。
- **两套向量库彻底分离**（红线：绝不混向量空间）：文字索引 `obsidian_kb`（bge-m3 1024 维）
  与页级 `obsidian_kb.wemm`（WeMM 512 维 + 独立 meta `data/wemm_meta_<库>.json` +
  独立版本号 `WEMM_VERSION=1` 独立自愈）；`navigate_knowledge` 绝不与 bge-m3 文本分数混合。
- **默认关闭（隐私/资源优先）**：`wemm_backend=off`；`wemm_server.py --unload-after` 空闲
  释放显存与 bge-m3 共存。
- **门禁/红线**：页级索引在 stat 前冻结未授权文件（红线6/7，零渲染零编码零 I/O，条目与页
  原样保留不裁剪）；`read_document`/去重只读既有提取缓存（`read_cached_markdown` 零触发，
  绝不后台启动扫描件 OCR / 云端 MinerU，红线7）。

### 实现
- **wemm_server.py**（全局 Python 本地看图服务，`python wemm_server.py --port 9101`）：
  `GET /health`（模型/维度/显存）；`POST /embed`（image 页图 base64 或 text → 512 维
  归一化向量）；不支持的 dim 返回 400；日志只含方法/路径不含图文内容（API Key/内容不泄漏）。
  已实测：文本"manometer pressure gauge fluid mechanics"与 Manometer 页图 cos 0.558，
  无关"quantum entanglement" cos 0.357，确定性可复现。
- **wemm_indexer.py**（项目 .venv）：pymupdf 逐页渲染（DPI 120，内存完成不写盘）→ base64 →
  HTTP 编码 → 写独立 `wemm_<collection>`；增量靠 size+mtime 快速路径 + MD5 字节指纹；
  一致性自愈（meta 期望页数 ≠ Chroma 实际 → 全量重建）；版本升级强制重建；删除文件精确清理
  页向量并裁剪 meta；统一终态（empty/extract-failed 不产向量也落 `_terminal_entry` 防 stale
  死循环）；CLI `--library name|all --full --backend on|off`。
- **wemm_retriever.py**：`wemm_search(query, libraries, top_k)` → 文字查询编码 → 对每库
  `wemm_<collection>` 余弦检索 → `(库, 相对路径, 绝对路径, 页码, score)` 降序；backend off /
  服务不可用时返回空 + 明确提示；某库页库损坏跳过不阻断整体。
- **server.py 两个新 MCP 工具**：
  - `navigate_knowledge(query, top_k, libraries, exclude)`：页级视觉导航，返回
    "库/[相对]（绝对路径）第 N 页 + 相似度"，需 wemm_backend 开启。
  - `read_document(library, path)`：按库内相对路径或不含扩展名标题定位（复用文字索引 meta），
    返回源文件绝对路径 + 完整正文；md/txt 直读源文件，pdf/docx 走只读缓存（未提取提示先索引）。
- **extractors.read_cached_markdown(path)**：零侵入缓存读（红线7实现的落点）——只查既有提取
  缓存，绝不触发新提取。
- **dedup.py**（文本级 MinHash+LSH，纯标准库 hashlib）：正文 → 4-gram 碎片 → bottom-k
  MinHash 签名（保留 k 个最小互异哈希；满 k 用 |A∩B|/k、未满用精确交并比）→ LSH 分桶
  （16 段×4 行）→ 桶内 Jaccard ≤ 阈值 → 连通分量分组；**只读建议绝不删改文件**；md/txt 直读源、
  pdf/docx 只读缓存（未提取跳过计数）。MCP 工具 `find_duplicates(library, threshold)`。

### 测试（新增 3 个测试文件：test_wemm_indexer 26 例、test_wemm_retriever 13 例、test_dedup 19 例）
- indexer：页数=向量数、门禁零渲染零编码、增量子自愈、内容变化重编码、删除精确清理、
  损坏 PDF 落 extract-failed 终态、版本升级全量重建、源目录零写入、collection 命名隔离。
- retriever：backend off 空+提示、服务 down 空+提示、命中排序/过滤/绝对路径透出、
  libraries 过滤、空页库不报错。
- dedup：sketch 自比 1.0 / 异文近 0 / 短文精确比、相同文档检出组、阈值过滤、完全不相关
  无组、未提取 pdf 计 skipped、连通分量归并、源目录零写入。
- 实测冒烟：临时库 ManometerEquation.pdf（9 页）→ WEMM 入库 9 向量 → navigate 查询
  "manometer pressure gauge fluid" 命中第 2/1/5/8 页（cos 0.51–0.58）；
  `read_document` 对 Obsidian Vault 一篇 md 返回绝对路径+全文；`find_duplicates` 对
  Obsidian Vault 扫 164 份无重复；冒烟后临时库/页库/服务已清理。
- 全量回归：六件套（audit 38/38、registry 15/15、singleton 5/5、config_editor/gui_store
  0 failures、extractors 71/71）+ 新三套全绿。

### 备注
- WEMM 页导航默认关闭，开启步骤：Config→视觉导航（WEMM）设 wemm_backend=on/local →
  `python wemm_server.py --port 9101`（全局 Python）→ `wemm_indexer.py --library <名> --backend on`
  → navigate_knowledge 即可用。
- 639 个 PDF 的全库页索引是**重活**（~2.4s/页冷启动、热态更快），建议按需对单个库开；
  `--unload-after N` 空闲释放显存与 bge-m3 共存。
- 去重是纯文本级、建议性质，不产向量不改索引，可放心对任意库跑。

## 问题 38：失败溯源（index_failures）+ WEMM 可确认手段（wemm_status）+ 渲染 DPI 档位 + 真实全量建库验证（2026-09-04）

### 背景
问题37 交付两件事：`index_failures` 的姊妹思路已在问题35/36 中提出（「失败清单」诊断工具），
以及用户要求**确认真实库上 WEMM 是否真的生效、由自己亲眼确认**（不能在测试冒烟里自证）。
两个诉求：
(A) 把"提取静默失败"变成可溯源清单；
(B) 让"WEMM 到底建没建、生效没生效"有用户可确认的手段。
另：复杂度发现——**单页嵌入耗时随渲染 DPI 强相关**（非问题37 里估的固定 ~2.5s/页）：
40 DPI≈0.5s/页、60≈2.5s、90≈13.2s、120≈25s；渲染本身仅 0.1s。LECTURE NOTE 共 **382 页**
（18 份 PDF，流体力学课件），60 DPI 全量约 16 分钟、120 则 2.6 小时。

### 决策
- (A) 加诊断工具 `index_failures(library, include_ok)`：读 `index_meta_*.json` 按终态原因
  （unreadable/extract-failed/empty/tbd/scanned）分组列出失败文件，并对「下轮将自动重试」的
  条目标注 `〆`（判定与 `_backend_changed` 同源：reason∈scanned/extract-failed 且
  `xsrc != current_backend_sig()`）。事件日志里的 API Key 仍不进内容（红线8）。
- (B) 两者都做：**加确认工具 `wemm_status()`**（每库 PDF 数/页向量/后端开关/服务存活/渲染失败
  清单，一眼确认真能用）+ **真建全库 WEMM 页索引实证**。渲染 DPI 做成**用户可选项**（默认 60），
  档位 40/60/90/120，改后需 `--full` 重建才能生效——把"快但糊 vs 慢但清"的选择交给用户。
- 长驻 MCP server 的 `CFG` 是 import 时快照：用户中途开关 wemm_backend/改 DPI 后旧进程读不到。
  `navigate_knowledge`/`wemm_status` 改为调用时用 `config.load_config()` 现读 WEMM 相关键。

### 实现
- **server.py 两个新 MCP 工具**（插在 `_dedup_report` 后）：
  - `index_failures(library="", include_ok=False)`：按库分组列失败文件 + `〆 下轮将自动重试`
    标注；import `current_backend_sig` 与 `REASON_*` 共用常量（不硬编码）。
  - `wemm_status()`：读每库 `wemm_meta_*.json` + 实时 config（后端/DPI）+ `health()`，
    报「库：N 份 PDF、M 页向量、K 份渲染失败」；后端 off 时清晰提示不可用。
  - 新增 `_wemm_cfg()` helper：调用时现读 `config.load_config()` 取 wemm_backend/wemm_url/
    wemm_render_dpi 三键，`navigate_knowledge` 门禁与 `wemm_status` 状态都用它（不信任快照）。
- **config.py / gui/config_editor.py**：新增 `wemm_render_dpi`（int，默认 60，choices 40/60/90/120，
  FIELD_META 标 rebuild:True → 改后需 `--full` 重建）。config.json 已自动补写该键。
- **wemm_indexer.py**：`WEMM_RENDER_DPI` 120→60；`index_wemm_library` 读 `wemm_render_dpi`
  并按 `render_page_b64(..., dpi=dpi)` 生效。

### 真实建库与端到端验证（用户可亲眼确认）
- 全库页索引（`--library all --backend on`，60 DPI，后台跑 ~16 分钟）：只有 LECTURE NOTE 有
  PDF（18 份/382 页），Obsidian Vault/test/agents/skills 均 0 PDF → 页库正确为空。**382 页向量、
  18 份 PDF meta 全部落库**。
- `wemm_status()`：`wemm_backend=local，渲染分辨率 60 DPI`、看图服务存活
  （tencent/WeMM-Embedding-2B）、LECTURE NOTE 报「18 份 PDF、382 页向量」，其余库「尚未建页索引」。
- `navigate_knowledge("Navier-Stokes equation viscous incompressible flow")` 返回真实命中：
  Bernoulli 方程 PDF 第 6/11/7 页（相似度 0.62/0.61/0.59）、Fluid Statics 第 33 页等，含绝对路径。
- 说明：`index_failures` 报 LECTURE NOTE 有 5 份 `extract-failed`（FLUID MECHANICS_*），但 WEMM
  页向量对这些 PDF 照样建出来了——页级视觉导航与文字提取相互独立，即使文字层提取失败也能看图导航。
- 交付过程的环境坑（记录备用）：Windows 下 `.venv\Scripts\python.exe` 是**重定向 shim**，会再
  spawn 一个真实解释器子进程——同一 launch 永远显示为"2 个 python.exe（同 cmdline）"，曾误判为
  双开去"杀重复"结果把真 worker 杀了。判定单实例要认 shim+子进程成对，别按进程数。后台长任务
  用 `schtasks /Run`（脱离本 shell，避免工具对前台子进程的 tree-kill），`/TR` 命令行有 261 字符
  上限需包一层 .cmd。

### 测试（test_extractors.py：71/71——含配置隔离修复）
- 修复了一批**既有环境暴露的测试隔离 bug**：真实 `data/config.json` 把 `pdf_scan_backend`/
  `pdf_text_backend` 都设成 `mineru-cloud`（带真 Key），而 extractor 用例的**前置假设**是后端为
  默认 local/none，读到真实配置即报「存在测试间配置泄漏」，一次挂 33 例。
  - 根因：`config.CFG` 是进程启动时从真实 config.json 一次性加载的全局单例，被用户生产配置污染。
  - 修法（不改用户磁盘 config.json）：`_run_all()` 包一层快照——跑测前把 OCR/路由相关键重置为
    `config.DEFAULTS`，测完原地还原。
  - 另两个遗留：`test_preview_job_process_isolation`（子进程 Windows spawn 重读真实 config →
    文字 PDF 被带偏去云端）→ 显式 `backend="local"`；`test_preview_job_uses_isolated_cache`
    （依赖环境全局 `pdf_scan_backend=none`）→ 用例内显式锁定 none。
  - 修复后 extractors **71/71 稳定**（两次跑一致）。
- 全量回归：extractors 71/71、audit 38/38、registry 15/15、singleton 5/5、config_editor/gui_store
  0 failures、verify_export_import 39/39、test_wemm_indexer 26、test_wemm_retriever 13。

### 备注
- 用户要亲证 WEMM 生效：**重启 MCP server**（加载新 server.py + 现读 config），然后调
  `wemm_status()`（看 382 页向量）→ `navigate_knowledge(...)`（看真实命中页）。
- `index_failures` 暴露的待办：`~$BAT3_Technical_Report.docx` 是 Word 锁临时文件（可删）；
  LECTURE NOTE 5 份 FLUID MECHANICS_* `extract-failed`（xsrc=当前签名，不会自动重试，需人工处理）。
- 60 DPI 是速度/精度折中；要更高版面清晰度可改 `wemm_render_dpi` 后 `--full` 重建（耗时见背景）。


---

## 问题39：全面质量审查修复轮（2026-09-04）

### 背景
用户请另一 agent 完成了问题37（WEMM 页级视觉导航 + read_document + 去重）与问题38（失败溯源
+ wemm_status + DPI 档位）后，要求复查其质量。审查（两个独立通读 + 关键结论逐条源码复核）
确认了红线合规面扎实（门禁镜像、零触发提取、无 Key 泄露、去重只读、页图不出本机、测试隔离），
但发现一个 P0、若干 P1/P2，本轮全部修复。用户约束：只改本项目文件、零联网零下载、不动本地
配置环境；跨工作区（Vault 文档组在 D:\_STOREROOM 另一仓库）本轮跳过待用户决策。

### 修复清单（按严重度）
1. **WEMM 失败终态死寂（P0）**：`wemm_indexer` 的 size+mtime 快速路径对终态条目照跳，日志写
   "记入终态待重试"却无任何重试机制——看图服务在索引中途抖一次，该 PDF 永久退出页级导航，
   直到人工 `--full`。这是统一终态红线想防的"死循环"的对偶缺陷"死寂"。修法：终态与成功条目
   一律携带 `xsrc = wemm:<模型>:<维度>:<DPI>` 能力签名；快速路径要求 `xsrc` 匹配且非终态；
   失败条目每轮真重试（失败原因多为服务不可用，重试成本仅一次 page_count/首页渲染即失败，
   可忽略）；改 DPI/换模型自动全量重渲染（原实现只能靠人工 --full，页库会静默滞留旧档）。
2. **写库假账（P1）**：原实现整库一把 `collection.upsert`（Chroma 单批有上限，大库直接炸），
   且 meta 在渲染循环里就记了成功页数——upsert 失败 → 下轮 count 失配 → 整库重编码 → 再失败
   的烧 GPU 循环。修法：分批 upsert（1000/批）+ 全部批次成功才把成功条目并入 meta（`pending_ok`
   延迟落账），写库失败宁可下轮重渲染，不留假账。
3. **显存管理（P1，用户重点）**：`wemm_server.py` 三处——①启动即加载 5.1GB 模型 → 改懒加载
   （启动只绑端口，首个 /embed 才进显存，"需要才拿去"）；②`--unload-after` 只在"有新请求进来"
   时检查空闲，而空闲的定义恰恰是没有请求，永不触发 → 改后台守护线程每 30s 检查 + 卸载时
   clear 引用 + gc + empty_cache 真正释放；③/health 抢模型锁 → 模型加载/编码期间 health 被
   挡 5s 超时，检索方误报"服务不可用" → 改快照读不持锁。另：dtype 参数兼容新旧 transformers
   （`dtype=`+`torch_dtype=` 双传，加载后校验并告警，防旧版静默 fp32 显存翻倍）；编码结束后
   再刷 `_last_use` 防刚编完就被判空闲。
4. **配置热读自相矛盾（P1）**：问题38 声称修了"现读 config"，但只修了外层——`navigate_knowledge`
   外层现读判 on 放行，内层 `wemm_search` 仍读 import 快照判 off 拒绝。修法：新增
   `config.reload_config()`（原地更新共享 CFG dict），`_wemm_cfg`/`ensure_fresh`/`reindex_knowledge`
   /`find_duplicates` 统一在任务边界调用——顺带修掉更重的同类问题：长驻 MCP 进程里 agent 触发的
   reindex 此前完全感知不到用户中途在 GUI 补的 OCR Key/切的后端。
5. **navigate_knowledge 库范围违约（P1）**：docstring 承诺"空=默认库"，实际传 None 给
   `wemm_search` = 搜全部注册库（test/agents 等非笔记库混入）；"all"+exclude 被丢弃。修法：
   统一走 `resolve_entries(libraries, exclude, defaults=...)`。**提示死循环**：工具让 AI"先调
   reindex_knowledge 跑 WEMM 页索引"，而 reindex 根本不建页库——改为指路 CLI
   `python wemm_indexer.py --backend on`。
6. **read_document 补齐存档设计（P2）**：问题37 实现与 TODO 存档设计不符——抬头缺字数/产出
   方式、正文超 2 万字符截断。修法：`read_cached_markdown` 命中改返回产出路由（原样丢弃），
   抬头补 `字数：N　产出方式：本地提取/MinerU 云端 OCR/…`，正文不截断（工具定位就是交付全文）。
   read_document 的保留系用户委托处置待办（2026-09-04"针对代办方案自行决定"），已在 TODO 记录
   下线路径。
7. **index_failures 判定与实际行为相反（P3）**：`will_retry` 要求 xsrc truthy，而 index 的
   `_backend_changed` 对缺 xsrc 的旧条目（None != sig）会真重试——溯源结论说"不重试"实际会重试。
   修法：直接复用 `_backend_changed` 同一谓词；空串 reason 折叠为 `unknown` 并在报告尾部兜底
   渲染（原来从报告无声消失）。
8. **dedup bottom-k 估计量偏置（P2）**：满 k 时直接 |A∩B|/k，把"在两边 bottom-k 里但大于并集
   第 k 小值 z"的交集元素多算——阈值附近边界对系统性偏高（假阳性）。修法：z = 并集第 k 小值，
   分子只数 ≤z 的交集元素。附反例 k=2：A={1,3},B={2,3} 真值 1/3，旧实现估 0.5。
9. **页级检索写副作用（P2）**：`wemm_search` 查询路径用 `get_or_create_collection`——查询会给
   生产 Chroma 创建空 collection。改 `get_collection`；单库异常不再静默吞掉，逐库汇总进 err。
10. **wemm_server HTTP keep-alive（P2）**：HTTP/1.1 下 404/超限分支不读净请求体，同连接下一
    请求把残留字节当请求行解析。修法：先读体再路由 + 错误响应置 close_connection。
11. **死代码/风格（P3）**：wemm_indexer 未用常量与导入清理；server 未用 REASON_* 导入、
    `import os as _os`、函数内重复导入；dedup 硬编码扩展名（含不存在的 "markdown"）改
    `TEXT_EXTS | BINARY_EXTS` 单一事实来源；`dedup._report` 提升为 `format_report` 供 server
    复用（删两处逐行重复）。

### 测试（新增 5 用例；wemm_indexer 36、dedup 23、extractors 72）
- `test_failed_pdf_retried_next_round`（P0 复现先行：服务抖动 → 终态带签名 → 恢复后次轮转正）
- `test_sig_change_reencodes`（DPI 改档自动重渲染 + meta 签名更新）
- `test_sketch_jaccard_bottomk_z_truncation`（z 截断反例 + 满签自比 1.0 + 单侧空 0）
- `test_read_cached_markdown_zero_trigger_and_route`（未命中不触发不写缓存 + 命中返回路由 +
  unsupported 拒绝）
- 其余全量回归：audit 38/38、registry 15/15、singleton 5/5、config_editor/gui_store 0 failures、
  wemm_retriever 13、verify_export_import 39/39。

### 备注
- 既有 WEMM 页库（问题38 建的 382 页向量）的 meta 条目无 `xsrc` 字段，问题39 后首轮增量会
  整体重渲染一次（一次性成本，正好把旧 DPI 向量统一到当前档位），此后稳定。
- `wemm_server.py` 跑在全局 Python（项目外依赖），本轮只改项目内文件未动全局环境；懒加载
  改动对用户透明：启动后 /health 返回 loaded=false，首个导航/索引请求自动加载（首次多等
  数十秒）。
- Vault 文档组（20-Projects/Obsidian RAG/）的同步更新本轮按用户约束跳过（跨工作区），待用户
  决策后补。

---

## 问题40：GUI「文件生效明细」面板——逐文件确认生效状态（2026-09-04）

### 背景
用户反馈两个点：①确认 WEMM/MinerU 是否生效，现有手段（wemm_status / index_failures /
"382 页向量"这种数字）不直观——看不出**哪个文件**是否正确生效；②问题38 交付的失败清单
"列出展开"体验没做好（MCP 输出只有 AI 能看，且 GUI 状态卡上的失败提示只有计数没有文件名）。

### 交付（零侵入，红线4：只读 meta/注册表，不加载模型、不碰 Chroma）
- **store 层（gui/store.py）**：
  - `file_index_rows_for(cfg)`：单库逐文件"未正常入索引"明细（rel + reason + will_retry），
    will_retry 复用 `index._backend_changed` 同一谓词——GUI 说"下轮会重试"就真会重试；
  - `wemm_status_for(cfg)`：单库逐 PDF 页索引状态（页向量数 / 渲染失败原因），
    meta 不存在或为空 = 尚未建页索引；
  - `wemm_backend_state()`（现读 config）/ `wemm_service_probe(url)`（127.0.0.1 回环
    短超时探测，人话返回：模型已进显存 / 待首次请求加载 / 未启动；只读绝不拉起服务）。
- **widgets 层**：新 `FileStatusDialog`——库下拉 + 两个分区：
  - 文字索引区：✔ 正常 N 份；每个失败/跳过文件一行（图标 + 文件名 + 人话原因 +
    "✅ 下轮索引将自动重试"标注）；
  - WEMM 区：后端未开启给三步开启指引；已开启则后台线程探测服务存活（不阻塞 UI）、
    每份 PDF 一行"已建 N 页向量"或"渲染失败：<原因>"；**点任意行用系统默认程序打开
    那份 PDF**（tooltip 显示绝对路径）——配合 navigate_knowledge 返回的页码翻页对内容，
    眼见为实；
  - 主界面入口：顶部工具栏新增"文件生效明细"按钮；状态卡的 ⚠ 失败后缀文案同步指引。
- flet 0.86 API 适配（沿用既有先例）：Dropdown 事件为 `on_select`（构造器不收 on_change）、
  `ft.Padding(...)` 而非 `ft.padding.symmetric`、Container 只有 `on_hover`（e.data=='true'
  为悬入）。

### 测试（test_gui_store.py：55 用例）
新增 5 例：失败明细与重试判定（xsrc 与签名比对两个方向）、meta 缺失空态、WEMM 逐行/
缺失语义、服务探测三分支（mock health）、对话框无窗口构造冒烟（提前抓 flet API 错位——
on_change/on_exit/padding 三个 API 错位全是这个用例先抓出来的）。

### 附记（同轮）：真库演练"双份模型并存"溢出修复
用户实测发现跑回归时两个 python 进程同时持显存（一份 bge-m3 在主进程、一份在检索对比
子进程），8GB 卡直接 WDDM 溢出。根因：`verify_export_import` 第 0 节的主进程检索让模型
常驻，第 6 节又拉子进程各加载一份。修法：`index.release_model()` + `retriever.release_reranker()`
（释放常驻模型 + gc + empty_cache，下次懒加载回来；生产 server 路径不调用——检索模型
常驻是响应速度的根基），演练在第 6 节拉子进程前先让主进程吐模型；子进程本身串行。
验证：39/39 全过，15s 间隔采样全程无"两进程并存"。

### 附记（同轮）：明细面板点开失效文件的 WinError 2
用户点行打开 PDF 报 WinError 2（系统找不到指定的文件）。排查：`test` 库
（Desktop	est）整个文件夹已删除，但文字索引 meta 还留着 17 条旧记录（LECTURE NOTE
的 18 份 PDF 全部在位）。修法：明细行渲染时现查文件在位性——已不在原位置的行换
灰色图标 + 行内注明"文件已不在原位置（下轮索引自动清理）"且**不可点击**；即便点了
（如窗口刷新间隙），`_open_any_file` 也把 FileNotFoundError 折叠成人话提示而非裸错误。
旧记录本就会在下轮索引裁剪（meta 裁剪纪律），GUI 只是把它说破。

---

## 问题41：GPU 显存仲裁——单模型在线、按需拉起、用完即关（2026-09-04）

### 背景
用户拍板：WEMM 是核心功能，应默认开启且全自动——"有需要的时候再自动拉起，用完了直接
自动关闭，确保不和其他模型同时在线"。此前 WEMM 三件套（开关/服务/页库）全部手动，且
9月3日 冒烟遗留的旧 wemm_server 进程一直占着 5.09GB 显存（旧代码启动即加载、无卸载）。

### 交付：gpu_arbiter.py（显存仲裁，纯标准库，.venv 与全局 Python 共用）
1. **服务生命周期**：`ensure_server()` 幂等按需拉起 wemm_server（分离进程、日志
   `data/wemm_server.log`、PID `data/wemm_server.pid`；已有实例只等不重拉；拉起失败折叠
   为 (False, 人话提示) 指向 `wemm_python` 配置）。navigate_knowledge 与 wemm_indexer
   CLI 在需要时调用。**旧实例已清理**（PID 43276，冒烟遗留）。
2. **用完即关**：wemm_server 默认 `--unload-after 300`（空闲 5 分钟卸显存，原为不卸）+
   新增 `--idle-exit 1800`（卸载后再空闲 30 分钟、无在途请求 → 进程自退出，下次按需再拉起）。
3. **显存互斥（单模型在线）**：
   - WEMM 加载前 `wait_for_vram(≥5.5GB)`：bge-m3 在线时不硬抢，等它让路（检索侧空闲
     自动卸载），超时（900s）报错本条请求而非溢出；
   - bge-m3 加载前（index.get_model）`_vram_maybe_evict_wemm()`：空闲显存 <3.5GB 且
     WEMM 在线 → 发 `POST /evict` 抢占（检索优先），WEMM 被抢占的编码批次由问题39 的
     失败终态记账下轮自动重试；
   - MCP server 新增 GPU 空闲卸载守护：600 秒无检索/索引活动 → `release_model()+
     release_reranker()`（复用问题40 接口），"完工直接卸载"；
   - navigate_knowledge 开始前主动释放本进程的 bge-m3/reranker（页级导航用不到它们），
     给 WEMM 让位。
4. **fail-open 铁律**：显存探测（torch → nvidia-smi 兜底）失败返回 None，所有仲裁路径
   视作"无法判断 → 不阻塞不抢占"——仲裁机制自身故障绝不影响检索/索引可用性。

### 配置
- `wemm_backend` 默认 off → **on**（DEFAULTS/模板/GUI）；用户 config.json 本就已是 local（等价开）。
- 新增 `wemm_python`（默认 "python"）：拉起 wemm_server 用的全局 Python（须已装 torch），
  GUI「视觉导航」组同步。
- GUI wemm_status/明细面板文案更新：服务未启动 → "下次导航自动拉起"。

### 测试（新增 test_gpu_arbiter.py：28 例；全套件变十件套）
探测 fail-open / wait 分支 / evict 折叠 / ensure_server 四分支（已运行不重拉、冷启动
拉起+PID 落盘、存活实例只等、拉起失败折叠）/ 端口与 PID 边界。测试先红后绿抓出两个真
bug：**父进程日志句柄泄漏**（with 修复）与**遗留旧实例**（本机 9101 真有旧服务在跑）。
九件套全绿 + gpu_arbiter 28/28。

### 备注（显存时间线，用户视角）
- 全静默：0 模型在线（server 10 分钟自动卸、WEMM 5 分钟卸 + 30 分钟退）。
- 检索：bge-m3 上（~2GB），完走自动下。
- 导航/页索引：WEMM 上（5.1GB），检索若同时发生会先抢占 WEMM（页索引失败批下轮续）。
- 任何时刻最多一个模型驻留显存；加载都走懒加载，磁盘加载代价按用户决策不值一提。

### 附记（同轮）：建页库接入索引管线——增量/全量重建自动触发
用户指出预期：建页库应跟随索引自动跑，而不是只有命令行。落地：
- `index_library` 文字索引完成后自动调用新增的 `_wemm_auto_phase(lib, full, agent_allowed)`：
  wemm_backend off → 静默跳过（零开销）；先 `release_model()+release_reranker()` 把本进程
  bge-m3 让出（文字嵌入已完成，显存互斥）；再进 `index_wemm_library`（增量/全量标志透传、
  Agent 门禁透传）；**任何异常只记日志，页级导航问题绝不波及文字索引成果**。
- `index_wemm_library` 改服务**懒拉起**：首个真正需要渲染的文件才 `ensure_server`，
  无变更轮次零拉起零开销（替代 main() 的预检，CLI/自动两路共用同一逻辑）；拉不起时
  该文件记 extract-failed 终态（带 wemm 签名，下轮自动重试）。
- 文案同步：wemm_status / GUI 明细面板的建库指引改为「跑一次索引自动建页库」。

### 附记（同轮）：「功能有但没接上」专项审计——4 实锤修复
按用户要求做接线完整性审计（config 键读写对照、TODO/占位标记、公开函数引用、高危路径核查）。
结论：config 全部键有读者；源码占位标记全部是有文档背书的刻意设计（mineru-local 本地部署
入口）。实锤并修复：
1. **串行路径 Token 永久锁死（P1）**：`mineru_token_reset()` 只在并行云端段调用——
   `mineru_concurrency=1` 的串行路径在长驻 server 进程里一旦置位 Token 失效标志就永远
   快速失败，用户补好 Key 也要重启进程。改为**按索引轮次复位**（_index_core 开头）：
   轮内首次 Token 错误后其余文件仍快速失败（不烧频控配额），下一轮自动恢复。
2. **配额记账串/并不对称（P2）**：`mineru_quota_add` 只在并行调度方统一入账——串行路径
   真实提交从未计入每日配额，800 页预检提示对串行用户失效。改为**提交点记账**：
   `_mineru_cloud_extract` 在每次真实提交（_pending_add 之后）调 `mineru_quota_add(1, pages)`，
   页数由 _extract_pdf（两分支）与并行 worker（cloud_jobs 已带）透传；续接与缓存命中
   天然不计（不走提交行）。
3. **首跑同步误触发页库全量建库（回归自堵）**：接线审查发现 server.ensure_fresh 的空库
   首跑分支同步调 index_library——若不设防，一次搜索会同步阻塞在数十分钟的页库建库上。
   `index_library` 增 `wemm_sync=True` 参数，首跑分支传 False（页库交下一轮常规索引）。
4. **AI_GUIDE 文档断链**：补 §9——WEMM 自动化行为、GPU 仲裁、诊断顺序、新 MCP 工具速查。
5. 小清理：`gpu_arbiter.WEMM_MIN_VRAM_GB` 成为 wemm_server `--min-vram` 默认值（单一事实
   来源）；index.py 移除已无调用的 `mineru_quota_add` 导入。
测试：extractors 73/73（+1 配额记账三段式：提交计 1 / 续接不计 / 缓存不计）、
wemm_indexer 49/49（+1 wemm_sync=False 跳过接线）；既有 6 个用例的假函数签名随
pages 参数同步更新。
- 测试 +4（wemm_indexer 47）：接线三态（on/off/异常吞并）+ 懒拉起（首轮恰好 1 次、
  无变更零拉起、拉起失败落可重试终态）。**测试纪律新增一条教训**：懒拉起接线后，未打桩
  的既有用例曾尝试真 spawn wemm_server（_wait_health 干等 120s 表现为套件挂死）——
  套件级默认替换 ensure_server 为假实现，杜绝测试拉起真实 GPU 服务。

## 问题42：guiweb——pywebview 桌面壳 + 全库图谱 GUI（与 Flet 版并存）（2026-09-06）

**动机**：demo 验证了「深黑玻璃 + 全库图谱」方向后，评估 GUI 架构：Flet 的
Python↔Flutter 双进程 JSON 桥扛不住逐帧画布动画（力导/涟漪/拖拽跟手），玻璃拟态
表现力受限。选型 **pywebview（单依赖，原生窗口 + WebView2 GPU 合成）+ HTML/JS
前端 + Python 后端原封复用**：Python 3.14 实测可用（.opencode/pywebview_smoke.py），
净增 1 依赖、可卸掉 flet-desktop 数百 MB 运行时。原 Flet GUI（gui/）**原样保留**，
两套并存（独立锁文件 data/guiweb_instance.lock）。

**架构**（guiweb/，约 4600 行）：
- `contracts.md`：前后端唯一契约（28 个 js_api 方法 + 4 类推送事件）
- `bridge.py`：js_api 桥——快照/库管理/索引/检索/设置/诊断/试验台/导出导入。
  复用 store/worker/config_editor/library/extractors/dedup 全部现有模块，
  零逻辑重写；检索结果在**后端集中解析为结构化 JSON**（替换文本协议正则散落）；
  检索/语义边均为纯读路径（不碰 Chroma、不写文件）
- `graph_data.py`：全库图谱纯函数——meta 的 links 字段直接建双链图（同
  resolve_note_relations 规则）、PDF 管线四态从 meta 终态推导（done/queued/
  failed/none，缺失在力学结构上就是"没有子节点"，WEMM/MinerU 是否生效图上
  可验证）、WEMM 页节点（>24 页折叠组节点）、磁盘未识别 PDF、主题族归簇
- `semantic.py`：可选语义边（encode_safe 编码「标题+路径」，阈值≥0.62，
  每节点 top-4 邻居，内存缓存按指纹失效）
- `app.py`：文件字节锁单例守卫（移植）+ pywebview 窗口 + 1 秒 snapshot 推送线程
- `ui/`：深黑玻璃生产前端（demo7 皮肤 + demo1 视图），六视图（图谱/检索/库/
  索引/试验台/诊断/设置）+ 动态岛 + 日志抽屉 + 全部确认门禁（全量红确认/
  移除勾选/导入逐字/云端同意），离线零 CDN
- `wiring_check.py`：接线静态检查——契约方法三向对齐（bridge/mock/app.js）、
  id 引用完整、无外链（离线铁律），纳入回归

**真实 bug 修复（可复现）**：
1. 检索整体提示行被渲染成幽灵结果（Flet gui/widgets 同样存在）：低置信查询的
   「（本次查询整体置信度偏低…）」游离行被当来源行渲染成畸形结果卡。
   guiweb.parse_search_text 归为 notice 条目渲染横幅。
2. mock 数据曾把真实 mineru_api_key 写进仓库文件（get_settings 导出未脱敏）——
   已清除；教训：任何"导出真实配置到代码/文件"的操作必须先过 secret 字段脱敏。

**测试**：tests/test_guiweb.py 45/45（解析 7、格式化 3、主题 7、双链 3、图谱
管线/页节点/折叠/hub/范围 12、语义边 4、接线检查 1、…）；gui/test_gui_store.py、
test_config_editor.py 基线零回归；接线检查全绿；pywebview 真机冒烟
（窗口+WebView2+双向桥）通过，真实数据实跑验证（5 库快照/397 节点图谱/
失败明细/WEMM 探活）。

## 问题43：guiweb 真机首轮反馈修复——设置页空下拉根因 + 原生路径弹窗 + 图谱检索反馈（2026-09-06）

**动机**：用户真机反馈三件事——图谱页检索"没有正确生效"、设置页"几乎全部多选项
不生效"、路径类输入只会粘贴，要求点一下弹原生选择窗口。

**根因定位（computer-use 驱动真机 + 运行日志取证）**：
1. **设置页空下拉（真凶，JS 空数组 truthy）**：`fieldRow` 用 `if (f.choices)`
   分支，而 bridge.get_settings 对无选项字段返回空数组 `[]`——JS 里空数组是
   truthy，导致几乎所有字段（含 bool 开关、文本框）被渲染成**零选项的空 select**，
   整个设置页不可用。修复：`if (f.choices && f.choices.length)`。浏览器与真机
   截图双重复现，Python 侧 apply_updates 回写链路实测无辜（临时副本 roundtrip
   全对：str/int/bool/list 落盘 + 注释保留）。
2. **图谱检索"没生效"**：日志证实检索链路本身通（旧实例 charmap 报错来自修复
   前进程；新实例 36.4s 冷启动成功后 5-6s 常速）——真正的问题是冷启动 30-60s
   界面零反馈，且 `G.searching` 守卫静默吞掉重复点击。修复：检索按钮 busy 态
   （禁用 + "检索中…"）+ 重复点击 toast 提示；顺带修掉 **clearGLit 不清理上一轮
   `.g-conf` 置信度角标**的残留 bug（浏览器复现：两轮连续检索旧角标滞留）。
   另离线核对真实数据 id 匹配：真检索命中（FLUENT 配置.md）与图谱节点
   `lib|rel` 完全对上。
3. **原生路径弹窗**：新增契约方法 `pick_path(mode, start)`（pywebview
   FOLDER_DIALOG/OPEN_DIALOG，start 用输入框现值定位起始目录），接线三处：
   设置页 vault（选文件夹）与 wemm_python（选文件）、添加库弹层路径、提取
   试验台文件路径。浏览器 mock + 接线检查同步。

**测试**：test_guiweb.py 48/48（新增"空 choices 渲染守卫"回归 3 用例——
长度判断存在、裸 if (f.choices) 禁绝、真桥 choices 一律数组）；十件套全绿
（38+15+5+0fail+0fail+73+49+13+23+28+48）+ verify_export_import；接线检查
全绿（pick_path 三向对齐）；浏览器实测两轮连续检索角标不残留、保存链路 toast
正常、busy 态往返正确。

## 问题45：检索置信度语义锚与展示分零点重标定——分数终于"0=无关、1=强命中"（2026-09-06）

> 编号备注：本条最初以"问题44"提交（commit 230f358）,但同日另一会话已把
> "库内路径级勾选"占用问题44（commit d5538e5 及其代码 docstring）,故整体改挂
> 问题45。本条覆盖两轮:前半=语义锚（已随 230f358 入库）,后半=零点重标定。

**动机**：用户质疑"检索分数最低只见 0.50、最高只见 0.70,0.73 和 0.50 看着只差
23%,实际却是'精确命中'vs'完全无关'的差别——这么小的数值差会不会误导 AI agent"。

**实测取证（九组真库查询：3 确定命中 / 3 模糊口语 / 3 库中不存在）**：
- 噪音地板 0.50~0.52（重排器 logit≈0 = "无法判断"，不是"半相关"）；
- 确定命中 top1 也只到 0.73（"GPU 显存仲裁"这条 vault 明确有整章内容的查询
  top1 仅 0.61）；模糊口语型全部 0.50~0.56。
- 结论：有效动态范围只有 0.50~0.73,用户体感完全属实。
- 附带发现：`confidence_drop_threshold=0.40` 的噪音护栏在 sigmoid 分数下**永远
  不触发**（地板 0.50 > 0.40）,形同虚设;warn=0.55 才是日常真正起作用的护栏。
  drop 保留作降级路径兜底（RRF 分场景单路第 2 名 0.375 仍需要它）,注释如实改写。

**前半轮修复——语义锚（只改展示与说明,排序/过滤逻辑零改动）**：
1. **语义锚**：`retriever._format_results` 来源行升级为
   `[置信度 0.73·高相关]`。分档边界取自实测分布：高相关 ≥0.65
   （`CONF_TIER_STRONG`,与实测强命中带 0.65~0.73 对齐）、中相关 ≥warn(0.55)、
   其余弱相关——中/弱分界直接引用 warn 阈值,单一事实来源。
2. **两套 GUI 同步**：flet `_parse_src` 与 guiweb `_RE_CONF` 正则兼容带档位
   后缀（旧格式继续兼容）;配色档位按实测重校准、guiweb `scoreBadge` 同步。
3. **工具说明写明读法**：`server.search_knowledge` docstring 增加置信度解读段,
   供 LLM 破除百分比直觉;config 模板与 GUI 配置编辑器的阈值注释同步如实。
4. **评估后不做**：sigmoid 温度拉伸——单调变换不改变排序,对 agent 的解读错位
   无实质帮助,只会移动错位位置。

**后半轮修复——展示分零点重标定（用户追问"未命中怎么还有 50%"后拍板）**：
语义锚只是"翻译数字",数值本身的零点错位仍在——sigmoid 把"证据=0"（完全无关）
映射到 0.50。落地 `retriever._conf_display()` 分段线性重映射,锚点取自实测分布：
`0.50(噪音地板)→0.00`、`0.55(warn 线)→0.20`、`0.65(高相关线)→0.85`、
`0.73(强命中上限)→1.00`,锚点外钳位 0~1。效果：红烧肉类无关查询显示 0.00~0.08,
确定命中显示 0.85+——**未命中归零,数字直觉与语义一致**。三处输出（来源行分数、
低置信标注数值、整体低置信头部"最高 x.xx"）全部换用展示分;排序、drop/warn 阈值
过滤、HyDE 触发判断**仍用原始分**（阈值是原始分尺度,注释写明换算关系：
warn=0.55 原始 ≈ 展示 0.20,高相关线 0.65 原始 ≈ 展示 0.85）;两套 GUI 配色
档位换到展示分尺度（绿 ≥0.85 / 青 ≥0.20）。
**约束：锚点基于当前重排模型（bge-reranker-v2-m3）实测,换打分模型必须重测**——
已写进 retriever 注释、audit 守卫用例与 AI_GUIDE/使用指南。

**测试**：audit `test_confidence_tier_semantic_anchor` 扩展（分档边界 + 重标定
锚点/单调性/钳位 + 展示层接线 + GUI 一致性 + 分档重标定不进排序路径）;
test_gui_store 配色档位断言换展示分尺度 + `test_parse_src_confidence_with_tier`;
test_guiweb 增 `test_parse_confidence_tier_suffix`。相关套件全绿 + 真检索端到端
确认新数值。

## 问题44：库内文件/文件夹级勾选建模——路径级排除全流程生效 + MCP 硬门禁（2026-09-06）

**动机**：用户要求库内按文件/文件夹粒度决定建不建向量库；被排除的文件**全流程
彻底不碰**（BGE/WEMM/MinerU/pymupdf 一律不触达）；AI Agent 经 MCP 提议变更必须
经用户确认（硬编码，无配置绕过）；GUI 在库管理每库出「勾选范围」右侧抽屉。
设计经用户逐项拍板（定稿并入本节，不再单开设计文件）：
①嵌套冲突=离文件近的显式选择赢；②新文件默认=跟随全局格式；③**无保护概念**——
全局格式开关=对该格式文件的批量勾/取消（显式勾选的"青苹果菜单"也跟着取消），
文件夹级选择不被批量触碰；④MCP 确认走对话流（agent 展示 diff + 确认码），用户
不一定开着 GUI。

**实现**：
- 数据面（library.py）：`selection_in`/`selection_out` 存注册表条目（相对路径，
  不进 OVERRIDE_KEYS——路径可能含逗号，专用 set_selection 增删）；resolve_selection
  最近显式赢；format_selection_bulk 格式批量语义（文件级按扩展名判定——子目录
  文件也有斜杠，不能以"/"有无区分文件/文件夹）；bulk_for_extensions 在 set_config
  的 extensions 变更上收口（GUI/CLI/MCP 单一漏斗；**必须在 save_registry 之后调**，
  其内部重读注册表读改写，先调会被本次 save 覆盖——实测踩过）；effective_config
  输出规范化两表 + selection_default（config 新键 selection_new_files，默认 follow，
  DEFAULTS/模板/config_editor 三处同步）
- 扫描漏斗（index.py collect_md_files）：新增 selection/selection_default 参数，
  过滤优先级 exclude_* 硬排除 > 显式勾选（显式 in 可穿透扩展名白名单）> 中性默认；
  全部 11 处调用点穿线（index/export/dedup/wemm_indexer/server/gui-store/graph_data）。
  **排除的语义 = 对管线不存在**：条目按既有 removed/幽灵路径裁剪、块清理、WEMM
  页向量裁剪，全部复用现有机制，零新终态类型（kb_stale 全排除稳态不误报，实测）
- MCP 硬门禁（selection_gate.py + server.py 薄封装）：propose 生成提案（提案号 +
  6 位确认码，盘上只存哈希，10 分钟 TTL）→ apply 校验码+TTL+一次性消费；确认码
  错误/过期/重放全拒绝并写审计日志。诚实边界：agent 理论可不真问用户直接带码
  apply——MCP 通道信任边界，两段式+过期+审计把风险压到最低。read_document 对
  显式排除的文件直接拒绝（中性但格式未启用不拦——那是过滤默认非排除决定）
- GUI（guiweb）：库卡新按钮「勾选范围」→ 右侧滑出抽屉：面包屑下钻/上钻（不越
  库根，bridge 强制校验）、文件夹/文件勾选框 + 生效态徽章（显式 in/out /
  auto_in/auto_out）、显式选择带「跟随」恢复按钮、格式快捷批量行（确认提示
  "将清除该格式单独勾选"）、目录懒加载、改动攒批+保存（GUI=用户本人，免确认码）。
  契约 3 新方法（selection_tree/selection_update/selection_format_bulk），接线全绿

**测试**：tests/test_selection.py 63/63（norm/resolve/set/bulk/ext 收口/eff/
collect 过滤 7 态/gate 11 态/bridge 9 态/kb_stale 收敛联动）；修 4 个测试期望错误
（中性 pdf 漏算、crumb 排序、根斜杠现属绝对路径拒绝、gate 捕 ValueError 而非仅
GateError）；十件套 + test_guiweb 53 + test_selection 63 全绿，接线检查全绿。
修真 bug 2 个：set_config 先 bulk 后 save 的读改写覆盖；collect 的文件级判定
"/"有无→按扩展名。

**设计定稿（数据模型与判定规则，实施前经用户逐项批准；决策背景另见 Vault ADR-19）**：
- 数据模型：libraries.json 每库 `"selection_in": ["课件/青苹果菜单.pdf"],
  "selection_out": ["私人/账单/"]`（库内相对路径，/ 分隔；不在两表 = 中性）。
  新库无两表 = 全按格式纳入（"默认全部勾选"）；新文件天然中性（跟随格式，
  可由 config `selection_new_files`=follow/include/exclude 调节）
- 判定优先级：exclude_* 硬排除 > 最近显式选择 > 格式开关 extensions >
  中性默认；GUI 徽章与 collect_md_files 逐条对齐（显示=实际）
- 已知取舍：路径键 = 相对路径，文件改名/移动后选择丢失（按新文件走默认），
  不做移动追踪；中性 include 档可穿透白名单（仅受支持格式）
**文档同步**：AGENTS/TODO/Vault（操作手册勾选范围段、决策记录 ADR-19、
Roadmap 状态）。

**问题44 附记（真机验证发现 2 个显示层真 bug，均已修 + 回归）**：
1. 中性文件夹显示"排除（跟随格式）"——文件夹没有扩展名，落进格式判定必 out；
   改为中性文件夹 = 跟随子内容（auto_in）。
2. 徽章没反映 exclude_dirs/files/patterns 硬排除（AGENTS.md 在排除名单却显示
   "入库"）——selection_tree 的 _state 补齐 exclude_* 三条规则（与 collect_md_files
   逐条对齐，且置于显式选择之上不可穿透），显示=实际。test_selection 65/65。
3. **UI 迭代（用户反馈"看不出文件夹/文件/类型区别、左边全是无用空间"）**：勾选
   面板从 560px 窄抽屉重做为 1180px 宽幅两栏弹窗——左栏整棵目录树（一次拉取
   folders 扁平结构、depth 缩进、状态圆点、点击切目录），右栏当前目录文件列表
   （文件夹行图标+加粗底色 / 文件行类型徽章 MD绿·PDF红·DOCX蓝·TXT灰·不支持灰），
   底部图例。真机调出一个渲染 bug：目录行复用 tree-ico 图标类，该类尺寸规则只在
   .sel-tree-row 作用域下，落进右栏后 svg 无约束放大到 452px，图标描边叠成巨块
   遮挡列表——双类修复。DevTools 定位法：RAG_GUIWEB_DEBUG=1 开 DevTools +
   elementsFromPoint/copy() 到剪贴板（SVG className 是对象不是字符串，typeof
   判断会误报无类名）。
4. **用户反馈"树完全平铺没有层级"**：renderSelTree 里 `var ind = f.depth*14+'px'`
   先拼了 'px'，后面 `8 + +ind` 把 "14px" 转数字得 NaN，padding-left 全部非法，
   整棵树视觉平铺。改为数字参与运算再拼 px，缩进恢复。
5. **用户反馈"没有展开收起，默认收起"**：目录树默认全展开一整列，大库上直接
   刷满。改为展开态模型（SEL.expanded，默认仅顶层可见）：行内 ▸ 箭头切换
   （纯前端重渲染，不重新拉数据）；点目录名导航时自动展开其子层；右栏面包屑
   深跳时祖先链自动展开保持可见。无子目录行箭头占位隐藏。

## 问题46：GUI 增量/全量重建"启动后无响应"——gpu_arbiter ROOT 指错致 WEMM 空等（2026-09-08）

**症状**：GUI 点增量重建后显示启动但进度不动、界面似卡死；再点报"已有任务在运行"/
"启动失败"；任务管理器里 python 占 0% CPU、显存 0 占用。全量重建同病（同一管线）。

**根因**（三级放大）：
1. `gpu_arbiter.py` 在项目根目录却写了 `ROOT = ...parent.parent`（子目录模块才用
   双 parent），PID/日志/拉起脚本路径全指到上级 `C:\Users\xbl26\projects\data`。
   实锤：该目录凭空出现 + 其 `wemm_server.log` 里 14 条 `can't open file
   'C:\Users\xbl26\projects\wemm_server.py'`。
2. `ensure_server` 拉起的是瞬间死亡的子进程，却照常 `_wait_health` 空等满 120s。
3. `wemm_indexer._ensure_server_lazy` 失败不 memoize——每文件一次 ensure，N 个待渲染
   PDF = N×120s。且 `_wemm_auto_phase` 在 WEMM 同步前已释放 bge-m3——进程睡等、
   模型已卸，正好是"0 CPU + 0 显存"。
文字索引本身数秒即完成（日志为证），全卡在每库的 WEMM 自动同步里。

**修复**：
- `gpu_arbiter.ROOT` 改 `.parent`（全仓唯一用错的地方，其余 parent.parent 都在子目录里，逐个核过）；
  删上级目录残留的 stale `wemm_server.pid`（30984，已死），错位日志保留为证据。
- `ensure_server` 双保险：脚本缺失直接 False（不 spawn 不等）；拉起后 5s 早夭检查，
  秒退立刻 False（正常 torch 冷启动进程存活，不受影响）。
- `index_wemm_library` 同轮单次拉起（tried 旗）：失败即记终态、下轮重试，不再同轮反复空等。
- 回归：test_gpu_arbiter +4（ROOT 断言/脚本缺失秒回/早夭秒回；旧"固定列表式" health mock
  改状态函数以兼容轮询），test_wemm_indexer +1（3 文件失败只拉起 1 次且都落可重试终态）。
  十件套其余 8 套全绿；verify_export_import 与本次路径零交集且需动真库+加载真实模型，
  本次未跑（曾因其"中途零输出、最后才汇总"被误认卡死而中止，中止本身无残留）。

## 问题47：WEMM 用完即卸 + 页同步进度上报 + 占用显示（2026-09-08）

**动机**：用户拍板——看图模型用完必须立刻卸载、不许后台静默占显存（要"释放显存
按钮"本身就是设计失误）；且索引时要能分清"真完工"还是"等 MinerU"；界面要常驻
显存/GPU/CPU/功耗。权限红线：只读计数器（nvidia-smi/系统时间 API/回环 /evict），
普通用户身份，不提权、不装依赖、不写系统目录，执行前拿用户审批（已批）。

**设计（按用户修正）**：不是每库一卸，而是**整轮全部库完成、没人用了再卸**——
收尾点在三个多库驱动处（index.py main / server._run_index / wemm_indexer.main，
finally 调 `release_server_after_run`，自身绝不抛异常；中途被杀由服务端 idle
守护 5min 自卸兜底）。抢占路径（bge 加载前显存不足调 evict）本就存在，覆盖
"别的进程需要时及时释放"。

**改动**：
1. `wemm_indexer.release_server_after_run` 新增 + 三处收尾调用。
2. 页同步阶段进度上报（文件级，用户拍板）：`_wemm_auto_phase` 经 progress 回调
   写 `phase="wemm"`（每文件推进，无需停滞宽限），收尾回到 done（此前末库会
   永远停在 wemm/running）；`index_wemm_library` 回调协议扩展为 (msg, done,
   total)，单参数旧回调 TypeError 回退兼容。
3. 前后端 `wemm→页库/页库同步` 映射（Flet PHASE_TEXT/COLOR、guiweb PHASE_NAME；
   stepper 两端都不进，与 Flet 旧决策一致）。
4. `store.gpu_stats()`（nvidia-smi，进程内 5s 缓存，fail-open）+
   `store.cpu_percent()`（ctypes GetSystemTimes 差值，psutil 遵注释不引入）→
   bridge 快照 `gpu/cpu/wemm_live`（wemm 实况 30s 缓存，防 health 日志刷屏；
   只读探测绝不拉起）→ guiweb 占用行 + Flet DeviceBar 同步显示。
   contracts/mock/接线检查同步更新。
5. 回归：wemm_indexer 60/60、gui_store（+3）、guiweb 63/63、arbiter 37/37，
   其余 8 套全绿；verify_export_import 与本次零交集（不碰 WEMM/GPU 路径）
   且动真库，本次未跑。
**附记（同日真机）**：修完用户仍报"点了显示启动、底下毫无反应、找不到功耗
行"。查实两条：①功耗行在诊断页，索引页看不见——已在索引页加 `idxSys` 行；
②真根因是生产版 app.js 从未定义 `window.__push`（只在 mock.js 里有），bridge
的每秒推送全被 `&&` 守卫静默吞掉，KPI/进度永远停在启动那一刻——只有直接 API
调用有反应。修复为顶层同语义分发器（与 mock 互覆盖无害）。   回归进
test_snapshot_sys_fields_contract，guiweb 66/66。
**附记2（同日）**：用户报增量时闪现"有别的 MCP 在调用"、全量不现。查实并无
MCP 进程——快照内 busy 与 running 来自两次读盘，5 秒跑完的增量必跨一次翻转，
拼出撕裂帧。修复：`index_busy(progress)` 单快照复用 + 新增 `index_task_owner`
四态（ours/starting/foreign/idle），前端门锁改走 task，"启动中"有独立文案，
   冤枉 MCP 的时代结束。回归：gui_store/guiweb 相关用例全绿。

## 问题47 附记2：勾选与排除打架——谁具体听谁的（2026-09-08，用户拍板）

**症状**：90-Archive 同时躺在 exclude_dirs 与 selection_in 里——保存成功、
文件有值，但树形永远显示排除（"回退"假象）。根因是旧优先级"排除名单永远
优先"没有给"点名道姓的纳入"留活路。

**用户拍板的三条语义**（指名道姓 vs 按类匹配）：
1. 全局规则指名道姓（exclude_dirs 写确切位置）+ 勾同一个位置 = 两道命令打架
   → 点击当场弹窗（不等保存）：仅本库移除排除并纳入 / 放弃；
2. 全局规则按类匹配（exclude_files/patterns/格式：凡叫这名的都算）+ 点其中
   某一份 = 一般规则+个别例外，意图唯一 → 静默放行，不弹窗
   （AGENT 例：全局按名排除某类文档，点名要其中一份即生效）；
3. 漏斗改"谁具体听谁的"：最近显式 vs 目录排除按深度（文件点名可穿透继承的
   目录排除，反之亦然）；同位置打架的手工态排除站住（安全），且保存侧与
   MCP 提案侧拒绝新建。

**实现**：裁决唯一函数 `library.decide_included`（显示 bridge._state 与漏斗
collect 共用）；`set_selection` 同位置拒绝（带指引）；`selection_gate`
提案侧事前拦截（确认码不花在注定无效的提案上）；bridge
`selection_resolve_conflict` 单方法原子解决（只写本库覆盖，全局不动）；
前端复用 askConfirm 即时弹窗。回归：test_selection 88/88（含翻转的旧断言
exclude_files+显式=纳入），其余十件套全绿（vee 除外，零交集）。
附带堵住测试污染生产日志：test_bridge_selection 重定向 worker.LOG_FILE
（此前"神秘 t/…/x 成对日志"即测试 bridge 调用所写）。
悬置未解：20:07:59 与用户手动保存同秒、同库（seltest_tmp）出现的一条
'../../x' 拒绝记录，发送方不明（影响为零：被拒且原子）。

## 问题48：提取质量第一档——索引层噪声清洗（2026-09-08）

**做了什么**：`_finalize` 漏斗新增三段纯文本清洗（`clean_wikilinks` 之后）：
`strip_dead_image_refs`（本地死图链→留 alt/删整段，远端活图不动）→
`strip_page_number_lines`（第X页/Page/-N-/多裸数字分页信号）→
`strip_boilerplate_lines`（逐字重复≥3次的普通段落行；标题/表格/列表/引用/
围栏/短行<4字一律保护）。META_VERSION 9→10（红线：清洗逻辑变更必升版），
下轮索引触发一次全量重建；`test_extractors` 版本钉子同步到 10。
**真库实测**（1446 份缓存 md，只读）：样板 307 份命中删 130 万字符、页码
147 份删 5.3 万字符、死图链 23 份删 5.6 万字符。抽查 Top 文件确认为真实噪声
（`<!-- Start of picture text -->` ×2081 等 pymupdf4llm 标记残留、各章重复页眉）。
附带发现：71 份 `.v1.md` 是旧命名孤儿缓存（现行查不到，死重，另行清理）。
回归：audit 43/43（新增 4 项）、extractors 73/73、guiweb 89/89、其余全绿。
**诚实声明**：清洗只在索引层，提取缓存原文不动——MCP `read_document` 全文
仍带死链），第一档本质是"靠重复次数猜样板"（误伤案例：某教材"Problems"×87、
"Thus,"碎句连词），治本靠第二档官方标注。

## 问题48附记：MinerU 结果包真实抓包（2026-09-08，第二档前置验证）

两个 1 页合成 PDF 真实提交（共 2 文件×1 页，配额已记账），结论全是实测：
- zip 内：`{id}_content_list.json` + `content_list_v2.json` + `layout.json` +
  `{id}_model.json` + `full.md` + `{id}_origin.pdf` + `images/`（有图时才有）。
- v1 清单 schema：`{type, text, page_idx, bbox}`；实测 type 有 text / header /
  footer（待含页脚样本）/ page_number / table。探针1的裸 "42" 被官方标为
  page_number——第一档的裸数字启发式猜对了，但官方标注零误伤。
- table 块结构：`{type, table_body(HTML), table_caption(list), table_footnote,
  img_path(指向images/), page_idx, bbox}`，无 text 键；实测 table_body HTML
  完整正确，但 caption 归因是启发式的（把表格下方的 Figure 图注误贴给表格）。
  full.md 里表格已是 markdown 形态，因此"HTML 替换 md 表格"收益未经证实，
  决议：表格不替换，json 只用于删 header/footer/page_number。
- v2 清单首条是嵌套 list（非扁平 dict），施工用 v1 即可。
- 治本施工方向（待授权）：解包处多存 sidecar `{md5}.v4.mineru.json`（与 md
  同键前缀、版本联动；同一字节至多一份有效 json，ocr/text 路由互斥故与路由
  无关；老文件无 json 走第一档回退）→ `_finalize` 双轨清洗（有 json 按官方
  类型删行、无 json 沿用第一档）→ META_VERSION 10→11（一次全量重嵌，GPU
  时间，零配额；EXTRACT_VERSION 不动，零重提）。图片落盘已决议暂不做。

## 问题48附记2：sidecar 双轨清洗施工落地（2026-09-08）

按抓包结论施工（用户"继续"授权），零配额零重提，META_VERSION 10→11：
- `extractors._mineru_poll_result(batch_id, headers, deadline, requests, key=None)`：
  解包处若 zip 含 v1 `content_list.json` 且给了 key，顺带原子落 sidecar
  `{md5}.v{EXTRACT_VERSION}.mineru.json`（`_cache_put_sidecar`）；文件不含 route——
  同一字节的 scanned/文字层 由内容确定性互斥（问题34），local 直提永不产。
  失败静默跳过（OSError 只记日志），正文交付绝不被 sidecar 拖累。
  `read_cache_sidecar(key)` 只读、损坏返 None。续接路径 `_mineru_resume` 同带
  `key=job["md5"]`（断点续接也落 sidecar）。
- `index._store_chunks` 双轨：`clean_wikilinks` 后 `read_cache_sidecar(bhash)` →
  有则 `strip_sidecar_noise`（整行逐字精确删 header/footer/page_number，标题行/
  表格/正文不动），再跑 v10 启发式（页码/样板）兜底删官方没标出来的残差；
  老文件无 sidecar → 全启发式，行为与 v10 完全一致。
- 真库：71 个 `.v1.md` 孤儿缓存移入 `data/extract_cache/_orphan_v1_bak_2026-09-08/`
  （现行命名 v2 起查不到的死重，非删除、可回滚）。
- 回归：extractors 74/74（+sidecar 落盘/读取测试）、audit 44/44（+官方标注精确删
  纯函数测试）、guiweb 89、其余套件全绿。
- 生效边界诚实声明：只有**新提交/续接成功的 MinerU 云端提取**会产 sidecar；老缓存
  （含 v10 那批云端文件）正文里已存在的页眉页码仍靠启发式，要让它也享受官方标注，
  只能等文件字节变化自然重提或日后手动"刷新 MinerU 缓存"（那会烧配额，未做）。
  图片落盘已决议不做；表格 HTML 替换已决议不换（实测 caption 归因启发式不可靠）。

## 问题50：guiweb 诊断页——失败明细"全部库=0"/标题计数错，WEMM 明细空列（2026-09-08）

**症状（用户报障）**：诊断页 ①失败明细选"全部库"恒 0 条（点进每个库其实都有）；
标题显示 vault "共176条"却只列 2 行；错误行点不开、看不到具体报错。
②WEMM 页库明细选了 vault 只出一排空行——没文件名没页数、状态全是"页库就绪"、
点不了。
**根因（三个独立的显示层 bug）**：
1. `Bridge.failures("")` 把空串当库名 `next(name=="")` → 直接返回"库不存在"→ 0 条
   （应聚合全部库）。
2. `store.file_index_rows_for` 的 `total`=正常索引文件数、`rows`=失败行——bridge
   原样转发，前端拿它当"失败条数"标题：vault 176 是正常数，真失败其实就 2 行，
   "共176条"纯属标签语义错位。
3. `store.wemm_status_for` 行是元组（Flet/widgets 共用契约，不能改 store），guiweb
   bridge 却原样转发不给前端做对象化——前端按 `r.rel/r.pages` 取字段恒 undefined，
   `r.failed` 恒 falsy → 每行空文件/空页数/一律"页库就绪"；且无任何点击入口。
   另：终态条目本身只存 reason 无自由文本错误，"更具体报错"只在索引日志里。
**修复（只动 guiweb 桥与前端，store 元组契约保持供 Flet）**：
- bridge：`failures(lib)` 支持 ""/"all" 聚合全部库，total=失败条数、healthy=正常数、
  multi 标记、每行 {lib,rel,reason,will_retry,detail}——detail 用 `_log_snippet_for`
  从 GUI 日志尾挑最近提到该文件的 ≤3 行（真报错出处）；
  `wemm_status` 把元组转 {lib,rel,pages,failed,reason} 对象。
- app.js：标题改"共 X 条失败 · 正常 Y 份（全部库）"；失败/WEMM 行点击展开详情行
  （库 chip、处置建议、will_retry 解释、日志摘录 pre、打开源文件按钮）；多库聚合时
  文件名前显示库 chip。
- mock.js / contracts.md 同步新形状；app.css 补 libchip/fd-* 样式。
**回归**：test_guiweb 89→96（新增 failures 聚合/计数语义 + wemm 元组→对象两条，
store 层函数需 patch `store.*`——bridge 是 `import store` 路径注入加载，patch
`gui.store.*` 会打到第二份副本上不生效）；audit 44、gui_store 0 failures。
**教训**：两个 GUI 共用一个 store 层时，"桥→前端"的整形必须各写各的，别把 Flet
能懂的元组直接喂给 JS；跨包 patch 注意模块加载方式（sys.path 注入的顶层名 ≠
包名路径，会变两份副本）。

**问题50 续（用户追问后补，2026-09-08）**：
- **WEMM 为何出现 md**：wemm_indexer 本就只收 pdf（collect 传 ["pdf"]），但
  collect_md_files 的"显式勾选 in"会穿透扩展名白名单（文字索引设计：点名要
  某文件即纳入）——Obsidian Vault 的 selection_in 含 00-Inbox/90-Archive/
  AGENTS.md，被勾选目录里的 md 因此混进 WEMM 当前集合并反复尝试渲染失败。
  修复：wemm_indexer 循环开头对非 `.pdf` 硬 continue（不进 current 集合 →
  结尾裁剪连 meta 条目与页向量一起清掉旧残留）；gui/store.wemm_status_for
  显示层只列 .pdf 兜底（历史残留即时隐藏，下次 WEMM 索引自动物理清除）。
  已实测：真库 wemm_meta 里 AGENTS.md 等 md 条目是旧版混入的残留。
- **失败明细详情语义**：用户要的不是贴日志，而是"白话为什么失败 + 怎么修怎么
  避免下次"；日志降级为一个小"复制日志"按钮（navigator.clipboard，点一下
  复制该文件最近日志摘录）。前端新增 REASON_WHY/REASON_FIX 故障树文案
  （scanned/unreadable/extract-failed/empty/tbd/unknown 各配"为什么"与
  "做法"），行展开显示：为什么没进来 / 怎么处理 / 是否自动重试 / 按钮组。
- 回归：test_guiweb 96、test_gui_store 0 failures（wemm 测试加 md 条目滤除
  断言）、wemm_indexer 60、node --check 双绿。

## 问题49：索引完成后全局回收 GC——"没用到就删"（2026-09-08）

**背景**：提取缓存是"按指纹追加、只增不删"，文件每改一次就留一份孤儿
（71 个 `.v1.md` 是手工清的，证明没有自动回收）；已从注册表移除的库还残留
文字/WEMM collection 与指纹文件。用户拍板：全清，每次索引完成即回收。
**盘点结论**：Chroma/WEMM 的"已删文件的块/页"每轮索引本就在精确清理
（index.py 裁剪+collection.delete、wemm_indexer 同构），库级残留与新活只在
"整个库没了 / 提取缓存孤儿"。
**实现**：`index.prune_unreferenced_data(data_dir/cache_dir/chroma_dir/entries
可注入，默认真实路径，幂等，任何单步失败只记日志绝不抛)`，只读快照后删：
1. 已删库指纹文件（index_meta_<n>.json / wemm_meta_<n>.json；base
   index_meta.json 默认入口保留）；
2. 活着指纹 = 各注册库 meta 的 hash 并集 + 在途 MinerU 断点簿记 md5
   （续接任务绝不被误删）；
3. extract_cache 顶层 md / sidecar / md5 前缀 tmp，指纹不在活着集即删；
   子目录（_orphan_v1_bak 等）与 mineru_pending/quota 簿记不碰；
4. Chroma 残留 collection（不属于任何注册库的文字或 <col>.wemm）删。
**触发**：仅整轮索引**全部成功**后——CLI `__main__` 与 `server._run_index`
（GUI/MCP 共用）finally 里，had_error 时跳过（meta 可能不完整，宁可不回收）。
**测试**：新增 `tests/test_prune.py` 5 用例（孤儿缓存/已删库 meta 与 collection/
在途保护/无注册库 no-op/子目录不碰），全隔离目录注入；全套件回归绿
（audit 44、extractors 74、guiweb 89、wemm 60/13、gpu 37、其余照旧）。
**边界诚实声明**：回收基准是"已注册库当前 meta 引用"——被排除/未索引文件、
已移除库的缓存会删；此后 `read_document` 对这类文件返回 not-cached（本就
不在索引里，语义一致）。修 bug/测试曾用非 hex 假指纹（g/h）被正则正确拒绝。

## guiweb 提取试验台"秒完成但空屏"（2026-09-08，用户报障）

**症状**：guiweb（注意不是 Flet 版）试验台，本地直提/MinerU 都是"完成 · 耗时
1s"但渲染/源码两格全空。状态行文案与 `guiweb/ui/app.js labPoll` 逐字对应，
定位到 guiweb 链路。

**根因两处（都在显示层，提取管线本身正常）**：
1. `bridge.preview_poll` 误读 `info["markdown"]`——`extract_preview` 的键是
   `md`，恒取空，成功提取永远空屏；
2. `reason/route/cached/elapsed/chars` 被丢弃，且子进程正常交付即 `ok=True`
   ——管线级未产出（scanned/缺 Key/空文件，MinerU 无 Key 瞬间失败也是 1s）
   同样冒充"完成"。
Flet 版 `_render` 用 `info["md"]` 一直是对的，故只有 guiweb 中招；FEATURE_PARITY
曾标"代码 ✅"但从未经真机验证。

**修复**：映射抽成纯函数 `Bridge._preview_result_of`（`md→markdown` + 全字段
透传）；前端三结局如实区分——完成（路由·字数·管线耗时）/ 未产出（原因+处置
建议，复用 REASON_LABEL/ADVICE）/ 失败；mock 与 contracts 同构跟进。
验证：真 spawn 子进程 payload 经新映射端到端出内容（local/54字）；用户环境
实测全局文字层后端 mineru-cloud 云端链路本身是通的（首轮复现 10s 真回包），
之前只是显示层全丢了。回归：test_guiweb 89/89（新增 16 项映射+契约断言）。





## 问题51：MinerU 本地部署 R3b——pipeline 本机解析（2026-09-09）

背景：8GB 卡（RTX 5060 Laptop）+ Windows + 全局 py3.14。云端配额够用，
做本地的核心收益是隐私（不出内网）与离线可用。官方后端三选一后定 pipeline：
vlm 本地引擎要 8GB 显存（本机贴线死路）、http-client 需另有算力服务端。

架构（照抄 wemm_server.py 模式）：mineru tool 环境（uv tool install 的
py3.12，全局 `mineru` 命令可用）跑 `mineru_server.py`（:9102，壳+串行锁+
懒加载+空闲卸载）；壳内复用 MinerU 官方 `ReusableLocalAPIServer` 管
`mineru-api` 子进程，对外只有 /health /parse /evict，单文件同步 POST
/file_parse（pipeline/auto/ch，return_md 内联 JSON）；.venv 只用 urllib
说话，零新增依赖。HF_HOME 只作用于子进程环境（E:\models\hf），C 盘
15GB 现有 HF 缓存原样不动；HF_TOKEN 走用户变量。

仲裁（checker B1）：拉起前先 evict_wemm → wait_for_vram(4.5)；bge 加载前
index._vram_maybe_evict_wemm 反向 evict_mineru；wait 超时不等 900s，直接
deferred 下轮重试。端口顺延 9102→9104（用户拍板）。

提取语义：扫描分支 mineru-local 真调用，route=ocr:mineru-local；服务瞬态
不可用→"deferred"（本轮跳过、不落终态、不动 meta，对标云端 Token 失效跳过）；
xsrc 签名带 tool 就绪位（后装环境自动翻转重试，checker B3 闭环）；超页上限
（配置 mineru_local_max_pages，默认 200）→scanned 提示拆分；EXTRACT_VERSION
4→5（防陈旧云端正文/sidecar 误命中；增量轮零重提，无需 --full）。
文字层 mineru-local 保持占位退化（开关预留）。

测试：extractors 78（1895 翻转+deferred+假服务路由+sig 就绪位+_IsoEnv index 级
deferred+classify local 行）、arbiter 52（resolve/顺延/evict 新增）、其余全绿。

**问题51附记（真机联调抓到的孤儿进程 bug，2026-09-09）**：
生产链路首跑失败 `torch.AcceleratorError: CUDA unknown`——根因不是模型/驱动，
是显存被 11 个测试残留进程吃光（6.5GB/8GB）：壳被 Stop-Process 强杀后，
Windows 下内 mineru-api（stdin 守望缺席）不会随父进程死，模型常驻显存。
修复：`mineru_server.py` 记内服务 PID（`data/mineru_inner.pid`，优雅退出清掉），
启动时 tasklist 验明 python 身份后认领回收（验不出 fail-closed 宁残留不误杀）。
教训：凡"父管子进程"结构，强杀路径必须有 PID 认领，否则每个调试会话漏一波
显存——WEMM 单进程结构天然免疫， MinerU 双进程必须显式处理。
生产链路复测全绿：ensure 拉起 :9102 → 解析 18s → `ocr:mineru-local.v5` 落盘 →
复用目录 cached=True。

**问题51附记2（用户“全量重建像增量+进程常驻”报障排查，2026-09-09）**：
三件事拼出的误会 + 两个真 bug。
(1) 用户那轮重建时后端还不是 mineru-local（v5 缓存 12 cloud + 4 text + 1 local，
0 local 产出），扫描件秒跳 + 文字层秒提，看着像增量；随后 LECTURE NOTE 库在
bge 加载时撞 HF DNS 瞬断（getaddrinfo failed）整库失败，体感“超快完工”。
(2) 真 bug A：并发 ensure 开出双壳同绑 :9102（allow_reuse 下共存、pid 互踩、
显存 double）——加跨进程拉起锁（msvcrt.locking，30s，持有者崩溃系统自动放），
锁超时只做只读复用；回归 test_ensure_mineru_concurrent_single_launch。
(3) 真 bug B：进度文件 WinError 5 一轮 510 次——Windows Defender/搜索索引与
原子 replace 竞争（仓内无第二个写者），_write_progress_file 加 3 次短退避重试。
(4) 非 bug：外壳 30min/内服务 5min 空闲退出是设计值，不是泄漏；试验台 41 页
preview 解析成功但只写临时缓存，所以生产 0 local 缓存是对的。
用户重跑全量前置条件：后端已切 mineru-local、DNS 已恢复、残留双壳已清。

## 问题52：检索命中默认渲染 + GUI 内正文查看（2026-09-10）

> 用户需求：GUI 里输出 md 时要看到渲染好的而不是纯字；要点开直接不离开 GUI 看正文。
> 两套 GUI 一起改（Flet gui/ + guiweb），共享层只加只读接口，不动索引/提取管线。

- 共享层：`store.read_document_text(cfg, rel)`——md/txt 读源文件，pdf/docx 只读既有
  提取缓存（未提取报 not-cached 指引先增量重建，绝不后台触发 OCR/云端，与
  server.read_document 同红线）；路径穿越/绝对路径拒绝；超长 20 万字截断。
- guiweb：`bridge._md_to_html` 从极简升级为 GFM mini 渲染（h1-h4/分割线/引用/
  ul-ol/GFM 表格/围栏代码/行内样式，先转义后套标签，XSS-safe）；`search()` 给
  每条命中带 `rendered_html`，前端默认渲染视图 + 渲染/源码切换 + 标签感知的查询
  高亮；新增 `read_document` + 正文弹层 mDoc（渲染默认/源码切换/字数路由抬头/
  截断提示/打开源文件）；mock/契约/FEATURE_PARITY 同步。
- Flet：命中展开态改 `ft.Markdown` 渲染；收起态此前只有标题行（注释与实现不符），
  现补去标记纯文本预览；每条命中加"在窗口内查看正文"按钮 → 正文弹层（渲染/
  源码双签，对齐提取试验台写法）+ 在外部打开。
- 测试：test_gui_store 新增直读 5 项 + 去标记/渲染/按钮 3 项；test_guiweb 新增
  渲染器/search 挂载/read_document/契约 parity 4 项；audit/config/extractors/
  registry/singleton/dedup/gpu/wemm×2 回归绿；verify_export_import 按用户要求跳过未跑。
- 教训：Windows 下 `os.path.isabs("/etc/passwd")` 为 False，绝对路径判定须显式补
  前导 "/"（用例先红后修，见 test_read_document_text_traversal_blocked）。

## guiweb 库配置门禁不动键名漂移修复（2026-09-11，用户报"agent_allowed 非法配置键"）

> 用户在 guiweb 库配置弹层改 Agent 门禁，保存报"部分配置未通过校验：
> agent_allowed 非法配置键"。深挖确认是前后端键名漂移，且显示/保存双坏。

- 根因：后端持久键是 `agent_formats`（OVERRIDE_KEYS 成员），`agent_allowed` 只是
  index_library/server 的运行期参数名；guiweb `app.js` CFG_KEYS/保存/回显三处全用
  错键 → 保存 100% 被拒，且 `effective.agent_allowed` 永为 undefined 导致开关永远
  显示关闭（即使 Flet/MCP 已批准，如 LECTURE NOTE 的 `agent_formats=['pdf','docx']`）。
  另有值语义坑：旧前端送"全部 extensions"，后端只收二进制子集，对上键名也会撞墙。
- 修：app.js 切 `agent_formats` + 开只发"对话框勾选格式的二进制交集"（用框内实时值，
  不用陈旧 eff，否则"取消 pdf 勾选+开门禁"撞"未启用无法授权"）+ 无二进制启用时禁用
  开关（对齐 Flet）；mock.js/bridge all_keys/contracts.md 同步；library.py docstring
  钉"持久键唯一、禁别名"+ CLI config 查看补 extensions/agent_formats/collection 覆盖态。
- 回归（test_guiweb 新增 2 用例 13 检查）：CFG_KEYS⊆OVERRIDE_KEYS 静态契约 +
  mock/bridge all_keys 一致 + 真桥开/关/旧键拒/非二进制拒端到端。`tests/run.py` 14/14 绿。
- 顺带结论（非问题，免后人重查）：全局配置层无漂移（config DEFAULTS/模板一致性校验 +
  config_editor.GROUPS 全覆盖 + guiweb 复用同一 GROUPS）；MCP 批准流/Flet 开关/export
  导入均正确；selection 不进 OVERRIDE_KEYS 是有意设计（逗号切坏路径）。

## 测试基建提速（2026-09-10，用户需求：全套太慢，不断言删减前提下降耗时）

> 用户拍板：固定隐藏测试库常驻 / 真模型只留 1 次 / 坚持标准库手写 /
> 每次仍全量必跑（不接受按改动选测）/ 先给切分清单再动手。

- `tests/hidden-vault/`（14 文件全覆盖：frontmatter+双链+围栏+死图链笔记/
  txt/空md/深路径/文字层pdf/扫描件/混合课件/大写PDF/中文名pdf/表格docx/
  空docx/大写Docx/坏docx/坏pdf，含"免费证书"检索词）+ `tests/make_hidden_vault.py`
  幂等生成器；永不进 `libraries.json`，生产索引扫不到。
- `tests/run.py` 统一入口（标准库零依赖，单进程 A→B→C，套级计时 Top10，
  套间快照/还原 library/config/index/gpu_arbiter/retriever 全局）：14/14 绿，
  实测 44.5s。`--suite` 调试单套，各文件仍可单独跑。
- `verify_export_import` 全隔离化重写：默认连隐藏库（`--vault` 可指真库手动演练）；
  全部落盘（chroma/meta/导出/缓存/vault_export/归档/注册表）重定向双临时区，
  提取后端钉死 none/local，HF 走离线缓存；7 个子进程→进程内直调函数；
  真模型 3 次加载→1 次（索引 embedding + 同进程 1 次真检索），两库对比改向量
  余弦免模型；38/38 绿，13.1s，真 `data/`/Vault 零触碰。
- 实测抓到两处真污染并清理：`reload(import)` 会重置落盘常量回真实路径
  （真目录多了 `vault_export/hidden-test`，已删——绝不 reload）；`retriever.CHROMA_DIR`
  是 import 期绑定旧值（真库多了空 `kb_hidden-test`，已删——补丁须同步改，
  已记入 AGENTS.md 测试纪律）。核对真库 collection 回到 9 个。
- B/C 组复核不动：probe/服务全 mock 已最简；`PersistentClient` 不可换内存库
  （同进程双内存库互不可见，读数永远 0），提速全靠入口复用导入 + verify 重构。
- AGENTS.md（统一命令+隐藏库+绑定坑纪律）与 HANDOFF.md（14 套命令）同步更新。

## 问题53：图谱第一屏可读性/性能/检索动效返工（2026-09-10）

> 用户反馈（guiweb 图谱视图，默认第一屏）：1）文档多时字堆字零可读；2）显存占用高；
> 3）放大后字与卡片模糊；4）检索只出两条，且不是"搜索词居中、命中连过去、其余排外围"；
> 5）整体像杂草。逐条根因定位后返工。

- 标签堆叠：此前每个节点常显 `.g-label`。现标签密度策略——缩小（k<1.05）或可见
  节点>350 时只留 hub（big)/命中（lit)/选中（sel)/悬停的标签（stage 级 class 翻转，
  不逐节点操作）。
- 显存：`.g-world` + 每个 `.g-node` 的 `will-change:transform` 造成几百个常驻合成层
  （各带阴影/文字光栅）是 VRAM 大头；卡片阴影 22px→10px；>500 节点自动进
  `.perf` 模式去阴影。
- 缩放模糊：同上 will-change 把 world（含 12000px SVG 画布）锁成巨型层，旧光栅放大
  后不清。去 will-change 后静止态正常重绘即清晰（不动坐标数学，零风险；曾评估 CSS
  zoom 方案，因位移换算要除 k 且 WebView2 外行为待验证，否决）。
- 只出两条：`API.search(q, topK=5)` 取 5 个块，去重到文件常只剩 2 个节点。现取
  `max(topK*4,20)` 个候选，按文件分组取最高置信度，上限 12 个文件上三环轨道
  （半径封顶 130/220/310 并按当前缩放折算）；非命中节点加径向排斥力 drift 向外围，
  清除检索后自动回落；toast 报文件数；此前从未被写入的 `G.lastSnips` 现真正落地，
  Inspector"命中片段"区被激活。
- 杂草：WEMM 页节点图层默认关闭（圆点+虚线边是杂乱主源；Inspector"定位页节点群"
  本就带自动开图层，不受影响）；库锚点按库数环形铺开（此前 8 固定槽位叠罗汉）；
  初始散布半径拉大。
- 回归：node --check + wiring 全绿；test_guiweb 117/117；test_gui_store 0 failures。

## 问题54：guiweb 命中路径串被标题残渣污染 —— 「查看正文/打开源文件」全废（2026-09-11）

> 用户反馈（guiweb，搜「UIUX」，top k 8）：1）前三条置信度 99/98/97，第四条直落 0，
> 且返回结果并非全都无关；2）首条命中的文件点「查看正文」报正文不可用、点「打开源文件」
> Obsidian 说文件不存在，增量重建也没解决；要求定位但**不许跑任何重建**，保住案发现场。

### A. 路径串污染（已修，真 bug）

- 机制：`guiweb/bridge.parse_search_text` 从来源行解析 `rel` 时留残渣。三个独立漏点：
  ① `_RE_HEAD = r"\((##+ [^)]+)\)"` 从**标题里第一个 `)`** 截断——标题是完整路径，
  编号小节天然含 `)`（`… / 要点 / 5) ui-ux-pro-max — 行业感知的设计系统`），于是只删掉
  `(## … / 要点 / 5)`，剩下一截粘在 rel 尾部；② `[已回填父节全文]`（`small_to_big` 回填
  标记，retriever 追加在置信度标记**之后**）在 guiweb 侧没有任何剥离规则；③ 低置信注记
  的剥离正则带 `$` 锚点，而回填标记追加在它后面 → 注记落在行中，同样漏进去。
- 案发证据（`data/gui_index.log:1650`）：`正文查看失败 Obsidian Vault/10-Areas/AI/工具/
  OpenCode 五个高价值 Skill 清单.md  ui-ux-pro-max — 行业感知的设计系统)   [已回填父节全文]
  ：不支持该格式`。用同一条来源行喂解析器，复现出的 rel **逐字节相同**。
- 后果链：`store.read_document_text` 由 `rel.rsplit(".",1)[-1]` 判扩展名 → 命中
  「不支持该格式」→ 弹层「正文不可用」；`open_source` 把污染串塞进
  `obsidian://open?…&file=` → Obsidian 报文件不存在；`titleOf()` 去不掉扩展名 → 标题
  显示成「…清单.md ui-ux-pro-max — 行业感知的设计系统」（用户据此以为文件名奇怪）。
  **文件与索引一直是对的**（磁盘存在、meta 键一致、用干净 rel 读得 5347 字），
  所以重建永远修不好——坏的是解析，不是索引。
- 边界：**Flet 版不受影响**（`gui/widgets._parse_src` 用 `find(" (## ")` + 在置信度标记处
  截断，三处都避开，同一条行上实测 rel/heading/conf 全对）；测试没拦住是因为原用例只覆盖
  「标题不含 `)`、无回填标记、注记在行尾」三种形状。
- 修法：改为「定位锚点、不认位置」——以置信度标记为截断点（其后全是协议尾巴）、标题用
  `find(" (## ")` + 剥末尾一个 `)`、无标题行再按首个 `" ["` 兜底切一刀（同 Flet 规则）。
- 同批硬化：`scoreBadge` 空分数守卫（JS `Math.round(null*100)===0` 会把「无分数」冒充成
  「0 低置信」，与展开态 `'--'` 自相矛盾）；`open_source` 不再拼字面 `#` 标题锚点——
  Obsidian 的标题跳转要求 `%23` 编码进 `file` 值，字面 `#` 是 URL 片段（规范解析器丢弃、
  非规范解析器会当成文件名 → 又一次「文件不存在」），跳标题待实测后再按 `%23` 形式回填。

### B. 置信度 97→0 跳变（已定位；根因已修：尺度修正）

- 实测复现（单库范围）：`raw 0.7270 → 显示 99`、`0.7243 → 99`、`0.7141 → 97`，随后
  `0.5008 / 0.5005 / 0.5003 … → 显示 0`。全池 50 块的重排原始分是**双峰**：
  `+3.87 / +3.35 / +2.38 / +1.15` 之后断崖到 `-5.7 ~ -8.0`（对数几率空间）。
- ① 真正的第 4 名被同篇封顶丢掉：前 3 名同属一篇笔记（`max_chunks_per_file = 3`），
  而全池第 4 高分的正是**同一篇笔记的「核心内容」节**（logit +1.15 → 真分 0.76）。
- ② 空出的名额被池底噪音填满：重排路径上没有生效的相关性下限（旧 `drop=0.4` 不可达），
  于是「给 8 条」被实现成「硬凑 8 条」。
- ③ **根因：重排分被算了两遍 sigmoid。** `sentence_transformers.CrossEncoder` 对
  `BAAI/bge-reranker-v2-m3` 自带 Sigmoid 激活（实测 `ce.predict == sigmoid(HF logits)`
  逐元素相等），`hybrid_search` 又套一次 → 全体置信度被压进 `(0.5, 0.731)`。
  这才是问题43 记录的「噪音地板 0.50~0.52」与问题45 锚点（0.50→0.00、0.73→1.00）
  的真正来源——两条历史修复都是在补这个压缩，副作用是真·中段被系统性夸大
  （真 0.44 显示成 0.59、真 0.60 显示成 0.82）、且 drop 阈值在数学上不可达。
- ④ 展示层 `_conf_display` 把 ≤0.50 钳成精确 0.00 → 徽章显示「0 低置信」，
  0.3% 与 0.03% 撞成同一个 0。JS `Math.round(null*100)=0` 又会把「无分数」也画成 0。
- 已排除：重排批次（`bs=1/4/16/64` 同对数同分，与「批次收紧为 4」无关）。

**修法（治本：拆掉压缩，让补丁随它一起消失）**

- `hybrid_search` 重排分支改为**只钳位、不二次激活**（`max(0, min(1, s))`）；
  `_CONF_ANCHORS` / `_conf_display` 整块删除（它们服务的压缩不存在了，留着就是新补丁）。
- 重标定（2026-09-11 九组真库查询实测，真分尺度）：确定命中 top1 = 0.98/0.89/0.98、
  同篇次优节 0.76、模糊口语 top1 = 0.28/0.04/0.14、库中不存在 top1 = 0.60（重排器误判）
  /0.009/0.017、池内噪音普遍 <0.05。据此：`CONF_TIER_STRONG` 0.65→**0.75**；
  `confidence_warn_threshold` 0.55→**0.30**（落在模糊口语 top1 之上：白话查询会被整体
  提示，确定命中不受影响）；`confidence_drop_threshold` 0.40→**0.0 = 关闭**——
  修好尺度后旧值 0.40 会突然开始真的丢结果，等于把用户明确「先放着」的及格线偷偷打开，
  故显式关闭并注明「真要开，实测建议 0.05~0.10」。`hyde_min_confidence=0.5` 现在才
  真的可用（旧尺度下 0.5 是地板，几乎不可能命中）。
- **曾经的补丁逐个处置**：① 问题43 分档词——保留机制、边界按真分重标（0.75/warn）；
  ② 问题45 零点重标定——删除（数值本身就是概率，零点天然在 0）；
  ③ 两套 GUI 的配色/徽章档位（Flet `_conf_color`、guiweb `scoreBadge`）0.85/0.20→
  **0.75/0.30**，并在用例里断言与后端常量同值（防各写一套漂移）；
  ④ `scoreBadge` 补空值守卫（无分数显示 `-- 无分数`，不再冒充 0）；
  ⑤ `data/config.json` 的两个阈值同步改写（否则旧值在新尺度下含义漂移）。
- 回归锚点：`audit_regression_test` 里把「`_conf_display` 必须存在」反转为
  「`math.exp` 不得出现在 `hybrid_search`、`_conf_display`/`_CONF_ANCHORS` 必须不存在」，
  并加行为锚——给定重排分 0.44/0.02/0.98，来源行必须原样出现该数值（旧实现在此处
  会变成 0.59/0.50/1.00）。
- 用户可见变化：中段分数不再虚高（0.44 现在显示 44%，此前 59%）；真无关的块显示
  0.1%~0.2%（仍是「0%」但含义诚实：重排器给它的概率就是这么低）；白话查询会出现
  「整体置信度偏低（最高 0.04）」的头部提示。**「98→0」的落差本身来自数据**
  （库里就只有 3~4 段相关），要靠「同节折叠」把被重复占用的名额腾出来才会明显改善。

- 用户决定：同篇封顶是**导航性质的有意设计**（让更多文档进上下文）——保留；及格线先放着
  （本次显式关闭）；同节折叠/弹性配额待用户选择，见 TODO.md。

### C. 同节折叠：名额改按"实际交付"计（已做，2026-09-11）

- 用户指出修复后前三条仍是"逐字节相同的内容、不同置信度"。核对属实：三块命中同一小节
  （`要点 / 5) ui-ux-pro-max`），`small_to_big` 回填后交付的是**同一段 765 字**（md5 相同），
  而同篇「核心内容」节（真分 0.76、全池第 4）被同篇封顶挡在门外。
- 机制错位：**名额数的是"命中块"，交付的是"小节正文"**。三份重复正文既没让 AI 多看到
  东西，又挤掉别节别篇——与"让更多内容进上下文"的导航意图相反。
- 修法（名额施加在交付侧）：正文模式下 `hybrid_search` 改交**候选窗口**
  （`_candidate_window = top_k × FOLD_WINDOW_FACTOR(4)`），由 `_format_results` 按序
  边走边定交付：① 同节折叠（同一小节只交付一次，留最高分代表，`emitted_sections` 按
  `(库, rel, hp)` 去重）；② 同篇封顶改为数**已交付条数**（折叠掉的候选不算）；
  ③ `limit=top_k` 也数交付条数。于是折叠腾出的名额由窗口后续候选补位，条数不减。
  list 模式三个开关都不生效（无正文可重复）。
- 附带收益：`_expand_parent` 结果按 `(库, rel, hp)` 加缓存——此前三块同节会做三次
  全文件 `get` 取同一段父节正文，现在一次。
- 实测（真库，top_k=8，范围=Obsidian Vault 搜「UIUX」）：
  `#0 98% §5(块18/21) → #1 76% 核心内容(块2/21) → #2.. 0% 噪音`，**8 条照给、无重复正文**
  （逐条 md5 校验）；对照修前：`98/97/92` 是同一段文字的三份拷贝，76% 那条根本见不到。
  另一组「WEMM 页级导航」得 98/96/95/72/68/15/3/0，三条不同文件、无重复。
- 提示行同步扩写：`（同一文件最多展示 3 块、同一小节只交付一次全文，完整内容请打开源文件）`。
- 用例：`audit_regression_test.test_section_folding_delivers_section_text_once`
  （折叠只交付一次 / limit 由窗口补位 / 关掉折叠即旧行为逐字节重复 / 第 4 块所在小节
  能进入交付 / 封顶施加在交付侧的源码断言）。
- 未做（用户明确先放着）：及格线（`confidence_drop_threshold` 保持 0）；弹性配额待选。

### 附记：全量测试会写真实 data/（与本问题无关的既有卫生缺口）

- `tests/run.py` 跑完后 `data/index_progress.json` 变成测试假库 `"library": "L"`、
  `index.lock` 被touch、`extract_cache/` 新增 16 个文件（含 `mineru_pending.json` 被写成
  `{}`）。与 AGENTS.md「真 `data/` 零触碰」不符。索引本体不受影响：本次核对
  `obsidian_kb` 1828 块 == meta 期望 1828 块、collection 仍是 9 个、`index_meta_Obsidian
  Vault.json` mtime 停在案发时（09-10 21:43）。

### 回归

- `tests/run.py`：14/14 套通过（46.3s）；test_guiweb 132/132（新增 8 项：编号含 `)`
  标题、回填标记+行中注记、无标题兜底、**生产端 round-trip 哨兵**、徽章空值守卫）；
  audit_regression_test 45/45（置信度两条用例按真分尺度重写 + 行为锚，新增同节折叠用例）；
  `node --check guiweb/ui/app.js` 通过。

## 问题55：检索结果自适应建议（给 AI agent 的"下一步怎么做"）+ 去重澄清（2026-09-11）

> 用户反馈两点：1）「我记得我的系统有做去重的功能不是？它没有生效吗？」；
> 2）希望 agent 用这套 MCP/系统时，**系统能根据当前返回结果自动给出建议**——
> 例如"5 条结果都是高置信度 → 提醒 agent 用更大的 top_k 找更多上下文"，
> 并覆盖至少 5~10 种情况，让 agent 知道这个系统是干嘛的、该怎么正确用。

### A. 去重（find_duplicates / 诊断页）没有失效 —— 它本来就不是"查同名"

- 实跑真库：`find_duplicates(Obsidian Vault, threshold=0.8)` → 扫描 177 份、
  跳过 1 份（未提取/太短）、**近似重复对 0、重复组 0**。功能正常，只是没有重复可报。
- 它判的是**内容相似度**（MinHash+LSH，对提取出的正文，默认 ≥0.8），**不是标题/文件名**。
  用户看到的「FLUENT配置」5 条其实是**两篇不同笔记**：
  `20-Projects/Summer-2026/ROCKETRY/概念/FLUENT配置与求解设置.md`（1524 字，HEBAT 3.0
  项目侧）与 `10-Areas/Aerospace/概念/FLUENT 配置与求解设置.md`（2170 字，通用概念）——
  两篇 `title:` 字段完全一样、文件名只差一个空格，**实测字面相似度 0.179、有效行重叠 10%**，
  远低于 0.8 → 不算重复（也不该算）。
- 真正的观感问题在**列表不消歧**（只显示文件名、不显示目录）：同篇封顶仍严格生效
  （实测 3+2：每篇各 3 条额度，第二篇只命中 2 条）。两篇同标题不同路径的笔记各自有 额度，
  这是有意的（否则同名笔记互相压制）；封顶键是"库 + 文件路径"，跨库同名文件也不互封顶。
- 兜底：新增结果建议规则直接把这件事告诉 agent（见下 B-4），不再只靠人眼发现。

### B. 结果建议引擎 `advice.py`（新增模块，纯规则、零 I/O、零模型）

- 定位：把"这一批结果的形态 → 下一步该怎么做"**随结果一起返回**（而不是写在文档里等
  agent 去读）。输出行一律**不以 `[来源]` 开头** → 两套 GUI 都按提示横幅渲染（guiweb
  早已如此；Flet 本次同步修好，见 C），agent 直接读到人话。每次最多 2 条（按优先级），
  建议本身不能变成噪音；同样输入必然同样输出（可单测、可复现）。
- 覆盖 12 种情况（规则表见模块 docstring；`*` = 优先级 1）：
  ① 整批最高分 <warn → 换笔记里的原始术语重搜 / 先 `include_body=false` 枚举候选；
  ② 只有 1 条 ≥0.75 → 只引用它，其余用 `read_document` 补上下文；
  ③ 最高分落在中相关（warn~0.75）→ 沾边非直答，读上下文或问得更具体；
  ④ ≥2 条**标题相同路径不同** → 那是两篇笔记，引用/打开用完整路径（实测 FLUENT 案）；
  ⑤ 命中落非默认库（agents/skills）→ 那不是用户的笔记，只要笔记请传 `libraries`；
  ⑥ ≥3 条 ≥0.75（或最高 ≥0.9 且已给满 top_k）→ **把 top_k 调大（如 15~20）**（用户点名要的这条）；
  ⑦ 同一文件占 ≥60% → 想横向比较就 `exclude` 该篇再搜；
  ⑧ 命中含 pdf/docx → `read_document` 拿全文、图表扫描页用 `navigate_knowledge`；
  ⑨ 有回填/折叠 → 正文是整节且同节只交付一次，要原文用 `read_document`、看双链用 `note_relations`；
  ⑩ 清单模式 → 挑 1~3 条再精读，别整清单读进来；
  ⑪ 命中 ≤2 条 → 放宽 folder/exclude/libraries 或换同义词；
  ⑫ query 像关键词罗列 → 问"怎么做/为什么"时用完整问句更全。
- 接线：`_format_results(..., query=...)` 末尾调用 `advice_for(hits_info, ...)`，把建议
  插在结果之前；`hits_info` 收集每条交付的 {lib, rel, title, score, backfilled}。
  原单独那条"（本次查询整体置信度偏低…）"**并入规则 ①**（同条件触发，避免同一件事说两遍）。
- MCP 说明书同步（`server.search_knowledge` docstring，agent 主要就靠它认识系统）：
  - 修掉**过期尺度**（旧文案还在写"零点重标定的 0.20~0.85 中相关"，问题54 已换真分尺度）：
    现在写明 <0.30 弱 / 0.30~0.75 中 / ≥0.75 高 / 0.5 = 无法判断；
  - 新增"正文前的（…）行是系统给你的建议，不是检索结果"一节 + 5 条常见建议的解释；
  - 新增"用法剧本"：要全文用 read_document、要看图用 navigate_knowledge、先摸底用
    `include_body=false`、嫌少调 top_k、嫌噪音收窄范围、问双链用 note_relations。

### C. Flet GUI 的提示行渲染（顺手修掉一个老 bug）

- `gui/widgets._render_results` 的块切分把**第一个非 `[来源]` 行当成某块的 src**、后续行
  全吞进它的正文 → 低置信提示/封顶说明会被渲染成"文件名位置显示整句提示"的畸形卡，
  且真实结果错位。之前只有单行提示所以看着只是"一张怪卡"，本次建议可能连着两行会更明显。
- 改为与 `guiweb.parse_search_text` **同规则**：非 `[来源]` 开头且当前尚无来源行 → 归入
  提示缓冲，连续提示行合并成一个横幅（warning 底色），`---` 处封口；末尾游离行同样成幅。
  两套 GUI 的行为现在一致（guiweb 一行一个 notice，Flet 连续行合并成一幅）。

### 回归

- `tests/run.py`：14/14 套通过（45.1s）。新增：`test_result_advice_rules`（12 种场景逐条
  断言 + 最多两行 + 空输入）、`test_parse_multiple_advice_lines_before_results`（guiweb：
  三行提示全归 notice、首条结果不受污染）、`test_search_card_renders_notice_lines_as_banners`
  （Flet：2 横幅 + 2 结果卡，提示不得混进来源行）；`test_parse_roundtrip_from_producer`
  顺手加"建议行不污染结果"断言。audit 46/46、test_guiweb 137/137、test_gui_store 0 failures。
- 真库实测：搜「FLUENT配置」→ 自动给出
  「注意同名不同目录…是两篇不同笔记（标题一样、内容不同）…」+「多条高置信命中（5 条 ≥0.75…）
  把 top_k 调大（如 15~20）…」；搜「UIUX」→ 给 top_k 建议 + 回填/折叠说明；
  搜「怎么让我的笔记更好找」→ 给整批偏低 + 命中含非笔记库（skills）两条。

## 问题56：模型冷加载提速（离线优先 + 可配空闲卸载 + 重排 fp16）（2026-09-12）

> 用户诉求："大部分程序都花在加载模型上，有没有可靠解法"。先实测定位再动手，
> 四项经用户逐项审批后执行。

### 定位（实测，每场景独立子进程冷加载）

| 场景 | import（进程固定） | 模型加载 | 合计 |
|---|---|---|---|
| 嵌入 现状（联网解析） | 5.2s | 11.38s | 16.4s |
| 嵌入 离线 `local_files_only` | 5.2s | **2.59s** | 7.7s |
| 重排 现状 fp32（联网解析） | 5.2s | 10.21s | 15.4s |
| 重排 离线 fp32 | 5.2s | 2.57s | 7.6s |
| 重排 离线 fp16 | 5.2s | 2.49s | 7.5s |

根因：`SentenceTransformer/CrossEncoder` 传 **repo id** 时，即使模型已下载，
每次加载都要先向 huggingface.co 核对 commit/文件新鲜度（串行 HTTPS 往返），
实测占冷加载约 8 秒（GitHub sentence-transformers#2842 亦记录 6~7x）。

### 四项改动

1. **离线优先加载**（`index._load_pretrained`，嵌入 `_build_model` + 重排
   `_get_reranker` 共用）：先 `local_files_only=True` 只读本地缓存，抛 `OSError`
   （本地缺文件/首次下载）才回退联网。无模型/结果差异，纯少等。冷加载 11.4s→2.6s。
2. **空闲卸载时间可配**：`server._GPU_IDLE_UNLOAD_S` 硬编码 600s → 新键
   `gpu_idle_unload_seconds`（DEFAULTS/模板/设置页三处同步，默认 **1800s**，
   **0 = 常驻不卸载**）。桌面"隔一会儿再查"不再每次重付冷加载。
3. **重排器 fp16**（`_get_reranker` 加 `torch_dtype=float16`）：显存/读盘减半，
   官方（BAAI）推荐。**阈值重测**：真库 12 组查询 fp32 vs fp16 逐条对比，分数
   几乎逐位相同（仅一处 0.89→0.88），命中率完全一致 → `CONF_TIER_STRONG=0.75` /
   `confidence_warn_threshold=0.30` 无需调整。
4. **清 bge-m3 冗余**：查 HF 官方发现 main 只有 `pytorch_model.bin`（无 safetensors），
   本地那份 safetensors 是**未合并的 PR 快照**（孤儿，2.17GB，main 用不到）。
   删除孤儿快照 + 其 ref，回收 **2.17GB**；main 完整、离线加载验证通过。
   （"切 safetensors"因官方无此产物不可行，如实记录。）

### 回归

- 新增用例（`audit_regression_test.py`）：离线优先"先本地后回退/命中不联网"、
  两条加载路径必须穿 `_load_pretrained` 且重排 fp16、空闲卸载键可配且 0 合法。
- `tests/run.py` 14/14 全绿（46.9s）；`verify_export_import` 顺带 17.3s→13.0s
  （真模型走离线加载的副作用）。真库 9 个 collection 原样、无污染。

## 问题57：显存管理补强 + 修复 config.json 静默失效（2026-09-12）

> 用户反馈："检索后拉起了 bge-m3+reranker，但建索引时不释放，显存一直满"，
> 同时要求空闲卸载改 5 分钟、有新调用刷新计时。

### 显存管理两个真实缺口

1. **建索引从不主动放下 reranker**：`index_library` 此前只在 WEMM 页同步阶段
   才释放模型。检索留下的 reranker（~1.1GB，检索专用）会全程陪跑索引，纯浪费。
   → 修：`index_library` 进 `_index_core` 前先 `release_reranker()`（bge 保留，
   本轮要拿它嵌块）；与 WEMM 后端无关，off 也释放。
2. **空闲卸载只看计时器**：`server._gpu_idle_unload_daemon` 原先只比活动时间戳，
   而 `_touch_gpu_activity()` 在任务开头只调一次。阈值缩到 5 分钟后，长索引
   会被空闲线程**半路抽走正在用的 bge**→反复重载。→ 修：守护线程先读
   `read_progress().get("running")`，任务运行期间刷新活动时间并跳过卸载
   （等价于计时从任务结束才起算）。

### 顺带修复：config_editor 写坏 config.json（严重）

- **现象**：`data/config.json` 第 177 行 `confidence_drop_threshold` 少一个逗号 →
  整份 JSON 解析失败 → `config.load_config` 静默回退全部默认值，用户真实设置
  （MinerU Key、`pdf_scan_backend=mineru-local` 等）全部被悄悄忽略（日志走 stderr，
  不易察觉）。
- **根因**：`config_editor._replace_value` 用 `rest.rstrip().endswith(",")` 判断
  原行是否以逗号结尾；当行尾有 `// 行内注释` 时 rstrip 落在注释文字上 → 误判
  "无逗号" → 替换后把逗号（连同注释）一起吞掉。docstring 声称"保留行内注释"，
  实际从未保留。
- **修复**：新增 `_value_span`（字符串含转义 / 数组可嵌套 / 标量各自正确收边），
  只替换值文本、原样保留其后的逗号与行内注释；`apply_updates` 写盘前用
  `config._strip_json_comments/_strip_trailing_commas + json.loads` 校验，坏 JSON
  一律拒绝写入。已把用户 `data/config.json` 缺失的逗号补回、验证解析通过。
- **回归**：新增 `test_replace_value_preserves_comma_and_inline_comment`、
  `test_replace_value_keeps_config_valid`（对真实模板多轮替换后仍可解析）、
  `test_apply_updates_rejects_invalid_json`。

### 配置变更

- `gpu_idle_unload_seconds` 默认 1800 → **300（5 分钟）**；DEFAULTS/模板/设置页同步，
  实机 `data/config.json` 已改 300。

### 回归

- 新增 `test_indexing_releases_reranker_up_front`、更新
  `test_gpu_idle_unload_is_configurable`（默认 300 + 运行中跳过）与
  `test_auto_phase_follows_index_library`（承认开跑前 reranker 释放）。
- `tests/run.py` 14/14 全绿（45.8s），真库 collection 原样。

## 问题59：完整修复 9 红灯 + 多 Agent 并发调度与资源管理（2026-09-12）

> council 咨询模式评审（`.council-state/round-plan-5/`，6 委员：🔴9/🟡35/🟢29）
> 后用户拍板"完整修复 + 多 Agent 并发 MCP 调度与资源管理"，立 GOAL.md（C1–C4 全绿）。
> 并发方案用户亲选 **C：全局 GPU 互斥锁**（而非"navigate 遇索引回忙"的轻方案）。

### C 方案：`gpu_arbiter.GPU_LOCK`（RLock，多 Agent 调度总线）

- 同进程一切"改变显存里住着谁"的操作全持锁：bge 加载/释放、reranker 释放、
  evict、让路判定；纯查询不持锁天然可并发。RLock 保证 navigate 持锁调
  release 不自死锁；持有期只包判定 + 快速变更，`ensure_server` 120s 长等待
  永远在锁外；拿不到锁（15s）走降级不硬等。
- `get_model` 检查-加载-赋值全程持锁 + 锁内双重检查（B6 单飞，2 线程实测
  loads 2→1）；`release_model`/`fallback_to_cpu`/`_try_switch_back_cuda`/
  `_vram_maybe_evict_wemm`/`index_library` 开跑驱逐全部进锁。
- `navigate_knowledge`：持锁做"`_index_running` 判定 + 释放"原子操作；在跑或
  拿不到锁 → veto（只查已活着的服务，否则回忙，绝不从索引线程嘴里拔模型）。
- `server` 空闲守护：检查-释放原子化（判定完释放前索引启动也抽不走）。

### 9 红灯逐项

- B1：`_load_pretrained` 回退放宽到 `(OSError, ValueError, RuntimeError)`，
  联网也失败则抛带清理指引的 RuntimeError（光放宽未必自愈，已如实记）。
- B2：CLI 是独立进程、`before_serve` 够不着 MCP 侧——不补无用回调，改为
  `_warn_if_vram_low()` 显存不足先警告 + AI_GUIDE 错峰铁律。
- B3：见 C 方案 veto（另发现 `ensure_server` 本身不查显存，如实记，未动）。
- B4：`_auto_batch_size` 探针包 try，失败取保守上限进正常降级链。
- B5：prune 存活集补读 base `index_meta.json`（不存在零副作用）。
- B6：见 C 方案单飞（`retriever._reranker_lock` 作对照已注记）。
- B7：`_check_idle_unload` 看 `_active_requests`（与 `_idle_exit_daemon` 同款守卫）；
  `/evict` 先 `_API_LOCK` 后 `_INNER_LOCK`（与 parse 同序不死锁），注释"等走完"
  变成真的。后果订正：此前是 deferred 空转活锁而非误落终态。
- B8：两处 navigate 文案改字（reindex 本来就自动同步页库）；server 1800 残留改 300。
- B9：AI_GUIDE 补 §8.1 guiweb（含 B2 错峰铁律）。

### 回归

- 新增 `tests/test_mcp_scheduling.py` 25/25（B1/B3 真跑 navigate/B4/B5/B6×2/B7/
  B2/锁单例；假加载器 + `_IsoEnv`，零真实模型；server 经信号隔离后导入真测）。
  经用户确认**已编入 `tests/run.py` SUITES**（A 组）：全量 15 套，`test_mcp_scheduling`
  套内 25/25、整轮 64s。注册后首次全量实测无套间污染。
- `tests/run.py` 15/15 全绿（64.2s）；真库 9 个 collection 原样；C2/C3/C4 原命令全绿。
- 待用户人工：Vault 用户文档组同步（跨工作区）；B2 若要根治需跨进程协调（另立目标）。

## 问题58：BGE↔WEMM 交接收紧（主动让路 + 延后释放）（2026-09-12）

> 用户追问："建库时 WEMM 用不到，建完了轮到 WEMM、BGE 用不到"。核对后发现：
> "建完放 BGE 给 WEMM"这半段是硬的（问题41 已有），但"建库期间 WEMM 别占显存"
> 这半段是**被动**的（只在 BGE 加载撞显存不足时才踢），且释放 BGE 过于激进。

### A. 建索引开跑前主动 evict WEMM（让"BGE 阶段无 WEMM"变硬）

- 原状：`get_model()` 里 `_vram_maybe_evict_wemm()` 只在 BGE **加载**且空闲显存
  不足时才踢 WEMM——极端时序下 BGE 可能先撞上 WEMM 占显存 → OOM → 降级 CPU。
- 修：`index_library` 进 `_index_core` 前，若 wemm 开启且服务在线，主动
  `evict_wemm`（`index.py`；fail-open，服务不在/探测失败静默跳过）。

### B. BGE 释放延后到"真要渲染"（避免白放）

- 原状：`_wemm_auto_phase` 无条件先 `release_reranker()`+`release_model()`，
  哪怕本轮没有 PDF 需要渲染（纯 md 增量）也白放 BGE，下次搜索重新上车。
- 修：删除该无条件释放，改为把释放包成 `before_serve` 回调传给
  `wemm_indexer.index_wemm_library`；`_ensure_server_lazy` 在**首次真正要拉
  看图服务**前调一次。无页可渲染 → 回调不触发 → BGE 原地保留。
  顺带清掉与 `index_library` 开跑前 reranker 释放重复的那次。

### 回归

- 新增/更新（`test_wemm_indexer.py`）：`test_indexing_proactively_evicts_wemm`
  （在线则 evict、off 零探测）、`test_before_serve_only_when_rendering`
  （渲染轮调一次、快速路径不调）、`test_before_serve_precedes_ensure_server`
  （顺序契约）、`test_auto_phase_follows_index_library` 改为断言回调延后释放。
- `tests/run.py` 14/14 全绿，WEMM Indexer 67/67。

## 问题60：库简介——导航性内容简介，采样生成 + 用户覆盖保护（2026-09-16）

> 用户："`list_libraries` 只能告诉 agent 有什么库、标题和一些元数据，但不能告诉
> agent 这个库到底有什么内容，agent 通读全文前根本不知道值不值得查。"
> 追问设计细节：谁来写（用户写不现实，几百篇文档；AI 顺手写又跟"检索省 token"
> 的本意矛盾）、几百个 md 文件几万字怎么喂给 LLM 不爆 token、用户手写后 AI 要不要
> 能覆盖。用户拍板：生成只能显式触发（GUI 按钮 / 对话明确要求）、用户和 AI 都能写、
> AI 想覆盖用户手写内容前必须 double confirm、本地/云端 LLM 二选一设置页可配。

### A. 数据模型（`library.py`）

- registry 条目新增 `summary` 字段：`{text, source: none|ai|user, updated_at,
  fingerprint, model}`，不进 `OVERRIDE_KEYS`（这是内容数据不是继承式配置）。
- 唯一写口 `set_library_summary`（同 `set_selection` 先例）：校验 source 合法
  （只收 ai/user）、长度上限 `SUMMARY_MAX_CHARS=300`，GUI 直改、GUI 刷新生成、
  MCP 门禁通过后的写入统一走这一处。读侧 `get_library_summary` 对非法/缺失字段
  静默兜底空白态，防手改逃逸。

### B. 采样与生成（新文件 `library_summary.py`）

- 不需要也不能把几百篇文档全文喂给 LLM：库内每个块在索引时已经过 BGE-M3 编码
  存进 Chroma，这是免费副产品。直接在这份向量空间里做**最远点采样**
  （farthest-point sampling：从任一点出发，每步选与已选点集最远的下一个点，
  确定性、无需 k-means 那样迭代收敛、天然覆盖语义分散区域）取 15~25 个代表块，
  输入规模由采样数固定，与库到底有 300 篇还是 3000 篇文档基本无关。
- `content_fingerprint`：聚合库内文件的 相对路径:md5（复用 `index.load_meta`，
  不重算）；`is_stale` 判断已生成简介的指纹是否对不上当前内容，`list_libraries`
  据此标注"简介或已过时"。
- LLM 调用复用 `retriever.hyde_generate` 的 OpenAI 兼容 chat completions 调用
  方式（`urllib.request`，零新依赖），加一个可选 `api_key`：本地服务（如 LM
  Studio）留空即可，云端只需换 URL + 填 Key 走 Bearer 认证——本地/云端只是
  `config.library_summary_llm_url`/`_model`/`_api_key` 三个字段的取值差异，
  不是两套代码路径。

### C. 覆盖保护（新文件 `summary_gate.py`，仿 `selection_gate.py`）

- `source` 为 `none`/`ai` 时可直接覆盖，不需要门禁；一旦 `source=="user"`
  （用户在 GUI 手写过），AI 想覆盖必须走两段式确认——`make_proposal` 生成
  提案号+6位确认码（10分钟TTL）→ `consume_proposal` 校验后一次性消费——与
  `selection_gate.py` 完全同一套机制，两个门禁互不依赖、各管各的数据面。
  用户自己在 GUI 编辑框手动改并保存不经过此门禁，无条件生效（不存在"用户向
  自己确认"的说法）。

### D. MCP 工具（`server.py`）

- `list_libraries` 展示每库简介（没有则提示未生成，过时则标注）。
- 新增只读 `get_library_sample(library, k)`：吐采样片段给 agent 自己组织语言写；
  新增 `propose_library_summary(library, text)`：非 user 锁定时直接写生效，
  锁定时走门禁生成提案；`apply_library_summary(library, proposal_id,
  confirmation_code)`：确认后写入。三者 docstring 内硬编码触发纪律
  （"仅在用户明确要求…才调用"）与内容规范（导航性质、禁止逐字摘抄、≤300字、
  不点名具体笔记），因为 agent 运行时只读 docstring，不读 AGENTS.md。

### E. GUI（仅 guiweb，Flet `gui/` 暂缓）

- 库卡片新增简介展示区 +「简介」按钮打开弹层：可手动编辑保存（`source=user`，
  无条件生效）、可点「刷新简介」调用生成（若当前是用户手写，先返回
  `needs_confirm` 由前端弹二次确认，用户同意后带 `force=true` 重调才覆盖——
  GUI 侧的"当面确认"不需要 MCP 那套确认码，用户此刻就在界面上）。
- 设置页新增「库简介生成」分组（`library_summary_llm_url`/`_model`/`_api_key`，
  仿 HyDE 字段渲染），因 `config_editor.py` 是两套 GUI 共享层，Flet `gui/` 的
  设置页自动一并可见，无需额外改动。
- `guiweb/contracts.md`/`FEATURE_PARITY.md` 同步；`wiring_check.py` 三向对齐
  （契约方法↔`mock.js`实现↔`app.js`调用）全绿。

### 回归

- 新增 `tests/test_library_summary.py` 49/49（唯一写口校验/指纹翻转/最远点
  采样含空库与collection异常降级/fake urlopen 覆盖 LLM 调用成功·失败·空白·
  api_key 头/summary_gate 门禁全流程含过期与重放拒绝/写入语义仅 user 锁定），
  已编入 `tests/run.py` SUITES（A 组）。
- `tests/run.py` 16/16 全绿（93.1s）；`guiweb/wiring_check.py` 全绿；
  `config.template_consistency_errors()` 空（DEFAULTS/CONFIG_TEMPLATE 一致）。
- 待用户人工：真实本地/云端 LLM 端到端冒烟（无现成服务，需用户自行配置
  LM Studio 或云端 Key 后验证）；Vault 用户文档组同步（跨工作区）。

### 附记（2026-09-16，用户真机实测反馈，两个真 bug + 一次交互返工）

> 用户接入 LM Studio 实测：先是 `TypeError: truth value of an array...
> ambiguous`；改用 `/v1/chat/completions` 端点后又炸——LM Studio 日志显示
> 连接在整 30 秒被客户端掐断，此时模型（思考型模型 qwen3.6-35b-a3b）还在
> `reasoning_content` 阶段没写到最终 `content`。随后追加需求：config 里
> timeout/token 预算要能调；GUI 要能"一键刷新当前选择的全部库"而不是一个个点；
> 点了刷新之后要能关掉窗口，且要有更明确的动效告诉用户任务确实在跑。

**F1（真 bug）：numpy 真值判断**——`library_summary.sample_representative_chunks`
里 `data.get("embeddings") or []` 对 Chroma 返回的 numpy 数组做真值判断，
数组元素数 >1 时 Python/numpy 直接拒绝回答"真假"并抛异常。改为一律
`is None`/`len()==0` 判空。测试同步加固：`test_sample_representative_chunks`
改用 `np.array(embs)` 而非 python list 灌假 collection——用 list 测不出这个坑，
这是本次教训（结构相同不代表真值语义相同）。

**F2（真 bug）：本地思考型模型的超时/预算太紧**——`call_llm` 原 30s 超时、
400 max_tokens 是照抄 `hyde_generate`（那是查询期的轻量调用），但库简介生成
调的是用户自己的本地大模型，思考阶段可能就要几十秒，reasoning_content 也要
吃掉一部分 token 预算。改为超时 180s、预算 2000，且都做成可调配置项
`library_summary_llm_timeout_seconds`/`_max_tokens`（写法同 `hyde_llm_*`）；
`call_llm` 新增识别：`content` 空但 `reasoning_content` 非空时明确判定为
"思考没写完"，绝不把思考过程当简介返回（这是内心独白，不是概括，返回它会
直接违反"一段导航性文字"的内容规范）。

**交互返工：生成从同步阻塞改成后台任务 + 轮询**——原设计里点"刷新简介"是
一次同步的 `refresh_library_summary` 调用，前端等 promise resolve，思考型
模型跑 1~3 分钟期间弹层等于"卡住"，用户也没法关窗口去干别的。改成
`refresh_library_summaries_batch`（立即返回）+ `refresh_library_summaries_poll`
（前端 800ms 轮询），单库/批量共用同一条路径（names 传一个元素即单库）——
跟提取试验台的 `preview_start`/`preview_poll` 是同一套异步模式。关掉「库简介」
弹层、切到别的标签页都不影响任务继续跑，跑完不管用户在不在原弹层都会 toast。
库页顶部新增常驻状态条（`.mini-spin` 转圈 + "X/Y（当前：库名）"进度文案）+
"隐藏（后台继续跑）"按钮——用户要的"更明确的动效"与"能关窗口"由这一套机制
一起满足。新增"刷新全部简介"按钮：目标 = 当前"库范围"多选（`S.scope`，
复用已有的全局库范围选择器，空=全部库）；批量遇到 source=user（用户手写）
的库会跳过并记 `needs_confirm`，全部跑完后一次性汇总问"这 N 个库要不要也
覆盖"，而不是每个库分别弹一次打断节奏。

回归：`tests/test_library_summary.py` 50/50（新增思考型模型"只有
reasoning_content"场景的用例）；`tests/run.py` 16/16；`guiweb/wiring_check.py`
全绿（契约方法改名 `refresh_library_summary` → `refresh_library_summaries_batch`
+ `refresh_library_summaries_poll`，`contracts.md`/mock.js/app.js 三向同步）。

### 附记二（2026-09-16，真机连续批量刷新暴露的 3 个问题）

> 用户实测批量刷新多个库后反馈：① 生成出来的简介明显"串味"，像是把上一个库
> 的内容也算了进去，不应该每次都是全新的；② 手写简介被拒绝覆盖一次之后，之后
> 刷新别的库它还是会反复再问同一个库要不要覆盖；③ 简介弹窗生成完之后点进去
> 看不到内容，而且弹窗本身太小，可读性几乎为零。

**真 bug①：本地 LLM 服务端"提示词前缀缓存"跨库串味**——`build_prompt` 原来
把 `_PROMPT_INSTRUCTIONS`（固定不变的规范文字）和库名等每次都变的内容拼进
同一条 user 消息里；批量连续刷新时，每个库发出去的请求前几百字节逐字相同、
后面才分叉。这正是 llama.cpp/LM Studio 这类本地推理服务端"提示词前缀缓存"
（复用相同前缀的 KV cache 加速生成）最容易在"匹配到一半"处出问题、把上一次
请求的话题续到这次回答里的触发条件——不是代码真的"传递了上下文"，是本地
服务端的缓存匹配点落在了不该落的地方。

第一版修法是索性关掉缓存（`cache_prompt: false`），用户反馈："别一刀切关掉，
一开始都是用的固定规范提示词，应该让这段留在缓存里给后面的库提速，同时避免
串味。"于是改成**结构性**修法：把 `_PROMPT_INSTRUCTIONS` 单独拆成一条
system 消息（逐库调用时这段文字逐字不变），`build_prompt` 只负责拼库名/
文件/采样片段这条 user 消息（每次都不同）。两条消息分开发，服务端的前缀
缓存命中点精确落在 system 消息末尾——要么整条 system 消息完全匹配复用
（safe，speed 收益还在），要么 user 消息一开始就不匹配、整条重算，不存在
"匹配到一半"的歧义地带，速度和正确性都要。不再需要 `cache_prompt: false`
这个字段，云端/本地两条路径又统一了，不用再区分要不要加它。

**真 bug②：批量场景下同一个手写库被反复追问**——原逻辑里用户对"要不要覆盖
手写简介"点了否/关掉确认框后，这个决定不会被记住，下次刷新（哪怕只是想刷新
别的库、并没打算动这个库）它又会被排进 needs_confirm、又弹一次确认——用户
明明已经表过态，却被无限重复打扰。修：`app.js` 新增会话级免打扰记忆
`SUMBATCH.skip`（Set，只活在这次页面会话里，刷新页面即清空，不做持久化——
覆盖用户内容这种事不该被"记住太久"），批量场景里已经问过且被拒绝的库直接
静默跳过、不再弹窗，只汇总计入"已跳过"提示；但如果用户专门打开那个库自己的
简介弹窗、主动点"刷新简介"，视为一次明确的针对性动作，会清掉这条免打扰记忆
重新问一次——批量场景的"别烦我"和单库场景的"我就是要动这个库"是两种不同
意图，不能用同一个开关兜底。

**真 bug③：简介弹窗太小，生成完看不出内容**——`mSum` 弹窗原用 `modal-wide`
（560px）+ 96px 高的文本框、12.5px 小字号，300 字的一段话挤在里面基本看不清，
用户描述为"点进去看不到简介"。改用已有的 `modal-doc`（720px）宽度类，文本框
新增 `.sum-textarea-lg`（最小高度 260px、14px 字号、1.8 行高），可读性问题
本质是尺寸问题，不是数据没写进去。

回归：`tests/run.py` 16/16；`guiweb/wiring_check.py` 全绿（本轮改动不涉及
契约方法签名，未触碰 `contracts.md`）。

**内容规范拍板**：给用户 3 个候选方向——①范围地图型（总体定位+主题板块）、
②检索决策卡型（直接说"适合查什么/查不到什么"，不列主题）、③性质画像型
（讲内容形式/详略/更新频率而非主题）。用户选①+②融合。改写
`_PROMPT_INSTRUCTIONS`：简介必须同时做两件事——先给总体定位和库内主要
主题板块（口语化提及，不用编号/项目符号罗列），再明确写"适合来这查什么类型
的问题"和"大概率查不到什么"（正反两面都要有），直接服务于 agent"值不值得
查"这个决策，而不是只报主题范围让 agent 自己去猜。其余约束不变（禁止逐字
摘抄、不点名具体笔记、100~300字一段话、不用 markdown）。

