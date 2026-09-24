"""library_manager 插件的核心判定逻辑：一个文件到底算不算在检索范围内。

裁决优先级（针对旧 obsidian-rag 项目问题44/47附记2记录的真实场景，用全新
代码重新设计实现——不是复制旧代码，具体实现细节可能不同，但要解决的问题
一样）：

1. 路径级显式规则（selection_in/selection_out）永远赢格式规则——格式/
   后缀过滤是最弱的一层，显式路径选择可以静默穿透它。
2. 显式规则之间"谁更具体谁赢"：精确文件匹配 > 祖先目录匹配；祖先目录
   匹配里，路径段数越多（越深）越具体。文件点名可以穿透继承来的目录级
   排除，反过来更具体的排除也能压过较浅的目录级纳入。
3. 同一路径被纳入和排除规则以相同具体度同时命中（例如同一条路径字面
   量地同时出现在两份名单里）时，排除赢——这是唯一的平局打破规则，安全
   默认是不纳入。
4. 都没有显式规则命中时，落到 new_file_default 这个库级默认值。本次
   重写只提供 "include"/"exclude" 两态，不提供旧项目文档里提到的"跟随"
   第三态——那需要额外的"父目录本身算不算被主动扫描过"上下文，从现有
   文档描述反推不出精确语义，与其猜错不如先做两态说清楚，需要时再补
   （这是一处已知的、刻意的简化，不是遗漏）。

唯一实现：本模块导出的 decide_included / collect_included_files 是这条
规则的唯一权威实现——GUI 的勾选树展示、建索引时的文件枚举漏斗，都必须调
这两个函数，不能各自维护一份影子逻辑（docs/DATA_FLOW.md 规则4）。
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Literal, Sequence

NewFileDefault = Literal["include", "exclude"]
SELECTION_ACTIONS = ("in", "out", "neutral")


def _segments(path: str) -> tuple[str, ...]:
    return tuple(p for p in path.replace("\\", "/").split("/") if p)


@dataclass(frozen=True)
class _Match:
    rule: str
    specificity: int  # 越大越具体


def _most_specific_match(path_segs: tuple[str, ...], rules: Sequence[str]) -> _Match | None:
    """在 rules 里找和 path 最相关的一条：精确匹配文件本身，或者是 path 的
    祖先目录。"""
    best: _Match | None = None
    for rule in rules:
        rule_segs = _segments(rule)
        if rule_segs == path_segs:
            specificity = len(rule_segs) * 2 + 1  # 精确匹配永远最具体
        elif len(rule_segs) < len(path_segs) and path_segs[: len(rule_segs)] == rule_segs:
            specificity = len(rule_segs) * 2  # 祖先目录匹配
        else:
            continue
        if best is None or specificity > best.specificity:
            best = _Match(rule=rule, specificity=specificity)
    return best


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
    """判定 path 算不算在检索范围内，返回 (included, reason)。"""
    path_segs = _segments(path)
    in_match = _most_specific_match(path_segs, selection_in)
    out_match = _most_specific_match(path_segs, selection_out)
    excluded_dirs = {_segments(value) for value in (exclude_dirs or ())}
    directory_excluded = any(
        rule and len(rule) < len(path_segs) and path_segs[: len(rule)] == rule
        for rule in excluded_dirs
    )
    if in_match is not None and _segments(in_match.rule) in excluded_dirs:
        return False, f"显式纳入目标同时位于排除目录：{in_match.rule}"
    if in_match is None and out_match is None and directory_excluded:
        return False, "命中排除目录"

    if in_match is None and out_match is None:
        included = new_file_default == "include"
        reason = f"没有显式规则命中，按库默认值（{new_file_default}）处理"
    elif out_match is None or (in_match is not None and in_match.specificity > out_match.specificity):
        assert in_match is not None
        included, reason = True, f"显式纳入规则命中：{in_match.rule}"
    elif in_match is None or out_match.specificity > in_match.specificity:
        included, reason = False, f"显式排除规则命中：{out_match.rule}"
    else:
        included, reason = False, f"路径 {path!r} 同时被纳入和排除规则命中，按安全默认排除"

    if in_match is None and out_match is None:
        basename = path.rsplit("/", 1)[-1]
        if basename in set(exclude_files or ()) or any(
            fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern)
            for pattern in (exclude_patterns or ())
        ):
            return False, "命中排除文件规则"

    if included and enabled_extensions is not None:
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in enabled_extensions and in_match is None:
            # 格式规则是最弱的一层：只有当前的"纳入"不是显式路径规则给的、
            # 而是走到默认值这一步才会被格式过滤拦下；显式路径命中静默
            # 穿透格式限制。
            return False, f"格式 {ext or '(无后缀)'} 不在已启用格式列表内"

    return included, reason


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
# 三个 MCP 工具的数据面，2026-09-23 全面功能审计发现的缺口B类）：判定逻辑
# 上面早就有了（decide_included），缺的是"AI 想改这份配置"这条写路径本身
# ——对齐 obsidian-rag library.py::norm_sel_path/set_selection 的校验与
# 合并语义，写权限门禁部分复用 core/write_gate.py（在 plugin.py 里接线，
# 这里只放纯函数）。
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


def normalize_selection_changes(changes: Sequence[dict] | None) -> list[dict]:
    """校验+规范化一批勾选变更：路径合法、action 合法——整体通过或整体
    拒绝（有一条非法就整体报错，不做"部分生效"）。"""
    if not changes:
        raise ValueError("changes 为空：至少提供一项 {path, action}")
    norm = []
    for ch in changes:
        if not isinstance(ch, dict):
            raise ValueError(f"变更项必须是字典：{ch!r}")
        action = ch.get("action")
        if action not in SELECTION_ACTIONS:
            raise ValueError(f"非法 action：{action!r}（只接受 {'/'.join(SELECTION_ACTIONS)}）")
        path = norm_selection_path(ch.get("path"))
        norm.append({"path": path, "action": action})
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
