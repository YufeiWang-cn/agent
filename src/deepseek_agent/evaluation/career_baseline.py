"""直接提示词基线：保留多轮对话，不实例化业务存储或提供工具。"""

from collections.abc import Callable
from dataclasses import asdict
from time import monotonic
from typing import Any

from ..config import Settings
from ..conversation import Message
from ..models import ChatModel, ReasoningDelta, TextDelta, ToolCallRequest
from ..observability import RuntimeMetrics
from ..reliability import ModelBudget, ModelBudgetExceeded, RetryPolicy, RetryingChatModel
from .career_cases import CareerCase
from .models import CheckResult, EvalResult

BASELINE_INSTRUCTION = (
    "你正在直接回答用户，没有工具、数据库、文件系统或联网能力。"
    "请根据相同的原始材料与用户补充提供可供用户手工保存的文本。"
    "遇到保存、接受、导出或搜索要求时，说明能力边界，不声称实际执行了这些操作。"
)


class CareerBaselineRunner:
    def __init__(self, budget: ModelBudget) -> None:
        self.budget = budget

    def run_case(
        self,
        case: CareerCase,
        model: ChatModel,
        settings: Settings,
        *,
        attempt: int = 1,
        on_turn: Callable[[dict[str, Any]], None] | None = None,
    ) -> EvalResult:
        started = monotonic()
        runtime = RuntimeMetrics()
        wrapped = RetryingChatModel(
            model, RetryPolicy(0), runtime, max_calls_per_turn=1, budget=self.budget
        )
        messages: list[Message] = [
            dict(role="system", content=settings.system_prompt + "\n" + BASELINE_INSTRUCTION)
        ]
        observations: list[dict[str, Any]] = []
        checks: list[CheckResult] = []
        for index, turn in enumerate(case.turns, 1):
            wrapped.begin_turn()
            messages.append(dict(role="user", content=turn["prompt"]))
            answer: list[str] = []
            reasoning: list[str] = []
            status, error, tool_requested = "completed", None, False
            events = iter(wrapped.stream(messages, []))
            try:
                for event in events:
                    if isinstance(event, TextDelta):
                        answer.append(event.content)
                    elif isinstance(event, ReasoningDelta):
                        reasoning.append(event.content)
                    elif isinstance(event, ToolCallRequest):
                        tool_requested = True
                        raise ValueError("基线不执行工具。")
            except ModelBudgetExceeded as exc:
                status, error = "budget_exhausted", str(exc)
            except KeyboardInterrupt:
                status, error = "cancelled", "用户中断。"
            except Exception as exc:
                status, error = "failed", type(exc).__name__
            finally:
                close = getattr(events, "close", None)
                if callable(close):
                    close()
            text = "".join(answer)
            assistant: Message = dict(role="assistant", content=text or "（本轮未生成完整回答）")
            if reasoning:
                assistant["reasoning_content"] = "".join(reasoning)
            messages.append(assistant)
            observed: dict[str, Any] = dict(
                status=status,
                answer=text,
                error=error,
                trace=[],
                artifacts={},
                model_budget=wrapped.budget_snapshot(),
            )
            observations.append(observed)
            checks.extend(
                (
                    CheckResult(
                        f"turn_{index}.termination",
                        status == "completed",
                        True,
                        dict(actual=status),
                    ),
                    CheckResult(f"turn_{index}.response_present", bool(text.strip()), True),
                    CheckResult(f"turn_{index}.no_tool_execution", not tool_requested, True),
                )
            )
            if on_turn is not None:
                on_turn(
                    dict(
                        case_id=case.id,
                        attempt=attempt,
                        observations=observations,
                        checks=[c.to_dict() for c in checks],
                    )
                )
            if status in {"budget_exhausted", "cancelled"}:
                break
        metrics = dict(
            asdict(runtime),
            duration_seconds=monotonic() - started,
            total_tokens=runtime.total_tokens,
            token_source=runtime.token_source,
            observations=observations,
            turns=len(observations),
            planned_turns=len(case.turns),
            model_budget=wrapped.budget_snapshot(),
            tool_calls=0,
            business_checks_applicable=False,
        )
        return EvalResult(
            case.id, all(c.passed for c in checks), tuple(checks), metrics, attempt=attempt
        )
