# HANDOFF — obsidian-rag 当前状态（2026-09-04）

> 本文件给**未来接手的 AI agent**：一页看懂项目现状、最近改动在做什么、用户侧待完成事项。

---

## 项目定位

个人 Obsidian 知识库的本地语义检索系统：多格式文档（md/txt/pdf/docx）→ 切块 →
BGE-M3 嵌入 → Chroma；混合检索 + 重排；MCP server 接 opencode；Flet 桌面 GUI。

---

## 当前代码状态

- **META_VERSION**: 9（多格式 + 统一终态 + 原始字节指纹）
- **EXTRACT_VERSION**: 4（PDF 分拣规则 + MinerU model_version 参数）
- **WEMM_VERSION**: 1（页级视觉导航独立版本号，独立自愈，互不影响文字索引）
- **回归十件套**: 全绿（extractors 72/72, audit 38/38, registry 15/15, singleton 5/5,
  config_editor 0, gui_store 55, wemm_indexer 36/36, wemm_retriever 13/13, dedup 23/23,
  gpu_arbiter 28/28, verify_export_import 39/39）

---

## 最近提交脉络

| 提交 | 内容 |
|---|---|
| `06f397a` | **问题34**：混合型 PDF（任一页无文字层）整本按扫描件路由，不再静默丢图片页 |
| `cb1be8e` | **问题35**：MinerU 云端批量并行——限流闸门、错误三分类重试、断点簿记续接 |
| `bd24227` | **问题36**：max 模式（`mineru_concurrency=0`）+ 轮询瞬时异常退让 |
| `a45826b` | **问题37**：WEMM 页级视觉导航 + read_document + 近似去重（另一 agent 实现） |
| `9e7fac3` | **问题38**：index_failures + wemm_status + 渲染 DPI 档位（另一 agent 实现） |
| **本次提交** | **问题39**：全面质量审查修复轮（见下） |

---

## 本次改动（问题39：审查修复轮）

另一 agent 完成问题37/38 后，审查发现一个 P0 + 若干 P1/P2，本轮全部修复：

1. **WEMM 失败终态死寂（P0）**：快速路径对终态条目照跳，"记入终态待重试"是假承诺——
   服务抖动一次该 PDF 永久退出页级导航。现终态/成功条目带 `xsrc=wemm:<模型>:<维度>:<DPI>`
   签名，失败每轮真重试，改 DPI/换模型自动重渲染（不再依赖人工 --full）。
2. **写库假账防护**：upsert 分批（1000/批），全部批次成功才把成功条目并入 meta。
3. **显存管理（wemm_server）**：懒加载（启动不进显存，首个请求才加载）+ `--unload-after`
   改后台守护线程驱动（原实现空闲时永不触发）+ /health 不持模型锁 + dtype 新旧兼容。
4. **配置热读**：新增 `config.reload_config()`；ensure_fresh / reindex_knowledge / WEMM
   工具入口统一现读。**新代码里长驻进程读配置一律走它，不要再读 import 快照。**
5. **navigate_knowledge**：库范围语义对齐 search_knowledge（空=默认库、all−exclude）；
   提示改指 CLI 建页库（reindex_knowledge 不建 WEMM 页库）。
6. **read_document**：抬头补字数/产出方式（`read_cached_markdown` 命中现返回产出路由），
   正文不截断（补齐 TODO 存档设计）。
7. 其余：index_failures 重试判定复用 `_backend_changed`、dedup bottom-k 估计按并集第 k 小值
   截断、页级检索 `get_collection` 零写副作用 + 错误汇总、HTTP keep-alive 请求体消费修复、
   死代码清理。

**接手注意**：既有 WEMM 页库 meta 无 `xsrc` 字段，本轮后首轮增量会整体重渲染一次（一次性）。

**问题41（GPU 显存仲裁，gpu_arbiter.py）**：同一时刻只让一个模型驻留显存。WEMM 默认开
（`wemm_backend=on`）：导航/页索引按需自动拉起 wemm_server（`wemm_python` 指定全局
Python），空闲 5 分钟卸显存、30 分钟自退出；WEMM 加载前等显存 ≥5.5GB，bge-m3 加载前
不足则 `/evict` 抢占（检索优先，WEMM 被抢占批次下轮自动重试）；server 空闲 10 分钟
自动卸 bge-m3。**fail-open：探测失败绝不阻塞路径。** 旧 wemm_server 遗留实例已清理。
建页库已接入索引管线：增量/全量重建后自动同步页库（_wemm_auto_phase），服务懒拉起——无变更轮次零拉起；CLI wemm_indexer.py 保留为手动入口。

