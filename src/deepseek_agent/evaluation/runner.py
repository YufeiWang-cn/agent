"""在临时工作区运行真实 Agent，并汇总确定性评测结果。"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from time import monotonic
from uuid import uuid4

from ..agent import Agent
from ..runtime import AgentEvent, AgentEventType, TurnOutcome
from ..tool_execution import ToolExecutionRecord
from ..tools import Tool
from .graders import (
    AnswerGrader,
    CommandGrader,
    FileChangeGrader,
    SafetyGrader,
    TraceGrader,
)
from .models import CheckResult, EvalCase, EvalResult
from .snapshot import snapshot_workspace


AgentFactory = Callable[[Path, Path, EvalCase], Agent]


class EvalToolConfirmer:
    """按案例策略自动批准或拒绝工具，并消除交互式输入。"""

    def __init__(self, approve: bool) -> None:
        self._approve = approve

    def confirm(self, _tool: Tool, _arguments: str) -> bool:
        return self._approve


class EvalRunner:
    """负责案例隔离、Agent 执行、检查器调用和失败现场保存。"""

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        failed_workspace_directory: Path | None = None,
    ) -> None:
        self._agent_factory = agent_factory
        self._failed_workspace_directory = failed_workspace_directory
        self._file_grader = FileChangeGrader()
        self._command_grader = CommandGrader()
        self._trace_grader = TraceGrader()
        self._safety_grader = SafetyGrader()
        self._answer_grader = AnswerGrader()

    def run_cases(
        self,
        cases: Iterable[EvalCase],
        *,
        repeat: int = 1,
    ) -> tuple[EvalResult, ...]:
        """顺序执行一组案例，避免并行模型调用和副作用互相干扰。"""
        if repeat <= 0:
            raise ValueError("repeat 必须是正整数。")
        case_values = tuple(cases)
        return tuple(
            self.run_case(case, attempt=attempt)
            for case in case_values
            for attempt in range(1, repeat + 1)
        )

    def run_case(self, case: EvalCase, *, attempt: int = 1) -> EvalResult:
        """在一次性目录中执行案例，并在失败时按需保存现场。"""
        started_at = monotonic()
        with tempfile.TemporaryDirectory(prefix=f"agent-eval-{case.id}-") as temp:
            temporary_root = Path(temp)
            workspace = temporary_root / "workspace"
            state_directory = temporary_root / "state"
            shutil.copytree(case.fixture, workspace, symlinks=True)
            state_directory.mkdir()
            before = snapshot_workspace(workspace)
            outside_directory = temporary_root / "outside"
            outside_before: dict[str, str] | None = None
            if case.outside_fixture is not None:
                shutil.copytree(case.outside_fixture, outside_directory, symlinks=True)
                outside_before = snapshot_workspace(outside_directory)

            outcome: TurnOutcome | None = None
            execution_error: str | None = None
            event_records: list[ToolExecutionRecord] = []
            agent: Agent | None = None
            try:
                agent = self._agent_factory(workspace, state_directory, case)
                outcome = agent.chat(
                    case.prompt,
                    on_event=lambda event: self._collect_record(event, event_records),
                )
            except Exception as error:
                execution_error = f"{type(error).__name__}: {error}"
                if agent is not None:
                    outcome = agent.last_turn_outcome

            after = snapshot_workspace(workspace)
            outside_after = (
                snapshot_workspace(outside_directory)
                if outside_directory.is_dir()
                else {}
                if outside_before is not None
                else None
            )
            grader_setup_error = self._inject_grader_fixture(case, workspace)
            records = self._records(outcome, event_records)
            checks = self._grade(
                before,
                after,
                workspace,
                case,
                outcome,
                records,
                execution_error,
                grader_setup_error,
                outside_before,
                outside_after,
            )
            passed = all(check.passed for check in checks if check.hard_gate)
            failed_workspace = (
                None
                if passed
                else self._preserve_failed_workspace(case, workspace, attempt)
            )
            return EvalResult(
                case_id=case.id,
                passed=passed,
                checks=checks,
                metrics=self._metrics(
                    agent,
                    outcome,
                    records,
                    monotonic() - started_at,
                ),
                error=execution_error,
                failed_workspace=(
                    str(failed_workspace) if failed_workspace is not None else None
                ),
                attempt=attempt,
            )

    def _grade(
        self,
        before: dict[str, str],
        after: dict[str, str],
        workspace: Path,
        case: EvalCase,
        outcome: TurnOutcome | None,
        records: tuple[ToolExecutionRecord, ...],
        execution_error: str | None,
        grader_setup_error: str | None,
        outside_before: dict[str, str] | None,
        outside_after: dict[str, str] | None,
    ) -> tuple[CheckResult, ...]:
        return (
            self._file_grader.grade(before, after, case),
            self._command_grader.grade(workspace, case, grader_setup_error),
            self._trace_grader.grade(outcome, records, case, execution_error),
            self._safety_grader.grade(
                records,
                case,
                outside_before,
                outside_after,
            ),
            self._answer_grader.grade(
                outcome.final_text if outcome is not None else None,
                case,
            ),
        )

    @staticmethod
    def _collect_record(
        event: AgentEvent,
        records: list[ToolExecutionRecord],
    ) -> None:
        if (
            event.type is AgentEventType.TOOL_RESULT
            and event.tool_record is not None
        ):
            records.append(event.tool_record)

    @staticmethod
    def _records(
        outcome: TurnOutcome | None,
        event_records: list[ToolExecutionRecord],
    ) -> tuple[ToolExecutionRecord, ...]:
        if outcome is not None and outcome.tool_records:
            return outcome.tool_records
        return tuple(event_records)

    def _preserve_failed_workspace(
        self,
        case: EvalCase,
        workspace: Path,
        attempt: int,
    ) -> Path | None:
        if self._failed_workspace_directory is None:
            return None
        root = self._failed_workspace_directory
        root.mkdir(parents=True, exist_ok=True)
        destination = root / f"{case.id}-attempt-{attempt}-{uuid4().hex[:8]}"
        shutil.copytree(workspace, destination, symlinks=True)
        return destination

    @staticmethod
    def _inject_grader_fixture(case: EvalCase, workspace: Path) -> str | None:
        """在 Agent 结束后注入隐藏验收文件，避免模型提前读取。"""
        if case.grader_fixture is None:
            return None
        try:
            for source in sorted(case.grader_fixture.rglob("*")):
                relative = source.relative_to(case.grader_fixture)
                destination = workspace / relative
                if source.is_symlink():
                    raise ValueError(f"隐藏验收文件不能包含符号链接：{relative}")
                if source.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if destination.exists():
                    raise FileExistsError(f"隐藏验收文件与工作区冲突：{relative}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        except (OSError, ValueError) as error:
            return f"隐藏验收文件注入失败：{error}"
        return None

    @staticmethod
    def _metrics(
        agent: Agent | None,
        outcome: TurnOutcome | None,
        records: tuple[ToolExecutionRecord, ...],
        duration_seconds: float,
    ) -> dict[str, int | float | str | None]:
        runtime = agent.runtime_metrics if agent is not None else None
        return {
            "duration_seconds": round(duration_seconds, 3),
            "steps_completed": (
                outcome.steps_completed if outcome is not None else None
            ),
            "tool_calls": len(records),
            "tool_duration_seconds": round(
                sum(record.duration_seconds for record in records),
                3,
            ),
            "model_requests": runtime.model_requests if runtime is not None else 0,
            "api_attempts": runtime.api_attempts if runtime is not None else 0,
            "retries": runtime.retries if runtime is not None else 0,
            "input_tokens": runtime.input_tokens if runtime is not None else 0,
            "output_tokens": runtime.output_tokens if runtime is not None else 0,
            "total_tokens": runtime.total_tokens if runtime is not None else 0,
            "token_source": (
                runtime.token_source if runtime is not None else "unavailable"
            ),
        }


__all__ = ["AgentFactory", "EvalRunner", "EvalToolConfirmer"]
