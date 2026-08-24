"""读取、校验并集中保存 Agent 的运行配置。"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system.md"
DEFAULT_MAX_CONTEXT_TOKENS = 8_000
DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 1.0
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_MAX_FILE_SIZE = 100_000
DEFAULT_COMMAND_TIMEOUT = 120.0
DEFAULT_MAX_COMMAND_OUTPUT = 50_000
DEFAULT_MAX_AGENT_STEPS = 12


@dataclass(frozen=True, slots=True)
class Settings:
    """保存已经校验的不可变配置，避免运行期间意外修改关键参数。"""
    api_key: str
    base_url: str
    model: str
    system_prompt: str
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY
    log_level: str = DEFAULT_LOG_LEVEL
    workspace_root: Path = PROJECT_ROOT
    max_file_size: int = DEFAULT_MAX_FILE_SIZE
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT
    max_command_output: int = DEFAULT_MAX_COMMAND_OUTPUT
    max_agent_steps: int = DEFAULT_MAX_AGENT_STEPS

    @classmethod
    def from_env(cls) -> "Settings":
        """从项目的 ``.env`` 和环境变量加载配置，并验证取值范围。"""
        load_dotenv(PROJECT_ROOT / ".env")

        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("请先在 .env 中设置 DEEPSEEK_API_KEY。")

        raw_max_context_tokens = os.getenv(
            "DEEPSEEK_MAX_CONTEXT_TOKENS",
            str(DEFAULT_MAX_CONTEXT_TOKENS),
        ).strip()
        try:
            max_context_tokens = int(raw_max_context_tokens)
        except ValueError as error:
            raise RuntimeError(
                "DEEPSEEK_MAX_CONTEXT_TOKENS 必须是正整数。"
            ) from error
        if max_context_tokens <= 0:
            raise RuntimeError("DEEPSEEK_MAX_CONTEXT_TOKENS 必须是正整数。")

        try:
            request_timeout = float(
                os.getenv(
                    "DEEPSEEK_REQUEST_TIMEOUT",
                    str(DEFAULT_REQUEST_TIMEOUT),
                ).strip()
            )
            max_retries = int(
                os.getenv(
                    "DEEPSEEK_MAX_RETRIES",
                    str(DEFAULT_MAX_RETRIES),
                ).strip()
            )
            retry_base_delay = float(
                os.getenv(
                    "DEEPSEEK_RETRY_BASE_DELAY",
                    str(DEFAULT_RETRY_BASE_DELAY),
                ).strip()
            )
        except ValueError as error:
            raise RuntimeError("模型超时和重试配置必须是数字。") from error

        if request_timeout <= 0:
            raise RuntimeError("DEEPSEEK_REQUEST_TIMEOUT 必须大于 0。")
        if max_retries < 0:
            raise RuntimeError("DEEPSEEK_MAX_RETRIES 不能小于 0。")
        if retry_base_delay < 0:
            raise RuntimeError("DEEPSEEK_RETRY_BASE_DELAY 不能小于 0。")

        log_level = os.getenv("AGENT_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise RuntimeError("AGENT_LOG_LEVEL 是无效的日志级别。")

        workspace_root = Path(
            os.getenv("AGENT_WORKSPACE", str(PROJECT_ROOT)).strip()
        ).expanduser()
        try:
            workspace_root = workspace_root.resolve(strict=True)
        except OSError as error:
            raise RuntimeError(f"AGENT_WORKSPACE 不可用：{workspace_root}") from error
        if not workspace_root.is_dir():
            raise RuntimeError(f"AGENT_WORKSPACE 不是文件夹：{workspace_root}")

        try:
            max_file_size = int(
                os.getenv(
                    "AGENT_MAX_FILE_SIZE",
                    str(DEFAULT_MAX_FILE_SIZE),
                ).strip()
            )
        except ValueError as error:
            raise RuntimeError("AGENT_MAX_FILE_SIZE 必须是正整数。") from error
        if max_file_size <= 0:
            raise RuntimeError("AGENT_MAX_FILE_SIZE 必须是正整数。")

        try:
            command_timeout = float(
                os.getenv(
                    "AGENT_COMMAND_TIMEOUT",
                    str(DEFAULT_COMMAND_TIMEOUT),
                ).strip()
            )
            max_command_output = int(
                os.getenv(
                    "AGENT_MAX_COMMAND_OUTPUT",
                    str(DEFAULT_MAX_COMMAND_OUTPUT),
                ).strip()
            )
        except ValueError as error:
            raise RuntimeError("命令超时和输出限制必须是数字。") from error
        if command_timeout <= 0:
            raise RuntimeError("AGENT_COMMAND_TIMEOUT 必须大于 0。")
        if max_command_output <= 0:
            raise RuntimeError("AGENT_MAX_COMMAND_OUTPUT 必须是正整数。")

        try:
            max_agent_steps = int(
                os.getenv(
                    "AGENT_MAX_STEPS",
                    str(DEFAULT_MAX_AGENT_STEPS),
                ).strip()
            )
        except ValueError as error:
            raise RuntimeError("AGENT_MAX_STEPS 必须是正整数。") from error
        if max_agent_steps <= 0:
            raise RuntimeError("AGENT_MAX_STEPS 必须是正整数。")

        return cls(
            api_key=api_key,
            base_url=os.getenv(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).strip(),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro").strip(),
            system_prompt=DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").strip(),
            max_context_tokens=max_context_tokens,
            request_timeout=request_timeout,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            log_level=log_level,
            workspace_root=workspace_root,
            max_file_size=max_file_size,
            command_timeout=command_timeout,
            max_command_output=max_command_output,
            max_agent_steps=max_agent_steps,
        )
