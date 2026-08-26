"""保存单轮 Agent 执行期间的临时状态。"""

from dataclasses import dataclass, field

from ..tool_execution import ToolExecutionRecord


@dataclass(slots=True)
class TurnRuntimeState:
    """集中保存工具记录和运行日志提交状态。"""

    id: str | None = None
    tool_records: list[ToolExecutionRecord] = field(default_factory=list)
    side_effects_saved: bool = False
    finish_recorded: bool = False

    def begin(self, turn_id: str) -> None:
        """使用新标识开始轮次，并清空上一轮的临时状态。"""
        self.id = turn_id
        self.tool_records.clear()
        self.side_effects_saved = False
        self.finish_recorded = False

    def add_tool_record(self, record: ToolExecutionRecord) -> None:
        """按执行顺序保存一条完整工具记录。"""
        self.tool_records.append(record)

    @property
    def has_side_effects(self) -> bool:
        """返回本轮是否包含已知或可能存在副作用的工具。"""
        return any(record.may_have_side_effect for record in self.tool_records)


__all__ = ["TurnRuntimeState"]
