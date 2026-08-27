"""在安全工作区内以无 Shell 方式运行受控命令。"""

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, cast

from ..workspace import WorkspaceAccessError, WorkspaceGuard
from .base import (
    JsonObject,
    Tool,
    ToolEffect,
    ToolExecutionContext,
    ToolExecutionError,
)
from .command_policy import (
    CommandPolicy,
    CommandPolicyResolver,
    MAX_COMMAND_ARGUMENTS,
)


POLL_INTERVAL_SECONDS = 0.1
SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


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
        self.timeout_seconds: float = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._policy_resolver = CommandPolicyResolver()

    def requires_confirmation_for(self, arguments: JsonObject) -> bool:
        # ToolExecutor 会在执行前调用本方法。
        # 直接调用 execute() 不会弹出确认框。
        return self._policy_resolver.resolve(arguments).requires_confirmation

    def effect_for(self, arguments: JsonObject) -> ToolEffect:
        return self._policy_resolver.resolve(arguments).effect

    def execute(self, arguments: JsonObject) -> str:
        return self.execute_with_context(arguments, ToolExecutionContext())

    def execute_with_context(
        self,
        arguments: JsonObject,
        context: ToolExecutionContext,
    ) -> str:
        # 执行阶段再次生成策略，避免调用方绕过 ToolExecutor 后跳过白名单校验。
        policy = self._policy_resolver.resolve(arguments)
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

            local_deadline = started_at + self.timeout_seconds
            while process.poll() is None:
                if context.cancellation_requested:
                    self._terminate_process(process)
                    # 写入类命令被中断时，系统无法断定它是否已经生效。
                    # 上层必须按照 RESULT_UNKNOWN 状态和副作用保留规则处理。
                    raise ToolExecutionError(
                        "命令执行已取消。",
                        side_effect_possible=policy.effect is not ToolEffect.READ_ONLY,
                    )
                if context.timed_out or time.monotonic() >= local_deadline:
                    self._terminate_process(process)
                    raise ToolExecutionError(
                        f"命令执行超过 {self.timeout_seconds:g} 秒，已终止。",
                        side_effect_possible=policy.effect is not ToolEffect.READ_ONLY,
                    )
                time.sleep(POLL_INTERVAL_SECONDS)

            stdout, stdout_truncated = self._read_output(
                cast(BinaryIO, stdout_file)
            )
            stderr, stderr_truncated = self._read_output(
                cast(BinaryIO, stderr_file)
            )

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

    def _resolve_cwd(self, arguments: JsonObject) -> Path:
        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, str):
            raise ToolExecutionError("cwd 必须是相对于工作目录的字符串。")
        try:
            return self._guard.resolve_directory(cwd)
        except WorkspaceAccessError as error:
            raise ToolExecutionError(str(error)) from error

    def _validate_git_repository(self, policy: CommandPolicy, cwd: Path) -> None:
        if Path(policy.argv[0]).name.lower().removesuffix(".exe") != "git":
            if len(policy.argv) >= 2 and policy.argv[1].lower().endswith(".py"):
                script_path = (cwd / policy.argv[1]).resolve(strict=False)
                script_is_valid = (
                    self._guard.is_accessible(script_path) and script_path.is_file()
                )
                if not script_is_valid:
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
                kill_process_group = getattr(os, "killpg", None)
                kill_signal = getattr(signal, "SIGKILL", None)
                if kill_process_group is None or kill_signal is None:
                    process.kill()
                else:
                    kill_process_group(process.pid, kill_signal)
        except (OSError, subprocess.SubprocessError):
            process.kill()
        finally:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
