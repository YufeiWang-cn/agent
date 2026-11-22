"""真实 Agent/SQLite/工具链的离线多轮评测；不创建真实模型或网络连接。"""

import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from time import monotonic
from typing import Any

from ..agent import Agent
from ..config import Settings
from ..memory import JsonSessionStore, JsonProjectStore
from ..runtime import AgentEventType, TurnOutcome
from ..tools import (
    ToolRegistry,
    WebSearchTool,
    ReadWebPageTool,
    TavilySearchClient,
    TavilyExtractClient,
)
from ..tool_execution import ToolExecutionRecord
from .career_cases import CareerCase
from .career_script import CareerScriptModel, OperationConfirmer
from .career_graders import grade_turn
from .models import CheckResult, EvalResult
from .runner import EvalRunner


class OfflineTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, body: bytes, headers: Mapping[str, str], timeout_seconds: float) -> bytes:
        payload = json.loads(body.decode("utf-8"))
        self.requests.append(payload)
        results = [
            dict(url=url, raw_content="公开岗位：Python 后端实习")
            for url in payload.get("urls", [])
        ]
        return json.dumps(dict(results=results)).encode("utf-8")


class CareerEvalRunner:
    def run_case(
        self, case: CareerCase, *, attempt: int = 1, omit_state_control: bool = False
    ) -> EvalResult:
        started = monotonic()
        with tempfile.TemporaryDirectory(prefix="career-eval-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / "state"
            model = CareerScriptModel()
            confirmer = OperationConfirmer()
            transport = OfflineTransport()
            tools = ToolRegistry(
                [
                    WebSearchTool(
                        "offline", client=TavilySearchClient("offline", transport=transport)
                    ),
                    ReadWebPageTool(
                        "offline", client=TavilyExtractClient("offline", transport=transport)
                    ),
                ]
            )
            agent = Agent(
                Settings(
                    api_key="offline",
                    base_url="https://example.invalid",
                    model=model.model_name,
                    system_prompt="本次为合成材料求职评测。",
                    workspace_root=workspace,
                    agent_mode="career",
                    max_retries=0,
                ),
                model=model,
                tools=tools,
                confirmer=confirmer,
                session_store=JsonSessionStore(state / "sessions"),
                project_store=JsonProjectStore(state / "projects.json"),
            )
            checks: list[CheckResult] = []
            all_records: list[ToolExecutionRecord] = []
            observations = []
            outcome: TurnOutcome | None = None
            total_steps = 0
            for index, turn in enumerate(case.turns, 1):
                model.begin(turn["script"], turn["answer"], turn.get("fail_model", False))
                confirmer.policy = turn["approvals"]
                confirmer.decisions = []
                records: list[ToolExecutionRecord] = []

                def collect(event: Any) -> None:
                    if event.type is AgentEventType.TOOL_RESULT and event.tool_record is not None:
                        records.append(event.tool_record)

                error = None
                try:
                    outcome = agent.chat(
                        turn["prompt"],
                        on_event=collect,
                        should_cancel=lambda: turn.get("cancel_before", False),
                    )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    outcome = agent.last_turn_outcome
                snapshot = agent.career_snapshot()
                if omit_state_control:
                    snapshot = dict(facts=[], documents=[], reviews=[])
                unapproved = []
                for record in records:
                    args = (
                        json.loads(record.arguments)
                        if isinstance(record.arguments, str)
                        else record.arguments
                    )
                    # 独立于生产 confirmation 策略，避免同一规则错误让评分一起失效。
                    protected = (
                        record.tool_name in {"export_career_review", "web_search", "read_web_page"}
                        or (
                            record.tool_name == "career_memory"
                            and (args or {}).get("action") in {"save", "forget"}
                        )
                        or (
                            record.tool_name == "career_review"
                            and (args or {}).get("action") == "decide"
                        )
                    )
                    if (
                        protected
                        and record.status.succeeded
                        and record.confirmation_granted is not True
                    ):
                        unapproved.append(record.call_id)
                observed = dict(
                    status=outcome.status.value if outcome else "failed",
                    answer=outcome.final_text or "" if outcome else "",
                    error=error,
                    trace=[[r.tool_name, r.status.value] for r in records],
                    snapshot=snapshot,
                    network=list(transport.requests),
                    unapproved_writes=unapproved,
                    decisions=list(confirmer.decisions),
                )
                checks.extend(grade_turn(turn["expected"], observed, workspace, f"turn_{index}"))
                observations.append(observed)
                all_records.extend(records)
                total_steps += outcome.steps_completed if outcome else 0
            metrics: dict[str, Any] = EvalRunner._metrics(
                agent, outcome, tuple(all_records), monotonic() - started
            )
            metrics.update(
                steps_completed=total_steps,
                turns=len(case.turns),
                real_api_calls=0,
                network_requests=len(transport.requests),
                observations=observations,
                evidence_mode="offline-script",
                control="omit_state" if omit_state_control else "normal",
            )
            return EvalResult(
                case.id,
                all(c.passed for c in checks if c.hard_gate),
                tuple(checks),
                metrics,
                attempt=attempt,
            )
