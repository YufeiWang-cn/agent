"""验证未安装源码入口、解释器选择和 PDF 依赖诊断。"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.career.documents import DocumentReader
from deepseek_agent.career.store import CareerDataError


ROOT = Path(__file__).resolve().parents[1]


class SourceStartupTests(unittest.TestCase):
    def load_bootstrap(self):
        spec = importlib.util.spec_from_file_location("startup_bootstrap", ROOT / "_bootstrap.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_all_entry_scripts_find_this_checkout_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in ("main.py", "gui.py", "eval.py", "career.py", "career_gui.py"):
                with self.subTest(filename=filename):
                    result = subprocess.run([sys.executable, "-B", str(ROOT / filename), "--check"],
                                            cwd=directory, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    state = json.loads(result.stdout)
                    self.assertEqual(Path(state["source"]).resolve(), ROOT / "src" / "deepseek_agent" / "__init__.py")
                    self.assertEqual(state["python"], sys.executable)
                    self.assertIn("pypdf", state["dependencies"])

    def test_source_import_works_with_site_packages_disabled(self):
        code = (
            "import runpy,importlib.util,json; "
            f"runpy.run_path({str(ROOT / '_bootstrap.py')!r})['setup_source_path'](); "
            "print(json.dumps(importlib.util.find_spec('deepseek_agent').origin))"
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-S", "-B", "-c", code], cwd=directory,
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)), ROOT / "src" / "deepseek_agent" / "__init__.py")

    def test_old_python_gets_actionable_message_before_importing_application(self):
        bootstrap = self.load_bootstrap()
        with patch.object(sys, "version_info", (3, 6, 3)), patch.object(sys, "argv", ["career_gui.py"]):
            with self.assertRaises(SystemExit) as caught:
                bootstrap.setup_source_path()
        self.assertIn("3.10", str(caught.exception))
        self.assertIn("start_career_gui.cmd", str(caught.exception))

    def test_launcher_selects_agent_python_without_requiring_conda_run(self):
        bootstrap = self.load_bootstrap()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interpreter = root / "envs" / "agent" / "python.exe"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"")
            with patch.object(bootstrap.sys, "prefix", str(root)), \
                    patch.dict(bootstrap.os.environ, {"CAREER_PYTHON": str(interpreter)}), \
                    patch.object(bootstrap.subprocess, "call", return_value=0) as call:
                self.assertEqual(bootstrap.launch_agent("career_gui.py", ["--check"]), 0)
            self.assertEqual(call.call_args[0][0], [str(interpreter), str(ROOT / "career_gui.py"), "--check"])

    def test_health_check_names_the_actual_interpreter_for_missing_dependency(self):
        bootstrap = self.load_bootstrap()
        with patch.object(sys, "argv", ["career_gui.py", "--check"]), \
                patch.object(bootstrap.importlib, "import_module", side_effect=ImportError("missing")):
            with self.assertRaises(SystemExit) as caught:
                bootstrap.setup_source_path()
        self.assertIn(sys.executable, str(caught.exception))
        self.assertIn("-m pip install -e", str(caught.exception))

    def test_pdf_import_error_identifies_current_environment(self):
        with patch.dict(sys.modules, {"pypdf": None}):
            with self.assertRaises(CareerDataError) as caught:
                DocumentReader._pdf(b"pdf")
        self.assertIn(sys.executable, str(caught.exception))
        self.assertIn("pypdf", str(caught.exception))
        self.assertIn("-m pip install -e", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
