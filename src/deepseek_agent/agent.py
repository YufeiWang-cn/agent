from .config import Settings
from .conversation import Conversation, Message
from .models import ChatModel, DeepSeekModel, TextDelta, ToolCallRequest
from .tools import ToolError, ToolRegistry, build_default_registry


EXIT_COMMANDS = {"/exit", "exit", "quit", "q", "退出"}
MAX_AGENT_STEPS = 5


class Agent:
    def __init__(
        self,
        settings: Settings,
        model: ChatModel | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        self._model = model or DeepSeekModel(settings)
        self._tools = tools if tools is not None else build_default_registry()
        self._conversation = Conversation(settings.system_prompt)
        self._running = True

    def run(self) -> None:
        self._print_welcome()

        while self._running:
            try:
                prompt = input("\n你：").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n对话已结束。")
                break

            if not prompt:
                continue

            if self._handle_command(prompt):
                continue

            self._chat(prompt)

    def _chat(self, prompt: str) -> None:
        checkpoint = len(self._conversation)
        self._conversation.add_user(prompt)

        try:
            for _step in range(MAX_AGENT_STEPS):
                answer_parts: list[str] = []
                tool_requests: list[ToolCallRequest] = []
                started_output = False

                for event in self._model.stream(
                    self._conversation.messages,
                    self._tools.schemas,
                ):
                    if isinstance(event, TextDelta):
                        if not started_output:
                            print("\n助手：", end="", flush=True)
                            started_output = True
                        print(event.content, end="", flush=True)
                        answer_parts.append(event.content)
                    elif isinstance(event, ToolCallRequest):
                        tool_requests.append(event)

                if started_output:
                    print()

                if not tool_requests:
                    answer = "".join(answer_parts)
                    if not answer:
                        answer = "（模型未返回内容）"
                        print(f"\n助手：{answer}")
                    self._conversation.add_assistant(answer)
                    return

                self._conversation.add_assistant_tool_calls(
                    [request.as_message_dict() for request in tool_requests],
                    content="".join(answer_parts) or None,
                )
                self._execute_tools(tool_requests)
        except Exception as error:
            self._conversation.truncate(checkpoint)
            print(f"\n调用失败：{error}")
            return

        message = f"Agent 已达到最大执行步数 {MAX_AGENT_STEPS}，任务已停止。"
        self._conversation.add_assistant(message)
        print(f"\n助手：{message}")

    def _execute_tools(self, requests: list[ToolCallRequest]) -> None:
        for request in requests:
            print(f"\n[工具调用] {request.name} 参数：{request.arguments}")
            try:
                result = self._tools.execute(request.name, request.arguments)
            except ToolError as error:
                result = f"工具执行失败：{error}"
            except Exception as error:
                result = f"工具发生未预期错误：{error}"

            print(f"[工具结果] {result}")
            self._conversation.add_tool_result(
                tool_call_id=request.id,
                name=request.name,
                content=result,
            )

    def _handle_command(self, prompt: str) -> bool:
        command = prompt.lower()

        if command in EXIT_COMMANDS:
            print("对话已结束。")
            self._running = False
            return True

        if command == "/help":
            self._print_help()
            return True

        if command == "/clear":
            self._conversation.clear()
            print("当前对话上下文已清空。")
            return True

        if command == "/history":
            self._print_history()
            return True

        if command == "/model":
            print(f"当前模型：{self._model.model_name}")
            return True

        if command == "/tools":
            print("可用工具：" + "、".join(self._tools.names))
            return True

        if command.startswith("/"):
            print("未知命令。输入 /help 查看可用命令。")
            return True

        return False

    def _print_welcome(self) -> None:
        print(f"DeepSeek Agent 已启动，当前模型：{self._model.model_name}")
        print("输入 /help 查看命令，输入 /exit 结束对话。")

    @staticmethod
    def _print_help() -> None:
        print(
            "可用命令：\n"
            "  /help     显示帮助\n"
            "  /clear    清空当前对话上下文\n"
            "  /history  查看当前对话历史\n"
            "  /model    查看当前模型\n"
            "  /tools    查看可用工具\n"
            "  /exit     退出程序"
        )

    def _print_history(self) -> None:
        history = self._conversation.visible_history()
        if not history:
            print("当前还没有对话记录。")
            return

        for message in history:
            print(self._format_history_message(message))

    @staticmethod
    def _format_history_message(message: Message) -> str:
        role = message["role"]
        if role == "user":
            return f"你：{message['content']}"
        if role == "tool":
            return f"工具 {message.get('name', '')}：{message['content']}"
        if message.get("tool_calls"):
            names = [
                call["function"]["name"] for call in message["tool_calls"]
            ]
            return "助手：[请求调用工具：" + "、".join(names) + "]"
        return f"助手：{message.get('content') or ''}"
