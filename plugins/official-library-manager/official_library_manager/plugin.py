"""official-library-manager 插件：多库/路径级勾选管理。

裁决算法在 selection.py（decide_included/collect_included_files，唯一
权威实现），配置持久化在 config.py。本文件是生命周期钩子的薄封装+对外
方法入口。
"""
from __future__ import annotations

import os
from pathlib import Path

from core.write_gate import WriteGateError

from .config import (
    AGENT_BINARY_FORMATS as AGENT_BINARY_EXTENSIONS,
    AGENT_TEXT_FORMATS as AGENT_TEXT_EXTENSIONS,
    LibraryConfig,
    LibraryConfigStore,
    config_defaults,
    norm_extension,
    norm_extension_list,
)
from .selection import (SUPPORTED_EXTS, apply_selection_changes, collect_included_files,
                        decide_included, dir_self_blocked, explicit_verdict,
                        format_selection_bulk, norm_selection_path, normalize_selection_changes,
                        selection_explicit)

#: 库配置弹层（旧 guiweb 契约的 get/set/unset_library_config）用旧项目的键名：
#: `extensions` 不带点、`agent_formats` 只收二进制子集。下面两组是 rag-redo 里
#: 真实支持按库配置的键，与不支持的键（切块粒度与 collection 名在 rag-redo 里
#: 是全局固定值，没有"按库覆盖"这个能力）。
GUI_CONFIG_KEYS = ("extensions", "agent_formats", "exclude_dirs", "exclude_files", "exclude_patterns")
UNSUPPORTED_CONFIG_KEYS = ("chunk_char_limit", "short_doc_char_limit", "collection")
_CONFIG_KEY_TO_FIELD = {
    "extensions": "enabled_extensions",
    "agent_formats": "agent_formats",
    "exclude_dirs": "exclude_dirs",
    "exclude_files": "exclude_files",
    "exclude_patterns": "exclude_patterns",
}


def rel_dir_join(parent: str, name: str) -> str:
    """库内相对路径拼接（正斜杠分隔，位于库根时返回裸名字）。"""
    return f"{parent}/{name}" if parent else name


def _exc_text(exc: BaseException) -> str:
    """异常转人话：`KeyError` 的 `str()` 会带一层引号，取它的第一个参数。"""
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def _split_list(value: object) -> list[str]:
    """GUI 传来的列表值：逗号分隔字符串（旧契约）或已经是列表。"""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


