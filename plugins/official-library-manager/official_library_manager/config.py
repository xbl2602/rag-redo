"""库配置的持久化：<data_dir>/libraries.json（data_dir 由核心通过
PluginContext.data_dir 提供，见 core/context.py，插件自己不拼路径）。

**已知的、刻意的 Phase 1 简化**：本模块直接负责自己配置的读写，而不是先
设计一套通用的"DataStore 落盘后端"再削足适履——目前只有这一个插件需要
持久化配置，等 Phase 1 后续插件（比如库摘要）显露出真实的持久化需求形状
后，再回头把"插件私有配置落盘"的通用部分收进 core.datastore，不提前拍
脑袋设计（YAGNI）。这是对 docs/DATA_FLOW.md"插件不直接读写持久化存储"
这条规则的已知临时妥协，在这里明确记录，不是假装没这回事——见
docs/ROADMAP.md Phase 1 进度记录。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from core.atomic import atomic_write_text

from .selection import norm_selection_path

# obsidian-rag/library.py:41 INVALID_NAME_CHARS。库 id/名称会拼进块 id
# （core/pipeline.py:106 约定 f"{library_id}:{path}:{chunk_index}"，:106/115
# 靠切分还原 library 与 path）和集合名（official-vector-store-chroma/
# store.py:42 f"lib_{library_id}"），冒号正是块 id 的字段分隔符：库 a:b 的块
# "a:b:notes/x.md:0" 会被解析成 library='a'、path='b:notes/x.md'——索引照样
# 建得起来（集合名走哈希），故障在检索装配时才爆，结果张冠李戴。旧项目在
# 结构上不可能发生：validate_name 直接把冒号这类字符拒掉。
INVALID_NAME_CHARS = set('/\\:*?"<>|')
MAX_NAME_LEN = 64

# Agent 人机分权（BC-02）：文本类恒可；二进制类必须用户逐个批准。放这里
# 而不是 plugin.py，是为了让 config.set_agent_formats 的写侧校验和
# plugin.agent_allowed_extensions 的读侧白名单共用同一份定义（两处各写一
# 份必然漂移）。
AGENT_TEXT_FORMATS = (".md", ".txt")
AGENT_BINARY_FORMATS = (".pdf", ".docx")

_LIST_FIELDS = (
    "selection_in", "selection_out", "enabled_extensions", "agent_formats",
    "exclude_dirs", "exclude_files", "exclude_patterns",
)


@dataclass
class LibraryConfig:
    library_id: str
    name: str
    root_path: str
    selection_in: list[str] = field(default_factory=list)
    selection_out: list[str] = field(default_factory=list)
    # 中性文件默认归属（旧 config.selection_new_files 三态：follow/include/
    # exclude，默认 follow=按该库格式开关判定）
    new_file_default: str = "follow"
    enabled_extensions: list[str] = field(default_factory=lambda: [".md", ".pdf", ".docx"])
    agent_formats: list[str] = field(default_factory=list)
    # 出厂排除默认集（对齐 obsidian-rag/config.py DEFAULTS，问题1/§4.2 索引
    # 清洗）：.obsidian/.git 等系统目录和 AGENTS.md/会话日志被照常建库会造成
    # 检索污染。仅"新建库未显式指定"时生效；已持久化的库配置不受影响。
    exclude_dirs: list[str] = field(
        default_factory=lambda: [".obsidian", ".smart-env", ".trash", ".git", "TEMP", "templates"]
    )
    exclude_files: list[str] = field(
        default_factory=lambda: ["目录.md", "AGENTS.md", "LOG.md", "README.md"]
    )
    exclude_patterns: list[str] = field(default_factory=lambda: ["session-", "会话", ".tmp"])


# 必须在 LibraryConfig 定义之后重建（上面的 _FIELD_BY_NAME 只是给模块级
# 常量占位，真正用的时候 dataclass 已经就绪）。
_FIELD_BY_NAME = {f.name: f for f in fields(LibraryConfig)}


def _field_default(key: str) -> list[str]:
    f = _FIELD_BY_NAME[key]
    if f.default_factory is not MISSING:  # type: ignore[misc]
        return list(f.default_factory())  # type: ignore[misc]
    if f.default is not MISSING:  # type: ignore[misc]
        return list(f.default)  # type: ignore[misc]
    return []


def config_defaults() -> dict[str, list[str]]:
    """库配置里"列表型"字段的出厂默认值——出厂默认集的**唯一出处**（新建库
    取它，"清空恢复默认"也取它，GUI 判断"这项是不是被覆盖过"同样取它，三处
    不各抄一份）。只含列表字段；`new_file_default` 是标量，默认 "follow"。"""
    return {key: _field_default(key) for key in _LIST_FIELDS if key in _FIELD_BY_NAME}


def _describe_char(char: str) -> str:
    """把一个非法字符说成人话：可见字符原样加引号，控制字符给码位。"""
    if ord(char) < 32 or ord(char) == 127:
        return f"U+{ord(char):04X}（控制字符）"
    return f"“{char}”"


def _path_key(path: str) -> str:
    """路径同一性比较键：Windows 下大小写不敏感（os.path.normcase 在
    Windows 上做 lower + 正斜杠归一，在 POSIX 上原样返回——同一台机器上的
    行为，不需要跨平台分支）。Path.resolve() 已经消掉了尾斜杠、`.`/`..`
    和 symlink/junction 别名（实测见注释），这里只补大小写。"""
    return os.path.normcase(str(path))


def _collection_name_for(library_id: str) -> str:
    """这个库在向量库里的 collection 名——对齐
    official-vector-store-chroma/store.py:42 `f"lib_{library_id}"`
    （有活跃 generation 时走 `libg_{sha256}`，与 id 无关）。"""
    return f"lib_{library_id}"


def norm_extension(value: object) -> str:
    s = str(value).strip().lower().lstrip(".")
    return f".{s}" if s else ""


def norm_extension_list(values: Any) -> list[str]:
    out: list[str] = []
    for value in values or ():
        ext = norm_extension(value)
        if ext and ext not in out:
            out.append(ext)
    return out


class LibraryConfigStore:
    def __init__(self, path: Path, *, logger: logging.Logger | None = None) -> None:
        self.path = path
        self._log = logger or logging.getLogger("rag_redo.library_manager.config")
        self._libraries: dict[str, LibraryConfig] = self._load()

    # ------------------------------------------------------------------
    # 读侧：损坏备份 + 逐条防御（对齐 obsidian-rag/library.py:391-419
    # load_registry 的"先改名 .bak 再按空注册表处理 + 非法条目跳过并记日志"）
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, LibraryConfig]:
        if not self.path.exists():
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            self._log.warning("libraries.json 读取失败（%s），按空注册表处理", exc)
            return {}
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            self._backup_corrupt(f"JSON 解析失败：{exc}")
            return {}
        if not isinstance(raw, dict):
            # 旧项目只对 JSON 解析失败备份（library.py:402-408），顶层结构
            # 不是对象时它直接当空注册表——下一次 save 就把原文件覆盖掉。
            # 这里一并备份：两种情况的性质完全一样（文件读不出内容），
            # 备份的代价只是多一个 .bak，收益是原配置可恢复。
            self._backup_corrupt(f"顶层不是对象（{type(raw).__name__}）")
            return {}
        out: dict[str, LibraryConfig] = {}
        for lib_id, data in raw.items():
            cfg = self._coerce_entry(str(lib_id), data)
            if cfg is not None:
                out[cfg.library_id] = cfg
        return out

    def _backup_corrupt(self, reason: str) -> None:
        """损坏的注册表先改名备份，再按空注册表处理。

        逐字对齐 obsidian-rag/library.py:399-408 的意图（原文注释："解析
        失败：先改名 .bak 备份再按空注册表处理（避免后续 save_registry 覆
        盖损坏文件导致其余库的注册信息永久丢失）"）。没有这一步，一次手滑
        写坏 JSON 之后，用户随便建一个库就会用 `{}`+新条目把原文件原子覆
        盖掉，全部库的勾选 / 排除名单 / 格式开关一起不可恢复。

        备份策略：单份、固定名 `libraries.json.bak`，已存在即覆盖（旧项目
        用的就是 `Path.replace`，本身就是覆盖语义）。不做 `.bak.1/.bak.2`
        轮换——损坏是罕见事件，轮换只会让人猜哪份最新，而"最近一次损坏前
        的完整副本"这个需求单份就够（旧项目也只留单份）。
        """
        backup = self.path.with_name(self.path.name + ".bak")
        try:
            os.replace(self.path, backup)
        except OSError as exc:
            self._log.error(
                "libraries.json %s，且备份失败（%s）；原文件保持原样未动，本次按"
                "空注册表处理——请立刻手工复制一份再排查，别在这个状态下新建库",
                reason, exc,
            )
            return
        self._log.error(
            "libraries.json %s，已备份为 %s 后按空注册表处理。恢复办法：把 %s "
            "改回 %s（在此之前不要新建库，否则会覆盖这份备份）",
            reason, backup.name, backup.name, self.path.name,
        )

    def _coerce_entry(self, lib_id: str, data: Any) -> LibraryConfig | None:
        """把一条原始 JSON 变成可用的 LibraryConfig，任何一处不合规都不让
        整个注册表陪葬（对齐 obsidian-rag/library.py:410-418：非 dict /
        缺 name / 缺 path / 库名非法 → 跳过该条并记日志，其余库照常可用）。

        读侧**不做**长度/首尾空格这类只影响展示的校验（旧项目读侧用完整
        validate_name，是因为它的 name 同时就是 id；REDO 里 name 不参与块
        id 拼接，为它把用户的库整个跳过代价大于收益）。这些规则在写侧
        `add_library` 查。
        """
        if not isinstance(data, dict):
            self._log.error(
                "libraries.json 条目 %r 不是对象（%s），已跳过", lib_id, type(data).__name__
            )
            return None
        known = set(_FIELD_BY_NAME)
        unknown = sorted(set(map(str, data)) - known)
        if unknown:
            self._log.warning(
                "libraries.json 条目 %r 含本版本不认识的字段：%s——已忽略（旧版本"
                "遗留或手工编辑）。这里必须容忍而不是抛 TypeError：任何一个多余"
                "键过去都会让插件 on_load 失败，宿主直接起不来（对齐 "
                "obsidian-rag/library.py:410-418 读侧逐条跳过并记日志）",
                lib_id, "、".join(unknown),
            )
        kwargs: dict[str, Any] = {k: v for k, v in data.items() if k in known}
        # 注册表的 key 才是库身份（_save 一律按 cfg.library_id 建 key）。字段
        # 里的 library_id 只做交叉校验：不一致时以 key 为准，否则用户以为库还
        # 是原来那个，下次保存却被悄悄改名。
        field_id = kwargs.get("library_id")
        if isinstance(field_id, str) and field_id and field_id != lib_id:
            self._log.warning(
                "libraries.json 条目 key=%r 与其中的 library_id=%r 不一致，以 key 为准",
                lib_id, field_id,
            )
        kwargs["library_id"] = lib_id

        missing = [
            key for key in ("name", "root_path")
            if not isinstance(kwargs.get(key), str) or not str(kwargs[key]).strip()
        ]
        if missing:
            self._log.error("libraries.json 条目 %r 缺 %s，已跳过", lib_id, "/".join(missing))
            return None

        # 库名含非法字符 → 跳过（逐字对齐 obsidian-rag/library.py:414-417
        # validate_name 失败即 continue）。库 id 含非法字符 → 保留但大声告警：
        # id 非法意味着这个库的块在检索装配时会归错库（见 INVALID_NAME_CHARS
        # 注释），但把整个库从 GUI 里抹掉会让用户"库凭空消失"且原因更难查，
        # 而 id 是可以手工改回来的字段。
        for label, value in (("库 id", lib_id), ("库名称", str(kwargs["name"]))):
            bad = [c for c in value if c in INVALID_NAME_CHARS or ord(c) < 32]
            if not bad:
                continue
            detail = "、".join(_describe_char(c) for c in dict.fromkeys(bad))
            if label == "库名称":
                self._log.error(
                    "libraries.json 条目 %r 库名含非法字符（%s），已跳过", lib_id, detail
                )
                return None
            self._log.error(
                "libraries.json 条目 %r 的库 id 含非法字符（%s）：它的块在检索"
                "装配时会被归到错误的库（块 id 用冒号分隔库 id/路径/块号）。"
                "仍保留该条目以便你手工改回——请直接编辑 libraries.json 里的 id",
                lib_id, detail,
            )

        for key in _LIST_FIELDS:
            kwargs[key] = self._coerce_list(lib_id, key, kwargs.get(key))
        kwargs["new_file_default"] = self._coerce_new_file_default(
            lib_id, kwargs.get("new_file_default", "follow")
        )
        return LibraryConfig(**kwargs)

    def _coerce_list(self, lib_id: str, key: str, value: Any) -> list[str]:
        if value is None:
            return _field_default(key)
        if not isinstance(value, list):
            self._log.warning(
                "libraries.json 条目 %r 的 %s 不是列表（%s），已用默认值",
                lib_id, key, type(value).__name__,
            )
            return _field_default(key)
        if key in ("selection_in", "selection_out"):
            return self._coerce_selection_list(lib_id, key, value)
        if key in ("enabled_extensions", "agent_formats"):
            # 纯归一（补点、小写、去重、保序），不改语义：decide_included 与
            # bulk_for_extensions 两边都按小写带点比较。
            return norm_extension_list(value)
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                self._log.warning(
                    "libraries.json 条目 %r 的 %s 含非字符串元素 %r，已静默丢弃",
                    lib_id, key, item,
                )
                continue
            text = item.strip()
            if text and text not in out:
                out.append(text)
        return out

    def _coerce_selection_list(self, lib_id: str, key: str, value: list) -> list[str]:
        """勾选两表的读侧归一：反斜杠/尾斜杠等写法全部归一，非法元素静默
        丢弃并告警。

        归一是**必须**的，不是洁癖：裁决侧 selection.py:82-91 做的是精确
        集合比对，磁盘上留着 "docs\\a.md" 或 "x/" 就永远命中不了那条规则，
        用户看到的是"我明明勾了，怎么没生效"——Windows 上手写/迁移产生的反
        斜杠和尾斜杠是极常见的。写侧（set_selection）也归一，这里是双保险，
        因为文件可能被手工编辑或从旧项目迁移过来（对齐
        obsidian-rag/library.py:225-238 _sel_entry_lists）。
        """
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                self._log.warning(
                    "libraries.json 条目 %r 的 %s 含非字符串元素 %r，已静默丢弃"
                    "（对齐 obsidian-rag library.py:225-238「非法元素静默丢弃，"
                    "防手改逃逸」）", lib_id, key, item,
                )
                continue
            try:
                norm = norm_selection_path(item)
            except ValueError as exc:
                self._log.warning(
                    "libraries.json 条目 %r 的 %s 含非法路径 %r，已静默丢弃：%s",
                    lib_id, key, item, exc,
                )
                continue
            if norm not in out:
                out.append(norm)
        return out

    def _coerce_new_file_default(self, lib_id: str, value: Any) -> str:
        if isinstance(value, str) and value in ("follow", "include", "exclude"):
            return value
        self._log.warning(
            "libraries.json 条目 %r 的 new_file_default=%r 非法，已回退 follow",
            lib_id, value,
        )
        return "follow"

    def _save(self) -> None:
        # 原子写——对齐 obsidian-rag/library.py::save_registry（422-427）对
        # libraries.json 的 tmp+replace 纪律：注册表写半截 = 全部库配置丢失。
        raw = {lib_id: asdict(cfg) for lib_id, cfg in self._libraries.items()}
        atomic_write_text(self.path, json.dumps(raw, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------
    # 写侧：身份与路径校验（对齐 obsidian-rag/library.py:487-505 add_library）
    # ------------------------------------------------------------------

    @staticmethod
    def _check_label(value: str, label: str) -> None:
        if not value:
            raise ValueError(f"{label}不能为空")
        if value.strip() != value:
            raise ValueError(f"{label}不能有首尾空格：{value!r}")
        if len(value) > MAX_NAME_LEN:
            raise ValueError(
                f"{label}过长（{len(value)} 字符，上限 {MAX_NAME_LEN}）：{value!r}"
            )
        bad = [c for c in value if c in INVALID_NAME_CHARS or ord(c) < 32]
        if bad:
            detail = "、".join(_describe_char(c) for c in dict.fromkeys(bad))
            why = (
                "库 id 会拼进块 id（库 id:路径:块号）与集合名，这些字符会让检索"
                "装配切错归属"
                if label == "库 id"
                else "库名称会作为检索结果与列表里的来源标注出现，这些字符在"
                "Windows 上还是保留/通配字符，放行会让展示与后续处理出现歧义"
            )
            raise ValueError(
                f"{label}含非法字符：{detail}（不可含 "
                f"{''.join(sorted(INVALID_NAME_CHARS))} 以及控制字符）——{why}。"
                f"原值：{value!r}"
            )

    def _resolve_identity(
        self, library_id: Any, name: Any, root_path: Any, *, require_existing_dir: bool
    ) -> tuple[str, str, str]:
        name = "" if name is None else str(name)
        self._check_label(name, "库名称")
        library_id = "" if library_id is None else str(library_id)
        if not library_id:
            # 缺省取名称（旧 obsidian-rag 里 name 与库身份是同一个字段，
            # `library.py add <路径> --name 名` 不给名字时取目录名，见
            # library.py:492-494；REDO 把两者拆开后就落在这一层）
            library_id = name
        self._check_label(library_id, "库 id")
        root_path = "" if root_path is None else str(root_path)
        if not root_path.strip() or "\x00" in root_path:
            raise ValueError(f"非法库路径: {root_path!r}")
        # 相对路径**不拒绝**，按 obsidian-rag/library.py:489 的语义解析到进程
        # 工作目录（旧 GUI/CLI 都传绝对路径，CLI 帮助里也写着"文件夹绝对
        # 路径"，但代码本身是 resolve 而非报错）。逐字对齐，不自行加严。
        try:
            resolved = Path(root_path.strip()).resolve()
        except OSError as exc:
            raise ValueError(f"库路径无法解析（{root_path!r}）：{exc}") from exc
        if require_existing_dir and not resolved.is_dir():
            raise ValueError(f"路径不存在或不是目录：{resolved}")
        return library_id, name, str(resolved)

    def add_library(
        self,
        library_id: str | None,
        name: str,
        root_path: str,
        *,
        require_existing_dir: bool = True,
    ) -> LibraryConfig:
        """注册一个库。

        `require_existing_dir=False` 是给"从归档导入、目标目录稍后才由用户
        摆文件"的场景留的口子（旧 obsidian-rag 是 import.py:248 先
        `mkdir(parents=True, exist_ok=True)` 再 add_library，所以它不需要这
        个开关）。默认 True：路径打错必须在**创建时**报错，而不是拖到之后
        每次检索的 freshness 诊断里才报 missing。
        """
        library_id, name, root = self._resolve_identity(
            library_id, name, root_path, require_existing_dir=require_existing_dir
        )
        if library_id in self._libraries:
            raise ValueError(f"库 {library_id!r} 已存在")
        # **不**拒绝重名（旧的 library.py:495-496 有这条"库名已存在"）：旧项目
        # 的 name 就是库身份（resolve_entries 按 name 索引），同名必然撞车；
        # REDO 把身份拆成 library_id 之后，name 只是展示标签，检索范围、
        # 选库参数、块归属全都走 library_id。硬要唯一反而会打死"从归档导入
        # 成一个新 id 的库"这条正当流程（import 把归档里的 name 原样带过来，
        # 源库还在注册表里就会撞名，见 tests/test_pipeline_e2e.py 的
        # TestExportImportLibrary）。代价是两个库的显示名可能一样——这个由
        # GUI 列表展示解决，不该用"不许注册"来换。
        root_key = _path_key(root)
        for other in self._libraries.values():
            if _path_key(other.root_path) == root_key:
                raise ValueError(
                    f"该路径已注册：{root}（已属于库 {other.library_id!r}）。同一"
                    f"个目录注册两次会让两库各建一份索引，跨库检索把同一批内容"
                    f"重复返回两遍"
                )
        # collection 名冲突（对齐 obsidian-rag/library.py:499-501）。REDO 里
        # collection 名由 library_id 直接派生，而 library_id 唯一 + Chroma
        # 实测区分大小写，所以这条目前按构造不可达；保留它是为了把"派生规则
        # 一旦变化"这个不变量挡在数据入口，而不是等到两个库写进同一个集合。
        collection = _collection_name_for(library_id)
        for other in self._libraries.values():
            if _collection_name_for(other.library_id) == collection:
                raise ValueError(f"派生 collection 与现有库冲突：{collection}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,510}[A-Za-z0-9]", collection):
            # 只告警不拒绝：现有真实库 id 里就有带空格和中文的
            # （tools/register_real_libraries.py），擅自加严等于让这些库
            # 消失。真正要修的是向量库插件把 id 派生成合法 Chroma 名，属
            # 另一个插件的职责，见报告"风险与遗留"。
            self._log.warning(
                "库 %r 派生出的向量库集合名 %r 不符合 Chroma 的命名要求"
                "（3-512 字符、仅 [A-Za-z0-9._-]、首尾为字母数字）；未索引过的"
                "库做块数统计时会取不到集合。", library_id, collection,
            )
        cfg = LibraryConfig(library_id=library_id, name=name, root_path=root)
        self._libraries[library_id] = cfg
        self._save()
        return cfg

    def list_libraries(self) -> list[LibraryConfig]:
        return list(self._libraries.values())

    def get(self, library_id: str) -> LibraryConfig | None:
        return self._libraries.get(library_id)

    def _selection_root(self, library_id: str, cfg: LibraryConfig) -> Path:
        try:
            return Path(cfg.root_path).resolve()
        except OSError as exc:
            raise ValueError(
                f"库 {library_id!r} 的根路径无法解析（{cfg.root_path!r}）：{exc}"
            ) from exc

    def _normalize_selection_write(
        self, library_id: str, root: Path, values: Any
    ) -> list[str]:
        """写侧归一 + 越界校验。

        归一与读侧同一套 `norm_selection_path`（反斜杠/尾斜杠/空白），保证
        落盘的就是裁决侧能命中的形式——写侧不归一的话，用户在 GUI 里勾的
        "docs\\a.md" 会在裁决时静默失配。

        越界校验对齐 obsidian-rag/library.py:261-263：把 `(root / rel)`
        整体 resolve 之后必须仍在 root 之内。这里 resolve 是必须的，因为
        Windows 的 junction（`mklink /J`）和 symlink 会让词法上在库内的路径
        实际指向库外——本机实测 junction 确实被 Path.resolve() 跟到了目标，
        所以这道校验对 junction 有效（symlink 需要管理员/开发者模式，本机
        无权限建，未能实测，见报告）。resolve 失败（不存在的路径在
        nonstrict 模式下不会失败，失败的是超长路径/非法字符等 OSError）时
        **fail closed** 拒绝写入：无法证明在库内就不能放行。
        """
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"勾选路径必须是字符串列表：{values!r}")
        root_key = _path_key(str(root))
        out: list[str] = []
        for item in values:
            rel = norm_selection_path(item)
            try:
                target = (root / rel).resolve()
            except OSError as exc:
                raise ValueError(f"勾选路径无法解析（{item!r}）：{exc}") from exc
            target_key = _path_key(str(target))
            if target_key != root_key and not any(
                _path_key(str(p)) == root_key for p in target.parents
            ):
                raise ValueError(
                    f"路径越出库范围：{rel}（解析后为 {target}，不在库根 {root} 内）"
                )
            if rel not in out:
                out.append(rel)
        return out

    def set_selection(
        self,
        library_id: str,
        *,
        selection_in: list[str] | None = None,
        selection_out: list[str] | None = None,
    ) -> LibraryConfig:
        cfg = self._libraries.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        root = self._selection_root(library_id, cfg)
        # 先在局部变量里算完再落回 cfg：同位置打架的拒绝必须连**内存**里的
        # 配置也原样不动，否则 GUI 那边一次被拒的改动会留在内存里、下一次
        # 无关的 set_* 就把它一起写进磁盘。
        new_in = (
            self._normalize_selection_write(library_id, root, selection_in)
            if selection_in is not None
            else None
        )
        new_out = (
            self._normalize_selection_write(library_id, root, selection_out)
            if selection_out is not None
            else None
        )
        effective_in = cfg.selection_in if new_in is None else new_in
        # 同位置打架最后兜底（旧 library.py::set_selection 问题47 用户拍板）：
        # 纳入目标本身躺在目录排除名单里（字符串相等）= "存上但永远不生效"的
        # 矛盾态，拒绝落盘。文件名/格式类规则不在此列——点具体文件属个别
        # 例外，静默生效。MCP 提案侧已在 normalize_selection_changes 事前拦截。
        blocked = {
            str(e).replace("\\", "/").strip().strip("/")
            for e in cfg.exclude_dirs
            if str(e).strip().strip("/")
        }
        clash = sorted(r for r in effective_in if r in blocked)
        if clash:
            raise ValueError(
                "勾选与排除名单打架（同位置矛盾）：%s 已在目录排除名单（exclude_dirs）里，"
                "纳入不会生效。请先从排除名单移除（库配置 exclude_dirs，仅本库生效即可），"
                "或改勾它下面的具体文件（个别例外直接生效，无需弹窗）。" % "、".join(clash)
            )
        if new_in is not None:
            cfg.selection_in = new_in
        if new_out is not None:
            cfg.selection_out = new_out
        self._save()
        return cfg

    def set_policy(
        self,
        library_id: str,
        *,
        new_file_default: str | None = None,
        enabled_extensions: list[str] | None = None,
        exclude_dirs: list[str] | None = None,
        exclude_files: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> LibraryConfig:
        """改"新文件默认策略"/"启用格式列表"——这两项在 `add_library` 时
        只能取字段默认值，之前没有任何公开方法能在创建后改它们（GUI 设置
        面板、tools/migrate_libraries_json.py 都需要这个能力，不是只服务
        迁移脚本一个调用方，所以做成正式的公开方法，不是迁移脚本专用的
        私有旁路）。

        这里同时是**格式开关批量勾选语义的唯一收口点**（对齐
        obsidian-rag/library.py:602-606：extensions 变更后必调
        bulk_for_extensions，且必须在 save 之后调——它内部是读改写，先调会
        被本次 save 覆盖，旧项目真机踩过这个坑，见 obsidian-rag/TASK_LOG.md
        问题44）。GUI/CLI/MCP 都只能通过这一个公开方法改格式开关，所以把
        收口挂在这里就够，不能只挂在 GUI 上。
        """
        cfg = self._libraries.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        if new_file_default is not None:
            if new_file_default not in ("follow", "include", "exclude"):
                raise ValueError(
                    f"非法中性默认值: {new_file_default!r}（只接受 follow/include/exclude）"
                )
            cfg.new_file_default = new_file_default
        old_exts: list[str] | None = None
        if enabled_extensions is not None:
            old_exts = list(cfg.enabled_extensions)
            cfg.enabled_extensions = norm_extension_list(enabled_extensions)
        if exclude_dirs is not None:
            cfg.exclude_dirs = [str(e) for e in exclude_dirs]
        if exclude_files is not None:
            cfg.exclude_files = [str(e) for e in exclude_files]
        if exclude_patterns is not None:
            cfg.exclude_patterns = [str(e) for e in exclude_patterns]
        self._save()
        if old_exts is not None:
            # 格式移除 → 显式勾选的该格式**文件**移入 selection_out（用户
            # 拍板③：全局格式开关 = 对该格式文件的批量勾/取消）；格式新增 →
            # selection_out 里的该格式文件条目移除（恢复跟随=纳入）。文件夹
            # 级条目永不被批量触碰。
            from .selection import bulk_for_extensions

            new_in, new_out, moved = bulk_for_extensions(
                old_exts, cfg.enabled_extensions, cfg.selection_in, cfg.selection_out
            )
            if moved:
                cfg.selection_in, cfg.selection_out = new_in, new_out
                self._save()
                self._log.info(
                    "库 %s 格式开关 %s → %s：已迁移 %d 条文件级勾选条目",
                    library_id, old_exts, cfg.enabled_extensions, moved,
                )
        return cfg

    def set_agent_formats(self, library_id: str, formats: list[str]) -> LibraryConfig:
        cfg = self._libraries.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        # 授权必须落在当前已启用的格式上（对齐 obsidian-rag/library.py:578-590
        # 与 server.py 的 set_config 拦截）：未在格式开关里启用的格式授权了
        # 也不会被索引，留着就是个"授权了但永远不生效"的矛盾态。授权清单随
        # 格式开关收窄自动失效（agent_allowed_extensions 读侧再取一次
        # 交集，effects 即使这一层被绕过也拦得住）。
        enabled = set(norm_extension_list(cfg.enabled_extensions))
        normalized: list[str] = []
        for value in formats or ():
            extension = norm_extension(value)
            if not extension:
                continue
            if extension not in AGENT_BINARY_FORMATS:
                raise ValueError(
                    f"Agent 二进制授权只支持 "
                    f"{'、'.join(AGENT_BINARY_FORMATS)}，收到: {value!r}"
                )
            if extension not in enabled:
                raise ValueError(
                    f"以下格式未在该库 enabled_extensions 中启用，无法授权："
                    f"{extension}（单一事实来源是格式开关——用户在格式开关里"
                    f"取消某格式时，对 Agent 的授权自动随之失效）"
                )
            if extension not in normalized:
                normalized.append(extension)
        cfg.agent_formats = normalized
        self._save()
        return cfg

    def remove_library(self, library_id: str) -> None:
        self._libraries.pop(library_id, None)
        self._save()
