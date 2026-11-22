"""离线模型仅读取预设动作和真实工具返回值，绝不读取 expected。"""

import json
from typing import Any
from collections.abc import Iterable, Sequence

from ..conversation import Message
from ..models import StreamEvent, ToolCallRequest, TextDelta
from ..tools import Tool


class CareerScriptModel:
    model_name = "offline-career-script"

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.actions: list[dict[str, Any]] = []
        self.pending: str | None = None
        self.answer = ""
        self.fail = False
        self.serial = 0

    def begin(self, actions: list[dict[str, Any]], answer: str, fail: bool = False) -> None:
        self.actions = list(actions)
        self.pending = None
        self.answer = answer
        self.fail = fail

    def resolve(self, value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                pieces = value["$ref"].split(".")
                result = self.values[pieces[0]]
                for piece in pieces[1:]:
                    result = result[piece]
                return result
            return {key: self.resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        return value

    def stream(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]
    ) -> Iterable[StreamEvent]:
        if self.fail:
            self.fail = False
            raise RuntimeError("offline injected model failure")
        if self.pending is not None:
            self.values[self.pending] = json.loads(str(messages[-1]["content"]))
            self.pending = None
        if self.actions:
            action = self.actions.pop(0)
            self.serial += 1
            self.pending = action.get("bind")
            yield ToolCallRequest(
                str(self.serial),
                action["tool"],
                json.dumps(self.resolve(action["arguments"]), ensure_ascii=False),
            )
        else:
            yield TextDelta(self.answer)


class OperationConfirmer:
    def __init__(self) -> None:
        self.policy: dict[str, bool] = {}
        self.decisions: list[dict[str, Any]] = []

    def confirm(self, tool: Tool, arguments: str) -> bool:
        parsed = json.loads(arguments)
        key = tool.name + ("." + parsed["action"] if "action" in parsed else "")
        allowed = self.policy.get(key, False)
        self.decisions.append(dict(operation=key, allowed=allowed))
        return allowed
