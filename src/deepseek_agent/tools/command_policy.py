"""校验受控命令，并生成不可变的执行策略。"""

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .base import JsonObject, ToolEffect, ToolExecutionError


MAX_COMMAND_ARGUMENTS = 64
MAX_ARGUMENT_LENGTH = 4_096
# 即使 ``shell=False`` 不会解释这些标记，也应提前拒绝以保持安全边界稳定。
SHELL_OPERATORS = frozenset(
    {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<"}
)
SHELL_EXECUTABLES = frozenset(
    {"cmd", "powershell", "pwsh", "bash", "sh", "zsh", "fish", "wsl"}
)
# 只有经过审计的 Git 查询才能跳过人工确认。
READ_ONLY_GIT_COMMANDS = frozenset(
    {"status", "diff", "log", "show", "rev-parse", "ls-files", "grep", "describe"}
)
CONFIRMED_GIT_COMMANDS = frozenset(
    {
        "add",
        "restore",
        "switch",
        "checkout",
        "commit",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "stash",
        "tag",
        "fetch",
        "pull",
        "push",
    }
)
# 高破坏性命令不会在确认后放行，避免一次误确认永久丢失改动。
FORBIDDEN_GIT_COMMANDS = frozenset({"clean", "reset"})
FORBIDDEN_GIT_OPTIONS = (
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--exec-path",
    "--output",
    "--ext-diff",
    "--textconv",
    "--no-index",
    "--open-files-in-pager",
)


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    """保存一次受控命令经过校验后的执行策略。"""

    argv: tuple[str, ...]
    effect: ToolEffect
    requires_confirmation: bool


class CommandPolicyResolver:
    """集中执行命令白名单、路径形态和副作用等级校验。"""

    def resolve(self, arguments: JsonObject) -> CommandPolicy:
        """校验一次工具参数，并返回可直接执行的规范化策略。"""
        unexpected_keys = set(arguments) - {"command", "cwd"}
        if unexpected_keys:
            names = "、".join(sorted(str(key) for key in unexpected_keys))
            raise ToolExecutionError(f"run_command 包含不支持的参数：{names}")

        command = self._validate_command(arguments.get("command"))
        self._reject_outside_path_tokens(command[1:])
        executable = Path(command[0]).name.lower().removesuffix(".exe")
        if executable in SHELL_EXECUTABLES:
            raise ToolExecutionError(f"禁止启动 Shell：{command[0]}")
        if executable == "git":
            return self._git_policy(command)
        if executable in {"python", "python3"}:
            return self._python_policy(command)
        raise ToolExecutionError("当前只允许受控的 git、python 和 python3 命令。")

    @staticmethod
    def _validate_command(raw_command: object) -> list[str]:
        if not isinstance(raw_command, list) or not raw_command:
            raise ToolExecutionError(
                f"command 必须是包含 1 到 {MAX_COMMAND_ARGUMENTS} 个非空字符串的数组。"
            )
        if len(raw_command) > MAX_COMMAND_ARGUMENTS:
            raise ToolExecutionError(
                f"command 最多包含 {MAX_COMMAND_ARGUMENTS} 个字符串。"
            )
        if not all(isinstance(item, str) and item for item in raw_command):
            raise ToolExecutionError("command 中的每一项都必须是非空字符串。")
        if any(
            len(item) > MAX_ARGUMENT_LENGTH or "\x00" in item
            for item in raw_command
        ):
            raise ToolExecutionError("命令参数过长或包含空字符。")
        if any(item in SHELL_OPERATORS for item in raw_command):
            raise ToolExecutionError("不支持 Shell 管道、重定向或命令拼接。")
        return raw_command

    @staticmethod
    def _git_policy(command: list[str]) -> CommandPolicy:
        if "/" in command[0] or "\\" in command[0]:
            raise ToolExecutionError(
                "git 必须通过系统 PATH 启动，不能指定自定义程序路径。"
            )
        # 只允许 PATH 中解析到的 Git，避免运行工作区里伪装成 git.exe 的程序。
        git_executable = shutil.which("git")
        if git_executable is None:
            raise ToolExecutionError("系统 PATH 中未找到 git。")
        normalized = [git_executable, *command[1:]]
        if len(command) < 2 or command[1].startswith("-"):
            raise ToolExecutionError("git 命令必须直接指定受支持的子命令。")
        if any(
            argument == option or argument.startswith(f"{option}=")
            for argument in command[1:]
            for option in FORBIDDEN_GIT_OPTIONS
        ):
            raise ToolExecutionError("git 命令包含可能绕过工作区或写入文件的选项。")
        subcommand = command[1].lower()
        if subcommand in FORBIDDEN_GIT_COMMANDS:
            raise ToolExecutionError(f"当前版本禁止执行破坏性 git {subcommand}。")
        if subcommand in READ_ONLY_GIT_COMMANDS:
            return CommandPolicy(tuple(normalized), ToolEffect.READ_ONLY, False)
        if subcommand in CONFIRMED_GIT_COMMANDS:
            return CommandPolicy(
                tuple(normalized),
                ToolEffect.EXTERNAL_SIDE_EFFECT,
                True,
            )
        raise ToolExecutionError(f"不支持 git 子命令：{subcommand}")

    @staticmethod
    def _python_policy(command: list[str]) -> CommandPolicy:
        # 命令始终使用 Agent 当前虚拟环境中的解释器。
        normalized = [sys.executable, *command[1:]]
        if len(command) == 2 and command[1] in {"--version", "-V"}:
            return CommandPolicy(tuple(normalized), ToolEffect.READ_ONLY, False)
        if len(command) >= 3 and command[1] == "-m":
            module = command[2]
            if module in {"unittest", "pytest", "pip", "compileall"}:
                return CommandPolicy(
                    tuple(normalized),
                    ToolEffect.EXTERNAL_SIDE_EFFECT,
                    True,
                )
            raise ToolExecutionError(f"不支持执行 Python 模块：{module}")
        if len(command) >= 2 and command[1].lower().endswith(".py"):
            script = Path(command[1])
            if script.is_absolute() or ".." in script.parts:
                raise ToolExecutionError("Python 脚本必须使用工作区内的相对路径。")
            return CommandPolicy(
                tuple(normalized),
                ToolEffect.EXTERNAL_SIDE_EFFECT,
                True,
            )
        if any(item in {"-c", "-"} for item in command[1:]):
            raise ToolExecutionError("禁止执行内联 Python 代码或标准输入脚本。")
        raise ToolExecutionError("不支持该 Python 命令形式。")

    @staticmethod
    def _reject_outside_path_tokens(arguments: list[str]) -> None:
        """拒绝参数中显式的绝对路径和父目录跳转。"""
        for argument in arguments:
            candidate = argument.split("=", 1)[1] if "=" in argument else argument
            candidate_paths = (
                PurePosixPath(candidate),
                PureWindowsPath(candidate),
            )
            escapes_workspace = any(
                path.is_absolute() or ".." in path.parts for path in candidate_paths
            )
            if escapes_workspace:
                raise ToolExecutionError("命令参数不得引用工作区外的路径。")


__all__ = ["CommandPolicy", "CommandPolicyResolver", "MAX_COMMAND_ARGUMENTS"]
