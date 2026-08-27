"""统一管理工具调用的解析、确认、执行和结果记录生命周期。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from time import monotonic

from .models import ToolCallRequest
from .permissions import ToolConfirmer
from .tools import (
    JsonObject,
    Tool,
    ToolEffect,
    ToolError,
    ToolExecutionContext,
    ToolExecutionError,
    ToolRegistry,
)


class ToolExecutionStatus(str, Enum):
    """表示一次工具调用的最终执行状态。"""

    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    RESULT_UNKNOWN = "result_unknown"

    @property
    def succeeded(self) -> bool:
        """返回工具是否已经明确执行成功。"""
        return self is ToolExecutionStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class ToolExecutionStart:
    """保存工具实际执行前需要持久化的身份和策略信息。"""

    call_id: str
    tool_name: str
    raw_arguments: str
    arguments: JsonObject
    effect: ToolEffect
    retryable: bool
    idempotent: bool
    supports_rollback: bool
    timeout_seconds: float | None


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    """保存一次工具调用的参数、策略、结果和时间信息。"""

    call_id: str
    tool_name: str
    raw_arguments: str
    arguments: JsonObject | None
    status: ToolExecutionStatus
    effect: ToolEffect
    model_result: str
    started_at: datetime
    finished_at: datetime
    confirmation_requested: bool
    confirmation_granted: bool | None
    retryable: bool
    idempotent: bool
    supports_rollback: bool
    timeout_seconds: float | None
    execution_started: bool
    error_message: str | None = None

    @property
    def duration_seconds(self) -> float:
        """返回本次工具调用从解析到完成所经过的秒数。"""
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    @property
    def may_have_side_effect(self) -> bool:
        """返回工具是否已经或可能改变了外部状态。"""
        if self.effect is ToolEffect.READ_ONLY or not self.execution_started:
            return False
        return self.status in {
            ToolExecutionStatus.SUCCEEDED,
            ToolExecutionStatus.RESULT_UNKNOWN,
        }

    @property
    def can_retry_safely(self) -> bool:
        """返回系统是否可以依据工具声明安全地重试本次调用。"""
        retryable_status = self.status in {
            ToolExecutionStatus.FAILED,
            ToolExecutionStatus.RESULT_UNKNOWN,
        }
        return retryable_status and self.retryable and self.idempotent


Clock = Callable[[], datetime]
ConfirmationStateCallback = Callable[[bool], None]
BeforeExecutionCallback = Callable[[ToolExecutionStart], None]


class ToolExecutor:
    """统一解析、确认并执行工具，确保每次调用都有结构化结果记录。"""

    def __init__(
        self,
        registry: ToolRegistry,
        confirmer: ToolConfirmer,
        *,
        clock: Clock | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._registry = registry
        self._confirmer = confirmer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock

    def execute(
        self,
        request: ToolCallRequest,
        *,
        on_confirmation_state: ConfirmationStateCallback | None = None,
        before_execution: BeforeExecutionCallback | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ToolExecutionRecord:
        """执行一次工具调用，并始终以结构化记录返回实际结果。"""
        started_at = self._clock()
        tool: Tool | None = None
        arguments: JsonObject | None = None
        confirmation_requested = False
        confirmation_granted: bool | None = None
        execution_started = False
        effect = ToolEffect.READ_ONLY

        try:
            tool, arguments = self._registry.prepare(
                request.name,
                request.arguments,
            )
            # 风险必须根据本次调用参数计算。
            # 例如，run_command 执行 git status 时属于只读操作。
            # 执行 git push 时会产生外部副作用，因此必须要求确认。
            effect = tool.effect_for(arguments)
            confirmation_required = tool.requires_confirmation_for(arguments)
            self._raise_if_cancellation_requested(should_cancel)
            if confirmation_required:
                confirmation_requested = True
                self._notify_confirmation_state(on_confirmation_state, True)
                try:
                    confirmation_granted = self._confirmer.confirm(
                        tool,
                        tool.confirmation_arguments_for(
                            arguments,
                            request.arguments,
                        ),
                    )
                finally:
                    self._notify_confirmation_state(on_confirmation_state, False)
                if not confirmation_granted:
                    return self._record(
                        request,
                        tool=tool,
                        effect=effect,
                        arguments=arguments,
                        status=ToolExecutionStatus.REJECTED,
                        model_result="用户拒绝执行该工具。",
                        started_at=started_at,
                        confirmation_requested=True,
                        confirmation_granted=False,
                        execution_started=False,
                    )
                # 用户确认期间仍可能点击停止，因此执行前需要再次检查取消信号。
                self._raise_if_cancellation_requested(should_cancel)

            start = ToolExecutionStart(
                call_id=request.id,
                tool_name=tool.name,
                raw_arguments=request.arguments,
                arguments=arguments,
                effect=effect,
                retryable=tool.retryable,
                idempotent=tool.idempotent,
                supports_rollback=tool.supports_rollback,
                timeout_seconds=tool.timeout_seconds,
            )
            if before_execution is not None:
                # 可能产生副作用的工具必须先把“即将执行”写入运行日志。
                # 完成日志写入后，系统才能真正启动命令。
                # 否则，崩溃恢复将无法判断命令是否已经执行。
                before_execution(start)
            self._raise_if_cancellation_requested(should_cancel)
            deadline = (
                self._monotonic_clock() + tool.timeout_seconds
                if tool.timeout_seconds is not None
                else None
            )
            execution_context = ToolExecutionContext(
                should_cancel=should_cancel,
                deadline=deadline,
                clock=self._monotonic_clock,
            )
            execution_started = True
            result = tool.execute_with_context(
                arguments,
                # 长时间运行的工具通过统一上下文协作处理取消和超时。
                execution_context,
            )
            return self._record(
                request,
                tool=tool,
                effect=effect,
                arguments=arguments,
                status=ToolExecutionStatus.SUCCEEDED,
                model_result=result,
                started_at=started_at,
                confirmation_requested=confirmation_requested,
                confirmation_granted=confirmation_granted,
                execution_started=True,
            )
        except ToolExecutionError as error:
            status = (
                ToolExecutionStatus.RESULT_UNKNOWN
                if error.side_effect_possible
                else ToolExecutionStatus.FAILED
            )
            return self._record(
                request,
                tool=tool,
                effect=effect,
                arguments=arguments,
                status=status,
                model_result=f"工具执行失败：{error}",
                started_at=started_at,
                confirmation_requested=confirmation_requested,
                confirmation_granted=confirmation_granted,
                execution_started=execution_started,
                error_message=str(error),
            )
        except ToolError as error:
            return self._record(
                request,
                tool=tool,
                effect=effect,
                arguments=arguments,
                status=ToolExecutionStatus.FAILED,
                model_result=f"工具执行失败：{error}",
                started_at=started_at,
                confirmation_requested=confirmation_requested,
                confirmation_granted=confirmation_granted,
                execution_started=execution_started,
                error_message=str(error),
            )
        except Exception as error:
            write_tool = tool is not None and effect is not ToolEffect.READ_ONLY
            side_effect_possible = execution_started and write_tool
            status = (
                ToolExecutionStatus.RESULT_UNKNOWN
                if side_effect_possible
                else ToolExecutionStatus.FAILED
            )
            return self._record(
                request,
                tool=tool,
                effect=effect,
                arguments=arguments,
                status=status,
                model_result=f"工具发生未预期错误：{error}",
                started_at=started_at,
                confirmation_requested=confirmation_requested,
                confirmation_granted=confirmation_granted,
                execution_started=execution_started,
                error_message=str(error),
            )

    @staticmethod
    def _notify_confirmation_state(
        callback: ConfirmationStateCallback | None,
        waiting: bool,
    ) -> None:
        """在调用方需要同步运行状态时通知确认阶段的开始或结束。"""
        if callback is not None:
            callback(waiting)

    @staticmethod
    def _raise_if_cancellation_requested(
        should_cancel: Callable[[], bool] | None,
    ) -> None:
        """在工具尚未启动时安全地响应调用方取消请求。"""
        if should_cancel is not None and should_cancel():
            raise ToolExecutionError("工具执行已取消。")

    def _record(
        self,
        request: ToolCallRequest,
        *,
        tool: Tool | None,
        effect: ToolEffect,
        arguments: JsonObject | None,
        status: ToolExecutionStatus,
        model_result: str,
        started_at: datetime,
        confirmation_requested: bool,
        confirmation_granted: bool | None,
        execution_started: bool,
        error_message: str | None = None,
    ) -> ToolExecutionRecord:
        """使用工具声明和实际结果创建不可变的执行记录。"""
        return ToolExecutionRecord(
            call_id=request.id,
            tool_name=request.name,
            raw_arguments=request.arguments,
            arguments=arguments,
            status=status,
            effect=effect,
            model_result=model_result,
            started_at=started_at,
            finished_at=self._clock(),
            confirmation_requested=confirmation_requested,
            confirmation_granted=confirmation_granted,
            retryable=tool.retryable if tool is not None else False,
            idempotent=tool.idempotent if tool is not None else False,
            supports_rollback=(
                tool.supports_rollback if tool is not None else False
            ),
            timeout_seconds=(
                tool.timeout_seconds if tool is not None else None
            ),
            execution_started=execution_started,
            error_message=error_message,
        )


__all__ = [
    "ToolExecutionRecord",
    "ToolExecutionStart",
    "ToolExecutionStatus",
    "ToolExecutor",
]
