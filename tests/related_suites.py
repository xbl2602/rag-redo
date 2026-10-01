"""按改动的文件挑出相关的测试套件（`tests/run.py --changed` 用）。

为什么要有：全量回归一遍 7～10 分钟，改代码的过程中每改一处都等这么久太浪费（2026-10-01
操作者确认：开发中途只跑相关的几组，**提交前仍跑全量**，AGENTS.md §12/§13 不变）。

怎么挑——宁可多挑，不能漏挑：
- 规范门禁（`tests/test_agents_contract.py`）每次都跑（AGENTS.md §9“统一入口必须包含规范门禁”）。
- Python 文件按 import 关系建依赖图（含相对导入；字符串里写的模块名也算，
  `importlib.import_module("x.y")` 这类动态导入就是这么接上的），改了一个文件，所有直接或
  间接依赖它的测试套件都挑出来。
- 插件按 id 加载、不靠 import：哪个文件的字符串里出现了插件 id（`"official-chunker"`、
  `plugins/official-visual-wemm/...` 这类路径），就算依赖这个插件的全部文件；插件目录里
  任何文件（`plugin.toml`、前端资产……）改了，依赖这个插件的套件和插件自带的套件都要跑。
- 测试目录里的非 Python 文件（fixture）改了：同目录里提到它文件名的套件；没人提到就整个
  目录的套件都跑。
- 其他顶层目录（`examples/`、`demo-vault/`、`installer/`……）里的文件改了：源码字符串里
  提到这个目录名的套件。
- `.md` 文档、`docs/` 下的文件、仓库根目录下的非 Python 文件只跑门禁（行为契约和
  AGENTS/CLAUDE 同步正是门禁查的东西）。
- 删掉的 Python 文件按它原来的模块名找还在 import 它的文件。

静态分析看不见的依赖（比如测试用 glob 扫全部插件目录）可能漏挑，所以这个模式只用于开发中途
快速反馈，提交前必须跑全量。"""
from __future__ import annotations

import ast
import re
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

#: 每次都跑的规范门禁。
GATE_SUITE = "tests/test_agents_contract.py"

#: 不进依赖图的目录：虚拟环境、模型、数据、构建产物和缓存。插件目录下的 `.venv` 有几万个文件，
#: 扫进来既慢又毫无意义。
_SKIP_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules", "build", "dist",
    "data", "data-real", "models", ".claude",
}

#: 这些顶层目录里的文件按各自的规则处理，不走“提到目录名”那条兜底规则。
_KNOWN_TOP_DIRS = {"core", "plugins", "tests", "tools", "docs"}

_DASH_TOKEN = re.compile(r"[A-Za-z0-9_\-]+")
_DOTTED_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


@dataclass
class _FileInfo:
    rel: str
    imports: set[str] = field(default_factory=set)
    dash_tokens: set[str] = field(default_factory=set)
    dotted_tokens: set[str] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Selection:
    """挑选结果：要跑的套件（仓库相对路径，正斜杠）和给人看的说明。"""

    suites: tuple[str, ...]
    notes: tuple[str, ...]


def _rel(path: Path, repo: Path) -> str:
    return path.relative_to(repo).as_posix()


def _iter_python_files(repo: Path):
    stack = [repo]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix == ".py":
                yield entry


def _module_name(rel: str, repo: Path) -> tuple[str | None, str | None]:
    """（全局点分模块名，只在本目录可见的裸模块名）。

    `core/x.py` → `core.x`；插件包 `plugins/<id>/<pkg>/x.py` → `<pkg>.x`（运行时和测试都把插件
    目录放进 sys.path）；`examples/<d>/<pkg>/x.py` 同理；其余（tests/、tools/、根目录脚本、
    插件 tests/ 目录）是裸模块名，只在同目录或 tests/、tools/、根目录里按名字找。"""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None, None
    if parts[0] == "core":
        return ".".join(parts), None
    if parts[0] in ("plugins", "examples") and len(parts) >= 3 and parts[2] != "tests":
        package_dir = repo / parts[0] / parts[1] / parts[2]
        if package_dir.is_dir() and (package_dir / "__init__.py").exists():
            return ".".join(parts[2:]), None
    return None, parts[-1]


def _package_of(rel: str, repo: Path) -> list[str] | None:
    """相对导入用：这个文件所在包的点分路径（拿不到就 None）。"""
    dotted, _ = _module_name(rel, repo)
    if dotted is None:
        return None
    parts = dotted.split(".")
    return parts if rel.endswith("__init__.py") else parts[:-1]


