"""GUI 后端测试用的最小真实环境（不是测试文件：文件名不以 `test_` 开头，不会被
`tests/run.py` 当成一套测试收进去）。

真实的 `PluginRuntime` + 真实的 `Pipeline` / library-manager / 向量库 / BM25 / 融合 / 库简介，
只把三个"重资源"换成假的：嵌入模型（按关键词计数）、重排模型、LLM HTTP 客户端。这样测的是
桥接层与 core 的**真实交互**，而不是对着 Mock 断言调用参数。

**清理顺序**（2026-09-28 审计 L-3）：先 `runtime.close()`（卸载插件、让 Chroma 释放句柄），
再 `gc.collect()`，最后删目录——否则 Windows 上文件被占用，`rmtree(ignore_errors=True)` 静默
失败，`%TEMP%` 里就留下越来越多的测试残留目录。
"""
from __future__ import annotations

import gc
import shutil
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent.parent
REPO_ROOT = PLUGIN_DIR.parent.parent
for _p in (REPO_ROOT, PLUGIN_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
for _other in (REPO_ROOT / "plugins").glob("*"):
    if _other.is_dir() and str(_other) not in sys.path:
        sys.path.insert(0, str(_other))

from official_gui_shell.api import Api  # noqa: E402

from core.pipeline import Pipeline  # noqa: E402
from core.runtime import PluginRuntime  # noqa: E402

REQUIRED_PLUGINS = [
    "official-extractor-text",
    "official-chunker",
    "official-library-manager",
    "official-lexical-bm25",
    "official-embedder-bge-m3",
    "official-vector-store-chroma",
    "official-fusion-rrf",
    "official-reranker",
    "official-import-export",
    "official-dedup",
    "official-library-summary",
    "official-llm-openai-compatible",
    "official-result-advisor",
]


class FakeEncoder:
    KEYWORDS = ["插件", "架构"]

    def encode(self, texts):
        return [[float(t.count(k)) for k in self.KEYWORDS] for t in texts]


class FakeReranker:
    def score(self, query, texts):
        terms = query.split()
        return [sum(text.count(term) for term in terms) for text in texts]


class FakeLlmClient:
    def __init__(self, response: str = "这是一个关于插件架构的知识库。") -> None:
        self.response = response
        self.calls: list[dict] = []

    def complete(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user})
        return self.response


def rmtree_retry(path: Path, attempts: int = 8) -> None:
    """Windows 上句柄释放有延迟：短退避重试，最后一次仍失败才放弃（不抛）。"""
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            gc.collect()
            time.sleep(0.15 * (attempt + 1))
    shutil.rmtree(path, ignore_errors=True)


class GuiTestEnv:
    """一个临时数据目录 + 一个带假模型的真实运行时 + 一个 `Api`。"""

    def __init__(self, plugins: list[str] | None = None) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gui_env_"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "notes.md").write_text("# 插件架构\n\n插件系统笔记。见 [[other]]。", encoding="utf-8")
        self.runtime = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.runtime.scan()
        for plugin_id in plugins or REQUIRED_PLUGINS:
            self.runtime.load(plugin_id)
            self.runtime.enable(plugin_id)

        loaded = set(plugins or REQUIRED_PLUGINS)
        self.fake_llm = FakeLlmClient()
        if "official-embedder-bge-m3" in loaded:
            from official_embedder_bge_m3.embed import BGEM3Embedder

            self.runtime.plugins["official-embedder-bge-m3"].instance.embedder = BGEM3Embedder(
                encoder=FakeEncoder()
            )
        if "official-reranker" in loaded:
            from official_reranker.rerank import RerankerEngine

            self.runtime.plugins["official-reranker"].instance.engine = RerankerEngine(
                reranker=FakeReranker()
            )
        if "official-llm-openai-compatible" in loaded:
            from official_llm_openai_compatible.llm import OpenAiCompatibleClient

            self.runtime.plugins["official-llm-openai-compatible"].instance._client = OpenAiCompatibleClient(  # noqa: SLF001
                http_client=self.fake_llm
            )
        self.lib_mgr = self.runtime.plugins["official-library-manager"].instance
        self.pipeline = Pipeline(self.runtime)
        self.api = Api(self.pipeline, self.lib_mgr)

    def make_vault(self, name: str, files: dict[str, str]) -> Path:
        """在临时目录下造一个额外的库目录并写入文件（相对路径 → 正文）。"""
        root = self.tmp / name
        for rel, text in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return root

    def close(self) -> None:
        try:
            self.runtime.close()
        except Exception:  # noqa: BLE001 - 清理不能掩盖真正的测试失败
            pass
        self.api = self.pipeline = self.lib_mgr = None  # type: ignore[assignment]
        gc.collect()
        rmtree_retry(self.tmp)
