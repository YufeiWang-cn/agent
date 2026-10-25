"""验证补丁准备与写入事务可以分别工作。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.tools.base import ToolExecutionError
from deepseek_agent.tools.patch_transaction import PatchPreparer, PatchTransaction
from deepseek_agent.workspace import WorkspaceGuard


class PatchTransactionTests(unittest.TestCase):
    """验证候选补丁不会提前写入，并可由事务组件统一提交。"""

    def test_preparation_is_side_effect_free_until_transaction_applies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "sample.txt"
            target.write_text("old", encoding="utf-8")
            guard = WorkspaceGuard(root, max_file_size=1_000)
            prepared = PatchPreparer(guard).prepare(
                {
                    "changes": [
                        {
                            "operation": "update",
                            "path": "sample.txt",
                            "edits": [
                                {"old_text": "old", "new_text": "new"}
                            ],
                        }
                    ]
                }
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertIn("+new", prepared[0].diff)

            PatchTransaction(guard).apply(prepared)

            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_rollback_preserves_file_modified_after_batch_write(self) -> None:
        """批次执行期间被外部修改的文件不得被回滚静默覆盖。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "first.txt").write_text("old1", encoding="utf-8")
            (root / "second.txt").write_text("old2", encoding="utf-8")
            guard = WorkspaceGuard(root, max_file_size=1_000)
            transaction = PatchTransaction(guard)
            original_write = transaction._guard.write_text
            written: list[str] = []

            def write_then_break(user_path: str, content: str):
                # 第一个文件写入成功后，外部进程修改了它，第二个写入失败。
                if user_path == "second.txt":
                    raise OSError("disk full")
                result = original_write(user_path, content)
                if user_path == "first.txt":
                    (root / "first.txt").write_text("external edit", encoding="utf-8")
                    written.append(user_path)
                return result

            prepared = PatchPreparer(guard).prepare(
                {
                    "changes": [
                        {
                            "operation": "update",
                            "path": "first.txt",
                            "edits": [{"old_text": "old1", "new_text": "new1"}],
                        },
                        {
                            "operation": "update",
                            "path": "second.txt",
                            "edits": [{"old_text": "old2", "new_text": "new2"}],
                        },
                    ]
                }
            )

            with patch.object(transaction._guard, "write_text", side_effect=write_then_break):
                with self.assertRaises(ToolExecutionError) as caught:
                    transaction.apply(prepared)

            self.assertIn("无法回滚", str(caught.exception))
            # 外部修改被保留，没有被回滚覆盖回 old1。
            self.assertEqual(
                (root / "first.txt").read_text(encoding="utf-8"),
                "external edit",
            )
            self.assertEqual(written, ["first.txt"])


if __name__ == "__main__":
    unittest.main()
