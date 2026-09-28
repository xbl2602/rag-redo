"""设置页登记表（`settings_schema.py`）的对账测试。

登记表里的每个键都必须满足两条，否则设置页就是在说谎：

1. **真的有人读它**——"假开关"（表里有、代码里没有任何地方读它）让用户改了没有任何效果，
   比没有这一项更糟；
2. **展示用的默认值与读它的地方一致**——设置页显示的"当前值"必须是真实生效的值。

默认值的权威出处是各个消费方自己（core 的常量、各插件的常量/字面量）；GUI 插件不许
import 别的插件，所以登记表里抄了一份展示用的默认值，这份测试就是它与真实消费方之间的
对账：把每个键在 `core/` 与各插件里的 `settings.get("<键>", <默认值>)` 调用点用 AST 找出来，
逐个与登记表比对。漂移了就红，而不是让设置页悄悄显示一个不生效的数。
"""
from __future__ import annotations

import ast
import importlib
import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for _p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
for _other in (_REPO_ROOT / "plugins").glob("*"):
    if _other.is_dir() and str(_other) not in sys.path:
        sys.path.insert(0, str(_other))

from official_gui_shell.settings_schema import (  # noqa: E402
    FIELDS,
    GROUPS,
    SETTING_FIELD_META,
    coerce_setting,
    format_setting_value,
    setting_is_secret,
)

#: 展示层自己：它们复用消费方的默认值，不是"读设置的地方"，不参与对账。
_PRESENTATION_PLUGINS = {"official-gui-shell", "official-mcp-server"}
_KINDS = {"str", "int", "float", "bool", "list"}


def _consumer_modules() -> list[tuple[str, Path]]:
    """`(可导入的模块名, 源文件)`：core 与各插件包顶层模块（不含展示层与测试）。"""
    found: list[tuple[str, Path]] = [
        (f"core.{p.stem}", p) for p in sorted((_REPO_ROOT / "core").glob("*.py")) if p.stem != "__init__"
    ]
    for plugin_dir in sorted((_REPO_ROOT / "plugins").glob("official-*")):
        if plugin_dir.name in _PRESENTATION_PLUGINS:
            continue
        for package in sorted(plugin_dir.iterdir()):
            if not package.is_dir() or package.name.startswith(".") or package.name == "tests":
                continue
            if not (package / "__init__.py").exists():
                continue
            found += [
                (f"{package.name}.{p.stem}", p)
                for p in sorted(package.glob("*.py")) if p.stem != "__init__"
            ]
    return found


def _call_sites() -> dict[str, list[tuple[str, str, object, bool]]]:
    """`{键: [(文件, 模块, 默认值, 是否解析成功), ...]}`——每个 `.get("<键>", <默认值>)` 调用点。"""
    sites: dict[str, list[tuple[str, str, object, bool]]] = {key: [] for key in FIELDS}
    for module_name, path in _consumer_modules():
        source = path.read_text(encoding="utf-8")
        if ".get(" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        module = None
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in FIELDS
            ):
                continue
            key = node.args[0].value
            default_node = node.args[1]
            try:
                value, ok = ast.literal_eval(default_node), True
            except ValueError:
                value, ok = None, False
                if isinstance(default_node, ast.Name):
                    if module is None:
                        module = importlib.import_module(module_name)
                    if hasattr(module, default_node.id):
                        value, ok = getattr(module, default_node.id), True
            sites[key].append((str(path.relative_to(_REPO_ROOT)), module_name, value, ok))
    return sites


