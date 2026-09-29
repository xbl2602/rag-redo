"""BC-17：模型存放目录（`core/paths.py::models_dir` / `models_env`）。

规则：设置项 `models_dir` 留空 = 项目内 `models/`（默认）；用户可以改成任意文件夹；
相对路径按发行目录解析而不是当前工作目录；HF_HOME 这种"模型其实在 hub 子文件夹里"的
路径自动落到 hub；子进程环境只增量覆盖 `HF_HUB_CACHE`。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import paths
from core.paths import models_dir, models_env


class TestModelsDir(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rag_redo_paths_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_empty_setting_defaults_to_models_folder_inside_the_project(self) -> None:
        self.assertEqual(models_dir(""), paths.repo_root() / "models")
        self.assertEqual(models_dir(), paths.repo_root() / "models")

    def test_none_and_blank_are_treated_as_not_configured(self) -> None:
        default = paths.repo_root() / "models"
        self.assertEqual(models_dir(None), default)
        self.assertEqual(models_dir("   "), default)

    def test_absolute_path_is_used_as_is(self) -> None:
        self.assertEqual(models_dir(str(self.tmp)), self.tmp)

    def test_relative_path_is_resolved_against_the_release_dir_not_the_cwd(self) -> None:
        """与 `data_root()` 同一条纪律：在别的目录跑一次 CLI 不能指到另一个空文件夹。"""
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.tmp)
        self.assertEqual(models_dir("my-models"), paths.repo_root() / "my-models")

    def test_tilde_and_environment_variables_are_expanded(self) -> None:
        with patch.dict(os.environ, {"RAG_REDO_TEST_MODELS": str(self.tmp)}):
            text = "%RAG_REDO_TEST_MODELS%" if os.name == "nt" else "$RAG_REDO_TEST_MODELS"
            self.assertEqual(models_dir(text), self.tmp)
        self.assertEqual(models_dir("~/some-models"), Path.home() / "some-models")

    def test_a_folder_that_does_not_exist_yet_is_fine_and_is_not_created(self) -> None:
        target = self.tmp / "not-there-yet"
        self.assertEqual(models_dir(str(target)), target)
        self.assertFalse(target.exists(), "只算路径，不许顺手建目录（首次下载由 HuggingFace 自己建）")

    def test_hf_home_style_path_is_redirected_to_its_hub_subfolder(self) -> None:
        """用户把路径指到 HF_HOME（模型其实在 hub/ 里）也要能找到模型。"""
        (self.tmp / "hub" / "models--BAAI--bge-m3").mkdir(parents=True)
        self.assertEqual(models_dir(str(self.tmp)), self.tmp / "hub")

    def test_a_folder_that_holds_models_directly_is_never_redirected(self) -> None:
        (self.tmp / "models--BAAI--bge-m3").mkdir()
        (self.tmp / "hub" / "models--other--model").mkdir(parents=True)
        self.assertEqual(models_dir(str(self.tmp)), self.tmp, "这一层自己就有模型，不能被 hub 抢走")


class TestModelsEnv(unittest.TestCase):
    def test_only_the_hf_cache_variable_is_overridden(self) -> None:
        base = {"PATH": "C:\\bin", "HF_HUB_CACHE": "D:\\old", "KEEP": "1"}
        env = models_env("E:\\my-models", base=base)
        self.assertEqual(env["HF_HUB_CACHE"], str(models_dir("E:\\my-models")))
        self.assertEqual(env["PATH"], "C:\\bin", "丢 PATH 子进程必死（旧项目 E1 教训）")
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(base["HF_HUB_CACHE"], "D:\\old", "不得改动传入的 base")

    def test_empty_setting_points_children_at_the_project_models_folder(self) -> None:
        env = models_env("", base={})
        self.assertEqual(env["HF_HUB_CACHE"], str(paths.repo_root() / "models"))

    def test_inherits_the_current_environment_by_default(self) -> None:
        with patch.dict(os.environ, {"RAG_REDO_TEST_MARKER": "yes"}):
            env = models_env("")
        self.assertEqual(env["RAG_REDO_TEST_MARKER"], "yes")


if __name__ == "__main__":
    unittest.main()
