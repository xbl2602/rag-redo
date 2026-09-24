# RAG REDO

> A full architectural rewrite of a local semantic search system for Obsidian notes. **The core search pipeline works end to end, and Windows distribution is a self-contained portable ZIP** (see "Status" below). [中文 README](README.md)

## What this is

Search your own Obsidian notes by meaning, on your own machine — notes never leave it. This is a ground-up rewrite of the `obsidian-rag` project: the core shrank from a handful of tens-of-thousands-line files down to two lightweight components ("plugin runtime" + "data-flow manager"), and everything else (which embedding model to use, whether to support OCR, whether to do page-level visual retrieval, which GUI to run) is a swappable plugin — like modding Minecraft, instead of one fixed bundle of features.

## Status

Phase 1 (text-search MVP) is implemented with real test coverage (350+ cases): 19 official plugins (multi-format extraction, chunking, library management, BM25 lexical search, BGE-M3 embeddings, Chroma vector store, RRF fusion, reranking, MCP tools, GUI shell, near-duplicate detection, export/import, MinerU cloud/local OCR, WEMM page-level visual navigation, library summaries, an OpenAI-compatible LLM provider) + the orchestration layer + the Agent write-gate. `official-visual-wemm` (page-level visual navigation — a "second retrieval system" independent of text search, exposed as the `navigate_knowledge` MCP tool) has been fully implemented and verified on a real Windows machine with the real `tencent/WeMM-Embedding-2B` model. Fine-grained GPU/VRAM lifecycle management (idle unload, self-exit, active eviction, retrieval-side priority preemption) has been ported to match the old project's real behavior. Library summaries (helping an AI judge "is this library worth searching?" before it commits to a full search, with user-authored summaries protected by the Agent write-gate) are fully implemented. See [docs/ROADMAP.md](docs/ROADMAP.md) for the itemized status.

`official-ocr-mineru-local` (on-device PDF OCR) is now wired to a real MinerU model — it detects and reuses an existing `uv tool install mineru[all]` environment on the machine (no re-downloading weights), and has been verified against real mixed Chinese/English scanned PDFs.

**Not done yet**: Windows distribution has switched to a portable ZIP with a bundled Python runtime (`installer/build_windows.py` — no PyInstaller/Inno Setup needed); it still has to be verified on a clean Windows machine without any development tools, and the package size can be slimmed further. On-device MinerU OCR (`official-ocr-mineru-local`) intentionally reuses an existing `uv tool install mineru[all]` environment instead of bundling one.

Architecture docs:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — architecture overview
- [docs/DATA_FLOW.md](docs/DATA_FLOW.md) — data-flow rules
- [docs/PLUGIN_SPEC.md](docs/PLUGIN_SPEC.md) — plugin spec
- [docs/ROADMAP.md](docs/ROADMAP.md) — phased roadmap and status
- [docs/FEATURE_TRIAGE.md](docs/FEATURE_TRIAGE.md) — old-project capability triage/migration status
- [docs/LESSONS.md](docs/LESSONS.md) — distilled architectural lessons from the old project

## Try it now (from source; verified on both Linux and Windows)

There is no published download yet. To build the portable ZIP yourself: `.venv/Scripts/python.exe installer/build_windows.py` produces `dist/rag-redo-portable.zip` (bundled Python runtime, core, plugins and start scripts — unzip, double-click `start-gui.cmd`; see [installer/README.md](installer/README.md)). The more common path below is source + a virtualenv. Commands are equivalent on Linux/Windows — swap `.venv/bin/` for `.venv\Scripts\`:

```bash
git clone <this-repo> rag-redo
cd rag-redo
python3 -m venv .venv
.venv/bin/pip install jieba chromadb pymupdf4llm python-docx pywebview mcp datasketch
# The next line is the model dependency for text semantic search (BGE-M3) + reranking —
# sizable (a few hundred MB to 1GB for the CPU build). The first search against a library
# will also auto-download the BGE-M3 + reranker weights (a few GB, one-time):
.venv/bin/pip install torch sentence-transformers --index-url https://download.pytorch.org/whl/cpu
# The next line is optional: only needed for official-visual-wemm (PDF page-level visual
# navigation, the navigate_knowledge tool), and is larger still (reuses torch from above
# if already installed):
.venv/bin/pip install transformers qwen-vl-utils torchvision --index-url https://download.pytorch.org/whl/cpu
```

Open the desktop GUI (it ships with a [demo-vault/](demo-vault/) you can try immediately — click "New Library", give it a name and the absolute path to `demo-vault`, click "Rebuild index", then search for something like "plugin architecture"):

```bash
.venv/bin/python gui_main.py
```

Or connect it to an MCP-capable AI tool (Claude Code, opencode, etc.) by pointing its config at `mcp_stdio.py`:

```json
{
  "type": "stdio",
  "command": "/path/to/rag-redo/.venv/bin/python",
  "args": ["mcp_stdio.py"],
  "cwd": "/path/to/rag-redo"
}
```

Once connected, the AI gets these tools: `search_knowledge` (hybrid text search, with multi-library search / exclude / folder-scoped filtering — `libraries` defaults to all registered libraries when left blank) / `navigate_knowledge` (PDF page-level visual navigation — an independent "second retrieval system" that never participates in the fusion ranking of the former) / `list_libraries` / `reindex_knowledge` / `export_library` / `import_library` / `get_library_sample` + `propose_library_summary` + `apply_library_summary` (library summaries: helps the AI judge "is this library worth searching?" before it dives in; user-authored summaries are protected by the write-gate — the AI cannot overwrite them without explicit confirmation).

Run the tests (no need to install torch/sentence-transformers/qwen-vl-utils — the whole suite runs against injected fake models, see [docs/LESSONS.md](docs/LESSONS.md)):

```bash
.venv/bin/python tests/run.py
```

## The goal (what "done" looks like)

- **Install**: on Windows, download an installer or a portable build and it just works — no manually installing Python/CUDA or anything like that
- **Plugin-based**: a tiny core; retrieval algorithms, embedding models, OCR, and the GUI are all plugins you can freely enable/disable/replace/write yourself
- **Data flow**: how data moves and what format it's in has one authoritative definition, not "wire it up wherever's convenient"
- **Independent dependencies**: doesn't rely on anything pre-installed on the user's machine; heavy optional capabilities download on demand instead of forcing everyone to install a pile of things they'll never use
- **Windows-first, Linux for development**
