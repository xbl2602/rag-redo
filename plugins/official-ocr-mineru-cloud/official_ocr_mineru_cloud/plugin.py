"""official-ocr-mineru-cloud 插件：生命周期钩子的薄封装，真实逻辑在
extract.py/ocr.py。"""
from __future__ import annotations

from pathlib import Path

from .extract import MineruCloudExtractor
from .ocr import resolve_api_key

#: MinerU 云端精准解析 API 单份文件的页数上限（官网文档 mineru.net/apiManage/docs，2026-10-01 核对：
#: 单份 ≤ 200MB、≤ 200 页，每个账号每天 1000 页最高优先级额度）。编排层把更长的段切成几份上传。
MAX_PAGES_PER_FILE = 200
#: 设置项 `mineru_local_overflow`：本机识别超过页数上限时怎么办。
OVERFLOW_OFF = "off"
OVERFLOW_TO_CLOUD = "mineru-cloud"


class MineruCloudOcrPlugin:
    def __init__(self) -> None:
        self.extractor = MineruCloudExtractor()
        self._settings = None

    def on_load(self, ctx):
        self._settings = ctx.settings
        self.extractor = MineruCloudExtractor(
            pending_path=ctx.storage.file("mineru_pending.json", legacy="mineru_pending.json"),
            sidecar_dir=ctx.storage.directory("mineru_sidecars", legacy="mineru_sidecars"),
            quota_path=ctx.storage.file("mineru_quota.json", legacy="mineru_quota.json"),
            logger=ctx.logger,
            settings=ctx.settings,
        )
        ctx.logger.info("MinerU云端OCR已加载")

    def on_enable(self, ctx):
        ctx.logger.info("MinerU云端OCR已启用")

    def on_disable(self, ctx):
        ctx.logger.info("MinerU云端OCR已禁用")

    def on_unload(self, ctx):
        self._settings = None

    def reset_token_flag(self) -> None:
        """每轮索引开始的钩子（core/pipeline.py:541-547 对每个
        extractor:pdf 插件调用一次）。LEGACY 在同一个时点做两件事：
        obsidian-rag/index.py:1878 复位 Token 失效标志、index.py:2176-2177
        清理断点簿记孤儿。少任何一件都会留下"标志永不复位"或"簿记只增不减"
        的慢性病（前者让用户补好 Key 后重跑索引毫无反应）。"""
        self.extractor.reset_token_flag()
        if self.is_active():
            # 本轮不走云端段时别去动云端簿记（清理要逐条读盘算指纹，不值这份开销）
            self.extractor.prune_pending()

    def is_active(self) -> bool:
        selected = self._settings.get("pdf_scan_backend", "none") if self._settings is not None else "none"
        return selected == "mineru-cloud"

    def index_signature(self) -> str:
        selected = self._settings.get("pdf_scan_backend", "none") if self._settings is not None else "none"
        key_state = "key" if resolve_api_key(self._settings) else "nokey"
        # 开了“超过本机上限送云端”，此前因为超上限没识别的书下一轮自动补上（BC-04）
        overflow = "overflow" if self.overflow_active() else "no-overflow"
        return f"selected:{selected}:{overflow}:{key_state}"

    def overflow_active(self) -> bool:
        """本机识别超过页数上限时，云端接不接手（2026-10-01 操作者确认，BC-01）：扫描件后端选的是
        本机、设置里选了“送 MinerU 云端”、并且配了 Key，三样都满足才接手。默认关——会把超出的那
        部分页面上传到 MinerU 云端、耗云端额度（本地优先，§1.3：出本机的能力必须用户显式开启）。"""
        if self._settings is None:
            return False
        if self._settings.get("pdf_scan_backend", "none") != "mineru-local":
            return False
        if str(self._settings.get("mineru_local_overflow", OVERFLOW_OFF) or "") != OVERFLOW_TO_CLOUD:
            return False
        return bool(resolve_api_key(self._settings))

    def page_budget(self) -> int | None:
        """云端不限一本书识别几页（按份计额度），只限一份的页数（见 `max_pages_per_request`）。"""
        return None

    def max_pages_per_request(self) -> int | None:
        return MAX_PAGES_PER_FILE

    def extract(self, library_id: str, path: str, root: Path):
        return self.extractor.extract(library_id, path, root)
