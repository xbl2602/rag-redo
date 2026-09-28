"""library_manager 插件的核心判定逻辑：一个文件到底算不算在检索范围内。

裁决语义逐字对齐旧 obsidian-rag（library.py::decide_included 的"问题47
用户拍板" + index.py::collect_md_files 的漏斗分支），2026-09-25 实测发现
此前的重写版在真实库上会产出不同结果，按旧代码移植：

1. 路径级显式规则（selection_in/selection_out）永远赢文件名/格式规则——
   显式勾选静默穿透目录继承排除、exclude_files/patterns、扩展名白名单。
2. 显式规则之间"最近显式赢"：从文件自身逐级向上找第一个命中祖先（含自身），
   同一层级 in/out 同时命中属非法状态（写路径已拦截），读侧 out 优先。
3. 显式纳入 vs 目录排除：更具体（更深）的赢——文件自身的勾选压过它所在
   目录的排除；反之更深的目录排除压过较浅的目录级纳入。同位置打架（纳入
   目标本身字符串相等地躺在 exclude_dirs 里）排除站住（安全），写路径拒绝
   新建此类状态。
4. 目录排除是子串语义：条目 'TEMP' 命中任意路径部件含 'TEMP' 的位置（含
   文件名部件，如 'MYTEMP notes.md'）——对齐旧 collect 漏斗逐字行为。
5. 都没有显式规则命中时走中性默认 new_file_default（旧 selection_new_files
   三态）：follow=按该库 enabled_extensions 格式开关判定（旧默认）；
   include=受支持格式（md/txt/pdf/docx）一律纳入、可穿透 extensions 白名单；
   exclude=中性文件一律排除。中性文件还要叠文件名排除（exclude_files 精确
   名单 + exclude_patterns 文件名前缀匹配——旧语义是 startswith，不是通配）。

唯一实现：本模块导出的 decide_included / collect_included_files 是这条
规则的唯一权威实现——GUI 的勾选树展示、建索引时的文件枚举漏斗，都必须调
这两个函数，不能各自维护一份影子逻辑（docs/DATA_FLOW.md 规则4）。
"""
from __future__ import annotations

import re
from typing import Literal, Sequence

NewFileDefault = Literal["follow", "include", "exclude"]
SELECTION_ACTIONS = ("in", "out", "neutral")
# 受支持格式（旧 extractors.SUPPORTED_EXTS = TEXT_EXTS | BINARY_EXTS）
SUPPORTED_EXTS = {"md", "txt", "pdf", "docx"}


def _norm_ex_dir_entries(ex_dirs: Sequence[str] | None) -> set[str]:
    """目录排除条目归一集合（旧 library.py::norm_ex_dir_entries）。

    注意 collect 漏斗侧是子串语义（条目 'TEMP' 会命中部件 'MYTEMP'），此处
    只做字符串归一不改语义：同位置打架只认字符串相等，子串覆盖面走深度规则。
    """
    out: set[str] = set()
    for e in (ex_dirs or ()):
        s = str(e).replace("\\", "/").strip().strip("/")
        if s:
            out.add(s)
    return out


def _excluded_dir_depth(ex_dirs: Sequence[str] | None, rel: str) -> int:
    """目录排除的最深命中深度（1-based；无命中 0）——旧
    library.py::excluded_dir_depth。

    匹配语义与 collect 漏斗逐字一致：条目为任一路径部件的子串即命中，取最深
    的部件位置（部件含文件名本身）。单文件库根（无斜杠）depth=1。
    """
    best = 0
    parts = str(rel).replace("\\", "/").strip("/").split("/")
    entries = [s for s in (str(e).replace("\\", "/").strip().strip("/")
                           for e in (ex_dirs or ())) if s]
    if not entries:
        return 0
    for i, part in enumerate(parts, 1):
        for en in entries:
            if en in part:
                best = i
                break
    return best


def _selection_hit(
    sel_in: Sequence[str] | None, sel_out: Sequence[str] | None, rel: str
) -> tuple[str | None, int, str]:
    """最近显式命中 → (action, depth, prefix)；无命中 → (None, 0, "")——旧
    library.py::selection_hit。

    depth = 命中的前缀段数（文件自身 = 全长，最具体）。同一层级同时命中两表
    属非法状态（写路径已防止），此处 out 优先（宁可少索引）。
    """
    sin = set(sel_in or ())
    sout = set(sel_out or ())
    parts = str(rel).replace("\\", "/").strip("/").split("/")
    for i in range(len(parts), 0, -1):
        pre = "/".join(parts[:i])
        if pre in sout:
            return ("out", i, pre)
        if pre in sin:
            return ("in", i, pre)
    return (None, 0, "")


