import logging
from pathlib import Path

from .config import PROJECT_ROOT, Settings
from .context import ContextManager
from .conversation import Conversation, Message
from .memory import JsonSessionStore, Session, SessionStoreError
from .models import ChatModel, DeepSeekModel, TextDelta, ToolCallRequest
from .observability import RuntimeMetrics
from .permissions import ConsoleToolConfirmer, ToolConfirmer
from .reliability import RetryPolicy, RetryingChatModel
from .tools import ToolError, ToolRegistry, build_default_registry


EXIT_COMMANDS = {"/exit", "exit", "quit", "q", "退出"}
MAX_AGENT_STEPS = 5
DEFAULT_SESSION_DIRECTORY = PROJECT_ROOT / "data" / "sessions"


class Agent:
    def __init__(
        self,
        settings: Settings,
        model: ChatModel | None = None,
        tools: ToolRegistry | None = None,
        session_store: JsonSessionStore | None = None,
        context_manager: ContextManager | None = None,
        confirmer: ToolConfirmer | None = None,
        retry_policy: RetryPolicy | None = None,
        metrics: RuntimeMetrics | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        base_model = model or DeepSeekModel(settings)
        self._metrics = metrics or RuntimeMetrics()
        self._model = RetryingChatModel(
            base_model,
            retry_policy
            or RetryPolicy(
                max_retries=settings.max_retries,
                base_delay_seconds=settings.retry_base_delay,
            ),
            self._metrics,
            logger=logger,
        )
        self._tools = (
            tools
            if tools is not None
            else build_default_registry(
                settings.workspace_root,
                settings.max_file_size,
            )
        )
        self._conversation = Conversation(settings.system_prompt)
        self._session_store = session_store or JsonSessionStore(
            Path(DEFAULT_SESSION_DIRECTORY)
        )
        self._context_manager = context_manager or ContextManager(
            settings.max_context_tokens
        )
        self._confirmer = (
            confirmer if confirmer is not None else ConsoleToolConfirmer()
        )
        self._session = self._restore_latest_session()
        self._running = True

    def run(self) -> None:
        self._print_welcome()

        while self._running:
            try:
                prompt = input("\n你：").strip()
            except (EOFError, KeyboardInterrupt):
                self._save_session_safely()
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

                context_window = self._context_manager.prepare(
                    self._conversation.messages
                )
                for event in self._model.stream(
                    context_window.messages,
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
                    self._save_session_safely()
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
        self._save_session_safely()
        print(f"\n助手：{message}")

    def _execute_tools(self, requests: list[ToolCallRequest]) -> None:
        for request in requests:
            print(f"\n[工具调用] {request.name} 参数：{request.arguments}")
            try:
                tool = self._tools.get(request.name)
                if tool.requires_confirmation and not self._confirmer.confirm(
                    tool,
                    request.arguments,
                ):
                    result = "用户拒绝执行该工具。"
                else:
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
            self._save_session_safely()
            print("对话已结束。")
            self._running = False
            return True

        if command == "/help":
            self._print_help()
            return True

        if command == "/clear":
            self._conversation.clear()
            self._save_session_safely()
            print("当前对话上下文已清空。")
            return True

        if command == "/new":
            self._save_session_safely()
            self._start_new_session()
            print(f"已创建新会话：{self._session.id[:8]}")
            return True

        if command == "/save":
            self._save_session_safely(success_message=True)
            return True

        if command == "/sessions":
            self._print_sessions()
            return True

        if command.startswith("/load "):
            self._load_session(prompt.split(maxsplit=1)[1])
            return True

        if command.startswith("/delete "):
            self._delete_session(prompt.split(maxsplit=1)[1])
            return True

        if command == "/history":
            self._print_history()
            return True

        if command == "/context":
            self._print_context()
            return True

        if command == "/stats":
            self._print_stats()
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

    def _restore_latest_session(self) -> Session:
        latest = self._session_store.latest()
        if latest is not None:
            try:
                self._conversation.restore(latest.messages)
                return latest
            except ValueError:
                pass
        return self._start_new_session()

    def _start_new_session(self) -> Session:
        self._conversation.clear()
        self._session = Session.create(self._conversation.messages)
        self._session_store.save(self._session)
        return self._session

    def _save_session(self) -> None:
        if self._session.title == "新会话":
            first_user_message = next(
                (
                    message.get("content", "")
                    for message in self._conversation.visible_history()
                    if message.get("role") == "user"
                ),
                "",
            )
            if first_user_message:
                compact_title = " ".join(str(first_user_message).split())
                self._session.title = compact_title[:30]
        self._session.update_messages(self._conversation.messages)
        self._session_store.save(self._session)

    def _save_session_safely(self, success_message: bool = False) -> None:
        try:
            self._save_session()
            if success_message:
                print(f"会话已保存：{self._session.id[:8]}")
        except (OSError, SessionStoreError) as error:
            print(f"会话保存失败：{error}")

    def _load_session(self, session_id: str) -> None:
        try:
            self._save_session()
            session = self._session_store.load(session_id)
            self._conversation.restore(session.messages)
        except (OSError, ValueError, SessionStoreError) as error:
            print(f"加载失败：{error}")
            return
        self._session = session
        print(f"已加载会话：{session.id[:8]}  {session.title}")

    def _delete_session(self, session_id: str) -> None:
        try:
            deleted_id = self._session_store.delete(session_id)
        except SessionStoreError as error:
            print(f"删除失败：{error}")
            return
        if deleted_id == self._session.id:
            self._start_new_session()
            print(f"当前会话已删除，并已创建新会话：{self._session.id[:8]}")
        else:
            print(f"会话已删除：{deleted_id[:8]}")

    def _print_sessions(self) -> None:
        sessions = self._session_store.list_sessions()
        if not sessions:
            print("还没有已保存的会话。")
            return
        print("已保存的会话：")
        for session in sessions:
            marker = "*" if session.id == self._session.id else " "
            print(
                f"{marker} {session.id[:8]}  {session.title}  "
                f"{session.updated_at}"
            )

    def _print_welcome(self) -> None:
        print(f"DeepSeek Agent 已启动，当前模型：{self._model.model_name}")
        print(
            f"当前会话：{self._session.id[:8]}  {self._session.title}\n"
            "输入 /help 查看命令，输入 /exit 结束对话。"
        )

    @staticmethod
    def _print_help() -> None:
        print(
            "可用命令：\n"
            "  /help         显示帮助\n"
            "  /new          创建新会话\n"
            "  /save         立即保存当前会话\n"
            "  /sessions     查看已保存的会话\n"
            "  /load <ID>    加载指定会话\n"
            "  /delete <ID>  删除指定会话\n"
            "  /clear        清空当前会话上下文\n"
            "  /history      查看当前对话历史\n"
            "  /context      查看上下文预算和裁剪情况\n"
            "  /stats        查看本次运行的模型调用统计\n"
            "  /model        查看当前模型\n"
            "  /tools        查看可用工具\n"
            "  /exit         退出程序"
        )

    def _print_history(self) -> None:
        history = self._conversation.visible_history()
        if not history:
            print("当前还没有对话记录。")
            return

        for message in history:
            print(self._format_history_message(message))

    def _print_context(self) -> None:
        window = self._context_manager.prepare(self._conversation.messages)
        print(
            "上下文状态（近似值）：\n"
            f"  消息 Token 预算：{self._context_manager.max_tokens}\n"
            f"  完整历史 Token：{window.total_estimated_tokens}\n"
            f"  预计发送 Token：{window.estimated_tokens}\n"
            f"  省略历史消息数：{window.omitted_messages}"
        )

    def _print_stats(self) -> None:
        print(
            "本次运行统计：\n"
            f"  模型请求数：{self._metrics.model_requests}\n"
            f"  API 尝试次数：{self._metrics.api_attempts}\n"
            f"  成功请求数：{self._metrics.successful_requests}\n"
            f"  失败请求数：{self._metrics.failed_requests}\n"
            f"  自动重试次数：{self._metrics.retries}\n"
            f"  平均请求耗时："
            f"{self._metrics.average_duration_seconds:.3f} 秒"
        )

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
