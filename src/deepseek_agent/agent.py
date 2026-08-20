"""编排模型、工具、上下文、持久化和运行恢复等 Agent 核心流程。"""

import logging
import threading
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .config import PROJECT_ROOT, Settings
from .context import ContextManager, ContextWindow
from .conversation import Conversation, Message
from .journal import RecoveryIssue, RunJournal, RunJournalError
from .tool_execution import (
    ToolExecutionRecord,
    ToolExecutionStart,
    ToolExecutor,
)
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
from .runtime import AgentEvent, RunStatus, TurnOutcome
from .tools import ToolEffect, ToolRegistry, build_default_registry


MAX_AGENT_STEPS = 5
DEFAULT_SESSION_DIRECTORY = PROJECT_ROOT / "data" / "sessions"
DEFAULT_PROJECT_FILE = PROJECT_ROOT / "data" / "projects.json"
DEFAULT_RUN_DIRECTORY = PROJECT_ROOT / "data" / "runs"


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
        outcome: TurnOutcome | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_records_preserved = tool_records_preserved
        self.outcome = outcome


TextCallback = Callable[[str], None]
ToolCallCallback = Callable[[ToolCallRequest], None]
ToolResultCallback = Callable[[ToolCallRequest, str], None]
AgentEventCallback = Callable[[AgentEvent], None]
CancelCheck = Callable[[], bool]