def _collect(path: Path, rel: str, repo: Path) -> _FileInfo:
    info = _FileInfo(rel)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return info
    package = _package_of(rel, repo)
    # 文档字符串是给人看的说明，里面顺嘴提到的模块名、插件 id 不是依赖——算进来会把
    # 半个仓库都连成一片，“只跑相关的”就退化成全量。
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                info.imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if package is None:
                    continue
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else list(package)
                if node.module:
                    base = [*base, *node.module.split(".")]
            else:
                base = node.module.split(".") if node.module else []
            if not base:
                continue
            info.imports.add(".".join(base))
            for alias in node.names:
                info.imports.add(".".join([*base, alias.name]))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            text = node.value
            info.texts.append(text)
            info.dash_tokens.update(_DASH_TOKEN.findall(text.replace("\\", "/")))
            info.dotted_tokens.update(_DOTTED_TOKEN.findall(text))
    # `import a.b.c` 同时执行了 a、a.b 的 __init__：父包也算依赖。
    for name in list(info.imports):
        pieces = name.split(".")
        for i in range(1, len(pieces)):
            info.imports.add(".".join(pieces[:i]))
    return info


class DependencyGraph:
    """整个仓库 Python 文件的依赖图，外加“插件”“顶层目录”两类虚拟节点。"""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.infos: dict[str, _FileInfo] = {}
        self.global_modules: dict[str, str] = {}
        self.local_modules: dict[tuple[str, str], str] = {}
        for path in _iter_python_files(repo):
            rel = _rel(path, repo)
            self.infos[rel] = _collect(path, rel, repo)
            dotted, bare = _module_name(rel, repo)
            if dotted is not None:
                self.global_modules[dotted] = rel
            if bare is not None:
                self.local_modules[(rel.rpartition("/")[0], bare)] = rel
        plugins_dir = repo / "plugins"
        self.plugin_ids = {p.name for p in plugins_dir.iterdir() if p.is_dir()} if plugins_dir.is_dir() else set()
        self.top_dirs = {
            p.name for p in repo.iterdir()
            if p.is_dir() and p.name not in _SKIP_DIRS and not p.name.startswith(".")
        }
        self.reverse: dict[str, set[str]] = {}
        for rel, info in self.infos.items():
            for dep in self._dependencies(rel, info):
                if dep != rel:
                    self.reverse.setdefault(dep, set()).add(rel)

    def _resolve(self, name: str, importer_dir: str) -> str | None:
        if name in self.global_modules:
            return self.global_modules[name]
        if "." in name:
            return None
        for directory in (importer_dir, "tests", "tools", ""):
            hit = self.local_modules.get((directory, name))
            if hit is not None:
                return hit
        return None

    def _dependencies(self, rel: str, info: _FileInfo) -> set[str]:
        importer_dir = rel.rpartition("/")[0]
        deps: set[str] = set()
        for name in info.imports:
            hit = self._resolve(name, importer_dir)
            if hit is not None:
                deps.add(hit)
        # 字符串里的点分名：`patch("core.pipeline.Pipeline.x")`、`import_module("official_x.y")`
        # 都是真依赖。取能对上模块的最长前缀。
        # 按文件路径加载的脚本（`REPO_ROOT / "gui_main.py"`、`patch("gui_main.main")`）同理，
        # 用裸模块名在本目录、tests/、tools/、根目录里找——但只认带点的写法：单独一个词
        # （"run"、"test_cli"）太常见，按它连会把半个仓库连到 tests/run.py 上。
        for token in info.dotted_tokens:
            pieces = token.split(".")
            for end in range(len(pieces), 0, -1):
                hit = self.global_modules.get(".".join(pieces[:end]))
                if hit is not None:
                    deps.add(hit)
                    break
            else:
                hit = self._resolve(pieces[0], importer_dir) if len(pieces) > 1 else None
                if hit is not None:
                    deps.add(hit)
        for token in info.dash_tokens:
            if token in self.plugin_ids:
                deps.add(f"plugin:{token}")
            if token in self.top_dirs and token not in _KNOWN_TOP_DIRS:
                deps.add(f"dir:{token}")
        parts = rel.split("/")
        # 插件自带的测试永远依赖自己的插件（哪怕只经运行时按 id 加载、一行 import 都没有）。
        if parts[0] == "plugins" and len(parts) >= 3 and parts[2] == "tests":
            deps.add(f"plugin:{parts[1]}")
        return deps

    def plugin_node_members(self) -> None:
        """插件虚拟节点依赖插件目录里的全部 Python 文件（tests/ 除外）。"""
        for rel in self.infos:
            parts = rel.split("/")
            if parts[0] == "plugins" and len(parts) >= 3 and parts[2] != "tests":
                self.reverse.setdefault(rel, set()).add(f"plugin:{parts[1]}")

    def dependents(self, starts: set[str]) -> set[str]:
        seen = set(starts)
        queue = deque(starts)
        while queue:
            node = queue.popleft()
            for parent in self.reverse.get(node, ()):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        return seen

    def importers_of_module(self, module: str) -> set[str]:
        return {rel for rel, info in self.infos.items() if module in info.imports}


