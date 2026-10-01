"""BC-15 阶段A 门禁：GUI 资产必须与 obsidian-rag/guiweb/ui 逐字节一致。

验收基准冻结在 tests/fixtures/legacy_guiweb_contract.json（sha256 + 字节数 +
37 个契约方法 + 5 类推送 + 7 个视图），这样本仓库的 CI 不需要旧项目仓库在场
也能独立校验"复刻"这件事，而不是靠人眼比对。

同时校验 BC-15 第(5)条零业务逻辑：前端不得出现检索算法计算，排序只能是
视图排序，且不得直连文件/网络（contracts.md 通用约定第 6 条）。
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from collections import Counter
from pathlib import Path


PLUGIN_DIR = Path(__file__).parent.parent
ASSETS_DIR = PLUGIN_DIR / "official_gui_shell" / "assets"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "legacy_guiweb_contract.json"

#: 前端绝对不允许出现的检索算法 token——这些一旦出现就说明业务判断漏到了
#: 前端，违反 AGENTS.md 第 6 节"入口不得各自实现排序、过滤或错误处理"。
FORBIDDEN_ALGORITHM_TOKENS = (
    "RRF",
    "rrf",
    "bm25",
    "BM25",
    "rerank",
    "Rerank",
    "cosine",
    "tf-idf",
    "tfidf",
    "BGE",
    "reciprocal_rank",
)

#: 允许出现的 .sort( 都是视图排序：按长度排列表 ×2。白名单化而不是禁掉 .sort，
#: 防止以后有人以"视图需要"为名把检索排序搬进前端。（原先还有按 conf 排图谱检索命中、
#: 按 s 排图谱节点尺寸各 1 处，2026-09-30 随旧图谱引擎一起移除，见 BC-18。）
SORT_PATTERN = r"\.sort\(function \(a, b\) \{ return ([^;]+); \}\);"
BENIGN_SORT_COMPARATORS = {
    "b.length - a.length": 2,
}

STARMAP_JS = ASSETS_DIR / "starmap" / "starmap.js"
#: 星图里允许的 3 处排序全是画图用的：求沿环密度的中位数、求内容差距的中位数（数值排序），
#: 以及按点数从少到多排轨道（点少的在内圈）。文件顺序本身一律来自核心，前端不排。
STARMAP_SORTS = Counter({
    ".sort()": 2,
    ".sort((a, b) => a.nPts - b.nPts)": 1,
})
#: 操作者 2026-09-30 定下的星图默认值（BC-18），正式实现必须用这一组。
STARMAP_DEFAULTS = {
    "size": 1.0, "speed": 1.0, "spread": 1.0, "bulge": 0.15, "gather": 5.2, "mix": 5.0, "body": 0.35, "bg": 1.0,
    "dens": 2750.0, "densTol": 2.0, "orbitGap": 1.0, "rMin": 1.0,
}


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class TestLegacyAssetParity(unittest.TestCase):
    """阶段A 量化指标：4/4 资产逐字节一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _fixture()
        cls.app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")

    def test_assets_directory_contains_exactly_the_legacy_ui_files(self) -> None:
        expected = sorted(item["name"] for item in self.fixture["assets"])
        actual = sorted(p.name for p in ASSETS_DIR.iterdir() if p.is_file())
        self.assertEqual(actual, expected)

    def test_every_legacy_asset_is_byte_identical(self) -> None:
        for item in self.fixture["assets"]:
            with self.subTest(asset=item["name"]):
                path = ASSETS_DIR / item["name"]
                self.assertTrue(path.is_file(), f"缺少资产 {item['name']}")
                raw = path.read_bytes()
                self.assertEqual(
                    len(raw),
                    item["bytes"],
                    f"{item['name']} 字节数与旧项目不一致",
                )
                self.assertEqual(
                    hashlib.sha256(raw).hexdigest().upper(),
                    item["sha256"].upper(),
                    f"{item['name']} sha256 与旧项目不一致——BC-15 要求逐字节复刻",
                )

    def test_index_html_still_loads_the_three_siblings(self) -> None:
        html = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
        for sibling in ("app.css", "app.js", "mock.js"):
            self.assertIn(sibling, html)


