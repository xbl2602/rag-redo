"""扫描真实 plugins/ 目录里全部 plugin.toml，校验声明的路径类字段真的
指向存在的文件——不是通用 schema 校验（那是 core/manifest.py 的事），是
"这份清单里写的相对路径，运行时真的会去这个地方找文件，文件真的在不在"
这类容易在开发时手滑的一次性检查。

**真实踩过的坑**（2026-09-23，PyInstaller 真机打包+真实安装+真实 MCP
协议调用时抓到）：`official-visual-wemm` 声明了 `env_bootstrap =
"env_bootstrap.py"`，但脚本文件当时放在插件根目录（`plugin.toml` 所在
的那一层），而 `core/subprocess_service.py::resolve_plugin_python()`
实际按插件的 Python 包目录（`entry` 指向的那个包，和 `server.py`/
`plugin.py` 同一层）去找——两个目录概念不一致，本地测试全程走
`RAG_REDO_SKIP_ENV_BOOTSTRAP=1` 从来没有真的执行到这一步，直到真机
打包安装后真实调用才暴露。这份文件把"env_bootstrap 声明的文件必须
真的能在运行时会去找的那个目录下找到"变成一条自动化检查，不用再靠
"打包了才发现"这种代价高的方式才能抓到。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.manifest import load_manifest, validate_manifest  # noqa: E402


class TestRealPluginManifestsPointAtRealFiles(unittest.TestCase):
    def _all_plugin_dirs(self) -> list[Path]:
        plugins_dir = REPO_ROOT / "plugins"
        return sorted(p for p in plugins_dir.iterdir() if p.is_dir() and (p / "plugin.toml").exists())

    def test_every_plugin_toml_parses_and_validates(self):
        for plugin_dir in self._all_plugin_dirs():
            manifest = load_manifest(plugin_dir)
            errors = validate_manifest(manifest)
            self.assertEqual(errors, [], f"{plugin_dir.name}: {errors}")

    def test_env_bootstrap_script_exists_relative_to_the_plugins_own_python_package(self):
        """`resolve_plugin_python()` 用的是插件自己 Python 包所在目录
        （`entry` 指向的那个包，和该包里的 server.py 同级），不是
        `plugin.toml` 所在的插件根目录——两者对目前所有插件来说是相邻但
        不同的两层，这条测试锁定"env_bootstrap 脚本必须和 server.py
        放在同一个目录"这条约定，防止再次踩这个坑。"""
        for plugin_dir in self._all_plugin_dirs():
            manifest = load_manifest(plugin_dir)
            env_bootstrap = manifest.runtime.env_bootstrap
            if not env_bootstrap:
                continue
            assert manifest.runtime.entry is not None
            module_path, _, _ = manifest.runtime.entry.partition(":")
            package_name = module_path.split(".")[0]
            package_dir = plugin_dir / package_name
            script_path = package_dir / env_bootstrap
            self.assertTrue(
                script_path.is_file(),
                f"{plugin_dir.name}: plugin.toml 声明 env_bootstrap={env_bootstrap!r}，"
                f"但 {script_path} 不存在（resolve_plugin_python 实际会去这里找）",
            )
            requirements = package_dir / "requirements.txt"
            self.assertTrue(
                requirements.is_file(),
                f"{plugin_dir.name}: 声明了 env_bootstrap 但没有 {requirements}（约定env_bootstrap.py该装的依赖列表放这里）",
            )


if __name__ == "__main__":
    unittest.main()
