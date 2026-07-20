import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system.md"
DEFAULT_MAX_CONTEXT_TOKENS = 8_000


@dataclass(frozen=True, slots=True)
class Settings:
    '''
    @dataclass(frozen=True, slots=True)将Settings类变成数据类\n
    frozen=True：创建后不允许修改配置，防止运行过程中误改Key或模型\n
    slots=True：限制对象只能包含定义过的属性，并减少少量内存使用
    '''
    api_key: str
    base_url: str
    model: str
    system_prompt: str
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS

    @classmethod
    def from_env(cls) -> "Settings":
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

        return cls(
            api_key=api_key,
            base_url=os.getenv(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).strip(),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro").strip(),
            system_prompt=DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").strip(),
            max_context_tokens=max_context_tokens,
        )
