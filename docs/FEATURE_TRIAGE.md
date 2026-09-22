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
| MinerU 云端 OCR | `official-ocr-mineru-cloud` | Phase 2 | subprocess_service（网络调用，隔离 API Key 相关代码） | |
| MinerU 本机 OCR | `official-ocr-mineru-local` | Phase 2 | subprocess_service | 独立 py3.12/uv 环境，按需下载，不进核心 venv |
| WEMM 页级视觉检索 | `official-visual-wemm` | Phase 2 | subprocess_service | **已确认：官方独立插件**，不降级为社区插件。复杂度最高的一项，Phase 2 单独评估工作量 |
| MinHash+LSH 去重 | `official-dedup` | Phase 3 | in_process | |
| 库 AI 摘要(HyDE 式 LLM 调用) | `official-library-summary` | Phase 3 | in_process | 依赖 `llm_provider` 扩展点 |
| GPU 显存仲裁 | 核心服务（不是插件） | Phase 2起随 subprocess_service 插件出现而启用 | — | 见 ARCHITECTURE.md 2.1节"资源仲裁器" |
| Agent 写权限门禁(`selection_gate`/`summary_gate`) | 核心服务（不是插件） | Phase 0起可用，Phase 3实际被`library-manager`/`library-summary`插件使用 | — | 见 ARCHITECTURE.md 2.2节 |
| 导出/导入(换电脑搬家) | `official-import-export` | Phase 3 | in_process | |
| 旧 `libraries.json` 迁移脚本 | 一次性 CLI 工具，不常驻 | Phase 3 | — | 已确认要做 |
| Flet 经典 GUI | **已确认：不迁移** | — | — | 和 guiweb 功能重复、维护负担已确认不值得——操作者已同意直接砍掉。新架构下 `gui_panel` 扩展点本身就支持替换整个 GUI，想要 Flet 体验的话可以有人做成一个独立的 `gui_panel` 实现，但不作为官方维护对象 |

**仍待确认的1处判断**：Agent 写权限门禁+GPU 仲裁定为"核心服务"而非"插件"（见 [ROADMAP.md](ROADMAP.md)"开放问题"）。WEMM 官方插件、Flet 不迁移两处已在 2026-09-22 确认。
