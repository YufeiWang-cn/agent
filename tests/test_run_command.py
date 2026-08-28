"""验证受控命令工具的白名单、确认、工作区和资源限制。"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.models import ToolCallRequest
from deepseek_agent.tool_execution import ToolExecutionStatus, ToolExecutor
from deepseek_agent.tools import (
    DockerCommandExecutor,
    RunCommandTool,
    ToolEffect,
    ToolExecutionContext,
    ToolExecutionError,
    ToolRegistry,
)
from deepseek_agent.workspace import WorkspaceGuard


class RecordingConfirmer:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls = 0

    def confirm(self, _tool, _arguments: str) -> bool:
        self.calls += 1
        return self.allowed


class RunCommandToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name) / "workspace"
        self.workspace.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", str(self.workspace)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.tool = RunCommandTool(
            WorkspaceGuard(self.workspace, 10_000),
            timeout_seconds=2,
            max_output_bytes=100,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_python_version_runs_without_shell_and_returns_json(self) -> None:
        result = json.loads(
            self.tool.execute({"command": ["python", "--version"]})
        )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["cwd"], ".")
        self.assertIn("Python", result["stdout"] + result["stderr"])
        self.assertFalse(result["stdout_truncated"])
        self.assertFalse(result["stderr_truncated"])
        self.assertEqual(result["execution_mode"], "local")

    def test_dynamic_policy_only_auto_allows_information_commands(self) -> None:
        git_status = {"command": ["git", "status", "--short"]}
        tests = {"command": ["python", "-m", "unittest"]}

        self.assertFalse(self.tool.requires_confirmation_for(git_status))
        self.assertEqual(self.tool.effect_for(git_status), ToolEffect.READ_ONLY)
        self.assertTrue(self.tool.requires_confirmation_for(tests))
        self.assertEqual(
            self.tool.effect_for(tests),
            ToolEffect.EXTERNAL_SIDE_EFFECT,
        )

    def test_executor_uses_dynamic_confirmation_and_effect(self) -> None:
        confirmer = RecordingConfirmer(allowed=False)
        executor = ToolExecutor(ToolRegistry([self.tool]), confirmer)
        request = ToolCallRequest(
            id="call_test",
            name="run_command",
            arguments='{"command":["python","-m","unittest"]}',
        )

        record = executor.execute(request)

        self.assertEqual(record.status, ToolExecutionStatus.REJECTED)
        self.assertEqual(record.effect, ToolEffect.EXTERNAL_SIDE_EFFECT)
        self.assertEqual(confirmer.calls, 1)
        self.assertFalse(record.execution_started)

    def test_shells_operators_and_inline_python_are_rejected(self) -> None:
        rejected = (
            ["cmd", "/c", "dir"],
            ["powershell", "-Command", "Get-ChildItem"],
            ["python", "-c", "print('unsafe')"],
            ["git", "status", "&&", "git", "log"],
        )
        for command in rejected:
            with self.subTest(command=command):
                with self.assertRaises(ToolExecutionError):
                    self.tool.execute({"command": command})

    def test_destructive_git_and_workspace_escape_are_rejected(self) -> None:
        rejected = (
            {"command": ["git", "reset", "--hard"]},
            {"command": ["git", "clean", "-fd"]},
            {"command": ["git", "status"], "cwd": ".."},
            {"command": ["git", "show", "--output=../leak.txt"]},
            {"command": ["git", "-C", "..", "status"]},
        )
        for arguments in rejected:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ToolExecutionError):
                    self.tool.execute(arguments)

    def test_git_repository_cannot_extend_outside_workspace(self) -> None:
        repository = Path(self.temporary_directory.name) / "outer-repository"
        nested_workspace = repository / "nested"
        nested_workspace.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "--quiet", str(repository)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tool = RunCommandTool(WorkspaceGuard(nested_workspace, 1_000))

        with self.assertRaisesRegex(ToolExecutionError, "工作区"):
            tool.execute({"command": ["git", "status"]})

    def test_output_is_capped_without_blocking_the_child(self) -> None:
        script = self.workspace / "large_output.py"
        script.write_text("print('x' * 1000)", encoding="utf-8")

        result = json.loads(
            self.tool.execute({"command": ["python", "large_output.py"]})
        )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(result["stdout"].encode("utf-8")), 100)
        self.assertTrue(result["stdout_truncated"])

    def test_sensitive_environment_values_are_not_inherited(self) -> None:
        script = self.workspace / "environment.py"
        script.write_text(
            "import os\nprint(os.getenv('DEEPSEEK_API_KEY', 'missing'))\n",
            encoding="utf-8",
        )

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "private-value"}):
            result = json.loads(
                self.tool.execute({"command": ["python", "environment.py"]})
            )

        self.assertEqual(result["stdout"].strip(), "missing")
        self.assertNotIn("private-value", json.dumps(result))

    def test_timeout_and_cancellation_terminate_the_process(self) -> None:
        script = self.workspace / "wait.py"
        script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
        timeout_tool = RunCommandTool(
            WorkspaceGuard(self.workspace, 1_000),
            timeout_seconds=0.2,
        )

        with self.assertRaises(ToolExecutionError) as timeout:
            timeout_tool.execute({"command": ["python", "wait.py"]})
        self.assertTrue(timeout.exception.side_effect_possible)

        with self.assertRaises(ToolExecutionError) as cancelled:
            self.tool.execute_with_context(
                {"command": ["python", "wait.py"]},
                ToolExecutionContext(should_cancel=lambda: True),
            )
        self.assertTrue(cancelled.exception.side_effect_possible)

    def test_docker_mode_fails_closed_when_backend_is_missing(self) -> None:
        executor = DockerCommandExecutor("python:3.10-slim")

        with patch(
            "deepseek_agent.tools.command_executor.shutil.which",
            return_value=None,
        ):
            with self.assertRaisesRegex(ToolExecutionError, "Docker"):
                executor.preflight()

    def test_docker_command_mounts_only_workspace_with_hardening_flags(self) -> None:
        (self.workspace / ".env").write_text("SECRET=value", encoding="utf-8")
        (self.workspace / "source.py").write_text("value = 1", encoding="utf-8")
        internal_state = self.workspace / "data" / "sessions"
        internal_state.mkdir(parents=True)
        (internal_state / "session.json").write_text("{}", encoding="utf-8")
        executor = DockerCommandExecutor("python:3.10-slim")
        completed = subprocess.CompletedProcess([], 0)
        with (
            patch(
                "deepseek_agent.tools.command_executor.shutil.which",
                return_value="docker",
            ),
            patch(
                "deepseek_agent.tools.command_executor.subprocess.run",
                return_value=completed,
            ),
        ):
            prepared = executor.prepare(
                (sys.executable, "escape.py"),
                effect=ToolEffect.EXTERNAL_SIDE_EFFECT,
                workspace=self.workspace,
                cwd=self.workspace,
                environment={"PATH": "safe"},
            )

        command = list(prepared.argv)
        self.assertIn("--network", command)
        self.assertIn("none", command)
        self.assertIn("--read-only", command)
        self.assertIn("no-new-privileges:true", command)
        joined = " ".join(command)
        self.assertIn("type=bind,source=", joined)
        self.assertNotIn(str(self.workspace), joined)
        self.assertEqual(command[-2:], ["python:3.10-slim", "escape.py"])
        self.assertIsNotNone(prepared.container_name)
        self.assertIsNotNone(prepared.staging_directory)
        self.assertFalse((prepared.cwd / ".env").exists())
        self.assertFalse((prepared.cwd / ".git").exists())
        self.assertFalse((prepared.cwd / "data" / "sessions").exists())
        self.assertTrue((prepared.cwd / "source.py").is_file())
        (prepared.cwd / "source.py").write_text("value = 2", encoding="utf-8")
        self.assertEqual(
            (self.workspace / "source.py").read_text(encoding="utf-8"),
            "value = 1",
        )
        with patch(
            "deepseek_agent.tools.command_executor.subprocess.run",
            return_value=completed,
        ):
            executor.cleanup(prepared)
        self.assertFalse((prepared.staging_directory or Path()).exists())

    def test_docker_mode_rejects_git_instead_of_running_it_on_host(self) -> None:
        executor = DockerCommandExecutor("python:3.10-slim")
        completed = subprocess.CompletedProcess([], 0)
        with (
            patch(
                "deepseek_agent.tools.command_executor.shutil.which",
                return_value="docker",
            ),
            patch(
                "deepseek_agent.tools.command_executor.subprocess.run",
                return_value=completed,
            ),
        ):
            with self.assertRaisesRegex(ToolExecutionError, "Git"):
                executor.prepare(
                    ("git", "status", "--short"),
                    effect=ToolEffect.READ_ONLY,
                    workspace=self.workspace,
                    cwd=self.workspace,
                    environment={"PATH": "safe"},
                )

    def test_docker_cleanup_fails_when_container_is_still_running(self) -> None:
        executor = DockerCommandExecutor("python:3.10-slim")
        completed = subprocess.CompletedProcess([], 0)
        with (
            patch(
                "deepseek_agent.tools.command_executor.shutil.which",
                return_value="docker",
            ),
            patch(
                "deepseek_agent.tools.command_executor.subprocess.run",
                return_value=completed,
            ),
        ):
            prepared = executor.prepare(
                (sys.executable, "escape.py"),
                effect=ToolEffect.EXTERNAL_SIDE_EFFECT,
                workspace=self.workspace,
                cwd=self.workspace,
                environment={"PATH": "safe"},
            )

        with patch(
            "deepseek_agent.tools.command_executor.subprocess.run",
            side_effect=(
                subprocess.CompletedProcess([], 1),
                subprocess.CompletedProcess([], 0, stdout=b"container-id\n"),
            ),
        ):
            with self.assertRaises(ToolExecutionError) as raised:
                executor.cleanup(prepared)
        self.assertTrue(raised.exception.side_effect_possible)
        self.assertTrue((prepared.staging_directory or Path()).exists())

        with patch(
            "deepseek_agent.tools.command_executor.subprocess.run",
            return_value=completed,
        ):
            executor.cleanup(prepared)
        self.assertFalse((prepared.staging_directory or Path()).exists())

    @unittest.skipUnless(shutil.which("docker"), "需要 Docker 运行真实隔离测试")
    def test_docker_blocks_python_from_writing_to_workspace_sibling(self) -> None:
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        script = self.workspace / "escape.py"
        script.write_text(
            "from pathlib import Path\n"
            "try:\n"
            "    Path('../outside/sentinel.txt').write_text('changed')\n"
            "except OSError:\n"
            "    print('blocked')\n"
            "else:\n"
            "    print('escaped')\n",
            encoding="utf-8",
        )
        executor = DockerCommandExecutor("python:3.10-slim")
        try:
            executor.preflight()
        except ToolExecutionError as error:
            self.skipTest(str(error))
        tool = RunCommandTool(
            WorkspaceGuard(self.workspace, 10_000),
            timeout_seconds=30,
            command_executor=executor,
        )

        result = json.loads(tool.execute({"command": ["python", "escape.py"]}))

        self.assertEqual(result["exit_code"], 0)
        self.assertIn("blocked", result["stdout"])
        self.assertNotIn("escaped", result["stdout"])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")


if __name__ == "__main__":
    unittest.main()
