"""测试入口的回收线程、失败退出及真实 Tk→线程顺序回归。"""

import gc
import io
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

from run_suite import run_suite

RUNNER = Path(__file__).with_name("run_suite.py")


class SuiteRunnerTests(unittest.TestCase):
    def test_failure_is_preserved_and_gc_setting_is_restored(self):
        before = gc.isenabled()

        def failure():
            self.assertFalse(gc.isenabled())
            raise AssertionError("intentional failure")

        result = run_suite(
            unittest.TestSuite([unittest.FunctionTestCase(failure)]),
            stream=io.StringIO(),
        )
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(gc.isenabled(), before)

    def test_background_runner_is_rejected(self):
        errors = []

        def worker():
            try:
                run_suite(unittest.TestSuite(), stream=io.StringIO())
            except RuntimeError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)

    def run_child(self, code):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "case_probe.py").write_text(
                textwrap.dedent(code), encoding="utf-8"
            )
            return subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(RUNNER),
                    "--start-directory",
                    folder,
                    "--pattern",
                    "case_*.py",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )

    def test_process_reports_failure_and_empty_discovery(self):
        result = self.run_child("""
            import unittest
            class Probe(unittest.TestCase):
                def test_failure(self):
                    self.fail('expected failure')
        """)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("FAILED (failures=1)", result.stderr)
        empty = self.run_child("import unittest")
        self.assertEqual(empty.returncode, 2, empty.stderr)

    def test_tk_cycles_are_collected_before_background_allocations(self):
        result = self.run_child("""
            import gc
            import threading
            import tkinter as tk
            import unittest
            import weakref
            reference = None
            destroyed_on = []
            class Cycle:
                def __init__(self, root):
                    self.variable = tk.StringVar(master=root, value='probe')
                    self.cycle = self
                def __del__(self):
                    destroyed_on.append(threading.get_ident())
            class Probe(unittest.TestCase):
                def test_01_destroy_gui(self):
                    global reference
                    root = tk.Tk()
                    root.withdraw()
                    try:
                        value = Cycle(root)
                        reference = weakref.ref(value)
                    finally:
                        root.destroy()
                def test_02_start_worker(self):
                    self.assertIsNone(reference())
                    self.assertEqual(destroyed_on,[threading.get_ident()])
                    observed = []
                    def worker():
                        observed.append(gc.isenabled())
                        for _ in range(20000):
                            value = []; value.append(value)
                    thread = threading.Thread(target=worker)
                    thread.start(); thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(observed,[False])
        """)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 2 tests", result.stderr)
        self.assertNotIn("Tcl_AsyncDelete", result.stderr)
        self.assertNotIn("main thread is not in main loop", result.stderr)