def _rel_suffix(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def explicit_verdict(
    selection_in: Sequence[str] | None,
    selection_out: Sequence[str] | None,
    exclude_dirs: Sequence[str] | None,
    path: str,
) -> tuple[str | None, bool, str]:
    """谁具体听谁的（旧 library.py::decide_included，问题47 用户拍板）——
    返回 `(verdict, tie, reason)`：

    - `verdict`：`"in"` / `"out"` / `None`。`None` = 既没有显式规则、也没有
      目录排除命中的**中性**路径，交给文件名/格式规则继续判；
    - `tie`：仅"同位置打架"（勾选纳入的目标本身字符串相等地躺在目录排除名单里）
      时为 True，排除站住；
    - `reason`：给 `decide_included` 拼"为什么没收"的人话。

    单独导出是因为**勾选树的展示**和**建索引的文件漏斗**必须吃同一份裁决：
    树上显示入库、索引时却没收录，正是"显示≠实际"的撕裂。两处都从这里取
    显式部分，不各写一遍深度比较。
    """
    action, dm, prefix = _selection_hit(selection_in, selection_out, path)
    de = _excluded_dir_depth(exclude_dirs, path)
    if action is None:
        return ("out", False, "命中排除目录") if de else (None, False, "")
    if action == "out":
        return "out", False, f"显式排除规则命中：{prefix}"
    # action == "in"：显式勾选穿透一切（目录继承排除、文件名、格式白名单），
    # 唯二例外：同位置打架（排除站住）与更深的目录排除。
    if prefix in _norm_ex_dir_entries(exclude_dirs):
        return "out", True, f"同位置打架：{prefix} 同时在勾选纳入与目录排除名单，排除站住"
    if de == 0 or dm > de:
        return "in", False, f"显式纳入规则命中：{prefix}"
    return "out", False, f"更深的目录排除压过较浅的纳入（排除深度{de} ≥ 勾选深度{dm}）"


def selection_explicit(
    selection_in: Sequence[str] | None, selection_out: Sequence[str] | None, path: str
) -> str | None:
    """`path` 自己或最近祖先的显式勾选态：`"in"` / `"out"` / `None`（旧
    library.py::resolve_selection）。勾选树用它标注"显式"徽记。"""
    return _selection_hit(selection_in, selection_out, path)[0]


def dir_self_blocked(exclude_dirs: Sequence[str] | None, path: str) -> bool:
    """该节点本身是否被目录排除名单指名道姓（旧 library.py::
    is_same_place_blocked，问题47：即时弹窗的触发条件）。只认字符串相等——
    文件名/格式类规则是"按类匹配"，点具体文件属个别例外，永远不触发。"""
    s = str(path).replace("\\", "/").strip().strip("/")
    return bool(s) and s in _norm_ex_dir_entries(exclude_dirs)


def decide_included(
    path: str,
    *,
    selection_in: Sequence[str],
    selection_out: Sequence[str],
    new_file_default: NewFileDefault,
    enabled_extensions: Sequence[str] | None = None,
    exclude_dirs: Sequence[str] | None = None,
    exclude_files: Sequence[str] | None = None,
    exclude_patterns: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """判定 path 算不算在检索范围内，返回 (included, reason)——语义为旧
    library.py::decide_included + 旧 index.py::collect_md_files 中性分支的
    合并（两处旧代码本就是同一条裁决的两半）。"""
    verdict, _tie, reason = explicit_verdict(selection_in, selection_out, exclude_dirs, path)
    if verdict == "in":
        return True, reason
    if verdict == "out":
        return False, reason

    # 中性文件（无显式规则、目录排除也没命中）：文件名名单 → 文件名前缀 → 中性默认
    name = path.rsplit("/", 1)[-1]
    if name in set(exclude_files or ()):
        return False, f"命中排除文件名单：{name}"
    patterns = [str(p) for p in (exclude_patterns or ())]
    hit_pat = next((p for p in patterns if name.startswith(p)), None)
    if hit_pat is not None:
        return False, f"命中文件名前缀排除：{hit_pat}"
    if new_file_default == "exclude":
        return False, "中性默认=exclude（中性文件一律排除）"
    suffix = _rel_suffix(path)
    if new_file_default == "include":
        if suffix not in SUPPORTED_EXTS:
            return False, f"格式 {suffix or '(无后缀)'} 不在受支持格式内（中性默认=include）"
        return True, "中性默认=include（受支持格式一律纳入）"
    # follow（旧默认）：按该库格式开关判定
    exts = {str(e).lower().lstrip(".") for e in (enabled_extensions or ())}
    if suffix not in exts:
        return False, f"格式 {suffix or '(无后缀)'} 不在库格式开关内"
    return True, "中性跟随格式开关"


def collect_included_files(
    all_paths: Sequence[str],
    *,
    selection_in: Sequence[str],
    selection_out: Sequence[str],
    new_file_default: NewFileDefault,
    enabled_extensions: Sequence[str] | None = None,
    exclude_dirs: Sequence[str] | None = None,
    exclude_files: Sequence[str] | None = None,
    exclude_patterns: Sequence[str] | None = None,
) -> list[tuple[str, bool, str]]:
    """文件枚举唯一漏斗：任何"这个库现在应该处理哪些文件"的需求都必须走
    这个函数，不能自己再写一遍遍历+判断（docs/DATA_FLOW.md 规则4，继承
    旧项目"文件枚举只走 collect_md_files 一个漏斗"的教训）。返回逐文件的
    裁决（含未纳入的，附原因），调用方自己按需过滤——保留全量结果是为了
    "文件生效明细"这类需要展示"为什么没收"的场景不用再跑一遍判定。
    """
    return [
        (path, *decide_included(
            path,
            selection_in=selection_in,
            selection_out=selection_out,
            new_file_default=new_file_default,
            enabled_extensions=enabled_extensions,
            exclude_dirs=exclude_dirs,
            exclude_files=exclude_files,
            exclude_patterns=exclude_patterns,
        ))
        for path in all_paths
    ]


# ---------------------------------------------------------------------------
# 勾选变更（get_selection/propose_selection_changes/apply_selection_changes
# 三个 MCP 工具的数据面）：判定逻辑上面早就有了，缺的是"AI 想改这份配置"
# 这条写路径本身——对齐 obsidian-rag library.py::norm_sel_path/set_selection
# 与 selection_gate.py::normalize_changes 的校验、同位置拦截与合并语义，
# 写权限门禁部分复用 core/write_gate.py（在 plugin.py 里接线，这里只放纯
# 函数）。
# ---------------------------------------------------------------------------


def norm_selection_path(path: object) -> str:
    """校验并规范化一条勾选路径：必须是库内相对路径，拒绝绝对路径/盘符/
    UNC/`~`/`..`逃逸——对齐 obsidian-rag `library.py::norm_sel_path` 的
    防御性校验（防止 MCP 提案挟带恶意路径试图越出库根目录范围）。返回
    不带首尾斜杠、正斜杠分隔的规范化路径。"""
    if not isinstance(path, str):
        raise ValueError(f"勾选路径必须是字符串：{path!r}")
    s = path.strip().replace("\\", "/")
    if s.startswith("/") or s.startswith("~") or re.match(r"^[A-Za-z]:", s):
        raise ValueError(f"必须是库内相对路径（不含盘符/UNC/~/正斜杠根）：{path!r}")
    s = s.strip("/")
    if not s:
        raise ValueError("勾选路径不能为空")
    parts = s.split("/")
    if any(part.strip() in ("", ".", "..") for part in parts):
        raise ValueError(f"路径非法（不得含空段/./..）：{path!r}")
    return "/".join(part.strip() for part in parts)


def normalize_selection_changes(
    changes: Sequence[dict] | None, exclude_dirs: Sequence[str] | None = None
) -> list[dict]:
    """校验+规范化一批勾选变更：路径合法、action 合法、同位置矛盾事前拦截
    （旧 selection_gate.py::normalize_changes 的"问题47 用户拍板"：action=in
    且目标本身就在目录排除名单里（字符串相等，指名道姓）→ 直接拒绝并指引
    先清排除，而不是等到 apply 才失败——确认码不应该花在注定无效的提案上。
    文件名/格式类规则不在此列：点具体文件属个别例外，静默生效）。整体通过
    或整体拒绝（有一条非法就整体报错，不做"部分生效"）。
    """
    blocked = _norm_ex_dir_entries(exclude_dirs)
    norm = []
    for ch in (changes or ()):
        if not isinstance(ch, dict):
            raise ValueError(f"变更项必须是字典：{ch!r}")
        action = ch.get("action")
        if action not in SELECTION_ACTIONS:
            raise ValueError(f"非法 action：{action!r}（只接受 {'/'.join(SELECTION_ACTIONS)}）")
        path = norm_selection_path(ch.get("path"))
        if action == "in" and path in blocked:
            raise ValueError(
                f"同位置矛盾已拒绝：{path} 本身就在目录排除名单（exclude_dirs）里，"
                "纳入不会生效。请先用库配置去掉 exclude_dirs 中的这一项（仅本库生效即可，"
                "不影响全局与其他库），再重新提案；或改勾它下面的具体文件（个别例外直接生效）。"
            )
        norm.append({"path": path, "action": action})
    if not norm:
        raise ValueError("changes 为空：至少提供一项 {path, action}")
    return norm


def apply_selection_changes(
    selection_in: Sequence[str], selection_out: Sequence[str], changes: Sequence[dict]
) -> tuple[list[str], list[str]]:
    """按顺序应用一批已规范化的勾选变更，返回新的 (selection_in,
    selection_out)——同一路径出现多条变更时后写覆盖先写，对齐
    obsidian-rag `library.py::set_selection` 的合并语义。"""
    sin = list(selection_in)
    sout = list(selection_out)
    for ch in changes:
        path, action = ch["path"], ch["action"]
        if path in sin:
            sin.remove(path)
        if path in sout:
            sout.remove(path)
        if action == "in":
            sin.append(path)
        elif action == "out":
            sout.append(path)
        # action == "neutral"：已经从两份名单里都移除，不需要再做什么
    return sin, sout


# ---------------------------------------------------------------------------
# 格式开关的批量勾选语义（obsidian-rag 问题44 用户拍板③：全局格式开关 =
# 对该格式文件的批量勾/取消，**无保护概念**——显式勾选的某份也跟着取消）。
# 纯函数，配置写入的收口在 config.py::set_policy（唯一收口，GUI/CLI/MCP
# 都只能改那一个公开方法）。
# ---------------------------------------------------------------------------


def _ext_suffixes(exts: Sequence[str] | None) -> tuple[str, ...]:
    out: list[str] = []
    for value in exts or ():
        s = str(value).strip().lower().lstrip(".")
        if s and f".{s}" not in out:
            out.append(f".{s}")
    return tuple(out)


def format_selection_bulk(
    exts: Sequence[str] | None,
    enabled: bool,
    selection_in: Sequence[str] | None,
    selection_out: Sequence[str] | None,
) -> tuple[list[str], list[str], int]:
    """格式批量语义，返回 (新 selection_in, 新 selection_out, 受影响条目数)
    ——对齐 obsidian-rag/library.py:290-323 format_selection_bulk。

    - `enabled=False`（关格式）：selection_in 里扩展名命中 exts 的条目移入
      selection_out（用户显式勾选的该格式文件跟着取消）；
    - `enabled=True`（开格式）：selection_out 里扩展名命中 exts 的条目移除
      （恢复"跟随"=纳入）。

    **文件夹级条目永不被批量触碰**，而"文件级"是按扩展名判定的——不能以
    "路径里有没有 /"来区分：子目录下的文件同样含斜杠（这条坑旧项目在
    TASK_LOG.md 问题44 里明确记过"文件级按扩展名判定——子目录文件也有斜杠，
    不能以'/'有无区分文件/文件夹"，这里照抄不重犯；文件夹条目极少以 .pdf
    之类结尾，误伤面可忽略，与旧项目一致）。

    与旧项目的一处刻意差异：不排序。旧实现落盘前 sorted()，REDO 的
    set_selection / apply_selection_changes 一律保持插入顺序；裁决侧本来就是
    建集合比对，顺序不可观测，统一成"保持原相对顺序"更自洽。
    """
    suffixes = _ext_suffixes(exts)

    def _file_fmt(path: str) -> bool:
        return bool(suffixes) and path.lower().endswith(suffixes)

    sin = [str(p) for p in (selection_in or ())]
    sout = [str(p) for p in (selection_out or ())]
    if enabled:
        kept = [p for p in sout if not _file_fmt(p)]
        return sin, kept, len(sout) - len(kept)
    moved = [p for p in sin if _file_fmt(p)]
    kept_in = [p for p in sin if not _file_fmt(p)]
    kept_out = list(sout)
    for path in moved:
        if path not in kept_out:
            kept_out.append(path)
    return kept_in, kept_out, len(moved)


def bulk_for_extensions(
    old_exts: Sequence[str] | None,
    new_exts: Sequence[str] | None,
    selection_in: Sequence[str] | None,
    selection_out: Sequence[str] | None,
) -> tuple[list[str], list[str], int]:
    """extensions 变更 → 批量勾选语义的收口（对齐 obsidian-rag/library.py
    326-339 bulk_for_extensions）。被移除的格式跟着取消勾选，新增的格式把
    selection_out 里对应的文件条目摘掉。"""
    old = set(_ext_suffixes(old_exts))
    new = set(_ext_suffixes(new_exts))
    sin = [str(p) for p in (selection_in or ())]
    sout = [str(p) for p in (selection_out or ())]
    total = 0
    for ext in sorted(old - new):
        sin, sout, moved = format_selection_bulk([ext], False, sin, sout)
        total += moved
    for ext in sorted(new - old):
        sin, sout, moved = format_selection_bulk([ext], True, sin, sout)
        total += moved
    return sin, sout, total
