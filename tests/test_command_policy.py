"""验证命令策略解析可以脱离子进程执行独立审查。"""

import unittest

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.tools import ToolEffect, ToolExecutionError
from deepseek_agent.tools.command_policy import CommandPolicyResolver


class CommandPolicyResolverTests(unittest.TestCase):
    """验证命令白名单、副作用等级和危险参数拒绝规则。"""

    def setUp(self) -> None:
        self.resolver = CommandPolicyResolver()

    def test_read_only_git_query_does_not_require_confirmation(self) -> None:
        policy = self.resolver.resolve({"command": ["git", "status"]})

        self.assertEqual(policy.effect, ToolEffect.READ_ONLY)
        self.assertFalse(policy.requires_confirmation)

    def test_python_test_command_requires_confirmation(self) -> None:
        policy = self.resolver.resolve(
            {"command": ["python", "-m", "unittest"]}
        )

        self.assertEqual(policy.effect, ToolEffect.EXTERNAL_SIDE_EFFECT)
        self.assertTrue(policy.requires_confirmation)

    def test_shell_and_workspace_escape_are_rejected(self) -> None:
        with self.assertRaises(ToolExecutionError):
            self.resolver.resolve({"command": ["powershell", "Get-ChildItem"]})
        with self.assertRaises(ToolExecutionError):
            self.resolver.resolve({"command": ["python", "../outside.py"]})

    def test_git_grep_pager_variants_are_rejected(self) -> None:
        """``git grep`` 的 pager 选项任意形式都不得作为只读命令放行。"""
        for argument in (
            "-O",
            "-Oevil.sh",
            "--open-files-in-pager",
            "--open-files-in-pager=evil.sh",
            "--open",
            "--open=evil.sh",
        ):
            with self.subTest(argument=argument), self.assertRaises(ToolExecutionError):
                self.resolver.resolve({"command": ["git", "grep", argument, "needle"]})

    def test_git_context_and_count_options_still_allowed(self) -> None:
        """上下文行数与计数等合法只读选项不能被 pager 拦截误伤。"""
        for command in (
            ["git", "grep", "-C3", "needle"],
            ["git", "diff", "-C3"],
            ["git", "grep", "-c", "needle"],
        ):
            with self.subTest(command=command):
                policy = self.resolver.resolve({"command": command})
                self.assertEqual(policy.effect, ToolEffect.READ_ONLY)
                self.assertFalse(policy.requires_confirmation)


if __name__ == "__main__":
    unittest.main()