def changed_files_from_git(repo: Path) -> list[str]:
    """工作区相对 HEAD 改过的文件（含未跟踪的新文件、改名前后两个名字）。git 不可用时抛 OSError。"""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(repo), capture_output=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise OSError(f"git status 失败：{type(exc).__name__}") from exc
    entries = result.stdout.decode("utf-8", errors="replace").split("\0")
    files: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        files.append(path)
        if "R" in status or "C" in status:
            if index < len(entries) and entries[index]:
                files.append(entries[index])
            index += 1
    return files


def _expand(repo: Path, paths: list[str]) -> list[str]:
    out: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            path = repo / path
        try:
            rel = path.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            continue  # 仓库外的文件与测试无关
        if path.is_dir():
            out.extend(
                _rel(p, repo) for p in path.rglob("*")
                if p.is_file() and not (set(p.relative_to(repo).parts) & _SKIP_DIRS)
            )
        else:
            out.append(rel)
    return out


def select_suites(repo: Path, all_suites: list[str], changed: list[str]) -> Selection:
    """从 `all_suites`（仓库相对路径，正斜杠）里挑出与 `changed` 相关的那些。"""
    graph = DependencyGraph(repo)
    graph.plugin_node_members()
    suite_set = set(all_suites)
    picked: set[str] = {GATE_SUITE} & suite_set
    notes: list[str] = []
    expanded = _expand(repo, changed)
    for rel in expanded:
        starts = _starts_for(graph, suite_set, repo, rel)
        if rel in suite_set:
            picked.add(rel)
        hits = graph.dependents(starts) & suite_set
        picked |= hits
        if Path(rel).suffix == ".py" and not hits and rel not in suite_set:
            notes.append(f"没有测试覆盖：{rel}")
    if not expanded:
        notes.append("没有找到改动的文件，只跑规范门禁")
    return Selection(tuple(s for s in all_suites if s in picked), tuple(notes))


def _starts_for(graph: DependencyGraph, suite_set: set[str], repo: Path, rel: str) -> set[str]:
    """一个改动文件在依赖图里的起点（空集合 = 只跑门禁）。"""
    parts = rel.split("/")
    if parts[0] in _SKIP_DIRS or parts[0].startswith("."):
        return set()
    if Path(rel).suffix == ".py":
        starts: set[str] = set()
        if rel in graph.infos:
            starts.add(rel)
        else:  # 已删除：按原来的模块名找还在 import 它的文件
            dotted, bare = _module_name(rel, repo)
            for module in filter(None, (dotted, bare)):
                starts |= graph.importers_of_module(module)
        if parts[0] == "plugins" and len(parts) >= 3 and parts[2] != "tests":
            starts.add(f"plugin:{parts[1]}")
        if len(parts) > 1 and parts[0] in graph.top_dirs and parts[0] not in _KNOWN_TOP_DIRS:
            starts.add(f"dir:{parts[0]}")  # examples/ 里的示例插件是按目录加载的，不靠 import
        return starts
    if parts[0] == "plugins" and len(parts) >= 2:
        if len(parts) >= 3 and parts[2] == "tests":
            return _fixture_users(graph, suite_set, "/".join(parts[:3]), parts[-1])
        return {f"plugin:{parts[1]}"}
    if parts[0] == "tests":
        return _fixture_users(graph, suite_set, "tests", parts[-1])
    if parts[0] == "docs" or len(parts) == 1:
        return set()  # 文档、根目录说明文件：只跑门禁
    if parts[0] in graph.top_dirs:
        return {f"dir:{parts[0]}"}
    return set()


def _fixture_users(graph: DependencyGraph, suite_set: set[str], tests_dir: str, basename: str) -> set[str]:
    in_dir = {s for s in suite_set if s.rpartition("/")[0] == tests_dir}
    users = {s for s in in_dir if any(basename in text for text in graph.infos.get(s, _FileInfo(s)).texts)}
    return users or in_dir