**问题40（GUI 文件生效明细）**：主界面工具栏「文件生效明细」对话框——逐文件看文字索引
失败原因（含下轮是否自动重试）与 WEMM 每 PDF 页向量数/渲染失败，点行直接打开源文件人工
核对。store 层只读函数：file_index_rows_for / wemm_status_for / wemm_backend_state /
wemm_service_probe；flet 0.86 事件名先例：Dropdown=on_select、Container 仅 on_hover、
padding 用 ft.Padding。

---

## 问题脉络

1. **问题33**：`model_version` 从未显式传（已修）
2. **问题34**：PDF 分拣整本二分 → 存在图片页即整本按扫描件
3. **问题35**：串行送云端 → 并行批量
4. **问题36**：固定并发上限 → max 模式
5. **问题37**：WEMM 页级视觉导航 + read_document + 近似去重（默认关闭）
6. **问题38**：失败溯源 index_failures + wemm_status + DPI 档位
7. **问题39**：37/38 质量审查修复轮（P0 死寂/显存懒加载/配置热读/语义对齐等）

---

## 用户侧待完成事项

- 开启 mineru-cloud：GUI 设置页「常用 → PDF 与云端 OCR」→ 扫描件 OCR 后端选 `MinerU 云端 OCR`（Key 已配）
- 导入课件：PDF 放进 vault，下一轮自动同步整本云端认字入库
- 真实课件验证（可选）：ManometerEquation / Note9 路径，若能给路径可做完整性验证
- 视觉导航（可选，默认关闭）：设置页开 `wemm_backend` → `python wemm_server.py --port 9101`
  （现默认懒加载，首个请求才进显存）→ `wemm_indexer.py --library <库名> --backend on`
- **Vault 文档组（20-Projects/Obsidian RAG/，D:\_STOREROOM 另一仓库）问题39 相关更新被用户
  约束跳过（跨工作区），待用户决策后补**——问题37/38 的 Vault 文档已在该仓库有未提交版本

---

## 关键配置键（data/config.json）

| 键 | 当前值 | 含义 |
|---|---|---|
| `pdf_scan_backend` | `none` | 扫描件/混合型 PDF 的处理方式（none 跳过 / mineru-cloud 云端 OCR） |
| `mineru_api_key` | 已配置 | mineru.net API Token，敏感信息不进日志 |
| `mineru_concurrency` | `3` | 0=最大吞吐、1=串行、≥2=固定并发 |
| `mineru_rate_per_minute` | `45` | 每分钟提交上限（官方 50/分钟，留余量） |
| `mineru_timeout_seconds` | `600` | 单文件提交+轮询+下载总超时 |
| `mineru_model_version` | `vlm` | 云端解析模型（pipeline 更省配额，vlm 精度更高） |
| `extensions` | `md,pdf,docx` | 多格式默认开启 |
| `wemm_backend` | `on` | 页级视觉导航开关；默认开——服务按需自动拉起/用完自动退出 |
| `wemm_url` | `http://127.0.0.1:9101` | 本地 WEMM 看图服务地址 |
| `wemm_dim` | `512` | 页向量维度（WeMM-Embedding-2B matryoshka） |
| `wemm_python` | `python` | 拉起 wemm_server 的全局 Python（须装 torch，别填 .venv） |
| `wemm_render_dpi` | `60` | 页图渲染档位（40/60/90/120；改后自动重渲染，无需 --full） |

---

## 架构红线（改代码前必读，违反 = 生产事故）

1. extractors 契约：绝不抛异常、绝不写源目录，失败一律折叠 `(None, reason)`
2. 统一终态：一切不产块的文件落持久化终态，失败终态带 xsrc 能力签名；
   **WEMM 同理——终态条目必须可重试，"待重试"不能是死寂**
3. API Key 不进日志
4. GUI 是零侵入观察者：不直写 Chroma，只读进度/meta 文件
5. 测试先于修改：`tests/run.py` 14 套全绿才能提交
6. **查询路径零写副作用**（retriever 用 get_collection，不建空库）
7. **长驻进程配置现读**：任务边界调 `config.reload_config()`，不读 import 快照
8. **GPU 显存仲裁 fail-open**：探测失败绝不阻塞路径；同一时刻只让一个模型驻留显存

---

## 测试命令

```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python tests\run.py   # 统一入口：14 套全绿 + 计时 Top10（约 45 秒，真模型只加载 1 次）
# 调试单套：python tests/run.py --suite test_dedup（各文件也可单独跑，用法不变）
```

---

## 文档位置

- **AGENTS.md**（本项目 AI 指令）：当前状态速览 + 架构红线
- **AI_GUIDE.md**：部署/使用手册
- **TASK_LOG.md**：问题 1–39 完整开发史
- **TODO.md**：路线图与 Backlog
- **Vault 内** `20-Projects/Obsidian RAG/`：用户视角文档组（另一仓库，未提交部分见上）

---

> 下次接手时：先读本 HANDOFF + AGENTS.md，跑一遍 `tests/run.py` 核对 14 套是否仍全绿，再决定从 Backlog 哪个条目继续。
