"""为受控命令提供可信本地执行与失败关闭的容器隔离。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..workspace.guard import DEFAULT_BLOCKED_NAMES, DEFAULT_BLOCKED_PREFIXES
from .base import ToolEffect, ToolExecutionError


LOCAL_EXECUTION_MODE = "local"
DOCKER_EXECUTION_MODE = "docker"
SUPPORTED_EXECUTION_MODES = frozenset(
    {LOCAL_EXECUTION_MODE, DOCKER_EXECUTION_MODE}
)
DEFAULT_CONTAINER_IMAGE = "python:3.10-slim"
SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


@dataclass(frozen=True, slots=True)
class PreparedCommand:
    """保存宿主机实际启动的命令及其清理信息。"""

    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    container_name: str | None = None
    staging_directory: Path | None = None


class CommandExecutor:
    """定义命令执行环境的最小接口。"""

    mode: str

    def preflight(self) -> None:
        """在执行任务前验证后端是否可用。"""

    def prepare(
        self,
        argv: tuple[str, ...],
        *,
        effect: ToolEffect,
        workspace: Path,
        cwd: Path,
        environment: dict[str, str],
    ) -> PreparedCommand:
        """把已校验的业务命令转换为宿主机命令。"""
        raise NotImplementedError

    def cleanup(self, command: PreparedCommand) -> None:
        """清理中断后可能残留的执行环境。"""


class LocalCommandExecutor(CommandExecutor):
    """在当前用户权限下执行命令，仅适用于可信工作区。"""

    mode = LOCAL_EXECUTION_MODE

    def prepare(
        self,
        argv: tuple[str, ...],
        *,
        effect: ToolEffect,
        workspace: Path,
        cwd: Path,
        environment: dict[str, str],
    ) -> PreparedCommand:
        del effect, workspace
        return PreparedCommand(argv, cwd, environment)


class DockerCommandExecutor(CommandExecutor):
    """只向容器挂载工作区，阻止命令访问其他宿主机路径。"""

    mode = DOCKER_EXECUTION_MODE

    def __init__(self, image: str = DEFAULT_CONTAINER_IMAGE) -> None:
        self._image = self._validate_image(image)
        self._docker: str | None = None
        self._ready = False

    @property
    def image(self) -> str:
        return self._image

    def preflight(self) -> None:
        if self._ready:
            return
        docker = shutil.which("docker")
        if docker is None:
            raise ToolExecutionError(
                "容器隔离不可用：系统 PATH 中未找到 Docker。"
            )
        environment = self._docker_client_environment()
        self._run_preflight(
            [docker, "version", "--format", "{{.Server.Version}}"],
            environment,
            "Docker 服务不可用，请先启动 Docker Desktop。",
        )
        self._run_preflight(
            [docker, "image", "inspect", self._image],
            environment,
            (
                f"隔离镜像不存在：{self._image}。请先显式拉取该镜像；"
                "评测器不会自动联网下载。"
            ),
        )
        self._docker = docker
        self._ready = True

    def prepare(
        self,
        argv: tuple[str, ...],
        *,
        effect: ToolEffect,
        workspace: Path,
        cwd: Path,
        environment: dict[str, str],
    ) -> PreparedCommand:
        self.preflight()
        del effect
        docker = self._docker
        if docker is None:  # pragma: no cover - 由 preflight 保证
            raise ToolExecutionError("容器隔离初始化失败。")

        executable = Path(argv[0]).name.lower().removesuffix(".exe")
        if executable == "git":
            raise ToolExecutionError(
                "容器隔离模式不执行 Git 命令；请使用文件工具，"
                "或由用户在可信环境中手动执行 Git。"
            )
        if not executable.startswith("python"):
            raise ToolExecutionError(
                "容器隔离模式当前只支持 Python 命令和只读 Git 查询。"
            )

        workspace = workspace.resolve(strict=True)
        cwd = cwd.resolve(strict=True)
        try:
            relative_cwd = cwd.relative_to(workspace)
        except ValueError as error:
            raise ToolExecutionError("容器工作目录位于工作区之外。") from error
        staging_directory = Path(
            tempfile.mkdtemp(prefix="deepseek-agent-command-")
        )
        sandbox_workspace = staging_directory / "workspace"
        try:
            shutil.copytree(
                workspace,
                sandbox_workspace,
                symlinks=True,
                ignore=self._ignored_entries(workspace),
            )
        except (OSError, ValueError) as error:
            self._remove_staging_directory(staging_directory)
            raise ToolExecutionError(f"无法准备隔离工作区副本：{error}") from error
        if "," in str(sandbox_workspace):
            self._remove_staging_directory(staging_directory)
            raise ToolExecutionError("Docker 隔离暂不支持路径中包含逗号的工作区。")

        container_name = f"deepseek-agent-{uuid4().hex}"
        mount = f"type=bind,source={sandbox_workspace},target=/workspace"
        container_cwd = Path("/workspace", *relative_cwd.parts).as_posix()
        command: list[str] = [
            docker,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "64",
            "--memory",
            "512m",
            "--cpus",
            "1.0",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            mount,
            "--workdir",
            container_cwd,
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--env",
            "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "--env",
            "NO_COLOR=1",
            "--entrypoint",
            "python",
        ]
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        if os.name != "nt" and callable(getuid) and callable(getgid):
            command.extend(["--user", f"{getuid()}:{getgid()}"])
        command.extend([self._image, *argv[1:]])
        return PreparedCommand(
            tuple(command),
            sandbox_workspace,
            self._docker_client_environment(environment),
            container_name,
            staging_directory,
        )

    def cleanup(self, command: PreparedCommand) -> None:
        if command.container_name is not None and self._docker is not None:
            environment = self._docker_client_environment(command.environment)
            try:
                removed = subprocess.run(
                    [self._docker, "rm", "--force", command.container_name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    shell=False,
                    creationflags=self._creation_flags(),
                    env=environment,
                )
                if removed.returncode != 0:
                    listed = subprocess.run(
                        [
                            self._docker,
                            "container",
                            "ls",
                            "--all",
                            "--quiet",
                            "--filter",
                            f"name=^/{command.container_name}$",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                        shell=False,
                        creationflags=self._creation_flags(),
                        env=environment,
                    )
                    if listed.returncode != 0 or (listed.stdout or b"").strip():
                        raise ToolExecutionError(
                            "无法确认隔离容器已经终止，命令结果状态未知。",
                            side_effect_possible=True,
                        )
            except ToolExecutionError:
                raise
            except (OSError, subprocess.SubprocessError) as error:
                raise ToolExecutionError(
                    "无法确认隔离容器已经终止，命令结果状态未知。",
                    side_effect_possible=True,
                ) from error
        if command.staging_directory is not None:
            self._remove_staging_directory(command.staging_directory)

    @staticmethod
    def _ignored_entries(
        workspace: Path,
    ) -> Callable[[str, list[str]], set[str]]:
        """返回 copytree 过滤器，避免把 Agent 敏感路径挂载进容器。"""

        def ignore(directory: str, names: list[str]) -> set[str]:
            relative_directory = Path(directory).resolve().relative_to(workspace)
            ignored: set[str] = set()
            for name in names:
                relative = relative_directory / name
                lowered = Path(*(part.lower() for part in relative.parts))
                if name.lower() in DEFAULT_BLOCKED_NAMES or any(
                    lowered == prefix or prefix in lowered.parents
                    for prefix in DEFAULT_BLOCKED_PREFIXES
                ):
                    ignored.add(name)
            return ignored

        return ignore

    @staticmethod
    def _remove_staging_directory(directory: Path) -> None:
        """只删除本模块在系统临时目录中创建的唯一目录。"""
        temporary_root = Path(tempfile.gettempdir()).resolve()
        try:
            resolved = directory.resolve(strict=True)
        except OSError:
            return
        if (
            resolved.parent != temporary_root
            or not resolved.name.startswith("deepseek-agent-command-")
        ):
            return
        if resolved.is_symlink():
            resolved.unlink(missing_ok=True)
        else:
            shutil.rmtree(resolved, ignore_errors=True)

    @staticmethod
    def _validate_image(image: str) -> str:
        value = image.strip()
        if (
            not value
            or len(value) > 255
            or value.startswith("-")
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError("Docker 隔离镜像名称无效。")
        return value

    @classmethod
    def _docker_client_environment(
        cls,
        source: dict[str, str] | None = None,
    ) -> dict[str, str]:
        values = os.environ if source is None else source
        return {
            key: value
            for key, value in values.items()
            if not any(marker in key.upper() for marker in SENSITIVE_ENV_MARKERS)
        }

    @classmethod
    def _run_preflight(
        cls,
        argv: list[str],
        environment: dict[str, str],
        error_message: str,
    ) -> None:
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                shell=False,
                creationflags=cls._creation_flags(),
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ToolExecutionError(error_message) from error
        if completed.returncode != 0:
            raise ToolExecutionError(error_message)

    @staticmethod
    def _creation_flags() -> int:
        if os.name != "nt":
            return 0
        return subprocess.CREATE_NO_WINDOW


def build_command_executor(
    mode: str,
    image: str = DEFAULT_CONTAINER_IMAGE,
) -> CommandExecutor:
    """按已校验的配置构造执行后端。"""
    if mode == LOCAL_EXECUTION_MODE:
        return LocalCommandExecutor()
    if mode == DOCKER_EXECUTION_MODE:
        return DockerCommandExecutor(image)
    raise ValueError(f"不支持的命令执行模式：{mode}")


__all__ = [
    "CommandExecutor",
    "DEFAULT_CONTAINER_IMAGE",
    "DOCKER_EXECUTION_MODE",
    "DockerCommandExecutor",
    "LOCAL_EXECUTION_MODE",
    "LocalCommandExecutor",
    "PreparedCommand",
    "SUPPORTED_EXECUTION_MODES",
    "build_command_executor",
]
