from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.registry import ExtensionConflictError, ExtensionRegistry


class TestExtensionRegistry(unittest.TestCase):
    def test_single_provider_singleton_active(self):
        reg = ExtensionRegistry()
        reg.register("plugin-a", {"embedder": "singleton"})
        self.assertEqual(reg.active_of("embedder"), "plugin-a")

    def test_multi_provider_singleton_without_choice_conflicts(self):
        reg = ExtensionRegistry()
        reg.register("plugin-a", {"embedder": "singleton"})
        reg.register("plugin-b", {"embedder": "singleton"})
        with self.assertRaises(ExtensionConflictError):
            reg.active_of("embedder")

    def test_conflicts_listed(self):
        reg = ExtensionRegistry()
        reg.register("plugin-a", {"embedder": "singleton"})
        reg.register("plugin-b", {"embedder": "singleton"})
        self.assertIn("embedder", reg.conflicts())

    def test_explicit_choice_resolves_conflict(self):
        reg = ExtensionRegistry()
        reg.register("plugin-a", {"embedder": "singleton"})
        reg.register("plugin-b", {"embedder": "singleton"})
        reg.set_active("embedder", "plugin-b")
        self.assertEqual(reg.active_of("embedder"), "plugin-b")
        self.assertEqual(reg.conflicts(), {})

    def test_multi_value_point_not_singleton(self):
        reg = ExtensionRegistry()
        reg.register("plugin-a", {"gui_panel": "multi"})
        reg.register("plugin-b", {"gui_panel": "multi"})
        self.assertEqual(sorted(reg.providers_of("gui_panel")), ["plugin-a", "plugin-b"])
        self.assertIsNone(reg.active_of("gui_panel"))
        self.assertEqual(reg.conflicts(), {})

    def test_unregister_removes_provider(self):
        reg = ExtensionRegistry()
        reg.register("plugin-a", {"embedder": "singleton"})
        reg.unregister("plugin-a")
        self.assertEqual(reg.providers_of("embedder"), [])

    def test_no_hardcoded_point_list_arbitrary_point_names_work(self):
        """核心不维护写死的扩展点名单——任意点名只要声明 singleton 就会被当作
        单例检测冲突，这是修掉"只有预先列在核心里的点才会检测冲突"这个真实
        踩到的坑之后补的回归测试。"""
        reg = ExtensionRegistry()
        reg.register("plugin-a", {"totally_custom_point": "singleton"})
        reg.register("plugin-b", {"totally_custom_point": "singleton"})
        self.assertIn("totally_custom_point", reg.conflicts())

    def test_cardinality_mismatch_defaults_to_singleton(self):
        """两个插件对同一个点的基数声明不一致时，保守按 singleton 处理
        （宁可多报冲突，不可漏报）。"""
        reg = ExtensionRegistry()
        reg.register("plugin-a", {"weird_point": "multi"})
        reg.register("plugin-b", {"weird_point": "singleton"})
        self.assertTrue(reg.is_singleton("weird_point"))


if __name__ == "__main__":
    unittest.main()