class Agent:
    """对外提供完整 Agent 能力，并维护每轮执行的一致性边界。"""

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
        run_journal: RunJournal | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
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
                settings.command_timeout,
                settings.max_command_output,
            )
        )
        self._conversation = Conversation(settings.system_prompt)
        self._session_store = session_store or JsonSessionStore(
            Path(DEFAULT_SESSION_DIRECTORY)
        )
        journal_directory = (
            Path(DEFAULT_RUN_DIRECTORY)
            if session_store is None
            else self._session_store.directory / ".runs"
        )
        self._run_journal = run_journal or RunJournal(journal_directory)
        self._recovery_issues = self._load_recovery_issues()
        self._project_store = project_store or JsonProjectStore(
            Path(DEFAULT_PROJECT_FILE)
        )
        self._context_manager = context_manager or ContextManager(
            settings.max_context_tokens
        )
        self._confirmer = (
            confirmer if confirmer is not None else ConsoleToolConfirmer()
        )
        self._tool_executor = ToolExecutor(self._tools, self._confirmer)
        self._session = self._restore_latest_session()
        # 回滚结果状态用于记录最近一次中断所采用的处理方式。
        # 界面根据该状态决定删除临时轮次还是重新渲染工具历史。
        self._last_turn_history_preserved = False
        # 结构化结果用于准确描述最近一轮的结束原因和实际完成情况。
        self._last_turn_outcome: TurnOutcome | None = None
        # 状态锁确保同一个 Agent 实例不会同时执行两个轮次。
        self._run_status_lock = threading.Lock()
        self._run_status = RunStatus.IDLE
        self._current_turn_tool_records: list[ToolExecutionRecord] = []
        self._current_turn_id: str | None = None
        self._current_turn_side_effects_saved = False

    def chat(
        self,
        prompt: str,
        *,
        on_text: TextCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
        on_event: AgentEventCallback | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> TurnOutcome:
        """执行一轮 Agent 对话，并通过回调发送流式事件。

        本方法只回滚 Conversation 中属于当前轮次的消息。
        已经完成的工具可能产生文件等外部副作用。
        这类外部副作用不会被自动撤销。
        相应的工具记录会被保留，从而使会话历史与真实外部状态保持一致。
        """
        # 回滚检查点保存本轮开始前的消息数量。
        # 回滚只删除本轮新增消息，不影响旧会话历史。
        checkpoint = len(self._conversation)
        self._begin_turn()
        self._current_turn_id = uuid4().hex
        self._record_turn_started_safely(self._current_turn_id)
        # 每轮开始先清空上轮的结果，避免界面误用旧的“已保留”状态。
        self._last_turn_history_preserved = False
        self._last_turn_outcome = None
        self._current_turn_tool_records = []
        self._current_turn_side_effects_saved = False
        self._conversation.add_user(prompt)
        steps_completed = 0

        try:
            self._emit_event(on_event, AgentEvent.turn_started())
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
                        self._emit_event(
                            on_event,
                            AgentEvent.text_delta(event.content),
                        )
                        if on_text is not None:
                            on_text(event.content)
                        answer_parts.append(event.content)
                    elif isinstance(event, ToolCallRequest):
                        tool_requests.append(event)
                steps_completed += 1

                if not tool_requests:
                    answer = "".join(answer_parts)
                    if not answer:
                        answer = "（模型未返回内容）"
                        self._emit_event(
                            on_event,
                            AgentEvent.text_delta(answer),
                        )
                        if on_text is not None:
                            on_text(answer)
                    self._conversation.add_assistant(answer)
                    self._save_completed_turn(checkpoint)
                    return self._finish_turn_outcome(
                        status=RunStatus.COMPLETED,
                        final_text=answer,
                        steps_completed=steps_completed,
                        checkpoint=checkpoint,
                        history_preserved=True,
                    )

                self._conversation.add_assistant_tool_calls(
                    [request.as_message_dict() for request in tool_requests],
                    content="".join(answer_parts) or None,
                )
                self._execute_tools(
                    tool_requests,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                    on_event=on_event,
                    should_cancel=should_cancel,
                )

            message = f"Agent 已达到最大执行步数 {MAX_AGENT_STEPS}，任务已停止。"
            self._conversation.add_assistant(message)
            self._save_completed_turn(checkpoint)
            self._emit_event(on_event, AgentEvent.text_delta(message))
            if on_text is not None:
                on_text(message)
            return self._finish_turn_outcome(
                status=RunStatus.STEP_LIMIT_REACHED,
                final_text=message,
                steps_completed=steps_completed,
                checkpoint=checkpoint,
                history_preserved=True,
            )
        except AgentCancelledError as error:
            # 回滚策略一：如果已有工具结果，外部状态可能已经改变，不能只删除对话记录。
            # 此时保留完整工具链，并补齐尚未执行的工具结果。
            completed_tool_calls = self._count_tool_results(checkpoint)
            records_preserved = self._preserve_completed_tool_records(
                checkpoint,
                pending_result="用户停止了本轮任务，本工具未执行。",
                final_message=(
                    "本轮已停止。停止前已完成的工具操作及其记录已保留。"
                ),
            )
            if records_preserved:
                error.tool_records_preserved = True
                self._last_turn_history_preserved = True
            else:
                # 回滚策略二适用于没有任何工具完成的情况。
                # 此时本轮没有已知外部副作用，因此可以安全地截断到检查点。
                self._conversation.truncate(checkpoint)
            outcome = self._finish_turn_outcome(
                status=RunStatus.CANCELLED,
                final_text=self._latest_assistant_text(checkpoint),
                steps_completed=steps_completed,
                checkpoint=checkpoint,
                history_preserved=error.tool_records_preserved,
                error_message=str(error),
                tool_calls_completed=completed_tool_calls,
            )
            error.outcome = outcome
            if records_preserved:
                try:
                    # 已完成工具的记录必须成功落盘，否则本轮最终状态应改为失败。
                    self._save_session()
                    self._current_turn_side_effects_saved = True
                    self._record_turn_finished_safely(outcome)
                except Exception as save_error:
                    self._finish_turn_outcome(
                        status=RunStatus.FAILED,
                        final_text=self._latest_assistant_text(checkpoint),
                        steps_completed=steps_completed,
                        checkpoint=checkpoint,
                        history_preserved=True,
                        error_message=str(save_error),
                    )
                    raise
            raise
        except Exception as error:
            # 模型异常与用户停止使用同一套回滚判断。
            # 有工具已经完成时保留真实记录，否则回滚本轮临时消息。
            completed_tool_calls = self._count_tool_results(checkpoint)
            records_preserved = self._preserve_completed_tool_records(
                checkpoint,
                pending_result="本轮因调用失败而中断，本工具未执行。",
                final_message=f"本轮因调用失败而中断：{error}",
            )
            if records_preserved:
                self._last_turn_history_preserved = True
            else:
                # 这里只回滚 Conversation，不会撤销文件系统或其他外部操作。
                self._conversation.truncate(checkpoint)
            outcome = self._finish_turn_outcome(
                status=RunStatus.FAILED,
                final_text=self._latest_assistant_text(checkpoint),
                steps_completed=steps_completed,
                checkpoint=checkpoint,
                history_preserved=self._last_turn_history_preserved,
                error_message=str(error),
                tool_calls_completed=completed_tool_calls,
            )
            if records_preserved:
                try:
                    # 已完成工具的记录必须成功落盘，否则保留失败状态并继续抛出保存异常。
                    self._save_session()
                    self._current_turn_side_effects_saved = True
                    self._record_turn_finished_safely(outcome)
                except Exception as save_error:
                    self._finish_turn_outcome(
                        status=RunStatus.FAILED,
                        final_text=self._latest_assistant_text(checkpoint),
                        steps_completed=steps_completed,
                        checkpoint=checkpoint,
                        history_preserved=True,
                        error_message=str(save_error),
                    )
                    raise
            raise

    def _execute_tools(
        self,
        requests: list[ToolCallRequest],
        *,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
        on_event: AgentEventCallback | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> list[ToolExecutionRecord]:
        if self._current_turn_id is None:
            # 私有方法的测试或扩展调用可能绕过 chat()，此时仍需建立可追踪的临时轮次。
            self._current_turn_id = uuid4().hex
            self._record_turn_started_safely(self._current_turn_id)
        records: list[ToolExecutionRecord] = []
        for request in requests:
            # 工具开始前需要检查停止信号。
            # 工具一旦开始执行就应完整结束，从而避免强行中断写入造成半写状态。
            # 工具执行结束后的记录由统一的回滚策略处理。
            self._raise_if_cancelled(should_cancel)
            self._emit_event(
                on_event,
                AgentEvent.tool_call_started(request),
            )
            if on_tool_call is not None:
                on_tool_call(request)
            record = self._tool_executor.execute(
                request,
                on_confirmation_state=lambda waiting, current=request: (
                    self._handle_confirmation_state_for_request(
                        current,
                        waiting,
                    )
                ),
                before_execution=self._record_tool_started,
                # GUI 的停止信号会继续传递给长时间运行的命令，而不只在工具之间检查。
                should_cancel=should_cancel,
            )
            records.append(record)
            self._current_turn_tool_records.append(record)

            # 工具可能已经改变文件等外部状态，因此必须先把真实结果写入历史。
            self._conversation.add_tool_result(
                tool_call_id=record.call_id,
                name=record.tool_name,
                content=record.model_result,
            )
            self._record_tool_finished(record)
            self._emit_event(
                on_event,
                AgentEvent.tool_call_completed(request, record),
            )
            # 界面回调发生异常时，已写入的工具结果仍可避免误回滚。
            if on_tool_result is not None:
                on_tool_result(request, record.model_result)
        return records

    def _handle_confirmation_state(self, waiting: bool) -> None:
        """根据工具确认阶段更新 Agent 的实时运行状态。"""
        self._set_run_status(
            RunStatus.WAITING_APPROVAL if waiting else RunStatus.RUNNING
        )

    def _handle_confirmation_state_for_request(
        self,
        request: ToolCallRequest,
        waiting: bool,
    ) -> None:
        """更新确认状态，并尽力记录确认请求已经出现。"""
        self._handle_confirmation_state(waiting)
        if not waiting or self._current_turn_id is None:
            return
        try:
            self._run_journal.record_confirmation_requested(
                self._current_turn_id,
                request,
            )
        except RunJournalError as error:
            self._logger.warning("无法记录工具确认请求：%s", error)

    def _record_tool_started(self, start: ToolExecutionStart) -> None:
        """在工具执行前记录开始事件，并保护可能产生副作用的操作。"""
        if self._current_turn_id is None:
            raise RunJournalError("当前工具调用缺少轮次标识。")
        try:
            self._run_journal.record_tool_started(
                self._current_turn_id,
                start,
            )
        except RunJournalError:
            # 只读工具不会改变外部状态，因此日志失败时仍可安全执行。
            if start.effect is ToolEffect.READ_ONLY:
                self._logger.warning("只读工具开始事件未能写入运行日志。")
                return
            # 写入类工具必须先留下开始记录，否则崩溃后无法判断它是否已经执行。
            raise

    def _record_tool_finished(self, record: ToolExecutionRecord) -> None:
        """记录工具结束事件，并对可能存在副作用的结果执行严格检查。"""
        if self._current_turn_id is None:
            raise RunJournalError("当前工具结果缺少轮次标识。")
        try:
            self._run_journal.record_tool_finished(
                self._current_turn_id,
                record,
            )
        except RunJournalError:
            # 已成功或结果未知的副作用工具调用必须写入可靠的结束记录。
            if record.may_have_side_effect:
                raise
            self._logger.warning("工具结束事件未能写入运行日志。")

    @staticmethod
    def _emit_event(
        callback: AgentEventCallback | None,
        event: AgentEvent,
    ) -> None:
        """在调用方订阅统一事件流时发送一个运行事件。"""
        if callback is not None:
            callback(event)

    def _finish_turn_outcome(
        self,
        *,
        status: RunStatus,
        final_text: str | None,
        steps_completed: int,
        checkpoint: int,
        history_preserved: bool,
        error_message: str | None = None,
        tool_calls_completed: int | None = None,
    ) -> TurnOutcome:
        """创建并保存最近一轮的结构化执行结果。"""
        outcome = TurnOutcome(
            status=status,
            final_text=final_text,
            steps_completed=steps_completed,
            tool_calls_completed=(
                self._count_tool_results(checkpoint)
                if tool_calls_completed is None
                else tool_calls_completed
            ),
            history_preserved=history_preserved,
            error_message=error_message,
            tool_records=tuple(self._current_turn_tool_records),
        )
        self._last_turn_outcome = outcome
        self._set_run_status(status)
        if (
            not any(
                record.may_have_side_effect
                for record in self._current_turn_tool_records
            )
            or self._current_turn_side_effects_saved
        ):
            self._record_turn_finished_safely(outcome)
        return outcome

    def _record_turn_started_safely(self, turn_id: str) -> None:
        """尽力记录轮次开始事件，且不因审计失败阻止纯文本对话。"""
        try:
            self._run_journal.record_turn_started(turn_id, self._session.id)
        except RunJournalError as error:
            self._logger.warning("无法记录轮次开始事件：%s", error)

    def _record_turn_finished_safely(self, outcome: TurnOutcome) -> None:
        """尽力记录轮次终止事件，且不覆盖原本的业务结果。"""
        if self._current_turn_id is None:
            return
        try:
            self._run_journal.record_turn_finished(
                self._current_turn_id,
                status=outcome.status.value,
                steps_completed=outcome.steps_completed,
                tool_calls_completed=outcome.tool_calls_completed,
                history_preserved=outcome.history_preserved,
                error_present=outcome.error_message is not None,
            )
        except RunJournalError as error:
            self._logger.warning("无法记录轮次结束事件：%s", error)

    def _load_recovery_issues(self) -> tuple[RecoveryIssue, ...]:
        """读取上次运行留下的悬空工具调用，并在失败时允许程序启动。"""
        try:
            return self._run_journal.find_recovery_issues()
        except RunJournalError as error:
            self._logger.warning("无法扫描待恢复的工具调用：%s", error)
            return ()

    def _begin_turn(self) -> None:
        """以原子方式开始新轮次，并拒绝同一实例上的并发调用。"""
        with self._run_status_lock:
            if self._run_status.active:
                raise RuntimeError("当前 Agent 已经有一轮任务正在执行。")
            self._run_status = RunStatus.RUNNING

    def _set_run_status(self, status: RunStatus) -> None:
        """在线程锁保护下更新当前轮次的运行状态。"""
        with self._run_status_lock:
            self._run_status = status

    def _count_tool_results(self, checkpoint: int) -> int:
        """统计当前轮次中已有结果记录的工具调用数量。"""
        return sum(
            1
            for message in self._conversation.messages[checkpoint:]
            if message.get("role") == "tool"
        )

    def _latest_assistant_text(self, checkpoint: int) -> str | None:
        """返回当前轮次最后一条非空助手文本。"""
        for message in reversed(self._conversation.messages[checkpoint:]):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if content:
                return str(content)
        return None

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
        # 如果调用已有对应的 ``tool`` 消息，就视为执行完成且结果已经记录。
        # 工具结果可以表示成功、失败或用户拒绝，但调用链结构必须始终完整。
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
        # 缺少结果会形成只有 ``tool_call`` 而没有 ``tool`` 的非法消息链。
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
            self._current_turn_side_effects_saved = True
            return
        # 纯文本回复没有外部副作用，因此采用尽力保存策略，避免保存失败阻断对话。
        self._save_session_best_effort()

    @property
    def last_turn_history_preserved(self) -> bool:
        return self._last_turn_history_preserved

    @property
    def last_turn_outcome(self) -> TurnOutcome | None:
        """返回最近一轮已经结束的结构化结果。"""
        return self._last_turn_outcome

    @property
    def run_status(self) -> RunStatus:
        """返回 Agent 当前轮次的实时运行状态。"""
        with self._run_status_lock:
            return self._run_status

    @property
    def recovery_issues(self) -> tuple[RecoveryIssue, ...]:
        """返回启动时发现的结果未知工具调用。"""
        return self._recovery_issues

    @property
    def model_name(self) -> str:
        return self._model.model_name

    @property
    def available_models(self) -> tuple[str, ...]:
        return self._model.available_models

    @property
    def tool_names(self) -> tuple[str, ...]:
        """返回当前已注册工具的名称。"""
        return self._tools.names

    @property
    def runtime_metrics(self) -> RuntimeMetrics:
        """返回当前进程累计的模型调用指标。"""
        return self._metrics

    @property
    def max_context_tokens(self) -> int:
        """返回发送给模型的消息 Token 预算。"""
        return self._context_manager.max_tokens

    def context_window(self) -> ContextWindow:
        """返回根据当前完整历史计算得到的上下文窗口。"""
        return self._context_manager.prepare(self._conversation.messages)

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
        # 移动会话前，补偿快照会保留磁盘中的完整 ``Session`` 对象。
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
        # 所有待提交的修改都先写入候选副本。
        # 当前 Session 和 Conversation 在磁盘保存成功前保持不变。
        candidate = self._copy_session(self._session)
        candidate.update_messages(cleared_messages)
        # 持久化阶段发生保存失败时会直接抛出异常。
        # 保存失败后不会执行内存提交，因此不需要恢复当前 Conversation。
        self._session_store.save(candidate)
        # 提交阶段：只有候选状态成功落盘后，才同步更新两个内存状态对象。
        self._conversation.restore(cleared_messages)
        self._session = candidate

    def load_session(self, session_id: str) -> Session:
        """先保存当前会话，再加载并提交目标会话。"""
        self._save_session()
        session = self._session_store.load(session_id)
        # 恢复操作会先校验消息结构，校验成功后才替换 ``Conversation``。
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
        # ``_create_new_session`` 返回前已经完成磁盘保存。
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
        # 保存操作抛出异常时不会替换内存对象，当前 ``Session`` 会保持原状。
        # 保存成功后才会把候选对象设置为新的当前 Session。
        self._session_store.save(candidate)
        self._session = candidate

    def _save_session_best_effort(self) -> bool:
        """尽力保存纯文本会话，并通过日志报告非关键保存失败。"""
        try:
            self._save_session()
        except (OSError, SessionStoreError) as error:
            self._logger.warning("会话保存失败：%s", error)
            return False
        return True
