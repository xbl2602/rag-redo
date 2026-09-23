# 现有能力去留提案

> 这是我通读旧项目 `AGENTS.md` 后，给每一项现有能力提的"做成哪个插件、放哪个阶段"建议，不是定论——请勾改。"运行时形态"对应 [PLUGIN_SPEC.md](PLUGIN_SPEC.md) 第3节的 `in_process` / `subprocess_service`。

| 旧项目能力 | 新插件（建议ID） | 建议阶段 | 运行时形态 | 备注 |
|---|---|---|---|---|
| md/txt 原生读取 | `official-extractor-text` | Phase 1 | in_process | 无外部依赖，最简单 |
| PDF 文字层(pymupdf4llm) | `official-extractor-pdf-text` | Phase 1 | in_process | |
| DOCX(python-docx) | `official-extractor-docx` | Phase 1 | in_process | |
| 切块策略 | `official-chunker` | Phase 1 | in_process | 2026-09-22 补录：ARCHITECTURE.md 扩展点表里一直有 `chunker`，这张表最初漏列了对应的插件行 |
| BGE-M3 向量化 | `official-embedder-bge-m3` | Phase 1 | in_process | torch 依赖较重但纯 Python，暂定 in_process；如果安装包体积问题突出可改 subprocess_service，Phase 1 中评估。实现上懒加载真实模型（`import sentence_transformers` 延迟到真正加载模型那一刻），单测用注入的假编码器（同旧项目的"假编码器 numpy 零向量"手法），不依赖真下载模型 |
| Chroma 向量库 | `official-vector-store-chroma` | Phase 1 | in_process | |
| BM25+jieba | `official-lexical-bm25` | Phase 1 | in_process | |
| RRF 融合 | `official-fusion-rrf` | Phase 1 | in_process | |
| 重排器 | `official-reranker` | Phase 1 | in_process | |
| 多库/路径级勾选(`decide_included`) | `official-library-manager` | Phase 1 | in_process | 默认必装插件（不是核心，但安装包强制预置），检索建立在"库范围"之上 |
| pywebview GUI 壳+基础面板 | `official-gui-shell` | Phase 1 | in_process | 只做壳+库管理/搜索/结果三个面板，其余面板随对应功能插件各自带 |
| MCP 检索工具 | 随各功能插件自己注册 `mcp_tool_provider` | Phase 1起逐步 | — | 不集中在一个插件，谁提供功能谁注册对应工具 |
| MinerU 云端 OCR | `official-ocr-mineru-cloud` | Phase 2 | **改成 in_process**（只是一次HTTP调用，没有需要独立环境隔离的重依赖，subprocess_service 是不必要的复杂度，见 ROADMAP.md Phase 2 状态） | ✅ 已实现，懒加载/可注入HTTP客户端（同 official-embedder-bge-m3 模式），9用例 |
| MinerU 本机 OCR | `official-ocr-mineru-local` | Phase 2 | subprocess_service | ✅ 已实现（真实子进程+本机HTTP+GPU资源租约协商），刻意不下载真实模型/未接入 env_bootstrap，见 ROADMAP.md Phase 2 状态；独立 py3.12/uv 环境的真正引导仍待做 |
| WEMM 页级视觉检索 | `official-visual-wemm` | Phase 2 | subprocess_service | **已确认：官方独立插件**，不降级为社区插件。复杂度最高的一项，Phase 2 单独评估工作量 |
| MinHash+LSH 去重 | `official-dedup` | Phase 3 | in_process | |
| 库 AI 摘要 | `official-library-summary` | Phase 3 | in_process | ✅ 已实现——最远点采样复用索引已有向量，AI生成随便覆盖/用户手写需走写权限门禁，依赖 `llm_provider` 扩展点（`official-llm-openai-compatible`，`multi`基数链式尝试）。注：表头原写"HyDE式LLM调用"容易误读成包含查询侧HyDE增强——调查后确认HyDE（`retriever.py::hyde_generate`）是旧项目里平行独立的另一功能，同协议不同配置端点，这次没有实现，见 ROADMAP.md TODO |
| GPU 显存仲裁 | 核心服务（不是插件） | Phase 2起随 subprocess_service 插件出现而启用 | — | 见 ARCHITECTURE.md 2.1节"资源仲裁器" |
| Agent 写权限门禁(`selection_gate`/`summary_gate`) | 核心服务（不是插件） | Phase 0起可用，Phase 3实际被`library-manager`/`library-summary`插件使用 | — | 见 ARCHITECTURE.md 2.2节 |
| 导出/导入(换电脑搬家) | `official-import-export` | Phase 3 | in_process | ✅ 已实现——`archive_codec` 单例扩展点，只负责 zip 归档打包/解包，不知道 library_manager/lexical_index/vector_store 的存在；跨插件编排在 `core/pipeline.py` 的 `export_library`/`import_library`（同 `index_library`/`search` 的编排模式）。9+9 用例（插件自身归档格式测试 + Pipeline 端到端往返测试），另有 MCP 工具（`export_library`/`import_library`，base64 传输）和 GUI Api（真实文件读写）两条调用路径的回归测试 |
| 旧 `libraries.json` 迁移脚本 | 一次性 CLI 工具，不常驻 | Phase 3 | — | ✅ 已实现（`tools/migrate_libraries_json.py`），字段映射对照旧项目 `library.py` 真实 schema 核对过，不迁移的字段（agent_formats/exclude_dirs等）在脚本 docstring 里逐条写清楚原因 |
| MinHash+LSH 去重 | `official-dedup` | Phase 3 | in_process | ✅ 已实现，用 datasketch 库 |
| Agent 写权限门禁 | 核心服务（`core/write_gate.py`） | Phase 0/3 | — | ✅ 已实现（提案号+确认码+TTL+一次性），✅ 已被真实调用（2026-09-23）——`official-library-summary` 是第一个真实调用方：AI经MCP对话想覆盖用户手写的库简介时走 propose/apply 两段式确认，端到端验证过完整链路，见 docs/ROADMAP.md Phase 3 说明 |
| Flet 经典 GUI | **已确认：不迁移** | — | — | 和 guiweb 功能重复、维护负担已确认不值得——操作者已同意直接砍掉。新架构下 `gui_panel` 扩展点本身就支持替换整个 GUI，想要 Flet 体验的话可以有人做成一个独立的 `gui_panel` 实现，但不作为官方维护对象 |

**仍待确认的1处判断**：Agent 写权限门禁+GPU 仲裁定为"核心服务"而非"插件"（见 [ROADMAP.md](ROADMAP.md)"开放问题"）。WEMM 官方插件、Flet 不迁移两处已在 2026-09-22 确认。