class TestLegacyPushChannel(unittest.TestCase):
    """BC-15 第(3)条：五类推送与 window.__push 必须在场。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _fixture()
        cls.app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")

    def test_app_js_defines_production_push_entry(self) -> None:
        # 旧项目问题47：生产版从未定义 window.__push，推送被守卫静默吞掉，
        # KPI 与进度永远停在启动那一刻。移植后必须保留这个定义。
        self.assertIn("window.__push = function (type, payloadJson)", self.app_js)
        self.assertIn("new CustomEvent(type, { detail: payloadJson })", self.app_js)

    def test_app_js_listens_to_snapshot_log_and_preview(self) -> None:
        for push_type in ("snapshot", "log", "preview"):
            with self.subTest(push=push_type):
                self.assertIn(
                    f"window.addEventListener('{push_type}'",
                    self.app_js,
                )

    def test_mock_js_is_gated_so_production_never_shows_fake_data(self) -> None:
        mock_js = (ASSETS_DIR / "mock.js").read_text(encoding="utf-8")
        self.assertIn("window.pywebview", mock_js)
        self.assertIn("__RAG_MOCK", mock_js)

    def test_fixture_declares_all_five_push_types(self) -> None:
        self.assertEqual(
            sorted(self.fixture["push_types"]),
            sorted(["snapshot", "log", "alert", "notice", "preview"]),
        )


class TestNoBusinessLogicInFrontend(unittest.TestCase):
    """BC-15 第(5)条：前端零业务判断。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")

    def test_no_retrieval_algorithm_tokens(self) -> None:
        for token in FORBIDDEN_ALGORITHM_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(
                    token,
                    self.app_js,
                    f"前端出现检索算法 token {token}——业务判断必须留在 core",
                )

    def test_every_sort_is_a_whitelisted_view_sort(self) -> None:
        found = re.findall(SORT_PATTERN, self.app_js)
        self.assertEqual(
            Counter(found),
            Counter(BENIGN_SORT_COMPARATORS),
            f"前端 .sort( 与基准不符：{found}",
        )

    def test_frontend_does_not_touch_files_or_network(self) -> None:
        for token in ("fetch(", "XMLHttpRequest", "require(", "import("):
            with self.subTest(token=token):
                self.assertNotIn(token, self.app_js)


class TestReleaseGpuButton(unittest.TestCase):
    """BC-16：手动释放显存按钮——本仓库唯一一处刻意打破 BC-15『逐字节
    复刻』的地方（2026-09-29 操作者确认，旧项目 guiweb 没有对应能力）。
    `TestLegacyAssetParity.test_every_legacy_asset_is_byte_identical` 已经
    用更新过的 bytes/sha256 覆盖了这三个文件的完整性，这里额外确认新增的
    这一小块内容真的在场——防止以后有人为了让字节数对上又把它删掉。"""

    def test_button_markup_is_in_index_html(self) -> None:
        html = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="btnReleaseGpu"', html)

    def test_app_js_wires_the_button_to_the_new_api_method(self) -> None:
        app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("$('btnReleaseGpu').addEventListener", app_js)
        self.assertIn("API.release_gpu_memory()", app_js)

    def test_mock_js_implements_the_method_for_demo_mode(self) -> None:
        mock_js = (ASSETS_DIR / "mock.js").read_text(encoding="utf-8")
        self.assertIn("release_gpu_memory: function", mock_js)