class TestSchemaIsInternallyConsistent(unittest.TestCase):
    def test_keys_are_unique_and_kinds_are_known(self) -> None:
        keys = [f.key for g in GROUPS for f in g.fields]
        self.assertEqual(len(keys), len(set(keys)), "设置键重复")
        self.assertEqual(set(keys), set(FIELDS))
        for field in FIELDS.values():
            self.assertIn(field.kind, _KINDS, field.key)
            self.assertTrue(field.label and field.hint, f"{field.key} 缺中文名/说明")

    def test_default_type_matches_the_declared_kind(self) -> None:
        for field in FIELDS.values():
            with self.subTest(key=field.key):
                default = field.default
                if field.kind == "bool":
                    self.assertIsInstance(default, bool)
                elif field.kind == "int":
                    self.assertIsInstance(default, int)
                    self.assertNotIsInstance(default, bool)
                elif field.kind == "float":
                    self.assertIsInstance(default, float)
                elif field.kind == "list":
                    self.assertIsInstance(default, list)
                else:
                    self.assertIsInstance(default, str)

    def test_choice_fields_include_their_own_default(self) -> None:
        for field in FIELDS.values():
            if field.choices:
                with self.subTest(key=field.key):
                    self.assertIn(field.default, [c[0] for c in field.choices])

    def test_every_default_survives_a_format_and_coerce_round_trip(self) -> None:
        """保存把页面上的值原样发回：默认值 → 显示串 → 转回真值，必须还是默认值（否则
        "什么都没改"的一次保存就会把默认值改掉/写死）。"""
        for field in FIELDS.values():
            with self.subTest(key=field.key):
                text = format_setting_value(field.kind, field.default)
                self.assertEqual(coerce_setting(field.key, text), field.default)

    def test_secret_flags_and_meta_are_consistent(self) -> None:
        self.assertTrue(setting_is_secret("hyde_llm_api_key"))
        self.assertTrue(setting_is_secret("some_new_api_key"))
        self.assertTrue(setting_is_secret("some_new_token"))
        self.assertFalse(setting_is_secret("fusion_dense_weight"))
        self.assertEqual(set(SETTING_FIELD_META), set(FIELDS))
        for key, meta in SETTING_FIELD_META.items():
            self.assertEqual(bool(meta.get("secret")), FIELDS[key].secret, key)

    def test_coerce_rejects_bad_values_with_a_chinese_reason(self) -> None:
        for key, bad in (
            ("max_chunks_per_file", "三"), ("max_chunks_per_file", "3.5"), ("fusion_dense_weight", "abc"),
            ("fusion_dense_weight", "nan"), ("hyde_enabled", "maybe"), ("pdf_scan_backend", "fake"),
        ):
            with self.subTest(key=key, bad=bad):
                with self.assertRaises(ValueError) as caught:
                    coerce_setting(key, bad)
                self.assertRegex(str(caught.exception), r"[一-鿿]")
        self.assertEqual(coerce_setting("max_chunks_per_file", "5"), 5)
        self.assertEqual(coerce_setting("max_chunks_per_file", 5.0), 5)
        self.assertIs(coerce_setting("hyde_enabled", "ON"), True)
        self.assertEqual(coerce_setting("default_libraries", " a, ,b "), ["a", "b"])
        self.assertEqual(coerce_setting("hyde_llm_api_key", "0123456789"), "0123456789")


class TestSchemaMatchesTheRealConsumers(unittest.TestCase):
    """对账：登记表 ↔ core/各插件里真实读设置的地方。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sites = _call_sites()

    def test_every_registered_key_is_actually_read_somewhere(self) -> None:
        unread = sorted(key for key, found in self.sites.items() if not found)
        self.assertEqual(
            unread, [],
            f"这些设置键在设置页里有、但 core/插件里没有任何地方读它（假开关，改了不生效）：{unread}",
        )

    def test_every_default_is_resolvable_and_agrees_with_every_reader(self) -> None:
        problems: list[str] = []
        for key, found in self.sites.items():
            expected = FIELDS[key].default
            for where, _module, value, ok in found:
                if not ok:
                    problems.append(f"{key}: {where} 的默认值不是字面量也不是模块常量，无法对账")
                elif value != expected or type(value) is not type(expected):
                    problems.append(f"{key}: {where} 读到的默认值 {value!r} ≠ 设置页显示的 {expected!r}")
        self.assertEqual(problems, [], "设置页默认值与真实消费方漂移：\n" + "\n".join(problems))

    def test_the_gui_never_keeps_its_own_second_copy_of_a_default(self) -> None:
        """展示层读设置时不得自己再写一份字面量默认值（只能取登记表的）：登记表已经是
        对账过的那一份，第二份就是下一次漂移的源头。"""
        source = (_PLUGIN_DIR / "official_gui_shell" / "contract_bridge.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = [
            f"contract_bridge.py:{node.lineno} {node.args[0].value}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in FIELDS and isinstance(node.args[1], ast.Constant)
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
