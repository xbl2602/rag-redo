"""插件清单(plugin.toml)的解析与校验。

字段定义见 ../docs/PLUGIN_SPEC.md 第2节；两边如果不一致，以本文件为准——
AGENTS.md 文首已经写明"类型定义的权威来源最终是代码"。
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ManifestError(Exception):
    """清单缺失、格式错误或缺字段。调用方必须捕获它，绝不能让一个插件的清单
    问题变成核心进程的未处理异常——这是架构红线4"插件失败必须被隔离折叠"
    在发现/校验阶段的体现。"""


@dataclass(frozen=True)
class RuntimeSpec:
    kind: str  # "in_process" | "subprocess_service"
    entry: str | None = None
    command: tuple[str, ...] | None = None
    health_check: str | None = None
    env_bootstrap: str | None = None


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    version: str
    api_version: str
    provides: dict
    requires: dict
    runtime: RuntimeSpec
    permissions: dict
    source_dir: Path


def load_manifest(plugin_dir: Path) -> PluginManifest:
    """读取 <plugin_dir>/plugin.toml。任何问题都折叠成 ManifestError，
    不抛 tomllib/KeyError 等原始异常类型出去。"""
    manifest_path = plugin_dir / "plugin.toml"
    try:
        with manifest_path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError as exc:
        raise ManifestError(f"{plugin_dir}: 缺少 plugin.toml") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{plugin_dir}: plugin.toml 格式错误: {exc}") from exc

    try:
        runtime_raw = raw["runtime"]
        runtime = RuntimeSpec(
            kind=runtime_raw["kind"],
            entry=runtime_raw.get("entry"),
            command=tuple(runtime_raw["command"]) if "command" in runtime_raw else None,
            health_check=runtime_raw.get("health_check"),
            env_bootstrap=runtime_raw.get("env_bootstrap"),
        )
        return PluginManifest(
            id=raw["id"],
            name=raw["name"],
            version=raw["version"],
            api_version=raw["api_version"],
            provides=raw.get("provides", {}),
            requires=raw.get("requires", {}),
            runtime=runtime,
            permissions=raw.get("permissions", {}),
            source_dir=plugin_dir,
        )
    except KeyError as exc:
        raise ManifestError(f"{plugin_dir}: plugin.toml 缺少必填字段 {exc}") from exc


_RANGE_RE = re.compile(r"(>=|<=|==|>|<)\s*(\d+)\.(\d+)(?:\.(\d+))?")


def _parse_version(v: str) -> tuple[int, int, int]:
    parts = (v.split(".") + ["0", "0"])[:3]
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def version_satisfies(version: str, spec: str) -> bool:
    """极简 semver 范围校验，只支持逗号分隔的 >=/<=/==/>/< 组合。

    刻意不引入第三方版本解析库（packaging 等）——核心本身"只依赖标准库+极少数
    轻量包"是 ARCHITECTURE.md 第5节写死的约束，这里用不到完整 PEP 440/npm
    semver 语法，自己写够用的一小段比拉一个依赖划算。
    """
    v = _parse_version(version)
    for clause in spec.split(","):
        clause = clause.strip()
        if not clause:
            continue
        m = _RANGE_RE.match(clause)
        if not m:
            raise ManifestError(f"无法解析版本范围: {clause!r}")
        op = m.group(1)
        bound = (int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))
        ok = {
            ">=": v >= bound,
            "<=": v <= bound,
            "==": v == bound,
            ">": v > bound,
            "<": v < bound,
        }[op]
        if not ok:
            return False
    return True


CORE_API_VERSION = "0.1.0"


def validate_manifest(manifest: PluginManifest) -> list[str]:
    """返回校验失败原因列表；空列表=通过。

    绝不抛异常——校验失败是正常业务结果（插件停在 invalid 状态，原因可见），
    不是异常情况，这条和 extractors 的"失败折叠成终态"是同一种纪律。
    """
    errors: list[str] = []
    if not manifest.id:
        errors.append("id 不能为空")
    if manifest.runtime.kind not in ("in_process", "subprocess_service"):
        errors.append(
            f"runtime.kind 必须是 in_process 或 subprocess_service，实际是 {manifest.runtime.kind!r}"
        )
    if manifest.runtime.kind == "in_process" and not manifest.runtime.entry:
        errors.append("in_process 插件必须声明 runtime.entry")
    if manifest.runtime.kind == "subprocess_service":
        if not manifest.runtime.command:
            errors.append("subprocess_service 插件必须声明 runtime.command")
        if not manifest.runtime.entry:
            # subprocess_service 插件也要有一个本地 Python 入口类——它在
            # on_enable/on_disable 里用 core.subprocess_service 启动/终止
            # 声明的 command、暴露的方法内部转发成对子进程的HTTP调用，见
            # docs/PLUGIN_SPEC.md 第3节生命周期表"subprocess_service在这
            # 一步拉起子进程"。真正跑模型/重依赖的是子进程，entry 指向的
            # 这个类本身很薄，不需要装任何重依赖。
            errors.append("subprocess_service 插件必须声明 runtime.entry（本地转发类，见 core/subprocess_service.py）")
    for point, cardinality in manifest.provides.items():
        if cardinality not in ("singleton", "multi"):
            errors.append(
                f"provides.{point} 的值必须是 'singleton' 或 'multi'，实际是 {cardinality!r}"
                "（见 docs/PLUGIN_SPEC.md 第2节；核心靠这个值判断要不要检测冲突，不是猜的）"
            )
    try:
        if not version_satisfies(CORE_API_VERSION, manifest.api_version):
            errors.append(
                f"api_version {manifest.api_version!r} 与核心版本 {CORE_API_VERSION} 不兼容"
            )
    except ManifestError as exc:
        errors.append(str(exc))
    return errors
