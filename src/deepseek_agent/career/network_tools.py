"""仅用于求职注册表的外发保护适配器；确认由现有 ToolExecutor 执行。"""

from copy import deepcopy

from ..tools import JsonObject, Tool, ToolEffect, ToolExecutionContext, ToolExecutionError
from .privacy import check_outbound, outbound_preview


class CareerNetworkTool(Tool):
    requires_confirmation = True
    effect = ToolEffect.EXTERNAL_SIDE_EFFECT

    def __init__(self, tool: Tool) -> None:
        self._tool = tool
        self.name = tool.name
        self.description = tool.description + (
            " 求职模式每次联网均需确认。仅发送岗位、城市、技术栈等公开条件；"
            "不要发送姓名、联系方式、完整简历或私人经历。敏感内容会被阻止。"
        )
        self.parameters = deepcopy(tool.parameters)
        self.confirmation_description = "将查询/网页参数发给第三方服务，请核对下方完整内容。"
        self.timeout_seconds = tool.timeout_seconds
        self.retryable = tool.retryable
        self.idempotent = tool.idempotent
        self.supports_rollback = tool.supports_rollback

    def begin_turn(self) -> None:
        self._tool.begin_turn()

    def _validate(self, arguments: JsonObject) -> None:
        if set(arguments) - set(self.parameters.get("properties", {})):
            raise ToolExecutionError("联网调用包含未知参数，请只提供工具声明的公开查询字段。")
        check_outbound(arguments)
        outbound_preview(arguments)

    def requires_confirmation_for(self, arguments: JsonObject) -> bool:
        self._validate(arguments)
        # 保留底层参数、URL、域名等校验，不沿用其自动批准额度。
        self._tool.requires_confirmation_for(arguments)
        return True

    def confirmation_arguments_for(self, arguments: JsonObject, raw_arguments: str) -> str:
        self._validate(arguments)
        return outbound_preview(arguments)

    def execute(self, arguments: JsonObject) -> str:
        return self.execute_with_context(arguments, ToolExecutionContext())

    def execute_with_context(self, arguments: JsonObject, context: ToolExecutionContext) -> str:
        # 到达这里代表受信任调用方已完成确认；再次检查，避免只在预览时检查。
        self._validate(arguments)
        return self._tool.execute_with_context(arguments, context)
