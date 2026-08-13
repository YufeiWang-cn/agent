import logging
from pathlib import Path
from typing import Callable

from .config import PROJECT_ROOT, Settings
from .context import ContextManager
from .conversation import Conversation, Message
from .memory import (
    JsonProjectStore,
    JsonSessionStore,
    Project,
    ProjectStoreError,
    Session,
    SessionStoreError,
)
from .models import ChatModel, DeepSeekModel, TextDelta, ToolCallRequest
from .observability import RuntimeMetrics
from .permissions import ConsoleToolConfirmer, ToolConfirmer
from .reliability import RetryPolicy, RetryingChatModel
from .tools import ToolError, ToolRegistry, build_default_registry


EXIT_COMMANDS = {"/exit", "exit", "quit", "q", "退出"}
MAX_AGENT_STEPS = 5
DEFAULT_SESSION_DIRECTORY = PROJECT_ROOT / "data" / "sessions"
DEFAULT_PROJECT_FILE = PROJECT_ROOT / "data" / "projects.json"


class AgentCancelledError(RuntimeError):
    """表示用户主动停止当前轮次。

    ``tool_records_preserved`` 用于通知界面本轮最终采用的回滚策略。
    ``False`` 表示本轮消息已经回滚。
    ``True`` 表示本轮已有工具完成，因此相关工具记录已经保留。
    当工具记录已经保留时，界面应以 Agent 历史为准重新渲染。
    """

    def __init__(
        self,
        message: str,
        *,
        tool_records_preserved: bool = False,
    ) -> None:
        super().__init__(message)
        self.tool_records_preserved = tool_records_preserved


TextCallback = Callable[[str], None]
ToolCallCallback = Callable[[ToolCallRequest], None]
ToolResultCallback = Callable[[ToolCallRequest, str], None]
CancelCheck = Callable[[], bool]


