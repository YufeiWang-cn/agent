from collections.abc import Iterable, Sequence
from typing import Protocol

from ..conversation import Message


class ChatModel(Protocol):
    @property
    def model_name(self) -> str: ...

    def stream(self, messages: Sequence[Message]) -> Iterable[str]: ...
