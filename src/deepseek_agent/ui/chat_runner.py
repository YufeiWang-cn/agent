"""在后台线程中运行 Agent，并把结果转换为 GUI 队列事件。"""

import queue
import threading
from typing import Any

from ..agent import Agent, AgentCancelledError
from ..runtime import AgentEvent, AgentEventType, RunStatus


class AgentChatRunner:
    """隔离 Agent 调用、取消检查和终止状态映射。"""

    def __init__(
        self,
        agent: Agent,
        event_queue: queue.Queue[tuple[str, Any]],
        cancel: threading.Event,
    ) -> None:
        self._agent = agent
        self._event_queue = event_queue
        self._cancel = cancel

    def run(self, prompt: str) -> None:
        """执行一轮对话，并保证 GUI 总能收到明确的终止事件。"""
        try:
            outcome = self._agent.chat(
                prompt,
                on_text=lambda content: self._put("text", content),
                on_tool_call=lambda request: self._put("tool_call", request),
                on_tool_result=lambda request, result: self._put(
                    "tool_result",
                    (request, result),
                ),
                on_event=self._queue_runtime_event,
                should_cancel=self._cancel.is_set,
            )
        except AgentCancelledError as error:
            outcome = error.outcome
            history_preserved = (
                outcome.history_preserved
                if outcome is not None
                else error.tool_records_preserved
            )
            self._put("cancelled", (str(error), history_preserved))
        except Exception as error:
            outcome = self._agent.last_turn_outcome
            history_preserved = (
                outcome.history_preserved
                if outcome is not None
                else self._agent.last_turn_history_preserved
            )
            self._put("error", (str(error), history_preserved))
        else:
            if outcome.status is RunStatus.STEP_LIMIT_REACHED:
                self._put("step_limit_reached", outcome)
            elif outcome.status is RunStatus.WAITING_USER:
                self._put("waiting_user", outcome)
            else:
                self._put("done", outcome)

    def _queue_runtime_event(self, event: AgentEvent) -> None:
        """转发需要在 GUI 中即时呈现的结构化运行事件。"""
        if event.type is AgentEventType.PLAN_UPDATED:
            self._put("plan_updated", event.plan)

    def _put(self, event_name: str, payload: Any) -> None:
        """使用统一入口写入线程安全事件队列。"""
        self._event_queue.put((event_name, payload))


__all__ = ["AgentChatRunner"]
