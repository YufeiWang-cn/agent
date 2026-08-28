"""编排模型、工具、上下文、持久化和运行恢复等 Agent 核心流程。"""

import logging
import threading
from dataclasses import dataclass
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
    Session,
    SessionCoordinator,
    SessionStoreError,
)
from .models import ChatModel, DeepSeekModel, TextDelta, ToolCallRequest
from .observability import RuntimeMetrics
from .plan_runtime import PlanRuntime
from .permissions import ConsoleToolConfirmer, ToolConfirmer
from .planning import (
    PlanExecutionScope,
    PlanKind,
    TaskPlan,
)
from .reliability import RetryPolicy, RetryingChatModel
from .runtime import AgentEvent, RunStatus, TurnOutcome, TurnRuntimeState
from .tools import ToolEffect, ToolRegistry, build_default_registry


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


@dataclass(frozen=True, slots=True)
class TurnCallbacks:
    """集中保存一轮对话使用的流式回调和取消检查。"""

    on_text: TextCallback | None
    on_tool_call: ToolCallCallback | None
    on_tool_result: ToolResultCallback | None
    on_event: AgentEventCallback | None
    should_cancel: CancelCheck | None


@dataclass(slots=True)
class TurnProgress:
    """保存单轮执行的回滚检查点和循环进度。"""

    checkpoint: int
    plan_checkpoint: TaskPlan | None
    steps_completed: int = 0
    force_plan_continuation: bool = False


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
        effective_retry_policy = retry_policy or RetryPolicy(
            max_retries=settings.max_retries,
            base_delay_seconds=settings.retry_base_delay,
        )
        self._model = RetryingChatModel(
            base_model,
            effective_retry_policy,
            self._metrics,
            logger=logger,
        )
        # 计划运行时独立维护计划快照和单步边界，避免把界面状态混入消息协议。
        self._plan_runtime = PlanRuntime()
        self._max_agent_steps = settings.max_agent_steps
        self._max_finalization_steps = settings.max_finalization_steps
        self._tools = (
            tools
            if tools is not None
            else build_default_registry(
                settings.workspace_root,
                settings.max_file_size,
                settings.command_timeout,
                settings.max_command_output,
                plan_updater=self._commit_plan,
            )
        )
        self._conversation = Conversation(settings.system_prompt)
        effective_session_store = session_store or JsonSessionStore(
            Path(DEFAULT_SESSION_DIRECTORY)
        )
        journal_directory = (
            Path(DEFAULT_RUN_DIRECTORY)
            if session_store is None
            else effective_session_store.directory / ".runs"
        )
        self._run_journal = run_journal or RunJournal(journal_directory)
        self._recovery_issues = self._load_recovery_issues()
        effective_project_store = project_store or JsonProjectStore(
            Path(DEFAULT_PROJECT_FILE)
        )
        self._sessions = SessionCoordinator(
            self._conversation,
            effective_session_store,
            effective_project_store,
        )
        self._context_manager = context_manager or ContextManager(
            settings.max_context_tokens
        )
        self._confirmer = (
            confirmer if confirmer is not None else ConsoleToolConfirmer()
        )
        self._tool_executor = ToolExecutor(self._tools, self._confirmer)
        self._plan_runtime.restore(self._sessions.current.plan)
        # 回滚结果状态用于记录最近一次中断所采用的处理方式。
        # 界面根据该状态决定删除临时轮次还是重新渲染工具历史。
        self._last_turn_history_preserved = False
        # 结构化结果用于准确描述最近一轮的结束原因和实际完成情况。
        self._last_turn_outcome: TurnOutcome | None = None
        # 状态锁确保同一个 Agent 实例不会同时执行两个轮次。
        self._run_status_lock = threading.Lock()
        self._run_status = RunStatus.IDLE
        self._turn_state = TurnRuntimeState()

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
        已经完成的工具可能产生无法自动撤销的外部副作用。
        系统会优先保留相应工具记录，使会话历史与真实状态保持一致。
        """
        progress = self._start_turn(prompt)
        callbacks = TurnCallbacks(
            on_text,
            on_tool_call,
            on_tool_result,
            on_event,
            should_cancel,
        )
        try:
            return self._run_turn_loop(progress, callbacks)
        except AgentCancelledError as error:
            self._handle_cancelled_turn(error, progress)
            raise
        except Exception as error:
            self._handle_failed_turn(error, progress)
            raise

    def _start_turn(self, prompt: str) -> TurnProgress:
        """建立回滚检查点，并初始化本轮运行状态。"""
        progress = TurnProgress(
            checkpoint=len(self._conversation),
            plan_checkpoint=self._current_plan,
        )
        self._begin_turn()
        turn_id = uuid4().hex
        self._turn_state.begin(turn_id)
        self._record_turn_started_safely(turn_id)
        self._last_turn_history_preserved = False
        self._last_turn_outcome = None
        self._conversation.add_user(prompt)
        return progress

    def _run_turn_loop(
        self,
        progress: TurnProgress,
        callbacks: TurnCallbacks,
    ) -> TurnOutcome:
        """持续执行模型和工具，直到得到明确的轮次终止状态。"""
        self._emit_event(callbacks.on_event, AgentEvent.turn_started())
        for _step in range(self._max_agent_steps):
            answer_parts, tool_requests = self._stream_model_step(
                progress,
                callbacks,
            )
            progress.steps_completed += 1

            if tool_requests and self._single_step_boundary_reached():
                return self._finish_single_step_boundary(
                    answer_parts,
                    progress,
                    callbacks,
                )

            if not tool_requests:
                outcome = self._handle_text_response(
                    answer_parts,
                    progress,
                    callbacks,
                )
                if outcome is not None:
                    return outcome
                continue

            self._conversation.add_assistant_tool_calls(
                [request.as_message_dict() for request in tool_requests],
                content="".join(answer_parts) or None,
            )
            self._execute_tools(
                tool_requests,
                on_tool_call=callbacks.on_tool_call,
                on_tool_result=callbacks.on_tool_result,
                on_event=callbacks.on_event,
                should_cancel=callbacks.should_cancel,
            )
            progress.force_plan_continuation = False

        return self._run_finalization_loop(progress, callbacks)

    def _run_finalization_loop(
        self,
        progress: TurnProgress,
        callbacks: TurnCallbacks,
    ) -> TurnOutcome:
        """在常规预算耗尽后，只允许闭合计划和生成最终回答。"""
        for _step in range(self._max_finalization_steps):
            answer_parts, tool_requests = self._stream_model_step(
                progress,
                callbacks,
                finalization=True,
            )
            progress.steps_completed += 1

            if any(request.name != "update_plan" for request in tool_requests):
                return self._finish_step_limit(progress, callbacks)

            if tool_requests:
                self._conversation.add_assistant_tool_calls(
                    [request.as_message_dict() for request in tool_requests],
                    content="".join(answer_parts) or None,
                )
                self._execute_tools(
                    tool_requests,
                    on_tool_call=callbacks.on_tool_call,
                    on_tool_result=callbacks.on_tool_result,
                    on_event=callbacks.on_event,
                    should_cancel=callbacks.should_cancel,
                )
                progress.force_plan_continuation = False
                continue

            outcome = self._handle_text_response(
                answer_parts,
                progress,
                callbacks,
            )
            if outcome is not None:
                return outcome

        return self._finish_step_limit(progress, callbacks)

    def _stream_model_step(
        self,
        progress: TurnProgress,
        callbacks: TurnCallbacks,
        *,
        finalization: bool = False,
    ) -> tuple[list[str], list[ToolCallRequest]]:
        """收集一次模型响应，并立即转发文本增量事件。"""
        self._raise_if_cancelled(callbacks.should_cancel)
        answer_parts: list[str] = []
        tool_requests: list[ToolCallRequest] = []
        model_messages = self._prepare_model_messages(
            force_plan_continuation=progress.force_plan_continuation,
            finalization=finalization,
        )
        tool_schemas = self._tools.schemas
        if finalization:
            tool_schemas = [
                schema
                for schema in tool_schemas
                if schema.get("function", {}).get("name") == "update_plan"
            ]
        for event in self._model.stream(model_messages, tool_schemas):
            self._raise_if_cancelled(callbacks.should_cancel)
            if isinstance(event, TextDelta):
                self._emit_text(event.content, callbacks)
                answer_parts.append(event.content)
            elif isinstance(event, ToolCallRequest):
                tool_requests.append(event)
        return answer_parts, tool_requests

    def _handle_text_response(
        self,
        answer_parts: list[str],
        progress: TurnProgress,
        callbacks: TurnCallbacks,
    ) -> TurnOutcome | None:
        """处理没有工具请求的模型响应，并判断计划是否需要继续。"""
        answer = "".join(answer_parts)
        if not answer:
            answer = "（模型未返回内容）"
            self._emit_text(answer, callbacks)
        self._conversation.add_assistant(answer)

        if self._has_active_execution_plan():
            current_plan = self._current_plan
            assert current_plan is not None
            if current_plan.waiting_for_user:
                return self._complete_turn(
                    RunStatus.WAITING_USER,
                    answer,
                    progress,
                )
            if self._single_step_boundary_reached():
                return self._complete_turn(
                    RunStatus.COMPLETED,
                    answer,
                    progress,
                )
            # 执行计划未结束时，下一次请求会收到仅存在于运行时的继续提示。
            progress.force_plan_continuation = True
            return None
        return self._complete_turn(RunStatus.COMPLETED, answer, progress)

    def _finish_single_step_boundary(
        self,
        answer_parts: list[str],
        progress: TurnProgress,
        callbacks: TurnCallbacks,
    ) -> TurnOutcome:
        """在单步目标结束后丢弃额外工具请求，并闭合当前轮次。"""
        current_plan = self._current_plan
        assert current_plan is not None
        waiting_for_user = current_plan.waiting_for_user
        answer = "".join(answer_parts) or (
            "当前步骤正在等待用户输入。"
            if waiting_for_user
            else "当前目标步骤已经结束。"
        )
        if not answer_parts:
            self._emit_text(answer, callbacks)
        self._conversation.add_assistant(answer)
        status = (
            RunStatus.WAITING_USER
            if waiting_for_user
            else RunStatus.COMPLETED
        )
        return self._complete_turn(status, answer, progress)

    def _finish_step_limit(
        self,
        progress: TurnProgress,
        callbacks: TurnCallbacks,
    ) -> TurnOutcome:
        """记录最大执行步数，并以未完成状态结束当前轮次。"""
        message = (
            f"Agent 已达到最大执行步数：常规步骤 {self._max_agent_steps} 和收尾步骤 "
            f"{self._max_finalization_steps} 的上限，任务已停止。"
        )
        self._conversation.add_assistant(message)
        self._save_completed_turn(progress.checkpoint)
        self._emit_text(message, callbacks)
        return self._finish_turn_outcome(
            status=RunStatus.STEP_LIMIT_REACHED,
            final_text=message,
            steps_completed=progress.steps_completed,
            checkpoint=progress.checkpoint,
            history_preserved=True,
        )

    def _complete_turn(
        self,
        status: RunStatus,
        final_text: str,
        progress: TurnProgress,
    ) -> TurnOutcome:
        self._save_completed_turn(progress.checkpoint)
        return self._finish_turn_outcome(
            status=status,
            final_text=final_text,
            steps_completed=progress.steps_completed,
            checkpoint=progress.checkpoint,
            history_preserved=True,
        )

    def _handle_cancelled_turn(
        self,
        error: AgentCancelledError,
        progress: TurnProgress,
    ) -> None:
        outcome, records_preserved = self._resolve_interrupted_turn(
            status=RunStatus.CANCELLED,
            progress=progress,
            pending_result="用户停止了本轮任务，本工具未执行。",
            final_message="本轮已停止。停止前已完成的工具操作及其记录已保留。",
            error_message=str(error),
        )
        error.tool_records_preserved = records_preserved
        error.outcome = outcome
        if records_preserved:
            self._persist_interrupted_records(outcome, progress)

    def _handle_failed_turn(
        self,
        error: Exception,
        progress: TurnProgress,
    ) -> None:
        outcome, records_preserved = self._resolve_interrupted_turn(
            status=RunStatus.FAILED,
            progress=progress,
            pending_result="本轮因调用失败而中断，本工具未执行。",
            final_message=f"本轮因调用失败而中断：{error}",
            error_message=str(error),
        )
        if records_preserved:
            self._persist_interrupted_records(outcome, progress)

    def _resolve_interrupted_turn(
        self,
        *,
        status: RunStatus,
        progress: TurnProgress,
        pending_result: str,
        final_message: str,
        error_message: str,
    ) -> tuple[TurnOutcome, bool]:
        """根据已完成工具决定保留记录还是回滚临时消息。"""
        completed_tool_calls = self._count_tool_results(progress.checkpoint)
        records_preserved = self._preserve_completed_tool_records(
            progress.checkpoint,
            pending_result=pending_result,
            final_message=final_message,
        )
        self._last_turn_history_preserved = records_preserved
        if not records_preserved:
            # 没有工具完成时，本轮不存在已知外部副作用，可以安全截断历史。
            self._conversation.truncate(progress.checkpoint)
            self._current_plan = progress.plan_checkpoint
        outcome = self._finish_turn_outcome(
            status=status,
            final_text=self._latest_assistant_text(progress.checkpoint),
            steps_completed=progress.steps_completed,
            checkpoint=progress.checkpoint,
            history_preserved=records_preserved,
            error_message=error_message,
            tool_calls_completed=completed_tool_calls,
        )
        return outcome, records_preserved

    def _persist_interrupted_records(
        self,
        outcome: TurnOutcome,
        progress: TurnProgress,
    ) -> None:
        """严格保存可能对应外部副作用的工具记录。"""
        try:
            self._save_session()
            self._turn_state.side_effects_saved = True
            self._record_turn_finished_safely(outcome)
        except Exception as save_error:
            self._finish_turn_outcome(
                status=RunStatus.FAILED,
                final_text=self._latest_assistant_text(progress.checkpoint),
                steps_completed=progress.steps_completed,
                checkpoint=progress.checkpoint,
                history_preserved=True,
                error_message=str(save_error),
            )
            raise

    def _emit_text(self, content: str, callbacks: TurnCallbacks) -> None:
        """同时发送统一事件和兼容的纯文本回调。"""
        self._emit_event(callbacks.on_event, AgentEvent.text_delta(content))
        if callbacks.on_text is not None:
            callbacks.on_text(content)

    def _execute_tools(
        self,
        requests: list[ToolCallRequest],
        *,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
        on_event: AgentEventCallback | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> list[ToolExecutionRecord]:
        if self._turn_state.id is None:
            # 私有方法的测试或扩展调用可能绕过 chat()，此时仍需建立可追踪的临时轮次。
            turn_id = uuid4().hex
            self._turn_state.begin(turn_id)
            self._record_turn_started_safely(turn_id)
        records: list[ToolExecutionRecord] = []
        for request in requests:
            # 工具开始前需要检查停止信号。
            # 工具一旦开始执行就应完整结束，避免强行中断写入造成半写状态。
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
                on_confirmation_state=self._confirmation_callback(request),
                before_execution=self._record_tool_started,
                # GUI 的停止信号会继续传递给长时间运行的命令，而不只在工具之间检查。
                should_cancel=should_cancel,
            )
            records.append(record)
            self._turn_state.add_tool_record(record)

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
            if request.name == "update_plan" and record.status.succeeded:
                # 计划事件必须在工具结果入历史后发送，确保回调失败时仍可恢复。
                self._record_plan_updated_safely()
                self._emit_event(
                    on_event,
                    AgentEvent.plan_updated(self._current_plan),
                )
            # 界面回调发生异常时，已写入的工具结果仍可避免误回滚。
            if on_tool_result is not None:
                on_tool_result(request, record.model_result)
        return records

    def _confirmation_callback(
        self,
        request: ToolCallRequest,
    ) -> Callable[[bool], None]:
        """为当前工具请求创建带静态类型的确认状态回调。"""
        def handle(waiting: bool) -> None:
            self._handle_confirmation_state_for_request(request, waiting)

        return handle

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
        """更新确认状态，并尽力记录工具已进入等待确认阶段。"""
        self._handle_confirmation_state(waiting)
        turn_id = self._turn_state.id
        if not waiting or turn_id is None:
            return
        try:
            self._run_journal.record_confirmation_requested(
                turn_id,
                request,
            )
        except RunJournalError as error:
            self._logger.warning("无法记录工具确认请求：%s", error)

    def _record_tool_started(self, start: ToolExecutionStart) -> None:
        """在工具执行前记录开始事件，并保护可能产生副作用的操作。"""
        turn_id = self._turn_state.id
        if turn_id is None:
            raise RunJournalError("当前工具调用缺少轮次标识。")
        try:
            self._run_journal.record_tool_started(
                turn_id,
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
        turn_id = self._turn_state.id
        if turn_id is None:
            raise RunJournalError("当前工具结果缺少轮次标识。")
        try:
            self._run_journal.record_tool_finished(
                turn_id,
                record,
            )
        except RunJournalError:
            # 已成功或结果未知的副作用工具调用必须写入可靠的结束记录。
            if record.may_have_side_effect:
                raise
            self._logger.warning("工具结束事件未能写入运行日志。")

    def _record_plan_updated_safely(self) -> None:
        """尽力记录不含步骤正文的计划状态变化。"""
        turn_id = self._turn_state.id
        if turn_id is None or self._current_plan is None:
            return
        try:
            self._run_journal.record_plan_updated(
                turn_id,
                self._current_plan,
            )
        except RunJournalError as error:
            self._logger.warning("无法记录计划更新事件：%s", error)

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
            tool_records=tuple(self._turn_state.tool_records),
        )
        self._last_turn_outcome = outcome
        self._set_run_status(status)
        if not self._turn_state.has_side_effects or self._turn_state.side_effects_saved:
            self._record_turn_finished_safely(outcome)
        return outcome

    def _record_turn_started_safely(self, turn_id: str) -> None:
        """尽力记录轮次开始事件，且不因审计失败阻止纯文本对话。"""
        try:
            self._run_journal.record_turn_started(turn_id, self._sessions.current.id)
        except RunJournalError as error:
            self._logger.warning("无法记录轮次开始事件：%s", error)

    def _record_turn_finished_safely(self, outcome: TurnOutcome) -> None:
        """尽力记录轮次终止事件，且不覆盖原本的业务结果。"""
        turn_id = self._turn_state.id
        if turn_id is None or self._turn_state.finish_recorded:
            return
        try:
            self._run_journal.record_turn_finished(
                turn_id,
                status=outcome.status.value,
                steps_completed=outcome.steps_completed,
                tool_calls_completed=outcome.tool_calls_completed,
                history_preserved=outcome.history_preserved,
                error_present=outcome.error_message is not None,
            )
            # 同一轮可能经过“生成结果、保存记录、保存失败处理”等多个收尾路径。
            # 只有首次成功写入才关闭日志边界，避免重复的 turn_finished 事件。
            self._turn_state.finish_recorded = True
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
        self._plan_runtime.begin_turn()

    def _commit_plan(
        self,
        plan: TaskPlan,
        replace_current: bool = False,
    ) -> TaskPlan:
        """把模型提交的计划交给独立计划运行时处理。"""
        return self._plan_runtime.commit(plan, replace_current)

    # 这些兼容属性保留既有内部扩展和测试入口，状态仍由 PlanRuntime 统一维护。
    @property
    def _current_plan(self) -> TaskPlan | None:
        return self._plan_runtime.current

    @_current_plan.setter
    def _current_plan(self, plan: TaskPlan | None) -> None:
        self._plan_runtime.restore(plan)

    @property
    def _current_turn_step_target(self) -> str | None:
        return self._plan_runtime.target

    @_current_turn_step_target.setter
    def _current_turn_step_target(self, target: str | None) -> None:
        self._plan_runtime.target = target

    @property
    def _current_turn_step_boundary_reached(self) -> bool:
        return self._plan_runtime.boundary_reached

    @_current_turn_step_boundary_reached.setter
    def _current_turn_step_boundary_reached(self, reached: bool) -> None:
        self._plan_runtime.boundary_reached = reached

    @property
    def _current_turn_plan_scope(self) -> PlanExecutionScope | None:
        return self._plan_runtime.scope

    @_current_turn_plan_scope.setter
    def _current_turn_plan_scope(
        self,
        scope: PlanExecutionScope | None,
    ) -> None:
        self._plan_runtime.scope = scope

    def _prepare_model_messages(
        self,
        *,
        force_plan_continuation: bool,
        finalization: bool = False,
    ) -> list[Message]:
        """注入计划运行时约束，再按上下文预算裁剪消息。"""
        messages = self._plan_runtime.attach_to_messages(
            self._conversation.messages,
            force_continuation=force_plan_continuation,
            finalization=finalization,
        )
        return list(self._context_manager.prepare(messages).messages)

    def _has_active_execution_plan(self) -> bool:
        """返回当前是否存在尚未结束的执行计划。"""
        plan = self._current_plan
        return plan is not None and plan.kind is PlanKind.EXECUTION and not plan.terminal

    def _single_step_boundary_reached(self) -> bool:
        """返回当前计划是否已到达本轮单步边界。"""
        plan = self._current_plan
        uses_single_step = (
            plan is not None and plan.scope is PlanExecutionScope.SINGLE_STEP
        )
        return uses_single_step and self._plan_runtime.boundary_reached

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
            if message.get("role") == "tool" and message.get("tool_call_id")
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
            self._turn_state.side_effects_saved = True
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

    @property
    def max_agent_steps(self) -> int:
        """返回单轮允许的最大模型执行步数。"""
        return self._max_agent_steps

    @property
    def current_plan(self) -> TaskPlan | None:
        """返回当前会话最近一次公开任务计划快照。"""
        return self._current_plan

    def context_window(self) -> ContextWindow:
        """返回根据当前完整历史计算得到的上下文窗口。"""
        return self._context_manager.prepare(self._conversation.messages)

    def select_model(self, model_name: str) -> None:
        self._model.select_model(model_name)

    @property
    def session_id(self) -> str:
        return self._sessions.current.id

    @property
    def session_title(self) -> str:
        return self._sessions.current.title

    def history(self) -> list[Message]:
        return self._conversation.visible_history()

    def list_sessions(self) -> list[Session]:
        return self._sessions.list_sessions()

    def list_projects(self) -> list[Project]:
        return self._sessions.list_projects()

    def create_project(self, name: str) -> Project:
        return self._sessions.create_project(name)

    def rename_project(self, project_id: str, name: str) -> Project:
        return self._sessions.rename_project(project_id, name)

    def delete_project(self, project_id: str) -> Project:
        return self._sessions.delete_project(project_id, self._current_plan)

    def start_new_session(self, project_id: str | None = None) -> Session:
        session = self._sessions.start_new_session(self._current_plan, project_id)
        self._plan_runtime.restore(session.plan)
        return session

    def clear_conversation(self) -> None:
        session = self._sessions.clear_conversation()
        self._plan_runtime.restore(session.plan)

    def load_session(self, session_id: str) -> Session:
        session = self._sessions.load_session(session_id, self._current_plan)
        self._plan_runtime.restore(session.plan)
        return session

    def delete_session(
        self,
        session_id: str,
        replacement_project_id: str | None = None,
    ) -> str:
        current_session_id = self._sessions.current.id
        deleted_id = self._sessions.delete_session(
            session_id,
            replacement_project_id,
        )
        if deleted_id == current_session_id:
            self._plan_runtime.restore(self._sessions.current.plan)
        return deleted_id

    def rename_session(self, session_id: str, title: str) -> Session:
        return self._sessions.rename_session(
            session_id,
            title,
            self._current_plan,
        )

    def move_session(
        self,
        session_id: str,
        project_id: str | None,
    ) -> Session:
        return self._sessions.move_session(
            session_id,
            project_id,
            self._current_plan,
        )

    def save_session(self) -> None:
        self._save_session()

    def _save_session(self) -> None:
        self._sessions.save_current(self._current_plan)

    def _save_session_best_effort(self) -> bool:
        """尽力保存纯文本会话，并通过日志报告非关键保存失败。"""
        try:
            self._save_session()
        except (OSError, SessionStoreError) as error:
            self._logger.warning("会话保存失败：%s", error)
            return False
        return True
