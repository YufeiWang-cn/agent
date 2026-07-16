from dataclasses import dataclass, field


# 定义一个类型别名
Message = dict[str, str]


@dataclass(slots=True)
class Conversation:
    system_prompt: str
    messages: list[Message] = field(init=False)  # init=False：创建对象时不允许外部传入messages，它由类内部初始化

    # 是 Python 数据类（dataclasses）中的一个特殊方法
    # 它会在 __init__ 方法执行后自动调用，适用于需要在初始化后执行额外逻辑的场景
    # 例如属性验证、派生属性计算或动态设置默认值
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