class LibraryManagerPlugin:
    def __init__(self) -> None:
        self.store: LibraryConfigStore | None = None
        self._settings = None
        self._write_gate = None
        self._logger = None

    def on_load(self, ctx):
        self.store = LibraryConfigStore(
            ctx.storage.file("libraries.json", legacy="libraries.json"),
            logger=ctx.logger,
        )
        self._settings = ctx.settings
        self._write_gate = ctx.write_gate
        self._logger = ctx.logger
        ctx.logger.info("library-manager 已加载，%d 个库", len(self.store.list_libraries()))

    def on_enable(self, ctx):
        ctx.logger.info("library-manager 已启用")

    def on_disable(self, ctx):
        ctx.logger.info("library-manager 已禁用")

    def on_unload(self, ctx):
        self.store = None
        self._settings = None
        self._write_gate = None
        self._logger = None

    def resolve_libraries(self, libraries: str = "", exclude: str = "") -> list[LibraryConfig]:
        """按"白名单减法"解析出这次检索该覆盖哪些库——对齐 obsidian-rag/
        library.py::resolve_entries 的多库选择语义（旧项目 GUI 的库勾选树、
        `search_knowledge` MCP 工具的 `libraries`/`exclude` 参数最终都调
        这一个函数）：`libraries` 为空 = 先查 `default_libraries` 设置
        （见下），配置也是空才回退全部已注册库；`"all"` 显式等同于"忽略
        default_libraries、就是要全部库"；`"A,B"` 按逗号切开多库并查
        （去重但保留顺序）；`exclude` 做减法，在解析结果之上再排除；
        两边出现未知库id直接报错并把全部可用库名列出来，不静默忽略
        打错的名字。

        **`default_libraries` 设置（2026-09-23 接入 core/settings.py 通用
        设置存储后补齐，此前是已知的、如实记录的简化）**：对齐
        obsidian-rag/config.py 的 `default_libraries` 项——用户可以设定
        "新增库/非笔记库默认不参与检索"，`libraries` 参数留空时优先用这份
        默认范围，而不是不由分说地查全部库；配置里的库id如果已经被删除，
        静默跳过（不算入未知库名报错），配置项全部失效或本来就没配才
        回退全部库——同 obsidian-rag `resolve_entries` 的"默认库全部失效
        →回退全部库（旧行为）"语义。
        """
        assert self.store is not None
        entries = self.store.list_libraries()
        by_id = {cfg.library_id: cfg for cfg in entries}
        if not by_id:
            raise ValueError("当前没有已注册库，请先创建一个库。")
        if libraries:
            names = list(dict.fromkeys(n.strip() for n in libraries.split(",") if n.strip()))
            if names and names[0].lower() == "all":
                names = list(by_id)
        else:
            defaults = self._settings.get("default_libraries", []) if self._settings is not None else []
            names = [n for n in defaults if n in by_id]
            if not names:
                names = list(by_id)
        ex = [n.strip() for n in exclude.split(",") if n.strip()]
        unknown = sorted({n for n in names + ex if n not in by_id})
        if unknown:
            raise ValueError(f"未知库id：{', '.join(unknown)}。可用库：{', '.join(sorted(by_id))}")
        final = [n for n in names if n not in ex]
        if not final:
            raise ValueError(
                f"所选范围为空：exclude 覆盖了全部指定库（libraries={libraries or '全部'}，"
                f"exclude={exclude or '无'}）。可用库：{', '.join(sorted(by_id))}"
            )
        return [by_id[n] for n in final]

    def pending_agent_formats(self, library_id: str) -> dict[str, int]:
        """已启用但未对 Agent 授权、且磁盘上确实存在文件的二进制格式 →
        {格式: 文件数}——对齐 obsidian-rag/server.py:120-137 _pending_formats，
        供 reindex_knowledge(allow_new_formats=true) 的授权流使用。

        **只数"按漏斗会被纳入"的文件**（逐字对齐旧代码走
        collect_md_files(..., [fmt], selection=...) 而不是裸 glob）：被
        exclude_dirs / selection_out / exclude_files 排除掉的 .pdf 原本就
        不会进索引，把它们算进"待授权"只会让横幅长期挂着一个用户授权了也
        不可能有变化的数字——那是"授权了但没生效"的静默不一致，比不提示更
        糟。这里复用 resolve_included_files 这个唯一漏斗（AGENTS.md §5），
        不另写一遍遍历；判定跑一次，按后缀分桶。
        """
        cfg = self.store.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        allowed = set(self.agent_allowed_extensions(library_id))
        targets = [
            ext for ext in (norm_extension(e) for e in cfg.enabled_extensions)
            if ext and ext not in allowed and ext in AGENT_BINARY_EXTENSIONS
        ]
        pending: dict[str, int] = {}
        if not targets:
            # 没有待授权的二进制格式就别去扫盘了——这个方法每次检索都会被
            # 调一遍，空跑一次全库 rglob 在大库上是白等。
            return pending
        decisions = self.resolve_included_files(library_id)
        for ext in targets:
            count = sum(
                1 for path, included, _reason in decisions
                if included and path.lower().endswith(ext)
            )
            if count:
                pending[ext] = count
        return pending

    def agent_allowed_extensions(self, library_id: str) -> tuple[str, ...]:
        """Agent 可处理的后缀集合 = 文本类恒可 ∪ 已批准**且当前启用**的
        二进制格式。

        与 enabled_extensions 取交集是逐字对齐 obsidian-rag/library.py:473
        `agent_formats = [f for f in exts if f in approved]`（那里的注释写明
        "用户在 extensions 里取消某格式时，授权自动随之失效——单一事实来源
        是 extensions"）。少了这一步，配合中性默认 include（该态会穿透格式
        白名单，见 decide_included），用户在格式开关里关掉的 .pdf 只要历史
        上授权过，Agent 触发的索引照样会把它收进去。文本类不受 extensions
        约束（同旧 _agent_allowed：TEXT_EXTS 恒在）。
        """
        cfg = self.store.get(library_id) if self.store is not None else None
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        enabled = {norm_extension(e) for e in cfg.enabled_extensions}
        allowed: set[str] = set(AGENT_TEXT_EXTENSIONS)
        allowed.update(
            norm_extension(extension)
            for extension in cfg.agent_formats
            if norm_extension(extension) in AGENT_BINARY_EXTENSIONS
            and norm_extension(extension) in enabled
        )
        return tuple(sorted(allowed))

    def resolve_included_files(
        self,
        library_id: str,
        *,
        format_allowlist: tuple[str, ...] | None = None,
    ) -> list[tuple[str, bool, str]]:
        """返回某个库里全部文件的裁决结果（含未纳入的，附原因）——给"文件
        生效明细"这类界面直接用，不用重新扫一遍。"""
        assert self.store is not None
        cfg = self.store.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        all_paths = self._enumerate_files(cfg.root_path)
        decisions = collect_included_files(
            all_paths,
            selection_in=cfg.selection_in,
            selection_out=cfg.selection_out,
            new_file_default=cfg.new_file_default,  # type: ignore[arg-type]
            enabled_extensions=cfg.enabled_extensions,
            exclude_dirs=cfg.exclude_dirs,
            exclude_files=cfg.exclude_files,
            exclude_patterns=cfg.exclude_patterns,
        )
        if format_allowlist is None:
            return decisions
        allowed = {str(value).strip().lower() for value in format_allowlist}
        result: list[tuple[str, bool, str]] = []
        for path, included, reason in decisions:
            extension = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if extension not in allowed:
                result.append((path, False, f"Agent 未授权格式 {extension or '(无后缀)'}"))
            else:
                result.append((path, included, reason))
        return result

    @staticmethod
    def _enumerate_files(root_path: str) -> list[str]:
        """库目录下全部文件的相对路径（正斜杠、排好序）。

        直接用 `os.scandir`：目录项自带“是文件还是目录”，不用再对每个路径单独问一次系统。
        2026-10-01 真机 Obsidian Vault 6,076 个文件：此前 `rglob` + 逐个 `is_file()` 要
        0.43～0.8 秒，这样 0.07 秒；每轮索引和每次搜索前的同步检查都要列一遍。结果与
        `rglob("*")` + `is_file()` 逐条一致：不钻进指向目录的符号链接（`rglob` 默认也不钻），
        指向文件的符号链接照收，断掉的链接不收，读不了的目录跳过。"""
        root = Path(root_path)
        if not root.exists():
            return []
        found: list[str] = []
        pending: list[tuple[str, str]] = [("", str(root))]
        while pending:
            prefix, directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending.append((f"{prefix}{entry.name}/", entry.path))
                            elif entry.is_file():
                                found.append(prefix + entry.name)
                        except OSError:
                            continue
            except OSError:
                continue
        return sorted(found)

    # ---- 路径级勾选变更（写权限门禁保护，2026-09-23 全面功能审计B类）------
    #
    # 对齐 obsidian-rag 的 get_selection/propose_selection_changes/
    # apply_selection_changes：判定逻辑（decide_included）早就有了，缺的
    # 是"AI 想改这份配置"这条写路径。与 official-library-summary 的
    # propose()/apply() 不同之处——**这里没有"此前非用户手写就直接生效"
    # 的分支，永远走门禁**：被排除的文件会从整个 RAG 流程里消失（不扫描/
    # 不嵌入/不OCR），风险等级比库简介文本高一截，obsidian-rag 原文档
    # 用词是"硬性确认门禁：本工具绝不直接生效"，逐字照做。

    def get_selection(self, library_id: str) -> dict:
        """只读：查看某库当前的路径级勾选状态。"""
        cfg = self.store.get(library_id) if self.store else None
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        return {"selection_in": list(cfg.selection_in), "selection_out": list(cfg.selection_out)}

    def propose_selection_changes(self, library_id: str, changes: list[dict]) -> dict:
        assert self.store is not None and self._write_gate is not None
        cfg = self.store.get(library_id)
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        norm = normalize_selection_changes(changes, exclude_dirs=cfg.exclude_dirs)  # 非法项直接抛异常，整体拒绝
        ticket = self._write_gate.propose(f"变更库「{library_id}」的路径级勾选", {
            "library_id": library_id,
            "changes": norm,
        })
        self._logger.info(
            "勾选变更提案已生成（库=%s，提案=%s，%d 项）——等待用户确认",
            library_id, ticket.proposal_id, len(norm),
        )
        return {
            "ok": True,
            "proposal_id": ticket.proposal_id,
            "confirmation_code": ticket.confirmation_code,
            "changes": norm,
        }

    def apply_selection_changes(self, library_id: str, proposal_id: str, confirmation_code: str) -> dict:
        assert self.store is not None and self._write_gate is not None
        try:
            payload = self._write_gate.confirm(proposal_id, confirmation_code)
        except WriteGateError as exc:
            self._logger.warning(
                "AUDIT 勾选变更提案被拒（库=%s，提案=%s）：%s", library_id, proposal_id, exc
            )
            return {"ok": False, "error": str(exc)}
        if payload.get("library_id") != library_id:
            return {"ok": False, "error": "提案与库名不匹配"}
        cfg = self.store.get(library_id)
        if cfg is None:
            return {"ok": False, "error": f"未知库: {library_id}"}
        new_in, new_out = apply_selection_changes(cfg.selection_in, cfg.selection_out, payload["changes"])
        self.store.set_selection(library_id, selection_in=new_in, selection_out=new_out)
        self._logger.info(
            "AUDIT 勾选变更已生效（库=%s，提案=%s，经用户确认，%d 项）",
            library_id, proposal_id, len(payload["changes"]),
        )
        return {"ok": True, "selection_in": new_in, "selection_out": new_out}

    # ==================================================================
    # 勾选树只读模型（BC-15 阶段C）：GUI 左栏目录树的权威数据源
    # ==================================================================
    # 放在 library-manager 而不是 GUI 层，是因为**裁决逻辑只能有一份**：
    # 下面每个节点的 state/explicit/state_text 全部由 selection.py 的
    # decide_included（问题47 的唯一权威实现）算出来，GUI 只负责画。GUI
    # 自己再判一遍就会出现"树上显示入库、索引时其实没收录"这种撕裂。

    def selection_tree(self, library_id: str, sub: str = "") -> dict:
        """勾选树（旧 guiweb/bridge.py::selection_tree 的逐项移植）：一次返回
        **全库目录树**（扁平 + depth，左栏）与**某子目录一层**的目录/文件（右栏）。

        节点状态口径与建索引的文件漏斗完全一致（`explicit_verdict` /
        `decide_included`，谁具体听谁的）：

        - `explicit`：`in` / `out` / None（显式勾选态）；
        - `state`：`in` / `out`（显式或被排除名单命中）| `auto_in` / `auto_out`
          （中性，按格式开关与中性默认判定）| `root`；
        - `self_blocked`：仅目录，本身就躺在 exclude_dirs 里 → 点击即弹窗，不等保存。

        与旧项目一致的三个要点（此前的重写版在这里偏离过）：
        ①**文件夹是容器**——中性时跟随子内容（`auto_in`），文件夹没有扩展名，
        不能落进格式判定，否则整棵树全显示"排除"；②**被排除的目录照样显示**
        （状态标为已排除），用户才有地方点它、才会触发"同位置打架"的一键解决；
        ③**所有文件都列出**（含未启用格式的），未启用的标为 `auto_out`，不是
        直接消失。`sub` 越出库根 / 非法 / 不是目录一律返回 `error`。
        """
        cfg = self.store.get(library_id) if self.store else None
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        name = cfg.name
        sin, sout = list(cfg.selection_in), list(cfg.selection_out)
        ex_dirs = list(cfg.exclude_dirs)
        ex_files = set(cfg.exclude_files)
        ex_pats = tuple(str(p) for p in cfg.exclude_patterns)
        default = cfg.new_file_default
        kwargs = dict(
            selection_in=sin,
            selection_out=sout,
            new_file_default=default,
            enabled_extensions=cfg.enabled_extensions,
            exclude_dirs=ex_dirs,
            exclude_files=cfg.exclude_files,
            exclude_patterns=cfg.exclude_patterns,
        )
        extensions = [str(e).lstrip(".") for e in cfg.enabled_extensions]

        def _result(root_text: str, sub_n: str, folders: list, dirs: list, files: list,
                    error: str | None) -> dict:
            return {
                "lib": name,
                "sub": sub_n,
                "root": root_text,
                "dirs": dirs,
                "files": files,
                "folders": folders,
                "selection_in": sin,
                "selection_out": sout,
                "extensions": extensions,
                "default": default,
                "error": error,
            }

        try:
            root = Path(cfg.root_path).resolve()
        except OSError as exc:
            return _result(cfg.root_path, "", [], [], [], f"库路径无法解析：{exc}")
        if not root.is_dir():
            return _result(str(root), "", [], [], [], f"库路径不存在：{root}")
        sub_n = ""
        if sub:
            try:
                sub_n = norm_selection_path(sub)
            except ValueError as exc:
                return _result(str(root), "", [], [], [], str(exc))
        try:
            cur = (root / sub_n).resolve() if sub_n else root
        except OSError as exc:
            return _result(str(root), sub_n, [], [], [], f"列目录失败：{exc}")
        if root != cur and root not in cur.parents:
            return _result(str(root), sub_n, [], [], [], "路径越出库范围")
        if not cur.is_dir():
            return _result(str(root), sub_n, [], [], [], f"目录不存在：{sub_n}")

        def _state(rel: str, *, is_dir: bool, node_name: str) -> tuple[str, str | None, str, bool]:
            self_blocked = is_dir and dir_self_blocked(ex_dirs, rel)
            verdict, tie, _reason = explicit_verdict(sin, sout, ex_dirs, rel)
            if tie:
                # 同位置打架的手工态：排除站住，但显式记录照实显示 + 消歧
                return "out", "in", "被排除名单挡住（显式勾选已保存但未生效）", self_blocked
            if verdict == "out":
                if selection_explicit(sin, sout, rel) == "out":
                    return "out", "out", "已排除（显式取消）", self_blocked
                return "out", None, "已排除（排除名单）", self_blocked
            if verdict == "in":
                return "in", "in", "已入库（显式勾选）", self_blocked
            # 中性：文件名类排除仍生效；目录容器跟随子内容；文件走格式/中性默认
            if not is_dir and (node_name in ex_files or node_name.startswith(ex_pats)):
                return "out", None, "已排除（排除名单）", False
            if is_dir:
                included = True
            else:
                # 与建索引的文件漏斗同一个函数——显示=实际
                included, _ = decide_included(rel, **kwargs)
            if included:
                return "auto_in", None, ("入库（跟随子内容）" if is_dir else "入库（跟随格式）"), False
            return "auto_out", None, "排除（跟随格式）", False

        dirs: list[dict] = []
        files: list[dict] = []
        try:
            entries = sorted(cur.iterdir(), key=lambda x: x.name.lower())
        except OSError as exc:
            return _result(str(root), sub_n, [], [], [], f"列目录失败：{exc}")
        for item in entries:
            try:
                is_dir = item.is_dir()
            except OSError:
                continue
            if item.name.startswith(".") and is_dir:
                continue  # 隐藏目录（.obsidian 等）不进面板
            rel = rel_dir_join(sub_n, item.name)
            if is_dir:
                st, ex, tx, sbl = _state(rel, is_dir=True, node_name=item.name)
                try:
                    n_children = sum(1 for _ in item.iterdir())
                except OSError:
                    n_children = 0
                dirs.append({
                    "name": item.name, "dir": True, "path": rel, "explicit": ex,
                    "state": st, "state_text": tx, "self_blocked": sbl,
                    "n_children": n_children,
                })
            else:
                st, ex, tx, _sbl = _state(rel, is_dir=False, node_name=item.name)
                files.append({
                    "name": item.name, "dir": False, "path": rel, "explicit": ex,
                    "state": st, "state_text": tx, "self_blocked": False,
                    "ext": item.suffix.lower().lstrip("."),
                })

        folders: list[dict] = [{
            "path": "", "depth": 0, "name": name, "explicit": None,
            "state": "root", "state_text": "库根",
        }]

        def _walk_dirs(base: Path, depth: int) -> None:
            try:
                children = sorted(base.iterdir(), key=lambda x: x.name.lower())
            except OSError:
                return
            for item in children:
                if item.name.startswith("."):
                    continue
                try:
                    if not item.is_dir():
                        continue
                except OSError:
                    continue
                rel = str(item.relative_to(root)).replace("\\", "/")
                st, ex, tx, sbl = _state(rel, is_dir=True, node_name=item.name)
                folders.append({
                    "path": rel, "depth": depth, "name": item.name, "explicit": ex,
                    "state": st, "state_text": tx, "self_blocked": sbl,
                })
                if len(folders) < 4000:  # 旧 contracts.md 约定的目录树上限
                    _walk_dirs(item, depth + 1)

        _walk_dirs(root, 1)
        return _result(str(root), sub_n, folders, dirs, files, None)

    def apply_selection_direct(self, library_id: str, changes: list[dict]) -> dict:
        """GUI 勾选写路径：**直接生效，不过写权限门禁**——门禁保护的是
        "AI 经 MCP 想改你的配置"，而 GUI 的操作者就是用户本人，让他向自己
        确认没有意义（对齐旧 guiweb/bridge.py::selection_update 的同一判断）。
        校验与合并仍然走 normalize/apply 两条唯一实现，同位置矛盾照样拒绝。
        """
        cfg = self.store.get(library_id) if self.store else None
        if cfg is None:
            return {"ok": False, "error": f"未知库: {library_id}",
                    "selection_in": [], "selection_out": []}
        try:
            norm = normalize_selection_changes(changes, exclude_dirs=cfg.exclude_dirs)
        except ValueError as exc:
            return {"ok": False, "error": str(exc),
                    "selection_in": list(cfg.selection_in),
                    "selection_out": list(cfg.selection_out)}
        new_in, new_out = apply_selection_changes(cfg.selection_in, cfg.selection_out, norm)
        try:
            self.store.set_selection(library_id, selection_in=new_in, selection_out=new_out)
        except ValueError as exc:  # 越界/同位置矛盾：拒绝并把库里现状原样带回
            return {"ok": False, "error": str(exc),
                    "selection_in": list(cfg.selection_in),
                    "selection_out": list(cfg.selection_out)}
        return {"ok": True, "error": None, "selection_in": new_in, "selection_out": new_out}

    def resolve_selection_conflict(self, library_id: str, path: str) -> dict:
        """同位置矛盾一键解决（问题47）：只在本库 exclude_dirs 移除该项并
        纳入勾选；全局与其他库一律不动。目录本身不在排除名单时返回
        `{ok: False}`（直接勾选即可，不需要本方法）。"""
        cfg = self.store.get(library_id) if self.store else None
        if cfg is None:
            return {"ok": False, "error": f"未知库: {library_id}",
                    "selection_in": [], "selection_out": []}
        try:
            target = norm_selection_path(path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc),
                    "selection_in": list(cfg.selection_in),
                    "selection_out": list(cfg.selection_out)}
        blocked = {
            str(e).replace("\\", "/").strip("/")
            for e in cfg.exclude_dirs
            if str(e).strip("/")
        }
        if target not in blocked:
            # 文案逐字取自旧 guiweb/bridge.py::selection_resolve_conflict
            return {"ok": False, "error": "该目录已不在排除名单里，直接勾选即可",
                    "selection_in": list(cfg.selection_in),
                    "selection_out": list(cfg.selection_out)}
        new_dirs = [e for e in cfg.exclude_dirs
                    if str(e).replace("\\", "/").strip("/") != target]
        self.store.set_policy(library_id, exclude_dirs=new_dirs)
        cfg = self.store.get(library_id)
        new_in, new_out = apply_selection_changes(
            cfg.selection_in, cfg.selection_out, [{"path": target, "action": "in"}]
        )
        self.store.set_selection(library_id, selection_in=new_in, selection_out=new_out)
        return {"ok": True, "error": None, "selection_in": new_in, "selection_out": new_out}

    def format_bulk(self, library_id: str, ext: str, include: bool) -> dict:
        """格式快捷批量勾选（契约 `selection_format_bulk`）。语义与收口都在
        selection.py::format_selection_bulk，这里只做落盘。"""
        cfg = self.store.get(library_id) if self.store else None
        if cfg is None:
            return {"ok": False, "changed": 0, "error": f"未知库: {library_id}"}
        new_in, new_out, changed = format_selection_bulk(
            [ext], include, cfg.selection_in, cfg.selection_out
        )
        if changed:
            self.store.set_selection(library_id, selection_in=new_in, selection_out=new_out)
        return {"ok": True, "changed": changed, "error": None}

    # ==================================================================
    # 库配置的 GUI 契约视图（旧 library.py::effective_config / set_config /
    # unset_config 的对应物）。键名沿用旧项目词汇（extensions 不带点、
    # agent_formats 只收二进制子集），翻译与校验都收在这一层——桥接层只转发。
    # ==================================================================

    def _require(self, library_id: str) -> LibraryConfig:
        cfg = self.store.get(library_id) if self.store else None
        if cfg is None:
            raise KeyError(f"未知库: {library_id}")
        return cfg

    def config_view(self, library_id: str) -> dict:
        """`{effective, overrides, all_keys}`：`effective` 是这个库现在生效的值，
        `overrides` 只含**偏离出厂默认**的键（旧语义"有 = 显式覆盖；没有 =
        继承默认"）。`all_keys` 只列 rag-redo 真实支持按库配置的键。"""
        cfg = self._require(library_id)
        enabled = {norm_extension(e) for e in cfg.enabled_extensions}
        effective = {
            "extensions": [str(e).lstrip(".") for e in cfg.enabled_extensions],
            # 与 enabled_extensions 取交集：格式被关掉，授权随之失效（旧 effective_config）
            "agent_formats": [
                str(e).lstrip(".") for e in cfg.agent_formats if norm_extension(e) in enabled
            ],
            "exclude_dirs": list(cfg.exclude_dirs),
            "exclude_files": list(cfg.exclude_files),
            "exclude_patterns": list(cfg.exclude_patterns),
        }
        defaults = config_defaults()
        overrides: dict = {}
        for key, field_name in _CONFIG_KEY_TO_FIELD.items():
            default = defaults[field_name]
            if key in ("extensions", "agent_formats"):
                default = [str(e).lstrip(".") for e in default]
            if effective[key] != default:
                overrides[key] = list(effective[key])
        return {"effective": effective, "overrides": overrides, "all_keys": list(GUI_CONFIG_KEYS)}

    def _set_one_config(self, library_id: str, key: str, value: object) -> None:
        if key in UNSUPPORTED_CONFIG_KEYS:
            raise ValueError(
                f"rag-redo 暂不支持按库覆盖 {key}（切块粒度与向量集合名目前是全局固定值，"
                "不随库变化）"
            )
        if key not in GUI_CONFIG_KEYS:
            raise ValueError(f"非法配置键：{key}（合法：{', '.join(GUI_CONFIG_KEYS)}）")
        parsed = _split_list(value)
        if key == "extensions":
            normalized = [e.lstrip(".") for e in norm_extension_list(parsed)]
            bad = [e for e in normalized if e not in SUPPORTED_EXTS]
            if bad:
                raise ValueError(
                    f"不支持的扩展名：{', '.join(bad)}"
                    f"（当前支持：{', '.join(sorted(SUPPORTED_EXTS))}）"
                )
            if not normalized:
                raise ValueError("extensions 不能为空")
            self.store.set_policy(library_id, enabled_extensions=normalized)
        elif key == "agent_formats":
            self.store.set_agent_formats(library_id, parsed)
        else:
            self.store.set_policy(library_id, **{_CONFIG_KEY_TO_FIELD[key]: parsed})

    def unset_config(self, library_id: str, keys: list[str]) -> None:
        """把指定配置键恢复为出厂默认。非法键抛 `ValueError`、未知库抛
        `KeyError`（旧 `library.unset_config` 同样抛，逐键吞异常是桥接层的事）。
        切块粒度 / collection 在 rag-redo 里永远是"继承"，没有覆盖可清，直接跳过。"""
        self._require(library_id)
        defaults = config_defaults()
        for key in keys or ():
            if key in UNSUPPORTED_CONFIG_KEYS:
                continue
            if key not in GUI_CONFIG_KEYS:
                raise ValueError(f"非法配置键：{key}（合法：{', '.join(GUI_CONFIG_KEYS)}）")
            if key == "agent_formats":
                self.store.set_agent_formats(library_id, [])
            else:
                field_name = _CONFIG_KEY_TO_FIELD[key]
                self.store.set_policy(library_id, **{field_name: list(defaults[field_name])})

    def set_config(self, library_id: str, updates: dict) -> dict[str, str]:
        """逐键写库配置，返回 `{键: 错误文案}`（空 = 全部成功）。空白字符串 =
        恢复默认（旧契约"空字符串 = 恢复继承"）。逐键各自生效——某个键校验
        失败不回滚前面已写的键（旧 `guiweb` 桥同一行为），错误按键返回。"""
        self._require(library_id)
        errors: dict[str, str] = {}
        for key, value in (updates or {}).items():
            try:
                if isinstance(value, str) and not value.strip():
                    self.unset_config(library_id, [key])
                else:
                    self._set_one_config(library_id, str(key), value)
            except Exception as exc:  # noqa: BLE001 - 逐键收集，不让一个键拖垮整批
                errors[str(key)] = _exc_text(exc)
        return errors
