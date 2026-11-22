"""本地与 CI 共用入口：仅在主线程回收测试遗留的 Tk 引用环。"""

import argparse
import gc
import threading
import unittest
from pathlib import Path


class MainThreadResult(unittest.TextTestResult):
    def stopTest(self, test: unittest.TestCase) -> None:
        super().stopTest(test)
        gc.collect()


def run_suite(suite: unittest.TestSuite, *, stream=None) -> unittest.TestResult:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("测试必须由主线程运行，Tk 对象不能在后台回收。")
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        return unittest.TextTestRunner(
            verbosity=2, resultclass=MainThreadResult, stream=stream
        ).run(suite)
    finally:
        gc.collect()
        if was_enabled:
            gc.enable()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-directory", type=Path, default=Path(__file__).parent)
    parser.add_argument("--pattern", default="test_*.py")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(
        str(args.start_directory), pattern=args.pattern
    )
    if not suite.countTestCases():
        parser.error("未发现测试，拒绝空跑通过。")
    result = run_suite(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