class TestDependentSettingsAndCloudConsent(unittest.TestCase):
    """设置页（2026-10-01，BC-01/BC-15）：“超过本机上限时”只在扫描件后端选本机时可改——前端按后端
    给的 enabled_when 通用地变灰，不写具体键名；选成送云端和切到云端识别一样先弹同意框。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
        cls.mock_js = (ASSETS_DIR / "mock.js").read_text(encoding="utf-8")

    def test_greying_out_is_generic(self) -> None:
        start = self.app_js.index("function bindEnabledWhen()")
        body = self.app_js[start : self.app_js.index("function bindPickButtons()")]
        self.assertIn("data-enkey", body)
        self.assertNotIn("mineru", body, "变灰逻辑不认具体的设置项")
        self.assertIn("f.enabled_when", self.app_js)
        self.assertIn('"enabled_when": {"key": "pdf_scan_backend"', self.mock_js)

    def test_sending_pages_to_the_cloud_asks_for_consent_first(self) -> None:
        self.assertIn("['pdf_scan_backend', 'pdf_text_backend', 'mineru_local_overflow']", self.app_js)


class TestFailuresAndSettingChangesAreVisible(unittest.TestCase):
    """2026-10-01 操作者反馈：“设置怎么改都不生效，也没有报错提示，根本不知道是没生效还是静默失败”。
    查下来一是某个库索引失败时界面照常显示“就绪”，二是保存后永远提示“已热读生效”。前端只照后端给的
    `progress.failed` 和保存结果里的 `changed`/`notes` 显示，不自己判断（BC-15）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
        cls.mock_js = (ASSETS_DIR / "mock.js").read_text(encoding="utf-8")

    def test_a_failed_index_run_turns_the_status_red_and_says_why_once(self) -> None:
        island = self.app_js[self.app_js.index("function paintIsland("): self.app_js.index("function paintHeartCap(")]
        self.assertIn("p.failed", island)
        self.assertIn("索引失败", island)
        cap = self.app_js[self.app_js.index("function paintHeartCap("): self.app_js.index("function paintStepper(")]
        self.assertIn("hb-dead", cap)
        self.assertIn("last.error", cap)
        snapshot = self.app_js[self.app_js.index("function onSnapshot("):]
        self.assertIn("S.failSeen[key]", snapshot, "每个库的每一轮只提示一次")
        self.assertIn("toggleLog(true)", snapshot)
        self.assertIn("failed: []", self.mock_js)

    def test_saving_settings_says_what_changed_and_when_it_takes_effect(self) -> None:
        self.assertNotIn("已保存，配置已热读生效", self.app_js)
        self.assertIn("res.changed", self.app_js)
        self.assertIn("res.notes", self.app_js)
        self.assertIn("changed: changed, notes: notes", self.mock_js)


