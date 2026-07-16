from dataclasses import dataclass, field


Message = dict[str, str]


@dataclass(slots=True)
class Conversation:
    system_prompt: str
    messages: list[Message] = field(init=False)

    def __post_init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def remove_last_user(self) -> None:
        if self.messages[-1]["role"] == "user":
            self.messages.pop()

    def visible_history(self) -> list[Message]:
        return self.messages[1:]
