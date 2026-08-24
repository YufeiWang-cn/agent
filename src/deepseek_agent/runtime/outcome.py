"""定义 Agent 实时运行状态和单轮结束后的结构化结果。"""

from dataclasses import dataclass
from enum import Enum

from ..tool_execution import ToolExecutionRecord


class RunStatus(str, Enum):
    """表示 Agent 当前轮次所处的运行状态。"""

    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    STEP_LIMIT_REACHED = "step_limit_reached"

    @property
    def succeeded(self) -> bool:
        """返回当前状态是否表示任务已经正常完成。"""
        return self is RunStatus.COMPLETED

    @property
    def active(self) -> bool:
        """返回当前状态是否表示一轮任务仍然占用 Agent。"""
        return self in {RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}

    @property
    def terminal(self) -> bool:
        """返回当前状态是否表示一轮任务已经结束。"""
        return self in {
            RunStatus.COMPLETED,
            RunStatus.WAITING_USER,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.STEP_LIMIT_REACHED,
        }


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """保存一轮 Agent 执行结束后的结构化结果。"""

    status: RunStatus
    final_text: str | None
    steps_completed: int
    tool_calls_completed: int
    history_preserved: bool
    error_message: str | None = None
    tool_records: tuple[ToolExecutionRecord, ...] = ()

    @property
    def succeeded(self) -> bool:
        """返回本轮任务是否已经正常完成。"""
        return self.status.succeeded