class Agent:
    def __init__(
        self,
        settings: Settings,
        model: ChatModel | None = None,
        tools: ToolRegistry | None = None,
        session_store: JsonSessionStore | None = None,
        project_store: JsonProjectStore | None = None,
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
        self._project_store = project_store or JsonProjectStore(
            Path(DEFAULT_PROJECT_FILE)
        )
        self._context_manager = context_manager or ContextManager(
            settings.max_context_tokens
        )
        self._confirmer = (
            confirmer if confirmer is not None else ConsoleToolConfirmer()
        )
        self._session = self._restore_latest_session()
        self._running = True
        # 回滚结果状态用于记录最近一次中断所采用的处理方式。
        # 界面根据该状态决定删除临时轮次还是重新渲染工具历史。
        self._last_turn_history_preserved = False

    def run(self) -> None:
        self._print_welcome()

        while self._running:
            try:
                prompt = input("\n您：").strip()
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
        started_output = False

        def print_text(content: str) -> None:
            nonlocal started_output
            if not started_output:
                print("\n助手：", end="", flush=True)
                started_output = True
            print(content, end="", flush=True)

        def print_tool_call(request: ToolCallRequest) -> None:
            nonlocal started_output
            if started_output:
                print()
                started_output = False
            print(f"\n[工具调用] {request.name} 参数：{request.arguments}")

        def print_tool_result(_request: ToolCallRequest, result: str) -> None:
            print(f"[工具结果] {result}")

        try:
            self.chat(
                prompt,
                on_text=print_text,
                on_tool_call=print_tool_call,
                on_tool_result=print_tool_result,
            )
            if started_output:
                print()
        except Exception as error:
            if started_output:
                print()
            print(f"\n调用失败：{error}")

    def chat(
        self,
        prompt: str,
        *,
        on_text: TextCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> None:
        """执行一轮 Agent 对话，并通过回调发送流式事件。

        本方法只回滚 Conversation 中属于当前轮次的消息。
        已经完成的工具可能产生文件等外部副作用。
        这类外部副作用不会被自动撤销。
        相应的工具记录会被保留，从而使会话历史与真实外部状态保持一致。
        """
        # 回滚检查点：保存本轮开始前的消息数量。
        # 需要回滚时，只删除该位置之后新增的用户消息、助手消息和工具消息，旧会话历史不受影响。
        checkpoint = len(self._conversation)
        # 每轮开始先清空上轮的结果，避免界面误用旧的“已保留”状态。
        self._last_turn_history_preserved = False
        self._conversation.add_user(prompt)

        try:
            for _step in range(MAX_AGENT_STEPS):
                self._raise_if_cancelled(should_cancel)
                answer_parts: list[str] = []
                tool_requests: list[ToolCallRequest] = []

                context_window = self._context_manager.prepare(
                    self._conversation.messages
                )
                for event in self._model.stream(
                    context_window.messages,
                    self._tools.schemas,
                ):
                    self._raise_if_cancelled(should_cancel)
                    if isinstance(event, TextDelta):
                        if on_text is not None:
                            on_text(event.content)
                        answer_parts.append(event.content)
                    elif isinstance(event, ToolCallRequest):
                        tool_requests.append(event)

                if not tool_requests:
                    answer = "".join(answer_parts)
                    if not answer:
                        answer = "（模型未返回内容）"
                        if on_text is not None:
                            on_text(answer)
                    self._conversation.add_assistant(answer)
                    self._save_completed_turn(checkpoint)
                    return

                self._conversation.add_assistant_tool_calls(
                    [request.as_message_dict() for request in tool_requests],
                    content="".join(answer_parts) or None,
                )
                self._execute_tools(
                    tool_requests,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                    should_cancel=should_cancel,
                )
        except AgentCancelledError as error:
            # 回滚策略一：如果已有工具结果，外部状态可能已经改变，不能只删除对话记录。
            # 此时保留完整工具链，并补齐尚未执行的工具结果。
            if self._preserve_completed_tool_records(
                checkpoint,
                pending_result="用户停止了本轮任务，本工具未执行。",
                final_message=(
                    "本轮已停止。停止前已完成的工具操作及其记录已保留。"
                ),
            ):
                error.tool_records_preserved = True
                self._last_turn_history_preserved = True
                # 已完成工具的记录必须成功落盘，否则调用方需要明确处理保存失败。
                self._save_session()
            else:
                # 回滚策略二适用于没有任何工具完成的情况。
                # 此时本轮没有已知外部副作用，因此可以安全地截断到检查点。
                self._conversation.truncate(checkpoint)
            raise
        except Exception as error:
            # 模型异常与用户停止使用同一套回滚判断。
            # 已经完成工具时保留真实记录，尚未完成任何工具时回滚临时消息。
            if self._preserve_completed_tool_records(
                checkpoint,
                pending_result="本轮因调用失败而中断，本工具未执行。",
                final_message=f"本轮因调用失败而中断：{error}",
            ):
                self._last_turn_history_preserved = True
                # 已完成工具的记录必须成功落盘，否则调用方需要明确处理保存失败。
                self._save_session()
            else:
                # 这里只回滚 Conversation，不会撤销文件系统或其他外部操作。
                self._conversation.truncate(checkpoint)
            raise

        message = f"Agent 已达到最大执行步数 {MAX_AGENT_STEPS}，任务已停止。"
        self._conversation.add_assistant(message)
        self._save_completed_turn(checkpoint)
        if on_text is not None:
            on_text(message)

    def _execute_tools(
        self,
        requests: list[ToolCallRequest],
        *,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> None:
        for request in requests:
            # 工具开始前需要检查停止信号。
            # 工具一旦开始执行就应完整结束，从而避免强行中断写入造成半写状态。
            # 工具执行结束后的记录由统一的回滚策略处理。
            self._raise_if_cancelled(should_cancel)
            if on_tool_call is not None:
                on_tool_call(request)
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

            # 工具可能已经改变文件等外部状态，因此必须先把真实结果写入历史。
            self._conversation.add_tool_result(
                tool_call_id=request.id,
                name=request.name,
                content=result,
            )
            # 界面回调发生异常时，已经写入的工具结果仍可阻止错误回滚。
            if on_tool_result is not None:
                on_tool_result(request, result)

    @staticmethod
    def _raise_if_cancelled(should_cancel: CancelCheck | None) -> None:
        if should_cancel is not None and should_cancel():
            raise AgentCancelledError("本轮对话已停止。")

    def _preserve_completed_tool_records(
        self,
        checkpoint: int,
        *,
        pending_result: str,
        final_message: str,
    ) -> bool:
        """在已有工具完成时保留本轮记录，并补齐未执行工具的结果。

        返回 ``True`` 表示本轮至少已有一条工具结果。
        返回 ``True`` 后，调用方不能再执行 ``truncate(checkpoint)``。
        返回 ``False`` 表示没有工具完成，可以安全回滚到检查点。
        本方法保留的是记录，不负责撤销工具产生的外部副作用。
        """
        # 只检查当前轮次，避免旧轮次中的工具结果影响本轮回滚判断。
        turn_messages = self._conversation.messages[checkpoint:]
        # 已存在 tool 消息的调用会被视为已经完成并且拥有结果记录。
        # 工具结果可以是成功、失败或用户拒绝，但任何结果都应保持调用链结构完整。
        completed_call_ids = {
            str(message.get("tool_call_id", ""))
            for message in turn_messages
            if message.get("role") == "tool"
            and message.get("tool_call_id")
        }
        if not completed_call_ids:
            return False

        # 一次模型响应可能同时请求多个工具。
        # 用户中途停止时，需要为剩余工具补充“未执行”结果。
        # 缺少结果会形成只有 tool_call 而没有 tool 的非法消息链。
        # 后续模型请求可能拒绝包含非法工具消息链的上下文。
        for message in turn_messages:
            for call in message.get("tool_calls") or ():
                call_id = str(call.get("id", ""))
                if not call_id or call_id in completed_call_ids:
                    continue
                function = call.get("function") or {}
                self._conversation.add_tool_result(
                    tool_call_id=call_id,
                    name=str(function.get("name", "未知工具")),
                    content=pending_result,
                )
                completed_call_ids.add(call_id)

        # 用一条终止消息闭合当前轮次，使重新加载会话时能解释中断原因。
        self._conversation.add_assistant(final_message)
        return True

    def _save_completed_turn(self, checkpoint: int) -> None:
        """根据当前轮次是否包含工具结果选择严格或尽力保存。"""
        turn_messages = self._conversation.messages[checkpoint:]
        has_tool_results = any(
            message.get("role") == "tool" for message in turn_messages
        )
        if has_tool_results:
            # 工具结果可能对应已经生效的外部副作用，因此保存失败必须向上抛出。
            self._save_session()
            return
        # 纯文本回复没有外部副作用，因此沿用尽力保存以保证对话可继续使用。
        self._save_session_safely()

    @property
    def last_turn_history_preserved(self) -> bool:
        return self._last_turn_history_preserved

    @property
    def model_name(self) -> str:
        return self._model.model_name

    @property
    def available_models(self) -> tuple[str, ...]:
        return self._model.available_models

    def select_model(self, model_name: str) -> None:
        self._model.select_model(model_name)

    @property
    def session_id(self) -> str:
        return self._session.id

    @property
    def session_title(self) -> str:
        return self._session.title

    def history(self) -> list[Message]:
        return self._conversation.visible_history()

    def list_sessions(self) -> list[Session]:
        return self._session_store.list_sessions()

    def list_projects(self) -> list[Project]:
        return self._project_store.list_projects()

    def create_project(self, name: str) -> Project:
        return self._project_store.create(name)

    def rename_project(self, project_id: str, name: str) -> Project:
        return self._project_store.rename(project_id, name)

    def delete_project(self, project_id: str) -> Project:
        """删除项目；失败时补偿恢复所有受影响会话的原项目归属。"""
        project = self._project_store.get(project_id)
        self._save_session()
        # 补偿快照会在移动会话前保留磁盘中的完整 Session 对象。
        # 后续步骤失败时，可以使用快照恢复原来的项目归属和其他会话字段。
        affected = [
            session
            for session in self._session_store.list_sessions()
            if session.project_id == project.id
        ]
        try:
            # 事务执行阶段会先把项目内的会话全部移动到“未分类”。
            # 所有会话移动成功后才会删除项目。
            # 整个阶段成功后才会更新当前 Agent 的 Session 对象。
            moved_sessions: dict[str, Session] = {}
            for session in affected:
                moved_sessions[session.id] = self._session_store.move_to_project(
                    session.id,
                    None,
                )
            deleted = self._project_store.delete(project.id)
        except Exception as error:
            # 补偿回滚会逐个写回操作前保存的会话快照。
            # JSON 文件存储不支持原生事务，因此这里使用反向操作进行恢复。
            rollback_errors: list[str] = []
            for snapshot in affected:
                try:
                    self._session_store.save(snapshot)
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if rollback_errors:
                # 补偿失败时必须显式报告部分回滚风险。
                # 上层不能在补偿失败后误认为项目和所有会话都已经恢复成功。
                raise ProjectStoreError(
                    "删除项目失败，且会话归属回滚失败："
                    + "；".join(rollback_errors)
                ) from error
            raise
        # 只有磁盘上的会话移动和项目删除都成功后，才会提交当前内存状态。
        # 延迟提交可以避免磁盘操作失败时界面提前显示尚未生效的状态。
        if self._session.id in moved_sessions:
            self._session = moved_sessions[self._session.id]
        return deleted

    def start_new_session(self, project_id: str | None = None) -> Session:
        """保存当前会话后创建新会话；任一步失败都不提前切换内存状态。"""
        resolved_project_id = self._resolve_project_id(project_id)
        self._save_session()
        return self._start_new_session(resolved_project_id)

    def clear_conversation(self) -> None:
        """以“先持久化、后提交内存”的顺序清空当前会话。"""
        cleared_messages = [
            {"role": "system", "content": self._conversation.system_prompt}
        ]
        # 所有待提交的修改都会先发生在候选副本上。
        # 当前 Session 和 Conversation 在磁盘保存成功前保持不变。
        candidate = self._copy_session(self._session)
        candidate.update_messages(cleared_messages)
        # 持久化阶段发生保存失败时会直接抛出异常。
        # 保存失败后不会执行内存提交，因此不需要恢复当前 Conversation。
        self._session_store.save(candidate)
        # 提交阶段：只有候选状态成功落盘后，才同步更新两个内存真相源。
        self._conversation.restore(cleared_messages)
        self._session = candidate

    def load_session(self, session_id: str) -> Session:
        """先保存当前会话，再加载并提交目标会话。"""
        self._save_session()
        session = self._session_store.load(session_id)
        # restore 会先校验消息结构；校验成功后才替换 Conversation。
        self._conversation.restore(session.messages)
        self._session = session
        return session

    def delete_session(
        self,
        session_id: str,
        replacement_project_id: str | None = None,
    ) -> str:
        """删除会话；删除当前会话时先准备可用的替代会话。"""
        target = self._session_store.load(session_id)
        if target.id != self._session.id:
            return self._session_store.delete(target.id)

        resolved_project_id = self._resolve_project_id(replacement_project_id)
        # 预提交阶段会先创建并保存一个替代会话。
        # 替代会话可以确保旧会话删除后 Agent 仍然拥有有效会话。
        # 预提交阶段不会切换当前内存状态。
        replacement = self._create_new_session(resolved_project_id)
        try:
            deleted_id = self._session_store.delete(target.id)
        except Exception:
            # 旧会话删除失败时，补偿回滚会删除刚创建的替代会话。
            # 删除替代会话可以使磁盘状态尽量恢复到操作前。
            # 即使补偿删除失败，当前内存仍然指向原会话。
            try:
                self._session_store.delete(replacement.id)
            except Exception:
                pass
            raise
        # 提交内存状态：旧会话确认删除后，才正式切换到替代会话。
        self._conversation.restore(replacement.messages)
        self._session = replacement
        return deleted_id

    def rename_session(self, session_id: str, title: str) -> Session:
        self._save_session()
        session = self._session_store.rename(session_id, title)
        if session.id == self._session.id:
            self._session = session
        return session

    def move_session(
        self,
        session_id: str,
        project_id: str | None,
    ) -> Session:
        self._save_session()
        resolved_project_id = self._resolve_project_id(project_id)
        session = self._session_store.move_to_project(
            session_id,
            resolved_project_id,
        )
        if session.id == self._session.id:
            self._session = session
        return session

    def save_session(self) -> None:
        self._save_session()

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
            try:
                self.clear_conversation()
            except (OSError, SessionStoreError) as error:
                print(f"清空失败：{error}")
            else:
                print("当前对话上下文已清空。")
            return True

        if command == "/new":
            try:
                session = self.start_new_session()
            except (OSError, SessionStoreError, ProjectStoreError) as error:
                print(f"创建新会话失败：{error}")
            else:
                print(f"已创建新会话：{session.id[:8]}")
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

    def _start_new_session(self, project_id: str | None = None) -> Session:
        """创建持久化会话成功后，将其提交为当前内存会话。"""
        session = self._create_new_session(project_id)
        # _create_new_session 返回前已经完成磁盘保存。
        # 因此后续内存切换不会造成界面进入一个尚未持久化的新会话。
        self._conversation.restore(session.messages)
        self._session = session
        return session

    def _create_new_session(self, project_id: str | None = None) -> Session:
        """创建并保存新会话，但不修改当前 Agent 的内存状态。"""
        messages = [
            {"role": "system", "content": self._conversation.system_prompt}
        ]
        session = Session.create(messages, project_id=project_id)
        # 保存失败时直接抛出异常，调用方仍保持原 Session 和 Conversation。
        self._session_store.save(session)
        return session

    def _resolve_project_id(self, project_id: str | None) -> str | None:
        if project_id is None:
            return None
        return self._project_store.get(project_id).id

    @staticmethod
    def _copy_session(session: Session) -> Session:
        """生成深拷贝候选对象，避免持久化前修改当前 Session。"""
        return Session.from_dict(session.to_dict())

    def _save_session(self) -> None:
        """事务式保存当前会话：先保存候选副本，成功后再替换内存对象。"""
        # 候选状态与当前 Session 相互独立。
        # 候选对象的标题、时间和消息更新不会提前影响当前内存对象。
        candidate = self._copy_session(self._session)
        if candidate.title == "新会话":
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
                candidate.title = compact_title[:30]
        candidate.update_messages(self._conversation.messages)
        # 持久化操作是候选状态的提交边界。
        # save 抛出异常时不会执行内存替换，因此当前 Session 保持操作前状态。
        # 保存成功后才会把候选对象设置为新的当前 Session。
        self._session_store.save(candidate)
        self._session = candidate

    def _save_session_safely(self, success_message: bool = False) -> None:
        """尽力保存会话；失败时报告错误，但不要求调用方执行回滚。"""
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
            deleting_current = self._session.id.startswith(session_id.strip().lower())
            deleted_id = self.delete_session(session_id)
        except (SessionStoreError, ProjectStoreError) as error:
            print(f"删除失败：{error}")
            return
        if deleting_current:
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
            return f"您：{message['content']}"
        if role == "tool":
            return f"工具 {message.get('name', '')}：{message['content']}"
        if message.get("tool_calls"):
            names = [
                call["function"]["name"] for call in message["tool_calls"]
            ]
            return "助手：[请求调用工具：" + "、".join(names) + "]"
        return f"助手：{message.get('content') or ''}"
