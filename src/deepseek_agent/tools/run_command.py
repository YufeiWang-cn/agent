"""在安全工作区内以无 Shell 方式运行受控命令。"""

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from ..workspace import WorkspaceAccessError, WorkspaceGuard
from .base import (
    JsonObject,
    Tool,
    ToolEffect,
    ToolExecutionContext,
    ToolExecutionError,
)


MAX_COMMAND_ARGUMENTS = 64
MAX_ARGUMENT_LENGTH = 4_096
POLL_INTERVAL_SECONDS = 0.1
# 这些标记即使出现在参数数组中也会被直接拒绝。
# 虽然 ``shell=False`` 不会解释它们，但提前拒绝可以防止未来重构意外扩大执行能力。
SHELL_OPERATORS = frozenset(
    {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<"}
)
SHELL_EXECUTABLES = frozenset(
    {"cmd", "powershell", "pwsh", "bash", "sh", "zsh", "fish", "wsl"}
)
SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
# 只有这里列出的 Git 查询可以跳过人工确认。
# 扩充本集合前，必须确认相应子命令及其允许选项不会写文件、启动外部程序或访问工作区外路径。
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
# 高破坏性命令暂不采用“确认后放行”，因为一次误确认就可能永久丢失改动。
FORBIDDEN_GIT_COMMANDS = frozenset({"clean", "reset"})
# 部分看似只读的子命令可以借助这些选项写文件、切换仓库或启动外部辅助程序。
# 因此，不能只根据子命令名称判断安全性。
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


class RunCommandTool(Tool):
    """只运行明确支持的命令族，并限制目录、时间和输出。"""

    name = "run_command"
    description = (
        "在工作目录内运行受控的 Git 或 Python 命令；不支持 Shell、管道、"
        "重定向、命令拼接和后台进程。"
    )
    # 类级默认值采用最保守的策略，实际调用会由下方两个动态方法重新判断。
    requires_confirmation = True
    effect = ToolEffect.EXTERNAL_SIDE_EFFECT
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "command": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MAX_COMMAND_ARGUMENTS,
                "description": "命令及参数数组，例如 [\"python\", \"-m\", \"unittest\"]。",
            },
            "cwd": {
                "type": "string",
                "description": "相对于工作目录的执行目录，默认为当前工作目录。",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        guard: WorkspaceGuard,
        *,
        timeout_seconds: float = 120.0,
        max_output_bytes: int = 50_000,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes 必须大于 0")
        self._guard = guard
        self.timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    def requires_confirmation_for(self, arguments: JsonObject) -> bool:
        # ToolExecutor 会在执行前调用本方法。
        # 直接调用 execute() 不会弹出确认框。
        return self._prepare_policy(arguments).requires_confirmation

    def effect_for(self, arguments: JsonObject) -> ToolEffect:
        return self._prepare_policy(arguments).effect

    def execute(self, arguments: JsonObject) -> str:
        return self.execute_with_context(arguments, ToolExecutionContext())

    def execute_with_context(
        self,
        arguments: JsonObject,
        context: ToolExecutionContext,
    ) -> str:
        # 执行阶段再次生成策略，避免调用方绕过 ToolExecutor 后跳过白名单校验。
        policy = self._prepare_policy(arguments)
        # WorkspaceGuard 会把 cwd 解析为真实路径。
        # 绝对路径、父目录跳转和越界符号链接都会在创建子进程前被拒绝。
        cwd = self._resolve_cwd(arguments)
        self._validate_git_repository(policy, cwd)
        started_at = time.monotonic()
        environment = self._safe_environment()

        with (
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            # 子进程输出先写入临时文件，防止大量 stdout/stderr 在内存中无限累积。
            # max_output_bytes 只限制最终返回量，持续输出的进程仍由超时机制终止。
            try:
                process = subprocess.Popen(
                    list(policy.argv),
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=environment,
                    # 安全边界：参数不会交给 cmd、PowerShell 或 Bash 二次解析。
                    shell=False,
                    start_new_session=os.name != "nt",
                    creationflags=self._creation_flags(),
                )
            except (OSError, ValueError) as error:
                raise ToolExecutionError(f"无法启动命令：{error}") from error

            deadline = started_at + self.timeout_seconds
            while process.poll() is None:
                if context.cancelled:
                    self._terminate_process(process)
                    # 写入类命令被中断时，系统无法断定它是否已经生效。
                    # 上层必须按照 RESULT_UNKNOWN 状态和副作用保留规则处理。
                    raise ToolExecutionError(
                        "命令执行已取消。",
                        side_effect_possible=policy.effect is not ToolEffect.READ_ONLY,
                    )
                if time.monotonic() >= deadline:
                    self._terminate_process(process)
                    raise ToolExecutionError(
                        f"命令执行超过 {self.timeout_seconds:g} 秒，已终止。",
                        side_effect_possible=policy.effect is not ToolEffect.READ_ONLY,
                    )
                time.sleep(POLL_INTERVAL_SECONDS)

            stdout, stdout_truncated = self._read_output(stdout_file)
            stderr, stderr_truncated = self._read_output(stderr_file)

        # Popen 正常结束不代表业务命令执行成功。
        # 调用模型必须检查 exit_code。
        return json.dumps(
            {
                "command": list(policy.argv),
                "cwd": self._guard.relative_path(cwd),
                "exit_code": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "duration_seconds": round(time.monotonic() - started_at, 3),
            },
            ensure_ascii=False,
        )

    def _prepare_policy(self, arguments: JsonObject) -> CommandPolicy:
        """完成通用校验并把命令分派到对应的命令族策略。"""
        unexpected_keys = set(arguments) - {"command", "cwd"}
        if unexpected_keys:
            raise ToolExecutionError(
                "run_command 包含不支持的参数："
                + "、".join(sorted(str(key) for key in unexpected_keys))
            )
        raw_command = arguments.get("command")
        if (
            not isinstance(raw_command, list)
            or not raw_command
            or len(raw_command) > MAX_COMMAND_ARGUMENTS
            or not all(isinstance(item, str) and item for item in raw_command)
        ):
            raise ToolExecutionError(
                f"command 必须是包含 1 到 {MAX_COMMAND_ARGUMENTS} 个非空字符串的数组。"
            )
        if any(
            len(item) > MAX_ARGUMENT_LENGTH or "\x00" in item
            for item in raw_command
        ):
            raise ToolExecutionError("命令参数过长或包含空字符。")
        if any(item in SHELL_OPERATORS for item in raw_command):
            raise ToolExecutionError("不支持 Shell 管道、重定向或命令拼接。")
        # 这里执行命令行层的第一道路径过滤。
        # 实际 cwd、脚本和仓库根目录稍后还会通过 WorkspaceGuard 校验真实路径。
        self._reject_outside_path_tokens(raw_command[1:])

        executable = Path(raw_command[0]).name.lower()
        if executable.endswith(".exe"):
            executable = executable[:-4]
        if executable in SHELL_EXECUTABLES:
            raise ToolExecutionError(f"禁止启动 Shell：{raw_command[0]}")
        if executable == "git":
            return self._git_policy(raw_command)
        if executable in {"python", "python3"}:
            return self._python_policy(raw_command)
        raise ToolExecutionError(
            "当前只允许受控的 git、python 和 python3 命令。"
        )

    def _git_policy(self, command: list[str]) -> CommandPolicy:
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
        # 子命令看似只读也可能被选项改变语义，因此必须先过滤危险选项。
        if any(
            argument == option or argument.startswith(f"{option}=")
            for argument in command[1:]
            for option in FORBIDDEN_GIT_OPTIONS
        ):
            raise ToolExecutionError("git 命令包含可能绕过工作区或写入文件的选项。")
        subcommand = command[1].lower()
        if subcommand in FORBIDDEN_GIT_COMMANDS:
            raise ToolExecutionError(f"第一版禁止执行破坏性 git {subcommand}。")
        if subcommand in READ_ONLY_GIT_COMMANDS:
            return CommandPolicy(tuple(normalized), ToolEffect.READ_ONLY, False)
        if subcommand in CONFIRMED_GIT_COMMANDS:
            return CommandPolicy(
                tuple(normalized),
                ToolEffect.EXTERNAL_SIDE_EFFECT,
                True,
            )
        raise ToolExecutionError(f"不支持 git 子命令：{subcommand}")

    def _python_policy(self, command: list[str]) -> CommandPolicy:
        # 命令始终使用 Agent 当前虚拟环境中的解释器。
        # 这样可以避免模型调用系统旧版 Python，或通过自定义 python.exe 路径替换实际程序。
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

    def _resolve_cwd(self, arguments: JsonObject) -> Path:
        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, str):
            raise ToolExecutionError("cwd 必须是相对于工作目录的字符串。")
        try:
            return self._guard.resolve_directory(cwd)
        except WorkspaceAccessError as error:
            raise ToolExecutionError(str(error)) from error

    @staticmethod
    def _reject_outside_path_tokens(arguments: list[str]) -> None:
        """拒绝参数中显式的绝对路径和父目录跳转。

        这里只检查命令行中可识别的路径形态，并不能构成操作系统沙箱。
        确认后运行的 Python 代码仍可能自行访问工作区外文件。
        """
        for argument in arguments:
            candidate = argument.split("=", 1)[1] if "=" in argument else argument
            if (
                PurePosixPath(candidate).is_absolute()
                or PureWindowsPath(candidate).is_absolute()
                or ".." in PurePosixPath(candidate).parts
                or ".." in PureWindowsPath(candidate).parts
            ):
                raise ToolExecutionError("命令参数不得引用工作区外的路径。")

    def _validate_git_repository(self, policy: CommandPolicy, cwd: Path) -> None:
        if Path(policy.argv[0]).name.lower().removesuffix(".exe") != "git":
            if len(policy.argv) >= 2 and policy.argv[1].lower().endswith(".py"):
                script_path = (cwd / policy.argv[1]).resolve(strict=False)
                if (
                    not self._guard.is_accessible(script_path)
                    or not script_path.is_file()
                ):
                    raise ToolExecutionError("Python 脚本不存在或不在工作区内。")
            return
        # Git 会从 cwd 开始向父目录寻找 .git。
        # 即使 cwd 位于工作区内，最终找到的仓库根目录仍可能位于工作区外。
        # 因此，必须单独查询并校验 --show-toplevel。
        try:
            completed = subprocess.run(
                [policy.argv[0], "rev-parse", "--show-toplevel"],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                shell=False,
                creationflags=self._creation_flags(),
                env=self._safe_environment(),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ToolExecutionError(f"无法确认 Git 工作区：{error}") from error
        if completed.returncode != 0:
            raise ToolExecutionError("执行目录不在工作区内的 Git 仓库中。")
        try:
            repository_root = Path(
                completed.stdout.decode("utf-8", errors="replace").strip()
            ).resolve(strict=True)
        except OSError as error:
            raise ToolExecutionError("无法解析 Git 仓库根目录。") from error
        if not self._guard.is_accessible(repository_root):
            raise ToolExecutionError("Git 仓库根目录位于工作区之外，禁止执行。")

    def _read_output(self, stream: BinaryIO) -> tuple[str, bool]:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        content = stream.read(self._max_output_bytes)
        return content.decode("utf-8", errors="replace"), size > self._max_output_bytes

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        # 子进程不应继承 Agent 的 API Key 等常见凭据。
        # 这里根据环境变量名称过滤敏感项，以降低凭据泄露风险。
        # 这种过滤不等同于完整的最小权限环境白名单。
        environment = {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in SENSITIVE_ENV_MARKERS)
        }
        environment.update(
            {
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "NO_COLOR": "1",
            }
        )
        return environment

    @staticmethod
    def _creation_flags() -> int:
        if os.name != "nt":
            return 0
        # 独立进程组用于在取消或超时时终止整棵进程树。
        # 隐藏窗口可以避免 GUI 运行命令时弹出额外的控制台窗口。
        return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                # /T 会连同子进程一起终止。
                # 单独调用 process.kill() 通常只能终止主进程。
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                    check=False,
                    shell=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            process.kill()
        finally:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
