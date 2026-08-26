"""验证补丁准备与写入事务可以分别工作。"""

import tempfile
import unittest
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

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


if __name__ == "__main__":
    unittest.main()
