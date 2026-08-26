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


def _read_int_env(
    name: str,
    default: int,
    *,
    minimum: int,
    error_message: str,
) -> int:
    """读取整数环境变量，并使用统一规则校验允许的下限。"""
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError as error:
        raise RuntimeError(error_message) from error
    if value < minimum:
        raise RuntimeError(error_message)
    return value


def _read_float_env(
    name: str,
    default: float,
    *,
    minimum: float,
    allow_minimum: bool,
    error_message: str,
) -> float:
    """读取浮点环境变量，并明确边界值是否可以使用。"""
    try:
        value = float(os.getenv(name, str(default)).strip())
    except ValueError as error:
        raise RuntimeError(error_message) from error
    invalid = value < minimum if allow_minimum else value <= minimum
    if invalid:
        raise RuntimeError(error_message)
    return value


def _read_log_level() -> str:
    """读取标准日志级别，并拒绝 logging 不识别的自定义值。"""
    value = os.getenv("AGENT_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper()
    if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise RuntimeError("AGENT_LOG_LEVEL 是无效的日志级别。")
    return value


def _resolve_workspace_root() -> Path:
    """解析工作区真实路径，确保后续安全检查使用稳定目录。"""
    workspace_root = Path(
        os.getenv("AGENT_WORKSPACE", str(PROJECT_ROOT)).strip()
    ).expanduser()
    try:
        resolved = workspace_root.resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"AGENT_WORKSPACE 不可用：{workspace_root}") from error
    if not resolved.is_dir():
        raise RuntimeError(f"AGENT_WORKSPACE 不是文件夹：{resolved}")
    return resolved


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

        max_context_tokens = _read_int_env(
            "DEEPSEEK_MAX_CONTEXT_TOKENS",
            DEFAULT_MAX_CONTEXT_TOKENS,
            minimum=1,
            error_message="DEEPSEEK_MAX_CONTEXT_TOKENS 必须是正整数。",
        )
        request_timeout = _read_float_env(
            "DEEPSEEK_REQUEST_TIMEOUT",
            DEFAULT_REQUEST_TIMEOUT,
            minimum=0,
            allow_minimum=False,
            error_message="DEEPSEEK_REQUEST_TIMEOUT 必须大于 0。",
        )
        max_retries = _read_int_env(
            "DEEPSEEK_MAX_RETRIES",
            DEFAULT_MAX_RETRIES,
            minimum=0,
            error_message="DEEPSEEK_MAX_RETRIES 不能小于 0。",
        )
        retry_base_delay = _read_float_env(
            "DEEPSEEK_RETRY_BASE_DELAY",
            DEFAULT_RETRY_BASE_DELAY,
            minimum=0,
            allow_minimum=True,
            error_message="DEEPSEEK_RETRY_BASE_DELAY 不能小于 0。",
        )
        max_file_size = _read_int_env(
            "AGENT_MAX_FILE_SIZE",
            DEFAULT_MAX_FILE_SIZE,
            minimum=1,
            error_message="AGENT_MAX_FILE_SIZE 必须是正整数。",
        )
        command_timeout = _read_float_env(
            "AGENT_COMMAND_TIMEOUT",
            DEFAULT_COMMAND_TIMEOUT,
            minimum=0,
            allow_minimum=False,
            error_message="AGENT_COMMAND_TIMEOUT 必须大于 0。",
        )
        max_command_output = _read_int_env(
            "AGENT_MAX_COMMAND_OUTPUT",
            DEFAULT_MAX_COMMAND_OUTPUT,
            minimum=1,
            error_message="AGENT_MAX_COMMAND_OUTPUT 必须是正整数。",
        )
        max_agent_steps = _read_int_env(
            "AGENT_MAX_STEPS",
            DEFAULT_MAX_AGENT_STEPS,
            minimum=1,
            error_message="AGENT_MAX_STEPS 必须是正整数。",
        )

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
            log_level=_read_log_level(),
            workspace_root=_resolve_workspace_root(),
            max_file_size=max_file_size,
            command_timeout=command_timeout,
            max_command_output=max_command_output,
            max_agent_steps=max_agent_steps,
        )
