from collections.abc import Iterable, Sequence

from openai import OpenAI

from ..config import Settings
from ..conversation import Message


class DeepSeekModel:
    def __init__(self, settings: Settings) -> None:
        # 属性名前面的_表示内部实现，不建议外部直接访问
        self._model_name = settings.model
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def stream(self, messages: Sequence[Message]) -> Iterable[str]:
        response = self._client.chat.completions.create(
            model=self._model_name,
            messages=list(messages),
            stream=True,
        )

        for chunk in response:
            content = chunk.choices[0].delta.content
            if content:
                yield content
