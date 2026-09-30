# -*- coding: utf-8 -*-
"""见 ./AGENTS.md 测试纪律。core/model_loading 的离线优先加载策略——对齐
旧 obsidian-rag/index.py::_load_pretrained（问题59 B1）的行为分支。"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.model_loading import load_pretrained, param_dtype_mixed  # noqa: E402


class TestLoadPretrained(unittest.TestCase):
    def test_local_cache_hit_never_touches_network(self):
        calls = []

        def factory(model_id, **kwargs):
            calls.append(kwargs)
            if kwargs.get("local_files_only"):
                return "local-model"
            raise AssertionError("不应触发联网加载")

        self.assertEqual(load_pretrained(factory, "BAAI/x"), "local-model")
        self.assertEqual(calls, [{"local_files_only": True}])

    def test_local_failure_falls_back_to_online(self):
        def factory(model_id, **kwargs):
            if kwargs.get("local_files_only"):
                raise OSError("缺文件")
            return "online-model"

        logs = []
        self.assertEqual(
            load_pretrained(factory, "BAAI/x", log=logs.append), "online-model"
        )
        self.assertTrue(any("回退联网" in line for line in logs))

    def test_corrupt_cache_also_falls_back(self):
        """损坏快照抛 ValueError/RuntimeError 也回退——旧问题59 B1：此前只接
        OSError，坏缓存永不回退直接硬失败。"""

        def factory(model_id, local_files_only=False, **kwargs):
            if local_files_only:
                raise ValueError("截断 JSON")
            return "online-model"

        self.assertEqual(load_pretrained(factory, "BAAI/x"), "online-model")

    def test_loading_never_asks_the_hub_to_convert_weights(self):
        """2026-09-30 真机 cProfile：bge-m3 本地快照只有 pytorch_model.bin，transformers
        每次冷加载都另起线程问 huggingface.co 要 safetensors 转换（4 个请求、约 2 秒），
        `local_files_only=True` 拦不住。加载期间必须把这个开关关掉（AGENTS.md §1.3 本地优先）。"""
        seen = []

        def factory(model_id, **kwargs):
            seen.append(os.environ.get("DISABLE_SAFETENSORS_CONVERSION"))
            return "local-model"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISABLE_SAFETENSORS_CONVERSION", None)
            load_pretrained(factory, "BAAI/x")
        self.assertEqual(seen, ["1"])

    def test_transformers_still_honours_the_conversion_switch(self):
        """上一条只有在 transformers 仍按这个名字读开关时才有效；换版本改了名字，这里转红提醒。
        只读源码文本，不 import transformers（冷启动好几秒）。"""
        spec = importlib.util.find_spec("transformers")
        if spec is None or not spec.submodule_search_locations:
            self.skipTest("本环境没装 transformers")
        source = Path(list(spec.submodule_search_locations)[0]) / "modeling_utils.py"
        self.assertIn("DISABLE_SAFETENSORS_CONVERSION", source.read_text(encoding="utf-8"))

    def test_both_paths_fail_raises_with_cleanup_guidance(self):
        def factory(model_id, local_files_only=False, **kwargs):
            raise OSError("nope")

        with self.assertRaises(RuntimeError) as ctx:
            load_pretrained(factory, "BAAI/x")
        self.assertIn("删除本地快照", str(ctx.exception))


class TestParamDtypeMixed(unittest.TestCase):
    def _fake_model(self, dtypes):
        class P:
            def __init__(self, dtype):
                self.dtype = dtype

        class M:
            def __init__(self, dtypes):
                self._dtypes = dtypes

            def parameters(self):
                return [P(d) for d in self._dtypes]

        return M(dtypes)

    def test_uniform_dtype_not_mixed(self):
        self.assertFalse(param_dtype_mixed(self._fake_model(["Half"] * 391)))

    def test_half_float_mix_detected(self):
        self.assertTrue(param_dtype_mixed(self._fake_model(["Half", "Float"])))

    def test_empty_parameters_not_mixed(self):
        self.assertFalse(param_dtype_mixed(self._fake_model([])))


if __name__ == "__main__":
    unittest.main()
