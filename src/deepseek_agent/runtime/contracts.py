"""运行时使用的业务扩展与不可变轮次上下文；不依赖具体业务。"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RunContext:
    profile_id: str
    session_id: str
    run_id: str
    turn_id: str
    user_message: str


class BusinessContext(Protocol):
    def bind_turn(self, context: RunContext) -> None: ...
    def runtime_context(self) -> str: ...