class TestConversionCachesAreVisible(unittest.TestCase):
    """BC-19：转换缓存看得见（2026-09-30 操作者确认，旧项目没有这个能力）。

    资产完整性由上面的字节比对覆盖；这里确认：库卡片、诊断页清单、文件详情真的接上了新接口，
    旧的“WEMM 页库明细”表已并入清单（前端不再调它），缺的原因和下一步的人话只在核心写一份。"""

    NEW_METHODS = (
        "conversion_caches", "conversion_cache_file", "open_cache_folder",
        "reveal_cache_file", "page_preview", "page_try_search",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
        cls.mock_js = (ASSETS_DIR / "mock.js").read_text(encoding="utf-8")

    def test_diagnostics_page_has_the_conversion_list_and_the_old_page_table_is_merged(self) -> None:
        for element_id in ("ccSec", "ccBody", "ccOnly", "ccLib", "ccOpenDir", "ccSum"):
            with self.subTest(element=element_id):
                self.assertIn(f'id="{element_id}"', self.html)
        self.assertLess(self.html.index('id="ccSec"'), self.html.index('id="failBody"'), "清单放在诊断页最上面")
        for gone in ('id="wemmBody"', 'id="wemmLibSel"'):
            self.assertNotIn(gone, self.html)
        self.assertNotIn("API.wemm_status(", self.app_js, "页库明细已并入转换缓存清单")
        self.assertIn('id="wemmProbeBtn"', self.html, "页库服务状态与“探测服务”保留")

    def test_app_js_uses_every_new_method_and_mock_js_implements_them(self) -> None:
        for method in self.NEW_METHODS:
            with self.subTest(method=method):
                self.assertIn(f"API.{method}(", self.app_js)
                self.assertIn(f"{method}: function", self.mock_js)
        self.assertIn("ccCardLine(l.name)", self.app_js, "库卡片上那一行")
        self.assertIn('id="giCc"', self.app_js, "文件详情里的转换缓存一节")

    def test_a_partially_recognized_pdf_shows_its_missing_pages(self) -> None:
        """PDF 按页分流（2026-10-01，BC-01）：转好了但有图片页没识别的，清单与详情都写出页码。"""
        self.assertIn("t.state === 'partial'", self.app_js)
        self.assertIn("页没识别", self.app_js)
        self.assertIn("state: 'partial'", self.mock_js, "演示数据里有一份这样的书")

    def test_reason_labels_live_only_in_core(self) -> None:
        import sys

        repo_root = PLUGIN_DIR.parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from core.conversion_cache import REASON_CODES, reason_text

        # 只查“下一步怎么办”那几句：短标签（如“文件打不开”）旧前端的失败明细本来就有同名文案
        for code in REASON_CODES:
            _label, step = reason_text(code)
            with self.subTest(code=code):
                self.assertNotIn(step, self.app_js, "缺的原因和下一步由桥接层带过来，前端不另写一份")

    def test_page_thumbnails_are_dropped_when_the_row_closes(self) -> None:
        # 小图只在内存里放一张：收起那一行就连同小图一起丢掉，不越积越多
        self.assertIn("else box.innerHTML = '';", self.app_js)


class TestStarMapReplacesGraphView(unittest.TestCase):
    """BC-18：图谱页换成总览星图（2026-09-30 操作者确认，旧项目没有这个能力）。

    index.html/app.js/app.css/mock.js 的完整性由上面的字节比对覆盖；星图模块和本地放置的
    three.js 另有一组固定值（夹具 `starmap_assets`）。这里再确认几件"改了就会出事"的事：
    three.js 只从本地加载、星图代码里没有检索算法和联网调用、显存开关没被人顺手打开、
    默认值还是操作者定的那一组。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _fixture()
        cls.html = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
        cls.starmap = STARMAP_JS.read_text(encoding="utf-8")

    def test_starmap_and_vendored_three_are_pinned(self) -> None:
        pinned = {item["name"]: item for item in self.fixture["starmap_assets"]}
        on_disk = sorted(
            p.relative_to(ASSETS_DIR).as_posix()
            for p in ASSETS_DIR.rglob("*")
            if p.is_file() and p.parent != ASSETS_DIR
        )
        self.assertEqual(on_disk, sorted(pinned), "资产子目录里只能有登记过的星图与 three.js 文件")
        for name, item in pinned.items():
            with self.subTest(asset=name):
                raw = (ASSETS_DIR / name).read_bytes()
                self.assertEqual(len(raw), item["bytes"])
                self.assertEqual(hashlib.sha256(raw).hexdigest().upper(), item["sha256"].upper())

    def test_three_is_loaded_locally_only(self) -> None:
        self.assertIn('"three":"./vendor/three/three.module.min.js"', self.html)
        self.assertIn('<script type="module" src="starmap/starmap.js"></script>', self.html)
        self.assertNotRegex(self.html, r"(src|href)=\"https?://", "界面不得从网上加载任何脚本或样式（离线可用）")
        imports = re.findall(r"^import .+ from '([^']+)';$", self.starmap, flags=re.M)
        self.assertEqual(imports, ["three", "../vendor/three/OrbitControls.js"])
        license_text = (ASSETS_DIR / "vendor" / "three" / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT", license_text)

    def test_starmap_has_no_retrieval_logic_or_network(self) -> None:
        for token in FORBIDDEN_ALGORITHM_TOKENS + ("fetch(", "XMLHttpRequest", "require(", "import("):
            with self.subTest(token=token):
                self.assertNotIn(token, self.starmap)
        sorts = Counter({form: self.starmap.count(form) for form in STARMAP_SORTS})
        self.assertEqual(sorts, STARMAP_SORTS)
        self.assertEqual(self.starmap.count(".sort("), sum(STARMAP_SORTS.values()), "星图里出现了白名单以外的 .sort(")

    def test_starmap_keeps_the_vram_guards(self) -> None:
        """显存敏感（每 MB 都算）：这些开关任何一个被改回去，显存都会成倍上涨。"""
        for guard in (
            "antialias: false", "depth: false", "stencil: false", "powerPreference: 'low-power'",
            "renderer.setPixelRatio(1)", "renderer.forceContextLoss()", "new THREE.BufferAttribute(D.pts, 1)",
        ):
            with self.subTest(guard=guard):
                self.assertIn(guard, self.starmap)
        self.assertNotIn("CanvasTexture", self.starmap, "库名和光晕不做成贴图")
        self.assertRegex(self.starmap, r"hide\(\) \{\s+visible = false;\s+destroy\(\);", "离开图谱页必须立刻交还显卡")

    def test_starmap_defaults_are_the_operator_values(self) -> None:
        block = re.search(r"const DEFAULTS = Object\.freeze\(\{(.+?)\}\);", self.starmap, flags=re.S)
        self.assertIsNotNone(block)
        values = {k: float(v) for k, v in re.findall(r"(\w+): ([\d.]+)", block.group(1))}
        self.assertEqual(values, STARMAP_DEFAULTS)

    def test_starmap_splats_conserve_energy_so_zoom_keeps_its_colour(self) -> None:
        """放大不能把颜色洗白（真机症状：拉到 20x 整片纯白，缩回去灰白看不出色相）。

        机制：加法混合（AddEquation）下，一个点的屏幕面积 ∝ gl_PointSize²，而点大小
        ∝ 1/相机距离 —— 放大时面积按缩放平方涨，同一像素累加的亮度跟着涨，冲破 1.0
        就被逐通道裁剪，三个通道都到 1.0 必然是白色，色相信息在这里丢的。所以每个点
        和每片体积光都要按面积反比把强度压回去。
        """
        # 点层和体积光层都必须归一化：只修一层，另一层照样能把画面推到纯白。
        self.assertRegex(
            self.starmap,
            r"vNorm = clamp\([^;]*area \* area[^;]*\);",
            "点层没有按面积做能量守恒",
        )
        self.assertRegex(
            self.starmap,
            r"vGain \*= clamp\([^;]*sHalf \* sHalf[^;]*\);",
            "体积光层没有按面积做能量守恒",
        )
        # 夹紧方向是最容易写反的一处：只写下界的话，缩到远处时面积被兜成 1 像素，
        # 算出来是 AREA_REF²=64 倍亮度，远处直接烧成噪点。所以上下界都要在。
        for m in re.finditer(r"clamp\(([^;]*?),\s*([\d.]+),\s*([\d.]+)\)", self.starmap):
            lo, hi = float(m.group(2)), float(m.group(3))
            if "area * area" in m.group(1) or "sHalf * sHalf" in m.group(1):
                self.assertLess(lo, hi, f"能量守恒的夹紧方向反了（{m.group(0)[:60]}）")
                self.assertEqual(hi, 1.0, "上界必须是 1.0（比参考点还小就不补偿）")
        # 归一化只能等比缩放三通道：一旦单独改某一通道就等于改色相。
        fs = re.search(r"const POINT_FS = `(.*?)`", self.starmap, flags=re.S)
        self.assertIsNotNone(fs)
        self.assertIn("vColor * (core + halo + spike) * vFade * vNorm", fs.group(1))

    def test_starmap_view_prefs_are_remembered(self) -> None:
        """星图外观与机位必须跨会话记住。

        `DEFAULTS` 是 Object.freeze 的，每次打开都回到默认——用户调好的视角得每次
        重来一遍，这是真机反馈的原话"设置是一次性的"。
        """
        self.assertIn("const PREFS_KEY = 'ragredo.starmap.ui.v1'", self.starmap)
        self.assertIn("localStorage.setItem(PREFS_KEY", self.starmap)
        self.assertIn("localStorage.getItem(PREFS_KEY)", self.starmap)
        # 12 个滑块 + 图层 mask + 轨道线 + 相机，三样都要存。
        for token in ("payload[sl.key] = ui[sl.key]", "mask: ui.mask.slice()",
                      "rings: !!ui.rings", "cam = {", "pos:", "target:"):
            self.assertIn(token, self.starmap, f"没有持久化：{token}")
        # 相机要等 gl 建好之后才回放：mount() 里 gl 还不存在。
        self.assertIn("const cam = loadPrefs();", self.starmap)
        self.assertIn("pendingCam = cam;", self.starmap)
        self.assertIn("restoreCamera(pendingCam)", self.starmap)
        # 拖滑块是连续事件，每帧写 localStorage 会卡手。
        self.assertRegex(self.starmap, r"if \(prefsTimer\) clearTimeout\(prefsTimer\)")
        # 恢复默认也要立刻写盘，否则关掉再打开又回到这一次的值，看起来像没生效。
        reset = re.search(r"function resetUi\(\) \{(.+?)\n    \}", self.starmap, flags=re.S)
        self.assertIsNotNone(reset)
        self.assertIn("savePrefs()", reset.group(1))

    def test_starmap_prefs_are_validated_before_adoption(self) -> None:
        """被手改坏的记录不能把半径/密度算成 0 或 NaN，否则整张图直接空掉。"""
        load = re.search(r"function loadPrefs\(\) \{(.+?)\n  function restoreCamera", self.starmap, flags=re.S)
        self.assertIsNotNone(load)
        body = load.group(1)
        self.assertIn("saved.v !== 1", body, "旧版本记录必须被识别出来丢掉")
        self.assertIn("isFinite", body)
        self.assertIn("Math.min(sl.hi, Math.max(sl.lo, v))", body, "滑块值必须夹回声明范围")

    def test_starmap_prefs_do_not_go_through_the_settings_store(self) -> None:
        """视图偏好不进 SettingsStore，这是有理由的，别后来人"顺手改成一致"。

        `save_settings` 只接受 settings_schema.py 登记过的键
        （contract_bridge.py：`if key not in FIELDS: continue` 静默跳过），而 §8.5 要求
        登记的键必须有真实读取点、禁止假开关。相机角度没有任何插件会读，登记它会同时
        踩到"假开关"和"设置页多出一个跟系统设置无关的条目"。所以走 localStorage。
        """
        for token in ("API.save_settings", "API.get_settings", "save_settings(", "settings_store"):
            self.assertNotIn(token, self.starmap, f"视图偏好不该走 SettingsStore：{token}")

    def test_app_js_uses_the_overview_map_and_no_longer_draws_the_old_graph(self) -> None:
        self.assertIn("API.overview_map(scopeStr())", self.app_js)
        self.assertNotIn("API.graph(", self.app_js)
        self.assertNotIn("API.semantic_edges(", self.app_js)
        self.assertIn("m.hide()", self.app_js)
        mock_js = (ASSETS_DIR / "mock.js").read_text(encoding="utf-8")
        self.assertIn("overview_map: function", mock_js)


if __name__ == "__main__":
    unittest.main()
